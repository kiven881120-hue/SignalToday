import json

from django.core.management.base import BaseCommand, CommandError

from apps.publishing.generation import ArticleGenerationError, generate_article


class Command(BaseCommand):
    help = "Generate one grounded Article draft for an eligible Event."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, required=True)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--retry", action="store_true")

    def handle(self, *args, **options):
        try:
            result = generate_article(
                options["event"], dry_run=options["dry_run"], retry=options["retry"]
            )
        except ArticleGenerationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "event_id": options["event"],
            "job_run_id": result.job_run.pk,
            "status": result.job_run.status,
            "dry_run": result.dry_run,
            "reused": result.reused,
            "eligible": result.eligibility.eligible,
            "reason_code": result.eligibility.reason_code,
            "article_id": result.article.pk if result.article else None,
            "article_version_id": result.article_version.pk if result.article_version else None,
            "model_call_id": result.model_call.pk if result.model_call else None,
            "content_validation_id": (
                result.content_validation.pk if result.content_validation else None
            ),
            "grounding_status": (
                result.content_validation.grounding_status if result.content_validation else None
            ),
            "quality_status": (
                result.content_validation.quality_status if result.content_validation else None
            ),
            "estimated_input_tokens": result.estimated_input_tokens,
            "estimated_cost": str(result.estimated_cost) if result.estimated_cost is not None else None,
        }, sort_keys=True))
