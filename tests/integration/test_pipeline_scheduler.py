import json
from datetime import date, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.ai_content.costs import BudgetDecision
from apps.ai_content.models import ModelCallStatus
from apps.events.models import Event
from apps.ingestion.errors import ErrorCode, IngestionError
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.jobs.operations import get_operations_status
from apps.jobs.pipeline import (
    StageOutcome,
    TransientStageError,
    execute_pipeline_stage,
    plan_pipeline,
)
from apps.jobs.pipeline_registry import (
    PIPELINE_REGISTRY,
    PIPELINE_STAGES,
    PIPELINE_TIMEZONE,
    validate_registry,
)
from apps.jobs.pipeline_stages import (
    _budget_gate,
    article_generation,
    article_review_gate,
    daily_digest,
    public_health_check,
    publish_gate,
    source_ingestion,
)
from apps.publishing.models import (
    Article,
    ArticleStatus,
    ArticleVersion,
    Digest,
    DigestCategory,
    DigestItem,
    DigestStatus,
)
from apps.publishing.generation import ArticleGenerationError
from apps.reviews.models import Review, ReviewDecision
from apps.sources.models import Source


BUSINESS_DATE = date(2026, 9, 12)


def successful_dependencies(stage_name, business_date=BUSINESS_DATE):
    for dependency in PIPELINE_REGISTRY[stage_name].dependencies:
        stage = PIPELINE_REGISTRY[dependency]
        JobRun.objects.create(
            job_type=stage.job_type,
            idempotency_key=stage.idempotency_key(business_date),
            status=JobStatus.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )


