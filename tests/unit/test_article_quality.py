from decimal import Decimal

from django.test import SimpleTestCase

from apps.publishing.quality import (
    ArticleQualityInput,
    ArticleQualityMode,
    ExampleStatus,
    InformationGainStatus,
    InformationKind,
    InformationUnit,
    QualityLevel,
    QualitySection,
    QualityStatus,
    WhoStatus,
    WhyStatus,
    evaluate_article_quality,
)


def unit(key, kind):
    return InformationUnit(key, kind)


EVENT = unit("event:new-object", InformationKind.CORE_FACT)
DETAIL = unit("detail:nasa-observation", InformationKind.CORE_FACT)
MEANING = unit("meaning:new-class-behavior", InformationKind.MEANING)
BOUNDARY = unit("boundary:affected-parties-unknown", InformationKind.EPISTEMIC_BOUNDARY)
ANALOGY = unit("analogy:classification-drawer", InformationKind.ABSTRACT_ANALOGY)


def section(text=None, *units):
    return QualitySection(text, units)


def standard(**overrides):
    values = {
        "article_mode": ArticleQualityMode.STANDARD,
        "grounding_status": "PASS",
        "title": section("NASA发现新天体", EVENT),
        "summary": section("NASA观测确认了新天体。", EVENT, DETAIL),
        "what_happened": section("观测记录确认了这次发现。", EVENT, DETAIL),
        "why_it_matters": section("证据支持它代表一类新的天体行为。", MEANING),
        "why_status": WhyStatus.WHY_ESTABLISHED,
        "who_is_affected": QualitySection(),
        "who_status": WhoStatus.WHO_NONE_ESTABLISHED,
        "example": QualitySection(),
        "example_status": ExampleStatus.EXAMPLE_NONE,
    }
    values.update(overrides)
    return ArticleQualityInput(**values)


