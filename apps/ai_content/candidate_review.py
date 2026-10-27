from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.evaluation import evaluate_claim
from apps.claims.evidence_review import _review_reason, review_claim_evidence
from apps.claims.models import ClaimEvidence, EvidenceStance
from apps.events.models import Event
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep

from .models import ClaimCandidate, EvidenceHintStatus, ReviewStatus
from .services import accept_claim_candidate, validate_evidence_hint


class CandidateReviewError(Exception):
    pass


@dataclass(frozen=True)
class CandidateReviewResult:
    candidate: ClaimCandidate
    claim: object
    evidence: ClaimEvidence
    job_run: JobRun
    changed: bool


def _complete_step(step, output):
    step.status = JobStatus.SUCCESS
    step.finished_at = timezone.now()
    step.output_summary = output
    step.save(update_fields=["status", "finished_at", "output_summary"])


def review_candidate_to_supported_claim(
    candidate_id,
    *,
    expected_event_id,
    evidence_hint_id,
    actor,
    candidate_reason,
    evidence_reason,
):
    if actor is None or getattr(actor, "pk", None) is None:
        raise CandidateReviewError("A persisted human actor is required")
    try:
        candidate_reason = _review_reason(candidate_reason)
        evidence_reason = _review_reason(evidence_reason)
    except Exception as exc:
        raise CandidateReviewError(str(exc)) from exc

    idempotency_key = f"candidate-evidence-review:{candidate_id}:supported"
    with transaction.atomic():
        Event.objects.select_for_update().get(pk=expected_event_id)
        candidate = (
            ClaimCandidate.objects.select_for_update()
            .select_related("event", "model_call")
            .filter(pk=candidate_id)
            .first()
        )
        if candidate is None:
            raise CandidateReviewError("ClaimCandidate does not exist")
        if candidate.event_id != expected_event_id:
            raise CandidateReviewError("ClaimCandidate does not belong to the expected Event")

        existing_job = JobRun.objects.filter(idempotency_key=idempotency_key).first()
        if existing_job is not None:
            if existing_job.status != JobStatus.SUCCESS or not candidate.accepted_claim_id:
                raise CandidateReviewError("An incomplete review JobRun already exists")
            evidence = ClaimEvidence.objects.filter(
                claim_id=candidate.accepted_claim_id,
                raw_item_id__in=candidate.evidence_hints.filter(pk=evidence_hint_id).values("raw_item_id"),
                normalized_evidence_text__in=candidate.evidence_hints.filter(pk=evidence_hint_id).values("normalized_quote"),
            ).first()
            if evidence is None or evidence.stance != EvidenceStance.SUPPORTS:
                raise CandidateReviewError("Existing review result is inconsistent")
            return CandidateReviewResult(candidate, candidate.accepted_claim, evidence, existing_job, False)

        if candidate.status != ReviewStatus.PROPOSED or candidate.accepted_claim_id is not None:
            raise CandidateReviewError("ClaimCandidate has already been processed")
        valid_hints = candidate.evidence_hints.filter(status=EvidenceHintStatus.VALID, is_current=True)
        if valid_hints.count() != 1:
            raise CandidateReviewError("Controlled review requires exactly one valid Evidence Hint")
        hint = valid_hints.select_related("raw_item__source").filter(pk=evidence_hint_id).first()
        if hint is None:
            raise CandidateReviewError("Evidence Hint does not match the Candidate")
        _, hint_status, invalid_reason = validate_evidence_hint(
            candidate.event,
            candidate.claim_text,
            {"raw_item_id": hint.raw_item_id, "quote": hint.quote},
        )
        if hint_status != EvidenceHintStatus.VALID:
            raise CandidateReviewError(f"Evidence Hint validation failed: {invalid_reason}")

        job = JobRun.objects.create(
            job_type="CANDIDATE_CLAIM_EVIDENCE_REVIEW",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            idempotency_key=idempotency_key,
        )
        candidate_step = JobStep.objects.create(
            job_run=job,
            step_name="candidate_review",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            input_summary={
                "candidate_id": candidate.pk,
                "event_id": candidate.event_id,
                "decision": "ACCEPT",
                "actor": actor.get_username(),
            },
        )
        claim = accept_claim_candidate(
            candidate,
            actor=actor,
            reason=candidate_reason,
        )
        _complete_step(candidate_step, {
            "candidate_id": candidate.pk,
            "claim_id": claim.pk,
            "decision": "ACCEPT",
        })

        creation_step = JobStep.objects.create(
            job_run=job,
            step_name="claim_evidence_creation",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            input_summary={"claim_id": claim.pk, "evidence_hint_id": hint.pk},
        )
        evidence = ClaimEvidence.objects.filter(
            claim=claim,
            raw_item=hint.raw_item,
            normalized_evidence_text=hint.normalized_quote,
        ).first()
        if evidence is None or evidence.stance != EvidenceStance.MENTIONS:
            raise CandidateReviewError("Accepted Candidate did not create MENTIONS Evidence")
        _complete_step(creation_step, {
            "claim_id": claim.pk,
            "evidence_id": evidence.pk,
            "initial_stance": EvidenceStance.MENTIONS,
            "model_hint_type": hint.hint_type,
        })

        review_step = JobStep.objects.create(
            job_run=job,
            step_name="evidence_review",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            input_summary={
                "evidence_id": evidence.pk,
                "formal_decision": "SUPPORTED",
                "actor": actor.get_username(),
            },
        )
        review_claim_evidence(
            evidence.pk,
            new_stance=EvidenceStance.SUPPORTS,
            reason=evidence_reason,
            actor=actor,
        )
        evaluation = evaluate_claim(claim)
        if evaluation.status != "SUPPORTED":
            raise CandidateReviewError("Reviewed evidence did not produce a SUPPORTED Claim")
        if claim.verification_status != evaluation.status:
            claim.verification_status = evaluation.status
            claim._audit_context = {
                "actor": actor,
                "job_run_id": job.pk,
                "reason": evaluation.reason,
            }
            claim.save(update_fields=["verification_status", "updated_at"])
        evidence.refresh_from_db()
        _complete_step(review_step, {
            "evidence_id": evidence.pk,
            "formal_stance": evidence.stance,
            "claim_status": claim.verification_status,
            "source_tier": hint.raw_item.source.tier,
            "source_authority": "OFFICIAL_TIER_A",
            "independent_corroboration": False,
        })

        AuditLog.objects.create(
            actor=actor,
            action="CANDIDATE_CLAIM_EVIDENCE_REVIEW_COMPLETED",
            object_type="ClaimCandidate",
            object_id=str(candidate.pk),
            before_data={
                "candidate_status": ReviewStatus.PROPOSED,
                "evidence_stance": EvidenceStance.MENTIONS,
                "model_hint_type": hint.hint_type,
            },
            after_data={
                "candidate_status": ReviewStatus.ACCEPTED,
                "claim_id": claim.pk,
                "claim_status": claim.verification_status,
                "evidence_id": evidence.pk,
                "formal_stance": evidence.stance,
                "source_tier": hint.raw_item.source.tier,
                "source_authority": "OFFICIAL_TIER_A",
                "independent_corroboration": False,
                "job_run_id": job.pk,
                "timestamp": timezone.now().isoformat(),
            },
        )
        job.status = JobStatus.SUCCESS
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
        candidate.refresh_from_db()
        claim.refresh_from_db()
        return CandidateReviewResult(candidate, claim, evidence, job, True)
