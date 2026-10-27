import hashlib
import logging
import random
import re
import time
from contextlib import contextmanager
from time import monotonic

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.evaluation import evaluate_claim
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.claims.normalization import normalize_claim_text, normalize_evidence_text
from apps.jobs.models import JobRun, JobStep
from apps.scoring.evaluation import get_rule_version, score_event
from apps.scoring.models import Score

from .costs import calculate_cost, check_budget, current_pricing, estimate_tokens
from .models import (
    ClaimCandidate,
    ClaimCandidateEvidenceHint,
    EvidenceHintInvalidReason,
    EvidenceHintStatus,
    LLMPurpose,
    ModelCall,
    ModelCallStatus,
    PromptVersion,
    ReviewStatus,
    ScoreSuggestion,
)
from .prompts import (
    build_analysis_prompt,
    raw_item_analysis_text,
    raw_item_body_analysis_text,
    select_source_material,
)
from .providers.base import (
    PermanentProviderError,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
)
from .providers.openai_compatible import OpenAICompatibleProvider
from .schemas import (
    CLAIM_EXTRACTION_SCHEMA,
    CLAIM_EXTRACTION_SCHEMA_V1,
    CLAIM_EXTRACTION_SCHEMA_V3,
    SCORE_SUGGESTION_SCHEMA,
    parse_claim_output,
    parse_score_output,
)


logger = logging.getLogger("signaltoday.llm")


class AnalysisPaused(Exception):
    pass


def configured_provider():
    if settings.LLM_PROVIDER != "openai-compatible":
        raise PermanentProviderError(f"Unsupported LLM_PROVIDER: {settings.LLM_PROVIDER}")
    return OpenAICompatibleProvider(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)


def active_prompt(purpose):
    prompt = PromptVersion.objects.filter(purpose=purpose, active=True).order_by("-created_at", "-pk").first()
    if not prompt:
        raise PermanentProviderError(f"No active PromptVersion for {purpose}")
    return prompt


def analysis_key(event, purpose, prompt, model):
    return f"event:{event.pk}:{purpose}:{prompt.version}:{model}"


@contextmanager
def analysis_lock(event_id, purpose):
    digest = hashlib.sha256(f"llm-analysis:{event_id}:{purpose}".encode()).digest()
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


def _set_failure(call, exc):
    if isinstance(exc, ProviderTimeout):
        call.status = ModelCallStatus.TIMEOUT
    elif isinstance(exc, ProviderRateLimited):
        call.status = ModelCallStatus.RATE_LIMITED
    elif isinstance(exc, PermanentProviderError):
        call.status = ModelCallStatus.PAUSED
    else:
        call.status = ModelCallStatus.FAILED
    call.error_code = getattr(exc, "error_code", "UNKNOWN_ERROR")
    call.error_message = str(exc)[:2000]
    call.completed_at = timezone.now()
    call.save(update_fields=["status", "error_code", "error_message", "completed_at"])


