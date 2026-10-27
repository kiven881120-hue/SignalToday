import json

from django.core.management.base import BaseCommand

from apps.claims.evaluation import ClaimEvaluationService
from apps.claims.models import Claim, VerificationStatus


class Command(BaseCommand):
    help = "Evaluate Claim verification statuses using deterministic evidence rules."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--event", type=int)
        parser.add_argument("--status", choices=VerificationStatus.values)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        queryset = Claim.objects.order_by("pk")
        if options["event"] is not None:
            queryset = queryset.filter(event_id=options["event"])
        if options["status"]:
            queryset = queryset.filter(verification_status=options["status"])
        queryset = queryset[: max(options["limit"], 0)]
        stats, results, job_run = ClaimEvaluationService().run(queryset, dry_run=options["dry_run"])
        for claim, result in results:
            self.stdout.write(json.dumps({
                "claim_id": claim.pk,
                "event_id": claim.event_id,
                **result.as_dict(),
            }, ensure_ascii=False, sort_keys=True))
        self.stdout.write(json.dumps({
            "dry_run": options["dry_run"],
            "job_run_id": job_run.pk if job_run else None,
            "stats": stats,
        }, ensure_ascii=False, sort_keys=True))
