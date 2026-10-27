import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.ingestion.errors import ErrorCode, IngestionError
from apps.sources.config import load_source_definitions
from apps.sources.models import Source


class SourceConfigurationTests(TestCase):
    def test_sync_sources_is_idempotent(self):
        output = StringIO()
        call_command("sync_sources", stdout=output)
        expected_count = len(load_source_definitions())
        self.assertEqual(Source.objects.count(), expected_count)
        output = StringIO()
        call_command("sync_sources", stdout=output)
        self.assertEqual(Source.objects.count(), expected_count)
        self.assertEqual(Source.objects.filter(source_type="OFFICIAL", tier="A").count(), expected_count)
        self.assertEqual(Source.objects.exclude(source_group="").count(), expected_count)
        self.assertIn(f"created=0 updated=0 unchanged={expected_count}", output.getvalue())

    def test_invalid_configuration_never_reaches_database(self):
        payload = {
            "identifier": "bad", "name": "Bad", "feed_url": "file:///etc/passwd",
            "domain": "", "organization": "Test", "source_group": "test",
            "feed_type": "RSS", "category": "WORLD", "notes": "",
            "compliance_note": "Discovery only.",
            "source_type": "OFFICIAL", "country": "US",
            "language": "en", "tier": "A", "reliability": 0.5, "active": True,
            "crawl_policy": {"feed_only": True}, "license_state": "UNKNOWN",
        }
        with TemporaryDirectory() as directory:
            Path(directory, "bad.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(IngestionError) as caught:
                load_source_definitions(Path(directory))
        self.assertEqual(caught.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertEqual(Source.objects.count(), 0)

    def test_configuration_rejects_embedded_credentials(self):
        payload = {
            "identifier": "bad", "name": "Bad", "feed_url": "https://user:pass@example.com/feed",
            "domain": "example.com", "organization": "Test", "source_group": "test",
            "feed_type": "RSS", "category": "WORLD", "notes": "",
            "compliance_note": "Discovery only.",
            "source_type": "OFFICIAL", "country": "US",
            "language": "en", "tier": "A", "reliability": 0.5, "active": True,
            "crawl_policy": {"feed_only": True}, "license_state": "UNKNOWN",
        }
        with TemporaryDirectory() as directory:
            Path(directory, "bad.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(IngestionError):
                load_source_definitions(Path(directory))

    def test_configuration_rejects_unknown_feed_type(self):
        payload = {
            "identifier": "bad", "name": "Bad", "feed_url": "https://example.com/feed",
            "domain": "example.com", "organization": "Test", "source_group": "test",
            "feed_type": "JSON", "category": "WORLD", "notes": "",
            "compliance_note": "Discovery only.",
            "source_type": "OFFICIAL", "country": "US",
            "language": "en", "tier": "A", "reliability": 0.5, "active": True,
            "crawl_policy": {"feed_only": True}, "license_state": "UNKNOWN",
        }
        with TemporaryDirectory() as directory:
            Path(directory, "bad.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(IngestionError):
                load_source_definitions(Path(directory))

    def test_configuration_rejects_unbounded_article_body_fetch(self):
        payload = {
            "identifier": "bad", "name": "Bad", "feed_url": "https://example.com/feed",
            "domain": "example.com", "organization": "Test", "source_group": "test",
            "feed_type": "RSS", "category": "WORLD", "notes": "",
            "compliance_note": "Discovery only.", "source_type": "OFFICIAL", "country": "US",
            "language": "en", "tier": "A", "reliability": 0.5, "active": True,
            "crawl_policy": {"feed_only": True, "fetch_article_body": True},
            "license_state": "UNKNOWN",
        }
        with TemporaryDirectory() as directory:
            Path(directory, "bad.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(IngestionError):
                load_source_definitions(Path(directory))

    def test_scoped_source_requires_valid_freshness_and_limit(self):
        payload = {
            "identifier": "bad", "name": "Bad", "feed_url": "https://example.com/sitemap.xml",
            "domain": "example.com", "organization": "Test", "source_group": "test",
            "feed_type": "SITEMAP", "category": "WORLD", "notes": "",
            "compliance_note": "Discovery only.", "source_type": "OFFICIAL", "country": "US",
            "language": "en", "tier": "A", "reliability": 0.5, "active": True,
            "crawl_policy": {
                "feed_only": True, "collection_method": "sitemap",
                "path_allow_prefixes": ["/news"], "source_freshness_days": 30,
            },
            "license_state": "UNKNOWN",
        }
        with TemporaryDirectory() as directory:
            Path(directory, "bad.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(IngestionError):
                load_source_definitions(Path(directory))

    def test_sync_updates_existing_source_by_identifier(self):
        definitions = load_source_definitions()
        with patch("apps.sources.management.commands.sync_sources.load_source_definitions", return_value=definitions):
            call_command("sync_sources", stdout=StringIO())
            changed = list(definitions)
            nasa_index = next(
                index for index, definition in enumerate(changed)
                if definition.identifier == "nasa"
            )
            changed[nasa_index] = type(changed[nasa_index])(
                **{**changed[nasa_index].__dict__, "name": "Updated NASA"}
            )
            with patch("apps.sources.management.commands.sync_sources.load_source_definitions", return_value=changed):
                call_command("sync_sources", stdout=StringIO())
        self.assertEqual(Source.objects.get(identifier="nasa").name, "Updated NASA")
