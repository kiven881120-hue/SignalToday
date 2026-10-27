from concurrent.futures import ThreadPoolExecutor
from io import StringIO

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connections
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.admin import ClaimAdmin, ClaimEvidenceAdmin, ClaimEvidenceInline
from apps.claims.evaluation import ClaimEvaluationService, evaluate_claim
from apps.claims.models import (
    Claim, ClaimEvidence, ClaimType, EvidenceStance, EvidenceType, VerificationStatus,
)
from apps.events.admin import ClaimInline, EventAdmin
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun
from apps.sources.models import Source


class ClaimEvidenceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.event = self.make_event("Rate decision")
        self.source = self.make_source("wire", "B", "wire-group")
        self.raw = self.make_raw(self.source, self.event, "wire-item")
        self.claim = Claim.objects.create(event=self.event, statement=" Bank cuts rate by 25 basis points. ")

    def make_event(self, title):
        return Event.objects.create(title=title, first_seen_at=self.now, last_seen_at=self.now)

    def make_source(self, key, tier="B", group=""):
        return Source.objects.create(
            identifier=key, name=key, source_type="MEDIA", url=f"https://{key}.test/feed",
            domain=f"{key}.test", tier=tier, source_group=group,
        )

    def make_raw(self, source, event, key):
        return RawItem.objects.create(
            source=source, event=event, external_id=key, url=f"https://{source.domain}/{key}",
            title=key, discovered_at=self.now,
        )

    def make_evidence(self, source=None, raw=None, stance=EvidenceStance.SUPPORTS, text="The rate was cut by 25 basis points.", evidence_type=""):
        source = source or self.source
        raw = raw or self.raw
        return ClaimEvidence.objects.create(
            claim=self.claim, raw_item=raw, source=source, evidence_text=text,
            stance=stance, evidence_type=evidence_type, captured_at=self.now,
        )

    def test_claim_creation_and_required_event(self):
        self.assertEqual(self.claim.event, self.event)
        with self.assertRaises(ValidationError):
            Claim.objects.create(statement="No event")

    def test_empty_claim_is_rejected(self):
        with self.assertRaises(ValidationError):
            Claim.objects.create(event=self.event, statement=" \t ")

    def test_claim_normalization_handles_unicode_entities_and_case(self):
        claim = Claim.objects.create(event=self.event, statement="Ａ &amp;   B")
        self.assertEqual(claim.normalized_statement, "a & b")

    def test_duplicate_normalized_claim_in_same_event_is_rejected(self):
        with self.assertRaises(ValidationError):
            Claim.objects.create(event=self.event, statement="BANK CUTS RATE BY 25 BASIS POINTS.")

    def test_same_claim_text_is_allowed_for_different_event(self):
        other = Claim.objects.create(event=self.make_event("Other"), statement=self.claim.statement)
        self.assertNotEqual(other.event_id, self.claim.event_id)

    def test_all_required_claim_types_exist(self):
        required = {"FACT", "DATE", "NUMBER", "QUOTE", "LOCATION", "PERSON", "ORGANIZATION", "PRODUCT", "IMPACT"}
        self.assertTrue(required.issubset(set(ClaimType.values)))

    def test_evidence_creation_normalizes_and_maps_source_tier(self):
        evidence = self.make_evidence(text="  The&nbsp;rate   was cut. ")
        self.assertEqual(evidence.normalized_evidence_text, "The rate was cut.")
        self.assertEqual(evidence.evidence_type, EvidenceType.NEWS_REPORT)
        self.assertEqual(evidence.source, self.raw.source)

    def test_manual_evidence_type_is_not_overwritten(self):
        evidence = self.make_evidence(evidence_type=EvidenceType.CONTEXT)
        self.assertEqual(evidence.evidence_type, EvidenceType.CONTEXT)

    def test_empty_evidence_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.make_evidence(text="  ")

    def test_evidence_over_2000_characters_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.make_evidence(text="x" * 2001)

    def test_evidence_requires_raw_item(self):
        with self.assertRaises(ValidationError):
            ClaimEvidence.objects.create(claim=self.claim, source=self.source, evidence_text="text", captured_at=self.now)

    def test_unassigned_raw_item_is_rejected(self):
        raw = self.make_raw(self.source, None, "unassigned")
        with self.assertRaises(ValidationError):
            self.make_evidence(raw=raw)

    def test_cross_event_evidence_is_rejected_with_clear_message(self):
        raw = self.make_raw(self.source, self.make_event("Different"), "different")
        with self.assertRaisesMessage(ValidationError, "Claim 与 Evidence 的 RawItem 必须属于同一个 Event。"):
            self.make_evidence(raw=raw)

    def test_source_must_match_raw_item(self):
        other = self.make_source("other", "B", "other")
        with self.assertRaises(ValidationError):
            self.make_evidence(source=other)

    def test_duplicate_normalized_evidence_is_rejected(self):
        self.make_evidence(text="Same  evidence")
        with self.assertRaises(ValidationError):
            self.make_evidence(text=" Same evidence ")

    def test_same_evidence_text_on_different_raw_item_is_allowed(self):
        self.make_evidence(text="Same evidence")
        other_raw = self.make_raw(self.source, self.event, "other-raw")
        self.make_evidence(raw=other_raw, text="Same evidence")
        self.assertEqual(ClaimEvidence.objects.count(), 2)

    def test_same_group_counts_as_one_independent_source(self):
        other = self.make_source("wire2", "B", "wire-group")
        self.make_evidence()
        self.make_evidence(source=other, raw=self.make_raw(other, self.event, "wire2-item"), text="Second report")
        self.assertEqual(evaluate_claim(self.claim).independent_supporting_source_count, 1)

    def test_different_groups_count_as_two_independent_sources(self):
        other = self.make_source("paper", "B", "paper-group")
        self.make_evidence()
        self.make_evidence(source=other, raw=self.make_raw(other, self.event, "paper-item"), text="Second report")
        self.assertEqual(evaluate_claim(self.claim).independent_supporting_source_count, 2)

    def test_empty_groups_count_each_source_independently(self):
        first = self.make_source("local1", "B")
        second = self.make_source("local2", "B")
        self.make_evidence(source=first, raw=self.make_raw(first, self.event, "local1-item"), text="First")
        self.make_evidence(source=second, raw=self.make_raw(second, self.event, "local2-item"), text="Second")
        self.assertEqual(evaluate_claim(self.claim).independent_supporting_source_count, 2)

    def test_official_support_is_supported(self):
        official = self.make_source("official", "A", "authority")
        self.make_evidence(source=official, raw=self.make_raw(official, self.event, "official-item"))
        self.assertEqual(evaluate_claim(self.claim).status, VerificationStatus.SUPPORTED)

    def test_two_independent_reliable_supports_are_supported(self):
        other = self.make_source("paper", "C", "paper")
        self.make_evidence()
        self.make_evidence(source=other, raw=self.make_raw(other, self.event, "paper-item"), text="Independent support")
        self.assertEqual(evaluate_claim(self.claim).status, VerificationStatus.SUPPORTED)

    def test_one_media_support_is_partially_supported(self):
        self.make_evidence()
        self.assertEqual(evaluate_claim(self.claim).status, VerificationStatus.PARTIALLY_SUPPORTED)

    def test_mentions_only_is_unverified(self):
        self.make_evidence(stance=EvidenceStance.MENTIONS)
        self.assertEqual(evaluate_claim(self.claim).status, VerificationStatus.UNVERIFIED)

    def test_social_lead_support_only_is_unverified(self):
        social = self.make_source("social", "D", "social")
        self.make_evidence(source=social, raw=self.make_raw(social, self.event, "social-item"))
        self.assertEqual(evaluate_claim(self.claim).status, VerificationStatus.UNVERIFIED)

    def test_contradiction_only_is_contradicted(self):
        self.make_evidence(stance=EvidenceStance.CONTRADICTS)
        self.assertEqual(evaluate_claim(self.claim).status, VerificationStatus.CONTRADICTED)

    def test_support_and_contradiction_are_conflicted(self):
        other = self.make_source("paper", "B", "paper")
        self.make_evidence(text="The cut was 25 basis points.")
        self.make_evidence(source=other, raw=self.make_raw(other, self.event, "paper-item"), stance=EvidenceStance.CONTRADICTS, text="The cut was 50 basis points.")
        result = evaluate_claim(self.claim)
        self.assertEqual(result.status, VerificationStatus.CONFLICTED)
        self.assertEqual(result.contradicting_evidence_count, 1)

    def test_different_dates_remain_distinct_claims(self):
        first = Claim.objects.create(event=self.event, statement="Decision takes effect September 11.", claim_type=ClaimType.DATE)
        second = Claim.objects.create(event=self.event, statement="Decision takes effect September 12.", claim_type=ClaimType.DATE)
        self.assertNotEqual(first.normalized_statement, second.normalized_statement)

    def test_rejected_status_is_preserved(self):
        self.claim.verification_status = VerificationStatus.REJECTED
        self.claim.save()
        self.make_evidence()
        self.assertEqual(evaluate_claim(self.claim).status, VerificationStatus.REJECTED)

    def test_dry_run_changes_no_database_state(self):
        self.make_evidence()
        before = (self.claim.verification_status, JobRun.objects.count(), AuditLog.objects.count())
        output = StringIO()
        call_command("evaluate_claims", dry_run=True, event=self.event.pk, stdout=output)
        self.claim.refresh_from_db()
        self.assertEqual((self.claim.verification_status, JobRun.objects.count(), AuditLog.objects.count()), before)
        self.assertIn('"claim_id"', output.getvalue())

    def test_formal_command_updates_status_and_records_job_step_and_audit(self):
        self.make_evidence()
        call_command("evaluate_claims", event=self.event.pk, stdout=StringIO())
        self.claim.refresh_from_db()
        run = JobRun.objects.get(job_type="CLAIM_EVALUATION")
        self.assertEqual(self.claim.verification_status, VerificationStatus.PARTIALLY_SUPPORTED)
        self.assertEqual(run.status, JobStatus.SUCCESS)
        self.assertEqual(run.steps.get().output_summary[VerificationStatus.PARTIALLY_SUPPORTED], 1)
        audit = AuditLog.objects.get(action="CLAIM_STATUS_CHANGED")
        self.assertEqual(audit.after_data["job_run_id"], run.pk)

    def test_repeated_command_does_not_duplicate_status_audit(self):
        self.make_evidence()
        call_command("evaluate_claims", event=self.event.pk, stdout=StringIO())
        call_command("evaluate_claims", event=self.event.pk, stdout=StringIO())
        self.assertEqual(AuditLog.objects.filter(action="CLAIM_STATUS_CHANGED").count(), 1)
        self.assertEqual(ClaimEvidence.objects.count(), 1)

    def test_evidence_create_update_delete_are_audited_without_excerpt(self):
        evidence = self.make_evidence()
        evidence.stance = EvidenceStance.MENTIONS
        evidence.save()
        evidence_id = evidence.pk
        evidence.delete()
        actions = list(AuditLog.objects.filter(object_type="ClaimEvidence", object_id=str(evidence_id)).values_list("action", flat=True))
        self.assertEqual(actions, ["CLAIM_EVIDENCE_CREATED", "CLAIM_EVIDENCE_UPDATED", "CLAIM_EVIDENCE_DELETED"])
        self.assertNotIn("evidence_text", str(AuditLog.objects.filter(object_id=str(evidence_id)).values("before_data", "after_data")))

    def test_admin_exposes_event_claim_and_claim_evidence_relationships(self):
        self.assertIn(ClaimInline, EventAdmin(Event, admin.site).inlines)
        self.assertIn(ClaimEvidenceInline, ClaimAdmin(Claim, admin.site).inlines)
        self.assertIn("source", ClaimEvidenceAdmin(ClaimEvidence, admin.site).readonly_fields)

    def test_admin_manual_status_change_records_actor(self):
        request = RequestFactory().post("/admin/")
        request.user = get_user_model().objects.create_user(username="claim-editor")
        self.claim.verification_status = VerificationStatus.REJECTED
        ClaimAdmin(Claim, admin.site).save_model(request, self.claim, form=None, change=True)
        self.assertEqual(AuditLog.objects.get(action="CLAIM_STATUS_CHANGED").actor, request.user)


class ClaimEvaluationConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        now = timezone.now()
        event = Event.objects.create(title="Concurrent event", first_seen_at=now, last_seen_at=now)
        source = Source.objects.create(name="Concurrent source", source_type="MEDIA", url="https://concurrent.test", domain="concurrent.test", tier="B")
        raw = RawItem.objects.create(source=source, event=event, url="https://concurrent.test/item", title="item", discovered_at=now)
        self.claim = Claim.objects.create(event=event, statement="Concurrent claim")
        ClaimEvidence.objects.create(claim=self.claim, raw_item=raw, source=source, evidence_text="Concurrent support", stance=EvidenceStance.SUPPORTS, captured_at=now)

    def test_concurrent_evaluation_has_valid_status_and_one_status_audit(self):
        claim_id = self.claim.pk

        def run_service():
            close_old_connections()
            try:
                ClaimEvaluationService().run(Claim.objects.filter(pk=claim_id))
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: run_service(), range(2)))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.verification_status, VerificationStatus.PARTIALLY_SUPPORTED)
        self.assertEqual(AuditLog.objects.filter(action="CLAIM_STATUS_CHANGED", object_id=str(claim_id)).count(), 1)
