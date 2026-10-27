import hashlib
from dataclasses import dataclass
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.ai_content.costs import calculate_cost, current_pricing, estimate_tokens
from apps.ai_content.models import LLMPurpose, ModelCall, ModelCallStatus
from apps.ai_content.prompts import build_article_prompt, source_material_for_raw_item
from apps.ai_content.schemas import article_schema_for_version, parse_article_output
from apps.ai_content.services import active_prompt, call_structured_output, configured_provider
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep

from .eligibility import ArticleGenerationEligibility, evaluate_article_generation_eligibility
from .content_gate import persist_content_gate
from .models import Article, ArticleStatus, ArticleVersion, ArticleVersionClaim


JOB_TYPE = "ARTICLE_GENERATION"
DRY_RUN_JOB_TYPE = "ARTICLE_GENERATION_DRY_RUN"
STEPS = (
    "check_eligibility", "prepare_input", "llm_generate",
    "validate_output", "create_article", "link_claims",
    "validate_grounding", "validate_quality",
)


class ArticleGenerationError(Exception):
    pass


class ArticleGenerationRunning(ArticleGenerationError):
    pass


class ArticleGenerationRetryRequired(ArticleGenerationError):
    pass


@dataclass(frozen=True)
class ArticleGenerationResult:
    job_run: JobRun
    eligibility: ArticleGenerationEligibility
    article: Article | None = None
    article_version: ArticleVersion | None = None
    model_call: ModelCall | None = None
    dry_run: bool = False
    reused: bool = False
    estimated_input_tokens: int | None = None
    estimated_cost: object | None = None
    content_validation: object | None = None


def _start(step):
    step.status = JobStatus.RUNNING
    step.started_at = timezone.now()
    step.finished_at = None
    step.error_message = ""
    step.save(update_fields=["status", "started_at", "finished_at", "error_message"])


def _complete(step, output):
    step.status = JobStatus.SUCCESS
    step.finished_at = timezone.now()
    step.output_summary = output
    step.error_message = ""
    step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])


def _skip(step, reason_code):
    step.status = JobStatus.CANCELLED
    step.finished_at = timezone.now()
    step.output_summary = {"skipped": True, "reason_code": reason_code}
    step.save(update_fields=["status", "finished_at", "output_summary"])


def _finish(run):
    run.status = JobStatus.SUCCESS
    run.finished_at = timezone.now()
    run.error_message = ""
    run.save(update_fields=["status", "finished_at", "error_message"])


def _fail(run, step, exc):
    now = timezone.now()
    message = str(exc)[:2000]
    if step is not None:
        step.status = JobStatus.FAILED
        step.finished_at = now
        step.error_message = message
        step.save(update_fields=["status", "finished_at", "error_message"])
    run.status = JobStatus.FAILED
    run.finished_at = now
    run.error_message = message
    run.save(update_fields=["status", "finished_at", "error_message"])
    for pending in run.steps.filter(status=JobStatus.PENDING):
        _skip(pending, "UPSTREAM_FAILURE")
    AuditLog.objects.create(
        action="ARTICLE_GENERATION_FAILED", object_type="Event",
        object_id=str(run.steps.first().input_summary.get("event_id", "")),
        after_data={
            "job_run_id": run.pk, "failed_step": step.step_name if step else None,
            "reason": exc.__class__.__name__,
        },
    )


def _new_run(event_id, *, dry_run, key):
    run = JobRun.objects.create(
        job_type=DRY_RUN_JOB_TYPE if dry_run else JOB_TYPE,
        status=JobStatus.RUNNING, started_at=timezone.now(), idempotency_key=key,
    )
    JobStep.objects.bulk_create([
        JobStep(job_run=run, step_name=name, input_summary={"event_id": event_id, "dry_run": dry_run})
        for name in STEPS
    ])
    return run


