from django.test import SimpleTestCase

from apps.ingestion.errors import ErrorCode, IngestionError
from apps.ingestion.feed import clean_summary, extract_article_body, parse_feed, parse_html_list, parse_sitemap


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title><item>
<guid>rss-1</guid><title>RSS item</title><link>https://example.com/a</link>
<pubDate>Tue, 09 Sep 2025 12:00:00 GMT</pubDate>
<description><![CDATA[<p>Short <b>summary</b>.</p><script>bad()</script>]]></description>
</item></channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title><entry>
<id>atom-1</id><title>Atom item</title><link rel="alternate" href="https://example.com/b"/>
<updated>2025-09-09T12:00:00Z</updated><summary>Atom summary</summary>
</entry></feed>"""

ATOM_WITH_PUBLISHED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title><entry>
<id>atom-2</id><title>Published Atom item</title><link rel="alternate" href="https://example.com/c"/>
<published>2025-09-08T12:00:00Z</published><updated>2025-09-09T12:00:00Z</updated>
</entry></feed>"""

SITEMAP = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/news/official-policy</loc><lastmod>2026-09-11T12:00:00Z</lastmod></url>
</urlset>"""

HTML_LIST = b"""<html><body><nav><a href='/'>Home</a></nav>
<a href='/news/2026/09/11/policy-update.html'>Policy update</a>
<a href='https://other.example/news/2026/09/11/nope'>Other</a></body></html>"""

ARTICLE_HTML = b"""<html><body><header>Site name</header><main class="article-content">
<h1>Policy update title</h1><div class="author">Reporter Name</div>
<p>First factual paragraph.</p><script>steal()</script><style>.bad{}</style>
<nav>Related links</nav><form>Subscribe</form><p>Second factual paragraph.</p>
</main><footer>Copyright</footer></body></html>"""


class FeedParserTests(SimpleTestCase):
    def test_parses_rss_2(self):
        entry = parse_feed(RSS)[0]
        self.assertEqual((entry.external_id, entry.title, entry.metadata["feed_format"]), ("rss-1", "RSS item", "rss2"))
        self.assertEqual(entry.summary, "Short summary .")
        self.assertIsNotNone(entry.published_at.tzinfo)

    def test_parses_atom(self):
        entry = parse_feed(ATOM)[0]
        self.assertEqual((entry.external_id, entry.url, entry.metadata["feed_format"]), ("atom-1", "https://example.com/b", "atom"))
        self.assertIsNone(entry.published_at)
        self.assertIsNotNone(entry.content_updated_at)

    def test_atom_published_and_updated_use_distinct_fields(self):
        entry = parse_feed(ATOM_WITH_PUBLISHED)[0]
        self.assertEqual(entry.published_at.isoformat(), "2025-09-08T12:00:00+00:00")
        self.assertEqual(entry.content_updated_at.isoformat(), "2025-09-09T12:00:00+00:00")

    def test_parses_sitemap_metadata_without_fetching_pages(self):
        entry = parse_sitemap(SITEMAP)[0]
        self.assertEqual(entry.url, "https://example.com/news/official-policy")
        self.assertEqual(entry.title, "official policy")
        self.assertEqual(entry.metadata["feed_format"], "sitemap")
        self.assertIsNone(entry.published_at)
        self.assertIsNotNone(entry.content_updated_at)

    def test_parses_same_site_html_list_links_without_inventing_publish_dates(self):
        entries = parse_html_list(HTML_LIST, "https://example.com/news/")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Policy update")
        self.assertEqual(entries[0].metadata["feed_format"], "html-list")
        self.assertIsNone(entries[0].published_at)

    def test_compact_html_url_date_is_not_treated_as_published_at(self):
        entries = parse_html_list(
            b'<a href="/news/20260911/item.html">Compact date</a>',
            "https://example.com/news/",
        )
        self.assertIsNone(entries[0].published_at)

    def test_rejects_malformed_xml(self):
        with self.assertRaises(IngestionError) as caught:
            parse_feed(b"<rss><broken></rss>")
        self.assertEqual(caught.exception.code, ErrorCode.PARSE_ERROR)

    def test_rejects_xxe_external_entity(self):
        malicious = b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&xxe;</title></item></channel></rss>'
        with self.assertRaises(IngestionError) as caught:
            parse_feed(malicious)
        self.assertEqual(caught.exception.code, ErrorCode.PARSE_ERROR)

    def test_summary_removes_dangerous_html_and_limits_length(self):
        value = '<p onclick="x">hello</p><iframe>secret</iframe><form>hidden</form>' + ("x" * 3000)
        cleaned = clean_summary(value)
        self.assertNotIn("secret", cleaned)
        self.assertNotIn("hidden", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertEqual(len(cleaned), 2000)

    def test_extracts_article_body_without_metadata_or_dangerous_regions(self):
        body = extract_article_body(ARTICLE_HTML, title="Policy update title")
        self.assertEqual(body, "First factual paragraph.\n\nSecond factual paragraph.")
        for excluded in ("Policy update title", "Reporter Name", "Site name", "steal", "Related", "Subscribe"):
            self.assertNotIn(excluded, body)

    def test_article_body_is_not_truncated(self):
        text = "x" * 5000
        body = extract_article_body(f'<article><p>{text}</p></article>'.encode())
        self.assertEqual(body, text)
