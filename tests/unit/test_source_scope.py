from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from apps.ingestion.feed import FeedEntry
from apps.ingestion.service import normalize_entry
from apps.ingestion.source_scope import apply_source_scope, source_scope_reason
from apps.sources.config import load_source_definitions


NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
POLICY = {
    "path_allow_prefixes": ["/briefings-statements", "/presidential-actions", "/fact-sheets", "/releases"],
    "path_allow_patterns": [
        r"^/(?:briefings-statements|presidential-actions|fact-sheets|releases)/[0-9]{4}/[0-9]{2}/[^/]+$",
    ],
    "path_deny_prefixes": ["/research", "/remarks"],
    "source_freshness_days": 30,
    "source_scope_limit": 2,
}


def entry(path, *, updated_at=NOW):
    return normalize_entry(FeedEntry(
        external_id=path,
        url=f"https://www.whitehouse.gov{path}",
        title="Policy action",
        published_at=None,
        content_updated_at=updated_at,
        summary="",
    ))


class SourceScopeTests(SimpleTestCase):
    def test_keeps_only_fresh_detail_pages_under_allowlisted_paths(self):
        entries = [
            entry("/briefings-statements/2026/09/policy-action/"),
            entry("/presidential-actions/2026/09/executive-order/", updated_at=NOW - timedelta(days=1)),
            entry("/fact-sheets/2026/09/fact-sheet/", updated_at=NOW - timedelta(days=2)),
            entry("/research/2026/09/report/"),
            entry("/remarks/2026/09/speech/"),
        ]
        kept, stats = apply_source_scope(entries, POLICY, now=NOW)
        self.assertEqual([item.url for item in kept], [entries[0].url, entries[1].url])
        self.assertEqual(stats.as_dict(), {
            "input_count": 5,
            "allowed_by_path": 3,
            "denied_by_path": 2,
            "denied_stale": 0,
            "denied_missing_time": 0,
            "denied_limit": 1,
            "kept_count": 2,
            "filtered_count": 3,
        })

    def test_rejects_section_indexes_stale_items_and_missing_lastmod(self):
        self.assertEqual(source_scope_reason(entry("/releases/"), POLICY, now=NOW), "path_denied")
        self.assertEqual(
            source_scope_reason(entry("/releases/historical-page/"), POLICY, now=NOW),
            "path_denied",
        )
        self.assertEqual(
            source_scope_reason(entry("/releases/2025/01/old/", updated_at=NOW - timedelta(days=31)), POLICY, now=NOW),
            "stale_content_updated_at",
        )
        self.assertEqual(
            source_scope_reason(entry("/releases/2026/09/unknown/", updated_at=None), POLICY, now=NOW),
            "missing_content_updated_at",
        )

    def test_scope_never_turns_sitemap_lastmod_into_published_at(self):
        scoped = entry("/releases/2026/09/current/")
        kept, _ = apply_source_scope([scoped], POLICY, now=NOW)
        self.assertIsNone(kept[0].published_at)
        self.assertEqual(kept[0].content_updated_at, NOW)

    def test_unconfigured_sources_are_unchanged(self):
        entries = [entry("/anything/")]
        kept, stats = apply_source_scope(entries, {}, now=NOW)
        self.assertEqual(kept, entries)
        self.assertEqual(stats.kept_count, 1)

    def test_path_only_scope_does_not_require_or_invent_a_timestamp(self):
        policy = {
            "path_allow_prefixes": ["/news"],
            "path_allow_patterns": [r"^/news/[0-9]+\.html$"],
        }
        scoped = entry("/news/123.html", updated_at=None)
        kept, stats = apply_source_scope([scoped], policy, now=NOW)
        self.assertEqual(kept, [scoped])
        self.assertEqual(stats.kept_count, 1)
        self.assertIsNone(kept[0].published_at)
        self.assertIsNone(kept[0].content_updated_at)


class ChinaMinistrySourcePolicyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.policies = {
            definition.identifier: definition.crawl_policy
            for definition in load_source_definitions()
            if definition.identifier in {"moe-list", "mee-list", "pboc-list", "ndrc-list"}
        }

    def assertScope(self, identifier, allowed, denied):
        policy = self.policies[identifier]
        for path in allowed:
            self.assertIsNone(source_scope_reason(entry(path, updated_at=None), policy), path)
        for path in denied:
            self.assertEqual(source_scope_reason(entry(path, updated_at=None), policy), "path_denied", path)

    def test_moe_policy_keeps_article_details_only(self):
        self.assertScope(
            "moe-list",
            ["/jyb_xwfb/gzdt/s5987/202609/t20260910_1408879.html"],
            [
                "/jyb_xwfb/",
                "/jyb_xwfb/moe_2082/2026/",
                "/jyb_xwfb/xw_fbh/",
                "/fbh/live/2026/78355/",
            ],
        )

    def test_mee_policy_keeps_official_details_only(self):
        self.assertScope(
            "mee-list",
            [
                "/ywdt/hjywnews/202609/t20260910_1145985.shtml",
                "/ywdt/xwfb/202609/t20260910_1145222.shtml",
                "/ywdt/zbft/202609/t20260910_1145073.shtml",
                "/ywdt/gsgg/sthjjg/202609/t20260910_1145350.shtml",
                "/ywgz/fgbz/fg/202609/t20260910_1145870.shtml",
            ],
            [
                "/ywdt/",
                "/ywdt/dfnews/202609/t20260910_1.shtml",
                "/ywdt/spxw/202609/t20260910_1.shtml",
                "/ywdt/zbft/",
                "/home/wzdt/",
            ],
        )

    def test_pboc_policy_keeps_nineteen_digit_details_only(self):
        self.assertScope(
            "pboc-list",
            ["/goutongjiaoliu/113456/113469/2026091018582672927/index.html"],
            [
                "/goutongjiaoliu/113456/113469/index.html",
                "/goutongjiaoliu/113456/113469/123/index.html",
                "/rmyh/105208/index.html",
                "/AiWza/index.html",
                "/en/3688006/index.html",
            ],
        )

    def test_ndrc_policy_remains_unscoped(self):
        policy = self.policies["ndrc-list"]
        self.assertNotIn("path_allow_prefixes", policy)
        self.assertNotIn("path_allow_patterns", policy)
        self.assertNotIn("path_deny_prefixes", policy)