def execute_model_call(call, *, provider, system_prompt, user_prompt, schema, max_output_tokens, essential=True):
    pricing = current_pricing(call.provider, call.model)
    if not pricing:
        exc = PermanentProviderError("No active CNY pricing configured for provider/model")
        _set_failure(call, exc)
        raise exc
    input_estimate = estimate_tokens(system_prompt + user_prompt)
    estimated_cost = calculate_cost(pricing, input_estimate, max_output_tokens)
    decision = check_budget(estimated_cost, essential=essential)
    call.pricing = pricing
    call.input_tokens = input_estimate
    call.estimated_cost = estimated_cost
    if not decision.allowed:
        call.status = decision.status
        call.error_code = decision.status
        call.error_message = "Daily LLM budget limit applies to this analysis"
        call.completed_at = timezone.now()
        call.save()
        raise AnalysisPaused(call.error_message)
    if decision.warning:
        logger.warning("LLM daily budget reached 80 percent; non-essential analysis should pause")
    call.status = ModelCallStatus.RUNNING
    call.request_started_at = timezone.now()
    call.save()
    started = monotonic()
    last_error = None
    for attempt in range(settings.LLM_MAX_ATTEMPTS):
        try:
            response = provider.analyze_structured(
                model=call.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=schema,
                timeout=settings.LLM_TIMEOUT_SECONDS,
                max_output_tokens=max_output_tokens,
            )
            call.status = ModelCallStatus.SUCCESS
            call.completed_at = timezone.now()
            call.latency_ms = max(0, int((monotonic() - started) * 1000))
            call.input_tokens = response.input_tokens
            call.output_tokens = response.output_tokens
            call.total_tokens = response.input_tokens + response.output_tokens
            call.actual_cost = calculate_cost(pricing, response.input_tokens, response.output_tokens)
            call.raw_output = response.content[:20000]
            call.error_code = ""
            call.error_message = ""
            call.save()
            return response.content
        except ProviderError as exc:
            last_error = exc
            if not exc.retryable or attempt + 1 >= settings.LLM_MAX_ATTEMPTS:
                break
            time.sleep((2 ** attempt) + random.uniform(0, settings.LLM_RETRY_JITTER_SECONDS))
        except Exception as exc:
            last_error = PermanentProviderError("Unexpected provider failure")
            logger.exception("Unexpected LLM provider failure: %s", exc.__class__.__name__)
            break
    _set_failure(call, last_error)
    raise last_error


def _new_call(event, purpose, prompt, *, job_run=None, job_step=None, suffix=""):
    key = analysis_key(event, purpose, prompt, settings.LLM_MODEL) + suffix
    return ModelCall.objects.create(
        provider=settings.LLM_PROVIDER,
        model=settings.LLM_MODEL,
        purpose=purpose,
        prompt=prompt,
        prompt_version=prompt.version,
        schema_version=prompt.output_schema_version,
        analysis_key=key,
        job_run=job_run,
        job_step=job_step,
        event=event,
    )


def _call_and_validate(
    event, purpose, parser, schema, *, provider, job_run=None, job_step=None,
    analysis_key_suffix="", prompt_pair=None, allow_repair=True,
):
    prompt = active_prompt(purpose)
    key = analysis_key(event, purpose, prompt, settings.LLM_MODEL) + analysis_key_suffix
    existing = ModelCall.objects.filter(analysis_key=key, status=ModelCallStatus.SUCCESS).first()
    if existing:
        return existing, parser(existing.raw_output)
    repaired = ModelCall.objects.filter(analysis_key=key + ":repair", status=ModelCallStatus.SUCCESS).first()
    if repaired:
        return repaired, parser(repaired.raw_output)
    if ModelCall.objects.filter(analysis_key=key).exists():
        raise AnalysisPaused("Analysis task already exists without a successful reusable result")
    system_prompt, user_prompt = prompt_pair or build_analysis_prompt(event, prompt)
    call = _new_call(event, purpose, prompt, job_run=job_run, job_step=job_step, suffix=analysis_key_suffix)
    current_score = event.scores.filter(rule_version__active=True).order_by("-updated_at", "-pk").first()
    essential = not current_score or current_score.recommendation_level in {"FOCUS", "NORMAL"}
    raw = execute_model_call(
        call,
        provider=provider,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema=schema,
        max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
        essential=essential,
    )
    try:
        return call, parser(raw)
    except ValidationError as first_error:
        call.status = ModelCallStatus.FAILED
        error_codes = [item.code for item in getattr(first_error, "error_list", []) if item.code]
        call.error_code = error_codes[0] if error_codes else "SCHEMA_VALIDATION"
        call.error_message = str(first_error)[:2000]
        call.save(update_fields=["status", "error_code", "error_message"])
        if not allow_repair:
            raise
        repair = _new_call(
            event, purpose, prompt, job_run=job_run, job_step=job_step,
            suffix=analysis_key_suffix + ":repair",
        )
        repair_prompt = (
            "Repair the following untrusted model output into JSON matching the schema. "
            "Do not add facts and return JSON only.\n<untrusted_output>"
            + raw[:10000]
            + "</untrusted_output>"
        )
        repaired = execute_model_call(
            repair,
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=repair_prompt,
            schema=schema,
            max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
        )
        try:
            return repair, parser(repaired)
        except ValidationError as repair_error:
            repair.status = ModelCallStatus.FAILED
            repair.error_code = "SCHEMA_REPAIR_FAILED"
            repair.error_message = str(repair_error)[:2000]
            repair.save(update_fields=["status", "error_code", "error_message"])
            raise


