import logging
from dataclasses import asdict, dataclass
from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.db.models import Case, CharField, Count, F, Max, Q, Value, When
from django.db.models.functions import Cast, Concat
from django.utils import timezone

from apps.claims.models import ClaimEvidence, ClaimType, EvidenceStance, EvidenceType, VerificationStatus
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep

from .models import Score, ScoreRuleVersion
from .rules import DIMENSIONS, recommendation_level, weighted_score


logger = logging.getLogger("signaltoday.scoring")


@dataclass
class ScoreCalculation:
    importance: Decimal
    credibility: Decimal
    user_relevance: Decimal
    information_gain: Decimal
    freshness: Decimal
    long_term_impact: Decimal
    final_score: Decimal
    recommendation_level: str
    reasons: dict
    rule_version: ScoreRuleVersion

    def score_values(self):
        return {field: getattr(self, field) for field in DIMENSIONS}

    def output_dict(self):
        return {
            **{field: str(getattr(self, field)) for field in DIMENSIONS},
            "final_score": str(self.final_score),
            "recommendation_level": self.recommendation_level,
            "reasons": self.reasons,
            "rule_version": self.rule_version.version,
        }


def _source_identity():
    return Case(
        When(source__source_group="", then=Concat(Value("source:"), Cast("source_id", CharField()))),
        default=Concat(Value("group:"), F("source__source_group")),
        output_field=CharField(),
    )


def _claim_metrics(event):
    return event.claims.aggregate(
        total=Count("id"),
        max_importance=Max("importance"),
        impact_count=Count("id", filter=Q(claim_type=ClaimType.IMPACT)),
        max_impact=Max("importance", filter=Q(claim_type=ClaimType.IMPACT)),
        supported=Count("id", filter=Q(verification_status=VerificationStatus.SUPPORTED)),
        partial=Count("id", filter=Q(verification_status=VerificationStatus.PARTIALLY_SUPPORTED)),
        unverified=Count("id", filter=Q(verification_status__in=[VerificationStatus.UNVERIFIED, VerificationStatus.NEEDS_REVIEW])),
        contradicted=Count("id", filter=Q(verification_status=VerificationStatus.CONTRADICTED)),
        conflicted=Count("id", filter=Q(verification_status=VerificationStatus.CONFLICTED)),
        rejected=Count("id", filter=Q(verification_status=VerificationStatus.REJECTED)),
    )


def _credibility(event, claims):
    if not claims["total"]:
        return Decimal("2"), "No Claims are available, so credibility remains low."
    if claims["conflicted"]:
        return Decimal("3"), "At least one Claim is conflicted; credibility is capped below the high range."
    if claims["contradicted"]:
        return Decimal("2"), "At least one Claim is contradicted; credibility is capped at 3."
    if claims["unverified"]:
        return Decimal("4"), "At least one Claim is unverified; credibility cannot enter the high range."
    if claims["partial"]:
        return Decimal("6.5"), "Evidence partially supports the available Claims."
    if claims["supported"] == claims["total"]:
        identity = _source_identity()
        evidence = ClaimEvidence.objects.filter(claim__event=event, stance=EvidenceStance.SUPPORTS).aggregate(
            official=Count(
                "id",
                filter=Q(source__tier="A") | Q(evidence_type=EvidenceType.OFFICIAL),
            ),
            reliable_groups=Count(
                identity,
                filter=Q(source__tier__in=["A", "B", "C"]),
                distinct=True,
            ),
        )
        if evidence["official"]:
            return Decimal("10"), "All Claims are supported and explicit official evidence is present."
        if evidence["reliable_groups"] >= 2:
            return Decimal("9"), "All Claims are supported by at least two independent reliable sources."
        return Decimal("8"), "All Claims are supported by available reliable evidence."
    return Decimal("2"), "Claims are rejected or lack qualifying verified support."


