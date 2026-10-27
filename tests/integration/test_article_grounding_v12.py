import json

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.ai_content.schemas import parse_article_output
from apps.publishing.grounding import (
    ContentUnit, ContentUnitType, EstablishmentStatus, ExampleSemanticType,
    GroundingReference, GroundingStatus, article_content_units, classify_example,
    validate_content_units,
)


CLAIM = "The eruption disrupted thousands of flights and degraded air quality."
EVIDENCE = (
    "A powerful eruption from the Indonesian volcano lofted ash into the atmosphere, "
    "disrupting thousands of flights and degrading air quality."
)


def result_for(text):
    reference = GroundingReference(5, 5, CLAIM, EVIDENCE, "NASA")
    unit = ContentUnit(
        text=text, content_type=ContentUnitType.EXAMPLE,
        claim_ids=(5,), evidence_ids=(5,), section_key="example",
    )
    return validate_content_units((unit,), (reference,))


class ExampleContractV12Tests(SimpleTestCase):
    PASS_CASES = {
        "T01": "例如，这次事件中，数千架次航班受到影响。",
        "T02": "可以把数千架次航班受到影响理解为多个航空安排同时受到干扰。",
        "T05": "举个例子：这次火山喷发导致数千架次航班受到影响，可以理解为大量航班安排同时受到干扰。",
        "T10": "没有合适的例子可提供。",
        "T11": "当前证据只确认航班受到影响，无法进一步确定其他影响。",
        "T13": "如果把这次事件想象成很多航班同时被迫调整，可以帮助理解事件规模。",
        "T16": "例如，NASA报告的这次事件中，火山灰进入大气层并影响了数千架次航班。",
    }
    BLOCK_CASES = {
        "T03": "这是一个假设示例：如果发生类似的强烈火山喷发，火山灰可能被送入大气层，并造成航班中断和空气质量下降。",
        "T04": "这是一个假设示例：如果未来发生类似事件，航班可能受到影响。",
        "T06": "例如，火山喷发可能导致飞机发动机故障。",
        "T07": "例如，火山灰可能影响儿童和老人健康。",
        "T08": "例如，人们可能需要佩戴口罩。",
        "T09": "例如，如果发生类似事件，航空公司可能会增加航班数量。",
        "T12": "可能受到影响的是谁？",
        "T14": "如果以后类似火山喷发再次发生，航班也可能大面积取消。",
        "T15": "例如，这次事件造成的航班中断说明火山灰扩散可以影响航空运输。",
    }

    def test_t01_to_t16_contract_matrix(self):
        for key, text in self.PASS_CASES.items():
            with self.subTest(key=key):
                self.assertEqual(result_for(text).status, GroundingStatus.PASS)
        for key, text in self.BLOCK_CASES.items():
            with self.subTest(key=key):
                self.assertEqual(result_for(text).status, GroundingStatus.REVISION_REQUIRED)

    def test_modal_deletion_cannot_bypass_generalization(self):
        variants = (
            "如果发生类似的强烈火山喷发，火山灰可能被送入大气层，并造成航班中断和空气质量下降。",
            "如果发生类似的强烈火山喷发，火山灰会被送入大气层，并造成航班中断和空气质量下降。",
            "如果发生类似的强烈火山喷发，火山灰将被送入大气层，并造成航班中断和空气质量下降。",
        )
        for text in variants:
            result = result_for(text)
            self.assertEqual(result.status, GroundingStatus.REVISION_REQUIRED)
            self.assertIn("EXAMPLE_HYPOTHETICAL_GENERALIZATION", result.reason_codes)

    def test_abstract_analogy_and_event_restatement_pass(self):
        for text in (
            "可以把这次事件理解为多个航空安排同时受到干扰。",
            "例如，这次事件中数千架次航班受到影响。",
        ):
            self.assertEqual(result_for(text).status, GroundingStatus.PASS)

    def test_semantic_classifier_distinguishes_required_types(self):
        expected = {
            "例如，这次事件中，数千架次航班受到影响。": ExampleSemanticType.EVENT_RESTATEMENT,
            "可以把这次事件理解为多个航空安排同时受到干扰。": ExampleSemanticType.ABSTRACT_ANALOGY,
            "如果未来发生类似事件，航班可能受到影响。": ExampleSemanticType.HYPOTHETICAL_GENERALIZATION,
            "例如，火山喷发可能导致飞机发动机故障。": ExampleSemanticType.UNSUPPORTED_REAL_WORLD_EXAMPLE,
            "没有合适的例子可提供。": ExampleSemanticType.EMPTY_OR_NOT_APPLICABLE,
        }
        for text, semantic_type in expected.items():
            self.assertEqual(classify_example(text), semantic_type)

    def test_health_audience_advice_and_engine_guards_remain(self):
        expected_reasons = {
            "例如，火山喷发可能导致飞机发动机故障。": "EXAMPLE_REAL_WORLD_UNSUPPORTED",
            "例如，火山灰可能影响儿童和老人健康。": "FACT_NEW_HEALTH_IMPACT",
            "例如，人们可能需要佩戴口罩。": "UNSUPPORTED_ADVICE",
        }
        for text, reason in expected_reasons.items():
            result = result_for(text)
            self.assertEqual(result.status, GroundingStatus.REVISION_REQUIRED)
            self.assertIn(reason, result.reason_codes)