def _advisory_xact_lock(event_id):
    if connection.vendor == "postgresql":
        digest = hashlib.sha256(f"article-generation:{event_id}".encode()).digest()
        lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def _acquire(event_id, *, retry):
    base = f"article-generation:event:{event_id}"
    with transaction.atomic():
        _advisory_xact_lock(event_id)
        article = Article.objects.filter(event_id=event_id).prefetch_related("versions").first()
        runs = JobRun.objects.filter(job_type=JOB_TYPE).filter(
            Q(idempotency_key=base) | Q(idempotency_key__startswith=f"{base}:")
        )
        running = runs.filter(status=JobStatus.RUNNING).first()
        if running:
            raise ArticleGenerationRunning("Article generation is already running")
        successful = runs.filter(status=JobStatus.SUCCESS).order_by("-created_at", "-pk").first()
        version = None
        if article:
            version = article.current_version or article.versions.order_by("-version_number").first()
        if article and version:
            if successful:
                return successful, article, version, True
            run = _new_run(event_id, dry_run=False, key=base if not runs.exists() else f"{base}:existing:{uuid4().hex}")
            check = run.steps.get(step_name="check_eligibility")
            _start(check)
            _complete(check, {"event_id": event_id, "eligible": True, "reason_code": "ARTICLE_ALREADY_EXISTS"})
            for name in STEPS[1:]:
                _skip(run.steps.get(step_name=name), "ARTICLE_ALREADY_EXISTS")
            _finish(run)
            return run, article, version, True
        if article:
            raise ArticleGenerationError("Existing Article has no ArticleVersion; manual recovery is required")
        failed = runs.filter(status=JobStatus.FAILED).order_by("-created_at", "-pk").first()
        if failed and not retry and not article:
            raise ArticleGenerationRetryRequired("Generation failed previously; pass --retry")
        if retry and not failed:
            raise ArticleGenerationError("No failed generation is available to retry")
        key = base if not runs.exists() else f"{base}:retry:{failed.pk}:{uuid4().hex}"
        run = _new_run(event_id, dry_run=False, key=key)
        if retry:
            AuditLog.objects.create(
                action="ARTICLE_GENERATION_RETRY", object_type="Event", object_id=str(event_id),
                after_data={"job_run_id": run.pk, "source_job_run_id": failed.pk},
            )
        return run, article, version, bool(article and version)


def _context(event, eligibility):
    claims = list(Claim.objects.filter(pk__in=eligibility.eligible_claim_ids, event=event).order_by("pk"))
    evidence = list(
        ClaimEvidence.objects.filter(
            pk__in=eligibility.eligible_evidence_ids,
            claim_id__in=eligibility.eligible_claim_ids,
            stance=EvidenceStance.SUPPORTS,
        ).select_related("raw_item__source", "source", "claim").order_by("pk")
    )
    return {
        "verified_claims": [
            {"claim_id": claim.pk, "statement": claim.statement, "claim_type": claim.claim_type}
            for claim in claims
        ],
        "supporting_evidence": [
            {
                "evidence_id": item.pk, "claim_id": item.claim_id,
                "quote": item.evidence_text, "source_name": item.source.name,
                "source_url": item.raw_item.url,
            }
            for item in evidence
        ],
        "source_material": [
            {
                **source_material_for_raw_item(item.raw_item),
                "untrusted": True,
            }
            for item in evidence
        ],
    }


def _validate_grounding(event, output):
    eligibility = evaluate_article_generation_eligibility(event)
    if not eligibility.eligible:
        raise ValidationError(f"Eligibility changed: {eligibility.reason_code}")
    selected = set(output["claim_ids"])
    allowed = set(eligibility.eligible_claim_ids)
    if not selected or not selected <= allowed:
        raise ValidationError("Output claim_ids are not eligible for this Event")
    claims = list(Claim.objects.filter(pk__in=selected, event=event))
    if {claim.pk for claim in claims} != selected:
        raise ValidationError("Output contains a missing or cross-Event Claim")
    eligible_evidence = set(eligibility.eligible_evidence_ids)
    for claim in claims:
        if not claim.evidence.filter(pk__in=eligible_evidence, stance=EvidenceStance.SUPPORTS).exists():
            raise ValidationError(f"Claim {claim.pk} has no valid SUPPORTS Evidence")
    return eligibility, claims


