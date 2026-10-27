import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.publishing.brief import (
    ArticleBriefDowngradeError,
    downgrade_article_to_brief,
)


class Command(BaseCommand):
    help = "Apply the controlled Article1 Version3 DOWNGRADE_TO_BRIEF operation."

    def add_arguments(self, parser):
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument("--article", type=int)
        selector.add_argument("--source-version", type=int)
        parser.add_argument("--actor", default="signal_admin")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(username=options["actor"]).first()
        if actor is None:
            raise CommandError("The requested persistent operator does not exist")
        try:
            result = downgrade_article_to_brief(
                actor=actor,
                article_id=options["article"],
                source_version_id=options["source_version"],
                dry_run=options["dry_run"],
            )
        except ArticleBriefDowngradeError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(json.dumps({
            "article_id": result.source_version.article_id,
            "source_version_id": result.source_version.pk,
            "target_version_id": (
                result.target_version.pk if result.target_version else None
            ),
            "job_run_id": result.job_run.pk if result.job_run else None,
            "article_mode": result.article_mode.value,
            "grounding_status": result.grounding.status.value,
            "fact_drift_status": result.fact_drift.status.value,
            "quality_status": result.quality.quality_status.value,
            "quality_level": result.quality.quality_level.value,
            "dry_run": result.dry_run,
            "already_applied": result.already_applied,
            "ready_for_article_review": result.ready_for_article_review,
        }, ensure_ascii=False, sort_keys=True))
