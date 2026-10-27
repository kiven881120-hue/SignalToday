import json

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.ai_content.prompts import (
    ARTICLE_GENERATION_SYSTEM_PROMPT_V1_1,
    ARTICLE_GENERATION_SYSTEM_PROMPT_V1_2,
    ARTICLE_GENERATION_SYSTEM_PROMPT_V1_3,
)
from apps.ai_content.schemas import (
    ARTICLE_GENERATION_SCHEMA_V1_1,
    ARTICLE_GENERATION_SCHEMA_V1_2,
    ARTICLE_GENERATION_SCHEMA_V1_3,
    article_schema_for_version,
    parse_article_output,
)
from apps.publishing.quality import (
    ArticleQualityInput,
    ArticleQualityMode,
    ExampleStatus,
    InformationKind,
    InformationUnit,
    QualitySection,
    QualityStatus,
    WhoStatus,
    WhyStatus,
    evaluate_article_quality,
)


CORE = InformationUnit("claim:1:core", InformationKind.CORE_FACT)
DETAIL = InformationUnit("claim:2:detail", InformationKind.CORE_FACT)
MEANING = InformationUnit("claim:3:meaning", InformationKind.MEANING)
IMPACT = InformationUnit("claim:4:impact", InformationKind.IMPACT_SCOPE)
WHO = InformationUnit("claim:5:who", InformationKind.AFFECTED_PARTY)
ANALOGY = InformationUnit("analogy:classification", InformationKind.ABSTRACT_ANALOGY)


def payload(**overrides):
    value = {
        "article_mode": "STANDARD",
        "title": "发现新天体",
        "one_sentence": "观测确认了新天体。",
        "what_happened": "NASA发布的观测记录确认了这次发现。",
        "why_status": "WHY_ESTABLISHED",
        "why_it_matters": "证据表明它代表一类新的天体行为。",
        "who_status": "WHO_NONE_ESTABLISHED",
        "who_affected": None,
        "example_type": "NONE",
        "example": None,
        "claim_ids": [1, 2, 3],
    }
    value.update(overrides)
    return value


def parse(value):
    return parse_article_output(
        json.dumps(value, ensure_ascii=False),
        schema_version="article-generation-v1.1",
    )


def section(text=None, *units):
    return QualitySection(text, units)


def quality_input(**overrides):
    values = {
        "article_mode": ArticleQualityMode.STANDARD,
        "grounding_status": "PASS",
        "title": section("发现新天体", CORE),
        "summary": section("观测确认了新天体。", CORE, DETAIL),
        "what_happened": section("观测记录确认发现。", CORE, DETAIL),
        "why_it_matters": section("这代表一类新的天体行为。", MEANING),
        "why_status": WhyStatus.WHY_ESTABLISHED,
        "who_is_affected": QualitySection(),
        "who_status": WhoStatus.WHO_NONE_ESTABLISHED,
        "example": QualitySection(),
        "example_status": ExampleStatus.EXAMPLE_NONE,
    }
    values.update(overrides)
    return ArticleQualityInput(**values)


