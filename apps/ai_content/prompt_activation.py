from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog

from .models import LLMPurpose, PromptVersion


class PromptActivationError(Exception):
    pass


@dataclass(frozen=True)
class PromptActivationResult:
    old_prompt: PromptVersion
    new_prompt: PromptVersion
    audit_log: AuditLog
    activation_time: object


def activate_claim_extraction_prompt(
    *,
    expected_current_version,
    target_version,
    expected_target_schema,
    operator,
    reason,
):
    reason = str(reason or "").strip()
    if not reason:
        raise PromptActivationError("An activation reason is required")
    if operator is None or getattr(operator, "pk", None) is None:
        raise PromptActivationError("A persisted operator is required")

    with transaction.atomic():
        prompts = list(
            PromptVersion.objects.select_for_update()
            .filter(purpose=LLMPurpose.CLAIM_EXTRACTION)
            .order_by("pk")
        )
        active = [prompt for prompt in prompts if prompt.active]
        if len(active) != 1:
            raise PromptActivationError("Exactly one active Claim Extraction prompt is required")

        old_prompt = active[0]
        if old_prompt.version != expected_current_version:
            raise PromptActivationError(
                f"Active prompt is {old_prompt.version}, expected {expected_current_version}"
            )
        new_prompt = next((prompt for prompt in prompts if prompt.version == target_version), None)
        if new_prompt is None:
            raise PromptActivationError("Target PromptVersion does not exist")
        if new_prompt.pk == old_prompt.pk:
            raise PromptActivationError("Target PromptVersion is already active")
        if new_prompt.output_schema_version != expected_target_schema:
            raise PromptActivationError(
                f"Target schema is {new_prompt.output_schema_version}, expected {expected_target_schema}"
            )

        activation_time = timezone.now()
        old_prompt.active = False
        old_prompt.save(update_fields=["active"])
        new_prompt.active = True
        new_prompt.save(update_fields=["active"])
        audit = AuditLog.objects.create(
            actor=operator,
            action="CLAIM_EXTRACTION_PROMPT_ACTIVATED",
            object_type="PromptVersion",
            object_id=str(new_prompt.pk),
            before_data={
                "prompt_version": old_prompt.version,
                "schema_version": old_prompt.output_schema_version,
                "active": True,
            },
            after_data={
                "prompt_version": new_prompt.version,
                "schema_version": new_prompt.output_schema_version,
                "active": True,
                "operator": operator.get_username(),
                "reason": reason,
                "activation_time": activation_time.isoformat(),
            },
        )
        return PromptActivationResult(old_prompt, new_prompt, audit, activation_time)
