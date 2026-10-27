import json

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model

from apps.claims.importance_review import ImportanceReviewError, review_claim_importance


class Command(BaseCommand):
    help = "Review one Claim importance/type and safely refresh its current Event Score."

    def add_arguments(self, parser):
        parser.add_argument("--claim", type=int, required=True)
        parser.add_argument("--importance", required=True, help="Integer 0-100 or null")
        parser.add_argument("--expected-importance", required=True, help="Integer 0-100 or null")
        parser.add_argument("--expected-status", required=True)
        parser.add_argument("--expected-claim-type", required=True)
        parser.add_argument("--claim-type")
        parser.add_argument("--actor", required=True, help="Username of the human operator")
        parser.add_argument("--reason", required=True)

    @staticmethod
    def _importance(value):
        if value.lower() == "null":
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise CommandError("Importance must be an integer 0-100 or null") from exc
        if str(parsed) != value.strip() or not 0 <= parsed <= 100:
            raise CommandError("Importance must be an integer 0-100 or null")
        return parsed

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(username=options["actor"]).first()
        if actor is None:
            raise CommandError("Actor does not exist")
        try:
            result = review_claim_importance(
                options["claim"],
                importance=self._importance(options["importance"]),
                actor=actor,
                reason=options["reason"],
                expected_current_importance=self._importance(options["expected_importance"]),
                expected_current_verification_status=options["expected_status"],
                expected_current_claim_type=options["expected_claim_type"],
                claim_type=options["claim_type"],
            )
        except ImportanceReviewError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "claim_id": result.claim.pk,
            "importance": result.claim.importance,
            "claim_type": result.claim.claim_type,
            "changed": result.changed,
            "correlation_key": result.correlation_key,
            "evaluation_job_run_id": result.evaluation_job_run_id,
            "refresh_job_run_id": result.refresh_job_run_id,
        }, sort_keys=True))
