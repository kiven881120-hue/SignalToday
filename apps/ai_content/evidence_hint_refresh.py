from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.evidence_review import _review_reason

from .models import ClaimCandidate, ClaimCandidateEvidenceHint, EvidenceHintStatus
from .services import validate_evidence_hint


class EvidenceHintRefreshError(Exception):
    pass


@dataclass(frozen=True)
class EvidenceHintRefreshResult:
    old_hint: ClaimCandidateEvidenceHint
    new_hint: ClaimCandidateEvidenceHint
    audit: AuditLog


def refresh_claim_evidence_hint(
    candidate_id, *, raw_item_id, quote, actor, reason, expected_old_hint_id
):
    """Create a manually selected, current-body-validated replacement Hint.

    No model/provider, Claim, Evidence, JobRun, or Candidate status changes occur.
    """
    if actor is None or getattr(actor, "pk", None) is None:
        raise EvidenceHintRefreshError("A persisted human actor is required")
    try:
        reason = _review_reason(reason)
    except Exception as exc:
        raise EvidenceHintRefreshError(str(exc)) from exc
    quote = str(quote or "").strip()
    if not quote:
        raise EvidenceHintRefreshError("Quote is required")

    try:
        with transaction.atomic():
            candidate = (
                ClaimCandidate.objects.select_for_update()
                .select_related("event")
                .filter(pk=candidate_id)
                .first()
            )
            if candidate is None:
                raise EvidenceHintRefreshError("ClaimCandidate does not exist")
            old = (
                ClaimCandidateEvidenceHint.objects.select_for_update()
                .filter(pk=expected_old_hint_id, candidate=candidate)
                .first()
            )
            if old is None or old.status != EvidenceHintStatus.VALID or not old.is_current:
                raise EvidenceHintRefreshError("Expected old Hint is not the Candidate's current VALID Hint")
            raw_item, status, invalid_reason = validate_evidence_hint(
                candidate.event,
                candidate.claim_text,
                {"raw_item_id": raw_item_id, "quote": quote},
                body_only=True,
            )
            if raw_item is None or raw_item.event_id != candidate.event_id:
                raise EvidenceHintRefreshError("RawItem does not belong to the Candidate Event")
            if status != EvidenceHintStatus.VALID:
                raise EvidenceHintRefreshError(f"Evidence Hint validation failed: {invalid_reason}")
            # Release the partial unique key inside the same transaction. Any
            # later failure rolls this transition back together with the insert.
            old.status = EvidenceHintStatus.SUPERSEDED
            old.is_current = False
            old.save(update_fields=["status", "is_current"])
            try:
                new = ClaimCandidateEvidenceHint.objects.create(
                    candidate=candidate,
                    raw_item=raw_item,
                    proposed_raw_item_id=raw_item_id,
                    quote=quote,
                    hint_type=old.hint_type,
                    status=EvidenceHintStatus.VALID,
                    is_current=True,
                    invalid_reason="",
                )
            except (IntegrityError, ValidationError) as exc:
                raise EvidenceHintRefreshError("A current VALID Hint already exists or quote is duplicated") from exc
            old.superseded_by = new
            old.save(update_fields=["status", "superseded_by"])
            audit = AuditLog.objects.create(
                actor=actor,
                action="CLAIM_EVIDENCE_HINT_REFRESHED",
                object_type="ClaimCandidateEvidenceHint",
                object_id=str(new.pk),
                before_data={
                    "candidate_id": candidate.pk,
                    "hint_id": old.pk,
                    "raw_item_id": old.raw_item_id,
                    "quote": old.quote,
                    "status": EvidenceHintStatus.VALID,
                },
                after_data={
                    "candidate_id": candidate.pk,
                    "old_hint_id": old.pk,
                    "new_hint_id": new.pk,
                    "raw_item_id": new.raw_item_id,
                    "old_quote": old.quote,
                    "new_quote": new.quote,
                    "status": new.status,
                    "reason": reason,
                    "actor_id": actor.pk,
                    "source": "manual_current_body_refresh",
                    "timestamp": timezone.now().isoformat(),
                },
            )
            return EvidenceHintRefreshResult(old, new, audit)
    except EvidenceHintRefreshError:
        raise
    except IntegrityError as exc:
        raise EvidenceHintRefreshError("Concurrent refresh lost; current VALID Hint already changed") from exc
