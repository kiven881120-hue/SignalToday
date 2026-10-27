import hashlib
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.ai_content.models import ClaimCandidate
from apps.audit.models import AuditLog
from apps.events.models import Event
from apps.scoring.models import Score, ScoreStatus
from apps.scoring.refresh import invalidate_event_score, refresh_event_score

from .evaluation import ClaimEvaluationService
from .evidence_review import EvidenceReviewError, _review_reason
from .models import Claim, ClaimType, EvidenceStance, VerificationStatus


class ImportanceReviewError(Exception):
    pass


class StaleImportanceReview(ImportanceReviewError):
    code = "STALE_REVIEW_STATE"


@dataclass(frozen=True)
class ImportanceReviewResult:
    claim: Claim
    changed: bool
    correlation_key: str
    evaluation_job_run_id: int | None
    refresh_job_run_id: int | None


def _strict_importance(value, label):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100):
        raise ImportanceReviewError(f"{label} must be an integer between 0 and 100, or null")
    return value


def _correlation_key(
    claim_id, importance, claim_type, expected_importance, expected_status,
    expected_claim_type, reason, actor,
):
    identity = ":".join(map(str, (
        claim_id, importance, claim_type, expected_importance, expected_status,
        expected_claim_type, reason, getattr(actor, "pk", None),
    )))
    return "claim-importance-review:" + hashlib.sha256(identity.encode()).hexdigest()


