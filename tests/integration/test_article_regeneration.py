import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.ai_content.models import LLMPurpose, ModelCall, ModelPricing, PromptVersion
from apps.ai_content.providers.base import LLMProvider, ProviderResponse
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.models import JobRun
from apps.publishing.models import (
    Article, ArticleContentValidation, ArticleStatus, ArticleVersion,
    ArticleVersionClaim,
)
from apps.publishing.regeneration import ArticleRegenerationError, regenerate_article
from apps.publishing.review import ArticleReviewError, publish_article
from apps.reviews.models import Review, ReviewDecision
from apps.scoring.models import Score, ScoreRuleVersion
from apps.sources.models import Source


def output_json(claim_id, **overrides):
    values = {
        "article_mode": "BRIEF",
        "title": "机构发布新规",
        "one_sentence": "机构发布新规，并确认实施日期。",
        "what_happened": "该规则将于2026年10月1日生效。",
        "why_it_matters": None,
        "why_status": "WHY_NONE_ESTABLISHED",
        "who_affected": None,
        "who_status": "WHO_NONE_ESTABLISHED",
        "example": None,
        "example_type": "NONE",
        "claim_ids": [claim_id],
    }
    values.update(overrides)
    return json.dumps(values, ensure_ascii=False)


class FakeProvider(LLMProvider):
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def analyze_structured(self, **kwargs):
        self.calls.append(kwargs)
        return ProviderResponse(self.responses.pop(0), 120, 60)


