from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.audit.models import AuditLog

from .models import ClaimCandidate, ReviewStatus, ScoreSuggestion


def remember_status(sender, instance, **kwargs):
    instance._old_review_status = None
    if instance.pk:
        instance._old_review_status = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()


def audit_review(instance, object_type):
    old = getattr(instance, "_old_review_status", None)
    if old == instance.status or instance.status not in {ReviewStatus.ACCEPTED, ReviewStatus.REJECTED}:
        return
    context = getattr(instance, "_audit_context", {})
    action_prefix = "CLAIM_CANDIDATE" if object_type == "ClaimCandidate" else "SCORE_SUGGESTION"
    AuditLog.objects.create(
        actor=context.get("actor"),
        action=f"{action_prefix}_{instance.status}",
        object_type=object_type,
        object_id=str(instance.pk),
        before_data={"status": old or ReviewStatus.PROPOSED},
        after_data={
            "event_id": instance.event_id,
            "model_call_id": instance.model_call_id,
            "claim_id": instance.accepted_claim_id if object_type == "ClaimCandidate" else None,
            "status": instance.status,
            "reason": context.get("reason", ""),
            "timestamp": timezone.now().isoformat(),
        },
    )


pre_save.connect(remember_status, sender=ClaimCandidate)
pre_save.connect(remember_status, sender=ScoreSuggestion)


@receiver(post_save, sender=ClaimCandidate)
def audit_candidate(sender, instance, **kwargs):
    audit_review(instance, "ClaimCandidate")


@receiver(post_save, sender=ScoreSuggestion)
def audit_suggestion(sender, instance, **kwargs):
    audit_review(instance, "ScoreSuggestion")
