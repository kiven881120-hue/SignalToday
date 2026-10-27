from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from apps.ai_content.evidence_hint_refresh import EvidenceHintRefreshError, refresh_claim_evidence_hint
from apps.ai_content.models import ClaimCandidate, ClaimCandidateEvidenceHint, EvidenceHintStatus, ModelCall
from apps.audit.models import AuditLog
from apps.claims.models import ClaimType, EvidenceStance
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.sources.models import Source, SourceTier, SourceType


class EvidenceHintRefreshTests(TestCase):
    def setUp(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.actor = get_user_model().objects.create_user(username="reviewer")
        self.source = Source.objects.create(name="Official", source_type=SourceType.OFFICIAL, url="https://x.test", domain="x.test", tier=SourceTier.A)
        self.event = Event.objects.create(title="Jobs", first_seen_at=now, last_seen_at=now)
        self.raw = RawItem.objects.create(source=self.source, event=self.event, external_id="r1", url="https://x.test/r1", title="Jobs", discovered_at=now, raw_metadata={"body": "Agency confirmed 100 jobs. Updated statement says 120 jobs."})
        self.call = ModelCall.objects.create(provider="fixture", model="fixture", purpose="CLAIM_EXTRACTION", prompt_version="v", schema_version="v", status="SUCCESS", event=self.event)
        self.candidate = ClaimCandidate.objects.create(event=self.event, model_call=self.call, claim_text="Agency confirmed 120 jobs.", claim_type=ClaimType.FACT, confidence="0.8")
        self.old = ClaimCandidateEvidenceHint.objects.create(candidate=self.candidate, raw_item=self.raw, proposed_raw_item_id=self.raw.pk, quote="Agency confirmed 100 jobs.", hint_type=EvidenceStance.SUPPORTS, status=EvidenceHintStatus.VALID, is_current=True)

    def refresh(self, **kwargs):
        params = dict(raw_item_id=self.raw.pk, quote="Updated statement says 120 jobs.", actor=self.actor, reason="Current body changed and replacement quote was verified.", expected_old_hint_id=self.old.pk)
        params.update(kwargs)
        return refresh_claim_evidence_hint(self.candidate.pk, **params)

    def test_success_is_append_only_and_audited(self):
        result = self.refresh()
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, EvidenceHintStatus.SUPERSEDED)
        self.assertEqual(self.old.quote, "Agency confirmed 100 jobs.")
        self.assertEqual(result.new_hint.status, EvidenceHintStatus.VALID)
        self.assertEqual(ClaimCandidateEvidenceHint.objects.filter(candidate=self.candidate, status=EvidenceHintStatus.VALID, is_current=True).count(), 1)
        audit = AuditLog.objects.get(action="CLAIM_EVIDENCE_HINT_REFRESHED")
        self.assertEqual(audit.before_data["quote"], self.old.quote)
        self.assertEqual(audit.after_data["new_hint_id"], result.new_hint.pk)

    def test_missing_quote_has_no_changes(self):
        before = list(ClaimCandidateEvidenceHint.objects.values_list("status", "quote"))
        with self.assertRaises(EvidenceHintRefreshError):
            self.refresh(quote="Not in current body")
        self.assertEqual(before, list(ClaimCandidateEvidenceHint.objects.values_list("status", "quote")))
        self.assertFalse(AuditLog.objects.exists())

    def test_raw_item_and_expected_hint_are_checked(self):
        other_event = Event.objects.create(title="Other", first_seen_at=self.raw.discovered_at, last_seen_at=self.raw.discovered_at)
        other = RawItem.objects.create(source=self.source, event=other_event, external_id="r2", url="https://x.test/r2", title="Other", discovered_at=self.raw.discovered_at, raw_metadata={"body": "Updated statement says 120 jobs."})
        with self.assertRaises(EvidenceHintRefreshError):
            self.refresh(raw_item_id=other.pk)
        with self.assertRaises(EvidenceHintRefreshError):
            self.refresh(expected_old_hint_id=self.old.pk + 100)
        self.assertEqual(ClaimCandidateEvidenceHint.objects.count(), 1)

    def test_refresh_does_not_create_pipeline_objects(self):
        self.refresh()
        self.assertEqual(ModelCall.objects.count(), 1)
        self.assertFalse(self.candidate.accepted_claim_id)

    def test_superseded_hint_cannot_be_used_as_current(self):
        result = self.refresh()
        with self.assertRaises(EvidenceHintRefreshError):
            self.refresh(expected_old_hint_id=self.old.pk)
        self.assertEqual(result.new_hint.status, EvidenceHintStatus.VALID)

    def test_current_valid_unique_constraint(self):
        with self.assertRaises(IntegrityError):
            ClaimCandidateEvidenceHint.objects.create(candidate=self.candidate, raw_item=self.raw, proposed_raw_item_id=self.raw.pk, quote="Another current quote", hint_type=EvidenceStance.SUPPORTS, status=EvidenceHintStatus.VALID, is_current=True)
