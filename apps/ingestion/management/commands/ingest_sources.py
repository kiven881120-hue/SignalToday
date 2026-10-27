from django.core.management.base import BaseCommand, CommandError

from apps.ingestion.errors import IngestionError
from apps.ingestion.service import FeedIngestionService
from apps.sources.models import Source


class Command(BaseCommand):
    help = "Ingest active configured RSS/Atom sources once."

    def add_arguments(self, parser):
        parser.add_argument("--source", help="Source identifier")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--idempotency-key")

    def handle(self, *args, **options):
        sources = Source.objects.filter(active=True).order_by("identifier")
        if options["source"]:
            sources = sources.filter(identifier=options["source"])
        if options["idempotency_key"] and not options["source"]:
            raise CommandError("--idempotency-key requires --source")
        if not sources.exists():
            raise CommandError("No matching active source")

        totals = {"sources": 0, "created": 0, "updated": 0, "duplicates": 0, "failed": 0}
        service = FeedIngestionService()
        for source in sources:
            try:
                _, stats = service.ingest_source(
                    source,
                    dry_run=options["dry_run"],
                    idempotency_key=options["idempotency_key"],
                )
                totals["sources"] += 1
                totals["created"] += stats.created
                totals["updated"] += stats.updated
                totals["duplicates"] += stats.duplicates
            except IngestionError as exc:
                totals["failed"] += 1
                self.stderr.write(f"source={source.identifier} code={exc.code}")
        self.stdout.write(
            f"ingestion: sources={totals['sources']} created={totals['created']} updated={totals['updated']} "
            f"duplicates={totals['duplicates']} failed={totals['failed']} dry_run={options['dry_run']}"
        )
        if totals["failed"]:
            raise CommandError("One or more sources failed")
