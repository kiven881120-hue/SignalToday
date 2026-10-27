import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.publishing.regeneration import ArticleRegenerationError, regenerate_article


class Command(BaseCommand):
    help = "Explicitly regenerate an existing Event into a new ArticleVersion."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--request-id", required=True)

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(
            username=options["actor"], is_active=True
        ).first()
        if actor is None:
            raise CommandError("A persistent active actor is required")
        try:
            result = regenerate_article(
                options["event"], actor=actor, reason=options["reason"],
                request_id=options["request_id"],
            )
        except ArticleRegenerationError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(json.dumps({
            "event_id": options["event"], "article_id": result.target_version.article_id,
            "source_version_id": result.source_version.pk,
            "target_version_id": result.target_version.pk,
            "target_version_number": result.target_version.version_number,
            "job_run_id": result.job_run.pk, "model_call_id": result.model_call.pk,
            "prompt_version": result.model_call.prompt_version,
            "schema_version": result.model_call.schema_version,
            "content_validation_id": result.content_validation.pk,
            "grounding_status": result.content_validation.grounding_status,
            "quality_status": result.content_validation.quality_status,
            "review_id": result.review.pk if result.review else None,
            "review_status": result.review.decision if result.review else None,
            "reused": result.reused,
        }, ensure_ascii=False, sort_keys=True))
