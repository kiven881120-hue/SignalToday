from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory
from unittest.mock import patch
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun
from apps.publishing.digest import (
    DailyDigestError,
    RECOMMENDATION_DISCOVER,
    RECOMMENDATION_PERSONALIZED,
    classify_recommendation_lane,
    generate_daily_digest,
)
from apps.publishing.content_gate import persist_content_gate
from apps.publishing.admin import DigestAdmin, DigestItemAdmin
from apps.publishing.models import Article, ArticleStatus, ArticleVersion, ArticleVersionClaim, Digest, DigestCategory, DigestItem, DigestStatus
from apps.publishing.review import approve_article, correct_article, publish_article, submit_article_for_review
from apps.reviews.models import Review, ReviewDecision
from apps.scoring.models import Score, ScoreRuleVersion
from apps.sources.models import Source


class DigestFixtureMixin:
    def setUp(self):
        self.now = timezone.now()
        self.digest_date = self.now.date()
        self.rule = ScoreRuleVersion.objects.create(
            version=f"digest-{self.now.timestamp()}", name="Digest",
            rule_config={"weights": {"importance": "0.20", "credibility": "0.25", "user_relevance": "0.15", "information_gain": "0.15", "freshness": "0.15", "long_term_impact": "0.10"}, "thresholds": {"FOCUS": "8.0", "NORMAL": "6.5", "CANDIDATE": "5.0"}, "penalties": {}},
        )

    def make_article(self, key, score=10, status=ArticleStatus.PUBLISHED, title=None, published_at=None, version_number=1):
        now = self.now
        event = Event.objects.create(title=title or key, first_seen_at=now, last_seen_at=now)
        score_obj = Score.objects.create(event=event, rule_version=self.rule, importance=score, credibility=score, user_relevance=score, information_gain=score, freshness=score, long_term_impact=score, final_score=score)
        statement = f"{title or key} confirmed fact 2026. Verified details were published."
        claim = Claim.objects.create(
            event=event, statement=statement,
            verification_status=VerificationStatus.SUPPORTED,
        )
        source = Source.objects.create(identifier=f"digest-{key}", name=f"Source {key}", source_type="OFFICIAL", url=f"https://{key}.invalid", domain=f"{key}.invalid", tier="A")
        raw = RawItem.objects.create(source=source, event=event, external_id=key, url=f"{source.url}/item", title=claim.statement, discovered_at=now)
        evidence = ClaimEvidence.objects.create(claim=claim, raw_item=raw, source=source, evidence_text=claim.statement, stance=EvidenceStance.SUPPORTS, captured_at=now)
        article = Article.objects.create(event=event, status=status, published_at=published_at or now)
        version_status = status
        version = ArticleVersion.objects.create(
            article=article, version_number=version_number, title=title or key,
            one_sentence_summary=statement,
            what_happened="Verified details were published.",
            why_it_matters="", who_is_affected="", example="",
            source_note=source.name, language="zh-CN", status=version_status,
        )
        article.current_version = version
        article.published_version = version if version.status == ArticleStatus.PUBLISHED else None
        article.save(update_fields=["current_version", "published_version", "updated_at"])
        link = ArticleVersionClaim.objects.create(article_version=version, claim=claim, paragraph_key="article", display_order=1)
        persist_content_gate(version, {"article_mode": "BRIEF"})
        if status == ArticleStatus.PUBLISHED:
            Review.objects.create(
                article_version=version,
                decision=ReviewDecision.APPROVED,
                comment="Fixture publication review",
                reviewed_at=now,
            )
        return article, event, score_obj, claim, evidence, link