class ArticleQualityGoldenTests(SimpleTestCase):
    def test_a_literal_repeat_why_is_too_thin(self):
        value = standard(
            summary=section("NASA发现新天体。", EVENT),
            what_happened=section("NASA发现新天体。", EVENT),
            why_it_matters=section("NASA发现新天体。", EVENT),
        )
        result = evaluate_article_quality(value)
        self.assertEqual(result.quality_status, QualityStatus.TOO_THIN)
        self.assertIn("WHY_REPEATS_CORE", result.failed_checks)

    def test_b_synonym_repeat_why_is_too_thin(self):
        value = standard(
            summary=section("NASA确认一个新天体。", EVENT),
            what_happened=section("一个此前未记录的天体被发现。", EVENT),
            why_it_matters=section("这项发现意味着NASA发现了新天体。", EVENT),
        )
        result = evaluate_article_quality(value)
        self.assertEqual(result.quality_status, QualityStatus.TOO_THIN)
        self.assertEqual(result.information_gain_status, InformationGainStatus.TOO_THIN)

    def test_c_meaningful_why_passes(self):
        result = evaluate_article_quality(standard())
        self.assertEqual(result.quality_status, QualityStatus.PASS)
        self.assertEqual(result.quality_level, QualityLevel.LEVEL_3)

    def test_d_none_who_does_not_block_good_article(self):
        result = evaluate_article_quality(standard(
            who_is_affected=section("现有证据未确定受影响对象。", BOUNDARY),
            who_status=WhoStatus.WHO_NONE_ESTABLISHED,
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_e_none_example_does_not_block_good_article(self):
        result = evaluate_article_quality(standard(
            example=QualitySection(), example_status=ExampleStatus.EXAMPLE_NONE
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_f_event_restatement_example_fails(self):
        result = evaluate_article_quality(standard(
            example=section("例如，NASA发现了这个新天体。", EVENT),
            example_status=ExampleStatus.EXAMPLE_EVENT_RESTATEMENT,
        ))
        self.assertEqual(result.quality_status, QualityStatus.FAIL)
        self.assertIn("EXAMPLE_EVENT_RESTATEMENT", result.failed_checks)

    def test_g_unsupported_real_world_example_fails(self):
        result = evaluate_article_quality(standard(
            example=section("另一个研究机构也发现过类似天体。", DETAIL),
            example_status=ExampleStatus.EXAMPLE_UNSUPPORTED_REAL_WORLD,
        ))
        self.assertEqual(result.quality_status, QualityStatus.FAIL)

    def test_h_hypothetical_future_example_fails(self):
        for status in (
            ExampleStatus.EXAMPLE_HYPOTHETICAL_GENERALIZATION,
            ExampleStatus.EXAMPLE_FUTURE_PREDICTION,
        ):
            with self.subTest(status=status):
                result = evaluate_article_quality(standard(
                    example=section("未来类似发现将改变科学认知。", MEANING),
                    example_status=status,
                ))
                self.assertEqual(result.quality_status, QualityStatus.FAIL)

    def test_i_abstract_analogy_passes(self):
        result = evaluate_article_quality(standard(
            example=section("可把分类过程理解为把观测放进不同抽屉。", ANALOGY),
            example_status=ExampleStatus.EXAMPLE_ABSTRACT_ANALOGY,
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)
        self.assertIn("analogy:classification-drawer", (
            result.section_results[-1].novel_unit_keys
        ))

    def test_j_limited_brief_passes(self):
        result = evaluate_article_quality(standard(
            article_mode=ArticleQualityMode.BRIEF,
            why_it_matters=QualitySection(),
            why_status=WhyStatus.WHY_NONE_ESTABLISHED,
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)
        self.assertEqual(result.quality_level, QualityLevel.LEVEL_2)

    def test_k_long_repetitive_article_is_too_thin(self):
        repeated = "NASA发现了新天体。" * 20
        result = evaluate_article_quality(standard(
            summary=section(repeated, EVENT),
            what_happened=section(repeated, EVENT),
            why_it_matters=section(repeated, EVENT),
        ))
        self.assertEqual(result.quality_status, QualityStatus.TOO_THIN)

    def test_l_short_dense_article_passes(self):
        result = evaluate_article_quality(standard(
            title=section("发现新天体", EVENT),
            summary=section("NASA确认。", EVENT, DETAIL),
            what_happened=section("观测成立。", EVENT, DETAIL),
            why_it_matters=section("揭示新行为。", MEANING),
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_m_clear_core_with_empty_optional_sections_passes_as_brief(self):
        result = evaluate_article_quality(standard(
            article_mode=ArticleQualityMode.BRIEF,
            why_it_matters=QualitySection(),
            why_status=WhyStatus.WHY_NONE_ESTABLISHED,
            who_is_affected=QualitySection(),
            who_status=WhoStatus.WHO_NONE_ESTABLISHED,
            example=QualitySection(),
            example_status=ExampleStatus.EXAMPLE_NONE,
        ))
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_n_grounding_pass_does_not_override_low_gain(self):
        result = evaluate_article_quality(standard(
            summary=section("NASA发现新天体。", EVENT),
            what_happened=section("NASA发现新天体。", EVENT),
            why_it_matters=section("NASA发现新天体。", EVENT),
        ))
        self.assertEqual(result.grounding_status, "PASS")
        self.assertEqual(result.quality_status, QualityStatus.TOO_THIN)

    def test_o_grounding_failure_blocks_high_gain(self):
        result = evaluate_article_quality(standard(grounding_status="FAIL"))
        self.assertEqual(result.information_gain_status, InformationGainStatus.PASS)
        self.assertEqual(result.quality_status, QualityStatus.BLOCKED)
        self.assertIn("GROUNDING_NOT_PASS", result.failed_checks)

    def test_p_high_score_does_not_override_low_quality(self):
        result = evaluate_article_quality(standard(
            upstream_score=Decimal("9.500"),
            summary=section("NASA发现新天体。", EVENT),
            what_happened=section("NASA发现新天体。", EVENT),
            why_it_matters=section("NASA发现新天体。", EVENT),
        ))
        self.assertEqual(result.upstream_score, Decimal("9.500"))
        self.assertFalse(result.quality_gate_passed)

    def test_q_low_score_is_not_changed_by_high_quality(self):
        result = evaluate_article_quality(standard(upstream_score=Decimal("2.000")))
        self.assertEqual(result.upstream_score, Decimal("2.000"))
        self.assertEqual(result.quality_status, QualityStatus.PASS)


class ArticleQualityContractTests(SimpleTestCase):
    def test_missing_semantic_annotations_needs_review(self):
        result = evaluate_article_quality(standard(
            why_it_matters=QualitySection("这可能有独立意义。"),
        ))
        self.assertEqual(result.quality_status, QualityStatus.NEEDS_REVIEW)
        self.assertIn("SEMANTIC_ANNOTATION_REQUIRED", result.failed_checks)

    def test_version_three_is_level_two_but_fails_standard_contract(self):
        eruption = unit("claim:5:eruption-impact", InformationKind.CORE_FACT)
        source = unit("claim:5:nasa-source", InformationKind.CORE_FACT)
        ash = unit("claim:5:ash-lofted", InformationKind.CORE_FACT)
        boundary = unit(
            "boundary:claim-5-no-audience",
            InformationKind.EPISTEMIC_BOUNDARY,
        )
        value = standard(
            title=section(
                "印尼火山喷发导致数千航班受影响、空气质量下降", eruption
            ),
            summary=section(
                "NASA观测信息显示，火山灰进入大气层并影响数千架次航班。",
                eruption, source, ash,
            ),
            what_happened=section(
                "NASA观测记录描述了喷发和影响。", eruption, source, ash
            ),
            why_it_matters=section(
                "该事件值得关注，因为发生了上述影响。", eruption
            ),
            who_is_affected=section("证据未确定具体对象。", boundary, eruption),
            who_status=WhoStatus.WHO_NONE_ESTABLISHED,
            example=section("例如，这次事件发生了上述影响。", eruption, source, ash),
            example_status=ExampleStatus.EXAMPLE_EVENT_RESTATEMENT,
        )
        result = evaluate_article_quality(value)
        self.assertEqual(result.quality_level, QualityLevel.LEVEL_2)
        self.assertEqual(result.information_gain_status, InformationGainStatus.TOO_THIN)
        self.assertEqual(result.quality_status, QualityStatus.FAIL)
        self.assertIn("WHY_REPEATS_CORE", result.failed_checks)
        self.assertIn("EXAMPLE_EVENT_RESTATEMENT", result.failed_checks)
