from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest.mock import patch

from django.contrib import admin
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.admin import ClaimEvidenceAdmin, ClaimEvidenceInline
from apps.claims.evidence_review import EvidenceReviewError, review_claim_evidence
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.sources.models import Source


class EvidenceReviewFixtureMixin:
    def make_fixture(self, *, stance=EvidenceStance.MENTIONS):
        now = timezone.now()
        event = Event.objects.create(title="Evidence review", first_seen_at=now, last_seen_at=now)
        source = Source.objects.create(
            identifier=f"review-{event.pk}", name="Official source", source_type="OFFICIAL",
            url=f"https://review-{event.pk}.invalid", domain=f"review-{event.pk}.invalid",
            tier="A", source_group="official-group",
        )
        text = "Agency awarded the launch contract to Company X."
        raw = RawItem.objects.create(
            source=source, event=event, external_id="one", url=f"{source.url}/one",
            title=text, discovered_at=now,
        )
        claim = Claim.objects.create(event=event, statement=text)
        evidence = ClaimEvidence.objects.create(
            claim=claim, raw_item=raw, source=source, evidence_text=text,
            stance=stance, captured_at=now,
        )
        return evidence


class EvidenceReviewTests(EvidenceReviewFixtureMixin, TestCase):
    def test_mentions_to_supports_updates_stance_and_audits_review(self):
        evidence = self.make_fixture()
        result = review_claim_evidence(
            evidence.pk,
            new_stance=EvidenceStance.SUPPORTS,
            reason="manual verification: quote directly supports the claim",
        )
        evidence.refresh_from_db()
        self.assertTrue(result.changed)
        self.assertEqual(evidence.stance, EvidenceStance.SUPPORTS)
        audit = AuditLog.objects.get(action="CLAIM_EVIDENCE_UPDATED", object_id=str(evidence.pk))
        self.assertEqual(audit.before_data["stance"], EvidenceStance.MENTIONS)
        self.assertEqual(audit.after_data["stance"], EvidenceStance.SUPPORTS)
        self.assertEqual(audit.after_data["reason"], "manual verification: quote directly supports the claim")
        self.assertEqual(audit.after_data["actor_type"], "MANUAL")
        self.assertIsNone(audit.actor)
        self.assertIn("timestamp", audit.after_data)

    def test_supports_to_supports_is_idempotent_without_second_audit(self):
        evidence = self.make_fixture()
        first = review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="first review")
        second = review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="repeat review")
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(AuditLog.objects.filter(action="CLAIM_EVIDENCE_UPDATED").count(), 1)
        self.assertEqual(ClaimEvidence.objects.count(), 1)

    def test_contradicts_to_supports_is_rejected(self):
        evidence = self.make_fixture(stance=EvidenceStance.CONTRADICTS)
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="reviewed")

    def test_unclear_to_supports_is_rejected(self):
        evidence = self.make_fixture(stance=EvidenceStance.UNCLEAR)
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="reviewed")

    def test_target_other_than_supports_is_rejected(self):
        evidence = self.make_fixture()
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(evidence.pk, new_stance=EvidenceStance.CONTRADICTS, reason="reviewed")

    def test_missing_evidence_is_rejected(self):
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(999999, new_stance=EvidenceStance.SUPPORTS, reason="reviewed")

    def test_quote_not_in_raw_item_is_rejected(self):
        evidence = self.make_fixture()
        ClaimEvidence.objects.filter(pk=evidence.pk).update(
            evidence_text="Invented quote", normalized_evidence_text="Invented quote"
        )
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="reviewed")

    def test_source_mismatch_is_rejected(self):
        evidence = self.make_fixture()
        other = Source.objects.create(
            identifier="other-source", name="Other", source_type="MEDIA",
            url="https://other.invalid", domain="other.invalid", tier="B",
        )
        ClaimEvidence.objects.filter(pk=evidence.pk).update(source=other)
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="reviewed")

    def test_raw_item_without_matching_quote_is_rejected(self):
        evidence = self.make_fixture()
        other_raw = RawItem.objects.create(
            source=evidence.source, event=evidence.claim.event, external_id="two",
            url=f"{evidence.source.url}/two", title="Unrelated material", discovered_at=timezone.now(),
        )
        ClaimEvidence.objects.filter(pk=evidence.pk).update(raw_item=other_raw)
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="reviewed")

    def test_cross_event_raw_item_is_rejected(self):
        evidence = self.make_fixture()
        other_event = Event.objects.create(
            title="Other event", first_seen_at=timezone.now(), last_seen_at=timezone.now()
        )
        other_raw = RawItem.objects.create(
            source=evidence.source, event=other_event, external_id="cross-event",
            url=f"{evidence.source.url}/cross", title=evidence.evidence_text, discovered_at=timezone.now(),
        )
        ClaimEvidence.objects.filter(pk=evidence.pk).update(raw_item=other_raw)
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="reviewed")

    def test_empty_and_overlong_reasons_are_rejected(self):
        evidence = self.make_fixture()
        for reason in ("", "   ", "x" * 501):
            with self.subTest(length=len(reason)):
                with self.assertRaises(EvidenceReviewError):
                    review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason=reason)

    @override_settings(SECRET_KEY="test-review-secret-value", LLM_API_KEY="test-llm-secret-value")
    def test_reason_containing_configured_secret_is_rejected(self):
        evidence = self.make_fixture()
        with self.assertRaises(EvidenceReviewError):
            review_claim_evidence(
                evidence.pk,
                new_stance=EvidenceStance.SUPPORTS,
                reason="reviewed with test-llm-secret-value",
            )
        self.assertFalse(AuditLog.objects.filter(action="CLAIM_EVIDENCE_UPDATED").exists())

    def test_audit_failure_rolls_back_stance_change(self):
        evidence = self.make_fixture()
        with patch("apps.claims.signals.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                review_claim_evidence(evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="reviewed")
        evidence.refresh_from_db()
        self.assertEqual(evidence.stance, EvidenceStance.MENTIONS)
        self.assertFalse(AuditLog.objects.filter(action="CLAIM_EVIDENCE_UPDATED").exists())

    def test_command_uses_service_and_requires_reason(self):
        evidence = self.make_fixture()
        with self.assertRaises(CommandError):
            call_command(
                "review_claim_evidence", evidence=evidence.pk,
                stance=EvidenceStance.SUPPORTS, stdout=StringIO(),
            )
        output = StringIO()
        call_command(
            "review_claim_evidence", evidence=evidence.pk, stance=EvidenceStance.SUPPORTS,
            reason="operator verified support", stdout=output,
        )
        self.assertIn('"changed": true', output.getvalue())

    def test_admin_stance_is_read_only_to_prevent_save_model_bypass(self):
        self.assertIn("stance", ClaimEvidenceAdmin(ClaimEvidence, admin.site).readonly_fields)
        self.assertIn("stance", ClaimEvidenceInline.readonly_fields)


class EvidenceReviewConcurrencyTests(EvidenceReviewFixtureMixin, TransactionTestCase):
    def test_concurrent_reviews_create_one_transition_audit(self):
        evidence = self.make_fixture()

        def review():
            close_old_connections()
            try:
                return review_claim_evidence(
                    evidence.pk, new_stance=EvidenceStance.SUPPORTS, reason="concurrent review"
                ).changed
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            changed = list(pool.map(lambda _: review(), range(2)))
        evidence.refresh_from_db()
        self.assertEqual(sorted(changed), [False, True])
        self.assertEqual(evidence.stance, EvidenceStance.SUPPORTS)
        self.assertEqual(AuditLog.objects.filter(action="CLAIM_EVIDENCE_UPDATED").count(), 1)
