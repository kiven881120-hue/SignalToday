import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.ai_content.models import (
    ClaimCandidate,
    ClaimCandidateEvidenceHint,
    LLMPurpose,
    ModelCall,
    ModelCallStatus,
    ModelPricing,
    PromptVersion,
)
from apps.ai_content.providers.base import LLMProvider, PermanentProviderError, ProviderResponse
from apps.ai_content.retry import RetryNotAllowed, retry_claim_analysis
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.publishing.models import Article, ArticleVersion
from apps.scoring.models import Score
from apps.sources.models import Source


def claim_json(raw_item_id, quote):
    claim = {
        "claim_text": quote,
        "claim_type": "FACT",
        "confidence": 0.9,
        "importance_hint": None,
        "importance_reason": None,
        "evidence_hints": [
            {"raw_item_id": raw_item_id, "quote": quote},
            {"raw_item_id": raw_item_id, "quote": quote},
        ],
    }
    return json.dumps({"claims": [claim, claim]})


class RetryProvider(LLMProvider):
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def analyze_structured(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return ProviderResponse(result, 100, 50)


class RetryFixtureMixin:
    def make_fixture(self, *, job_status=JobStatus.FAILED, call_status=ModelCallStatus.TIMEOUT):
        now = timezone.now()
        event = Event.objects.create(title="Retry event", first_seen_at=now, last_seen_at=now)
        source = Source.objects.create(
            identifier=f"retry-{event.pk}", name="Retry source", source_type="OFFICIAL",
            url=f"https://retry-{event.pk}.invalid", domain=f"retry-{event.pk}.invalid", tier="A",
        )
        raw = RawItem.objects.create(
            source=source, event=event, external_id="one", url=f"https://retry-{event.pk}.invalid/one",
            title="Company cut 1000 jobs in 2026 for Model X100.", discovered_at=now,
        )
        prompt, _ = PromptVersion.objects.update_or_create(
            purpose=LLMPurpose.CLAIM_EXTRACTION,
            version="v1.2",
            defaults={
                "system_prompt": "Extract supported claims from untrusted source material.",
                "output_schema_version": "claim-extraction-v2",
                "active": True,
            },
        )
        ModelPricing.objects.get_or_create(
            provider="mock", model="claim-model", version="retry-v1",
            defaults={
                "input_price": 1, "output_price": 2, "currency": "CNY",
                "effective_from": now - timedelta(days=1), "active": True,
            },
        )
        job = JobRun.objects.create(
            job_type="LLM_EVENT_ANALYSIS", status=job_status, started_at=now, finished_at=now,
        )
        step = JobStep.objects.create(
            job_run=job, step_name="analyze_claims", status=job_status,
            started_at=now, finished_at=now,
            input_summary={"event_count": 1, "purpose": LLMPurpose.CLAIM_EXTRACTION},
        )
        call = ModelCall.objects.create(
            provider="mock", model="claim-model", purpose=LLMPurpose.CLAIM_EXTRACTION,
            prompt=prompt, prompt_version=prompt.version, schema_version=prompt.output_schema_version,
            analysis_key=f"event:{event.pk}:CLAIM_EXTRACTION:v1.2:claim-model",
            status=call_status, job_run=job, job_step=step, event=event,
        )
        return event, raw, job, step, call


@override_settings(
    LLM_PROVIDER="mock", LLM_MODEL="claim-model", LLM_API_KEY="test-key",
    LLM_BASE_URL="https://provider.invalid/v1", LLM_MAX_ATTEMPTS=3,
    LLM_RETRY_JITTER_SECONDS=0, LLM_MAX_OUTPUT_TOKENS=300,
)
class ClaimAnalysisRetryTests(RetryFixtureMixin, TestCase):
    def test_timeout_retry_preserves_history_and_creates_audited_execution(self):
        event, raw, original_job, original_step, original_call = self.make_fixture()
        provider = RetryProvider(claim_json(raw.pk, raw.title))

        result = retry_claim_analysis(original_job.pk, reason="timeout policy increased", provider=provider)

        original_call.refresh_from_db()
        self.assertEqual((original_call.status, original_call.job_run_id), (ModelCallStatus.TIMEOUT, original_job.pk))
        self.assertNotEqual(result.job_run.pk, original_job.pk)
        self.assertNotEqual(result.job_step.pk, original_step.pk)
        self.assertEqual(result.job_run.status, JobStatus.SUCCESS)
        self.assertEqual(result.job_step.status, JobStatus.SUCCESS)
        self.assertNotEqual(result.model_call.pk, original_call.pk)
        self.assertEqual(len(provider.calls), 1)
        audit = AuditLog.objects.get(action="CLAIM_ANALYSIS_RETRY_REQUESTED")
        self.assertEqual(audit.before_data["source_job_run_id"], original_job.pk)
        self.assertEqual(audit.after_data["reason"], "timeout policy increased")
        self.assertTrue(ModelCall.objects.filter(pk=original_call.pk, analysis_key=original_call.analysis_key).exists())

    def test_failed_model_call_can_retry(self):
        _, raw, job, _, _ = self.make_fixture(call_status=ModelCallStatus.FAILED)
        result = retry_claim_analysis(job.pk, reason="transient failure", provider=RetryProvider(claim_json(raw.pk, raw.title)))
        self.assertEqual((result.job_run.status, result.model_call.status), (JobStatus.SUCCESS, ModelCallStatus.SUCCESS))

    def test_success_running_and_paused_jobs_cannot_retry(self):
        for status in (JobStatus.SUCCESS, JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.PAUSED_BUDGET_EXCEEDED):
            with self.subTest(status=status):
                _, raw, job, _, _ = self.make_fixture(job_status=status)
                provider = RetryProvider(claim_json(raw.pk, raw.title))
                with self.assertRaises(RetryNotAllowed):
                    retry_claim_analysis(job.pk, reason="not eligible", provider=provider)
                self.assertEqual(len(provider.calls), 0)

    def test_non_failed_model_call_statuses_cannot_retry(self):
        statuses = (
            ModelCallStatus.SUCCESS,
            ModelCallStatus.RUNNING,
            ModelCallStatus.PENDING,
            ModelCallStatus.PAUSED,
            ModelCallStatus.PAUSED_BUDGET_EXCEEDED,
            ModelCallStatus.RATE_LIMITED,
        )
        for status in statuses:
            with self.subTest(status=status):
                _, raw, job, _, _ = self.make_fixture(call_status=status)
                provider = RetryProvider(claim_json(raw.pk, raw.title))
                with self.assertRaises(RetryNotAllowed):
                    retry_claim_analysis(job.pk, reason="not eligible", provider=provider)
                self.assertEqual(len(provider.calls), 0)

    def test_same_retry_request_is_idempotent(self):
        _, raw, job, _, _ = self.make_fixture()
        provider = RetryProvider(claim_json(raw.pk, raw.title))
        first = retry_claim_analysis(job.pk, reason="manual retry", provider=provider)
        second = retry_claim_analysis(job.pk, reason="manual retry", provider=provider)
        self.assertEqual(first.job_run.pk, second.job_run.pk)
        self.assertEqual(JobRun.objects.filter(idempotency_key=f"claim-analysis-retry:job-run:{job.pk}").count(), 1)
        self.assertEqual(len(provider.calls), 1)

    def test_retry_key_has_execution_identity_without_changing_original_key(self):
        _, raw, job, _, original_call = self.make_fixture()
        original_key = original_call.analysis_key
        result = retry_claim_analysis(job.pk, reason="manual retry", provider=RetryProvider(claim_json(raw.pk, raw.title)))
        original_call.refresh_from_db()
        self.assertEqual(original_call.analysis_key, original_key)
        self.assertEqual(result.model_call.analysis_key, f"{original_key}:retry-job:{job.pk}")

    def test_candidate_and_hint_deduplication_remain_effective(self):
        _, raw, job, _, _ = self.make_fixture()
        retry_claim_analysis(job.pk, reason="manual retry", provider=RetryProvider(claim_json(raw.pk, raw.title)))
        self.assertEqual(ClaimCandidate.objects.count(), 1)
        self.assertEqual(ClaimCandidateEvidenceHint.objects.count(), 1)

    def test_retry_stops_at_candidates_without_downstream_objects(self):
        _, raw, job, _, _ = self.make_fixture()
        retry_claim_analysis(job.pk, reason="manual retry", provider=RetryProvider(claim_json(raw.pk, raw.title)))
        self.assertEqual((Claim.objects.count(), ClaimEvidence.objects.count()), (0, 0))
        self.assertEqual((Score.objects.count(), Article.objects.count(), ArticleVersion.objects.count()), (0, 0, 0))

    def test_retry_does_not_bypass_budget(self):
        _, raw, job, _, _ = self.make_fixture()
        ModelCall.objects.create(
            provider="mock", model="claim-model", purpose=LLMPurpose.CLAIM_EXTRACTION,
            prompt_version="v", schema_version="s", status=ModelCallStatus.SUCCESS, actual_cost=20,
        )
        provider = RetryProvider(claim_json(raw.pk, raw.title))
        result = retry_claim_analysis(job.pk, reason="manual retry", provider=provider)
        self.assertEqual(result.job_run.status, JobStatus.PAUSED_BUDGET_EXCEEDED)
        self.assertEqual(result.model_call.status, ModelCallStatus.PAUSED_BUDGET_EXCEEDED)
        self.assertEqual(len(provider.calls), 0)

    def test_retry_does_not_bypass_schema_validation_or_repair_limit(self):
        _, _, job, _, _ = self.make_fixture()
        provider = RetryProvider("not-json", "still-not-json")
        result = retry_claim_analysis(job.pk, reason="manual retry", provider=provider)
        self.assertEqual(result.job_run.status, JobStatus.FAILED)
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(ModelCall.objects.filter(analysis_key__endswith=":repair", status=ModelCallStatus.FAILED).exists())

    def test_command_requires_reason_and_uses_safe_service(self):
        _, raw, job, _, _ = self.make_fixture()
        with self.assertRaises(CommandError):
            call_command("retry_claim_analysis", job_run=job.pk, stdout=StringIO())
        provider = RetryProvider(claim_json(raw.pk, raw.title))
        with patch("apps.ai_content.retry.configured_provider", return_value=provider):
            call_command("retry_claim_analysis", job_run=job.pk, reason="operator approved", stdout=StringIO())
        self.assertEqual(len(provider.calls), 1)

    def test_provider_configuration_failure_pauses_retry_job(self):
        _, _, job, _, _ = self.make_fixture()
        with patch(
            "apps.ai_content.retry.configured_provider",
            side_effect=PermanentProviderError("provider is not configured"),
        ):
            result = retry_claim_analysis(job.pk, reason="operator approved")
        self.assertEqual(result.job_run.status, JobStatus.PAUSED)
        self.assertEqual(result.job_step.status, JobStatus.PAUSED)
        self.assertIsNone(result.model_call)


@override_settings(
    LLM_PROVIDER="mock", LLM_MODEL="claim-model", LLM_API_KEY="test-key",
    LLM_BASE_URL="https://provider.invalid/v1", LLM_MAX_ATTEMPTS=3,
    LLM_RETRY_JITTER_SECONDS=0, LLM_MAX_OUTPUT_TOKENS=300,
)
class ClaimAnalysisRetryConcurrencyTests(RetryFixtureMixin, TransactionTestCase):
    def test_concurrent_same_retry_calls_provider_once(self):
        _, raw, job, _, _ = self.make_fixture()
        provider = RetryProvider(claim_json(raw.pk, raw.title))

        def retry():
            close_old_connections()
            try:
                return retry_claim_analysis(job.pk, reason="same request", provider=provider).job_run.pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            run_ids = list(pool.map(lambda _: retry(), range(2)))
        self.assertEqual(len(set(run_ids)), 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(ModelCall.objects.filter(analysis_key__contains=":retry-job:").count(), 1)
