import json

from django.core.management.base import BaseCommand, CommandError

from apps.events.models import Event, EventStatus
from apps.scoring.evaluation import EventScoringService, get_rule_version
from apps.scoring.models import ScoreRuleVersion


class Command(BaseCommand):
    help = "Score Events with the deterministic, versioned information-value rules."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--event", type=int)
        parser.add_argument("--status", choices=EventStatus.values)
        parser.add_argument("--rule-version")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            rule = get_rule_version(options["rule_version"])
        except ScoreRuleVersion.DoesNotExist as exc:
            raise CommandError(str(exc)) from exc
        queryset = Event.objects.order_by("pk")
        if options["event"] is not None:
            queryset = queryset.filter(pk=options["event"])
        if options["status"]:
            queryset = queryset.filter(status=options["status"])
        queryset = queryset[: max(options["limit"], 0)]
        stats, results, job_run = EventScoringService().run(queryset, rule_version=rule, dry_run=options["dry_run"])
        for event, result in results:
            self.stdout.write(json.dumps({"event_id": event.pk, **result.output_dict()}, ensure_ascii=False, sort_keys=True))
        self.stdout.write(json.dumps({"dry_run": options["dry_run"], "job_run_id": job_run.pk if job_run else None, "stats": stats}, ensure_ascii=False, sort_keys=True))
