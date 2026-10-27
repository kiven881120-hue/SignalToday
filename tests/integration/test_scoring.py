from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connections
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, ClaimType, EvidenceStance, EvidenceType, VerificationStatus
from apps.events.admin import EventAdmin
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun
from apps.scoring.admin import ScoreAdmin, ScoreRuleVersionAdmin
from apps.scoring.evaluation import calculate_score, score_event
from apps.scoring.models import RecommendationLevel, Score, ScoreRuleVersion
from apps.scoring.rules import DEFAULT_RULE_CONFIG, recommendation_level, validate_rule_config, weighted_score
from apps.sources.models import Source


class InformationScoringTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.event = Event.objects.create(title="Scoring event", first_seen_at=self.now, last_seen_at=self.now)
        self.rule, _ = ScoreRuleVersion.objects.get_or_create(
            version="v1.0", defaults={"name": "default-information-value-v1"}
        )
        self.counter = 0

    def make_claim(self, status=VerificationStatus.SUPPORTED, importance=80, claim_type=ClaimType.FACT):
        self.counter += 1
        if status != VerificationStatus.SUPPORTED:
            importance = None
        return Claim.objects.create(event=self.event, statement=f"Claim {self.counter}", verification_status=status, importance=importance, claim_type=claim_type)

    def make_source(self, key, tier="B", group=""):
        return Source.objects.create(identifier=key, name=key, source_type="MEDIA", url=f"https://{key}.test", domain=f"{key}.test", tier=tier, source_group=group)

    def add_support(self, claim, source, key, evidence_type=""):
        raw = RawItem.objects.create(source=source, event=self.event, external_id=key, url=f"https://{source.domain}/{key}", title=key, discovered_at=self.now)
        return ClaimEvidence.objects.create(claim=claim, raw_item=raw, source=source, evidence_text=f"Evidence {key}", stance=EvidenceStance.SUPPORTS, evidence_type=evidence_type, captured_at=self.now)

    def test_weights_sum_to_one(self):
        weights, _ = validate_rule_config(DEFAULT_RULE_CONFIG)
        self.assertEqual(sum(weights.values()), Decimal("1"))

    def test_negative_weight_is_rejected(self):
        config = {key: value.copy() for key, value in DEFAULT_RULE_CONFIG.items()}
        config["weights"]["importance"] = "-0.1"
        with self.assertRaises(ValidationError):
            ScoreRuleVersion.objects.create(version="bad", name="Bad", rule_config=config)

    def test_score_range_accepts_zero_and_ten(self):
        score = Score.objects.create(event=self.event, rule_version=self.rule, final_score=0, **{field: 10 for field in ("importance", "credibility", "user_relevance", "information_gain", "freshness", "long_term_impact")})
        self.assertEqual(score.final_score, Decimal("10.000"))

    def test_score_below_zero_is_rejected(self):
        with self.assertRaises(ValidationError):
            Score.objects.create(event=self.event, rule_version=self.rule, final_score=0, importance=-1)

    def test_score_above_ten_is_rejected(self):
        with self.assertRaises(ValidationError):
            Score.objects.create(event=self.event, rule_version=self.rule, final_score=0, credibility=11)

    def test_weighted_six_dimension_calculation(self):
        values = {"importance": 8, "credibility": 8, "user_relevance": 6, "information_gain": 4, "freshness": 10, "long_term_impact": 2}
        self.assertEqual(weighted_score(values, DEFAULT_RULE_CONFIG), Decimal("6.700"))

    def test_decimal_calculation_is_exact(self):
        values = {field: Decimal("7.333") for field in ("importance", "credibility", "user_relevance", "information_gain", "freshness", "long_term_impact")}
        self.assertEqual(weighted_score(values, DEFAULT_RULE_CONFIG), Decimal("7.333"))

    def test_final_score_is_clamped_to_ten(self):
        self.assertEqual(weighted_score({field: 20 for field in ("importance", "credibility", "user_relevance", "information_gain", "freshness", "long_term_impact")}, DEFAULT_RULE_CONFIG), Decimal("10.000"))

    def test_recommendation_8_is_focus(self):
        self.assertEqual(recommendation_level("8.0", DEFAULT_RULE_CONFIG), RecommendationLevel.FOCUS)

    def test_recommendation_799_is_normal(self):
        self.assertEqual(recommendation_level("7.99", DEFAULT_RULE_CONFIG), RecommendationLevel.NORMAL)

    def test_recommendation_65_is_normal(self):
        self.assertEqual(recommendation_level("6.5", DEFAULT_RULE_CONFIG), RecommendationLevel.NORMAL)

    def test_recommendation_649_is_candidate(self):
        self.assertEqual(recommendation_level("6.49", DEFAULT_RULE_CONFIG), RecommendationLevel.CANDIDATE)

    def test_recommendation_5_is_candidate(self):
        self.assertEqual(recommendation_level("5.0", DEFAULT_RULE_CONFIG), RecommendationLevel.CANDIDATE)

    def test_recommendation_499_is_reject(self):
        self.assertEqual(recommendation_level("4.99", DEFAULT_RULE_CONFIG), RecommendationLevel.REJECT)

    def test_official_support_gives_high_credibility(self):
        claim = self.make_claim()
        source = self.make_source("official", "A", "authority")
        self.add_support(claim, source, "official", EvidenceType.OFFICIAL)
        self.assertEqual(calculate_score(self.event, self.rule, now=self.now).credibility, Decimal("10"))

    def test_two_independent_reliable_sources_give_high_credibility(self):
        claim = self.make_claim()
        for key in ("one", "two"):
            source = self.make_source(key, "B", key)
            self.add_support(claim, source, key)
        self.assertEqual(calculate_score(self.event, self.rule, now=self.now).credibility, Decimal("9"))

    def test_single_reliable_partial_claim_gives_middle_credibility(self):
        self.make_claim(VerificationStatus.PARTIALLY_SUPPORTED)
        self.assertEqual(calculate_score(self.event, self.rule, now=self.now).credibility, Decimal("6.5"))

    def test_unverified_claim_cannot_have_high_credibility(self):
        self.make_claim(VerificationStatus.UNVERIFIED)
        self.assertLessEqual(calculate_score(self.event, self.rule, now=self.now).credibility, 5)

    def test_conflicted_claim_cannot_have_high_credibility(self):
        self.make_claim(VerificationStatus.CONFLICTED)
        self.assertLessEqual(calculate_score(self.event, self.rule, now=self.now).credibility, 5)

    def test_contradicted_claim_has_low_credibility(self):
        self.make_claim(VerificationStatus.CONTRADICTED)
        self.assertLessEqual(calculate_score(self.event, self.rule, now=self.now).credibility, 3)

    def test_same_source_group_counts_once(self):
        claim = self.make_claim()
        for key in ("same1", "same2"):
            source = self.make_source(key, "B", "same")
            self.add_support(claim, source, key)
        self.assertEqual(calculate_score(self.event, self.rule, now=self.now).credibility, Decimal("8"))

    def test_different_source_groups_count_separately(self):
        claim = self.make_claim()
        for key in ("diff1", "diff2"):
            source = self.make_source(key, "B", key)
            self.add_support(claim, source, key)
        self.assertEqual(calculate_score(self.event, self.rule, now=self.now).credibility, Decimal("9"))

    def freshness_at(self, hours):
        self.event.first_seen_at = self.now - timedelta(hours=hours)
        self.event.save(update_fields=["first_seen_at"])
        return calculate_score(self.event, self.rule, now=self.now).freshness

    def test_freshness_within_six_hours(self):
        self.assertEqual(self.freshness_at(6), Decimal("10"))

    def test_freshness_at_twenty_four_hours(self):
        self.assertEqual(self.freshness_at(24), Decimal("9"))

    def test_freshness_at_forty_eight_hours(self):
        self.assertEqual(self.freshness_at(48), Decimal("7"))

    def test_freshness_at_seventy_two_hours(self):
        self.assertEqual(self.freshness_at(72), Decimal("5"))

    def test_missing_event_time_is_conservative(self):
        self.event.first_seen_at = None
        result = calculate_score(self.event, self.rule, now=self.now)
        self.assertEqual(result.freshness, Decimal("3"))
        self.assertIn("unavailable", result.reasons["freshness_reason"])

    def test_information_gain_ignores_duplicate_source_volume(self):
        claim = self.make_claim()
        before = calculate_score(self.event, self.rule, now=self.now).information_gain
        source = self.make_source("volume")
        for index in range(10):
            RawItem.objects.create(source=source, event=self.event, url=f"https://volume.test/{index}", title=str(index), discovered_at=self.now)
        after = calculate_score(self.event, self.rule, now=self.now).information_gain
        self.assertEqual(before, after)

    def test_same_event_and_rule_is_idempotent(self):
        self.make_claim()
        score_event(self.event, self.rule, now=self.now)
        score_event(self.event, self.rule, now=self.now)
        self.assertEqual(Score.objects.filter(event=self.event, rule_version=self.rule).count(), 1)

    def test_different_rule_versions_preserve_history(self):
        self.make_claim()
        other = ScoreRuleVersion.objects.create(version="v1.1", name="Next")
        score_event(self.event, self.rule, now=self.now)
        score_event(self.event, other, now=self.now)
        self.assertEqual(Score.objects.filter(event=self.event).count(), 2)

    def test_dry_run_changes_no_database_state(self):
        before = (Score.objects.count(), JobRun.objects.count(), AuditLog.objects.count())
        call_command("score_events", dry_run=True, event=self.event.pk, stdout=StringIO())
        self.assertEqual((Score.objects.count(), JobRun.objects.count(), AuditLog.objects.count()), before)

    def test_formal_command_records_job_statistics(self):
        call_command("score_events", event=self.event.pk, stdout=StringIO())
        run = JobRun.objects.get(job_type="EVENT_SCORING")
        self.assertEqual(run.status, JobStatus.SUCCESS)
        self.assertEqual(run.steps.get().output_summary["scored_count"], 1)

    def test_score_change_creates_audit_with_job_and_rule(self):
        call_command("score_events", event=self.event.pk, stdout=StringIO())
        audit = AuditLog.objects.get(action="EVENT_SCORE_CHANGED")
        self.assertEqual(audit.after_data["rule_version"], "v1.0")
        self.assertIsNotNone(audit.after_data["job_run_id"])

    def test_admin_exposes_score_rule_and_event_summary(self):
        score_admin = ScoreAdmin(Score, admin.site)
        rule_admin = ScoreRuleVersionAdmin(ScoreRuleVersion, admin.site)
        event_admin = EventAdmin(Event, admin.site)
        self.assertIn("recommendation_level", score_admin.list_display)
        self.assertIn("active", rule_admin.list_display)
        self.assertIn("current_score", event_admin.list_display)


class ScoringConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.event = Event.objects.create(title="Concurrent score", first_seen_at=self.now, last_seen_at=self.now)
        self.rule, _ = ScoreRuleVersion.objects.get_or_create(
            version="v1.0", defaults={"name": "default-information-value-v1"}
        )

    def test_concurrent_scoring_creates_one_score(self):
        event_id, rule_id, now = self.event.pk, self.rule.pk, self.now

        def run():
            close_old_connections()
            try:
                score_event(Event.objects.get(pk=event_id), ScoreRuleVersion.objects.get(pk=rule_id), now=now)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: run(), range(2)))
        self.assertEqual(Score.objects.filter(event_id=event_id, rule_version_id=rule_id).count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="EVENT_SCORE_CHANGED").count(), 1)
