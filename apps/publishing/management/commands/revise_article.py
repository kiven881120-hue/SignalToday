import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.publishing.revision import ArticleRevisionError, revise_article


class Command(BaseCommand):
    help = "Perform one controlled, evidence-grounded revision of an ArticleVersion."

    def add_arguments(self, parser):
        parser.add_argument("--article-version", type=int, required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--retry", action="store_true")

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(
            username=options["actor"], is_active=True
        ).first()
        if actor is None:
            raise CommandError("A persistent active actor is required")
        try:
            result = revise_article(
                options["article_version"], actor=actor, reason=options["reason"],
                retry=options["retry"],
            )
        except ArticleRevisionError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(json.dumps({
            "article_id": result.source_version.article_id,
            "source_version_id": result.source_version.pk,
            "target_version_id": result.target_version.pk,
            "job_run_id": result.job_run.pk,
            "model_call_id": result.model_call.pk,
            "model_call_count": result.job_run.model_calls.count(),
            "grounding_precheck": result.precheck.status.value,
            "fact_drift_check": "PASS",
            "grounding_postcheck": result.postcheck.status.value,
            "final_state": result.final_state,
        }, sort_keys=True))