@override_settings(
    LLM_PROVIDER="mock", LLM_MODEL="regeneration-mock", LLM_API_KEY="not-logged",
    LLM_MAX_ATTEMPTS=1, LLM_MAX_OUTPUT_TOKENS=300, LLM_DAILY_BUDGET_CNY=20,
)
class ArticleRegenerationTests(TestCase):
    def setUp(self):
        now = timezone.now()
        PromptVersion.objects.filter(purpose=LLMPurpose.ARTICLE_GENERATION).update(active=False)
        self.prompt, _ = PromptVersion.objects.update_or_create(
            purpose=LLMPurpose.ARTICLE_GENERATION,
            version="article-generation-v1.3",
            defaults={
                "system_prompt": "Use evidence only.",
                "output_schema_version": "article-generation-v1.3", "active": True,
            },
        )
        ModelPricing.objects.create(
            provider="mock", model="regeneration-mock",
            input_price=Decimal("1"), output_price=Decimal("6"), currency="CNY",
            effective_from=now - timedelta(days=1), version="regeneration-test",
        )
        rule = ScoreRuleVersion.objects.create(
            version="regeneration-rule", name="Regeneration rule",
            rule_config={
                "weights": {
                    "importance": "0.20", "credibility": "0.25",
                    "user_relevance": "0.15", "information_gain": "0.15",
                    "freshness": "0.15", "long_term_impact": "0.10",
                },
                "thresholds": {"FOCUS": "8.0", "NORMAL": "6.5", "CANDIDATE": "5.0"},
                "penalties": {},
            },
        )
        self.actor = get_user_model().objects.create_user(username="regenerator")
        self.event = Event.objects.create(
            title="Existing event", first_seen_at=now, last_seen_at=now,
        )
        Score.objects.create(
            event=self.event, rule_version=rule, importance=10, credibility=10,
            user_relevance=10, information_gain=10, freshness=10,
            long_term_impact=10, final_score=10,
        )
        statement = "机构发布新规，并确认该规则将于2026年10月1日生效。"
        self.claim = Claim.objects.create(
            event=self.event, statement=statement,
            verification_status=VerificationStatus.SUPPORTED,
        )
        source = Source.objects.create(
            identifier="regeneration-source", name="Official source",
            source_type="OFFICIAL", url="https://regeneration.invalid",
            domain="regeneration.invalid", tier="A",
        )
        raw = RawItem.objects.create(
            source=source, event=self.event, external_id="regeneration",
            url="https://regeneration.invalid/item", title=statement,
            raw_metadata={"summary": statement}, discovered_at=now,
        )
        ClaimEvidence.objects.create(
            claim=self.claim, raw_item=raw, source=source, evidence_text=statement,
            stance=EvidenceStance.SUPPORTS, captured_at=now,
        )
        self.article = Article.objects.create(event=self.event, status=ArticleStatus.DRAFT)
        self.source_version = ArticleVersion.objects.create(
            article=self.article, version_number=1, title="旧标题",
            one_sentence_summary="旧导语", what_happened="旧正文",
            source_note=f"Official source: {raw.url}", language="zh-CN",
            status=ArticleStatus.DRAFT, created_by=self.actor,
        )
        self.article.current_version = self.source_version
        self.article.save(update_fields=["current_version", "updated_at"])
        ArticleVersionClaim.objects.create(
            article_version=self.source_version, claim=self.claim,
            paragraph_key="article", display_order=1,
        )

    def regenerate(self, request_id="request-one", raw=None, provider=None):
        provider = provider or FakeProvider(raw or output_json(self.claim.pk))
        result = regenerate_article(
            self.event.pk, actor=self.actor, reason="人工请求重新生成",
            request_id=request_id, provider=provider,
        )
        return result, provider

    def test_existing_version_creates_linked_new_version_without_overwrite(self):
        before = {field: getattr(self.source_version, field) for field in ArticleVersion.CONTENT_FIELDS}

        result, provider = self.regenerate()

        self.source_version.refresh_from_db()
        self.article.refresh_from_db()
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.target_version.version_number, 2)
        self.assertEqual(self.article.current_version_id, result.target_version.pk)
        self.assertEqual(before, {
            field: getattr(self.source_version, field) for field in ArticleVersion.CONTENT_FIELDS
        })
        audit = AuditLog.objects.get(action="ARTICLE_VERSION_REGENERATED")
        self.assertEqual(audit.before_data["source_version_id"], self.source_version.pk)
        self.assertEqual(audit.after_data["target_version_id"], result.target_version.pk)

    def test_active_v13_creates_independent_generation_model_call(self):
        result, _ = self.regenerate()

        self.assertEqual(result.model_call.purpose, LLMPurpose.ARTICLE_GENERATION)
        self.assertEqual(result.model_call.prompt_id, self.prompt.pk)
        self.assertEqual(result.model_call.prompt_version, "article-generation-v1.3")
        self.assertEqual(result.model_call.schema_version, "article-generation-v1.3")
        self.assertEqual(result.model_call.job_run_id, result.job_run.pk)
        self.assertEqual(ModelCall.objects.count(), 1)

    def test_pass_runs_both_gates_and_creates_pending_review(self):
        result, _ = self.regenerate()

        self.assertEqual(result.content_validation.grounding_status, "PASS")
        self.assertEqual(result.content_validation.quality_status, "PASS")
        self.assertEqual(result.review.decision, ReviewDecision.PENDING)
        self.assertEqual(result.target_version.status, ArticleStatus.IN_REVIEW)
        self.assertEqual(result.job_run.steps.count(), 9)
        self.assertTrue(all(step.status == "SUCCESS" for step in result.job_run.steps.all()))

    def test_published_source_and_history_remain_unchanged(self):
        approved = Review.objects.create(
            article_version=self.source_version, reviewer=self.actor,
            decision=ReviewDecision.APPROVED, reviewed_at=timezone.now(),
        )
        self.source_version.status = ArticleStatus.PUBLISHED
        self.source_version.save(update_fields=["status"])
        self.article.status = ArticleStatus.PUBLISHED
        self.article.published_version = self.source_version
        self.article.published_at = timezone.now()
        self.article.save(update_fields=[
            "status", "published_version", "published_at", "updated_at",
        ])

        result, _ = self.regenerate()

        self.article.refresh_from_db()
        self.source_version.refresh_from_db()
        approved.refresh_from_db()
        self.assertEqual(self.article.status, ArticleStatus.PUBLISHED)
        self.assertEqual(self.article.current_version_id, self.source_version.pk)
        self.assertEqual(self.article.published_version_id, self.source_version.pk)
        self.assertEqual(self.source_version.status, ArticleStatus.PUBLISHED)
        self.assertEqual(approved.decision, ReviewDecision.APPROVED)
        self.assertEqual(result.target_version.status, ArticleStatus.IN_REVIEW)
        self.assertEqual(result.review.decision, ReviewDecision.PENDING)

    def test_grounding_failure_stays_draft_without_review(self):
        raw = output_json(self.claim.pk, title="机构在北京地区发布新规")

        result, _ = self.regenerate(raw=raw)

        self.assertNotEqual(result.content_validation.grounding_status, "PASS")
        self.assertEqual(result.content_validation.quality_status, "BLOCKED")
        self.assertEqual(result.target_version.status, ArticleStatus.DRAFT)
        self.assertIsNone(result.review)
        with self.assertRaises(ArticleReviewError):
            publish_article(result.target_version.pk, "不得发布", self.actor)

    def test_too_thin_quality_enters_human_review_when_grounding_passes(self):
        repeated = "机构发布新规"
        raw = output_json(
            self.claim.pk, title=repeated, one_sentence=repeated,
            what_happened=repeated,
        )

        result, _ = self.regenerate(raw=raw)

        self.assertEqual(result.content_validation.grounding_status, "PASS")
        self.assertEqual(result.content_validation.quality_status, "TOO_THIN")
        self.assertEqual(result.target_version.status, ArticleStatus.IN_REVIEW)
        self.assertEqual(result.review.decision, ReviewDecision.PENDING)

    def test_same_request_is_write_idempotent(self):
        first, _ = self.regenerate()
        before = {
            "versions": ArticleVersion.objects.count(),
            "calls": ModelCall.objects.count(),
            "gates": ArticleContentValidation.objects.count(),
            "reviews": Review.objects.count(),
            "audits": AuditLog.objects.count(),
            "runs": JobRun.objects.count(),
        }
        provider = FakeProvider(output_json(self.claim.pk))

        second, provider = self.regenerate(provider=provider)

        self.assertTrue(second.reused)
        self.assertEqual(second.target_version.pk, first.target_version.pk)
        self.assertEqual(provider.calls, [])
        self.assertEqual(before, {
            "versions": ArticleVersion.objects.count(),
            "calls": ModelCall.objects.count(),
            "gates": ArticleContentValidation.objects.count(),
            "reviews": Review.objects.count(),
            "audits": AuditLog.objects.count(),
            "runs": JobRun.objects.count(),
        })

    def test_new_explicit_request_creates_another_version_and_call(self):
        first, _ = self.regenerate(request_id="request-one")
        second, _ = self.regenerate(request_id="request-two")

        self.assertNotEqual(first.job_run.pk, second.job_run.pk)
        self.assertNotEqual(first.model_call.pk, second.model_call.pk)
        self.assertEqual(second.source_version.pk, first.target_version.pk)
        self.assertEqual(second.target_version.version_number, 3)

    def test_schema_failure_has_no_repair_or_target_and_cannot_retry_same_request(self):
        provider = FakeProvider("not-json", output_json(self.claim.pk))
        with self.assertRaises(ArticleRegenerationError):
            self.regenerate(request_id="bad-schema", provider=provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(ModelCall.objects.count(), 1)
        self.assertEqual(ArticleVersion.objects.count(), 1)

        second = FakeProvider(output_json(self.claim.pk))
        with self.assertRaises(ArticleRegenerationError) as caught:
            self.regenerate(request_id="bad-schema", provider=second)
        self.assertEqual(caught.exception.code, "ARTICLE_REGENERATION_PREVIOUSLY_FAILED")
        self.assertEqual(second.calls, [])
        self.assertEqual(ModelCall.objects.count(), 1)
