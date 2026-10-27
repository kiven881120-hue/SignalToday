from types import SimpleNamespace
from difflib import SequenceMatcher

from django.test import SimpleTestCase

from apps.publishing.content_gate import build_quality_input
from apps.publishing.quality import QualityStatus, evaluate_article_quality


def version(**overrides):
    values = {
        "title": "政策更新",
        "one_sentence_summary": "新规将于2026年1月1日生效",
        "what_happened": "",
        "why_it_matters": "",
        "who_is_affected": "",
        "example": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ContentQualityInputTests(SimpleTestCase):
    def evaluate(self, value):
        quality_input, serialized = build_quality_input(
            value, {"article_mode": "BRIEF"}, grounding_status="PASS"
        )
        return evaluate_article_quality(quality_input), serialized

    def test_event_16_style_repetition_is_too_thin(self):
        result, serialized = self.evaluate(version(
            title="韦伯中红外数据揭示富氧硅酸盐尘埃清晰特征",
            one_sentence_summary="NASA的资料显示，韦伯的中红外数据揭示了富氧硅酸盐尘埃的清晰特征。",
            what_happened="根据NASA的资料，韦伯望远镜的中红外数据揭示了富氧硅酸盐尘埃的清晰特征。",
        ))
        self.assertEqual(result.quality_status, QualityStatus.TOO_THIN)
        self.assertTrue(serialized["sections"]["summary"]["units"])

    def test_event_3_style_body_repetition_is_recorded(self):
        result, _ = self.evaluate(version(
            title="NASA与欧洲伙伴发射Sentinel-6B卫星",
            one_sentence_summary="NASA及其欧洲伙伴于去年11月发射了Sentinel-6B卫星，以改善飓风预报。",
            what_happened="NASA及其欧洲伙伴于去年11月发射了Sentinel-6B卫星。",
            why_it_matters="据NASA介绍，该卫星旨在改善飓风预报。",
        ))
        body = next(item for item in result.section_results if item.section_key == "what_happened")
        self.assertTrue(body.repeated_unit_keys)

    def test_short_article_with_distinct_supported_information_passes(self):
        result, _ = self.evaluate(version())
        self.assertEqual(result.quality_status, QualityStatus.PASS)

    def test_job_3_supported_amount_in_similar_lead_is_information_gain(self):
        title = "监管机构对一名个人及其控制的两家公司提起诉讼"
        lead = "监管机构对一名个人及其控制的两家公司提起诉讼，涉案金额约1600万美元"
        self.assertGreater(
            SequenceMatcher(None, title, lead, autojunk=False).ratio(),
            0.72,
        )

        result, serialized = self.evaluate(version(
            title=title,
            one_sentence_summary=lead,
        ))

        self.assertEqual(result.quality_status, QualityStatus.PASS)
        self.assertNotIn("NO_GAIN_BEYOND_TITLE", result.failed_checks)
        self.assertNotEqual(
            serialized["sections"]["title"]["units"][0]["key"],
            serialized["sections"]["summary"]["units"][0]["key"],
        )

    def test_none_established_chinese_boundaries_are_not_factual_gain(self):
        value = version(
            title="政策发布",
            one_sentence_summary="政策发布。",
            why_it_matters="尚未确定这项政策为什么重要。",
            who_is_affected="没有证据表明存在受影响群体。",
        )
        quality_input, serialized = build_quality_input(
            value,
            {
                "article_mode": "BRIEF",
                "why_status": "WHY_NONE_ESTABLISHED",
                "who_status": "WHO_NONE_ESTABLISHED",
            },
            grounding_status="PASS",
        )

        result = evaluate_article_quality(quality_input)

        self.assertEqual(result.quality_status, QualityStatus.TOO_THIN)
        self.assertEqual(result.information_gain_score, 10)
        self.assertEqual(
            serialized["sections"]["why_it_matters"]["units"][0]["kind"],
            "EPISTEMIC_BOUNDARY",
        )
        self.assertEqual(
            serialized["sections"]["who_is_affected"]["units"][0]["kind"],
            "EPISTEMIC_BOUNDARY",
        )
