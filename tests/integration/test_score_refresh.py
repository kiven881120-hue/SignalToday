from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.ai_content.models import ModelCall
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, EvidenceType, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun
from apps.publishing.eligibility import evaluate_article_generation_eligibility
from apps.publishing.models import Article, ArticleVersion
from apps.reviews.models import Review
from apps.scoring.models import Score, ScoreRuleVersion, ScoreStatus
from apps.scoring.refresh import ScoreRefreshError, invalidate_event_score, refresh_event_score
from apps.sources.models import Source


class ScoreRefreshFixtureMixin:
    def make_fixture(self):
        self.now = timezone.now()
        ScoreRuleVersion.objects.update(active=False)
        self.rule = ScoreRuleVersion.objects.create(version="refresh-v1", name="Refresh", active=True)
        self.event = Event.objects.create(
            title="Refresh event", first_seen_at=self.now, last_seen_at=self.now,
        )
        self.score = Score.objects.create(
            event=self.event,
            rule_version=self.rule,
            importance=0,
            credibility=2,
            user_relevance=0,
            information_gain=0,
            freshness=10,
            long_term_impact=0,
            final_score=0,
        )
        AuditLog.objects.all().delete()

    def add_supported_claim(self):
        claim = Claim.objects.create(
            event=self.event,
            statement="NASA confirmed launch award 2026",
            importance=80,
            verification_status=VerificationStatus.SUPPORTED,
        )
        source = Source.objects.create(
            identifier="refresh-source",
            name="Refresh source",
            source_type="OFFICIAL",
            url="https://refresh.invalid",
            domain="refresh.invalid",
            tier="A",
        )
        raw = RawItem.objects.create(
            source=source,
            event=self.event,
            external_id="refresh-item",
            url="https://refresh.invalid/item",
            title=claim.statement,
            discovered_at=self.now,
        )
        evidence = ClaimEvidence.objects.create(
            claim=claim,
            raw_item=raw,
            source=source,
            evidence_type=EvidenceType.OFFICIAL,
            stance=EvidenceStance.SUPPORTS,
            evidence_text=claim.statement,
            captured_at=self.now,
        )
        return claim, evidence


