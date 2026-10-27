import hashlib
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from importlib import import_module
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep

from .pipeline_registry import PIPELINE_REGISTRY, PIPELINE_STAGES, PIPELINE_TIMEZONE


class StageError(Exception):
    code = "STAGE_ERROR"
    retryable = False

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class TransientStageError(StageError):
    retryable = True


class IntegrityStageError(StageError):
    code = "INTEGRITY_ERROR"


@dataclass(frozen=True)
class StageOutcome:
    status: str = JobStatus.SUCCESS
    input_count: int = 0
    output_count: int = 0
    metadata: dict = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class StageExecution:
    stage: str
    business_date: date
    status: str
    job_run_id: int | None = None
    changed: bool = False
    reason: str = ""


def _local_now(now=None):
    value = now or timezone.now()
    if timezone.is_naive(value):
        value = value.replace(tzinfo=ZoneInfo(PIPELINE_TIMEZONE))
    return value.astimezone(ZoneInfo(PIPELINE_TIMEZONE))


def _load_executor(path):
    module_name, function_name = path.rsplit(".", 1)
    return getattr(import_module(module_name), function_name)


def _lock_id(stage, business_date):
    value = f"pipeline:{business_date.isoformat()}:{stage}".encode()
    return int.from_bytes(hashlib.sha256(value).digest()[:8], "big", signed=True)


def _try_lock(stage, business_date):
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_lock_id(stage, business_date)])
        return cursor.fetchone()[0]


def _unlock(stage, business_date):
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [_lock_id(stage, business_date)])


def _successful(stage_name, business_date):
    stage = PIPELINE_REGISTRY[stage_name]
    if stage_name == "daily_digest":
        from apps.publishing.models import Digest, DigestStatus

        if Digest.objects.filter(digest_date=business_date, status=DigestStatus.READY).exists():
            return True
    return JobRun.objects.filter(
        idempotency_key=stage.idempotency_key(business_date), status=JobStatus.SUCCESS
    ).exists()


def plan_pipeline(business_date, *, now=None):
    local_now = _local_now(now)
    output = []
    for stage in PIPELINE_STAGES:
        run = JobRun.objects.filter(idempotency_key=stage.idempotency_key(business_date)).first()
        due = business_date < local_now.date() or (
            business_date == local_now.date() and local_now.time().replace(tzinfo=None) >= stage.schedule
        )
        dependencies = {name: _successful(name, business_date) for name in stage.dependencies}
        if _successful(stage.name, business_date):
            action, reason = "IDEMPOTENT_NOOP", "already successful or READY"
        elif run and run.status in {JobStatus.PAUSED, JobStatus.PAUSED_BUDGET_EXCEEDED}:
            action, reason = "MANUAL_REQUIRED", run.error_message or run.status
        elif not due:
            action, reason = "NOT_DUE", "scheduled time has not arrived"
        elif not all(dependencies.values()):
            action, reason = "BLOCKED", "dependency not successful"
        else:
            action, reason = "WOULD_RUN", "schedule and dependencies satisfied"
        output.append({
            "name": stage.name,
            "job_type": stage.job_type,
            "business_date": business_date.isoformat(),
            "idempotency_key": stage.idempotency_key(business_date),
            "enabled": bool(settings.SCHEDULER_PIPELINE_ENABLED and stage.enabled),
            "schedule": stage.schedule.strftime("%H:%M"),
            "timezone": PIPELINE_TIMEZONE,
            "dependencies": dependencies,
            "timeout_seconds": stage.timeout_seconds,
            "retry_policy": asdict(stage.retry_policy),
            "mode": stage.mode,
            "action": action,
            "reason": reason,
        })
    return output


def _get_or_create_run(stage, business_date):
    key = stage.idempotency_key(business_date)
    try:
        with transaction.atomic():
            return JobRun.objects.get_or_create(
                idempotency_key=key,
                defaults={
                    "job_type": stage.job_type,
                    "status": JobStatus.RUNNING,
                    "started_at": timezone.now(),
                },
            )
    except IntegrityError:
        return JobRun.objects.get(idempotency_key=key), False


def _finish_run(run, status, error=""):
    run.status = status
    run.finished_at = timezone.now()
    run.error_message = error[:2000]
    run.save(update_fields=["status", "finished_at", "error_message"])