class PipelineRegistryTests(TestCase):
    def test_registry_has_order_timezone_schedules_and_policies(self):
        self.assertTrue(validate_registry())
        self.assertEqual(PIPELINE_TIMEZONE, "Asia/Shanghai")
        self.assertEqual(
            [stage.name for stage in PIPELINE_STAGES],
            [
                "source_ingestion", "event_clustering", "claim_analysis",
                "claim_evaluation", "importance_review", "scoring",
                "article_generation", "article_validation", "article_review_gate",
                "publish_gate", "daily_digest", "public_health_check",
            ],
        )
        self.assertEqual(PIPELINE_REGISTRY["source_ingestion"].schedule.strftime("%H:%M"), "05:30")
        self.assertEqual(PIPELINE_REGISTRY["daily_digest"].schedule.strftime("%H:%M"), "08:00")
        self.assertEqual(PIPELINE_REGISTRY["public_health_check"].schedule.strftime("%H:%M"), "08:05")
        self.assertEqual(PIPELINE_REGISTRY["article_review_gate"].schedule.strftime("%H:%M"), "07:44")
        self.assertEqual(PIPELINE_REGISTRY["claim_analysis"].retry_policy.max_attempts, 3)
        self.assertEqual(PIPELINE_REGISTRY["article_review_gate"].mode, "MANUAL_GATE")

    @override_settings(SCHEDULER_PIPELINE_ENABLED=False)
    def test_disabled_by_default_and_dry_run_has_no_side_effects(self):
        before = (JobRun.objects.count(), JobStep.objects.count(), Digest.objects.count())
        output = StringIO()
        call_command(
            "run_scheduler", once=True, dry_run=True,
            date=BUSINESS_DATE.isoformat(), at="2026-09-12T08:10:00+08:00",
            stdout=output,
        )
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["pipeline_enabled"])
        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertEqual(before, (JobRun.objects.count(), JobStep.objects.count(), Digest.objects.count()))
        self.assertEqual(payload["stages"][0]["action"], "WOULD_RUN")
        self.assertEqual(payload["stages"][1]["action"], "BLOCKED")

    @override_settings(SCHEDULER_PIPELINE_ENABLED=False)
    def test_disabled_execution_writes_nothing(self):
        result = execute_pipeline_stage("source_ingestion", BUSINESS_DATE, executor=Mock())
        self.assertEqual(result.status, "DISABLED")
        self.assertEqual(JobRun.objects.count(), 0)

    def test_plan_checks_schedule_dependencies_and_idempotency(self):
        before = datetime(2026, 9, 12, 5, 29, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(plan_pipeline(BUSINESS_DATE, now=before)[0]["action"], "NOT_DUE")
        stage = PIPELINE_REGISTRY["source_ingestion"]
        JobRun.objects.create(
            job_type=stage.job_type,
            idempotency_key=stage.idempotency_key(BUSINESS_DATE),
            status=JobStatus.SUCCESS,
        )
        after = datetime(2026, 9, 12, 5, 46, tzinfo=ZoneInfo("Asia/Shanghai"))
        plan = {item["name"]: item for item in plan_pipeline(BUSINESS_DATE, now=after)}
        self.assertEqual(plan["source_ingestion"]["action"], "IDEMPOTENT_NOOP")
        self.assertEqual(plan["event_clustering"]["action"], "WOULD_RUN")


@override_settings(SCHEDULER_PIPELINE_ENABLED=True)
class PipelineExecutionTests(TestCase):
    def test_idempotent_stage_has_one_effective_result(self):
        executor = Mock(return_value=StageOutcome(JobStatus.SUCCESS, 2, 1, {"ok": True}))
        first = execute_pipeline_stage("source_ingestion", BUSINESS_DATE, executor=executor)
        second = execute_pipeline_stage("source_ingestion", BUSINESS_DATE, executor=executor)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(executor.call_count, 1)
        self.assertEqual(JobRun.objects.count(), 1)
        step = JobStep.objects.get()
        self.assertEqual(step.input_summary["business_date"], BUSINESS_DATE.isoformat())
        self.assertEqual(step.output_summary["input_count"], 2)
        self.assertEqual(step.output_summary["output_count"], 1)

    def test_stage_failure_blocks_downstream(self):
        result = execute_pipeline_stage("scoring", BUSINESS_DATE, executor=Mock())
        self.assertEqual(result.status, JobStatus.PAUSED)
        self.assertIn("importance_review", result.reason)
        self.assertEqual(PIPELINE_REGISTRY["article_generation"].dependencies, ("scoring",))

    def test_transient_retry_is_bounded_and_audited(self):
        executor = Mock(side_effect=[
            TransientStageError("timeout", code="TIMEOUT"),
            TransientStageError("retry", code="TIMEOUT"),
            StageOutcome(JobStatus.SUCCESS, 1, 1),
        ])
        sleeps = []
        result = execute_pipeline_stage(
            "source_ingestion", BUSINESS_DATE, executor=executor,
            sleeper=sleeps.append, random_value=lambda: 0,
        )
        self.assertEqual(result.status, JobStatus.SUCCESS)
        self.assertEqual(executor.call_count, 3)
        self.assertEqual(JobStep.objects.count(), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_permanent_error_is_not_retried(self):
        executor = Mock(side_effect=RuntimeError("schema incompatible"))
        result = execute_pipeline_stage("source_ingestion", BUSINESS_DATE, executor=executor)
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(executor.call_count, 1)
        self.assertEqual(JobStep.objects.count(), 1)

    @patch("apps.jobs.pipeline._try_lock", return_value=False)
    def test_advisory_lock_prevents_second_owner(self, _lock):
        result = execute_pipeline_stage("source_ingestion", BUSINESS_DATE, executor=Mock())
        self.assertEqual(result.status, "LOCKED")
        self.assertEqual(JobRun.objects.count(), 0)

    def test_digest_is_blocked_before_publish_stage(self):
        result = execute_pipeline_stage("daily_digest", BUSINESS_DATE, executor=Mock())
        self.assertEqual(result.status, JobStatus.PAUSED)
        self.assertIn("publish_gate", result.reason)


class PipelineIsolationAndGateTests(TestCase):
    def make_source(self, key):
        return Source.objects.create(
            identifier=key,
            name=key,
            source_type="OFFICIAL",
            url=f"https://{key}.example.com/feed.xml",
            domain=f"{key}.example.com",
            tier="A",
            active=True,
            crawl_policy={"feed_only": True},
        )

    def make_article(self, status):
        now = timezone.now()
        event = Event.objects.create(title="gate", first_seen_at=now, last_seen_at=now)
        article = Article.objects.create(event=event, status=status)
        version = ArticleVersion.objects.create(
            article=article,
            version_number=1,
            title="title",
            one_sentence_summary="summary",
            what_happened="fact",
            status=status,
        )
        article.current_version = version
        article.save(update_fields=["current_version", "updated_at"])
        return article, version

    @patch("apps.jobs.pipeline_stages.FeedIngestionService.ingest_source")
    def test_source_failure_is_isolated(self, ingest):
        first = self.make_source("first")
        self.make_source("second")
        stats = SimpleNamespace(as_dict=lambda: {"entries": 1, "created": 1, "duplicates": 0, "failed": 0})
        ingest.side_effect = [
            (SimpleNamespace(status=JobStatus.SUCCESS), stats),
            IngestionError(ErrorCode.TIMEOUT, "timeout"),
        ]
        result = source_ingestion(BUSINESS_DATE)
        self.assertEqual(result.status, JobStatus.SUCCESS)
        self.assertEqual(result.output_count, 1)
        self.assertEqual(result.metadata["failed_source_count"], 1)
        self.assertEqual(ingest.call_count, 2)
        self.assertEqual(ingest.call_args_list[0].args[0], first)

    @patch("apps.jobs.pipeline_stages.generate_article")
    @patch("apps.jobs.pipeline_stages.evaluate_article_generation_eligibility")
    def test_candidate_failure_is_isolated(self, eligibility, generate):
        now = timezone.now()
        Event.objects.create(title="one", first_seen_at=now, last_seen_at=now)
        Event.objects.create(title="two", first_seen_at=now, last_seen_at=now)
        eligibility.return_value = SimpleNamespace(eligible=True)
        generate.side_effect = [
            SimpleNamespace(article=object(), reused=False, job_run=SimpleNamespace(status=JobStatus.SUCCESS)),
            ArticleGenerationError("bad candidate"),
        ]
        with patch("apps.jobs.pipeline_stages._budget_gate", return_value=None):
            result = article_generation(BUSINESS_DATE)
        self.assertEqual(result.status, JobStatus.SUCCESS)
        self.assertEqual(result.output_count, 1)
        self.assertEqual(result.metadata["candidate_failure_count"], 1)

    def test_manual_review_and_publish_authorization_are_gates_only(self):
        article, version = self.make_article(ArticleStatus.IN_REVIEW)
        Review.objects.create(article_version=version, decision=ReviewDecision.PENDING)
        review_result = article_review_gate(BUSINESS_DATE)
        self.assertEqual(review_result.status, JobStatus.SUCCESS)
        self.assertEqual(review_result.metadata["scheduler_approved_count"], 0)
        self.assertEqual(review_result.metadata["deferred_for_human_review"], 1)
        self.assertEqual(article.status, ArticleStatus.IN_REVIEW)
        Article.objects.filter(pk=article.pk).update(status=ArticleStatus.APPROVED)
        ArticleVersion.objects.filter(pk=version.pk).update(status=ArticleStatus.APPROVED)
        Review.objects.filter(article_version=version).update(decision=ReviewDecision.APPROVED)
        publish_result = publish_gate(BUSINESS_DATE)
        self.assertEqual(publish_result.status, JobStatus.SUCCESS)
        self.assertEqual(publish_result.metadata["scheduler_published_count"], 0)
        self.assertEqual(publish_result.metadata["deferred_for_human_authorization"], 1)
        article.refresh_from_db()
        self.assertEqual(article.status, ArticleStatus.APPROVED)
        self.assertIsNone(article.published_version_id)

    @patch("apps.jobs.pipeline_stages.check_budget")
    def test_budget_80_percent_pauses_low_priority_llm(self, check):
        check.return_value = BudgetDecision(False, True, ModelCallStatus.PAUSED, 16, 16)
        self.assertEqual(_budget_gate().status, JobStatus.PAUSED)

    @patch("apps.jobs.pipeline_stages.check_budget")
    def test_budget_100_percent_uses_budget_paused_status(self, check):
        check.return_value = BudgetDecision(
            False, True, ModelCallStatus.PAUSED_BUDGET_EXCEEDED, 20, 20
        )
        self.assertEqual(_budget_gate().status, JobStatus.PAUSED_BUDGET_EXCEEDED)

    def test_zero_item_digest_is_valid_after_healthy_upstream(self):
        result = daily_digest(BUSINESS_DATE)
        digest = Digest.objects.get(digest_date=BUSINESS_DATE)
        self.assertEqual(result.status, JobStatus.SUCCESS)
        self.assertEqual(digest.status, DigestStatus.READY)
        self.assertEqual(digest.items.count(), 0)
        self.assertTrue(result.metadata["dry_run_first"])

    def test_ready_digest_is_never_rebuilt(self):
        digest = Digest.objects.create(digest_date=BUSINESS_DATE, status=DigestStatus.READY)
        before = (digest.pk, Digest.objects.count(), DigestItem.objects.count())
        result = daily_digest(BUSINESS_DATE)
        self.assertFalse(result.metadata["changed"])
        self.assertEqual(before, (digest.pk, Digest.objects.count(), DigestItem.objects.count()))

    @patch("apps.jobs.pipeline_stages.generate_daily_digest")
    def test_digest_always_runs_dry_run_before_formal_generation(self, generate):
        generate.side_effect = [
            SimpleNamespace(candidates=(), selected=()),
            SimpleNamespace(
                digest=SimpleNamespace(pk=9), selected=(), changed=True
            ),
        ]
        result = daily_digest(BUSINESS_DATE)
        self.assertEqual(result.status, JobStatus.SUCCESS)
        self.assertEqual(generate.call_args_list[0].kwargs, {"dry_run": True, "rebuild": False})
        self.assertEqual(generate.call_args_list[1].kwargs, {"rebuild": False})

    def test_historical_digest_version_remains_pinned_after_version_five(self):
        article, version4 = self.make_article(ArticleStatus.PUBLISHED)
        article.published_version = version4
        article.published_at = timezone.now()
        article.save(update_fields=["published_version", "published_at", "updated_at"])
        digest = Digest.objects.create(digest_date=BUSINESS_DATE, status=DigestStatus.READY)
        item = DigestItem.objects.create(
            digest=digest,
            article=article,
            article_version=version4,
            category=DigestCategory.DISCOVER,
            rank=1,
            recommendation_reason="reason",
        )
        version5 = ArticleVersion.objects.create(
            article=article,
            version_number=5,
            title="version five",
            one_sentence_summary="summary",
            what_happened="fact",
            status=ArticleStatus.PUBLISHED,
        )
        ArticleVersion.objects.filter(pk=version4.pk).update(status=ArticleStatus.CORRECTED)
        Article.objects.filter(pk=article.pk).update(current_version=version5, published_version=version5)
        result = daily_digest(BUSINESS_DATE)
        item.refresh_from_db()
        self.assertFalse(result.metadata["changed"])
        self.assertEqual(item.article_version_id, version4.pk)

    @patch("apps.jobs.pipeline_stages.urlopen")
    def test_public_web_health_check(self, open_url):
        response = Mock()
        response.__enter__ = Mock(return_value=SimpleNamespace(status=200))
        response.__exit__ = Mock(return_value=False)
        open_url.return_value = response
        result = public_health_check(BUSINESS_DATE)
        self.assertEqual(result.status, JobStatus.SUCCESS)
        self.assertEqual(result.output_count, 4)


class OperationsStatusTests(TestCase):
    def test_status_is_read_only_and_detects_stale_job(self):
        stage = PIPELINE_REGISTRY["source_ingestion"]
        JobRun.objects.create(
            job_type=stage.job_type,
            idempotency_key=stage.idempotency_key(BUSINESS_DATE),
            status=JobStatus.RUNNING,
            started_at=timezone.now() - timedelta(hours=1),
        )
        before = (JobRun.objects.count(), JobStep.objects.count(), Digest.objects.count())
        payload = get_operations_status(business_date=BUSINESS_DATE)
        after = (JobRun.objects.count(), JobStep.objects.count(), Digest.objects.count())
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["pipeline_enabled"])
        source_stage = next(item for item in payload["pipeline"] if item["stage"] == "source_ingestion")
        self.assertEqual(source_stage["stale_running"], 1)
        self.assertEqual(before, after)

    def test_management_command_returns_json_without_writes(self):
        output = StringIO()
        before = JobRun.objects.count()
        call_command("show_operations_status", date=BUSINESS_DATE.isoformat(), stdout=output)
        self.assertTrue(json.loads(output.getvalue())["read_only"])
        self.assertEqual(JobRun.objects.count(), before)


@override_settings(SCHEDULER_PIPELINE_ENABLED=True)
class PipelineConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_stage_execution_has_one_owner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        entered = Event()
        release = Event()

        def executor(_business_date):
            entered.set()
            release.wait(5)
            return StageOutcome(JobStatus.SUCCESS, 1, 1)

        def invoke():
            close_old_connections()
            try:
                return execute_pipeline_stage(
                    "source_ingestion", BUSINESS_DATE, executor=executor
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke)
            self.assertTrue(entered.wait(5))
            second = pool.submit(invoke)
            second_result = second.result(timeout=5)
            release.set()
            first_result = first.result(timeout=5)
        self.assertEqual({first_result.status, second_result.status}, {JobStatus.SUCCESS, "LOCKED"})
        self.assertEqual(JobRun.objects.count(), 1)
