import re
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import ClaimEvidence, EvidenceStance


MAX_REVIEW_REASON_LENGTH = 500


class EvidenceReviewError(Exception):
    pass


@dataclass(frozen=True)
class EvidenceReviewResult:
    evidence: ClaimEvidence
    changed: bool


def _review_reason(reason):
    value = str(reason or "").strip()
    if not value:
        raise EvidenceReviewError("A review reason is required")
    if len(value) > MAX_REVIEW_REASON_LENGTH:
        raise EvidenceReviewError("Review reason exceeds 500 characters")

    database_password = settings.DATABASES.get("default", {}).get("PASSWORD", "")
    configured_secrets = (settings.SECRET_KEY, settings.LLM_API_KEY, database_password)
    if any(secret and len(str(secret)) >= 8 and str(secret) in value for secret in configured_secrets):
        raise EvidenceReviewError("Review reason contains sensitive configuration")
    if re.search(r"(?i)(api[_ -]?key|secret[_ -]?key|password)\s*[:=]\s*\S+", value):
        raise EvidenceReviewError("Review reason appears to contain a secret")
    return value


def _validate_evidence(evidence):
    try:
        evidence.full_clean()
    except ValidationError as exc:
        raise EvidenceReviewError("ClaimEvidence integrity validation failed") from exc

    if not evidence.evidence_text.strip():
        raise EvidenceReviewError("ClaimEvidence quote is empty")
    if evidence.source_id != evidence.raw_item.source_id:
        raise EvidenceReviewError("ClaimEvidence Source does not match its RawItem")
    if evidence.claim.event_id != evidence.raw_item.event_id:
        raise EvidenceReviewError("ClaimEvidence RawItem does not belong to the Claim Event")

    from apps.ai_content.models import EvidenceHintStatus
    from apps.ai_content.services import validate_evidence_hint

    _, status, _ = validate_evidence_hint(
        evidence.claim.event,
        evidence.claim.statement,
        {"raw_item_id": evidence.raw_item_id, "quote": evidence.evidence_text},
    )
    if status != EvidenceHintStatus.VALID:
        raise EvidenceReviewError("ClaimEvidence quote is not valid for the Claim source material")


def review_claim_evidence(evidence_id, *, new_stance, reason, actor=None):
    reason = _review_reason(reason)
    if new_stance != EvidenceStance.SUPPORTS:
        raise EvidenceReviewError("Only review transitions to SUPPORTS are allowed")

    with transaction.atomic():
        evidence = (
            ClaimEvidence.objects.select_for_update()
            .select_related("claim", "raw_item__source", "source")
            .filter(pk=evidence_id)
            .first()
        )
        if not evidence:
            raise EvidenceReviewError("ClaimEvidence does not exist")
        if evidence.stance == EvidenceStance.SUPPORTS:
            return EvidenceReviewResult(evidence=evidence, changed=False)
        if evidence.stance != EvidenceStance.MENTIONS:
            raise EvidenceReviewError("Only MENTIONS evidence may transition to SUPPORTS")

        _validate_evidence(evidence)
        evidence.stance = EvidenceStance.SUPPORTS
        evidence._audit_context = {
            "actor": actor,
            "actor_type": "ADMIN" if actor else "MANUAL",
            "reason": reason,
        }
        evidence.save(update_fields=["stance"])
        return EvidenceReviewResult(evidence=evidence, changed=True)