def call_structured_output(
    event, purpose, parser, schema, *, provider, prompt_pair, job_run=None,
    job_step=None, analysis_key_suffix="", allow_repair=True,
):
    """Use the shared ModelCall, budget, provider retry, and one-repair path."""
    return _call_and_validate(
        event, purpose, parser, schema, provider=provider, job_run=job_run,
        job_step=job_step, analysis_key_suffix=analysis_key_suffix,
        prompt_pair=prompt_pair, allow_repair=allow_repair,
    )


def _critical_tokens(value):
    normalized = normalize_evidence_text(value).casefold()
    numbers = re.findall(r"\d+(?:[.,]\d+)*(?:%|万|亿|千|百)?", normalized)
    model_tokens = re.findall(r"(?=[a-z0-9._/-]*[a-z])(?=[a-z0-9._/-]*\d)[a-z0-9][a-z0-9._/-]*", normalized)
    return set(numbers + model_tokens)


def validate_evidence_hint(event, claim_text, hint, *, body_only=False):
    raw_item = event.raw_items.select_related("source").filter(pk=hint["raw_item_id"]).first()
    if not raw_item:
        return None, EvidenceHintStatus.INVALID, EvidenceHintInvalidReason.RAW_ITEM_NOT_IN_EVENT
    quote = normalize_evidence_text(hint["quote"])
    if len(quote) > 2000:
        return raw_item, EvidenceHintStatus.INVALID, EvidenceHintInvalidReason.QUOTE_TOO_LONG
    source_text = raw_item_body_analysis_text(raw_item) if body_only else raw_item_analysis_text(raw_item)
    material = normalize_evidence_text(source_text)
    if quote.casefold() not in material.casefold():
        return raw_item, EvidenceHintStatus.INVALID, EvidenceHintInvalidReason.QUOTE_NOT_FOUND
    quote_folded = quote.casefold()
    if any(token not in quote_folded for token in _critical_tokens(claim_text)):
        return raw_item, EvidenceHintStatus.INVALID, EvidenceHintInvalidReason.CRITICAL_TOKEN_MISMATCH
    return raw_item, EvidenceHintStatus.VALID, ""


def _deduplicate_candidates(candidates):
    grouped = {}
    for candidate in candidates:
        normalized = normalize_claim_text(candidate["claim_text"])
        current = grouped.get(normalized)
        if current is None:
            grouped[normalized] = {**candidate, "evidence_hints": list(candidate["evidence_hints"])}
            continue
        current["confidence"] = max(current["confidence"], candidate["confidence"])
        known = {(hint["raw_item_id"], normalize_evidence_text(hint["quote"])) for hint in current["evidence_hints"]}
        for hint in candidate["evidence_hints"]:
            key = (hint["raw_item_id"], normalize_evidence_text(hint["quote"]))
            if key not in known:
                current["evidence_hints"].append(hint)
                known.add(key)
    return grouped


