import hashlib

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.audit.models import AuditLog

from .models import Claim, ClaimEvidence


def _context(instance, default_reason):
    context = getattr(instance, "_audit_context", {})
    return {
        "actor": context.get("actor"),
        "actor_type": context.get("actor_type", "SYSTEM"),
        "job_run_id": context.get("job_run_id"),
        "candidate_id": context.get("candidate_id"),
        "reason": context.get("reason", default_reason),
    }


@receiver(pre_save, sender=Claim)
def remember_claim_status(sender, instance, **kwargs):
    instance._old_verification_status = None
    if instance.pk:
        instance._old_verification_status = sender.objects.filter(pk=instance.pk).values_list(
            "verification_status", flat=True
        ).first()


@receiver(post_save, sender=Claim)
def audit_claim_status(sender, instance, created, **kwargs):
    old_status = getattr(instance, "_old_verification_status", None)
    if created or old_status == instance.verification_status:
        return
    context = _context(instance, "claim_status_changed")
    AuditLog.objects.create(
        actor=context["actor"],
        action="CLAIM_STATUS_CHANGED",
        object_type="Claim",
        object_id=str(instance.pk),
        before_data={"status": old_status},
        after_data={
            "claim_id": instance.pk,
            "old_status": old_status,
            "new_status": instance.verification_status,
            "reason": context["reason"],
            "job_run_id": context["job_run_id"],
            "timestamp": timezone.now().isoformat(),
        },
    )


def _evidence_snapshot(instance):
    normalized = instance.normalized_evidence_text or ""
    return {
        "claim_id": instance.claim_id,
        "raw_item_id": instance.raw_item_id,
        "source_id": instance.source_id,
        "stance": instance.stance,
        "evidence_type": instance.evidence_type,
        "evidence_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


@receiver(pre_save, sender=ClaimEvidence)
def remember_evidence(sender, instance, **kwargs):
    instance._old_evidence_snapshot = None
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).first()
        if old:
            instance._old_evidence_snapshot = _evidence_snapshot(old)


@receiver(post_save, sender=ClaimEvidence)
def audit_evidence_save(sender, instance, created, **kwargs):
    before = getattr(instance, "_old_evidence_snapshot", None)
    after = _evidence_snapshot(instance)
    if not created and before == after:
        return
    context = _context(instance, "evidence_created" if created else "evidence_updated")
    AuditLog.objects.create(
        actor=context["actor"],
        action="CLAIM_EVIDENCE_CREATED" if created else "CLAIM_EVIDENCE_UPDATED",
        object_type="ClaimEvidence",
        object_id=str(instance.pk),
        before_data=before or {},
        after_data={
            **after,
            "actor_type": context["actor_type"],
            "candidate_id": context["candidate_id"],
            "reason": context["reason"],
            "timestamp": timezone.now().isoformat(),
        },
    )


@receiver(pre_delete, sender=ClaimEvidence)
def remember_deleted_evidence(sender, instance, **kwargs):
    instance._deleted_evidence_snapshot = _evidence_snapshot(instance)


@receiver(post_delete, sender=ClaimEvidence)
def audit_evidence_delete(sender, instance, **kwargs):
    context = _context(instance, "evidence_deleted")
    AuditLog.objects.create(
        actor=context["actor"],
        action="CLAIM_EVIDENCE_DELETED",
        object_type="ClaimEvidence",
        object_id=str(instance.pk),
        before_data=getattr(instance, "_deleted_evidence_snapshot", {}),
        after_data={"reason": context["reason"], "timestamp": timezone.now().isoformat()},
    )
