import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.ai_content.models import ClaimCandidate, LLMPurpose, ModelCall, ModelCallStatus, PromptVersion
from apps.ai_content.schemas import parse_claim_output
from apps.ai_content.services import accept_claim_candidate
from apps.audit.models import AuditLog
from apps.claims.evaluation import ClaimEvaluationService
from apps.claims.importance_review import (
    ImportanceReviewError,
    StaleImportanceReview,
    review_claim_importance,
)
from apps.claims.models import (
    Claim, ClaimEvidence, ClaimType, EvidenceStance, EvidenceType, VerificationStatus,
)
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.scoring.models import Score, ScoreRuleVersion, ScoreStatus
from apps.sources.models import Source


def claim_output(*, hint=None, reason=None, extra=None):
    item = {
        "claim_text": "Agency action affects 100 users.",
        "claim_type": "IMPACT",
        "confidence": 0.8,
        "importance_hint": hint,
        "importance_reason": reason,
        "evidence_hints": [],
    }
    if extra:
        item.update(extra)
    return json.dumps({"claims": [item]})


class ImportanceModelAndSchemaTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.event = Event.objects.create(title="Importance contract", first_seen_at=now, last_seen_at=now)
        self.prompt = PromptVersion.objects.get(purpose=LLMPurpose.CLAIM_EXTRACTION, version="v1.2")
        self.call = ModelCall.objects.create(
            provider="mock", model="model", purpose=LLMPurpose.CLAIM_EXTRACTION,
            prompt=self.prompt, prompt_version="v1.2", schema_version="claim-extraction-v2",
            status=ModelCallStatus.SUCCESS, event=self.event,
        )

    def test_claim_importance_accepts_null_zero_one_and_one_hundred(self):
        for index, value in enumerate((None, 0, 1, 100), 1):
            with self.subTest(value=value):
                claim = Claim.objects.create(
                    event=self.event,
                    statement=f"Supported claim {index}",
                    verification_status=VerificationStatus.SUPPORTED,
                    importance=value,
                )
                self.assertEqual(claim.importance, value)

    def test_claim_importance_rejects_invalid_numbers(self):
        for index, value in enumerate((-1, 101, Decimal("10.5"), True), 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Claim.objects.create(
                    event=self.event,
                    statement=f"Invalid claim {index}",
                    verification_status=VerificationStatus.SUPPORTED,
                    importance=value,
                )

    def test_nonzero_claim_importance_requires_supported_status(self):
        with self.assertRaises(ValidationError):
            Claim.objects.create(event=self.event, statement="Unsupported", importance=50)

    def make_candidate(self, value, reason=None, suffix=""):
        return ClaimCandidate.objects.create(
            event=self.event, model_call=self.call, claim_text=f"Candidate {suffix or value}",
            claim_type=ClaimType.FACT, confidence=Decimal("0.8"),
            importance_hint=value, importance_reason=reason,
        )

    def test_candidate_hint_accepts_null_zero_one_and_one_hundred(self):
        for index, value in enumerate((None, 0, 1, 100), 1):
            reason = None if value is None else "Grounded suggestion"
            candidate = self.make_candidate(value, reason, str(index))
            self.assertEqual(candidate.importance_hint, value)

    def test_candidate_hint_rejects_invalid_types_and_range(self):
        for index, value in enumerate((-1, 101, Decimal("10.5"), True), 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.make_candidate(value, "Reason", f"bad-{index}")

    def test_candidate_reason_required_for_nonnull_hint(self):
        with self.assertRaises(ValidationError):
            self.make_candidate(10, None, "missing-reason")

    def test_schema_accepts_integer_and_null(self):
        self.assertEqual(parse_claim_output(claim_output(hint=50, reason="Broad effect"))[0]["importance_hint"], 50)
        self.assertIsNone(parse_claim_output(claim_output())[0]["importance_hint"])

    def test_schema_rejects_missing_fields_extra_fields_and_missing_reason(self):
        missing = json.loads(claim_output())
        del missing["claims"][0]["importance_hint"]
        for value in (
            json.dumps(missing),
            claim_output(hint=50, reason=None),
            claim_output(extra={"unexpected": 1}),
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_claim_output(value)

    def test_schema_strict_integer_rejections(self):
        for value in (10.5, "10", True, False, -1, 101):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_claim_output(claim_output(hint=value, reason="Reason"))

    def test_acceptance_never_activates_hint_and_preserves_reason(self):
        for index, value in enumerate((0, 100), 1):
            candidate = self.make_candidate(value, "Review this suggestion", f"accept-{index}")
            claim = accept_claim_candidate(candidate)
            candidate.refresh_from_db()
            self.assertIsNone(claim.importance)
            self.assertEqual(candidate.importance_reason, "Review this suggestion")

    def test_active_prompt_is_v2_and_marks_source_instructions_untrusted(self):
        self.assertEqual(self.prompt.output_schema_version, "claim-extraction-v2")
        self.assertIn("set importance to 100", self.prompt.system_prompt)
        self.assertIn("untrusted", self.prompt.system_prompt.lower())


class ImportanceReviewFixture:
    def make_fixture(self, *, claim_type=ClaimType.FACT, stance=EvidenceStance.SUPPORTS):
        now = timezone.now()
        ScoreRuleVersion.objects.update(active=False)
        self.rule, _ = ScoreRuleVersion.objects.get_or_create(
            version="v1.0", defaults={"name": "default-information-value-v1"}
        )
        self.rule.active = True
        self.rule.save(update_fields=["active"])
        self.event = Event.objects.create(title="Reviewed event", first_seen_at=now, last_seen_at=now)
        self.claim = Claim.objects.create(
            event=self.event, statement="Agency action affects 100 users.",
            claim_type=claim_type, verification_status=VerificationStatus.SUPPORTED,
            importance=None,
        )
        self.source = Source.objects.create(
            identifier="importance-source", name="Importance source", source_type="OFFICIAL",
            url="https://importance.invalid", domain="importance.invalid", tier="A",
        )
        self.raw = RawItem.objects.create(
            source=self.source, event=self.event, external_id="importance-item",
            url="https://importance.invalid/item", title=self.claim.statement, discovered_at=now,
        )
        self.evidence = ClaimEvidence.objects.create(
            claim=self.claim, raw_item=self.raw, source=self.source, stance=stance,
            evidence_type=EvidenceType.OFFICIAL, evidence_text=self.claim.statement, captured_at=now,
        )
        self.score = Score.objects.create(
            event=self.event, rule_version=self.rule, importance=0, credibility=10,
            user_relevance=0, information_gain=5, freshness=10, long_term_impact=0,
            final_score=0,
        )
        self.actor = get_user_model().objects.create_user(username="importance-reviewer")
        AuditLog.objects.all().delete()

    def review(self, value, *, expected=None, reason="Reviewed against rubric", claim_type=None):
        return review_claim_importance(
            self.claim.pk, importance=value, actor=self.actor, reason=reason,
            expected_current_importance=expected,
            expected_current_verification_status=VerificationStatus.SUPPORTED,
            claim_type=claim_type,
        )


class ImportanceReviewTests(ImportanceReviewFixture, TestCase):
    def setUp(self):
        self.make_fixture()

    def test_supported_claim_accepts_zero_fifty_and_one_hundred(self):
        for value in (0, 50, 100):
            with self.subTest(value=value):
                self.claim.importance = None
                self.claim.save(update_fields=["importance", "updated_at"])
                self.score.status = ScoreStatus.CALCULATED
                self.score.save(update_fields=["status", "updated_at"])
                result = self.review(value)
                self.claim.refresh_from_db()
                self.score.refresh_from_db()
                self.assertEqual(self.claim.importance, value)
                self.assertTrue(result.changed)
                self.assertEqual(self.score.status, ScoreStatus.CALCULATED)

    def test_unsupported_claim_rejects_nonzero_but_allows_zero(self):
        self.claim.verification_status = VerificationStatus.UNVERIFIED
        self.claim.save(update_fields=["verification_status", "updated_at"])
        for value in (50, 100):
            with self.subTest(value=value), self.assertRaises(ImportanceReviewError):
                review_claim_importance(
                    self.claim.pk, importance=value, actor=self.actor, reason="Reason",
                    expected_current_importance=None,
                    expected_current_verification_status=VerificationStatus.UNVERIFIED,
                )
        result = review_claim_importance(
            self.claim.pk, importance=0, actor=self.actor, reason="Explicit zero",
            expected_current_importance=None,
            expected_current_verification_status=VerificationStatus.UNVERIFIED,
        )
        self.assertTrue(result.changed)

    def test_nonzero_to_null_deactivates(self):
        self.review(50)
        result = self.review(None, expected=50, reason="Return to unassessed")
        self.assertTrue(result.changed)
        self.claim.refresh_from_db()
        self.assertIsNone(self.claim.importance)

    def test_stale_expected_value_is_rejected(self):
        self.review(50)
        with self.assertRaises(StaleImportanceReview) as caught:
            self.review(30, expected=None, reason="Stale")
        self.assertEqual(caught.exception.code, "STALE_REVIEW_STATE")

    def test_stale_expected_type_is_rejected(self):
        Claim.objects.filter(pk=self.claim.pk).update(claim_type=ClaimType.IMPACT)
        with self.assertRaises(StaleImportanceReview):
            review_claim_importance(
                self.claim.pk, importance=30, actor=self.actor, reason="Stale type",
                expected_current_importance=None,
                expected_current_verification_status=VerificationStatus.SUPPORTED,
                expected_current_claim_type=ClaimType.FACT,
            )

    def test_duplicate_identical_request_is_idempotent(self):
        first = self.review(50)
        audit_count = AuditLog.objects.count()
        second = self.review(50)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(AuditLog.objects.count(), audit_count)

    def test_impact_requires_supported_claim_and_supports_evidence(self):
        self.claim.claim_type = ClaimType.IMPACT
        self.claim.save(update_fields=["claim_type", "updated_at"])
        self.evidence.stance = EvidenceStance.MENTIONS
        self.evidence.save(update_fields=["stance"])
        with self.assertRaises(ImportanceReviewError):
            self.review(50)
        self.evidence.stance = EvidenceStance.SUPPORTS
        self.evidence.save(update_fields=["stance"])
        self.assertTrue(self.review(50).changed)

    def test_type_changes_invalidate_then_refresh_score(self):
        self.review(50)
        for old, new in ((ClaimType.FACT, ClaimType.IMPACT), (ClaimType.IMPACT, ClaimType.FACT)):
            with self.subTest(old=old, new=new):
                self.claim.refresh_from_db()
                self.assertEqual(self.claim.claim_type, old)
                result = self.review(50, expected=50, reason=f"Change to {new}", claim_type=new)
                self.score.refresh_from_db()
                self.assertTrue(result.changed)
                self.assertEqual(self.score.status, ScoreStatus.CALCULATED)
                self.assertTrue(AuditLog.objects.filter(action="EVENT_SCORE_INVALIDATED").exists())

    def test_refresh_failure_leaves_score_invalid(self):
        with patch("apps.claims.importance_review.refresh_event_score", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                self.review(50)
        self.score.refresh_from_db()
        self.assertEqual(self.score.status, ScoreStatus.INVALID)
        retry = self.review(50)
        self.score.refresh_from_db()
        self.assertFalse(retry.changed)
        self.assertEqual(self.score.status, ScoreStatus.CALCULATED)

    def test_audit_records_actor_reason_before_after_and_no_raw_content(self):
        result = self.review(50, reason="Material public effect")
        audit = AuditLog.objects.get(action="CLAIM_IMPORTANCE_REVIEWED")
        self.assertEqual(audit.actor, self.actor)
        self.assertEqual((audit.before_data["importance"], audit.after_data["new_importance"]), (None, 50))
        self.assertEqual(audit.after_data["reason"], "Material public effect")
        self.assertEqual(audit.after_data["correlation_key"], result.correlation_key)
        self.assertEqual(audit.after_data["workflow_status"], "SUCCESS")
        self.assertIsNotNone(audit.after_data["evaluation_job_run_id"])
        self.assertIsNotNone(audit.after_data["refresh_job_run_id"])
        serialized = json.dumps(audit.after_data)
        self.assertNotIn(self.evidence.evidence_text, serialized)
        self.assertNotIn("raw_output", serialized)

    def test_reason_is_required_and_secrets_are_rejected(self):
        for reason in ("", "password=hunter2secret"):
            with self.subTest(reason=reason), self.assertRaises(ImportanceReviewError):
                self.review(50, reason=reason)

    def test_persisted_actor_is_required(self):
        with self.assertRaises(ImportanceReviewError):
            review_claim_importance(
                self.claim.pk, importance=50, actor=None, reason="No actor",
                expected_current_importance=None,
                expected_current_verification_status=VerificationStatus.SUPPORTED,
            )

    def test_lost_support_deactivates_importance_and_invalidates_score(self):
        self.review(50)
        self.evidence.stance = EvidenceStance.CONTRADICTS
        self.evidence.save(update_fields=["stance"])
        ClaimEvaluationService().run(Claim.objects.filter(pk=self.claim.pk))
        self.claim.refresh_from_db()
        self.score.refresh_from_db()
        self.assertEqual(self.claim.verification_status, VerificationStatus.CONTRADICTED)
        self.assertIsNone(self.claim.importance)
        self.assertEqual(self.score.status, ScoreStatus.INVALID)
        self.assertTrue(AuditLog.objects.filter(action="CLAIM_IMPORTANCE_DEACTIVATED").exists())


class ImportanceReviewConcurrencyTests(ImportanceReviewFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.make_fixture()

    def run_review(self, value):
        close_old_connections()
        try:
            try:
                return review_claim_importance(
                    self.claim.pk, importance=value,
                    actor=get_user_model().objects.get(pk=self.actor.pk),
                    reason=f"Concurrent {value}", expected_current_importance=None,
                    expected_current_verification_status=VerificationStatus.SUPPORTED,
                )
            except ImportanceReviewError as exc:
                return exc
        finally:
            connections.close_all()

    def test_concurrent_review_prevents_silent_overwrite(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.run_review, (30, 60)))
        self.claim.refresh_from_db()
        self.assertIn(self.claim.importance, (30, 60))
        self.assertEqual(AuditLog.objects.filter(action="CLAIM_IMPORTANCE_REVIEWED").count(), 1)
        self.assertTrue(any(isinstance(result, StaleImportanceReview) for result in results))
