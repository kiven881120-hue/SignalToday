from django.test import SimpleTestCase

from apps.events.clustering import (
    calculate_metrics,
    detect_conflicts,
    is_navigation_only_title,
    mixed_language_tokens,
    normalize_event_title,
    speech_event_signature,
    token_overlap,
)


class EventTitleNormalizationTests(SimpleTestCase):
    def test_navigation_only_titles_are_narrowly_identified(self):
        for title in ("更多", "更多 >", "查看更多>>", "More", " MORE "):
            with self.subTest(title=title):
                self.assertTrue(is_navigation_only_title(title))

        for title in ("更多财政政策细节发布", "More banks announce rate cuts"):
            with self.subTest(title=title):
                self.assertFalse(is_navigation_only_title(title))

    def test_normalizes_chinese_spacing_and_media_suffix(self):
        self.assertEqual(
            normalize_event_title("  OpenAI 发布新模型！——某媒体  "),
            "openai发布新模型",
        )

    def test_normalizes_english_case_and_whitespace(self):
        self.assertEqual(normalize_event_title("  OPENAI   Releases New Model "), "openai releases new model")

    def test_normalizes_full_width_characters(self):
        self.assertEqual(normalize_event_title("ＡＩ发布２０２６新品"), "ai发布2026新品")

    def test_decodes_html_entities(self):
        self.assertEqual(normalize_event_title("Research &amp; Development"), "research development")

    def test_preserves_mixed_language_numbers_and_models(self):
        self.assertEqual(normalize_event_title("苹果 发布 iPhone 17 Pro"), "苹果发布iphone 17 pro")

    def test_empty_title(self):
        self.assertEqual(normalize_event_title(None), "")
        self.assertEqual(normalize_event_title("  "), "")


class DeterministicMatchRuleTests(SimpleTestCase):
    def test_mixed_tokens_handle_chinese_without_spaces(self):
        tokens = mixed_language_tokens("苹果发布新款 iPhone")
        self.assertIn("苹果", tokens)
        self.assertIn("iphone", tokens)
        self.assertGreaterEqual(token_overlap("苹果发布新款iPhone", "苹果正式发布新款 iPhone"), 0.8)

    def test_conflicts_on_different_numbers_and_years(self):
        self.assertIn("different_numbers_or_models", detect_conflicts("苹果发布2026年新品", "苹果发布2025年新品"))
        self.assertIn("different_numbers_or_models", detect_conflicts("美联储降息25个基点", "美联储降息50个基点"))

    def test_conflicts_on_different_regions(self):
        self.assertIn("different_regions", detect_conflicts("中国央行降准", "美国央行降息"))

    def test_conflicts_on_different_companies_or_people(self):
        self.assertIn("different_entities", detect_conflicts("OpenAI发布新模型", "Google发布新模型"))
        self.assertIn("different_entities", detect_conflicts("Biden addresses summit", "Trump addresses summit"))

    def test_conflicts_on_product_model(self):
        self.assertIn("different_numbers_or_models", detect_conflicts("iPhone 17 Pro发布", "iPhone 18 Pro发布"))

    def test_conflicts_on_distinct_banking_entities(self):
        left = "Federal Reserve Board announces approval of the application by Coastal Bend Bancshares, Inc."
        right = "Federal Reserve Board announces approval of the application by FS Bancorp, Inc."
        self.assertIn("different_named_entities", detect_conflicts(left, right))

    def test_conflicts_on_distinct_named_companies(self):
        self.assertIn(
            "different_named_entities",
            detect_conflicts(
                "Acme Corp announces a strategic cloud partnership",
                "Globex Corp announces a strategic cloud partnership",
            ),
        )

    def test_conflicts_on_distinct_people_as_subjects(self):
        self.assertIn(
            "different_subject_entities",
            detect_conflicts("Alice Smith addresses climate summit", "Bob Jones addresses climate summit"),
        )

    def test_conflicts_on_named_product_variants(self):
        self.assertIn(
            "different_products",
            detect_conflicts("Apple unveils iPhone Air", "Apple unveils iPhone Pro"),
        )

    def test_same_entity_different_wording_has_no_entity_conflict(self):
        conflicts = detect_conflicts(
            "Acme Corp announces quarterly earnings",
            "Acme Corp reports quarterly earnings",
        )
        self.assertFalse(any(value.startswith("different_") for value in conflicts))

    def test_conflict_penalty_prevents_high_similarity_merge(self):
        metrics = calculate_metrics(0.98, 0.95, 1.0, ("different_numbers_or_models",))
        self.assertEqual(metrics.final_confidence, 0.0)

    def test_speech_report_variants_have_the_same_structural_signature(self):
        full_text = "习近平在金砖国家领导人第十八次会晤第一阶段会议的讲话（全文）"
        news_report = "习近平出席金砖国家领导人第十八次会晤第一阶段会议并发表重要讲话"
        self.assertEqual(speech_event_signature(full_text), speech_event_signature(news_report))

    def test_different_meeting_stages_have_different_structural_signatures(self):
        first_stage = "习近平在金砖国家领导人第十八次会晤第一阶段会议的讲话（全文）"
        second_stage = "习近平出席金砖国家领导人第十八次会晤第二阶段会议并发表重要讲话"
        self.assertNotEqual(speech_event_signature(first_stage), speech_event_signature(second_stage))
