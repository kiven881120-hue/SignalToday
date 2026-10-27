from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.ingestion.errors import IngestionError
from apps.sources.config import load_source_definitions
from apps.sources.models import Source


class Command(BaseCommand):
    help = "Validate versioned source configuration and synchronize it idempotently."

    def handle(self, *args, **options):
        try:
            definitions = load_source_definitions()
        except IngestionError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        created = updated = unchanged = 0
        with transaction.atomic():
            for item in definitions:
                values = {
                    "name": item.name, "source_type": item.source_type, "url": item.feed_url,
                    "domain": item.domain, "organization": item.organization,
                    "source_group": item.source_group, "country": item.country,
                    "language": item.language, "tier": item.tier, "reliability": item.reliability,
                    "active": item.active, "crawl_policy": item.crawl_policy,
                    "license_state": item.license_state,
                }
                source, was_created = Source.objects.get_or_create(
                    identifier=item.identifier, defaults=values
                )
                if was_created:
                    created += 1
                    continue
                changed_fields = [
                    field for field, value in values.items()
                    if getattr(source, field) != value
                ]
                if not changed_fields:
                    unchanged += 1
                    continue
                for field in changed_fields:
                    setattr(source, field, values[field])
                source.save(update_fields=[*changed_fields, "updated_at"])
                updated += 1
        self.stdout.write(self.style.SUCCESS(
            f"sources synced: total={len(definitions)} created={created} "
            f"updated={updated} unchanged={unchanged}"
        ))
