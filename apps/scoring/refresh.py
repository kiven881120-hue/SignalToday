from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.events.models import Event
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep

from .evaluation import calculate_score, get_rule_version
from .models import Score, ScoreRuleVersion, ScoreStatus
from .rules import DIMENSIONS
from .signals import score_snapshot


REFRESH_REASON = "score refresh after claim/evidence verification"


class ScoreRefreshError(Exception):
    pass


@dataclass(frozen=True)
class ScoreInvalidationResult:
    score: Score | None
    changed: bool
    reason_code: str


@dataclass(frozen=True)
class ScoreRefreshResult:
    score: Score
    changed: bool
    job_run: JobRun
    job_step: JobStep


def invalidate_event_score(event_id, reason):
    reason = str(reason or "").strip()
    if not reason:
        raise ScoreRefreshError("A score invalidation reason is required")

    with transaction.atomic():
        event = Event.objects.select_for_update().filter(pk=event_id).first()
        if event is None:
            raise ScoreRefreshError("Event does not exist")
        try:
            rule = get_rule_version()
        except ScoreRuleVersion.DoesNotExist as exc:
            raise ScoreRefreshError("No active ScoreRuleVersion exists") from exc
        score = Score.objects.select_for_update().filter(event=event, rule_version=rule).first()
        if score is None:
            return ScoreInvalidationResult(None, False, "SCORE_NOT_FOUND")
        if score.status == ScoreStatus.INVALID:
            return ScoreInvalidationResult(score, False, "SCORE_ALREADY_INVALID")
        if score.status != ScoreStatus.CALCULATED:
            raise ScoreRefreshError(f"Unsupported Score status: {score.status}")

        score.status = ScoreStatus.INVALID
        score._audit_context = {
            "action": "EVENT_SCORE_INVALIDATED",
            "reason": reason,
        }
        score.save(update_fields=["status", "updated_at"])
        return ScoreInvalidationResult(score, True, "SCORE_INVALIDATED")


def _finish(run, step, status, *, output=None, error=""):
    now = timezone.now()
    step.status = status
    step.finished_at = now
    step.output_summary = output or {}
    step.error_message = error
    step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])
    run.status = status
    run.finished_at = now
    run.error_message = error
    run.save(update_fields=["status", "finished_at", "error_message"])


def refresh_event_score(event_id):
    if not Event.objects.filter(pk=event_id).exists():
        raise ScoreRefreshError("Event does not exist")
    try:
        rule = get_rule_version()
    except ScoreRuleVersion.DoesNotExist as exc:
        raise ScoreRefreshError("No active ScoreRuleVersion exists") from exc
    if not Score.objects.filter(event_id=event_id, rule_version=rule).exists():
        raise ScoreRefreshError("Current active Rule has no Score for this Event")

    run = JobRun.objects.create(
        job_type="EVENT_SCORE_REFRESH",
        status=JobStatus.RUNNING,
        started_at=timezone.now(),
    )
    step = JobStep.objects.create(
        job_run=run,
        step_name="refresh_event_score",
        status=JobStatus.RUNNING,
        started_at=timezone.now(),
        input_summary={"event_id": event_id, "rule_version": rule.version},
    )
    try:
        with transaction.atomic():
            event = Event.objects.select_for_update().get(pk=event_id)
            current_rule = get_rule_version()
            if current_rule.pk != rule.pk:
                raise ScoreRefreshError("Active ScoreRuleVersion changed during refresh")
            score = Score.objects.select_for_update().get(event=event, rule_version=rule)
            result = calculate_score(event, rule)
            old_snapshot = score_snapshot(score)
            new_values = {**result.score_values(), "reasons": result.reasons}
            changed = score.status != ScoreStatus.CALCULATED or any(
                getattr(score, key) != value for key, value in new_values.items()
            )
            if changed:
                for key, value in new_values.items():
                    setattr(score, key, value)
                score.status = ScoreStatus.CALCULATED
                score._audit_context = {
                    "action": "EVENT_SCORE_REFRESHED",
                    "reason": REFRESH_REASON,
                    "job_run_id": run.pk,
                }
                score.save(update_fields=[*DIMENSIONS, "reasons", "status", "updated_at"])
            new_snapshot = score_snapshot(score)
        output = {
            "event_id": event_id,
            "score_id": score.pk,
            "rule_version": rule.version,
            "changed": changed,
            "old": old_snapshot,
            "new": new_snapshot,
        }
        _finish(run, step, JobStatus.SUCCESS, output=output)
        return ScoreRefreshResult(score, changed, run, step)
    except Exception as exc:
        _finish(
            run,
            step,
            JobStatus.FAILED,
            output={"event_id": event_id, "rule_version": rule.version},
            error=str(exc)[:2000],
        )
        raise
