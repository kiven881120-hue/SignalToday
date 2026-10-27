import hashlib
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import connection, models, transaction
from django.utils import timezone

from apps.ai_content.costs import calculate_cost, current_pricing, estimate_tokens
from apps.ai_content.models import LLMPurpose, ModelCall, ModelCallStatus
from apps.ai_content.prompts import build_article_prompt
from apps.ai_content.schemas import article_schema_for_version, parse_article_output
from apps.ai_content.services import active_prompt, call_structured_output, configured_provider
from apps.audit.models import AuditLog
from apps.claims.models import ClaimEvidence
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.reviews.models import Review, ReviewDecision

from .content_gate import persist_content_gate
from .eligibility import evaluate_article_generation_eligibility
from .generation import _context, _validate_grounding
from .models import Article, ArticleStatus, ArticleVersion, ArticleVersionClaim


JOB_TYPE = "ARTICLE_REGENERATION"
STEPS = (
    "load_source_version", "prepare_input", "llm_generate", "validate_output",
    "create_version", "link_claims", "validate_grounding", "validate_quality",
    "submit_review",
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class ArticleRegenerationError(Exception):
    def __init__(self, message, code="ARTICLE_REGENERATION_FAILED"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ArticleRegenerationResult:
    job_run: JobRun
    source_version: ArticleVersion
    target_version: ArticleVersion
    model_call: ModelCall
    content_validation: object
    review: Review | None
    reused: bool = False


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


def _fail(run, step, exc, *, event_id, actor):
    now = timezone.now()
    message = str(exc)[:2000]
    code = getattr(exc, "code", exc.__class__.__name__)
    if step is not None:
        step.status = JobStatus.FAILED
        step.finished_at = now
        step.error_message = message
        step.save(update_fields=["status", "finished_at", "error_message"])
    for pending in run.steps.filter(status=JobStatus.PENDING):
        _skip(pending, code)
    run.status = JobStatus.FAILED
    run.finished_at = now
    run.error_message = message
    run.save(update_fields=["status", "finished_at", "error_message"])
    AuditLog.objects.create(
        actor=actor, action="ARTICLE_REGENERATION_FAILED", object_type="Event",
        object_id=str(event_id),
        after_data={"job_run_id": run.pk, "reason_code": code},
    )


def _validate_request(actor, reason, request_id):
    if actor is None or not getattr(actor, "pk", None) or not actor.is_active:
        raise ArticleRegenerationError(
            "A persistent active human actor is required",
            "ARTICLE_REGENERATION_ACTOR_REQUIRED",
        )
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise ArticleRegenerationError(
            "A regeneration reason of at most 500 characters is required",
            "ARTICLE_REGENERATION_REASON_INVALID",
        )
    request_id = str(request_id or "").strip()
    if not _REQUEST_ID.fullmatch(request_id):
        raise ArticleRegenerationError(
            "request_id must be 1-100 safe identifier characters",
            "ARTICLE_REGENERATION_REQUEST_ID_INVALID",
        )
    return reason, request_id


def _lock_event(event_id):
    if connection.vendor == "postgresql":
        digest = hashlib.sha256(f"article-regeneration:{event_id}".encode()).digest()
        lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def _new_run(article, source, actor, reason, request_id):
    key = f"article-regeneration:event:{article.event_id}:request:{request_id}"
    run = JobRun.objects.create(
        job_type=JOB_TYPE, status=JobStatus.RUNNING,
        started_at=timezone.now(), idempotency_key=key,
    )
    common = {
        "event_id": article.event_id, "article_id": article.pk,
        "source_version_id": source.pk, "actor_id": actor.pk,
        "reason": reason, "request_id": request_id,
    }
    JobStep.objects.bulk_create([
        JobStep(job_run=run, step_name=name, input_summary=common) for name in STEPS
    ])
    return run


def _result_for(run, *, reused):
    audit = AuditLog.objects.filter(
        action="ARTICLE_VERSION_REGENERATED",
        after_data__job_run_id=run.pk,
    ).order_by("-pk").first()
    if audit is None:
        raise ArticleRegenerationError(
            "Successful regeneration has no target audit",
            "ARTICLE_REGENERATION_AUDIT_MISSING",
        )
    source = ArticleVersion.objects.get(pk=audit.after_data["source_version_id"])
    target = ArticleVersion.objects.get(pk=audit.after_data["target_version_id"])
    call = ModelCall.objects.get(pk=audit.after_data["model_call_id"])
    review = target.reviews.filter(decision=ReviewDecision.PENDING).order_by("-pk").first()
    return ArticleRegenerationResult(
        run, source, target, call, target.content_validation, review, reused=reused,
    )


@transaction.atomic
def _acquire(event_id, actor, reason, request_id):
    _lock_event(event_id)
    article = (
        Article.objects.select_for_update().select_related("event")
        .filter(event_id=event_id).first()
    )
    if article is None or article.current_version_id is None:
        raise ArticleRegenerationError(
            "Existing Article and current ArticleVersion are required",
            "ARTICLE_REGENERATION_SOURCE_MISSING",
        )
    key = f"article-regeneration:event:{event_id}:request:{request_id}"
    existing = JobRun.objects.filter(job_type=JOB_TYPE, idempotency_key=key).first()
    if existing:
        first_input = existing.steps.order_by("pk").values_list("input_summary", flat=True).first() or {}
        if first_input.get("actor_id") != actor.pk or first_input.get("reason") != reason:
            raise ArticleRegenerationError(
                "request_id is already bound to different request data",
                "ARTICLE_REGENERATION_REQUEST_CONFLICT",
            )
        if existing.status == JobStatus.SUCCESS:
            return article, article.current_version, existing, True
        if existing.status == JobStatus.RUNNING:
            raise ArticleRegenerationError(
                "Regeneration request is already running", "ARTICLE_REGENERATION_RUNNING"
            )
        raise ArticleRegenerationError(
            "Failed request_id is immutable; use a new explicit request_id",
            "ARTICLE_REGENERATION_PREVIOUSLY_FAILED",
        )
    running = JobRun.objects.filter(
        job_type=JOB_TYPE, status=JobStatus.RUNNING,
        idempotency_key__startswith=f"article-regeneration:event:{event_id}:request:",
    ).exists()
    if running:
        raise ArticleRegenerationError(
            "Another regeneration for this Event is running",
            "ARTICLE_REGENERATION_RUNNING",
        )
    source = ArticleVersion.objects.get(pk=article.current_version_id)
    return article, source, _new_run(article, source, actor, reason, request_id), False


def _source_note(eligibility, claim_ids):
    evidence = ClaimEvidence.objects.filter(
        pk__in=eligibility.eligible_evidence_ids, claim_id__in=claim_ids,
    ).select_related("source", "raw_item").order_by("pk")
    values = []
    for item in evidence:
        label = f"{item.source.name}: {item.raw_item.url}"
        if label not in values:
            values.append(label)
    return "\n".join(values)


def _create_target(article, source, output, claims, eligibility, actor, reason, request_id, call, run):
    locked_article = Article.objects.select_for_update().get(pk=article.pk)
    locked_source = ArticleVersion.objects.select_for_update().get(pk=source.pk)
    if locked_article.current_version_id != locked_source.pk:
        raise ArticleRegenerationError(
            "Current ArticleVersion changed during regeneration",
            "ARTICLE_REGENERATION_SOURCE_CHANGED",
        )
    version_number = (
        locked_article.versions.aggregate(maximum=models.Max("version_number"))["maximum"] or 0
    ) + 1
    target = ArticleVersion.objects.create(
        article=locked_article, version_number=version_number,
        title=output["title"], one_sentence_summary=output["summary"],
        what_happened=output["what_happened"], why_it_matters=output["why_it_matters"],
        who_is_affected=output["who_is_affected"], example=output["example"],
        source_note=_source_note(eligibility, output["claim_ids"]),
        language="zh-CN", status=ArticleStatus.DRAFT, created_by=actor,
    )
    ArticleVersionClaim.objects.bulk_create([
        ArticleVersionClaim(
            article_version=target, claim=claim,
            paragraph_key="article", display_order=index,
        )
        for index, claim in enumerate(claims, 1)
    ])
    gate = persist_content_gate(target, output)
    review = None
    passed = gate.grounding_status == "PASS"
    if passed:
        target.status = ArticleStatus.IN_REVIEW
        target.save(update_fields=["status"])
        review = Review.objects.create(
            article_version=target, reviewer=actor, risk_level=locked_article.risk_level,
            decision=ReviewDecision.PENDING, comment=reason,
        )
    source_is_published = (
        locked_article.published_version_id == locked_source.pk
        and locked_source.status == ArticleStatus.PUBLISHED
    )
    if not source_is_published:
        locked_article.current_version = target
        locked_article.status = ArticleStatus.IN_REVIEW if passed else ArticleStatus.DRAFT
        locked_article.save(update_fields=["current_version", "status", "updated_at"])
    AuditLog.objects.create(
        actor=actor, action="ARTICLE_VERSION_REGENERATED",
        object_type="ArticleVersion", object_id=str(target.pk),
        before_data={"source_version_id": locked_source.pk},
        after_data={
            "event_id": locked_article.event_id, "article_id": locked_article.pk,
            "source_version_id": locked_source.pk, "target_version_id": target.pk,
            "request_id": request_id, "reason": reason, "actor_id": actor.pk,
            "job_run_id": run.pk, "model_call_id": call.pk,
            "prompt_version": call.prompt_version, "schema_version": call.schema_version,
            "content_validation_id": gate.pk, "grounding_status": gate.grounding_status,
            "quality_status": gate.quality_status,
            "review_id": review.pk if review else None,
            "published_version_preserved": source_is_published,
            "timestamp": timezone.now().isoformat(),
        },
    )
    return target, gate, review, source_is_published


def regenerate_article(event_id, *, actor, reason, request_id, provider=None):
    reason, request_id = _validate_request(actor, reason, request_id)
    article, source, run, reused = _acquire(event_id, actor, reason, request_id)
    if reused:
        return _result_for(run, reused=True)
    active_step = None
    try:
        active_step = run.steps.get(step_name="load_source_version")
        _start(active_step)
        eligibility = evaluate_article_generation_eligibility(event_id)
        if not eligibility.eligible:
            raise ArticleRegenerationError(
                f"Event is not eligible: {eligibility.reason_code}",
                "ARTICLE_REGENERATION_INELIGIBLE",
            )
        _complete(active_step, {
            "article_id": article.pk, "source_version_id": source.pk,
            "source_version_number": source.version_number,
            "source_status": source.status,
        })

        active_step = run.steps.get(step_name="prepare_input")
        _start(active_step)
        context = _context(article.event, eligibility)
        prompt = active_prompt(LLMPurpose.ARTICLE_GENERATION)
        prompt_pair = build_article_prompt(prompt, context)
        input_tokens = estimate_tokens("".join(prompt_pair))
        pricing = current_pricing(settings.LLM_PROVIDER, settings.LLM_MODEL)
        estimate = calculate_cost(pricing, input_tokens, settings.LLM_MAX_OUTPUT_TOKENS) if pricing else None
        _complete(active_step, {
            "prompt_version": prompt.version,
            "schema_version": prompt.output_schema_version,
            "eligible_claim_ids": list(eligibility.eligible_claim_ids),
            "eligible_evidence_ids": list(eligibility.eligible_evidence_ids),
            "estimated_input_tokens": input_tokens,
            "estimated_cost": str(estimate) if estimate is not None else None,
        })

        active_step = run.steps.get(step_name="llm_generate")
        _start(active_step)
        call, output = call_structured_output(
            article.event, LLMPurpose.ARTICLE_GENERATION,
            lambda raw: parse_article_output(raw, schema_version=prompt.output_schema_version),
            article_schema_for_version(prompt.output_schema_version),
            provider=provider or configured_provider(), prompt_pair=prompt_pair,
            job_run=run, job_step=active_step,
            analysis_key_suffix=f":regeneration-request:{request_id}:job:{run.pk}",
            allow_repair=False,
        )
        _complete(active_step, {"model_call_id": call.pk, "status": call.status})

        active_step = run.steps.get(step_name="validate_output")
        _start(active_step)
        current, claims = _validate_grounding(article.event, output)
        _complete(active_step, {
            "schema_version": call.schema_version, "claim_ids": output["claim_ids"],
            "eligibility_reason": current.reason_code,
        })

        active_step = run.steps.get(step_name="create_version")
        _start(active_step)
        with transaction.atomic():
            target, gate, review, published_preserved = _create_target(
                article, source, output, claims, current, actor, reason,
                request_id, call, run,
            )
            _complete(active_step, {
                "source_version_id": source.pk, "target_version_id": target.pk,
                "version_number": target.version_number, "status": target.status,
            })
            active_step = run.steps.get(step_name="link_claims")
            _start(active_step)
            _complete(active_step, {
                "target_version_id": target.pk, "claim_ids": output["claim_ids"],
            })
            active_step = run.steps.get(step_name="validate_grounding")
            _start(active_step)
            _complete(active_step, {
                "content_validation_id": gate.pk, "status": gate.grounding_status,
                "validator_version": gate.grounding_validator_version,
                "reason_codes": gate.grounding_result.get("reason_codes", []),
            })
            active_step = run.steps.get(step_name="validate_quality")
            _start(active_step)
            _complete(active_step, {
                "content_validation_id": gate.pk, "status": gate.quality_status,
                "validator_version": gate.quality_validator_version,
                "failed_checks": gate.quality_result.get("failed_checks", []),
            })
            active_step = run.steps.get(step_name="submit_review")
            _start(active_step)
            _complete(active_step, {
                "review_id": review.pk if review else None,
                "decision": review.decision if review else None,
                "blocked": review is None,
                "published_version_preserved": published_preserved,
            })
            _finish(run)
        return ArticleRegenerationResult(run, source, target, call, gate, review)
    except Exception as exc:
        _fail(run, active_step, exc, event_id=event_id, actor=actor)
        if isinstance(exc, ArticleRegenerationError):
            raise
        raise ArticleRegenerationError(str(exc)) from exc
