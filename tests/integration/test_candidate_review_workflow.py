from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.ai_content.candidate_review import (
    CandidateReviewError,
    review_candidate_to_supported_claim,
)
from apps.ai_content.models import (
    ClaimCandidate,
    ClaimCandidateEvidenceHint,
    EvidenceHintStatus,
    ModelCall,
    ReviewStatus,
)
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.models import JobRun, JobStep
from apps.sources.models import Source


class CandidateReviewFixtureMixin:
    def make_fixture(self):
        now = timezone.now()
        self.actor = get_user_model().objects.create_user(username=f"reviewer-{Event.objects.count()}")
        self.event = Event.objects.create(title="Controlled review", first_seen_at=now, last_seen_at=now)
        self.source = Source.objects.create(
            identifier=f"controlled-{self.event.pk}", name="Official source", source_type="OFFICIAL",
            url=f"https://controlled-{self.event.pk}.invalid", domain=f"controlled-{self.event.pk}.invalid",
            source_group="official", tier="A", reliability=Decimal("0.9500"),
        )
        text = "Agency will sign the agreement on Sept. 14."
        self.raw = RawItem.objects.create(
            source=self.source, event=self.event, external_id="one",
            url=f"{self.source.url}/one", title=text, discovered_at=now,
        )
        self.call = ModelCall.objects.create(
            provider="test", model="test", purpose="CLAIM_EXTRACTION",
            prompt_version="v1.3", schema_version="claim-extraction-v3",
        )
        self.candidate = ClaimCandidate.objects.create(
            event=self.event, model_call=self.call, claim_text=text, claim_type="DATE",
            confidence=Decimal("0.9500"), reason="Directly stated by the material.",
        )
        self.hint = ClaimCandidateEvidenceHint.objects.create(
            candidate=self.candidate, raw_item=self.raw, proposed_raw_item_id=self.raw.pk,
            quote=text, hint_type=EvidenceStance.SUPPORTS, status=EvidenceHintStatus.VALID,
        )

    def review(self):
        return review_candidate_to_supported_claim(
            self.candidate.pk,
            expected_event_id=self.event.pk,
            evidence_hint_id=self.hint.pk,
            actor=self.actor,
            candidate_reason="Human verified the future tense and direct factual support.",
            evidence_reason=(
                "Human verified direct support from one official Tier-A source; "
                "there is no independent corroboration."
            ),
        )


class CandidateReviewWorkflowTests(CandidateReviewFixtureMixin, TestCase):
    def setUp(self):
        self.make_fixture()

    def test_accepts_candidate_then_reviews_mentions_as_supports(self):
        result = self.review()
        self.candidate.refresh_from_db()

        self.assertTrue(result.changed)
        self.assertEqual(self.candidate.status, ReviewStatus.ACCEPTED)
        self.assertEqual(result.claim.verification_status, VerificationStatus.SUPPORTED)
        self.assertEqual(result.evidence.stance, EvidenceStance.SUPPORTS)
        self.assertEqual((Claim.objects.count(), ClaimEvidence.objects.count()), (1, 1))
        creation = AuditLog.objects.get(action="CLAIM_EVIDENCE_CREATED")
        review = AuditLog.objects.get(action="CLAIM_EVIDENCE_UPDATED")
        self.assertEqual(creation.after_data["stance"], EvidenceStance.MENTIONS)
        self.assertEqual(review.before_data["stance"], EvidenceStance.MENTIONS)
        self.assertEqual(review.after_data["stance"], EvidenceStance.SUPPORTS)

    def test_audit_preserves_candidate_traceability_and_single_source_limit(self):
        result = self.review()
        created = AuditLog.objects.get(action="CLAIM_CREATED_FROM_CANDIDATE")
        completed = AuditLog.objects.get(action="CANDIDATE_CLAIM_EVIDENCE_REVIEW_COMPLETED")

        self.assertEqual(created.after_data["candidate_id"], self.candidate.pk)
        self.assertEqual(created.after_data["created_from"], "ClaimCandidate")
        self.assertEqual(created.after_data["confidence"], "0.9500")
        self.assertEqual(completed.after_data["claim_id"], result.claim.pk)
        self.assertEqual(completed.after_data["source_authority"], "OFFICIAL_TIER_A")
        self.assertFalse(completed.after_data["independent_corroboration"])
        self.assertEqual(JobRun.objects.count(), 1)
        self.assertEqual(JobStep.objects.count(), 3)

    def test_repeat_is_idempotent(self):
        first = self.review()
        counts = (Claim.objects.count(), ClaimEvidence.objects.count(), JobRun.objects.count(), AuditLog.objects.count())
        second = self.review()

        self.assertFalse(second.changed)
        self.assertEqual(first.claim.pk, second.claim.pk)
        self.assertEqual(counts, (
            Claim.objects.count(), ClaimEvidence.objects.count(), JobRun.objects.count(), AuditLog.objects.count()
        ))

    def test_wrong_event_or_hint_fails_without_writes(self):
        other = Event.objects.create(title="Other", first_seen_at=timezone.now(), last_seen_at=timezone.now())
        for event_id, hint_id in ((other.pk, self.hint.pk), (self.event.pk, self.hint.pk + 1000)):
            with self.subTest(event_id=event_id, hint_id=hint_id), self.assertRaises(CandidateReviewError):
                review_candidate_to_supported_claim(
                    self.candidate.pk, expected_event_id=event_id, evidence_hint_id=hint_id,
                    actor=self.actor, candidate_reason="Reviewed", evidence_reason="Reviewed",
                )
        self.assertEqual(self.candidate.status, ReviewStatus.PROPOSED)
        self.assertEqual((Claim.objects.count(), ClaimEvidence.objects.count(), JobRun.objects.count()), (0, 0, 0))


class CandidateReviewConcurrencyTests(CandidateReviewFixtureMixin, TransactionTestCase):
    def setUp(self):
        self.make_fixture()

    def test_concurrent_repeat_creates_one_claim_evidence_and_job(self):
        def run_review():
            close_old_connections()
            try:
                return self.review().changed
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            changed = list(pool.map(lambda _: run_review(), range(2)))
        self.assertEqual(sorted(changed), [False, True])
        self.assertEqual((Claim.objects.count(), ClaimEvidence.objects.count(), JobRun.objects.count()), (1, 1, 1))
