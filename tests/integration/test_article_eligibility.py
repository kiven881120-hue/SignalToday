from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.ai_content.models import ClaimCandidate, LLMPurpose, ModelCall, ModelCallStatus
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.publishing.eligibility import evaluate_article_generation_eligibility
from apps.publishing.models import Article, ArticleVersion
from apps.reviews.models import Review
from apps.scoring.models import RecommendationLevel, Score, ScoreRuleVersion
from apps.sources.models import Source


class ArticleEligibilityTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.rule = ScoreRuleVersion.objects.create(
            version=f"eligibility-{self.now.timestamp()}",
            name="Eligibility rule",
            rule_config={
                "weights": {
                    "importance": "0.20",
                    "credibility": "0.25",
                    "user_relevance": "0.15",
                    "information_gain": "0.15",
                    "freshness": "0.15",
                    "long_term_impact": "0.10",
                },
                "thresholds": {"FOCUS": "8.0", "NORMAL": "6.5", "CANDIDATE": "5.0"},
                "penalties": {},
            },
        )

    def make_event(self, key="event"):
        return Event.objects.create(
            title=key, first_seen_at=self.now, last_seen_at=self.now,
        )

    def add_score(self, event, value=10):
        return Score.objects.create(
            event=event, rule_version=self.rule,
            importance=value, credibility=value, user_relevance=value,
            information_gain=value, freshness=value, long_term_impact=value,
            final_score=value,
        )

    def add_claim(self, event, status=VerificationStatus.SUPPORTED, key="supported"):
        return Claim.objects.create(
            event=event, statement=f"{key} fact 2026", verification_status=status,
        )

    def add_evidence(self, claim, *, stance=EvidenceStance.SUPPORTS, key="supported"):
        source = Source.objects.create(
            identifier=f"source-{key}", name=f"Source {key}", source_type="OFFICIAL",
            url=f"https://{key}.invalid", domain=f"{key}.invalid", tier="A",
        )
        raw = RawItem.objects.create(
            source=source, event=claim.event, external_id=key, url=f"{source.url}/item",
            title=claim.statement, discovered_at=self.now,
        )
        return ClaimEvidence.objects.create(
            claim=claim, raw_item=raw, source=source, evidence_text=claim.statement,
            stance=stance, captured_at=self.now,
        )

    def eligible_event(self):
        event = self.make_event()
        self.add_score(event)
        claim = self.add_claim(event)
        evidence = self.add_evidence(claim)
        return event, claim, evidence

    def test_event_does_not_exist(self):
        result = evaluate_article_generation_eligibility(999999)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, "ARTICLE_EVENT_NOT_FOUND")

    def test_event_without_score_is_ineligible(self):
        result = evaluate_article_generation_eligibility(self.make_event())
        self.assertEqual((result.eligible, result.reason_code), (False, "ARTICLE_SCORE_MISSING"))

    def test_score_from_an_older_active_rule_is_not_current(self):
        event = self.make_event()
        older_rule = ScoreRuleVersion.objects.create(
            version=f"older-{self.now.timestamp()}",
            name="Older eligibility rule",
            rule_config=self.rule.rule_config,
        )
        ScoreRuleVersion.objects.filter(pk=self.rule.pk).update(created_at=self.now)
        ScoreRuleVersion.objects.filter(pk=older_rule.pk).update(
            created_at=self.now - timedelta(days=1)
        )
        Score.objects.create(
            event=event, rule_version=older_rule,
            importance=10, credibility=10, user_relevance=10,
            information_gain=10, freshness=10, long_term_impact=10,
            final_score=10,
        )
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual((result.eligible, result.reason_code), (False, "ARTICLE_SCORE_MISSING"))

    def test_reject_score_is_priority_not_value_judgment(self):
        event = self.make_event()
        score = self.add_score(event, value=0)
        claim = self.add_claim(event, key="niche-reject")
        self.add_evidence(claim, key="niche-reject")
        self.assertEqual(score.recommendation_level, RecommendationLevel.REJECT)
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual((result.eligible, result.reason_code), (True, "ARTICLE_ELIGIBLE"))
        self.assertIn("priority, not factual worth", result.reason)

    def test_candidate_score_can_proceed_with_traceable_evidence(self):
        event = self.make_event()
        score = self.add_score(event, value=6)
        claim = self.add_claim(event, key="niche-candidate")
        self.add_evidence(claim, key="niche-candidate")
        self.assertEqual(score.recommendation_level, RecommendationLevel.CANDIDATE)
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual((result.eligible, result.reason_code), (True, "ARTICLE_ELIGIBLE"))

    def test_event_without_claim_is_ineligible(self):
        event = self.make_event()
        self.add_score(event)
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual((result.eligible, result.reason_code), (False, "ARTICLE_CLAIMS_MISSING"))

    def test_only_proposed_candidate_is_ineligible(self):
        event = self.make_event()
        self.add_score(event)
        call = ModelCall.objects.create(
            provider="mock", model="mock", purpose=LLMPurpose.CLAIM_EXTRACTION,
            prompt_version="v", schema_version="v", status=ModelCallStatus.SUCCESS,
        )
        ClaimCandidate.objects.create(
            event=event, model_call=call, claim_text="Candidate only",
            claim_type="FACT", confidence=Decimal("0.9000"),
        )
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual((result.eligible, result.reason_code), (False, "ARTICLE_CLAIMS_MISSING"))

    def test_unverified_claim_is_ineligible(self):
        event = self.make_event()
        self.add_score(event)
        self.add_claim(event, status=VerificationStatus.UNVERIFIED)
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual((result.eligible, result.reason_code), (False, "ARTICLE_NO_SUPPORTED_CLAIMS"))

    def test_supported_claim_without_supporting_evidence_is_ineligible(self):
        event = self.make_event()
        self.add_score(event)
        claim = self.add_claim(event)
        self.add_evidence(claim, stance=EvidenceStance.MENTIONS)
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual(result.reason_code, "ARTICLE_NO_TRACEABLE_SUPPORTED_CLAIMS")

    def test_supported_claim_with_valid_supporting_evidence_is_eligible(self):
        event, claim, evidence = self.eligible_event()
        result = evaluate_article_generation_eligibility(event)
        self.assertTrue(result.eligible)
        self.assertEqual(result.reason_code, "ARTICLE_ELIGIBLE")
        self.assertEqual(result.eligible_claim_ids, (claim.pk,))
        self.assertEqual(result.eligible_evidence_ids, (evidence.pk,))

    def test_unverified_claim_is_excluded_without_blocking_supported_claim(self):
        event, supported, _ = self.eligible_event()
        self.add_claim(event, status=VerificationStatus.UNVERIFIED, key="unverified")
        result = evaluate_article_generation_eligibility(event)
        self.assertTrue(result.eligible)
        self.assertEqual(result.eligible_claim_ids, (supported.pk,))

    def test_contradicted_claim_is_excluded_and_conservatively_blocks_event(self):
        event, supported, _ = self.eligible_event()
        contradicted = self.add_claim(event, status=VerificationStatus.CONTRADICTED, key="contradicted")
        result = evaluate_article_generation_eligibility(event)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, "ARTICLE_CLAIM_CONFLICT_PRESENT")
        self.assertEqual(result.eligible_claim_ids, (supported.pk,))
        self.assertNotIn(contradicted.pk, result.eligible_claim_ids)

    def test_conflicted_claim_is_excluded_and_conservatively_blocks_event(self):
        event, supported, _ = self.eligible_event()
        conflicted = self.add_claim(event, status=VerificationStatus.CONFLICTED, key="conflicted")
        result = evaluate_article_generation_eligibility(event)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, "ARTICLE_CLAIM_CONFLICT_PRESENT")
        self.assertEqual(result.eligible_claim_ids, (supported.pk,))
        self.assertNotIn(conflicted.pk, result.eligible_claim_ids)

    def test_cross_event_evidence_excludes_supported_claim(self):
        event, claim, evidence = self.eligible_event()
        other = self.make_event("other")
        RawItem.objects.filter(pk=evidence.raw_item_id).update(event=other)
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual(result.reason_code, "ARTICLE_NO_TRACEABLE_SUPPORTED_CLAIMS")
        self.assertNotIn(claim.pk, result.eligible_claim_ids)

    def test_source_mismatch_excludes_supported_claim(self):
        event, claim, evidence = self.eligible_event()
        other = Source.objects.create(
            identifier="other", name="Other", source_type="MEDIA",
            url="https://other.invalid", domain="other.invalid", tier="B",
        )
        ClaimEvidence.objects.filter(pk=evidence.pk).update(source=other)
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual(result.reason_code, "ARTICLE_NO_TRACEABLE_SUPPORTED_CLAIMS")
        self.assertNotIn(claim.pk, result.eligible_claim_ids)

    def test_invalid_quote_excludes_supported_claim(self):
        event, claim, evidence = self.eligible_event()
        ClaimEvidence.objects.filter(pk=evidence.pk).update(
            evidence_text="Invented quote", normalized_evidence_text="Invented quote"
        )
        result = evaluate_article_generation_eligibility(event)
        self.assertEqual(result.reason_code, "ARTICLE_NO_TRACEABLE_SUPPORTED_CLAIMS")
        self.assertNotIn(claim.pk, result.eligible_claim_ids)

    def test_repeated_evaluation_is_identical_and_has_no_side_effects(self):
        event, _, _ = self.eligible_event()
        models = (Event, Claim, ClaimEvidence, Score, Article, ArticleVersion, Review, ModelCall, AuditLog)
        before = tuple(model.objects.count() for model in models)
        first = evaluate_article_generation_eligibility(event)
        second = evaluate_article_generation_eligibility(event.pk)
        after = tuple(model.objects.count() for model in models)
        self.assertEqual(first, second)
        self.assertEqual(before, after)
