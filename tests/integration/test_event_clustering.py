from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import close_old_connections, connections
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.events.admin import ClaimInline, EventAdmin, RawItemInline
from apps.events.clustering import ClusterAction
from apps.events.models import ClusterMethod, Event
from apps.events.service import EventClusteringService, cluster_raw_item, decide_cluster
from apps.ingestion.admin import RawItemAdmin
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun
from apps.sources.models import Source


class EventClusteringIntegrationTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.source_a = self.make_source("a", "group-a")
        self.source_b = self.make_source("b", "group-b")

    def make_source(self, identifier, group=""):
        return Source.objects.create(
            identifier=identifier,
            name=f"Source {identifier}",
            source_type="OFFICIAL",
            url=f"https://{identifier}.example.test/feed",
            domain=f"{identifier}.example.test",
            source_group=group,
        )

    def make_item(self, title, source=None, published_at=None):
        suffix = RawItem.objects.count() + 1
        return RawItem.objects.create(
            source=source or self.source_a,
            external_id=f"item-{suffix}",
            url=f"https://example.test/{suffix}",
            canonical_url=f"https://example.test/{suffix}",
            title=title,
            published_at=published_at,
            discovered_at=published_at or self.now,
            content_hash=f"hash-{suffix}",
        )

    def test_navigation_only_items_do_not_enter_event_clustering(self):
        for title in ("更多 >", "查看更多>>", "More"):
            item = self.make_item(title, published_at=self.now)
            result = cluster_raw_item(item)
            self.assertEqual(result.action, ClusterAction.NO_ACTION)
            self.assertEqual(result.reason, "navigation_only_title")
            self.assertIsNone(RawItem.objects.get(pk=item.pk).event_id)
        self.assertEqual(Event.objects.count(), 0)

    def test_real_headlines_containing_navigation_words_are_not_filtered(self):
        for title in ("更多财政政策细节发布", "More banks announce rate cuts"):
            item = self.make_item(title, published_at=self.now)
            result = cluster_raw_item(item)
            self.assertEqual(result.action, ClusterAction.CREATE_NEW)
            self.assertIsNotNone(RawItem.objects.get(pk=item.pk).event_id)

    def test_same_event_titles_assign_to_one_event(self):
        first = self.make_item("OpenAI发布新模型", published_at=self.now)
        second = self.make_item("OpenAI 发布新模型", source=self.source_b, published_at=self.now + timedelta(hours=1))
        first_result = cluster_raw_item(first)
        second_result = cluster_raw_item(second)
        self.assertEqual(first_result.action, ClusterAction.CREATE_NEW)
        self.assertEqual(second_result.action, ClusterAction.ASSIGN_EXISTING)
        self.assertEqual(RawItem.objects.get(pk=first.pk).event_id, RawItem.objects.get(pk=second.pk).event_id)
        self.assertEqual(Event.objects.count(), 1)
        event = Event.objects.get()
        self.assertEqual(event.title, "OpenAI发布新模型")
        self.assertEqual(event.first_seen_at, self.now)
        self.assertEqual(event.last_seen_at, self.now + timedelta(hours=1))
        audits = AuditLog.objects.filter(action="RAW_ITEM_EVENT_ASSIGNED").order_by("id")
        self.assertEqual(audits.count(), 2)
        self.assertEqual(audits.last().after_data["method"], ClusterMethod.EXACT_TITLE)

    def test_job_11_brics_speech_report_variants_assign_to_one_event(self):
        full_text = self.make_item(
            "习近平在金砖国家领导人第十八次会晤第一阶段会议的讲话（全文）",
            published_at=self.now,
        )
        news_report = self.make_item(
            "习近平出席金砖国家领导人第十八次会晤第一阶段会议并发表重要讲话",
            published_at=self.now - timedelta(days=1),
        )

        cluster_raw_item(full_text)
        result = cluster_raw_item(news_report)

        self.assertEqual(result.action, ClusterAction.ASSIGN_EXISTING)
        self.assertEqual(result.confidence, 0.95)
        self.assertEqual(Event.objects.count(), 1)

    def test_structural_speech_match_does_not_merge_different_meeting_stages(self):
        first_stage = self.make_item(
            "习近平在金砖国家领导人第十八次会晤第一阶段会议的讲话（全文）",
            published_at=self.now,
        )
        second_stage = self.make_item(
            "习近平出席金砖国家领导人第十八次会晤第二阶段会议并发表重要讲话",
            published_at=self.now - timedelta(days=1),
        )

        cluster_raw_item(first_stage)
        result = cluster_raw_item(second_stage)

        self.assertEqual(result.action, ClusterAction.CREATE_NEW)
        self.assertEqual(Event.objects.count(), 2)

    def test_similar_titles_with_conflicting_numbers_create_separate_events(self):
        cluster_raw_item(self.make_item("美联储降息25个基点", published_at=self.now))
        result = cluster_raw_item(self.make_item("美联储降息50个基点", source=self.source_b, published_at=self.now))
        self.assertEqual(result.action, ClusterAction.CREATE_NEW)
        self.assertEqual(Event.objects.count(), 2)

    def test_event97_banking_entities_are_not_auto_merged(self):
        first = self.make_item(
            "Federal Reserve Board announces approval of the application by Coastal Bend Bancshares, Inc.",
            published_at=self.now,
        )
        second = self.make_item(
            "Federal Reserve Board announces approval of the application by FS Bancorp, Inc.",
            source=self.source_b,
            published_at=self.now,
        )
        cluster_raw_item(first)
        result = cluster_raw_item(second)
        self.assertEqual(result.action, ClusterAction.CREATE_NEW)
        self.assertIn("different_named_entities", result.candidates[0].conflicts)
        self.assertEqual(Event.objects.count(), 2)

    def test_same_company_different_wording_can_merge_at_high_similarity(self):
        event = Event.objects.create(
            title="Acme Corp announces quarterly earnings",
            first_seen_at=self.now,
            last_seen_at=self.now,
        )
        event.trigram = 0.95
        item = self.make_item("Acme Corp reports quarterly earnings", published_at=self.now)
        with patch("apps.events.service.find_candidate_events", return_value=[event]):
            result = decide_cluster(item)
        self.assertEqual(result.action, ClusterAction.ASSIGN_EXISTING)

    def test_same_institution_different_wording_can_merge(self):
        event = Event.objects.create(
            title="European Space Agency announces Hera mission launch",
            first_seen_at=self.now,
            last_seen_at=self.now,
        )
        event.trigram = 0.95
        item = self.make_item("European Space Agency confirms Hera mission launch", published_at=self.now)
        with patch("apps.events.service.find_candidate_events", return_value=[event]):
            result = decide_cluster(item)
        self.assertEqual(result.action, ClusterAction.ASSIGN_EXISTING)

    def test_different_companies_with_high_similarity_do_not_auto_merge(self):
        event = Event.objects.create(
            title="Acme Corp announces a strategic cloud partnership",
            first_seen_at=self.now,
            last_seen_at=self.now,
        )
        event.trigram = 0.99
        item = self.make_item("Globex Corp announces a strategic cloud partnership", published_at=self.now)
        with patch("apps.events.service.find_candidate_events", return_value=[event]):
            result = decide_cluster(item)
        self.assertEqual(result.action, ClusterAction.CREATE_NEW)

    def test_different_named_products_do_not_auto_merge(self):
        event = Event.objects.create(
            title="Apple unveils iPhone Air",
            first_seen_at=self.now,
            last_seen_at=self.now,
        )
        event.trigram = 0.99
        item = self.make_item("Apple unveils iPhone Pro", published_at=self.now)
        with patch("apps.events.service.find_candidate_events", return_value=[event]):
            result = decide_cluster(item)
        self.assertEqual(result.action, ClusterAction.CREATE_NEW)

    def test_different_explicit_regions_do_not_auto_merge(self):
        event = Event.objects.create(
            title="Agency announces factory investment in France",
            first_seen_at=self.now,
            last_seen_at=self.now,
        )
        event.trigram = 0.99
        item = self.make_item("Agency announces factory investment in Germany", published_at=self.now)
        with patch("apps.events.service.find_candidate_events", return_value=[event]):
            result = decide_cluster(item)
        self.assertEqual(result.action, ClusterAction.CREATE_NEW)

    def test_different_year_country_company_and_model_do_not_merge(self):
        cases = [
            ("苹果发布2026年新品", "苹果发布2025年新品"),
            ("中国央行宣布政策调整", "美国央行宣布政策调整"),
            ("OpenAI发布新模型", "Google发布新模型"),
            ("iPhone 17 Pro正式发布", "iPhone 18 Pro正式发布"),
        ]
        for index, (left, right) in enumerate(cases):
            with self.subTest(left=left):
                Event.objects.all().delete()
                RawItem.objects.all().delete()
                first = self.make_item(left, published_at=self.now + timedelta(minutes=index))
                second = self.make_item(right, source=self.source_b, published_at=self.now + timedelta(minutes=index))
                cluster_raw_item(first)
                cluster_raw_item(second)
                self.assertEqual(Event.objects.count(), 2)

    def test_time_window_allows_72_hours_but_not_beyond(self):
        cluster_raw_item(self.make_item("OpenAI发布新模型", published_at=self.now))
        within = self.make_item("OpenAI 发布新模型", source=self.source_b, published_at=self.now + timedelta(hours=71))
        self.assertEqual(cluster_raw_item(within).action, ClusterAction.ASSIGN_EXISTING)
        outside = self.make_item("OpenAI发布新模型", published_at=self.now + timedelta(hours=73))
        self.assertEqual(cluster_raw_item(outside).action, ClusterAction.CREATE_NEW)

    def test_missing_published_time_falls_back_to_discovered_time(self):
        first = self.make_item("OpenAI发布新模型")
        second = self.make_item("OpenAI 发布新模型", source=self.source_b)
        cluster_raw_item(first)
        self.assertEqual(cluster_raw_item(second).action, ClusterAction.ASSIGN_EXISTING)

    def test_multi_source_statistics_distinguish_sources_and_groups(self):
        source_c = self.make_source("c", "group-a")
        event = Event.objects.create(title="Event", first_seen_at=self.now, last_seen_at=self.now)
        for index, source in enumerate((self.source_a, self.source_b, source_c), start=1):
            item = self.make_item(f"Report {index}", source=source)
            item.event = event
            item.save(update_fields=["event"])
        self.assertEqual(event.raw_item_count, 3)
        self.assertEqual(event.unique_source_count, 3)
        self.assertEqual(event.independent_source_count, 2)

    def test_three_independent_sources_cluster_into_one_event(self):
        source_c = self.make_source("c", "group-c")
        items = [
            self.make_item("OpenAI发布新模型", source=source, published_at=self.now)
            for source in (self.source_a, self.source_b, source_c)
        ]
        for item in items:
            cluster_raw_item(item)
        self.assertEqual(Event.objects.count(), 1)
        event = Event.objects.get()
        self.assertEqual(event.raw_item_count, 3)
        self.assertEqual(event.unique_source_count, 3)
        self.assertEqual(event.independent_source_count, 3)

    def test_same_group_feeds_cluster_but_count_once_as_independent(self):
        source_c = self.make_source("c", "group-a")
        items = [
            self.make_item("OpenAI发布新模型", source=source, published_at=self.now)
            for source in (self.source_a, self.source_b, source_c)
        ]
        for item in items:
            cluster_raw_item(item)
        event = Event.objects.get()
        self.assertEqual(event.raw_item_count, 3)
        self.assertEqual(event.unique_source_count, 3)
        self.assertEqual(event.independent_source_count, 2)

    def test_same_source_multiple_items_count_as_one_source(self):
        event = Event.objects.create(title="Event", first_seen_at=self.now, last_seen_at=self.now)
        for index in range(3):
            item = self.make_item(f"Report {index}")
            item.event = event
            item.save(update_fields=["event"])
        self.assertEqual(event.raw_item_count, 3)
        self.assertEqual(event.unique_source_count, 1)

    def test_ungrouped_sources_are_independent(self):
        source_c = self.make_source("c")
        source_d = self.make_source("d")
        event = Event.objects.create(title="Event", first_seen_at=self.now, last_seen_at=self.now)
        for source in (source_c, source_d):
            item = self.make_item(f"Report from {source.identifier}", source=source)
            item.event = event
            item.save(update_fields=["event"])
        self.assertEqual(event.independent_source_count, 2)

    def test_medium_confidence_candidate_requires_manual_review(self):
        event = Event.objects.create(
            title="OpenAI unveils model",
            first_seen_at=self.now,
            last_seen_at=self.now,
        )
        event.trigram = 0.78
        item = self.make_item("OpenAI launches model")
        with patch("apps.events.service.find_candidate_events", return_value=[event]):
            result = decide_cluster(item)
        self.assertEqual(result.action, ClusterAction.MANUAL_REVIEW)
        self.assertIsNone(item.event_id)

    def test_dry_run_makes_no_database_changes(self):
        self.make_item("Dry run event")
        before = (Event.objects.count(), JobRun.objects.count(), AuditLog.objects.count())
        output = StringIO()
        call_command("cluster_events", dry_run=True, limit=10, stdout=output)
        self.assertEqual((Event.objects.count(), JobRun.objects.count(), AuditLog.objects.count()), before)
        self.assertIn('"action": "CREATE_NEW"', output.getvalue())

    def test_formal_batch_records_event_audit_job_and_step(self):
        item = self.make_item("Formal event")
        call_command("cluster_events", limit=10, stdout=StringIO())
        item.refresh_from_db()
        run = JobRun.objects.get(job_type="EVENT_CLUSTER")
        self.assertIsNotNone(item.event_id)
        self.assertEqual(run.status, JobStatus.SUCCESS)
        self.assertEqual(run.steps.get().output_summary["new_events"], 1)
        audit = AuditLog.objects.get(object_type="RawItem", object_id=str(item.pk))
        self.assertEqual(audit.after_data["job_run_id"], run.pk)
        self.assertEqual(audit.after_data["new_event_id"], item.event_id)
        self.assertIn("timestamp", audit.after_data)

    def test_repeated_batch_is_idempotent(self):
        self.make_item("Idempotent event")
        call_command("cluster_events", limit=10, stdout=StringIO())
        call_command("cluster_events", limit=10, stdout=StringIO())
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_command_honors_source_and_limit(self):
        self.make_item("First source item")
        self.make_item("Second source item")
        self.make_item("Other source item", source=self.source_b)
        call_command("cluster_events", source="a", limit=1, stdout=StringIO())
        self.assertEqual(RawItem.objects.filter(event__isnull=False).count(), 1)
        self.assertEqual(RawItem.objects.filter(source=self.source_b, event__isnull=True).count(), 1)

    def test_empty_title_is_skipped_safely(self):
        item = self.make_item("")
        result = cluster_raw_item(item)
        self.assertEqual(result.action, ClusterAction.NO_ACTION)
        self.assertIsNone(RawItem.objects.get(pk=item.pk).event_id)

    def test_pg_trgm_failure_causes_no_action(self):
        item = self.make_item("Candidate query failure")
        with patch("apps.events.service.find_candidate_events", return_value=None):
            result = decide_cluster(item)
        self.assertEqual(result.action, ClusterAction.NO_ACTION)

    def test_assigned_item_is_not_reassigned_by_concurrent_style_retry(self):
        item = self.make_item("One assignment")
        first = cluster_raw_item(item)
        second = cluster_raw_item(item)
        self.assertEqual(first.action, ClusterAction.CREATE_NEW)
        self.assertEqual(second.action, ClusterAction.NO_ACTION)
        self.assertEqual(Event.objects.count(), 1)

    def test_event_delete_sets_raw_item_event_to_null(self):
        item = self.make_item("Disposable event")
        event = cluster_raw_item(item).event
        event.delete()
        item.refresh_from_db()
        self.assertIsNone(item.event_id)

    def test_admin_manual_reassignment_is_audited(self):
        first_event = Event.objects.create(title="First", first_seen_at=self.now, last_seen_at=self.now)
        second_event = Event.objects.create(title="Second", first_seen_at=self.now, last_seen_at=self.now)
        item = self.make_item("Manual")
        item.event = first_event
        item.save(update_fields=["event"])
        changed = RawItem.objects.get(pk=item.pk)
        changed.event = second_event
        request = RequestFactory().post("/admin/")
        request.user = get_user_model().objects.create_user(username="editor")
        RawItemAdmin(RawItem, admin.site).save_model(request, changed, form=None, change=True)
        audit = AuditLog.objects.get(object_id=str(item.pk))
        self.assertEqual(audit.before_data["event_id"], first_event.pk)
        self.assertEqual(audit.after_data["new_event_id"], second_event.pk)
        self.assertEqual(audit.after_data["method"], ClusterMethod.MANUAL)

    def test_event_admin_exposes_linked_items_and_counts(self):
        event_admin = EventAdmin(Event, admin.site)
        self.assertIn("raw_items_total", event_admin.list_display)
        self.assertEqual(event_admin.inlines, (RawItemInline, ClaimInline))


class EventClusteringConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_matching_items_converge_on_one_event(self):
        now = timezone.now()
        source = Source.objects.create(
            identifier="concurrent",
            name="Concurrent Source",
            source_type="OFFICIAL",
            url="https://concurrent.example.test/feed",
            domain="concurrent.example.test",
        )
        items = [
            RawItem.objects.create(
                source=source,
                external_id=f"concurrent-{index}",
                url=f"https://example.test/concurrent-{index}",
                canonical_url=f"https://example.test/concurrent-{index}",
                title="OpenAI发布新模型" if index == 1 else "OpenAI 发布新模型",
                discovered_at=now,
                content_hash=f"concurrent-hash-{index}",
            )
            for index in (1, 2)
        ]

        def run(item_id):
            close_old_connections()
            try:
                return cluster_raw_item(RawItem.objects.get(pk=item_id)).action
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            actions = list(executor.map(run, [item.pk for item in items]))
        self.assertCountEqual(actions, [ClusterAction.CREATE_NEW, ClusterAction.ASSIGN_EXISTING])
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(RawItem.objects.filter(event__isnull=False).count(), 2)
