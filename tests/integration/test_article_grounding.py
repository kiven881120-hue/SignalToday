from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.ai_content.models import ModelCall
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.publishing.grounding import (
    ContentUnit, ContentUnitType, GroundingReason, GroundingReference,
    GroundingStatus, ImpactLevel, validate_article_grounding,
    validate_content_units,
)
from apps.publishing.models import Article, ArticleVersion, ArticleVersionClaim
from apps.sources.models import Source


def reference(evidence_text, claim_text=None, **overrides):
    values = {
        "claim_id": 1,
        "evidence_id": 1,
        "claim_text": claim_text or evidence_text,
        "evidence_text": evidence_text,
        "source_name": "NASA",
    }
    values.update(overrides)
    return GroundingReference(**values)


def unit(text, content_type=ContentUnitType.FACT, **overrides):
    values = {"claim_ids": (1,), "evidence_ids": (1,), "section_key": "test"}
    values.update(overrides)
    return ContentUnit(text=text, content_type=content_type, **values)


class GroundingRuleTests(SimpleTestCase):
    def assert_passes(self, content_unit, source):
        result = validate_content_units((content_unit,), (source,))
        self.assertTrue(result.passed)
        self.assertEqual(result.status, GroundingStatus.PASS)
        self.assertEqual(result.reason_codes, (GroundingReason.GROUNDING_PASS.value,))

    def assert_revision(self, content_unit, source, reason):
        result = validate_content_units((content_unit,), (source,))
        self.assertFalse(result.passed)
        self.assertEqual(result.status, GroundingStatus.REVISION_REQUIRED)
        self.assertIn(reason.value, result.reason_codes)

    def test_accurate_chinese_contract_translation_passes(self):
        source = reference("NASA awarded a contract to Relativity Space.")
        self.assert_passes(unit("NASA 向 Relativity Space 授予了一份合同。"), source)

    def test_explanation_requires_the_meaning_to_exist_in_evidence(self):
        text = "简单来说，这意味着 NASA 现在可以让这家公司参与未来的相关任务。"
        insufficient = reference("NASA awarded a contract to Relativity Space.")
        self.assert_revision(
            unit(text, ContentUnitType.EXPLANATION),
            insufficient,
            GroundingReason.EXPLANATION_NEW_FACT,
        )
        sufficient = reference(
            "The contract allows Relativity Space to participate in future NASA missions."
        )
        self.assert_passes(unit(text, ContentUnitType.EXPLANATION), sufficient)

    def test_unsupported_health_impact_requires_revision(self):
        self.assert_revision(
            unit("这项发现可能影响普通人的健康。", ContentUnitType.EXPLANATION),
            reference("Scientists reported a new class of objects."),
            GroundingReason.FACT_NEW_HEALTH_IMPACT,
        )

    def test_unsupported_engine_failure_requires_revision(self):
        self.assert_revision(
            unit("这项发现可能导致发动机故障。", ContentUnitType.EXPLANATION),
            reference("Scientists reported a new class of objects."),
            GroundingReason.EXPLANATION_NEW_FACT,
        )

    def test_clearly_hypothetical_analogy_passes(self):
        self.assert_passes(
            unit(
                "如果把这个系统想象成一个很大的拼图，那么每块信息都帮助理解整体。",
                ContentUnitType.EXAMPLE,
            ),
            reference("The system combines information to describe the whole."),
        )

    def test_example_with_unsupported_real_date_requires_revision(self):
        self.assert_revision(
            unit("比如 NASA 将在 2027 年使用这项技术。", ContentUnitType.EXAMPLE),
            reference("NASA is evaluating the technology."),
            GroundingReason.FACT_NEW_DATE,
        )

    def test_none_established_is_valid_without_an_impact_claim(self):
        content = ContentUnit(
            text="目前没有明确的普通消费者直接影响。",
            content_type=ContentUnitType.EXPLANATION,
            section_key="who_is_affected",
            impact_level=ImpactLevel.NONE_ESTABLISHED,
        )
        self.assert_passes(content, reference("Scientists reported a new class of objects."))

    def test_unsupported_amount_requires_revision(self):
        self.assert_revision(
            unit("NASA awarded a $500 million contract."),
            reference("NASA awarded a contract."),
            GroundingReason.FACT_NEW_NUMBER,
        )

    def test_unsupported_entity_requires_revision(self):
        self.assert_revision(
            unit("美国国防部和 NASA 向 Relativity Space 授予了一份合同。"),
            reference("NASA awarded a contract to Relativity Space."),
            GroundingReason.FACT_NEW_ENTITY,
        )

    def test_certainty_expansion_requires_revision(self):
        self.assert_revision(
            unit("This proves that the finding helps researchers understand the issue."),
            reference("The finding may help researchers understand the issue."),
            GroundingReason.FACT_CERTAINTY_EXPANSION,
        )

    def test_normal_chinese_translation_and_explanation_markers_pass(self):
        source = reference("Scientists discovered a new class of objects not previously observed.")
        self.assert_passes(unit("科学家发现了一类此前没有观察到的对象。"), source)
        self.assert_passes(
            unit(
                "换句话说，科学家发现了一类此前没有观察到的对象。",
                ContentUnitType.EXPLANATION,
            ),
            source,
        )

    def test_background_and_speculation_are_not_allowed(self):
        source = reference("NASA awarded a contract.")
        self.assert_revision(
            unit("合同通常会持续多年。", ContentUnitType.BACKGROUND),
            source,
            GroundingReason.BACKGROUND_NOT_SUPPORTED,
        )
        self.assert_revision(
            unit("未来很可能产生其他结果。", ContentUnitType.SPECULATION),
            source,
            GroundingReason.SPECULATION_NOT_ALLOWED,
        )

    def test_invalid_evidence_chain_is_fail_not_revision(self):
        result = validate_content_units(
            (unit("NASA awarded a contract."),),
            (reference("NASA awarded a contract.", relationship_valid=False),),
        )
        self.assertEqual(result.status, GroundingStatus.FAIL)
        self.assertIn(GroundingReason.FACT_EVIDENCE_INVALID.value, result.reason_codes)

    def test_empty_input_fails_closed(self):
        result = validate_content_units((), ())
        self.assertEqual(result.status, GroundingStatus.FAIL)
        self.assertIn(GroundingReason.GROUNDING_EMPTY.value, result.reason_codes)

    def test_partial_claim_mapping_requires_revision(self):
        result = validate_content_units(
            (unit("NASA awarded a contract.", claim_ids=(1, 2)),),
            (reference("NASA awarded a contract."),),
        )
        self.assertEqual(result.status, GroundingStatus.REVISION_REQUIRED)
        self.assertIn(GroundingReason.FACT_NO_CLAIM.value, result.reason_codes)


class ArticleGroundingAdapterTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.event = Event.objects.create(
            title="Anak Krakatau Rumbles Again", first_seen_at=now, last_seen_at=now,
        )
        source = Source.objects.create(
            identifier="grounding-nasa", name="NASA", source_type="OFFICIAL",
            url="https://grounding.invalid", domain="grounding.invalid", tier="A", active=True,
        )
        statement = "The eruption disrupted thousands of flights and degraded air quality."
        claim = Claim.objects.create(
            event=self.event, statement=statement,
            verification_status=VerificationStatus.SUPPORTED, importance=74,
        )
        evidence_text = (
            "A powerful eruption from the Indonesian volcano lofted ash into the atmosphere, "
            "disrupting thousands of flights and degrading air quality."
        )
        raw = RawItem.objects.create(
            source=source, event=self.event, external_id="grounding-regression",
            url="https://grounding.invalid/item", title=self.event.title,
            raw_metadata={"summary": evidence_text}, discovered_at=now,
        )
        ClaimEvidence.objects.create(
            claim=claim, raw_item=raw, source=source, evidence_text=evidence_text,
            stance=EvidenceStance.SUPPORTS, captured_at=now,
        )
        article = Article.objects.create(event=self.event)
        self.version = ArticleVersion.objects.create(
            article=article, version_number=1,
            title="印尼火山喷发导致数千航班受影响、空气质量下降",
            one_sentence_summary="一场强烈的印尼火山喷发导致数千架次航班中断，并使空气质量下降。",
            what_happened="喷发将火山灰送入大气，干扰了数千架次航班并使空气质量下降。",
            why_it_matters="火山灰进入飞机发动机可能造成故障。空气质量下降可能影响呼吸健康。",
            who_is_affected="儿童、老人和呼吸道疾病患者等敏感人群可能受到影响。",
            example="例如，人们可能需要减少户外活动或佩戴口罩。",
            source_note=f"NASA: {raw.url}", language="zh-CN",
        )
        article.current_version = self.version
        article.save(update_fields=["current_version", "updated_at"])
        ArticleVersionClaim.objects.create(
            article_version=self.version, claim=claim,
            paragraph_key="article", display_order=1,
        )

    def test_article_one_regression_is_read_only_and_revision_required(self):
        before = {
            "article": Article.objects.count(),
            "version": ArticleVersion.objects.count(),
            "links": ArticleVersionClaim.objects.count(),
            "calls": ModelCall.objects.count(),
            "audits": AuditLog.objects.count(),
        }
        original = {
            field: getattr(self.version, field) for field in ArticleVersion.CONTENT_FIELDS
        }

        result = validate_article_grounding(self.version.pk)

        self.version.refresh_from_db()
        after = {
            "article": Article.objects.count(),
            "version": ArticleVersion.objects.count(),
            "links": ArticleVersionClaim.objects.count(),
            "calls": ModelCall.objects.count(),
            "audits": AuditLog.objects.count(),
        }
        self.assertEqual(result.status, GroundingStatus.REVISION_REQUIRED)
        self.assertIn(GroundingReason.EXPLANATION_NEW_FACT.value, result.reason_codes)
        self.assertIn(GroundingReason.FACT_NEW_HEALTH_IMPACT.value, result.reason_codes)
        self.assertIn(GroundingReason.FACT_NEW_AUDIENCE.value, result.reason_codes)
        self.assertIn(GroundingReason.UNSUPPORTED_ADVICE.value, result.reason_codes)
        self.assertEqual(before, after)
        self.assertEqual(
            original,
            {field: getattr(self.version, field) for field in ArticleVersion.CONTENT_FIELDS},
        )