def _analyze_event_claims(event, *, provider=None, job_run=None, job_step=None, analysis_key_suffix=""):
    provider = provider or configured_provider()
    with analysis_lock(event.pk, LLMPurpose.CLAIM_EXTRACTION):
        locked = event.__class__.objects.get(pk=event.pk)
        prompt = active_prompt(LLMPurpose.CLAIM_EXTRACTION)
        schemas = {
            "claim-extraction-v1": CLAIM_EXTRACTION_SCHEMA_V1,
            "claim-extraction-v2": CLAIM_EXTRACTION_SCHEMA,
            "claim-extraction-v3": CLAIM_EXTRACTION_SCHEMA_V3,
        }
        schema = schemas.get(prompt.output_schema_version)
        if schema is None:
            raise PermanentProviderError("Unsupported Claim Extraction schema version")
        if prompt.version == "v1.4" and not select_source_material(locked, prompt):
            return [], None
        call, candidates = _call_and_validate(
            locked,
            LLMPurpose.CLAIM_EXTRACTION,
            lambda raw: parse_claim_output(raw, schema_version=prompt.output_schema_version),
            schema,
            provider=provider,
            job_run=job_run,
            job_step=job_step,
            analysis_key_suffix=analysis_key_suffix,
        )
        grouped = _deduplicate_candidates(candidates)
        single_source = locked.raw_items.values("source_id").distinct().count() == 1
        with transaction.atomic():
            objects = []
            for normalized, candidate in grouped.items():
                hints = candidate.pop("evidence_hints")
                obj, created = ClaimCandidate.objects.get_or_create(
                    event=locked,
                    normalized_claim=normalized,
                    defaults={
                        **candidate,
                        "model_call": call,
                        "raw_output": call.raw_output,
                        "single_source_event": single_source,
                    },
                )
                for hint in hints:
                    raw_item, status, invalid_reason = validate_evidence_hint(
                        locked,
                        obj.claim_text,
                        hint,
                        body_only=prompt.version == "v1.4",
                    )
                    # A Candidate has one current VALID Hint. Additional
                    # independently valid hints remain append-only history but
                    # are not current review evidence.
                    is_current = status == EvidenceHintStatus.VALID and not obj.evidence_hints.filter(
                        status=EvidenceHintStatus.VALID, is_current=True
                    ).exists()
                    ClaimCandidateEvidenceHint.objects.get_or_create(
                        candidate=obj,
                        proposed_raw_item_id=hint["raw_item_id"],
                        normalized_quote=normalize_evidence_text(hint["quote"]),
                        defaults={
                            "raw_item": raw_item,
                            "quote": hint["quote"],
                            "hint_type": hint.get("hint_type", ""),
                            "status": status,
                            "is_current": is_current,
                            "invalid_reason": invalid_reason,
                        },
                    )
                invalid_count = obj.evidence_hints.filter(status=EvidenceHintStatus.INVALID).count()
                valid_count = obj.evidence_hints.filter(status=EvidenceHintStatus.VALID).count()
                confidence = min(obj.confidence, 0.5) if valid_count == 0 else obj.confidence
                ClaimCandidate.objects.filter(pk=obj.pk).update(
                    confidence=confidence,
                    invalid_evidence_hint_count=invalid_count,
                    single_source_event=single_source,
                )
                obj.refresh_from_db()
                obj._was_created = created
                objects.append(obj)
        return objects, call


def analyze_event_claims(event, *, provider=None, job_run=None, job_step=None):
    return _analyze_event_claims(event, provider=provider, job_run=job_run, job_step=job_step)


def retry_event_claims(event, source_job_run_id, *, provider, job_run, job_step):
    return _analyze_event_claims(
        event,
        provider=provider,
        job_run=job_run,
        job_step=job_step,
        analysis_key_suffix=f":retry-job:{source_job_run_id}",
    )


def suggest_event_score(event, *, provider=None, job_run=None, job_step=None):
    provider = provider or configured_provider()
    with analysis_lock(event.pk, LLMPurpose.SCORING_SUGGESTION):
        locked = event.__class__.objects.get(pk=event.pk)
        call, suggestion = _call_and_validate(
            locked,
            LLMPurpose.SCORING_SUGGESTION,
            parse_score_output,
            SCORE_SUGGESTION_SCHEMA,
            provider=provider,
            job_run=job_run,
            job_step=job_step,
        )
        with transaction.atomic():
            obj, _ = ScoreSuggestion.objects.get_or_create(event=locked, model_call=call, defaults=suggestion)
        return obj, call


