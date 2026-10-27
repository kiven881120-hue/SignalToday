import json

from django.core.management.base import BaseCommand, CommandError

from apps.scoring.refresh import ScoreRefreshError, invalidate_event_score


class Command(BaseCommand):
    help = "Invalidate the current active-rule Score for one Event."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        try:
            result = invalidate_event_score(options["event"], options["reason"])
        except ScoreRefreshError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "event_id": options["event"],
            "score_id": result.score.pk if result.score else None,
            "changed": result.changed,
            "reason_code": result.reason_code,
            "status": result.score.status if result.score else None,
        }, sort_keys=True))
