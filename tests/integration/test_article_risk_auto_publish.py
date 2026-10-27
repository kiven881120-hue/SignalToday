from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.publishing.auto_publish import evaluate_and_maybe_auto_publish
from apps.publishing.content_gate import persist_content_gate
from apps.publishing.models import Article, ArticleStatus, ArticleVersion, ArticleVersionClaim
from apps.publishing.risk import assess_article_risk
from apps.scoring.models import Score, ScoreRuleVersion, ScoreStatus
from apps.settings.models import SiteSettings
from apps.sources.models import Source


class RiskFixtureMixin:
    def make_risk_fixture(self, key, *, tier="A", verification_status=VerificationStatus.SUPPORTED,
                           content_quality="CLEAN", score_status=ScoreStatus.CALCULATED):
        now = timezone.now()
        rule = ScoreRuleVersion.objects.create(
            version=f"risk-rule-{key}", name="Risk rule",
            rule_config={
                "weights": {
                    "importance": "0.20", "credibility": "0.25", "user_relevance": "0.15",
                    "information_gain": "0.15", "freshness": "0.15", "long_term_impact": "0.10",
                },
                "thresholds": {"FOCUS": "8.0", "NORMAL": "6.5", "CANDIDATE": "5.0"},
                "penalties": {},
            },
        )
        event = Event.objects.create(title=f"Risk event {key}", first_seen_at=now, last_seen_at=now)
        Score.objects.create(
            event=event, rule_version=rule, importance=10, credibility=10,
            user_relevance=10, information_gain=10, freshness=10,
            long_term_impact=10, final_score=10, status=score_status,
        )
        statement = f"{key} 项目在 2026 年确认更新，并公布实施细节"
        claim = Claim.objects.create(
            event=event, statement=statement, verification_status=verification_status,
        )
        source = Source.objects.create(
            identifier=f"risk-source-{key}", name=f"Risk source {key}", source_type="OFFICIAL",
            url=f"https://{key}.invalid", domain=f"{key}.invalid", tier=tier,
        )
        raw = RawItem.objects.create(
            source=source, event=event, external_id=key, url=f"{source.url}/item",
            title=statement, discovered_at=now,
        )
        ClaimEvidence.objects.create(
            claim=claim, raw_item=raw, source=source, evidence_text=statement,
            stance=EvidenceStance.SUPPORTS, captured_at=now,
        )
        article = Article.objects.create(event=event, status=ArticleStatus.DRAFT)
        version = ArticleVersion.objects.create(
            article=article, version_number=1, title=f"{key} 项目确认更新",
            one_sentence_summary=statement, what_happened="并公布实施细节",
            why_it_matters="", who_is_affected="", example="",
            source_note=f"{source.name}: {raw.url}", language="zh-CN", status=ArticleStatus.DRAFT,
        )
        article.current_version = version
        article.save(update_fields=["current_version", "updated_at"])
        ArticleVersionClaim.objects.create(
            article_version=version, claim=claim, paragraph_key="article", display_order=1,
        )
        persist_content_gate(version, {"article_mode": "BRIEF", "content_quality": content_quality})
        return article, version, claim


@override_settings(SECRET_KEY="test-review-secret-value", LLM_API_KEY="test-llm-secret-value")
class AssessArticleRiskTests(RiskFixtureMixin, TestCase):
    def test_all_conditions_satisfied_returns_low(self):
        _, version, _ = self.make_risk_fixture("low")
        result = assess_article_risk(version)
        self.assertEqual(result.risk_level, "LOW")
        self.assertEqual(result.reason_code, "RISK_LOW_ELIGIBLE")

    def test_score_ineligible_defaults_to_high(self):
        _, version, _ = self.make_risk_fixture("score-bad", score_status=ScoreStatus.INVALID)
        result = assess_article_risk(version)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertEqual(result.reason_code, "RISK_SCORE_INELIGIBLE")

    def test_claim_conflict_defaults_to_high(self):
        _, version, claim = self.make_risk_fixture("conflict")
        claim.verification_status = VerificationStatus.CONTRADICTED
        claim.save(update_fields=["verification_status"])
        result = assess_article_risk(version)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertEqual(result.reason_code, "RISK_CLAIM_CONFLICT")

    def test_source_tier_below_a_defaults_to_high(self):
        _, version, _ = self.make_risk_fixture("tier-b", tier="B")
        result = assess_article_risk(version)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertEqual(result.reason_code, "RISK_SOURCE_TIER_INSUFFICIENT")

    def test_content_quality_flagged_defaults_to_high(self):
        _, version, _ = self.make_risk_fixture("flagged", content_quality="EXAGGERATED")
        result = assess_article_risk(version)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertEqual(result.reason_code, "RISK_CONTENT_QUALITY_FLAGGED")

    def test_content_quality_unknown_defaults_to_high(self):
        _, version, _ = self.make_risk_fixture("unknown", content_quality="")
        result = assess_article_risk(version)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertEqual(result.reason_code, "RISK_CONTENT_QUALITY_UNKNOWN")