def _freshness(event, now):
    anchor = event.occurred_at or event.first_seen_at
    if not anchor:
        return Decimal("3"), "Event time is unavailable; job time was used with a conservative score.", True
    if timezone.is_naive(anchor):
        anchor = timezone.make_aware(anchor, timezone.get_current_timezone())
    hours = max(Decimal("0"), Decimal(str((now - anchor).total_seconds())) / Decimal("3600"))
    if hours <= 6:
        value = Decimal("10")
    elif hours <= 24:
        value = Decimal("9")
    elif hours <= 48:
        value = Decimal("7")
    elif hours <= 72:
        value = Decimal("5")
    else:
        periods = int((hours - Decimal("72")) // Decimal("72"))
        value = max(Decimal("0"), Decimal("4") - Decimal(periods))
    return value, f"Freshness is based on an event age of {hours.quantize(Decimal('0.1'))} hours.", False


def calculate_score(event, rule_version, *, now=None):
    now = now or timezone.now()
    claims = _claim_metrics(event)
    importance = (Decimal(claims["max_importance"] or 0) / Decimal("10")).quantize(Decimal("0.001"))
    importance_reason = "Importance uses the highest human-maintained Claim importance; source volume is ignored."
    credibility, credibility_reason = _credibility(event, claims)
    if claims["impact_count"]:
        user_relevance = (Decimal(claims["max_impact"] or 0) / Decimal("10")).quantize(Decimal("0.001"))
        user_reason = "User relevance uses the highest human-maintained IMPACT Claim importance."
        long_term = user_relevance
        long_term_reason = "Long-term impact uses the strongest explicit IMPACT Claim."
    else:
        user_relevance = min(Decimal("5"), importance)
        user_reason = "No IMPACT Claim exists; general relevance is conservatively capped at 5."
        long_term = min(Decimal("5"), (importance * Decimal("0.6")).quantize(Decimal("0.001")))
        long_term_reason = "No IMPACT Claim exists; long-term impact is conservatively derived and capped at 5."
    claim_count = claims["supported"] + claims["partial"]
    information_gain = Decimal("0") if claim_count == 0 else min(Decimal("10"), Decimal("4") + Decimal(claim_count))
    information_reason = f"Information gain is based on {claim_count} supported or partially supported Claims, not RawItem or source count."
    freshness, freshness_reason, missing_time = _freshness(event, now)
    if missing_time and credibility > Decimal("5"):
        credibility = Decimal("5")
        credibility_reason += " Missing event time caps credibility at 5."
    values = {
        "importance": importance,
        "credibility": credibility,
        "user_relevance": user_relevance,
        "information_gain": information_gain,
        "freshness": freshness,
        "long_term_impact": long_term,
    }
    final = weighted_score(values, rule_version.rule_config)
    level = recommendation_level(final, rule_version.rule_config)
    reasons = {
        "importance_reason": importance_reason,
        "credibility_reason": credibility_reason,
        "user_relevance_reason": user_reason,
        "information_gain_reason": information_reason,
        "freshness_reason": freshness_reason,
        "long_term_impact_reason": long_term_reason,
        "recommendation_reason": f"Deterministic rule {rule_version.version} produced {final} ({level}).",
    }
    return ScoreCalculation(**values, final_score=final, recommendation_level=level, reasons=reasons, rule_version=rule_version)


def get_rule_version(version=None):
    queryset = ScoreRuleVersion.objects.filter(active=True) if version is None else ScoreRuleVersion.objects.filter(version=version)
    rule = queryset.order_by("-created_at", "-pk").first()
    if not rule:
        raise ScoreRuleVersion.DoesNotExist(f"No scoring rule found for {version or 'active rule'}")
    return rule


def score_event(event, rule_version=None, *, now=None, job_run=None):
    rule_version = rule_version or get_rule_version()
    with transaction.atomic():
        locked_event = event.__class__.objects.select_for_update().get(pk=event.pk)
        result = calculate_score(locked_event, rule_version, now=now)
        score = Score.objects.filter(event=locked_event, rule_version=rule_version).first()
        created = score is None
        if created:
            score = Score(
                event=locked_event,
                rule_version=rule_version,
                **result.score_values(),
                reasons=result.reasons,
                final_score=result.final_score,
            )
            score._audit_context = {"job_run_id": job_run.pk if job_run else None}
            score.save()
        else:
            new_values = {**result.score_values(), "reasons": result.reasons}
            changed = any(getattr(score, key) != value for key, value in new_values.items())
            if changed:
                for key, value in new_values.items():
                    setattr(score, key, value)
                score._audit_context = {"job_run_id": job_run.pk if job_run else None}
                score.save(update_fields=[*DIMENSIONS, "reasons", "updated_at"])
        return score, result, created


class EventScoringService:
    def run(self, queryset, *, rule_version=None, dry_run=False):
        rule = rule_version or get_rule_version()
        event_ids = list(queryset.values_list("pk", flat=True))
        stats = {"input_count": len(event_ids), "scored_count": 0, "focus_count": 0, "normal_count": 0, "candidate_count": 0, "reject_count": 0, "error_count": 0}
        results = []
        if dry_run:
            for event in queryset.model.objects.filter(pk__in=event_ids).order_by("pk").iterator():
                result = calculate_score(event, rule)
                stats["scored_count"] += 1
                stats[f"{result.recommendation_level.lower()}_count"] += 1
                results.append((event, result))
            return stats, results, None

        job_run = JobRun.objects.create(job_type="EVENT_SCORING", status=JobStatus.RUNNING, started_at=timezone.now(), idempotency_key=f"event-scoring:{uuid4()}")
        step = JobStep.objects.create(job_run=job_run, step_name="score_events", status=JobStatus.RUNNING, started_at=timezone.now(), input_summary={"event_count": len(event_ids), "rule_version": rule.version})
        for event_id in event_ids:
            try:
                event = queryset.model.objects.get(pk=event_id)
                score, result, _ = score_event(event, rule, job_run=job_run)
                stats["scored_count"] += 1
                stats[f"{score.recommendation_level.lower()}_count"] += 1
                results.append((event, result))
            except Exception:
                stats["error_count"] += 1
                logger.exception("Event scoring failed event_id=%s", event_id)
        final_status = JobStatus.FAILED if stats["error_count"] else JobStatus.SUCCESS
        now = timezone.now()
        step.status, step.finished_at, step.output_summary = final_status, now, stats
        step.error_message = "One or more Events failed" if stats["error_count"] else ""
        step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])
        job_run.status, job_run.finished_at, job_run.error_message = final_status, now, step.error_message
        job_run.save(update_fields=["status", "finished_at", "error_message"])
        return stats, results, job_run