def accept_claim_candidate(candidate, *, actor=None, reason=""):
    with transaction.atomic():
        candidate = ClaimCandidate.objects.select_for_update().get(pk=candidate.pk)
        if candidate.status == ReviewStatus.ACCEPTED and candidate.accepted_claim_id:
            return candidate.accepted_claim
        if candidate.status != ReviewStatus.PROPOSED:
            raise ValidationError("Only a PROPOSED ClaimCandidate can be accepted")
        if normalize_claim_text(candidate.claim_text) != candidate.normalized_claim:
            raise ValidationError("ClaimCandidate normalized_claim is inconsistent")
        claim = Claim.objects.filter(event=candidate.event, normalized_statement=candidate.normalized_claim).first()
        claim_created = claim is None
        if not claim:
            claim = Claim.objects.create(
                event=candidate.event,
                statement=candidate.claim_text,
                claim_type=candidate.claim_type,
                verification_status=VerificationStatus.UNVERIFIED,
                importance=None,
            )
        if claim_created:
            AuditLog.objects.create(
                actor=actor,
                action="CLAIM_CREATED_FROM_CANDIDATE",
                object_type="Claim",
                object_id=str(claim.pk),
                after_data={
                    "candidate_id": candidate.pk,
                    "claim_id": claim.pk,
                    "event_id": candidate.event_id,
                    "model_call_id": candidate.model_call_id,
                    "claim_text": candidate.claim_text,
                    "claim_type": candidate.claim_type,
                    "confidence": str(candidate.confidence),
                    "importance_hint": candidate.importance_hint,
                    "importance_reason": candidate.importance_reason,
                    "created_from": "ClaimCandidate",
                    "timestamp": timezone.now().isoformat(),
                },
            )
        for hint in candidate.evidence_hints.filter(
            status=EvidenceHintStatus.VALID, is_current=True
        ).select_related("raw_item__source"):
            evidence = ClaimEvidence.objects.filter(
                claim=claim,
                raw_item=hint.raw_item,
                normalized_evidence_text=hint.normalized_quote,
            ).first()
            if not evidence:
                evidence = ClaimEvidence(
                    claim=claim,
                    raw_item=hint.raw_item,
                    source=hint.raw_item.source,
                    evidence_text=hint.quote,
                    stance=EvidenceStance.MENTIONS,
                    captured_at=hint.raw_item.published_at or hint.raw_item.discovered_at,
                )
                evidence._audit_context = {
                    "actor": actor,
                    "candidate_id": candidate.pk,
                    "reason": "accepted_candidate_validated_hint",
                }
                evidence.save()
        evaluation = evaluate_claim(claim)
        if claim.verification_status != evaluation.status:
            claim.verification_status = evaluation.status
            claim._audit_context = {"actor": actor, "reason": evaluation.reason}
            claim.save(update_fields=["verification_status", "updated_at"])
        candidate.status = ReviewStatus.ACCEPTED
        candidate.accepted_claim = claim
        candidate._audit_context = {"actor": actor, "reason": reason}
        candidate.save(update_fields=["status", "accepted_claim"])
        return claim


def reject_claim_candidate(candidate, *, actor=None, reason="reviewer_rejected"):
    with transaction.atomic():
        candidate = ClaimCandidate.objects.select_for_update().get(pk=candidate.pk)
        if candidate.status == ReviewStatus.REJECTED:
            return
        if candidate.status != ReviewStatus.PROPOSED:
            raise ValidationError("Only a PROPOSED ClaimCandidate can be rejected")
        candidate.status = ReviewStatus.REJECTED
        candidate.review_reason = reason[:500]
        candidate._audit_context = {"actor": actor, "reason": candidate.review_reason}
        candidate.save(update_fields=["status", "review_reason"])


def accept_score_suggestion(suggestion, *, actor=None):
    with transaction.atomic():
        suggestion = ScoreSuggestion.objects.select_for_update().get(pk=suggestion.pk)
        rule = get_rule_version()
        score = Score.objects.filter(event=suggestion.event, rule_version=rule).first()
        if not score:
            score, _, _ = score_event(suggestion.event, rule)
        suggestion.status = ReviewStatus.ACCEPTED
        suggestion.accepted_score = score
        suggestion._audit_context = {"actor": actor}
        suggestion.save(update_fields=["status", "accepted_score"])
        return score


def reject_score_suggestion(suggestion, *, actor=None):
    suggestion.status = ReviewStatus.REJECTED
    suggestion._audit_context = {"actor": actor}
    suggestion.save(update_fields=["status"])