class NoneEstablishedContractTests(SimpleTestCase):
    CASES = (
        "已核实证据未具体确定受影响的人群或机构。",
        "目前没有明确的普通消费者直接影响。",
        "现有证据无法确定具体受影响人群。",
        "没有足够证据确定具体受影响对象。",
        "暂无明确受影响群体。",
        "证据未说明具体受影响对象。",
        "现有证据未建立该价格变动的重要性或影响。",
        "现有证据并未对具体受影响群体作出直接界定。",
    )

    def test_n01_to_n06_infer_none_established(self):
        for text in self.CASES:
            unit = article_content_units({"who_is_affected": text}, (5,), (5,))[0]
            self.assertEqual(unit.establishment_status, EstablishmentStatus.NONE_ESTABLISHED)
            self.assertEqual(unit.impact_level.value, "NONE_ESTABLISHED")

    def test_structured_status_is_preserved_when_wording_matches(self):
        unit = article_content_units({
            "who_is_affected": "现有证据无法确定具体受影响人群。",
            "who_is_affected_status": "NONE_ESTABLISHED",
        }, (5,), (5,))[0]
        self.assertEqual(unit.establishment_status, EstablishmentStatus.NONE_ESTABLISHED)
        self.assertTrue(unit.establishment_status_valid)

    def test_structured_status_cannot_hide_asserted_audience(self):
        reference = GroundingReference(5, 5, CLAIM, EVIDENCE, "NASA")
        unit = article_content_units({
            "who_is_affected": "儿童和老人受到直接影响。",
            "who_is_affected_status": "NONE_ESTABLISHED",
        }, (5,), (5,))[0]
        result = validate_content_units((unit,), (reference,))
        self.assertEqual(result.status, GroundingStatus.REVISION_REQUIRED)
        self.assertIn("ESTABLISHMENT_STATUS_MISMATCH", result.reason_codes)
        self.assertIn("FACT_NEW_AUDIENCE", result.reason_codes)

    def test_negative_preface_cannot_hide_assertion_after_contrast(self):
        unit = article_content_units({
            "who_is_affected": "现有证据未说明影响对象，但是儿童会受到直接影响。",
            "who_is_affected_status": "NONE_ESTABLISHED",
        }, (5,), (5,))[0]
        self.assertFalse(unit.establishment_status_valid)

    def test_job_125_absence_boundary_is_not_mistaken_for_causality(self):
        reference = GroundingReference(
            5, 5,
            "2026年8月，全国居民消费价格同比上涨0.8%。",
            "2026年8月份，全国居民消费价格同比上涨0.8%。",
            "国家统计局",
        )
        units = article_content_units({
            "why_it_matters": (
                "现有证据仅提供了一个价格指数同比涨幅，未说明这一涨幅对经济、政策、"
                "市场或民生的具体影响，因此无法建立其重要性。"
            ),
        }, (5,), (5,))
        result = validate_content_units(units, (reference,))
        self.assertEqual(result.status, GroundingStatus.PASS)


