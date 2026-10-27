from dataclasses import dataclass

from apps.ai_content.models import EvidenceHintStatus
from apps.ai_content.services import validate_evidence_hint
from apps.claims.models import EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.scoring.models import Score, ScoreRuleVersion, ScoreStatus


@dataclass(frozen=True)
class ArticleGenerationEligibility:
    eligible: bool
    reason_code: str
    reason: str
    eligible_claim_ids: tuple[int, ...] = ()
    eligible_evidence_ids: tuple[int, ...] = ()
    score_id: int | None = None
    recommendation_level: str | None = None


def _result(
    eligible,
    reason_code,
    reason,
    *,
    claims=(),
    evidence=(),
    score=None,
):
    return ArticleGenerationEligibility(
        eligible=eligible,
        reason_code=reason_code,
        reason=reason,
        eligible_claim_ids=tuple(claims),
        eligible_evidence_ids=tuple(evidence),
        score_id=score.pk if score else None,
        recommendation_level=score.recommendation_level if score else None,
    )


def _current_score(event):
    rule = ScoreRuleVersion.objects.filter(active=True).order_by("-created_at", "-pk").first()
    if rule is None:
        return None
    return Score.objects.filter(
        event=event,
        rule_version=rule,
        status=ScoreStatus.CALCULATED,
    ).select_related("rule_version").first()


def _is_traceable_support(evidence, event, claim):
    if evidence.raw_item.event_id != event.pk:
        return False
    if evidence.source_id != evidence.raw_item.source_id:
        return False
    _, status, _ = validate_evidence_hint(
        event,
        claim.statement,
        {"raw_item_id": evidence.raw_item_id, "quote": evidence.evidence_text},
    )
    return status == EvidenceHintStatus.VALID


def evaluate_article_generation_eligibility(event_or_id):
    """Return the deterministic, read-only article-generation decision for an Event."""
    event_id = getattr(event_or_id, "pk", event_or_id)
    event = Event.objects.filter(pk=event_id).first() if event_id else None
    if event is None:
        return _result(False, "ARTICLE_EVENT_NOT_FOUND", "Event does not exist.")

    score = _current_score(event)
    if score is None:
        return _result(
            False,
            "ARTICLE_SCORE_MISSING",
            "Event has no calculated Score under an active rule version.",
        )
    claims = list(event.claims.order_by("pk"))
    if not claims:
        return _result(
            False,
            "ARTICLE_CLAIMS_MISSING",
            "Event has no formal Claims; ClaimCandidates are not sufficient.",
            score=score,
        )

    supported_claims = [
        claim for claim in claims if claim.verification_status == VerificationStatus.SUPPORTED
    ]
    if not supported_claims:
        return _result(
            False,
            "ARTICLE_NO_SUPPORTED_CLAIMS",
            "Event has no SUPPORTED formal Claims.",
            score=score,
        )

    eligible_claim_ids = []
    eligible_evidence_ids = []
    for claim in supported_claims:
        valid_evidence_ids = [
            item.pk
            for item in claim.evidence.filter(stance=EvidenceStance.SUPPORTS)
            .select_related("raw_item__source", "source")
            .order_by("pk")
            if _is_traceable_support(item, event, claim)
        ]
        if valid_evidence_ids:
            eligible_claim_ids.append(claim.pk)
            eligible_evidence_ids.extend(valid_evidence_ids)

    if not eligible_claim_ids:
        return _result(
            False,
            "ARTICLE_NO_TRACEABLE_SUPPORTED_CLAIMS",
            "No SUPPORTED Claim has valid, traceable SUPPORTS evidence.",
            score=score,
        )

    conflict_statuses = {VerificationStatus.CONTRADICTED, VerificationStatus.CONFLICTED}
    if any(claim.verification_status in conflict_statuses for claim in claims):
        return _result(
            False,
            "ARTICLE_CLAIM_CONFLICT_PRESENT",
            "A CONTRADICTED or CONFLICTED Claim requires resolution before generation.",
            claims=eligible_claim_ids,
            evidence=eligible_evidence_ids,
            score=score,
        )

    return _result(
        True,
        "ARTICLE_ELIGIBLE",
        (
            "Event has a calculated Score and at least one traceable SUPPORTED Claim. "
            "The recommendation level controls priority, not factual worth."
        ),
        claims=eligible_claim_ids,
        evidence=eligible_evidence_ids,
        score=score,
    )
