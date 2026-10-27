import json

from django.core.management.base import BaseCommand, CommandError

from apps.events.service import EventClusteringService
from apps.ingestion.models import RawItem


class Command(BaseCommand):
    help = "Cluster unassigned RawItems into Events using deterministic rules."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--source", help="Source identifier")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be at least 1")
        queryset = RawItem.objects.filter(event__isnull=True).order_by("discovered_at", "pk")
        if options["source"]:
            queryset = queryset.filter(source__identifier=options["source"])
        queryset = queryset[: options["limit"]]
        stats, decisions, job_run = EventClusteringService().run(queryset, dry_run=options["dry_run"])
        if options["dry_run"]:
            for raw_item, decision in decisions:
                candidates = [candidate.as_dict() for candidate in decision.candidates]
                self.stdout.write(
                    json.dumps(
                        {
                            "raw_item_id": raw_item.pk,
                            "action": decision.action,
                            "event_id": decision.event.pk if decision.event else None,
                            "confidence": round(decision.confidence, 4),
                            "method": decision.method,
                            "reason": decision.reason,
                            "candidates": candidates,
                        },
                        ensure_ascii=False,
                    )
                )
        self.stdout.write(
            self.style.SUCCESS(
                "event clustering: "
                f"input={stats.input_count} new={stats.new_events} assigned={stats.auto_assigned} "
                f"review={stats.manual_review} skipped={stats.skipped} errors={stats.errors} "
                f"dry_run={options['dry_run']} job_run={job_run.pk if job_run else '-'}"
            )
        )