class CrossLanguageEntityContractTests(SimpleTestCase):
    def assert_entity_result(self, claim, evidence, output, expected):
        reference = GroundingReference(5, 5, claim, evidence, "Official source")
        unit = ContentUnit(
            text=output, content_type=ContentUnitType.FACT,
            claim_ids=(5,), evidence_ids=(5,), section_key="what_happened",
        )
        result = validate_content_units((unit,), (reference,))
        self.assertEqual(result.status, expected)
        return result

    def test_job_125_sec_chinese_official_name_matches_english_source(self):
        result = self.assert_entity_result(
            "The Securities and Exchange Commission issued a proposal.",
            "The Securities and Exchange Commission today issued a proposal.",
            "美国证券交易委员会发布了一项提案。",
            GroundingStatus.PASS,
        )
        self.assertNotIn("FACT_NEW_ENTITY", result.reason_codes)

    def test_job_125_apple_app_case_does_not_create_an_entity(self):
        result = self.assert_entity_result(
            "A redesigned Health app is coming later this year to iPhone.",
            "a redesigned Health app coming later this year to iPhone",
            "重新设计的健康 App 将于今年晚些时候登陆 iPhone。",
            GroundingStatus.PASS,
        )
        self.assertNotIn("FACT_NEW_ENTITY", result.reason_codes)

    def test_genuinely_new_entity_remains_blocked(self):
        result = self.assert_entity_result(
            "The SEC issued a proposal.",
            "The SEC today issued a proposal.",
            "微软公司发布了一项提案。",
            GroundingStatus.REVISION_REQUIRED,
        )
        self.assertIn("FACT_NEW_ENTITY", result.reason_codes)

    def test_job_6_ernest_ossei_boateng_repeated_across_article_sections_passes(self):
        reference = GroundingReference(
            5, 5,
            "The Securities and Exchange Commission charged Ernest Ossei Boateng.",
            "The Securities and Exchange Commission today charged Ernest Ossei Boateng.",
            "SEC",
        )
        units = article_content_units(
            {
                "title": "美国证交会指控 Ernest Ossei Boateng",
                "summary": "Ernest Ossei Boateng 被美国证券交易委员会指控。",
                "what_happened": "美国证券交易委员会指控 Ernest Ossei Boateng。",
            },
            (5,), (5,),
        )
        result = validate_content_units(units, (reference,))
        self.assertEqual(result.status, GroundingStatus.PASS)
        self.assertNotIn("FACT_NEW_ENTITY", result.reason_codes)

    def test_job_6_new_person_entity_is_still_blocked(self):
        result = self.assert_entity_result(
            "The Securities and Exchange Commission charged Ernest Ossei Boateng.",
            "The Securities and Exchange Commission charged Ernest Ossei Boateng.",
            "美国证券交易委员会指控 Ernest Ossei Boateng 和 Alice Example。",
            GroundingStatus.REVISION_REQUIRED,
        )
        self.assertIn("FACT_NEW_ENTITY", result.reason_codes)

    def test_job_3_regulator_aliases_match_english_sources(self):
        cases = (
            (
                "The Federal Deposit Insurance Corporation announced the action.",
                "联邦存款保险委员会宣布了这项行动。",
            ),
            (
                "The Federal Deposit Insurance Corporation announced the action.",
                "联邦存款保险公司宣布了这项行动。",
            ),
            (
                "The Commodity Futures Trading Commission announced the action.",
                "商品期货交易委员会宣布了这项行动。",
            ),
        )
        for source, output in cases:
            with self.subTest(source=source):
                result = self.assert_entity_result(
                    source, source, output, GroundingStatus.PASS
                )
                self.assertNotIn("FACT_NEW_ENTITY", result.reason_codes)

    def test_job_3_generic_company_count_is_not_an_entity(self):
        result = self.assert_entity_result(
            "The agency acted against an individual and two companies under his control.",
            "The agency acted against an individual and two companies under his control.",
            "该机构对一名个人及其控制的两家公司采取了行动。",
            GroundingStatus.PASS,
        )
        self.assertNotIn("FACT_NEW_ENTITY", result.reason_codes)

    def test_job_3_named_company_is_still_an_entity(self):
        result = self.assert_entity_result(
            "The agency announced an action.",
            "The agency announced an action.",
            "微软公司宣布了这项行动。",
            GroundingStatus.REVISION_REQUIRED,
        )
        self.assertIn("FACT_NEW_ENTITY", result.reason_codes)

    def test_lunar_south_pole_chinese_location_matches_english_source(self):
        result = self.assert_entity_result(
            "NASA is seeking proposals to establish a Moon Base in the lunar South Pole region.",
            "NASA is seeking proposals to establish a Moon Base in the lunar South Pole region.",
            "项目位于月球南极地区。",
            GroundingStatus.PASS,
        )
        self.assertNotIn("FACT_NEW_LOCATION", result.reason_codes)

    def test_genuinely_new_chinese_location_remains_blocked(self):
        result = self.assert_entity_result(
            "NASA is seeking proposals for lunar technology.",
            "NASA is seeking proposals for lunar technology.",
            "NASA正在北京地区征集月球技术提案。",
            GroundingStatus.REVISION_REQUIRED,
        )
        self.assertIn("FACT_NEW_LOCATION", result.reason_codes)


