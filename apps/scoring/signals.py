from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.audit.models import AuditLog

from .models import Score


def score_snapshot(score):
    if not score:
        return None
    return {
        "score": str(score.final_score),
        "level": score.recommendation_level,
        "status": score.status,
        "rule_version": score.rule_version.version,
        "dimensions": {
            "importance": str(score.importance),
            "credibility": str(score.credibility),
            "user_relevance": str(score.user_relevance),
            "information_gain": str(score.information_gain),
            "freshness": str(score.freshness),
            "long_term_impact": str(score.long_term_impact),
        },
        "reasons": score.reasons,
    }


@receiver(pre_save, sender=Score)
def remember_score(sender, instance, **kwargs):
    instance._old_score_snapshot = None
    if instance.pk:
        instance._old_score_snapshot = score_snapshot(sender.objects.filter(pk=instance.pk).first())


@receiver(post_save, sender=Score)
def audit_score(sender, instance, created, **kwargs):
    old = getattr(instance, "_old_score_snapshot", None)
    new = score_snapshot(instance)
    if not created and old == new:
        return
    context = getattr(instance, "_audit_context", {})
    AuditLog.objects.create(
        actor=context.get("actor"),
        action=context.get("action", "EVENT_SCORE_CHANGED"),
        object_type="Score",
        object_id=str(instance.pk),
        before_data={
            "event_id": instance.event_id,
            "old_score": old["score"] if old else None,
            "old_level": old["level"] if old else None,
            "snapshot": old,
        },
        after_data={
            "event_id": instance.event_id,
            "new_score": new["score"],
            "new_level": new["level"],
            "rule_version": instance.rule_version.version,
            "reason": context.get("reason", instance.reasons.get("recommendation_reason", "")),
            "job_run_id": context.get("job_run_id"),
            "snapshot": new,
            "timestamp": timezone.now().isoformat(),
        },
    )
