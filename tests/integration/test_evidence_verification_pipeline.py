from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.ai_content.models import ModelCall
from apps.audit.models import AuditLog
from apps.claims.models import (
    Claim,
    ClaimEvidence,
    EvidenceStance,
    EvidenceType,
    VerificationStatus,
)
from apps.claims.verification_pipeline import (
    EvidenceVerificationPipelineError,
    EvidenceVerificationPipelineRetryRequired,
    PIPELINE_JOB_TYPE,
    verify_evidence_pipeline,
)
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun
from apps.publishing.models import Article, ArticleVersion
from apps.reviews.models import Review
from apps.scoring.models import Score, ScoreRuleVersion, ScoreStatus
from apps.sources.models import Source


class VerificationPipelineFixtureMixin:
    def make_fixture(self, *, tier="A", stance=EvidenceStance.MENTIONS, claim_status=VerificationStatus.UNVERIFIED):
        self.now = timezone.now()
        ScoreRuleVersion.objects.update(active=False)
        self.rule = ScoreRuleVersion.objects.create(version="pipeline-v1", name="Pipeline", active=True)
        self.event = Event.objects.create(
            title="Pipeline event", first_seen_at=self.now, last_seen_at=self.now,
        )
        self.claim = Claim.objects.create(
            event=self.event,
            statement="Agency confirmed award 2026",
            importance=80 if claim_status == VerificationStatus.SUPPORTED else None,
            verification_status=claim_status,
        )
        self.source = Source.objects.create(
            identifier="pipeline-source",
            name="Pipeline source",
            source_type="OFFICIAL",
            url="https://pipeline.invalid",
            domain="pipeline.invalid",
            tier=tier,
        )
        self.raw = RawItem.objects.create(
            source=self.source,
            event=self.event,
            external_id="pipeline-item",
            url="https://pipeline.invalid/item",
            title=self.claim.statement,
            discovered_at=self.now,
        )
        self.evidence = ClaimEvidence.objects.create(
            claim=self.claim,
            raw_item=self.raw,
            source=self.source,
            stance=stance,
            evidence_text=self.claim.statement,
            captured_at=self.now,
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
        JobRun.objects.all().delete()

    def fake_evaluation(self, status):
        self.claim.verification_status = status
        self.claim.save(update_fields=["verification_status", "updated_at"])
        run = JobRun.objects.create(job_type="CLAIM_EVALUATION", status=JobStatus.SUCCESS)
        result = SimpleNamespace(status=status)
        stats = {"errors": 0}
        return stats, [(self.claim, result)], run


class EvidenceVerificationPipelineTests(VerificationPipelineFixtureMixin, TestCase):
    def setUp(self):
        self.make_fixture()

    def test_mentions_to_supports_runs_complete_pipeline(self):
        result = verify_evidence_pipeline(self.evidence.pk)
        self.evidence.refresh_from_db()
        self.claim.refresh_from_db()
        self.score.refresh_from_db()
        self.assertEqual(result.job_run.status, JobStatus.SUCCESS)
        self.assertEqual(self.evidence.stance, EvidenceStance.SUPPORTS)
        self.assertEqual(self.claim.verification_status, VerificationStatus.SUPPORTED)
        self.assertEqual(self.score.status, ScoreStatus.CALCULATED)
        self.assertTrue(result.eligibility.eligible)
        self.assertEqual(result.eligibility.recommendation_level, "REJECT")
        self.assertIsNone(self.claim.importance)

    def test_already_final_state_skips_mutation_stages(self):
        self.evidence.stance = EvidenceStance.SUPPORTS
        self.evidence.save(update_fields=["stance"])
        self.claim.verification_status = VerificationStatus.SUPPORTED
        self.claim.save(update_fields=["verification_status", "updated_at"])
        AuditLog.objects.all().delete()
        before = self.score.updated_at
        result = verify_evidence_pipeline(self.evidence.pk)
        self.score.refresh_from_db()
        self.assertEqual(self.score.updated_at, before)
        self.assertFalse(AuditLog.objects.exists())
        self.assertFalse(result.job_run.steps.get(step_name="refresh_score").output_summary["changed"])

    def test_review_failure_stops_pipeline(self):
        with patch("apps.claims.verification_pipeline.review_claim_evidence", side_effect=RuntimeError("review failed")):
            with self.assertRaises(EvidenceVerificationPipelineError):
                verify_evidence_pipeline(self.evidence.pk)
        self.assertEqual(JobRun.objects.get(job_type=PIPELINE_JOB_TYPE).status, JobStatus.FAILED)
        self.assertFalse(JobRun.objects.filter(job_type="CLAIM_EVALUATION").exists())

    def test_evaluation_failure_does_not_invalidate_or_refresh(self):
        with patch("apps.claims.verification_pipeline.ClaimEvaluationService.run", side_effect=RuntimeError("evaluation failed")):
            with self.assertRaises(EvidenceVerificationPipelineError):
                verify_evidence_pipeline(self.evidence.pk)
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, ScoreStatus.CALCULATED)
        self.assertFalse(JobRun.objects.filter(job_type="EVENT_SCORE_REFRESH").exists())

    def test_unverified_result_still_refreshes_when_evidence_changed(self):
        self.source.tier = "E"
        self.source.save(update_fields=["tier"])
        self.evidence.evidence_type = EvidenceType.SECONDARY
        self.evidence.save(update_fields=["evidence_type"])
        result = verify_evidence_pipeline(self.evidence.pk)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.verification_status, VerificationStatus.UNVERIFIED)
        self.assertFalse(result.eligibility.eligible)
        self.assertTrue(JobRun.objects.filter(job_type="EVENT_SCORE_REFRESH").exists())

    def test_supported_result_refreshes_score(self):
        verify_evidence_pipeline(self.evidence.pk)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.verification_status, VerificationStatus.SUPPORTED)
        self.assertTrue(AuditLog.objects.filter(action="EVENT_SCORE_REFRESHED").exists())

    def test_partially_supported_result_refreshes_score(self):
        self.source.tier = "B"
        self.source.save(update_fields=["tier"])
        self.evidence.evidence_type = EvidenceType.NEWS_REPORT
        self.evidence.save(update_fields=["evidence_type"])
        verify_evidence_pipeline(self.evidence.pk)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.verification_status, VerificationStatus.PARTIALLY_SUPPORTED)
        self.assertTrue(JobRun.objects.filter(job_type="EVENT_SCORE_REFRESH").exists())

    def test_contradicted_result_refreshes_score(self):
        with patch(
            "apps.claims.verification_pipeline.ClaimEvaluationService.run",
            side_effect=lambda queryset: self.fake_evaluation(VerificationStatus.CONTRADICTED),
        ):
            verify_evidence_pipeline(self.evidence.pk)
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, ScoreStatus.CALCULATED)

    def test_conflicted_result_refreshes_score(self):
        other_raw = RawItem.objects.create(
            source=self.source,
            event=self.event,
            external_id="contradiction",
            url="https://pipeline.invalid/contradiction",
            title="Contradiction",
            discovered_at=self.now,
        )
        ClaimEvidence.objects.create(
            claim=self.claim,
            raw_item=other_raw,
            source=self.source,
            stance=EvidenceStance.CONTRADICTS,
            evidence_text="Contradiction",
            captured_at=self.now,
        )
        verify_evidence_pipeline(self.evidence.pk)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.verification_status, VerificationStatus.CONFLICTED)

    def test_invalidation_failure_stops_before_refresh(self):
        with patch("apps.claims.verification_pipeline.invalidate_event_score", side_effect=RuntimeError("invalidate failed")):
            with self.assertRaises(EvidenceVerificationPipelineError):
                verify_evidence_pipeline(self.evidence.pk)
        self.assertFalse(JobRun.objects.filter(job_type="EVENT_SCORE_REFRESH").exists())

    def test_refresh_failure_leaves_score_invalid(self):
        with patch("apps.claims.verification_pipeline.refresh_event_score", side_effect=RuntimeError("refresh failed")):
            with self.assertRaises(EvidenceVerificationPipelineError):
                verify_evidence_pipeline(self.evidence.pk)
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, ScoreStatus.INVALID)

    def test_refresh_failure_requires_explicit_retry(self):
        with patch("apps.claims.verification_pipeline.refresh_event_score", side_effect=RuntimeError("refresh failed")):
            with self.assertRaises(EvidenceVerificationPipelineError):
                verify_evidence_pipeline(self.evidence.pk)
        with self.assertRaises(EvidenceVerificationPipelineRetryRequired):
            verify_evidence_pipeline(self.evidence.pk)

    def test_refresh_failure_retry_resumes_only_refresh(self):
        with patch("apps.claims.verification_pipeline.refresh_event_score", side_effect=RuntimeError("refresh failed")):
            with self.assertRaises(EvidenceVerificationPipelineError):
                verify_evidence_pipeline(self.evidence.pk)
        review_audits = AuditLog.objects.filter(action="CLAIM_EVIDENCE_UPDATED").count()
        evaluation_runs = JobRun.objects.filter(job_type="CLAIM_EVALUATION").count()
        result = verify_evidence_pipeline(self.evidence.pk, retry=True)
        self.assertEqual(result.job_run.status, JobStatus.SUCCESS)
        self.assertEqual(AuditLog.objects.filter(action="CLAIM_EVIDENCE_UPDATED").count(), review_audits)
        self.assertEqual(JobRun.objects.filter(job_type="CLAIM_EVALUATION").count(), evaluation_runs)

    def test_successful_pipeline_is_idempotent(self):
        first = verify_evidence_pipeline(self.evidence.pk)
        counts = (JobRun.objects.count(), AuditLog.objects.count())
        second = verify_evidence_pipeline(self.evidence.pk)
        self.assertEqual(first.job_run.pk, second.job_run.pk)
        self.assertEqual((JobRun.objects.count(), AuditLog.objects.count()), counts)

    def test_pipeline_has_five_successful_steps(self):
        result = verify_evidence_pipeline(self.evidence.pk)
        self.assertEqual(result.job_run.steps.count(), 5)
        self.assertFalse(result.job_run.steps.exclude(status=JobStatus.SUCCESS).exists())

    def test_pipeline_does_not_duplicate_business_entities(self):
        before = (Claim.objects.count(), ClaimEvidence.objects.count(), Score.objects.count())
        verify_evidence_pipeline(self.evidence.pk)
        verify_evidence_pipeline(self.evidence.pk)
        self.assertEqual((Claim.objects.count(), ClaimEvidence.objects.count(), Score.objects.count()), before)

    def test_data_changes_use_existing_service_audits(self):
        verify_evidence_pipeline(self.evidence.pk)
        actions = list(AuditLog.objects.values_list("action", flat=True))
        self.assertEqual(actions.count("CLAIM_EVIDENCE_UPDATED"), 1)
        self.assertEqual(actions.count("CLAIM_STATUS_CHANGED"), 1)
        self.assertEqual(actions.count("EVENT_SCORE_INVALIDATED"), 1)
        self.assertEqual(actions.count("EVENT_SCORE_REFRESHED"), 1)

    def test_eligibility_step_is_read_only(self):
        result = verify_evidence_pipeline(self.evidence.pk)
        before = AuditLog.objects.count()
        verify_evidence_pipeline(self.evidence.pk)
        self.assertEqual(AuditLog.objects.count(), before)
        self.assertEqual(result.job_run.steps.get(step_name="check_eligibility").status, JobStatus.SUCCESS)

    def test_pipeline_does_not_call_llm_or_create_article(self):
        before = (ModelCall.objects.count(), Article.objects.count(), ArticleVersion.objects.count(), Review.objects.count())
        verify_evidence_pipeline(self.evidence.pk)
        after = (ModelCall.objects.count(), Article.objects.count(), ArticleVersion.objects.count(), Review.objects.count())
        self.assertEqual(before, after)

    def test_management_command_reports_result(self):
        output = StringIO()
        call_command("verify_evidence_pipeline", evidence=self.evidence.pk, stdout=output)
        self.assertIn('"status": "SUCCESS"', output.getvalue())

    def test_non_reviewable_evidence_is_rejected_without_job(self):
        self.evidence.stance = EvidenceStance.CONTRADICTS
        self.evidence.save(update_fields=["stance"])
        with self.assertRaises(EvidenceVerificationPipelineError):
            verify_evidence_pipeline(self.evidence.pk)
        self.assertFalse(JobRun.objects.filter(job_type=PIPELINE_JOB_TYPE).exists())


class EvidenceVerificationPipelineConcurrencyTests(VerificationPipelineFixtureMixin, TransactionTestCase):
    def setUp(self):
        self.make_fixture()

    def run_pipeline(self):
        close_old_connections()
        try:
            try:
                return verify_evidence_pipeline(self.evidence.pk)
            except EvidenceVerificationPipelineError as exc:
                return exc
        finally:
            connections.close_all()

    def test_concurrent_pipeline_runs_only_one_mutation_path(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.run_pipeline(), range(2)))
        self.evidence.refresh_from_db()
        self.claim.refresh_from_db()
        self.score.refresh_from_db()
        self.assertEqual(JobRun.objects.filter(job_type=PIPELINE_JOB_TYPE).count(), 1)
        self.assertEqual(self.evidence.stance, EvidenceStance.SUPPORTS)
        self.assertEqual(self.claim.verification_status, VerificationStatus.SUPPORTED)
        self.assertEqual(self.score.status, ScoreStatus.CALCULATED)
        self.assertEqual(AuditLog.objects.filter(action="CLAIM_EVIDENCE_UPDATED").count(), 1)
        self.assertTrue(any(not isinstance(result, Exception) for result in results))
