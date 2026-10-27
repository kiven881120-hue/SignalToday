import json

from django.core.management.base import BaseCommand, CommandError

from apps.scoring.refresh import ScoreRefreshError, refresh_event_score


class Command(BaseCommand):
    help = "Refresh the current active-rule Score for one Event."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, required=True)

    def handle(self, *args, **options):
        try:
            result = refresh_event_score(options["event"])
        except ScoreRefreshError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "event_id": options["event"],
            "score_id": result.score.pk,
            "changed": result.changed,
            "status": result.score.status,
            "final_score": str(result.score.final_score),
            "recommendation_level": result.score.recommendation_level,
            "job_run_id": result.job_run.pk,
            "job_step_id": result.job_step.pk,
        }, sort_keys=True))
