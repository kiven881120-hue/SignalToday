from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.ingestion.http import FetchResult
from apps.ingestion.models import RawItem
from apps.ingestion.service import FeedIngestionService
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun
from apps.sources.models import Source

from tests.unit.test_feed_parser import RSS


PUBLIC_IP = "93.184.216.34"


class FakeHttpClient:
    resolver = staticmethod(lambda host, port: (PUBLIC_IP,))

    def fetch(self, url):
        return FetchResult(url=url, status=200, content_type="application/rss+xml", body=RSS)


class ArticleHttpClient(FakeHttpClient):
    def __init__(self):
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        if url == "https://example.com/feed":
            return super().fetch(url)
        body = ("<html><body><article><h1>RSS item</h1><div class='author'>By Person</div><p>" +
                ("complete body " * 250) + "</p></article></body></html>").encode()
        return FetchResult(url=url, status=200, content_type="text/html", body=body)


def _iso(delta_days):
    return (timezone.now() - timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class SitemapHttpClient(FakeHttpClient):
    def fetch(self, url):
        recent = _iso(1)
        items = "".join(
            f"<url><loc>https://example.com/news/item-{index}</loc>"
            f"<lastmod>{recent}</lastmod></url>"
            for index in range(25)
        )
        body = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'.encode()
        return FetchResult(url=url, status=200, content_type="application/xml", body=body)


class ScopedSitemapHttpClient(FakeHttpClient):
    def fetch(self, url):
        fresh = _iso(1)
        stale = _iso(400)
        body = f"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.com/news/press-releases/current</loc><lastmod>{fresh}</lastmod></url>
        <url><loc>https://example.com/news/press-releases/old</loc><lastmod>{stale}</lastmod></url>
        <url><loc>https://example.com/about/history</loc><lastmod>{fresh}</lastmod></url>
        </urlset>""".encode()
        return FetchResult(url=url, status=200, content_type="application/xml", body=body)


class FeedIngestionServiceTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(
            identifier="test-feed",
            name="Test Feed",
            source_type="OFFICIAL",
            url="https://example.com/feed",
            domain="example.com",
            tier="A",
            reliability=0.9,
            crawl_policy={"feed_only": True},
        )
        self.service = FeedIngestionService(http_client=FakeHttpClient())

    def test_creates_raw_item_job_run_and_all_steps(self):
        run, stats = self.service.ingest_source(self.source, idempotency_key="ingest-1")
        item = RawItem.objects.get()
        self.assertEqual((stats.entries, stats.created, stats.failed), (1, 1, 0))
        self.assertEqual(run.status, JobStatus.SUCCESS)
        self.assertEqual(run.steps.count(), 7)
        self.assertFalse(run.steps.exclude(status=JobStatus.SUCCESS).exists())
        self.assertEqual(item.raw_metadata["summary"], "Short summary .")
        self.assertEqual(item.raw_metadata["body"], "Short summary .")
        self.assertEqual(item.raw_metadata["content_status"], "READY")
        self.assertIsNone(item.content_updated_at)
        self.assertEqual(item.status, "DISCOVERED")

    def test_repeated_feed_does_not_create_duplicate_raw_items(self):
        self.service.ingest_source(self.source, idempotency_key="ingest-a")
        _, stats = self.service.ingest_source(self.source, idempotency_key="ingest-b")
        self.assertEqual(RawItem.objects.count(), 1)
        self.assertEqual((stats.created, stats.duplicates), (0, 1))

    def test_repeated_idempotency_key_returns_original_result(self):
        first_run, first_stats = self.service.ingest_source(self.source, idempotency_key="same-task")
        second_run, second_stats = self.service.ingest_source(self.source, idempotency_key="same-task")
        self.assertEqual(first_run.pk, second_run.pk)
        self.assertEqual(first_stats, second_stats)
        self.assertEqual(JobRun.objects.count(), 1)

    def test_dry_run_creates_no_raw_item(self):
        run, stats = self.service.ingest_source(self.source, dry_run=True, idempotency_key="dry")
        self.assertEqual(run.status, JobStatus.SUCCESS)
        self.assertEqual(stats.created, 1)
        self.assertEqual(RawItem.objects.count(), 0)

    def test_inactive_source_is_rejected_and_audited(self):
        self.source.active = False
        self.source.save(update_fields=["active"])
        with self.assertRaises(Exception):
            self.service.ingest_source(self.source, idempotency_key="inactive")
        run = JobRun.objects.get(idempotency_key="inactive")
        self.assertEqual(run.status, JobStatus.FAILED)
        self.assertIn("SECURITY_REJECTED", run.error_message)

    def test_database_constraints_protect_canonical_and_hash_dedupe(self):
        self.service.ingest_source(self.source, idempotency_key="constraints")
        original = RawItem.objects.get()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RawItem.objects.create(
                    source=self.source, external_id="other", url="https://example.com/other",
                    canonical_url=original.canonical_url, title="Other", discovered_at=original.discovered_at,
                    content_hash="different",
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RawItem.objects.create(
                    source=self.source, external_id="third", url="https://example.com/third",
                    canonical_url="https://example.com/third", title="Third", discovered_at=original.discovered_at,
                    content_hash=original.content_hash,
                )

    def test_first_import_is_limited_to_twenty_recent_items(self):
        source = Source.objects.create(
            identifier="test-sitemap",
            name="Test Sitemap",
            source_type="OFFICIAL",
            url="https://example.com/sitemap.xml",
            domain="example.com",
            tier="A",
            reliability=0.9,
            crawl_policy={"feed_only": True, "collection_method": "sitemap", "first_import_limit": 20},
        )
        service = FeedIngestionService(http_client=SitemapHttpClient())
        _, stats = service.ingest_source(source, idempotency_key="sitemap-limit")
        self.assertEqual((stats.entries, stats.created), (20, 20))
        self.assertEqual(RawItem.objects.filter(source=source).count(), 20)
        item = RawItem.objects.filter(source=source).first()
        self.assertIsNone(item.published_at)
        self.assertIsNotNone(item.content_updated_at)

    def test_sitemap_scope_filters_before_persistence_without_fetching_articles(self):
        source = Source.objects.create(
            identifier="scoped-sitemap",
            name="Scoped Sitemap",
            source_type="OFFICIAL",
            url="https://example.com/sitemap.xml",
            domain="example.com",
            tier="A",
            reliability=0.9,
            crawl_policy={
                "feed_only": True,
                "collection_method": "sitemap",
                "path_allow_prefixes": ["/news/press-releases"],
                "source_freshness_days": 30,
                "source_scope_limit": 100,
            },
        )
        service = FeedIngestionService(http_client=ScopedSitemapHttpClient())
        run, stats = service.ingest_source(source, idempotency_key="scoped-sitemap")
        item = RawItem.objects.get(source=source)
        scope = run.steps.get(step_name="normalize").output_summary["source_scope"]
        self.assertEqual((stats.entries, stats.created), (1, 1))
        self.assertEqual((scope["input_count"], scope["kept_count"]), (3, 1))
        self.assertEqual((scope["denied_by_path"], scope["denied_stale"]), (1, 1))
        self.assertIsNone(item.published_at)
        self.assertIsNotNone(item.content_updated_at)

    def test_article_body_is_sanitized_stored_in_full_and_idempotent(self):
        self.source.crawl_policy = {
            "feed_only": True, "fetch_article_body": True, "article_body_limit": 1,
        }
        self.source.save(update_fields=["crawl_policy"])
        client = ArticleHttpClient()
        service = FeedIngestionService(http_client=client)
        _, first = service.ingest_source(self.source, idempotency_key="article-first")
        item = RawItem.objects.get()
        self.assertGreater(len(item.raw_metadata["body"]), 2000)
        self.assertEqual(item.raw_metadata["body_source"], "article_html")
        self.assertNotIn("RSS item", item.raw_metadata["body"])
        self.assertNotIn("By Person", item.raw_metadata["body"])
        _, second = service.ingest_source(self.source, idempotency_key="article-second")
        self.assertEqual((first.created, second.created, second.updated), (1, 0, 0))
        self.assertEqual(RawItem.objects.count(), 1)

    def test_cross_domain_article_is_not_fetched_or_used_as_body(self):
        external_rss = RSS.replace(b"https://example.com/a", b"https://other.example/a")

        class CrossDomainClient(ArticleHttpClient):
            def fetch(inner_self, url):
                inner_self.urls.append(url)
                if url == "https://example.com/feed":
                    return FetchResult(url=url, status=200, content_type="application/rss+xml", body=external_rss)
                raise AssertionError("cross-domain article must not be fetched")

        self.source.crawl_policy = {
            "feed_only": True, "fetch_article_body": True, "article_body_limit": 1,
        }
        self.source.save(update_fields=["crawl_policy"])
        client = CrossDomainClient()
        FeedIngestionService(http_client=client).ingest_source(self.source, idempotency_key="cross-domain")
        item = RawItem.objects.get()
        self.assertEqual(client.urls, ["https://example.com/feed"])
        self.assertEqual(item.raw_metadata["body_source"], "feed_summary")

    def test_empty_article_extraction_is_marked_parse_failed(self):
        empty_rss = RSS.replace(
            b"<description><![CDATA[<p>Short <b>summary</b>.</p><script>bad()</script>]]></description>",
            b"<description></description>",
        )

        class EmptyArticleClient(ArticleHttpClient):
            def fetch(inner_self, url):
                if url == "https://example.com/feed":
                    return FetchResult(url=url, status=200, content_type="application/rss+xml", body=empty_rss)
                return FetchResult(
                    url=url, status=200, content_type="text/html",
                    body=b"<html><body><nav>Only navigation</nav></body></html>",
                )

        self.source.crawl_policy = {
            "feed_only": True, "fetch_article_body": True, "article_body_limit": 1,
        }
        self.source.save(update_fields=["crawl_policy"])
        FeedIngestionService(http_client=EmptyArticleClient()).ingest_source(
            self.source, idempotency_key="parse-failed",
        )
        item = RawItem.objects.get()
        self.assertEqual(item.raw_metadata["body"], "")
        self.assertEqual(item.raw_metadata["content_status"], "PARSE_FAILED")
