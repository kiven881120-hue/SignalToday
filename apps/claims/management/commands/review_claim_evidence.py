import json

from django.core.management.base import BaseCommand, CommandError

from apps.claims.evidence_review import EvidenceReviewError, review_claim_evidence
from apps.claims.models import EvidenceStance


class Command(BaseCommand):
    help = "Review one ClaimEvidence transition from MENTIONS to SUPPORTS."

    def add_arguments(self, parser):
        parser.add_argument("--evidence", type=int, required=True)
        parser.add_argument("--stance", choices=[EvidenceStance.SUPPORTS], required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        try:
            result = review_claim_evidence(
                options["evidence"],
                new_stance=options["stance"],
                reason=options["reason"],
            )
        except EvidenceReviewError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "evidence_id": result.evidence.pk,
            "stance": result.evidence.stance,
            "changed": result.changed,
        }, sort_keys=True))
