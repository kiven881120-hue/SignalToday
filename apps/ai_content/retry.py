from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.ai_content.models import LLMPurpose, ModelCall, ModelCallStatus
from apps.ai_content.providers.base import ProviderError
from apps.audit.models import AuditLog
from apps.events.models import Event
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep

from .services import AnalysisPaused, configured_provider, retry_event_claims


class RetryNotAllowed(Exception):
    pass


@dataclass
class RetryResult:
    job_run: JobRun
    job_step: JobStep
    model_call: ModelCall | None = None
    candidates: list = field(default_factory=list)
    error: str = ""


def _finish(job, step, status, *, output=None, error=""):
    now = timezone.now()
    step.status = status
    step.finished_at = now
    step.output_summary = output or {}
    step.error_message = error
    step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])
    job.status = status
    job.finished_at = now
    job.error_message = error
    job.save(update_fields=["status", "finished_at", "error_message"])


def retry_claim_analysis(source_job_run_id, *, reason, provider=None):
    if not reason or not reason.strip():
        raise RetryNotAllowed("A retry reason is required")

    idempotency_key = f"claim-analysis-retry:job-run:{source_job_run_id}"
    with transaction.atomic():
        source_job = JobRun.objects.select_for_update().filter(pk=source_job_run_id).first()
        if not source_job:
            raise RetryNotAllowed("Source JobRun does not exist")
        existing = JobRun.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            existing_call = existing.model_calls.order_by("-pk").first()
            return RetryResult(
                job_run=existing,
                job_step=existing.steps.order_by("pk").first(),
                model_call=existing_call,
                candidates=list(existing_call.claim_candidates.all()) if existing_call else [],
            )
        if source_job.status != JobStatus.FAILED:
            raise RetryNotAllowed("Only FAILED JobRuns may be retried")
        source_calls = list(source_job.model_calls.filter(purpose=LLMPurpose.CLAIM_EXTRACTION).order_by("-pk"))
        if not source_calls or source_calls[0].status not in {ModelCallStatus.TIMEOUT, ModelCallStatus.FAILED}:
            raise RetryNotAllowed("Source JobRun has no retryable failed Claim ModelCall")
        event_ids = {call.event_id for call in source_calls if call.event_id is not None}
        if len(event_ids) != 1:
            raise RetryNotAllowed("Source JobRun must identify exactly one Event")
        event = Event.objects.get(pk=event_ids.pop())
        job = JobRun.objects.create(
            job_type="LLM_CLAIM_ANALYSIS_RETRY",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            idempotency_key=idempotency_key,
        )
        step = JobStep.objects.create(
            job_run=job,
            step_name="analyze_claims",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            input_summary={
                "purpose": LLMPurpose.CLAIM_EXTRACTION,
                "event_id": event.pk,
                "retry_of_job_run_id": source_job.pk,
            },
        )
        AuditLog.objects.create(
            action="CLAIM_ANALYSIS_RETRY_REQUESTED",
            object_type="JobRun",
            object_id=str(job.pk),
            before_data={
                "source_job_run_id": source_job.pk,
                "source_model_call_id": source_calls[0].pk,
                "source_model_call_status": source_calls[0].status,
            },
            after_data={
                "retry_job_run_id": job.pk,
                "event_id": event.pk,
                "reason": reason.strip(),
                "timestamp": timezone.now().isoformat(),
            },
        )

    try:
        provider = provider or configured_provider()
    except ProviderError as exc:
        _finish(job, step, JobStatus.PAUSED, error=str(exc))
        return RetryResult(job_run=job, job_step=step, error=str(exc))
    try:
        candidates, call = retry_event_claims(
            event,
            source_job.pk,
            provider=provider,
            job_run=job,
            job_step=step,
        )
        output = {
            "event_id": event.pk,
            "claim_candidates": len(candidates),
            "invalid_hints": sum(item.invalid_evidence_hint_count for item in candidates),
        }
        _finish(job, step, JobStatus.SUCCESS, output=output)
        return RetryResult(job_run=job, job_step=step, model_call=call, candidates=candidates)
    except AnalysisPaused as exc:
        call = job.model_calls.order_by("-pk").first()
        status = JobStatus.PAUSED_BUDGET_EXCEEDED if call and call.status == ModelCallStatus.PAUSED_BUDGET_EXCEEDED else JobStatus.PAUSED
        _finish(job, step, status, error=str(exc))
        return RetryResult(job_run=job, job_step=step, model_call=call, error=str(exc))
    except (ProviderError, ValidationError) as exc:
        call = job.model_calls.order_by("-pk").first()
        _finish(job, step, JobStatus.FAILED, error=str(exc))
        return RetryResult(job_run=job, job_step=step, model_call=call, error=str(exc))
