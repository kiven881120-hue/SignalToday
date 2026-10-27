import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.publishing.review import ArticleReviewError


class ArticleReviewCommand(BaseCommand):
    service = None

    def add_arguments(self, parser):
        parser.add_argument("--article-version", type=int, required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--actor", required=True, help="Username of the human operator")

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(username=options["actor"], is_active=True).first()
        if actor is None:
            raise CommandError("Active actor username does not exist")
        try:
            result = self.run_service(options, actor)
        except ArticleReviewError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(json.dumps({
            "action": result.action,
            "article_id": result.article.pk,
            "article_version_id": result.article_version.pk,
            "status": result.article_version.status,
            "changed": result.changed,
            "review_id": result.review.pk if result.review else None,
            "job_run_id": result.job_run.pk,
        }, sort_keys=True))

    def run_service(self, options, actor):
        return self.service(options["article_version"], options["reason"], actor)