@override_settings(SECRET_KEY="test-review-secret-value", LLM_API_KEY="test-llm-secret-value")
class EvaluateAndMaybeAutoPublishTests(RiskFixtureMixin, TestCase):
    def test_low_risk_with_flag_on_publishes_end_to_end(self):
        SiteSettings.load()
        SiteSettings.objects.filter(pk=1).update(automatic_publish_enabled=True)
        article, version, _ = self.make_risk_fixture("auto-low")

        evaluate_and_maybe_auto_publish(version.pk)

        article.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(article.status, ArticleStatus.PUBLISHED)
        self.assertEqual(version.status, ArticleStatus.PUBLISHED)
        self.assertEqual(article.published_version_id, version.pk)
        actor = get_user_model().objects.get(username="signaltoday-auto-publish")
        self.assertFalse(actor.has_usable_password())
        self.assertTrue(
            AuditLog.objects.filter(
                action="ARTICLE_AUTO_PUBLISH_EVALUATED", object_id=str(version.pk),
                after_data__fired=True,
            ).exists()
        )

    def test_high_risk_with_flag_on_stays_in_draft(self):
        SiteSettings.load()
        SiteSettings.objects.filter(pk=1).update(automatic_publish_enabled=True)
        article, version, _ = self.make_risk_fixture("auto-high", tier="B")

        evaluate_and_maybe_auto_publish(version.pk)

        article.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(article.status, ArticleStatus.DRAFT)
        self.assertEqual(version.status, ArticleStatus.DRAFT)
        self.assertTrue(
            AuditLog.objects.filter(
                action="ARTICLE_AUTO_PUBLISH_EVALUATED", object_id=str(version.pk),
                after_data__fired=False,
            ).exists()
        )

    def test_low_risk_with_flag_off_does_nothing(self):
        SiteSettings.load()
        SiteSettings.objects.filter(pk=1).update(automatic_publish_enabled=False)
        article, version, _ = self.make_risk_fixture("auto-flag-off")

        evaluate_and_maybe_auto_publish(version.pk)

        article.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(article.status, ArticleStatus.DRAFT)
        self.assertEqual(version.status, ArticleStatus.DRAFT)
        log = AuditLog.objects.get(
            action="ARTICLE_AUTO_PUBLISH_EVALUATED", object_id=str(version.pk),
        )
        self.assertFalse(log.after_data["fired"])
        self.assertFalse(log.after_data["automatic_publish_enabled"])

    def test_idempotent_on_already_published_version(self):
        SiteSettings.load()
        SiteSettings.objects.filter(pk=1).update(automatic_publish_enabled=True)
        article, version, _ = self.make_risk_fixture("auto-idempotent")

        evaluate_and_maybe_auto_publish(version.pk)
        evaluate_and_maybe_auto_publish(version.pk)

        article.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(article.status, ArticleStatus.PUBLISHED)
        self.assertEqual(
            AuditLog.objects.filter(
                action="ARTICLE_AUTO_PUBLISH_EVALUATED", object_id=str(version.pk),
            ).count(),
            2,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="ARTICLE_PUBLISHED", object_id=str(version.pk),
            ).count(),
            1,
        )
