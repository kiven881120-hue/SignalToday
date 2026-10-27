import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.jobs.operations import get_operations_status


class Command(BaseCommand):
    help = "Show read-only Source, Pipeline, Article, Digest, and LLM operations status."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Business date in YYYY-MM-DD format")

    def handle(self, *args, **options):
        try:
            business_date = date.fromisoformat(options["date"]) if options["date"] else None
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc
        self.stdout.write(json.dumps(
            get_operations_status(business_date=business_date),
            ensure_ascii=False,
            sort_keys=True,
        ))
