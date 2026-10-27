import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.ai_content.evidence_hint_refresh import (
    EvidenceHintRefreshError,
    refresh_claim_evidence_hint,
)


class Command(BaseCommand):
    help = "Create an append-only, current-body-validated replacement EvidenceHint."

    def add_arguments(self, parser):
        parser.add_argument("--candidate", type=int, required=True)
        parser.add_argument("--raw-item", type=int, required=True)
        parser.add_argument("--quote", required=True)
        parser.add_argument("--operator", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--expected-old-hint", type=int, required=True)

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(username=options["operator"]).first()
        if actor is None:
            raise CommandError("Operator does not exist")
        try:
            result = refresh_claim_evidence_hint(
                options["candidate"],
                raw_item_id=options["raw_item"],
                quote=options["quote"],
                actor=actor,
                reason=options["reason"],
                expected_old_hint_id=options["expected_old_hint"],
            )
        except EvidenceHintRefreshError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "candidate_id": result.new_hint.candidate_id,
            "old_hint_id": result.old_hint.pk,
            "old_status": result.old_hint.status,
            "new_hint_id": result.new_hint.pk,
            "new_status": result.new_hint.status,
            "audit_id": result.audit.pk,
        }, sort_keys=True))
