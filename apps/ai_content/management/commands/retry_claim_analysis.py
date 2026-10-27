import json

from django.core.management.base import BaseCommand, CommandError

from apps.ai_content.retry import RetryNotAllowed, retry_claim_analysis


class Command(BaseCommand):
    help = "Safely retry one failed Claim Extraction JobRun."

    def add_arguments(self, parser):
        parser.add_argument("--job-run", type=int, required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        try:
            result = retry_claim_analysis(options["job_run"], reason=options["reason"])
        except RetryNotAllowed as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "job_run_id": result.job_run.pk,
            "job_step_id": result.job_step.pk if result.job_step else None,
            "model_call_id": result.model_call.pk if result.model_call else None,
            "status": result.job_run.status,
            "claim_candidates": len(result.candidates),
            "error": result.error,
        }, sort_keys=True))