def _mark_model_validation_failure(call, exc):
    call.status = ModelCallStatus.FAILED
    call.error_code = "ARTICLE_GROUNDING_FAILED"
    call.error_message = str(exc)[:2000]
    call.completed_at = timezone.now()
    call.save(update_fields=["status", "error_code", "error_message", "completed_at"])


def _create_article(event, output, claims, eligibility, call, run):
    with transaction.atomic():
        event.__class__.objects.select_for_update().get(pk=event.pk)
        current, current_claims = _validate_grounding(event, output)
        if current != eligibility or {claim.pk for claim in current_claims} != {claim.pk for claim in claims}:
            raise ValidationError("Grounding inputs changed before Article creation")
        article = Article.objects.create(event=event, status=ArticleStatus.DRAFT)
        evidence = ClaimEvidence.objects.filter(
            pk__in=current.eligible_evidence_ids, claim_id__in=output["claim_ids"]
        ).select_related("source", "raw_item").order_by("pk")
        sources = []
        for item in evidence:
            label = f"{item.source.name}: {item.raw_item.url}"
            if label not in sources:
                sources.append(label)
        version = ArticleVersion.objects.create(
            article=article, version_number=1, title=output["title"],
            one_sentence_summary=output["summary"], what_happened=output["what_happened"],
            why_it_matters=output["why_it_matters"], who_is_affected=output["who_is_affected"],
            example=output["example"], source_note="\n".join(sources), language="zh-CN",
            status=ArticleStatus.DRAFT,
        )
        links = [
            ArticleVersionClaim(article_version=version, claim=claim, paragraph_key="article", display_order=index)
            for index, claim in enumerate(claims, 1)
        ]
        ArticleVersionClaim.objects.bulk_create(links)
        article.current_version = version
        article.save(update_fields=["current_version", "updated_at"])
        common = {
            "event_id": event.pk, "article_id": article.pk, "article_version_id": version.pk,
            "claim_ids": output["claim_ids"], "model_call_id": call.pk,
            "prompt_version": call.prompt_version, "schema_version": call.schema_version,
            "job_run_id": run.pk, "article_status": ArticleStatus.DRAFT,
            "article_version_status": ArticleStatus.DRAFT,
        }
        AuditLog.objects.create(action="ARTICLE_CREATED", object_type="Article", object_id=str(article.pk), after_data=common)
        AuditLog.objects.create(action="ARTICLE_VERSION_CREATED", object_type="ArticleVersion", object_id=str(version.pk), after_data=common)
        AuditLog.objects.create(action="ARTICLE_VERSION_CLAIMS_LINKED", object_type="ArticleVersion", object_id=str(version.pk), after_data=common)
        gate = persist_content_gate(version, output)
        return article, version, gate