class CrossLanguageDateContractTests(SimpleTestCase):
    def test_job_3_english_month_date_matches_chinese_date(self):
        reference = GroundingReference(
            5, 5,
            "The order was issued on Sept. 3, 2026.",
            "The order was issued on Sept. 3, 2026.",
            "Official source",
        )
        unit = ContentUnit(
            text="该命令于2026年9月3日发布。",
            content_type=ContentUnitType.FACT,
            claim_ids=(5,), evidence_ids=(5,), section_key="what_happened",
        )

        result = validate_content_units((unit,), (reference,))

        self.assertEqual(result.status, GroundingStatus.PASS)
        self.assertNotIn("FACT_NEW_DATE", result.reason_codes)
        self.assertNotIn("FACT_NEW_NUMBER", result.reason_codes)

    def test_genuinely_new_date_is_still_blocked(self):
        reference = GroundingReference(
            5, 5, "The order was issued on Sept. 3, 2026.",
            "The order was issued on Sept. 3, 2026.", "Official source",
        )
        unit = ContentUnit(
            text="该命令于2026年9月4日发布。",
            content_type=ContentUnitType.FACT,
            claim_ids=(5,), evidence_ids=(5,), section_key="what_happened",
        )

        result = validate_content_units((unit,), (reference,))

        self.assertEqual(result.status, GroundingStatus.REVISION_REQUIRED)
        self.assertIn("FACT_NEW_DATE", result.reason_codes)

    def test_job_6_production_chinese_date_matches_english_month_date(self):
        reference = GroundingReference(
            5, 5,
            "The order was issued on September 3rd, 2026.",
            "The order was issued on Sept. 3, 2026.",
            "Official source",
        )
        units = article_content_units(
            {
                "title": "该命令于2026年9月3日发布",
                "summary": "2026年9月3日，美国机构发布该命令。",
                "what_happened": "该命令于2026年9月3日发布。",
            },
            (5,), (5,),
        )
        result = validate_content_units(units, (reference,))
        self.assertEqual(result.status, GroundingStatus.PASS)
        self.assertNotIn("FACT_NEW_DATE", result.reason_codes)


class ArticleContentSchemaV11Tests(SimpleTestCase):
    def payload(self, **overrides):
        data = {
            "title": "标题", "summary": "摘要", "what_happened": "事实",
            "why_it_matters": "现有证据没有确认明确的直接影响。",
            "why_it_matters_status": "NONE_ESTABLISHED",
            "who_is_affected": "现有证据无法确定具体受影响人群。",
            "who_is_affected_status": "NONE_ESTABLISHED",
            "example": None, "example_status": "NOT_APPLICABLE", "claim_ids": [5],
        }
        data.update(overrides)
        return json.dumps(data, ensure_ascii=False)

    def test_nullable_example_is_normalized_to_blank_database_value(self):
        parsed = parse_article_output(self.payload(), schema_version="article-content-v1.1")
        self.assertEqual(parsed["example"], "")
        self.assertEqual(parsed["example_status"], "NOT_APPLICABLE")

    def test_provided_example_requires_text(self):
        with self.assertRaises(ValidationError):
            parse_article_output(
                self.payload(example_status="PROVIDED"), schema_version="article-content-v1.1"
            )

    def test_empty_status_rejects_nonempty_example(self):
        with self.assertRaises(ValidationError):
            parse_article_output(
                self.payload(example="不应存在", example_status="EMPTY"),
                schema_version="article-content-v1.1",
            )
