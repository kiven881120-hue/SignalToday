from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.ai_content.models import LLMPurpose, PromptVersion
from apps.ai_content.prompt_activation import activate_claim_extraction_prompt
from apps.audit.models import AuditLog


class ClaimExtractionPromptActivationTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_user(username="activation_operator")
        self.prompts = PromptVersion.objects.filter(purpose=LLMPurpose.CLAIM_EXTRACTION)
        self.before_content = list(self.prompts.order_by("pk").values(
            "pk", "version", "system_prompt", "output_schema_version"
        ))

    def activate_v3(self):
        return activate_claim_extraction_prompt(
            expected_current_version="v1.2",
            target_version="v1.3",
            expected_target_schema="claim-extraction-v3",
            operator=self.operator,
            reason="Controlled V3 activation test",
        )

    def test_activation_is_atomic_and_audited_without_changing_prompt_content(self):
        result = self.activate_v3()

        self.assertEqual(
            list(self.prompts.filter(active=True).values_list("version", "output_schema_version")),
            [("v1.3", "claim-extraction-v3")],
        )
        self.assertEqual(
            list(self.prompts.order_by("pk").values(
                "pk", "version", "system_prompt", "output_schema_version"
            )),
            self.before_content,
        )
        self.assertEqual(result.audit_log.action, "CLAIM_EXTRACTION_PROMPT_ACTIVATED")
        self.assertEqual(result.audit_log.actor, self.operator)
        self.assertEqual(result.audit_log.before_data["prompt_version"], "v1.2")
        self.assertEqual(result.audit_log.after_data["prompt_version"], "v1.3")
        self.assertEqual(result.audit_log.after_data["schema_version"], "claim-extraction-v3")

    def test_stale_expected_version_and_schema_mismatch_make_no_change(self):
        cases = (
            {"expected_current_version": "v1.1", "expected_target_schema": "claim-extraction-v3"},
            {"expected_current_version": "v1.2", "expected_target_schema": "claim-extraction-v2"},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(CommandError):
                call_command(
                    "activate_claim_extraction_prompt",
                    target_version="v1.3",
                    operator=self.operator.username,
                    reason="Must fail closed",
                    **case,
                )
            self.assertEqual(list(self.prompts.filter(active=True).values_list("version", flat=True)), ["v1.2"])
            self.assertFalse(AuditLog.objects.filter(action="CLAIM_EXTRACTION_PROMPT_ACTIVATED").exists())

    def test_explicit_reverse_activation_provides_rollback(self):
        self.activate_v3()
        result = activate_claim_extraction_prompt(
            expected_current_version="v1.3",
            target_version="v1.2",
            expected_target_schema="claim-extraction-v2",
            operator=self.operator,
            reason="Controlled rollback test",
        )

        self.assertEqual(result.old_prompt.version, "v1.3")
        self.assertEqual(result.new_prompt.version, "v1.2")
        self.assertEqual(list(self.prompts.filter(active=True).values_list("version", flat=True)), ["v1.2"])
        self.assertEqual(AuditLog.objects.filter(action="CLAIM_EXTRACTION_PROMPT_ACTIVATED").count(), 2)

    def test_missing_operator_fails_before_activation(self):
        with self.assertRaises(CommandError):
            call_command(
                "activate_claim_extraction_prompt",
                expected_current_version="v1.2",
                target_version="v1.3",
                expected_target_schema="claim-extraction-v3",
                operator="missing",
                reason="Must fail",
            )
        self.assertEqual(list(self.prompts.filter(active=True).values_list("version", flat=True)), ["v1.2"])