def generate_article(event_id, *, dry_run=False, retry=False, provider=None):
    eligibility = evaluate_article_generation_eligibility(event_id)
    if dry_run and retry:
        raise ArticleGenerationError("--dry-run and --retry cannot be combined")
    if not eligibility.eligible:
        run = _new_run(
            event_id, dry_run=dry_run,
            key=f"article-generation-check:event:{event_id}:{uuid4().hex}",
        )
        check = run.steps.get(step_name="check_eligibility")
        _start(check)
        _complete(check, {
            "event_id": event_id, "eligible": False,
            "reason_code": eligibility.reason_code, "reason": eligibility.reason,
        })
        for name in STEPS[1:]:
            _skip(run.steps.get(step_name=name), eligibility.reason_code)
        _finish(run)
        return ArticleGenerationResult(run, eligibility, dry_run=dry_run)

    event = Claim.objects.filter(event_id=event_id).first().event
    if dry_run:
        run = _new_run(event_id, dry_run=True, key=f"article-generation-dry-run:event:{event_id}:{uuid4().hex}")
        article = version = None
        reused = False
    else:
        run, article, version, reused = _acquire(event_id, retry=retry)
        if reused:
            gate = getattr(version, "content_validation", None)
            if gate is None:
                prior_call = ModelCall.objects.filter(
                    event_id=event_id, purpose=LLMPurpose.ARTICLE_GENERATION,
                    status=ModelCallStatus.SUCCESS,
                ).order_by("-pk").first()
                if prior_call and prior_call.raw_output:
                    prior_output = parse_article_output(
                        prior_call.raw_output, schema_version=prior_call.schema_version
                    )
                    gate = persist_content_gate(version, prior_output)
            return ArticleGenerationResult(
                run, eligibility, article, version, dry_run=False, reused=True,
                content_validation=gate,
            )

    active_step = None
    call = None
    try:
        active_step = run.steps.get(step_name="check_eligibility")
        _start(active_step)
        _complete(active_step, {
            "event_id": event_id, "eligible": True, "reason_code": eligibility.reason_code,
            "eligible_claim_ids": list(eligibility.eligible_claim_ids),
            "eligible_evidence_ids": list(eligibility.eligible_evidence_ids),
        })
        active_step = run.steps.get(step_name="prepare_input")
        _start(active_step)
        context = _context(event, eligibility)
        prompt = active_prompt(LLMPurpose.ARTICLE_GENERATION)
        prompt_pair = build_article_prompt(prompt, context)
        input_tokens = estimate_tokens("".join(prompt_pair))
        pricing = current_pricing(settings.LLM_PROVIDER, settings.LLM_MODEL)
        estimate = calculate_cost(pricing, input_tokens, settings.LLM_MAX_OUTPUT_TOKENS) if pricing else None
        _complete(active_step, {
            "claim_ids": list(eligibility.eligible_claim_ids),
            "evidence_ids": list(eligibility.eligible_evidence_ids),
            "estimated_input_tokens": input_tokens,
            "estimated_cost": str(estimate) if estimate is not None else None,
        })
        if dry_run:
            for name in STEPS[2:]:
                _skip(run.steps.get(step_name=name), "DRY_RUN")
            _finish(run)
            return ArticleGenerationResult(
                run, eligibility, dry_run=True, estimated_input_tokens=input_tokens,
                estimated_cost=estimate,
            )

        active_step = run.steps.get(step_name="llm_generate")
        _start(active_step)
        provider = provider or configured_provider()
        call, output = call_structured_output(
            event, LLMPurpose.ARTICLE_GENERATION,
            lambda raw: parse_article_output(raw, schema_version=prompt.output_schema_version),
            article_schema_for_version(prompt.output_schema_version),
            provider=provider, prompt_pair=prompt_pair,
            job_run=run, job_step=active_step, analysis_key_suffix=f":job:{run.pk}",
        )
        _complete(active_step, {"model_call_id": call.pk, "status": call.status})
        active_step = run.steps.get(step_name="validate_output")
        _start(active_step)
        try:
            current, claims = _validate_grounding(event, output)
        except ValidationError as exc:
            _mark_model_validation_failure(call, exc)
            raise
        _complete(active_step, {"claim_ids": output["claim_ids"], "reason_code": current.reason_code})
        active_step = run.steps.get(step_name="create_article")
        _start(active_step)
        article, version, gate = _create_article(event, output, claims, current, call, run)
        _complete(active_step, {"article_id": article.pk, "article_version_id": version.pk})
        active_step = run.steps.get(step_name="link_claims")
        _start(active_step)
        _complete(active_step, {"article_version_id": version.pk, "claim_ids": output["claim_ids"]})
        active_step = run.steps.get(step_name="validate_grounding")
        _start(active_step)
        _complete(active_step, {
            "content_validation_id": gate.pk,
            "status": gate.grounding_status,
            "validator_version": gate.grounding_validator_version,
            "reason_codes": gate.grounding_result.get("reason_codes", []),
        })
        active_step = run.steps.get(step_name="validate_quality")
        _start(active_step)
        _complete(active_step, {
            "content_validation_id": gate.pk,
            "status": gate.quality_status,
            "validator_version": gate.quality_validator_version,
            "failed_checks": gate.quality_result.get("failed_checks", []),
        })
        _finish(run)
        return ArticleGenerationResult(
            run, current, article, version, call, content_validation=gate
        )
    except Exception as exc:
        _fail(run, active_step, exc)
        if isinstance(exc, ArticleGenerationError):
            raise
        raise ArticleGenerationError(str(exc)) from exc
