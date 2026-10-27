import logging
from dataclasses import asdict, dataclass
from uuid import uuid4

from django.db import transaction
from django.db.models import Case, CharField, Count, F, Q, Value, When
from django.db.models.functions import Cast, Concat
from django.utils import timezone

from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.audit.models import AuditLog
from apps.events.models import Event
from apps.scoring.refresh import invalidate_event_score

from .models import Claim, EvidenceStance, EvidenceType, VerificationStatus


RELIABLE_TIERS = ("A", "B", "C")
logger = logging.getLogger("signaltoday.claims")


@dataclass
class ClaimEvaluation:
    status: str
    supporting_evidence_count: int
    contradicting_evidence_count: int
    independent_supporting_source_count: int
    independent_contradicting_source_count: int
    reason: str

    def as_dict(self):
        return asdict(self)


def _source_identity():
    return Case(
        When(
            source__source_group="",
            then=Concat(Value("source:"), Cast("source_id", CharField())),
        ),
        default=Concat(Value("group:"), F("source__source_group")),
        output_field=CharField(),
    )


def evaluate_claim(claim):
    identity = _source_identity()
    evidence = claim.evidence.all()
    counts = evidence.aggregate(
        supports=Count("id", filter=Q(stance=EvidenceStance.SUPPORTS)),
        contradicts=Count("id", filter=Q(stance=EvidenceStance.CONTRADICTS)),
        independent_supports=Count(identity, filter=Q(stance=EvidenceStance.SUPPORTS), distinct=True),
        independent_contradicts=Count(identity, filter=Q(stance=EvidenceStance.CONTRADICTS), distinct=True),
        reliable_supports=Count(
            "id", filter=Q(stance=EvidenceStance.SUPPORTS, source__tier__in=RELIABLE_TIERS)
        ),
        independent_reliable_supports=Count(
            identity,
            filter=Q(stance=EvidenceStance.SUPPORTS, source__tier__in=RELIABLE_TIERS),
            distinct=True,
        ),
        official_supports=Count(
            "id",
            filter=Q(stance=EvidenceStance.SUPPORTS)
            & (Q(source__tier="A") | Q(evidence_type=EvidenceType.OFFICIAL)),
        ),
    )

    supports = counts["supports"]
    contradicts = counts["contradicts"]
    if claim.verification_status == VerificationStatus.REJECTED:
        status, reason = VerificationStatus.REJECTED, "Claim is manually rejected; automatic evaluation preserves this state."
    elif supports and contradicts:
        status, reason = VerificationStatus.CONFLICTED, "Both supporting and contradicting evidence exist."
    elif contradicts:
        status, reason = VerificationStatus.CONTRADICTED, "Contradicting evidence exists without supporting evidence."
    elif counts["official_supports"]:
        status, reason = VerificationStatus.SUPPORTED, "At least one official or tier-A source explicitly supports the claim."
    elif counts["independent_reliable_supports"] >= 2:
        status, reason = VerificationStatus.SUPPORTED, "At least two independent reliable sources explicitly support the claim."
    elif counts["reliable_supports"]:
        status, reason = VerificationStatus.PARTIALLY_SUPPORTED, "One reliable non-official source explicitly supports the claim."
    else:
        status, reason = VerificationStatus.UNVERIFIED, "No qualifying supporting evidence is available."

    return ClaimEvaluation(
        status=status,
        supporting_evidence_count=supports,
        contradicting_evidence_count=contradicts,
        independent_supporting_source_count=counts["independent_supports"],
        independent_contradicting_source_count=counts["independent_contradicts"],
        reason=reason,
    )


class ClaimEvaluationService:
    def run(self, queryset, *, dry_run=False):
        claim_ids = list(queryset.values_list("pk", flat=True))
        stats = {status: 0 for status in (
            VerificationStatus.SUPPORTED,
            VerificationStatus.PARTIALLY_SUPPORTED,
            VerificationStatus.UNVERIFIED,
            VerificationStatus.CONTRADICTED,
            VerificationStatus.CONFLICTED,
            VerificationStatus.REJECTED,
        )}
        stats.update(input_count=len(claim_ids), errors=0)
        results = []

        if dry_run:
            for claim in Claim.objects.filter(pk__in=claim_ids).order_by("pk").iterator():
                result = evaluate_claim(claim)
                stats[result.status] += 1
                results.append((claim, result))
            return stats, results, None

        job_run = JobRun.objects.create(
            job_type="CLAIM_EVALUATION",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            idempotency_key=f"claim-evaluation:{uuid4()}",
        )
        step = JobStep.objects.create(
            job_run=job_run,
            step_name="evaluate_claims",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            input_summary={"claim_count": len(claim_ids)},
        )
        try:
            for claim_id in claim_ids:
                try:
                    with transaction.atomic():
                        event_id = Claim.objects.filter(pk=claim_id).values_list("event_id", flat=True).get()
                        Event.objects.select_for_update().get(pk=event_id)
                        claim = Claim.objects.select_for_update().get(pk=claim_id)
                        result = evaluate_claim(claim)
                        old_importance = claim.importance
                        status_changed = claim.verification_status != result.status
                        deactivate_importance = bool(
                            old_importance and result.status != VerificationStatus.SUPPORTED
                        )
                        if status_changed or deactivate_importance:
                            claim.verification_status = result.status
                            if deactivate_importance:
                                claim.importance = None
                            claim._audit_context = {"job_run_id": job_run.pk, "reason": result.reason}
                            update_fields = ["updated_at"]
                            if status_changed:
                                update_fields.append("verification_status")
                            if deactivate_importance:
                                update_fields.append("importance")
                            claim.save(update_fields=update_fields)
                            if deactivate_importance:
                                AuditLog.objects.create(
                                    action="CLAIM_IMPORTANCE_DEACTIVATED",
                                    object_type="Claim",
                                    object_id=str(claim.pk),
                                    before_data={"importance": old_importance},
                                    after_data={
                                        "claim_id": claim.pk,
                                        "event_id": claim.event_id,
                                        "importance": None,
                                        "verification_status": result.status,
                                        "reason": result.reason,
                                        "job_run_id": job_run.pk,
                                        "timestamp": timezone.now().isoformat(),
                                    },
                                )
                                invalidate_event_score(
                                    claim.event_id,
                                    "claim importance deactivated after verification loss",
                                )
                        stats[result.status] += 1
                        results.append((claim, result))
                except Exception:
                    stats["errors"] += 1
                    logger.exception("Claim evaluation failed claim_id=%s", claim_id)
            final_status = JobStatus.FAILED if stats["errors"] else JobStatus.SUCCESS
            now = timezone.now()
            step.status = final_status
            step.finished_at = now
            step.output_summary = stats
            step.error_message = "One or more Claims failed" if stats["errors"] else ""
            step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])
            job_run.status = final_status
            job_run.finished_at = now
            job_run.error_message = step.error_message
            job_run.save(update_fields=["status", "finished_at", "error_message"])
            return stats, results, job_run
        except Exception as exc:
            now = timezone.now()
            step.status = JobStatus.FAILED
            step.finished_at = now
            step.error_message = str(exc)
            step.output_summary = stats
            step.save(update_fields=["status", "finished_at", "error_message", "output_summary"])
            job_run.status = JobStatus.FAILED
            job_run.finished_at = now
            job_run.error_message = str(exc)
            job_run.save(update_fields=["status", "finished_at", "error_message"])
            raise
