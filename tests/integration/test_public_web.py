from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.publishing.models import (
    Article,
    ArticleClassification,
    ArticleStatus,
    ArticleVersion,
    ArticleVersionClaim,
    Digest,
    DigestCategory,
    DigestItem,
    DigestStatus,
)
from apps.reviews.models import Review
from apps.sources.models import Source


class PublicWebFixtureMixin:
    def setUp(self):
        self.now = timezone.now()
        self.today = timezone.localdate()

    def make_digest(self, day=None, status=DigestStatus.READY):
        return Digest.objects.create(digest_date=day or self.today, status=status)

    def make_article(self, key="public", status=ArticleStatus.PUBLISHED):
        event = Event.objects.create(
            title=f"Event {key}", first_seen_at=self.now, last_seen_at=self.now
        )
        source = Source.objects.create(
            identifier=f"web-{key}", name=f"真实来源 {key}", source_type="OFFICIAL",
            url=f"https://{key}.example.com", domain=f"{key}.example.com", tier="A",
        )
        raw = RawItem.objects.create(
            source=source, event=event, external_id=key,
            url=f"https://{key}.example.com/original", title=f"原始报道 {key}",
            discovered_at=self.now,
        )
        claim = Claim.objects.create(
            event=event, statement=f"{key} 已确认发生",
            verification_status=VerificationStatus.SUPPORTED,
        )
        ClaimEvidence.objects.create(
            claim=claim, raw_item=raw, source=source, evidence_text=f"{key} 已确认发生",
            stance=EvidenceStance.SUPPORTS, captured_at=self.now,
        )
        article = Article.objects.create(
            event=event, status=status,
            published_at=self.now if status == ArticleStatus.PUBLISHED else None,
        )
        version = ArticleVersion.objects.create(
            article=article, version_number=1, title=f"标题 {key}",
            one_sentence_summary=f"一句话看懂 {key}", what_happened=f"发生了什么 {key}",
            why_it_matters=f"为什么重要 {key}", who_is_affected=f"影响对象 {key}",
            example=f"例子 {key}", source_note=source.name, language="zh-CN", status=status,
        )
        article.current_version = version
        article.published_version = version if status == ArticleStatus.PUBLISHED else None
        article.save(update_fields=["current_version", "published_version", "updated_at"])
        ArticleVersionClaim.objects.create(
            article_version=version, claim=claim, paragraph_key="article", display_order=1
        )
        return article, version, source, raw

    def add_item(self, digest, article, rank=1, category=DigestCategory.WORLD):
        return DigestItem.objects.create(
            digest=digest, article=article, article_version=article.published_version,
            category=category, rank=rank,
            recommendation_reason=f"推荐理由 {rank}",
        )


