from dataclasses import dataclass

from apps.claims.models import EvidenceStance, VerificationStatus
from apps.sources.models import SourceTier

from .eligibility import evaluate_article_generation_eligibility


@dataclass(frozen=True)
class RiskAssessment:
    risk_level: str
    reason_code: str
    reason: str


def _result(risk_level, reason_code, reason):
    return RiskAssessment(risk_level=risk_level, reason_code=reason_code, reason=reason)


def assess_article_risk(article_version):
    """Return the deterministic, read-only risk level for an ArticleVersion.

    LOW only when the Event's Score is eligible under the active rule version,
    no linked Claim is CONTRADICTED/CONFLICTED, every linked Claim's SUPPORTS
    Evidence comes from a SourceTier.A Source, and content_quality is CLEAN.
    Any other outcome defaults to HIGH; MEDIUM is never assigned here.
    """
    article = article_version.article
    links = list(article_version.claim_links.select_related("claim").order_by("pk"))
    if not links:
        return _result(
            "HIGH", "RISK_CLAIM_CONFLICT",
            "ArticleVersion has no linked Claims.",
        )

    conflict_statuses = {VerificationStatus.CONTRADICTED, VerificationStatus.CONFLICTED}
    claims = [link.claim for link in links]
    if any(claim.verification_status in conflict_statuses for claim in claims):
        return _result(
            "HIGH", "RISK_CLAIM_CONFLICT",
            "A linked Claim is CONTRADICTED or CONFLICTED.",
        )

    eligibility = evaluate_article_generation_eligibility(article.event_id)
    if not eligibility.eligible:
        return _result(
            "HIGH", "RISK_SCORE_INELIGIBLE",
            f"Article generation eligibility failed: {eligibility.reason_code}",
        )

    for claim in claims:
        supports = list(claim.evidence.filter(stance=EvidenceStance.SUPPORTS).select_related("source"))
        if not supports:
            return _result(
                "HIGH", "RISK_SOURCE_TIER_INSUFFICIENT",
                f"Claim {claim.pk} has no SUPPORTS Evidence.",
            )
        if any(item.source.tier != SourceTier.A for item in supports):
            return _result(
                "HIGH", "RISK_SOURCE_TIER_INSUFFICIENT",
                f"Claim {claim.pk} has SUPPORTS Evidence from a Source below tier A.",
            )

    gate = getattr(article_version, "content_validation", None)
    content_quality = getattr(gate, "content_quality", "") or None
    if content_quality is None:
        return _result(
            "HIGH", "RISK_CONTENT_QUALITY_UNKNOWN",
            "ArticleVersion has no content_quality flag (older schema version or missing gate).",
        )
    if content_quality != "CLEAN":
        return _result(
            "HIGH", "RISK_CONTENT_QUALITY_FLAGGED",
            f"content_quality is {content_quality}, not CLEAN.",
        )

    return _result(
        "LOW", "RISK_LOW_ELIGIBLE",
        "Score eligible, no Claim conflict, all SUPPORTS Evidence is tier A, content_quality is CLEAN.",
    )