def execute_pipeline_stage(
    stage_name,
    business_date=None,
    *,
    executor=None,
    sleeper=time.sleep,
    random_value=random.random,
):
    if not settings.SCHEDULER_PIPELINE_ENABLED:
        return StageExecution(stage_name, business_date or timezone.localdate(), "DISABLED", reason="pipeline disabled")
    stage = PIPELINE_REGISTRY[stage_name]
    business_date = business_date or _local_now().date()
    if not stage.enabled:
        return StageExecution(stage_name, business_date, "DISABLED", reason="stage disabled")
    if _successful(stage_name, business_date):
        run = JobRun.objects.filter(idempotency_key=stage.idempotency_key(business_date)).first()
        return StageExecution(stage_name, business_date, JobStatus.SUCCESS, run.pk if run else None, False, "idempotent no-op")
    if not _try_lock(stage_name, business_date):
        return StageExecution(stage_name, business_date, "LOCKED", reason="another scheduler owns the stage lock")
    try:
        if _successful(stage_name, business_date):
            run = JobRun.objects.filter(idempotency_key=stage.idempotency_key(business_date)).first()
            return StageExecution(stage_name, business_date, JobStatus.SUCCESS, run.pk if run else None, False, "idempotent no-op")
        missing = [name for name in stage.dependencies if not _successful(name, business_date)]
        run, created = _get_or_create_run(stage, business_date)
        if not created and run.status == JobStatus.SUCCESS:
            return StageExecution(stage_name, business_date, JobStatus.SUCCESS, run.pk, False, "idempotent no-op")
        if missing:
            error = f"Blocked by dependencies: {', '.join(missing)}"
            _finish_run(run, JobStatus.PAUSED, error)
            return StageExecution(stage_name, business_date, JobStatus.PAUSED, run.pk, created, error)

        run.status = JobStatus.RUNNING
        run.started_at = run.started_at or timezone.now()
        run.finished_at = None
        run.error_message = ""
        run.save(update_fields=["status", "started_at", "finished_at", "error_message"])
        execute = executor or _load_executor(stage.executor)
        policy = stage.retry_policy
        for attempt in range(1, policy.max_attempts + 1):
            step = JobStep.objects.create(
                job_run=run,
                step_name=f"attempt_{run.steps.count() + 1}",
                status=JobStatus.RUNNING,
                started_at=timezone.now(),
                input_summary={
                    "business_date": business_date.isoformat(),
                    "stage": stage.name,
                    "attempt": attempt,
                    "timeout_seconds": stage.timeout_seconds,
                },
            )
            try:
                outcome = execute(business_date)
                if outcome is None:
                    outcome = StageOutcome()
                step.status = outcome.status
                step.finished_at = timezone.now()
                step.output_summary = {
                    "input_count": outcome.input_count,
                    "output_count": outcome.output_count,
                    "metadata": outcome.metadata,
                }
                step.error_message = outcome.error[:2000]
                step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])
                _finish_run(run, outcome.status, outcome.error)
                return StageExecution(stage_name, business_date, outcome.status, run.pk, True, outcome.error)
            except StageError as exc:
                step.status = JobStatus.FAILED
                step.finished_at = timezone.now()
                step.error_message = str(exc)[:2000]
                step.output_summary = {"error_code": exc.code, "retryable": exc.retryable, "attempt": attempt}
                step.save(update_fields=["status", "finished_at", "error_message", "output_summary"])
                can_retry = (
                    exc.retryable
                    and exc.code in policy.retryable_codes
                    and attempt < policy.max_attempts
                )
                if not can_retry:
                    _finish_run(run, JobStatus.FAILED, str(exc))
                    return StageExecution(stage_name, business_date, JobStatus.FAILED, run.pk, True, str(exc))
                run.status = JobStatus.RETRYING
                run.error_message = str(exc)[:2000]
                run.save(update_fields=["status", "error_message"])
                delay = min(policy.max_delay_seconds, policy.base_delay_seconds * (2 ** (attempt - 1)))
                sleeper(delay + policy.jitter_seconds * random_value())
            except Exception as exc:
                step.status = JobStatus.FAILED
                step.finished_at = timezone.now()
                step.error_message = str(exc)[:2000]
                step.output_summary = {"error_code": "UNEXPECTED_ERROR", "retryable": False}
                step.save(update_fields=["status", "finished_at", "error_message", "output_summary"])
                _finish_run(run, JobStatus.FAILED, str(exc))
                return StageExecution(stage_name, business_date, JobStatus.FAILED, run.pk, True, str(exc))
    finally:
        _unlock(stage_name, business_date)


def scheduled_stage(stage_name):
    """APScheduler entry point. The global feature flag remains authoritative."""
    return execute_pipeline_stage(stage_name, _local_now().date())