class ArticleGenerationV11ContractTests(SimpleTestCase):
    def test_01_standard_with_established_why(self):
        output = parse(payload())
        self.assertEqual(output["why_status"], "WHY_ESTABLISHED")
        self.assertEqual(
            evaluate_article_quality(quality_input()).quality_status,
            QualityStatus.PASS,
        )

    def test_02_standard_with_none_why(self):
        output = parse(payload(
            why_status="WHY_NONE_ESTABLISHED",
            why_it_matters=None,
        ))
        result = evaluate_article_quality(quality_input(
            what_happened=section("证据确认了影响范围。", CORE, DETAIL, IMPACT),
            why_it_matters=QualitySection(),
            why_status=WhyStatus.WHY_NONE_ESTABLISHED,
        ))
        self.assertEqual(output["why_it_matters"], "")
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_03_standard_with_none_who(self):
        output = parse(payload())
        self.assertEqual(output["who_is_affected"], "")

    def test_04_standard_with_abstract_analogy(self):
        output = parse(payload(
            example_type="ABSTRACT_ANALOGY",
            example="可把分类过程理解为把观测放入不同抽屉。",
        ))
        result = evaluate_article_quality(quality_input(
            example=section(output["example"], ANALOGY),
            example_status=ExampleStatus.EXAMPLE_ABSTRACT_ANALOGY,
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_05_standard_with_none_example(self):
        self.assertEqual(parse(payload())["example"], "")

    def test_06_brief_with_all_optional_sections_none(self):
        output = parse(payload(
            article_mode="BRIEF",
            why_status="WHY_NONE_ESTABLISHED",
            why_it_matters=None,
        ))
        result = evaluate_article_quality(quality_input(
            article_mode=ArticleQualityMode.BRIEF,
            why_it_matters=QualitySection(),
            why_status=WhyStatus.WHY_NONE_ESTABLISHED,
        ))
        self.assertEqual(output["article_mode"], "BRIEF")
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_07_event_restatement_example_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse(payload(example_type="EVENT_RESTATEMENT", example="再次描述事件。"))

    def test_08_unsupported_real_world_example_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse(payload(example_type="UNSUPPORTED_REAL_WORLD", example="另一个机构也发现过。"))

    def test_09_hypothetical_example_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse(payload(
                example_type="HYPOTHETICAL_GENERALIZATION",
                example="如果未来再次发生，可能产生影响。",
            ))

    def test_10_core_fact_duplicated_in_why_is_rejected_by_quality(self):
        result = evaluate_article_quality(quality_input(
            summary=section("发现新天体。", CORE),
            what_happened=section("确认发现新天体。", CORE),
            why_it_matters=section("这很重要，因为发现了新天体。", CORE),
        ))
        self.assertEqual(result.quality_status, QualityStatus.TOO_THIN)

    def test_11_common_sense_who_is_blocked_by_grounding(self):
        output = parse(payload(
            who_status="WHO_ESTABLISHED",
            who_affected="相关企业和消费者。",
        ))
        result = evaluate_article_quality(quality_input(
            grounding_status="FAIL",
            who_is_affected=section(output["who_is_affected"], WHO),
            who_status=WhoStatus.WHO_ESTABLISHED,
        ))
        self.assertEqual(result.quality_status, QualityStatus.BLOCKED)

    def test_12_short_high_density_article_passes(self):
        result = evaluate_article_quality(quality_input(
            title=section("发现新天体", CORE),
            summary=section("NASA确认。", CORE, DETAIL),
            what_happened=section("揭示新行为。", MEANING),
            why_it_matters=QualitySection(),
            why_status=WhyStatus.WHY_NONE_ESTABLISHED,
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_13_long_repetitive_article_fails(self):
        repeated = "NASA发现了新天体。" * 30
        result = evaluate_article_quality(quality_input(
            summary=section(repeated, CORE),
            what_happened=section(repeated, CORE),
            why_it_matters=section(repeated, CORE),
        ))
        self.assertEqual(result.quality_status, QualityStatus.TOO_THIN)

    def test_14_grounding_pass_quality_fail_cannot_advance(self):
        result = evaluate_article_quality(quality_input(
            summary=section("发现新天体。", CORE),
            what_happened=section("发现新天体。", CORE),
            why_it_matters=section("发现新天体。", CORE),
        ))
        self.assertEqual((result.grounding_status, result.quality_gate_passed), ("PASS", False))

    def test_15_grounding_and_quality_pass_can_advance(self):
        result = evaluate_article_quality(quality_input())
        self.assertEqual((result.grounding_status, result.quality_gate_passed), ("PASS", True))

    def test_16_null_example_with_high_gain_passes(self):
        parse(payload())
        self.assertEqual(
            evaluate_article_quality(quality_input()).quality_status,
            QualityStatus.PASS,
        )

    def test_17_null_why_with_high_gain_passes(self):
        parse(payload(why_status="WHY_NONE_ESTABLISHED", why_it_matters=None))
        result = evaluate_article_quality(quality_input(
            what_happened=section("证据确认影响范围。", CORE, DETAIL, IMPACT),
            why_it_matters=QualitySection(),
            why_status=WhyStatus.WHY_NONE_ESTABLISHED,
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_18_null_who_with_high_gain_passes(self):
        parse(payload())
        self.assertEqual(
            evaluate_article_quality(quality_input()).quality_status,
            QualityStatus.PASS,
        )

    def test_prompt_and_schema_are_strict_v11_contracts(self):
        self.assertIs(
            article_schema_for_version("article-generation-v1.1"),
            ARTICLE_GENERATION_SCHEMA_V1_1,
        )
        normalized_prompt = " ".join(ARTICLE_GENERATION_SYSTEM_PROMPT_V1_1.split())
        for phrase in (
            "not traditional news copy",
            "Choose BRIEF",
            "WHY_NONE_ESTABLISHED",
            "WHO_NONE_ESTABLISHED",
            "ABSTRACT_ANALOGY",
            "Never use general knowledge",
            "cannot bypass downstream Grounding, Quality, or human Review gates",
        ):
            self.assertIn(phrase, normalized_prompt)

    def test_status_and_null_semantics_are_deterministic(self):
        with self.assertRaises(ValidationError):
            parse(payload(why_status="WHY_NONE_ESTABLISHED"))
        with self.assertRaises(ValidationError):
            parse(payload(example_type="NONE", example="强行填写例子。"))
        with self.assertRaises(ValidationError):
            parse(payload(article_mode="LONG_FORM"))

    def test_v12_schema_preserves_nullable_sections_and_grounding_statuses(self):
        output = parse_article_output(
            json.dumps(payload(
                article_mode="BRIEF",
                why_status="WHY_NONE_ESTABLISHED",
                why_it_matters=None,
            ), ensure_ascii=False),
            schema_version="article-generation-v1.2",
        )
        self.assertIs(
            article_schema_for_version("article-generation-v1.2"),
            ARTICLE_GENERATION_SCHEMA_V1_2,
        )
        self.assertEqual(output["why_it_matters"], "")
        self.assertEqual(output["why_it_matters_status"], "NONE_ESTABLISHED")
        self.assertEqual(output["who_is_affected_status"], "NONE_ESTABLISHED")
        self.assertEqual(output["example_status"], "EMPTY")

    def test_v12_prompt_requires_natural_body_and_omits_unsupported_sections(self):
        normalized = " ".join(ARTICLE_GENERATION_SYSTEM_PROMPT_V1_2.split())
        for phrase in (
            "one coherent short article",
            "without field headings",
            "Do not write boilerplate",
            "Score controls recommendation priority, not factual worth",
            "article-generation-v1.2",
        ):
            self.assertIn(phrase, normalized)

    def test_v13_allows_empty_body_without_changing_v12_contract(self):
        raw = json.dumps(payload(what_happened=None), ensure_ascii=False)
        output = parse_article_output(raw, schema_version="article-generation-v1.3")
        self.assertEqual(output["what_happened"], "")
        self.assertIs(
            article_schema_for_version("article-generation-v1.3"),
            ARTICLE_GENERATION_SCHEMA_V1_3,
        )
        with self.assertRaises(ValidationError):
            parse_article_output(raw, schema_version="article-generation-v1.2")

    def test_v13_prompt_forbids_title_summary_body_padding(self):
        normalized = " ".join(ARTICLE_GENERATION_SYSTEM_PROMPT_V1_3.split())
        for phrase in (
            "what_happened is optional and may be null",
            "Never paraphrase the same proposition",
            "title plus one sentence is a complete BRIEF article",
            "Do not target a fixed length",
            "article-generation-v1.3",
        ):
            self.assertIn(phrase, normalized)