class PublicHomeTests(PublicWebFixtureMixin, TestCase):
    def test_ready_digest_returns_200(self):
        self.make_digest()
        response = self.client.get(reverse("publishing:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.today.strftime("%Y 年 %m 月 %d 日"))

    def test_no_ready_digest_has_safe_empty_state(self):
        response = self.client.get(reverse("publishing:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "当前没有可展示的文章")

    def test_latest_digest_is_explicitly_ready(self):
        ready = self.make_digest(self.today - timedelta(days=1))
        article, *_ = self.make_article("ready-latest")
        self.add_item(ready, article)
        self.make_digest(self.today, DigestStatus.DRAFT)
        response = self.client.get(reverse("publishing:home"))
        self.assertContains(response, "标题 ready-latest")
        self.assertNotContains(response, self.today.strftime("%Y 年 %m 月 %d 日"))

    def test_home_never_uses_a_future_ready_digest(self):
        current = self.make_digest(self.today)
        current_article, *_ = self.make_article("current-day")
        self.add_item(current, current_article)
        future = self.make_digest(self.today + timedelta(days=1))
        future_article, *_ = self.make_article("future-day")
        self.add_item(future, future_article)
        response = self.client.get("/")
        self.assertContains(response, "标题 current-day")
        self.assertNotContains(response, "标题 future-day")

    def test_draft_digest_content_is_not_shown(self):
        digest = self.make_digest(status=DigestStatus.DRAFT)
        article, *_ = self.make_article("draft-digest")
        self.add_item(digest, article)
        self.assertNotContains(self.client.get("/"), "标题 draft-digest")

    def test_home_query_count_does_not_grow_with_items(self):
        digest = self.make_digest()
        for index in range(5):
            article, *_ = self.make_article(f"queries-{index}")
            self.add_item(digest, article, index + 1)
        with self.assertNumQueries(2):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)


class PublicDigestTests(PublicWebFixtureMixin, TestCase):
    def test_ready_digest_and_empty_copy(self):
        self.make_digest()
        response = self.client.get(f"/digest/{self.today.isoformat()}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.today.isoformat())
        self.assertContains(response, "当前没有可展示的文章")

    def test_draft_missing_and_invalid_digest_return_404(self):
        draft_day = self.today - timedelta(days=1)
        self.make_digest(draft_day, DigestStatus.DRAFT)
        for path in (
            f"/digest/{draft_day.isoformat()}/",
            "/digest/2001-01-01/",
            "/digest/not-a-date/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_items_display_in_rank_order_and_hide_retracted(self):
        digest = self.make_digest()
        second, *_ = self.make_article("second")
        first, *_ = self.make_article("first")
        hidden, hidden_version, *_ = self.make_article("hidden")
        self.add_item(digest, second, 2)
        self.add_item(digest, first, 1)
        self.add_item(digest, hidden, 3)
        Article.objects.filter(pk=hidden.pk).update(status=ArticleStatus.RETRACTED)
        ArticleVersion.objects.filter(pk=hidden_version.pk).update(status=ArticleStatus.RETRACTED)
        response = self.client.get(f"/digest/{self.today.isoformat()}/")
        self.assertContains(response, "标题 first")
        self.assertNotContains(response, "推荐理由 1")
        self.assertNotContains(response, "标题 hidden")
        self.assertLess(response.content.index(b"first"), response.content.index(b"second"))

    def test_digest_has_fixed_lane_order_caps_and_honest_empty_states(self):
        digest = self.make_digest()
        for index in range(6):
            article, *_ = self.make_article(f"discover-{index}")
            self.add_item(
                digest, article, index + 1, category=DigestCategory.DISCOVER
            )
        response = self.client.get(f"/digest/{self.today.isoformat()}/")
        content = response.content.decode()
        self.assertLess(content.index("今日必须知道"), content.index("你可能不知道"))
        self.assertLess(content.index("你可能不知道"), content.index("我想看的"))
        self.assertLess(content.index("我想看的"), content.index("更多主题"))
        self.assertContains(response, "当前暂无真实数据验证", count=3)
        self.assertContains(response, "标题 discover-4")
        self.assertNotContains(response, "标题 discover-5")

    def test_card_uses_evidence_source_and_hides_internal_recommendation_reason(self):
        digest = self.make_digest()
        article, _, source, _ = self.make_article("card-source")
        self.add_item(digest, article, category=DigestCategory.DISCOVER)
        response = self.client.get(f"/digest/{self.today.isoformat()}/")
        self.assertContains(response, source.name)
        self.assertNotContains(response, "推荐线索")
        self.assertNotContains(response, "推荐理由 1")

    def test_card_star_comes_from_reviewed_version_classification_not_title_or_lane(self):
        digest = self.make_digest()
        important, important_version, *_ = self.make_article("important-card")
        normal, normal_version, *_ = self.make_article("normal-card")
        ArticleVersion.objects.filter(pk=important_version.pk).update(
            classification=ArticleClassification.MUST_KNOW
        )
        self.add_item(digest, important, 1, category=DigestCategory.DISCOVER)
        self.add_item(digest, normal, 2, category=DigestCategory.MUST_KNOW)
        response = self.client.get(f"/digest/{self.today.isoformat()}/")
        self.assertContains(response, "⭐")
        self.assertContains(
            response,
            f'<h3><span aria-label="非常重要">⭐</span> {important_version.title}</h3>',
            html=True,
        )
        self.assertNotContains(
            response,
            f'<h3><span aria-label="非常重要">⭐</span> {normal_version.title}</h3>',
            html=True,
        )
        important_version.refresh_from_db()
        normal_version.refresh_from_db()
        self.assertEqual(important_version.title, "标题 important-card")
        self.assertEqual(normal_version.title, "标题 normal-card")

    def test_card_omits_summary_that_only_repeats_a_short_article_title(self):
        digest = self.make_digest()
        article, version, *_ = self.make_article("short-card")
        ArticleVersion.objects.filter(pk=version.pk).update(
            title="2026年8月居民消费价格同比上涨0.8%",
            one_sentence_summary="据统计，2026年8月居民消费价格同比上涨0.8%。",
        )
        version.refresh_from_db()
        self.add_item(digest, article, category=DigestCategory.DISCOVER)
        response = self.client.get(f"/digest/{self.today.isoformat()}/")
        self.assertContains(response, version.title)
        self.assertNotContains(response, version.one_sentence_summary)

    def test_digest_hides_item_bound_to_non_public_version(self):
        digest = self.make_digest()
        article, *_ = self.make_article("unsafe-bound-version")
        draft = ArticleVersion.objects.create(
            article=article, version_number=2, title="不应出现在日报中的草稿",
            one_sentence_summary="草稿摘要", what_happened="草稿正文", language="zh-CN",
            status=ArticleStatus.DRAFT,
        )
        item = self.add_item(digest, article)
        DigestItem.objects.filter(pk=item.pk).update(article_version=draft)
        response = self.client.get(f"/digest/{self.today.isoformat()}/")
        self.assertNotContains(response, draft.title)

    def test_digest_query_count_does_not_grow_with_items(self):
        digest = self.make_digest()
        for index in range(5):
            article, *_ = self.make_article(f"digest-query-{index}")
            self.add_item(digest, article, index + 1)
        with self.assertNumQueries(2):
            response = self.client.get(f"/digest/{self.today.isoformat()}/")
        self.assertEqual(response.status_code, 200)


class PublicArticleTests(PublicWebFixtureMixin, TestCase):
    def test_published_article_displays_formal_content(self):
        digest = self.make_digest()
        article, version, *_ = self.make_article("published")
        self.add_item(digest, article, category=DigestCategory.AI_TECH)
        response = self.client.get(f"/article/{article.pk}/")
        self.assertEqual(response.status_code, 200)
        for value in (version.title, version.one_sentence_summary, version.what_happened):
            self.assertContains(response, value)
        self.assertContains(response, "AI / 科技")

    def test_article_title_star_is_present_only_for_must_know(self):
        important, important_version, *_ = self.make_article("important-detail")
        normal, normal_version, *_ = self.make_article("normal-detail")
        ArticleVersion.objects.filter(pk=important_version.pk).update(
            classification=ArticleClassification.MUST_KNOW
        )
        important_response = self.client.get(f"/article/{important.pk}/")
        normal_response = self.client.get(f"/article/{normal.pk}/")
        self.assertContains(
            important_response,
            f'<span aria-label="非常重要">⭐</span> {important_version.title}',
            html=True,
        )
        self.assertNotContains(normal_response, "⭐")
        important_version.refresh_from_db()
        normal_version.refresh_from_db()
        self.assertEqual(important_version.title, "标题 important-detail")
        self.assertEqual(normal_version.title, "标题 normal-detail")

    def test_article_renders_natural_paragraphs_without_internal_field_headings(self):
        article, version, *_ = self.make_article("natural")
        response = self.client.get(f"/article/{article.pk}/")
        for value in (
            version.what_happened,
            version.why_it_matters,
            version.who_is_affected,
            version.example,
        ):
            self.assertContains(response, f"<p>{value}</p>", html=True)
        for heading in ("发生了什么", "为什么重要", "对谁有影响", "举个例子"):
            self.assertNotContains(response, f"<h2>{heading}</h2>", html=True)

    def test_home_copy_does_not_claim_to_decide_what_is_worth_knowing(self):
        self.make_digest()
        response = self.client.get("/")
        self.assertContains(response, "今天出现了")
        self.assertNotContains(response, "今天有什么<br>值得知道的？", html=True)

    def test_non_published_article_states_return_404(self):
        for status in (
            ArticleStatus.DRAFT, ArticleStatus.IN_REVIEW, ArticleStatus.APPROVED,
            ArticleStatus.RETRACTED,
        ):
            with self.subTest(status=status):
                article, *_ = self.make_article(f"state-{status.lower()}", status=status)
                self.assertEqual(self.client.get(f"/article/{article.pk}/").status_code, 404)

    def test_old_unpublished_version_is_never_exposed(self):
        article, *_ = self.make_article("versions")
        old_draft = ArticleVersion.objects.create(
            article=article, version_number=2, title="绝不能公开的旧草稿",
            one_sentence_summary="草稿摘要", what_happened="草稿正文", language="zh-CN",
            status=ArticleStatus.DRAFT,
        )
        response = self.client.get(f"/article/{article.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, old_draft.title)

    def test_current_version_must_be_the_formal_published_version(self):
        article, *_ = self.make_article("not-current")
        draft = ArticleVersion.objects.create(
            article=article, version_number=2, title="新草稿", one_sentence_summary="摘要",
            what_happened="正文", language="zh-CN", status=ArticleStatus.DRAFT,
        )
        Article.objects.filter(pk=article.pk).update(current_version=draft)
        self.assertEqual(self.client.get(f"/article/{article.pk}/").status_code, 404)

    def test_sources_come_from_real_database_relations(self):
        article, _, source, raw = self.make_article("source")
        response = self.client.get(f"/article/{article.pk}/")
        self.assertContains(response, source.name)
        self.assertContains(response, raw.url)
        self.assertContains(response, 'target="_blank" rel="noopener noreferrer"')
        for internal in ("Claim", "Evidence", "Score"):
            self.assertNotContains(response, internal)


class PublicHistoryAndSafetyTests(PublicWebFixtureMixin, TestCase):
    def test_history_lists_past_digest(self):
        past_day = self.today - timedelta(days=1)
        self.make_digest(past_day)

        response = self.client.get("/history/")

        self.assertContains(response, past_day.isoformat())

    def test_history_lists_current_digest(self):
        self.make_digest(self.today)

        response = self.client.get("/history/")

        self.assertContains(response, self.today.isoformat())

    def test_history_hides_future_ready_digest_with_published_article(self):
        future_day = self.today + timedelta(days=1)
        future = self.make_digest(future_day, DigestStatus.READY)
        article, *_ = self.make_article("future-history", ArticleStatus.PUBLISHED)
        self.add_item(future, article)

        response = self.client.get("/history/")

        self.assertNotContains(response, future_day.isoformat())
        self.assertTrue(Digest.objects.filter(pk=future.pk).exists())

    def test_history_only_lists_ready_digests_from_last_30_days(self):
        ready = self.make_digest()
        article, *_ = self.make_article("history")
        self.add_item(ready, article)
        draft_day = self.today - timedelta(days=1)
        old_day = self.today - timedelta(days=31)
        self.make_digest(draft_day, DigestStatus.DRAFT)
        self.make_digest(old_day, DigestStatus.READY)
        response = self.client.get("/history/")
        self.assertContains(response, self.today.isoformat())
        self.assertContains(response, "精选 1 条")
        self.assertNotContains(response, draft_day.isoformat())
        self.assertNotContains(response, old_day.isoformat())

    def test_public_get_does_not_write_business_data(self):
        digest = self.make_digest()
        article, *_ = self.make_article("readonly")
        self.add_item(digest, article)
        before = (
            Digest.objects.count(), DigestItem.objects.count(), Article.objects.count(),
            Review.objects.count(), AuditLog.objects.count(),
        )
        for path in ("/", f"/digest/{self.today.isoformat()}/", f"/article/{article.pk}/", "/history/"):
            self.assertEqual(self.client.get(path).status_code, 200)
        after = (
            Digest.objects.count(), DigestItem.objects.count(), Article.objects.count(),
            Review.objects.count(), AuditLog.objects.count(),
        )
        self.assertEqual(after, before)

    def test_public_pages_only_allow_safe_methods(self):
        self.assertEqual(self.client.head("/").status_code, 200)
        self.assertEqual(self.client.post("/").status_code, 405)
        self.assertEqual(self.client.post("/history/").status_code, 405)

    def test_seo_metadata_and_canonical(self):
        digest = self.make_digest()
        article, version, *_ = self.make_article("seo")
        self.add_item(digest, article)
        home = self.client.get("/")
        detail = self.client.get(f"/article/{article.pk}/")
        self.assertContains(home, "<title>SignalToday｜少刷一点，多知道一点</title>", html=True)
        self.assertContains(home, '<link rel="canonical" href="http://testserver/">', html=True)
        self.assertContains(detail, f"<title>{version.title}｜SignalToday</title>", html=True)
        self.assertContains(detail, version.one_sentence_summary)

    def test_robots_and_custom_404(self):
        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertContains(robots, "Allow: /")
        self.assertContains(robots, "Disallow: /admin/")
        missing = self.client.get("/article/999999/")
        self.assertEqual(missing.status_code, 404)
        self.assertContains(missing, "这条信息不存在或已经下线。", status_code=404)

    def test_production_static_css_is_available(self):
        response = self.client.get("/static/css/site.css")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/css"))
