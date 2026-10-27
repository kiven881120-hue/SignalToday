import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.ai_content.candidate_review import (
    CandidateReviewError,
    review_candidate_to_supported_claim,
)


class Command(BaseCommand):
    help = "Accept one reviewed Candidate and formally review its Evidence as supported."

    def add_arguments(self, parser):
        parser.add_argument("--candidate", type=int, required=True)
        parser.add_argument("--expected-event", type=int, required=True)
        parser.add_argument("--evidence-hint", type=int, required=True)
        parser.add_argument("--decision", choices=["ACCEPT"], required=True)
        parser.add_argument("--evidence-decision", choices=["SUPPORTED"], required=True)
        parser.add_argument("--operator", required=True)
        parser.add_argument("--candidate-reason", required=True)
        parser.add_argument("--evidence-reason", required=True)

    def handle(self, *args, **options):
        operator = get_user_model().objects.filter(username=options["operator"]).first()
        if operator is None:
            raise CommandError("Operator does not exist")
        try:
            result = review_candidate_to_supported_claim(
                options["candidate"],
                expected_event_id=options["expected_event"],
                evidence_hint_id=options["evidence_hint"],
                actor=operator,
                candidate_reason=options["candidate_reason"],
                evidence_reason=options["evidence_reason"],
            )
        except CandidateReviewError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "candidate_id": result.candidate.pk,
            "candidate_status": result.candidate.status,
            "changed": result.changed,
            "claim_id": result.claim.pk,
            "claim_status": result.claim.verification_status,
            "evidence_id": result.evidence.pk,
            "formal_stance": result.evidence.stance,
            "job_run_id": result.job_run.pk,
        }, sort_keys=True))