class ScoreRefreshTests(ScoreRefreshFixtureMixin, TestCase):
    def setUp(self):
        self.make_fixture()

    def test_calculated_score_can_be_invalidated(self):
        result = invalidate_event_score(self.event.pk, "facts changed")
        result.score.refresh_from_db()
        self.assertTrue(result.changed)
        self.assertEqual(result.score.status, ScoreStatus.INVALID)

    def test_repeated_invalidation_is_idempotent(self):
        invalidate_event_score(self.event.pk, "facts changed")
        second = invalidate_event_score(self.event.pk, "facts changed again")
        self.assertFalse(second.changed)
        self.assertEqual(AuditLog.objects.filter(action="EVENT_SCORE_INVALIDATED").count(), 1)

    def test_invalidation_requires_reason(self):
        with self.assertRaises(ScoreRefreshError):
            invalidate_event_score(self.event.pk, "")

    def test_invalidation_does_not_change_score_values(self):
        before = (self.score.final_score, self.score.recommendation_level)
        invalidate_event_score(self.event.pk, "facts changed")
        self.score.refresh_from_db()
        self.assertEqual((self.score.final_score, self.score.recommendation_level), before)

    def test_invalidation_without_score_does_not_create_one(self):
        other = Event.objects.create(title="No score", first_seen_at=self.now, last_seen_at=self.now)
        result = invalidate_event_score(other.pk, "facts changed")
        self.assertEqual(result.reason_code, "SCORE_NOT_FOUND")
        self.assertFalse(Score.objects.filter(event=other).exists())

    def test_invalidation_rejects_missing_event(self):
        with self.assertRaises(ScoreRefreshError):
            invalidate_event_score(999999, "facts changed")

    def test_active_rule_is_required(self):
        self.rule.active = False
        self.rule.save(update_fields=["active"])
        with self.assertRaises(ScoreRefreshError):
            invalidate_event_score(self.event.pk, "facts changed")

    def test_rule_mismatch_does_not_invalidate_old_score(self):
        ScoreRuleVersion.objects.create(version="refresh-v2", name="New", active=True)
        result = invalidate_event_score(self.event.pk, "facts changed")
        self.score.refresh_from_db()
        self.assertEqual(result.reason_code, "SCORE_NOT_FOUND")
        self.assertEqual(self.score.status, ScoreStatus.CALCULATED)

    def test_invalid_score_refreshes_with_latest_claim_and_evidence(self):
        self.add_supported_claim()
        invalidate_event_score(self.event.pk, "facts changed")
        result = refresh_event_score(self.event.pk)
        result.score.refresh_from_db()
        self.assertEqual(result.score.status, ScoreStatus.CALCULATED)
        self.assertEqual(result.score.credibility, 10)
        self.assertEqual(result.score.information_gain, 5)
        self.assertEqual(result.score.importance, 8)

    def test_refresh_reuses_existing_calculation_function(self):
        invalidate_event_score(self.event.pk, "facts changed")
        with patch("apps.scoring.refresh.calculate_score", wraps=__import__(
            "apps.scoring.evaluation", fromlist=["calculate_score"]
        ).calculate_score) as calculation:
            refresh_event_score(self.event.pk)
        calculation.assert_called_once()

    def test_refresh_failure_leaves_invalid_and_records_failed_job(self):
        invalidate_event_score(self.event.pk, "facts changed")
        with patch("apps.scoring.refresh.calculate_score", side_effect=RuntimeError("calculation failed")):
            with self.assertRaises(RuntimeError):
                refresh_event_score(self.event.pk)
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, ScoreStatus.INVALID)
        self.assertEqual(JobRun.objects.get(job_type="EVENT_SCORE_REFRESH").status, JobStatus.FAILED)

    def test_failed_refresh_can_be_retried(self):
        invalidate_event_score(self.event.pk, "facts changed")
        with patch("apps.scoring.refresh.calculate_score", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                refresh_event_score(self.event.pk)
        result = refresh_event_score(self.event.pk)
        self.assertEqual(result.score.status, ScoreStatus.CALCULATED)

    def test_repeated_refresh_is_idempotent(self):
        invalidate_event_score(self.event.pk, "facts changed")
        first = refresh_event_score(self.event.pk)
        audits = AuditLog.objects.filter(action="EVENT_SCORE_REFRESHED").count()
        second = refresh_event_score(self.event.pk)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(AuditLog.objects.filter(action="EVENT_SCORE_REFRESHED").count(), audits)

    def test_refresh_audit_has_complete_old_and_new_snapshots(self):
        self.add_supported_claim()
        invalidate_event_score(self.event.pk, "facts changed")
        result = refresh_event_score(self.event.pk)
        audit = AuditLog.objects.get(action="EVENT_SCORE_REFRESHED")
        for side in (audit.before_data["snapshot"], audit.after_data["snapshot"]):
            self.assertEqual(set(side["dimensions"]), {
                "importance", "credibility", "user_relevance", "information_gain",
                "freshness", "long_term_impact",
            })
            self.assertIn("score", side)
            self.assertIn("level", side)
            self.assertIn("status", side)
            self.assertIn("rule_version", side)
        self.assertEqual(audit.after_data["job_run_id"], result.job_run.pk)

    def test_eligibility_rejects_invalid_score(self):
        self.add_supported_claim()
        invalidate_event_score(self.event.pk, "facts changed")
        self.assertEqual(
            evaluate_article_generation_eligibility(self.event).reason_code,
            "ARTICLE_SCORE_MISSING",
        )

    def test_eligibility_uses_calculated_refreshed_score(self):
        self.add_supported_claim()
        invalidate_event_score(self.event.pk, "facts changed")
        refresh_event_score(self.event.pk)
        result = evaluate_article_generation_eligibility(self.event)
        self.assertTrue(result.eligible)
        self.assertEqual(result.reason_code, "ARTICLE_ELIGIBLE")

    def test_refresh_records_successful_job_and_step(self):
        invalidate_event_score(self.event.pk, "facts changed")
        result = refresh_event_score(self.event.pk)
        self.assertEqual(result.job_run.status, JobStatus.SUCCESS)
        self.assertEqual(result.job_step.status, JobStatus.SUCCESS)
        self.assertEqual(result.job_step.output_summary["score_id"], self.score.pk)

    def test_refresh_without_score_is_rejected_without_job(self):
        other = Event.objects.create(title="No score", first_seen_at=self.now, last_seen_at=self.now)
        with self.assertRaises(ScoreRefreshError):
            refresh_event_score(other.pk)
        self.assertFalse(JobRun.objects.filter(job_type="EVENT_SCORE_REFRESH").exists())

    def test_commands_are_single_event_and_report_results(self):
        output = StringIO()
        call_command("invalidate_event_score", event=self.event.pk, reason="facts changed", stdout=output)
        call_command("refresh_event_score", event=self.event.pk, stdout=output)
        self.assertIn(f'"score_id": {self.score.pk}', output.getvalue())

    def test_refresh_has_no_unrelated_business_or_llm_side_effects(self):
        before = (
            Event.objects.count(), Claim.objects.count(), ClaimEvidence.objects.count(),
            Article.objects.count(), ArticleVersion.objects.count(), Review.objects.count(),
            ModelCall.objects.count(), Score.objects.count(),
        )
        invalidate_event_score(self.event.pk, "facts changed")
        refresh_event_score(self.event.pk)
        after = (
            Event.objects.count(), Claim.objects.count(), ClaimEvidence.objects.count(),
            Article.objects.count(), ArticleVersion.objects.count(), Review.objects.count(),
            ModelCall.objects.count(), Score.objects.count(),
        )
        self.assertEqual(before, after)


class ScoreRefreshConcurrencyTests(ScoreRefreshFixtureMixin, TransactionTestCase):
    def setUp(self):
        self.make_fixture()

    def run_in_thread(self, function):
        close_old_connections()
        try:
            return function()
        finally:
            connections.close_all()

    def test_concurrent_invalidation_creates_one_audit(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(
                lambda _: self.run_in_thread(
                    lambda: invalidate_event_score(self.event.pk, "facts changed")
                ),
                range(2),
            ))
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, ScoreStatus.INVALID)
        self.assertEqual(AuditLog.objects.filter(action="EVENT_SCORE_INVALIDATED").count(), 1)

    def test_concurrent_refresh_is_consistent_and_audited_once(self):
        invalidate_event_score(self.event.pk, "facts changed")
        AuditLog.objects.all().delete()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda _: self.run_in_thread(lambda: refresh_event_score(self.event.pk)),
                range(2),
            ))
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, ScoreStatus.CALCULATED)
        self.assertEqual(Score.objects.filter(event=self.event, rule_version=self.rule).count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="EVENT_SCORE_REFRESHED").count(), 1)
        self.assertEqual(sum(result.changed for result in results), 1)
        self.assertEqual(JobRun.objects.filter(job_type="EVENT_SCORE_REFRESH").count(), 2)
