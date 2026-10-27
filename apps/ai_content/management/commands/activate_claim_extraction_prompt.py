import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.ai_content.prompt_activation import (
    PromptActivationError,
    activate_claim_extraction_prompt,
)


class Command(BaseCommand):
    help = "Atomically activate one Claim Extraction PromptVersion with an audit record."

    def add_arguments(self, parser):
        parser.add_argument("--expected-current-version", required=True)
        parser.add_argument("--target-version", required=True)
        parser.add_argument("--expected-target-schema", required=True)
        parser.add_argument("--operator", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        operator = get_user_model().objects.filter(username=options["operator"]).first()
        if operator is None:
            raise CommandError("Operator does not exist")
        try:
            result = activate_claim_extraction_prompt(
                expected_current_version=options["expected_current_version"],
                target_version=options["target_version"],
                expected_target_schema=options["expected_target_schema"],
                operator=operator,
                reason=options["reason"],
            )
        except PromptActivationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "activation_time": result.activation_time.isoformat(),
            "audit_log_id": result.audit_log.pk,
            "new_prompt": result.new_prompt.version,
            "new_schema": result.new_prompt.output_schema_version,
            "old_prompt": result.old_prompt.version,
            "old_schema": result.old_prompt.output_schema_version,
            "operator": operator.get_username(),
        }, sort_keys=True))