class DailyDigestTests(DigestFixtureMixin, TestCase):
    def test_empty_digest_ready_and_no_llm(self):
        result = generate_daily_digest(self.digest_date)
        self.assertEqual((result.digest.status, result.digest.items.count(), result.job_run.status), (DigestStatus.READY, 0, JobStatus.SUCCESS))
        self.assertEqual(set(AuditLog.objects.filter(object_type="Digest").values_list("action", flat=True)), {"DIGEST_CREATED", "DIGEST_READY"})

    def test_filters_status_but_score_only_controls_priority(self):
        self.make_article("draft", status=ArticleStatus.DRAFT)
        self.make_article("review", status=ArticleStatus.IN_REVIEW)
        self.make_article("approved", status=ArticleStatus.APPROVED)
        reject, *_ = self.make_article("reject", score=4)
        candidate, *_ = self.make_article("candidate", score=6)
        normal, *_ = self.make_article("normal", score=7)
        focus, *_ = self.make_article("focus", score=10)
        result = generate_daily_digest(self.digest_date, dry_run=True)
        self.assertEqual(
            [item.article.pk for item in result.selected],
            [focus.pk, normal.pk, candidate.pk, reject.pk],
        )

    def test_fewer_than_twelve_is_not_padded(self):
        for index in range(8):
            self.make_article(f"few-{index}", score=7)
        self.assertEqual(len(generate_daily_digest(self.digest_date, dry_run=True).selected), 8)

    def test_limit_sort_determinism_and_must_know(self):
        for index in range(30):
            self.make_article(f"many-{index}", score=10 - index % 3)
        result = generate_daily_digest(self.digest_date, dry_run=True)
        again = generate_daily_digest(self.digest_date, dry_run=True)
        self.assertEqual(len(result.selected), 12)
        self.assertLessEqual(sum(item.must_know for item in result.selected), 3)
        self.assertEqual([(x.article.pk, x.category, x.recommendation_reason) for x in result.selected], [(x.article.pk, x.category, x.recommendation_reason) for x in again.selected])

    def test_categories_and_reason(self):
        ai, *_ = self.make_article("ai", title="AI software technology", score=10)
        world, *_ = self.make_article("world", title="International global event", score=7)
        result = generate_daily_digest(self.digest_date, dry_run=True)
        values = {item.article.pk: item for item in result.selected}
        self.assertEqual(values[ai.pk].category, DigestCategory.MUST_KNOW)
        self.assertEqual(values[world.pk].category, DigestCategory.WORLD)
        self.assertIn("重要性高", values[ai.pk].recommendation_reason)

    def test_all_fixed_category_rules(self):
        cases = {
            "International geopolitics": DigestCategory.WORLD,
            "AI robotics": DigestCategory.AI_TECH,
            "Finance banking": DigestCategory.FINANCE,
            "Automotive vehicle": DigestCategory.AUTO,
            "Space science": DigestCategory.SCIENCE,
            "University education": DigestCategory.EDUCATION,
            "Health medicine": DigestCategory.HEALTH,
            "A curious local fact": DigestCategory.GENERAL,
        }
        for index, (title, category) in enumerate(cases.items()):
            article, *_ = self.make_article(f"category-{index}", score=7, title=title)
            candidate = next(item for item in generate_daily_digest(self.digest_date, dry_run=True).selected if item.article.pk == article.pk)
            self.assertEqual(candidate.category, category)

    def test_discover_requires_information_gain_not_unknown_topic(self):
        article, _, score, *_ = self.make_article(
            "high-gain-niche", score=7, title="A curious local fact"
        )
        Score.objects.filter(pk=score.pk).update(
            information_gain=Decimal("9"), user_relevance=Decimal("2")
        )
        candidate = next(
            item for item in generate_daily_digest(self.digest_date, dry_run=True).selected
            if item.article.pk == article.pk
        )
        self.assertEqual(candidate.category, DigestCategory.DISCOVER)
        self.assertEqual(candidate.recommendation_lane, RECOMMENDATION_DISCOVER)

    def test_personalized_lane_is_an_explicit_future_interface(self):
        _, _, score, *_ = self.make_article("explicit-interest", score=6)
        self.assertEqual(
            classify_recommendation_lane(score, has_explicit_interest=True),
            RECOMMENDATION_PERSONALIZED,
        )

    def test_dry_run_has_no_side_effects_and_command_json(self):
        self.make_article("dry")
        before = (Digest.objects.count(), DigestItem.objects.count(), AuditLog.objects.count(), JobRun.objects.count())
        result = generate_daily_digest(self.digest_date, dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(before, (Digest.objects.count(), DigestItem.objects.count(), AuditLog.objects.count(), JobRun.objects.count()))
        output = StringIO()
        call_command("generate_daily_digest", date=self.digest_date.isoformat(), dry_run=True, stdout=output)
        self.assertIn('"candidate_count": 1', output.getvalue())

    def test_idempotent_rebuild_and_constraints(self):
        article, event, *_ = self.make_article("idem")
        first = generate_daily_digest(self.digest_date)
        before = (Digest.objects.count(), DigestItem.objects.count(), AuditLog.objects.count(), JobRun.objects.count())
        second = generate_daily_digest(self.digest_date)
        self.assertFalse(second.changed)
        self.assertEqual(before, (Digest.objects.count(), DigestItem.objects.count(), AuditLog.objects.count(), JobRun.objects.count()))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Digest.objects.create(digest_date=self.digest_date)
        with self.assertRaises(DailyDigestError):
            generate_daily_digest(self.digest_date, rebuild=True)
        self.assertFalse(AuditLog.objects.filter(action="DIGEST_REBUILT").exists())
        with self.assertRaises((ValidationError, IntegrityError)):
            with transaction.atomic():
                first.digest.items.create(article=article, article_version=article.published_version, category=DigestCategory.DISCOVER, rank=99, recommendation_reason="duplicate")

    def test_digest_item_requires_matching_current_formal_published_version(self):
        digest = Digest.objects.create(digest_date=self.digest_date)
        article, _, _, _, _, _ = self.make_article("valid-binding")
        other, _, _, _, _, _ = self.make_article("other-binding")
        draft = ArticleVersion.objects.create(
            article=article, version_number=2, title="draft", one_sentence_summary="summary",
            what_happened="facts", status=ArticleStatus.DRAFT,
        )
        values = {
            "digest": digest, "article": article, "category": DigestCategory.DISCOVER,
            "rank": 1, "recommendation_reason": "reason",
        }
        with self.assertRaises(ValidationError):
            DigestItem.objects.create(article_version=None, **values)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DigestItem.objects.bulk_create([DigestItem(**values)])
        with self.assertRaises(ValidationError):
            DigestItem.objects.create(article_version=other.published_version, **values)
        with self.assertRaises(ValidationError):
            DigestItem.objects.create(article_version=draft, **values)
        item = DigestItem.objects.create(
            article_version=article.published_version, **values
        )
        self.assertEqual(item.article_version_id, article.published_version_id)
        replacement = ArticleVersion.objects.create(
            article=article, version_number=3, title="replacement",
            one_sentence_summary="summary", what_happened="facts",
            status=ArticleStatus.PUBLISHED,
        )
        item.article_version = replacement
        with self.assertRaises(ValidationError):
            item.save()
        item.refresh_from_db()
        self.assertEqual(item.article_version_id, article.published_version_id)
        with self.assertRaises(ProtectedError):
            article.published_version.delete()

    def test_historical_digest_keeps_version_four_after_version_five_is_published(self):
        actor = get_user_model().objects.create_user(username="snapshot-reviewer")
        article, _, _, _, _, _ = self.make_article(
            "snapshot", title="Version4 fixed snapshot", version_number=4
        )
        version4 = article.published_version
        digest_result = generate_daily_digest(self.digest_date)
        item = digest_result.digest.items.get(article=article)
        self.assertEqual(item.article_version_id, version4.pk)

        corrected = correct_article(version4.pk, "Create Version5", actor)
        version5 = corrected.article_version
        version5.title = "Version5 current article"
        version5.save(update_fields=["title"])
        persist_content_gate(version5, {"article_mode": "BRIEF"})
        submit_article_for_review(version5.pk, "Submit Version5", actor)
        approve_article(version5.pk, "Approve Version5", actor)
        publish_article(version5.pk, "Publish Version5", actor)

        item.refresh_from_db()
        article.refresh_from_db()
        self.assertEqual(article.published_version_id, version5.pk)
        self.assertEqual(item.article_version_id, version4.pk)
        digest_url = f"/digest/{self.digest_date.isoformat()}/"
        digest_page = self.client.get(digest_url)
        self.assertEqual(digest_page.status_code, 200)
        self.assertContains(digest_page, version4.title)
        self.assertNotContains(digest_page, version5.title)
        snapshot_url = f"{digest_url}article/{article.pk}/"
        snapshot_page = self.client.get(snapshot_url)
        self.assertEqual(snapshot_page.status_code, 200)
        self.assertEqual(snapshot_page.context["version"].pk, version4.pk)
        self.assertContains(snapshot_page, version4.title)
        self.assertNotContains(snapshot_page, version5.title)
        current_page = self.client.get(f"/article/{article.pk}/")
        self.assertEqual(current_page.context["version"].pk, version5.pk)
        self.assertContains(current_page, version5.title)

    def test_invalid_published_chain_excluded(self):
        article, _, _, claim, evidence, _ = self.make_article("invalid")
        Claim.objects.filter(pk=claim.pk).update(verification_status=VerificationStatus.CONTRADICTED)
        self.assertEqual(generate_daily_digest(self.digest_date, dry_run=True).selected, ())
        Claim.objects.filter(pk=claim.pk).update(verification_status=VerificationStatus.SUPPORTED)
        evidence.delete()
        self.assertEqual(generate_daily_digest(self.digest_date, dry_run=True).selected, ())

    def test_job_steps_are_observable(self):
        self.make_article("observable")
        result = generate_daily_digest(self.digest_date)
        self.assertEqual(result.job_run.steps.count(), 6)
        self.assertTrue(all(step.status == JobStatus.SUCCESS for step in result.job_run.steps.all()))
        item = result.digest.items.get()
        audit = AuditLog.objects.get(action="DIGEST_ITEM_CREATED")
        self.assertEqual(audit.after_data["article_version_id"], item.article_version_id)

    def test_build_failure_rolls_back_items_and_preserves_failed_job(self):
        self.make_article("failure")
        with patch("apps.publishing.digest.DigestItem.objects.create", side_effect=IntegrityError("db")):
            with self.assertRaises(DailyDigestError):
                generate_daily_digest(self.digest_date)
        self.assertEqual(DigestItem.objects.count(), 0)
        self.assertEqual(Digest.objects.get().status, DigestStatus.DRAFT)
        self.assertEqual(JobRun.objects.get(job_type="DAILY_DIGEST").status, JobStatus.FAILED)

    def test_admin_digest_fields_are_read_only(self):
        request = RequestFactory().get("/admin/")
        digest_admin = DigestAdmin(Digest, admin.site)
        item_admin = DigestItemAdmin(DigestItem, admin.site)
        self.assertIn("status", digest_admin.get_readonly_fields(request))
        self.assertIn("rank", item_admin.get_readonly_fields(request))
        self.assertFalse(digest_admin.has_add_permission(request))
        self.assertFalse(item_admin.has_add_permission(request))


class DailyDigestHistoricalDedupTests(DigestFixtureMixin, TestCase):
    def _historical_item(self, article, digest_date, rank=1):
        digest = Digest.objects.create(
            digest_date=digest_date, status=DigestStatus.READY
        )
        return DigestItem.objects.create(
            digest=digest,
            article=article,
            article_version=article.published_version,
            category=DigestCategory.DISCOVER,
            rank=rank,
            recommendation_reason="Historical selection",
        )

    def _publish_next_version(self, article, key):
        actor = get_user_model().objects.create_user(username=f"dedup-{key}")
        corrected = correct_article(
            article.published_version_id, "Create next version", actor
        )
        version = corrected.article_version
        persist_content_gate(version, {"article_mode": "BRIEF"})
        submit_article_for_review(version.pk, "Submit next version", actor)
        approve_article(version.pk, "Approve next version", actor)
        publish_article(version.pk, "Publish next version", actor)
        article.refresh_from_db()
        return version

    def test_same_version_is_excluded_from_future_digest(self):
        article, *_ = self.make_article("same-version")
        version = article.published_version
        self._historical_item(article, self.digest_date)

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertEqual((len(result.candidates), len(result.selected)), (0, 0))
        self.assertEqual(
            [(item.article_version_id, item.reason_code) for item in result.exclusions],
            [(version.pk, "EXCLUDED_ALREADY_IN_HISTORICAL_DIGEST")],
        )

    def test_new_published_version_can_enter_future_digest(self):
        article, *_ = self.make_article("new-version")
        old_version = article.published_version
        self._historical_item(article, self.digest_date)
        new_version = self._publish_next_version(article, "new-version")

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertNotEqual(old_version.pk, new_version.pk)
        self.assertEqual([item.article_version.pk for item in result.selected], [new_version.pk])

    def test_draft_current_version_does_not_fall_back_to_historical_version(self):
        article, *_ = self.make_article("draft-current")
        old_version = article.published_version
        self._historical_item(article, self.digest_date)
        actor = get_user_model().objects.create_user(username="dedup-draft")
        draft = correct_article(old_version.pk, "Create draft", actor).article_version

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        article.refresh_from_db()
        self.assertEqual(article.current_version_id, draft.pk)
        self.assertEqual(article.published_version_id, old_version.pk)
        self.assertEqual(result.selected, ())

    def test_published_current_version_without_approved_review_is_excluded(self):
        article, *_ = self.make_article("missing-review")
        self._historical_item(article, self.digest_date)
        actor = get_user_model().objects.create_user(username="dedup-no-review")
        version = correct_article(
            article.published_version_id, "Create unreviewed version", actor
        ).article_version
        ArticleVersion.objects.filter(pk=version.pk).update(status=ArticleStatus.PUBLISHED)
        Article.objects.filter(pk=article.pk).update(
            status=ArticleStatus.PUBLISHED,
            current_version=version,
            published_version=version,
            published_at=self.now,
        )

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertFalse(Review.objects.filter(
            article_version=version, decision=ReviewDecision.APPROVED
        ).exists())
        self.assertEqual(result.selected, ())

    def test_published_reviewed_version_with_failed_publish_gate_is_excluded(self):
        article, _, _, claim, *_ = self.make_article("failed-gate")
        self._historical_item(article, self.digest_date)
        version = self._publish_next_version(article, "failed-gate")
        Claim.objects.filter(pk=claim.pk).update(
            verification_status=VerificationStatus.CONTRADICTED
        )

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertTrue(Review.objects.filter(
            article_version=version, decision=ReviewDecision.APPROVED
        ).exists())
        self.assertEqual(result.selected, ())

    def test_new_version_with_all_existing_publish_gates_passes(self):
        article, *_ = self.make_article("all-gates")
        self._historical_item(article, self.digest_date)
        version = self._publish_next_version(article, "all-gates")

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertEqual([item.article_version.pk for item in result.selected], [version.pk])

    def test_multiple_historical_items_are_all_excluded(self):
        articles = [self.make_article(f"many-history-{index}")[0] for index in range(3)]
        digest = Digest.objects.create(
            digest_date=self.digest_date, status=DigestStatus.READY
        )
        for rank, article in enumerate(articles, 1):
            DigestItem.objects.create(
                digest=digest,
                article=article,
                article_version=article.published_version,
                category=DigestCategory.DISCOVER,
                rank=rank,
                recommendation_reason="Historical selection",
            )

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertEqual(result.selected, ())
        self.assertEqual(
            {item.article_version_id for item in result.exclusions},
            {article.published_version_id for article in articles},
        )

    def test_versions_across_multiple_historical_digests_remain_excluded(self):
        article, *_ = self.make_article("multiple-digests")
        version1 = article.published_version
        self._historical_item(article, self.digest_date - timedelta(days=1))
        version2 = self._publish_next_version(article, "multiple-digests")
        self._historical_item(article, self.digest_date)

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertEqual(
            set(DigestItem.objects.filter(article=article).values_list(
                "article_version_id", flat=True
            )),
            {version1.pk, version2.pk},
        )
        self.assertEqual(result.selected, ())
        self.assertEqual(result.exclusions[0].article_version_id, version2.pk)

    def test_dedup_uses_version_id_not_article_id(self):
        article, *_ = self.make_article("version-key")
        version1 = article.published_version
        self._historical_item(article, self.digest_date)
        version2 = self._publish_next_version(article, "version-key")

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertNotEqual(version1.pk, version2.pk)
        self.assertEqual(result.selected[0].article.pk, article.pk)
        self.assertEqual(result.selected[0].article_version.pk, version2.pk)

    def test_ready_digest_cannot_be_rebuilt(self):
        article, *_ = self.make_article("ready-no-rebuild")
        result = generate_daily_digest(self.digest_date)
        before = (Digest.objects.count(), DigestItem.objects.count(), AuditLog.objects.count())

        with self.assertRaises(DailyDigestError):
            generate_daily_digest(self.digest_date, rebuild=True)

        self.assertEqual(result.digest.status, DigestStatus.READY)
        self.assertEqual(
            before,
            (Digest.objects.count(), DigestItem.objects.count(), AuditLog.objects.count()),
        )
        self.assertFalse(AuditLog.objects.filter(action="DIGEST_REBUILT").exists())

    def test_production_shape_dry_run_excludes_article1_version4(self):
        article, *_ = self.make_article(
            "production-shape", version_number=4
        )
        version4 = article.published_version
        self._historical_item(article, self.digest_date)

        result = generate_daily_digest(
            self.digest_date + timedelta(days=1), dry_run=True
        )

        self.assertEqual((len(result.candidates), len(result.selected)), (0, 0))
        self.assertEqual(
            result.exclusions[0].reason_code,
            "EXCLUDED_ALREADY_IN_HISTORICAL_DIGEST",
        )
        self.assertEqual(result.exclusions[0].article_version_id, version4.pk)
        self.assertEqual(Digest.objects.count(), 1)


class DailyDigestConcurrencyTests(DigestFixtureMixin, TransactionTestCase):
    def test_concurrent_generation_has_one_digest(self):
        self.make_article("concurrent")
        def invoke():
            close_old_connections()
            try:
                return generate_daily_digest(self.digest_date)
            except Exception as exc:
                return exc
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: invoke(), range(2)))
        self.assertEqual(Digest.objects.filter(digest_date=self.digest_date).count(), 1)
        self.assertEqual(DigestItem.objects.count(), 1)
        item = DigestItem.objects.get()
        self.assertEqual(item.article_version_id, item.article.published_version_id)
        successful = [result for result in results if not isinstance(result, Exception)]
        self.assertEqual(sum(result.changed for result in successful), 1)
        self.assertLessEqual(sum(isinstance(result, DailyDigestError) for result in results), 1)

    def test_concurrent_different_dates_do_not_duplicate_article_version(self):
        self.make_article("concurrent-cross-date")

        def invoke(offset):
            close_old_connections()
            try:
                return generate_daily_digest(
                    self.digest_date + timedelta(days=offset)
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, (0, 1)))

        self.assertEqual(Digest.objects.count(), 2)
        self.assertEqual(DigestItem.objects.count(), 1)
        self.assertEqual(sum(len(result.selected) for result in results), 1)
