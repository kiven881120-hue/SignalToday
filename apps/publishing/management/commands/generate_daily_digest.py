import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.publishing.digest import DailyDigestError, generate_daily_digest
from apps.publishing.models import DigestCategory


class Command(BaseCommand):
    help = "Build one deterministic Daily Digest from published Articles."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--rebuild", action="store_true")

    def handle(self, *args, **options):
        try:
            digest_date = date.fromisoformat(options["date"])
            result = generate_daily_digest(digest_date, dry_run=options["dry_run"], rebuild=options["rebuild"])
        except (ValueError, DailyDigestError) as exc:
            raise CommandError(str(exc)) from exc
        category_counts = {
            category: sum(item.category == category for item in result.selected)
            for category in DigestCategory.values
        }
        self.stdout.write(json.dumps({
            "digest_date": options["date"], "digest_id": result.digest.pk if result.digest else None,
            "job_run_id": result.job_run.pk if result.job_run else None, "changed": result.changed,
            "dry_run": result.dry_run, "candidate_count": len(result.candidates),
            "selected_count": len(result.selected), "must_know_count": category_counts["MUST_KNOW"],
            "category_counts": category_counts,
            "items": [{"article_id": item.article.pk, "article_version_id": item.article_version.pk, "event_id": item.article.event_id, "final_score": str(item.score.final_score), "recommendation": item.score.recommendation_level, "category": item.category, "recommendation_reason": item.recommendation_reason} for item in result.selected],
            "exclusions": [{"article_id": item.article_id, "article_version_id": item.article_version_id, "reason_code": item.reason_code} for item in result.exclusions],
        }, ensure_ascii=False, sort_keys=True))