def review_claim_importance(
    claim_id,
    *,
    importance,
    actor,
    reason,
    expected_current_importance,
    expected_current_verification_status,
    expected_current_claim_type=None,
    claim_type=None,
):
    importance = _strict_importance(importance, "importance")
    expected_current_importance = _strict_importance(
        expected_current_importance, "expected_current_importance"
    )
    try:
        reason = _review_reason(reason)
    except EvidenceReviewError as exc:
        raise ImportanceReviewError(str(exc)) from exc
    if expected_current_verification_status not in VerificationStatus.values:
        raise ImportanceReviewError("expected_current_verification_status is invalid")
    if actor is None or getattr(actor, "pk", None) is None:
        raise ImportanceReviewError("A persisted human actor is required")
    if expected_current_claim_type is not None and expected_current_claim_type not in ClaimType.values:
        raise ImportanceReviewError("expected_current_claim_type is invalid")
    if claim_type is not None and claim_type not in ClaimType.values:
        raise ImportanceReviewError("claim_type is invalid")

    initial = Claim.objects.filter(pk=claim_id).values("event_id", "claim_type").first()
    if initial is None:
        raise ImportanceReviewError("Claim does not exist")
    target_type = claim_type if claim_type is not None else initial["claim_type"]
    correlation_key = _correlation_key(
        claim_id, importance, target_type, expected_current_importance,
        expected_current_verification_status, expected_current_claim_type, reason, actor,
    )

    changed = False
    with transaction.atomic():
        Event.objects.select_for_update().get(pk=initial["event_id"])
        claim = Claim.objects.select_for_update().get(pk=claim_id)

        prior_audit = AuditLog.objects.filter(
            action="CLAIM_IMPORTANCE_REVIEWED",
            object_type="Claim",
            object_id=str(claim_id),
            after_data__correlation_key=correlation_key,
        ).first()
        if prior_audit and claim.importance == importance and claim.claim_type == target_type:
            incomplete_score = Score.objects.select_for_update().filter(
                event_id=claim.event_id,
                rule_version__active=True,
                status=ScoreStatus.INVALID,
            ).first()
            if incomplete_score is None:
                return ImportanceReviewResult(claim, False, correlation_key, None, None)
            invalidation = invalidate_event_score(
                claim.event_id, f"resume claim importance review: {correlation_key}"
            )
            audit = prior_audit
        else:
            if (
                claim.importance != expected_current_importance
                or claim.verification_status != expected_current_verification_status
                or (
                    expected_current_claim_type is not None
                    and claim.claim_type != expected_current_claim_type
                )
            ):
                raise StaleImportanceReview("STALE_REVIEW_STATE")

            if importance and claim.verification_status != VerificationStatus.SUPPORTED:
                raise ImportanceReviewError("Nonzero importance requires a SUPPORTED Claim")
            supporting_evidence = list(
                claim.evidence.filter(stance=EvidenceStance.SUPPORTS).values_list("pk", flat=True)
            )
            if importance and not supporting_evidence:
                raise ImportanceReviewError("Nonzero importance requires SUPPORTS Evidence")

            old_importance = claim.importance
            old_type = claim.claim_type
            changed = old_importance != importance or old_type != target_type
            if not changed:
                return ImportanceReviewResult(claim, False, correlation_key, None, None)

            claim.importance = importance
            claim.claim_type = target_type
            try:
                claim.save(update_fields=["importance", "claim_type", "updated_at"])
            except ValidationError as exc:
                raise ImportanceReviewError("Claim importance review validation failed") from exc

            candidate = (
                ClaimCandidate.objects.filter(accepted_claim=claim)
                .select_related("model_call__prompt")
                .order_by("pk")
                .first()
            )
            model_call = candidate.model_call if candidate else None
            audit = AuditLog.objects.create(
                actor=actor,
                action="CLAIM_IMPORTANCE_REVIEWED",
                object_type="Claim",
                object_id=str(claim.pk),
                before_data={"importance": old_importance, "claim_type": old_type},
                after_data={
                    "claim_id": claim.pk,
                    "event_id": claim.event_id,
                    "candidate_id": candidate.pk if candidate else None,
                    "model_call_id": model_call.pk if model_call else None,
                    "prompt_version": model_call.prompt_version if model_call else None,
                    "schema_version": model_call.schema_version if model_call else None,
                    "model": model_call.model if model_call else None,
                    "old_importance": old_importance,
                    "new_importance": importance,
                    "old_type": old_type,
                    "new_type": target_type,
                    "verification_status": claim.verification_status,
                    "supporting_evidence_ids": supporting_evidence,
                    "reason": reason,
                    "correlation_key": correlation_key,
                    "timestamp": timezone.now().isoformat(),
                },
            )
            invalidation = invalidate_event_score(
                claim.event_id, f"claim importance/type review: {correlation_key}"
            )

    evaluation_job_run_id = None
    refresh_job_run_id = None
    try:
        stats, _, evaluation_run = ClaimEvaluationService().run(Claim.objects.filter(pk=claim_id))
        evaluation_job_run_id = evaluation_run.pk
        if evaluation_run.status != "SUCCESS" or stats["errors"]:
            raise ImportanceReviewError("Claim Evaluation failed after importance review")
        claim.refresh_from_db()
        if claim.importance and claim.verification_status != VerificationStatus.SUPPORTED:
            raise ImportanceReviewError("Claim lost SUPPORTED status after importance review")
        if invalidation.score is not None:
            refresh = refresh_event_score(claim.event_id)
            refresh_job_run_id = refresh.job_run.pk
        audit.after_data = {
            **audit.after_data,
            "evaluation_job_run_id": evaluation_job_run_id,
            "refresh_job_run_id": refresh_job_run_id,
            "workflow_status": "SUCCESS",
        }
        audit.save(update_fields=["after_data"])
    except Exception as exc:
        # The core transaction already committed the INVALID state. Never restore a stale Score.
        audit.after_data = {
            **audit.after_data,
            "evaluation_job_run_id": evaluation_job_run_id,
            "refresh_job_run_id": refresh_job_run_id,
            "workflow_status": "FAILED",
            "workflow_error_type": exc.__class__.__name__,
        }
        audit.save(update_fields=["after_data"])
        raise

    return ImportanceReviewResult(
        claim, changed, correlation_key, evaluation_job_run_id, refresh_job_run_id
    )
