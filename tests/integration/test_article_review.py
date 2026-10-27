from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connections
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.publishing.admin import ArticleAdmin, ArticleVersionAdmin, ArticleVersionClaimAdmin
from apps.publishing.models import (
    Article, ArticleClassification, ArticleStatus, ArticleVersion, ArticleVersionClaim,
)
from apps.publishing.content_gate import persist_content_gate
from apps.publishing.review import (
    ArticleReviewError, approve_article, correct_article, publish_article,
    reject_article, request_article_revision, retract_article, submit_article_for_review,
)
from apps.reviews.admin import ReviewAdmin
from apps.reviews.models import Review, ReviewDecision
from apps.scoring.models import Score, ScoreRuleVersion, ScoreStatus
from apps.sources.models import Source


class ArticleReviewFixtureMixin:
    def make_fixture(self, key="review"):
        now = timezone.now()
        actor = get_user_model().objects.create_user(username=f"actor-{key}")
        rule = ScoreRuleVersion.objects.create(
            version=f"rule-{key}", name="Review rule",
            rule_config={
                "weights": {
                    "importance": "0.20", "credibility": "0.25", "user_relevance": "0.15",
                    "information_gain": "0.15", "freshness": "0.15", "long_term_impact": "0.10",
                },
                "thresholds": {"FOCUS": "8.0", "NORMAL": "6.5", "CANDIDATE": "5.0"},
                "penalties": {},
            },
        )
        event = Event.objects.create(title=f"Event {key}", first_seen_at=now, last_seen_at=now)
        score = Score.objects.create(
            event=event, rule_version=rule, importance=10, credibility=10,
            user_relevance=10, information_gain=10, freshness=10,
            long_term_impact=10, final_score=10,
        )
        statement = f"{key} 项目在 2026 年确认更新，并公布实施细节"
        claim = Claim.objects.create(
            event=event, statement=statement,
            verification_status=VerificationStatus.SUPPORTED,
        )
        source = Source.objects.create(
            identifier=f"source-{key}", name=f"Source {key}", source_type="OFFICIAL",
            url=f"https://{key}.invalid", domain=f"{key}.invalid", tier="A",
        )
        raw = RawItem.objects.create(
            source=source, event=event, external_id=key, url=f"{source.url}/item",
            title=statement, discovered_at=now,
        )
        evidence = ClaimEvidence.objects.create(
            claim=claim, raw_item=raw, source=source, evidence_text=statement,
            stance=EvidenceStance.SUPPORTS, captured_at=now,
        )
        article = Article.objects.create(event=event, status=ArticleStatus.DRAFT)
        version = ArticleVersion.objects.create(
            article=article, version_number=1, title=f"{key} 项目确认更新",
            one_sentence_summary=statement, what_happened="并公布实施细节",
            why_it_matters="", who_is_affected="", example="",
            source_note=f"{source.name}: {raw.url}",
            language="zh-CN", status=ArticleStatus.DRAFT,
        )
        article.current_version = version
        article.save(update_fields=["current_version", "updated_at"])
        link = ArticleVersionClaim.objects.create(
            article_version=version, claim=claim, paragraph_key="article", display_order=1,
        )
        persist_content_gate(version, {"article_mode": "BRIEF"})
        return actor, article, version, claim, evidence, score, link

    def approve_fixture(self, key="review"):
        fixture = self.make_fixture(key)
        actor, _, version, *_ = fixture
        submit_article_for_review(version.pk, "提交人工审核", actor)
        approve_article(version.pk, "事实和来源均已核对", actor)
        return fixture

    def publish_fixture(self, key="review"):
        fixture = self.approve_fixture(key)
        actor, _, version, *_ = fixture
        publish_article(version.pk, "批准公开发布", actor)
        return fixture


class ArticleReviewTests(ArticleReviewFixtureMixin, TestCase):
    def test_grounding_failure_blocks_review(self):
        actor, _, version, *_ = self.make_fixture("grounding-block")
        version.title = "项目位于北京地区"
        version.save(update_fields=["title"])
        gate = persist_content_gate(version, {"article_mode": "BRIEF"})
        self.assertNotEqual(gate.grounding_status, "PASS")
        with self.assertRaises(ArticleReviewError) as caught:
            submit_article_for_review(version.pk, "提交", actor)
        self.assertEqual(caught.exception.code, "ARTICLE_GROUNDING_BLOCKED")

    def test_quality_too_thin_with_grounding_pass_enters_review_and_can_publish(self):
        actor, _, version, claim, *_ = self.make_fixture("quality-reviewable")
        version.title = claim.statement
        version.one_sentence_summary = claim.statement
        version.what_happened = claim.statement
        version.save(update_fields=["title", "one_sentence_summary", "what_happened"])
        gate = persist_content_gate(version, {"article_mode": "BRIEF"})
        self.assertEqual(gate.grounding_status, "PASS")
        self.assertEqual(gate.quality_status, "TOO_THIN")
        submitted = submit_article_for_review(version.pk, "提交", actor)
        self.assertEqual(submitted.article_version.status, ArticleStatus.IN_REVIEW)
        approved = approve_article(version.pk, "人工接受简短文章", actor)
        self.assertEqual(approved.article_version.status, ArticleStatus.APPROVED)
        published = publish_article(version.pk, "发布", actor)
        self.assertEqual(published.article_version.status, ArticleStatus.PUBLISHED)

    def test_quality_pass_with_grounding_pass_enters_review(self):
        actor, _, version, *_ = self.make_fixture("quality-pass")
        gate = version.content_validation
        self.assertEqual((gate.grounding_status, gate.quality_status), ("PASS", "PASS"))
        result = submit_article_for_review(version.pk, "提交", actor)
        self.assertEqual(result.article_version.status, ArticleStatus.IN_REVIEW)

    def test_draft_submit_approve_publish_state_machine_and_reviews(self):
        actor, article, version, claim, evidence, _, _ = self.make_fixture()
        submitted = submit_article_for_review(version.pk, "提交人工审核", actor)
        self.assertTrue(submitted.changed)
        self.assertEqual(submitted.review.decision, ReviewDecision.PENDING)
        self.assertEqual((submitted.article.status, submitted.article_version.status), (ArticleStatus.IN_REVIEW, ArticleStatus.IN_REVIEW))
        approved = approve_article(version.pk, "人工核对通过", actor)
        self.assertTrue(approved.changed)
        self.assertEqual((approved.review.decision, approved.review.comment, approved.review.reviewer), (ReviewDecision.APPROVED, "人工核对通过", actor))
        self.assertIsNotNone(approved.review.reviewed_at)
        published = publish_article(version.pk, "批准公开发布", actor)
        published.article.refresh_from_db()
        self.assertTrue(published.changed)
        self.assertEqual((published.article.status, published.article_version.status), (ArticleStatus.PUBLISHED, ArticleStatus.PUBLISHED))
        self.assertEqual(published.article.published_version_id, version.pk)
        self.assertIsNotNone(published.article.published_at)
        self.assertEqual(evidence.claim_id, claim.pk)
        self.assertEqual(published.job_run.status, JobStatus.SUCCESS)

    def test_review_rejection_returns_to_draft_and_can_be_resubmitted(self):
        actor, article, version, *_ = self.make_fixture()
        submit_article_for_review(version.pk, "首次提交", actor)
        rejected = reject_article(version.pk, "需要修订措辞", actor)
        self.assertEqual((rejected.article.status, rejected.article_version.status), (ArticleStatus.DRAFT, ArticleStatus.DRAFT))
        self.assertEqual(rejected.review.decision, ReviewDecision.REJECTED)
        repeated = reject_article(version.pk, "重复请求", actor)
        self.assertFalse(repeated.changed)
        resubmitted = submit_article_for_review(version.pk, "完成修订后再次提交", actor)
        self.assertTrue(resubmitted.changed)
        self.assertEqual(Review.objects.filter(article_version=version).count(), 2)

    def test_review_can_request_revision_without_approving(self):
        actor, article, version, *_ = self.make_fixture("revision-request")
        submit_article_for_review(version.pk, "提交", actor)
        result = request_article_revision(version.pk, "补充上下文", actor)
        self.assertEqual(result.review.decision, ReviewDecision.NEEDS_EDIT)
        self.assertEqual((article.status, version.status), (ArticleStatus.DRAFT, ArticleStatus.DRAFT))
        self.assertFalse(version.reviews.filter(decision=ReviewDecision.APPROVED).exists())

    def test_illegal_publish_transitions_are_rejected(self):
        actor, article, version, *_ = self.make_fixture()
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "不能从草稿发布", actor)
        submit_article_for_review(version.pk, "提交", actor)
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "不能从审核中发布", actor)
        reject_article(version.pk, "拒绝", actor)
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "不能从拒绝后发布", actor)
        self.assertIsNone(article.published_at)

    def test_published_cannot_be_approved_and_retracted_cannot_publish(self):
        actor, _, version, *_ = self.publish_fixture()
        with self.assertRaises(ArticleReviewError):
            approve_article(version.pk, "不可重复批准已发布文章", actor)
        retract_article(version.pk, "来源撤回", actor)
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "不可重新发布已撤回版本", actor)

    def test_approve_and_publish_are_idempotent_without_duplicate_business_audit(self):
        actor, article, version, *_ = self.make_fixture()
        submit_article_for_review(version.pk, "提交", actor)
        first_approve = approve_article(version.pk, "批准", actor)
        second_approve = approve_article(version.pk, "重复批准", actor)
        self.assertTrue(first_approve.changed)
        self.assertFalse(second_approve.changed)
        first_publish = publish_article(version.pk, "发布", actor)
        first_time = first_publish.article.published_at
        second_publish = publish_article(version.pk, "重复发布", actor)
        second_publish.article.refresh_from_db()
        self.assertTrue(first_publish.changed)
        self.assertFalse(second_publish.changed)
        self.assertEqual(second_publish.article.published_at, first_time)
        self.assertEqual(AuditLog.objects.filter(action="ARTICLE_REVIEW_APPROVED").count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="ARTICLE_PUBLISHED").count(), 1)
        self.assertEqual(Review.objects.count(), 1)

    def test_audit_payload_has_actor_reason_review_job_and_no_body(self):
        actor, article, version, *_ = self.make_fixture()
        submit_article_for_review(version.pk, "明确提交理由", actor)
        approve_article(version.pk, "明确批准理由", actor)
        for action in ("ARTICLE_SUBMITTED_FOR_REVIEW", "ARTICLE_REVIEW_APPROVED"):
            audit = AuditLog.objects.get(action=action)
            self.assertEqual(audit.actor, actor)
            self.assertEqual(audit.after_data["article_id"], article.pk)
            self.assertEqual(audit.after_data["article_version_id"], version.pk)
            self.assertIsNotNone(audit.after_data["review_id"])
            self.assertIsNotNone(audit.after_data["job_run_id"])
            self.assertNotIn("what_happened", audit.after_data)

    def test_reason_and_actor_are_required_and_secret_like_reason_is_rejected(self):
        actor, _, version, *_ = self.make_fixture()
        for reason in ("", "api_key=do-not-store-this"):
            with self.assertRaises(ArticleReviewError):
                submit_article_for_review(version.pk, reason, actor)
        with self.assertRaises(ArticleReviewError):
            submit_article_for_review(version.pk, "提交", None)

    def test_publish_requires_approved_review_even_if_status_was_tampered(self):
        actor, article, version, *_ = self.make_fixture()
        Article.objects.filter(pk=article.pk).update(status=ArticleStatus.APPROVED)
        ArticleVersion.objects.filter(pk=version.pk).update(status=ArticleStatus.APPROVED)
        with self.assertRaises(ArticleReviewError) as caught:
            publish_article(version.pk, "试图绕过审核", actor)
        self.assertEqual(caught.exception.code, "ARTICLE_REVIEW_MISSING")

    def test_publish_revalidates_content_gate(self):
        actor, _, version, *_ = self.approve_fixture("publish-revalidate")
        ArticleVersion.objects.filter(pk=version.pk).update(what_happened="未验证的新事实")
        with self.assertRaises(ArticleReviewError) as caught:
            publish_article(version.pk, "发布", actor)
        self.assertEqual(caught.exception.code, "ARTICLE_CONTENT_GATE_STALE")

    def test_old_approved_review_cannot_bypass_missing_gate(self):
        actor, _, version, *_ = self.approve_fixture("missing-gate")
        version.content_validation.delete()
        with self.assertRaises(ArticleReviewError) as caught:
            publish_article(version.pk, "发布", actor)
        self.assertEqual(caught.exception.code, "ARTICLE_CONTENT_GATE_MISSING")

    def test_publish_blocks_invalid_claim_status(self):
        actor, _, version, claim, *_ = self.approve_fixture()
        Claim.objects.filter(pk=claim.pk).update(verification_status=VerificationStatus.CONTRADICTED)
        with self.assertRaises(ArticleReviewError) as caught:
            publish_article(version.pk, "发布", actor)
        self.assertEqual(caught.exception.code, "ARTICLE_CLAIM_INVALID")

    def test_publish_blocks_deleted_evidence(self):
        actor, _, version, _, evidence, *_ = self.approve_fixture()
        evidence.delete()
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "发布", actor)

    def test_publish_blocks_non_supporting_or_broken_quote_evidence(self):
        actor, _, version, _, evidence, *_ = self.approve_fixture()
        ClaimEvidence.objects.filter(pk=evidence.pk).update(stance=EvidenceStance.CONTRADICTS)
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "发布", actor)

        ClaimEvidence.objects.filter(pk=evidence.pk).update(
            stance=EvidenceStance.SUPPORTS,
            evidence_text="伪造引文",
            normalized_evidence_text="伪造引文",
        )
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "发布", actor)

    def test_non_supporting_evidence_cannot_enter_review(self):
        actor, _, version, _, evidence, *_ = self.make_fixture("unsupported-review")
        ClaimEvidence.objects.filter(pk=evidence.pk).update(stance=EvidenceStance.CONTRADICTS)
        with self.assertRaises(ArticleReviewError) as caught:
            submit_article_for_review(version.pk, "提交", actor)
        self.assertIn(caught.exception.code, {"ARTICLE_EVIDENCE_INVALID", "ARTICLE_GROUNDING_BLOCKED"})
        version.refresh_from_db()
        self.assertEqual(version.status, ArticleStatus.DRAFT)

    def test_quote_not_found_cannot_enter_review(self):
        actor, _, version, _, evidence, *_ = self.make_fixture("quote-review")
        ClaimEvidence.objects.filter(pk=evidence.pk).update(
            evidence_text="源材料中不存在的引文", normalized_evidence_text="源材料中不存在的引文"
        )
        with self.assertRaises(ArticleReviewError) as caught:
            submit_article_for_review(version.pk, "提交", actor)
        self.assertIn(caught.exception.code, {"ARTICLE_EVIDENCE_INVALID", "ARTICLE_GROUNDING_BLOCKED"})
        version.refresh_from_db()
        self.assertEqual(version.status, ArticleStatus.DRAFT)

    def test_too_thin_article_can_be_human_classified_must_know(self):
        actor, _, version, claim, *_ = self.make_fixture("article-17")
        Claim.objects.filter(pk=claim.pk).update(importance=78)
        version.title = claim.statement
        version.one_sentence_summary = claim.statement
        version.what_happened = claim.statement
        original_title = version.title
        version.save(update_fields=["title", "one_sentence_summary", "what_happened"])
        gate = persist_content_gate(version, {"article_mode": "BRIEF"})
        self.assertEqual((gate.grounding_status, gate.quality_status), ("PASS", "TOO_THIN"))
        submit_article_for_review(version.pk, "提交", actor)
        result = approve_article(
            version.pk, "重要且事实成立", actor,
            classification=ArticleClassification.MUST_KNOW,
        )
        result.article_version.refresh_from_db()
        self.assertEqual(result.article_version.classification, ArticleClassification.MUST_KNOW)
        self.assertEqual(result.article_version.classified_by, actor)
        self.assertIsNotNone(result.article_version.classified_at)
        self.assertEqual(result.article_version.title, original_title)
        audit = AuditLog.objects.get(action="ARTICLE_REVIEW_APPROVED")
        self.assertEqual(audit.after_data["classification"], ArticleClassification.MUST_KNOW)

    def test_normal_article_approval_does_not_gain_classification(self):
        actor, _, version, *_ = self.make_fixture("article-19")
        submit_article_for_review(version.pk, "提交", actor)
        approve_article(version.pk, "普通发布", actor)
        publish_article(version.pk, "发布", actor)
        version.refresh_from_db()
        self.assertEqual(version.status, ArticleStatus.PUBLISHED)
        self.assertEqual(version.classification, "")
        self.assertIsNone(version.classified_by)
        self.assertIsNone(version.classified_at)

    def test_approved_classification_cannot_be_changed_outside_review(self):
        actor, _, version, *_ = self.make_fixture("classification-immutable")
        submit_article_for_review(version.pk, "提交", actor)
        approve_article(
            version.pk, "批准", actor, classification=ArticleClassification.MUST_KNOW
        )
        version.classification = ArticleClassification.DISCOVER
        with self.assertRaises(ValidationError):
            version.save(update_fields=["classification"])

    def test_evidence_review_block_is_audited(self):
        actor, _, version, _, evidence, *_ = self.make_fixture("evidence-audit")
        ClaimEvidence.objects.filter(pk=evidence.pk).update(stance=EvidenceStance.CONTRADICTS)
        with self.assertRaises(ArticleReviewError):
            submit_article_for_review(version.pk, "提交", actor)
        audit = AuditLog.objects.get(action="ARTICLE_CONTENT_GATE_BLOCKED")
        self.assertEqual(audit.after_data["transition"], "SUBMIT_REVIEW")
        self.assertIn(
            audit.after_data["reason_code"],
            {"ARTICLE_EVIDENCE_INVALID", "ARTICLE_GROUNDING_BLOCKED"},
        )

    def test_publish_blocks_raw_item_event_and_source_mismatch(self):
        actor, article, version, _, evidence, *_ = self.approve_fixture()
        other = Event.objects.create(title="Other", first_seen_at=timezone.now(), last_seen_at=timezone.now())
        RawItem.objects.filter(pk=evidence.raw_item_id).update(event=other)
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "发布", actor)
        RawItem.objects.filter(pk=evidence.raw_item_id).update(event=article.event)
        other_source = Source.objects.create(
            identifier="other-source", name="Other", source_type="MEDIA",
            url="https://other-source.invalid", domain="other-source.invalid", tier="B",
        )
        ClaimEvidence.objects.filter(pk=evidence.pk).update(source=other_source)
        with self.assertRaises(ArticleReviewError):
            publish_article(version.pk, "发布", actor)

    def test_publish_blocks_cross_event_claim_link(self):
        actor, _, version, _, _, _, link = self.approve_fixture("main")
        _, _, _, other_claim, _, _, _ = self.make_fixture("other")
        ArticleVersionClaim.objects.filter(pk=link.pk).update(claim=other_claim)
        with self.assertRaises(ArticleReviewError) as caught:
            publish_article(version.pk, "发布", actor)
        self.assertEqual(caught.exception.code, "ARTICLE_CLAIM_INVALID")

    def test_publish_blocks_invalid_score_and_eligibility(self):
        actor, _, version, _, _, score, _ = self.approve_fixture()
        Score.objects.filter(pk=score.pk).update(status=ScoreStatus.INVALID)
        with self.assertRaises(ArticleReviewError) as caught:
            publish_article(version.pk, "发布", actor)
        self.assertEqual(caught.exception.code, "ARTICLE_SCORE_INVALID")

    def test_claim_with_published_link_cannot_be_deleted(self):
        _, _, _, claim, *_ = self.publish_fixture()
        with self.assertRaises(ProtectedError):
            claim.delete()

    def test_correction_preserves_v1_and_opens_editable_v2_for_review(self):
        actor, article, version1, *_ = self.publish_fixture()
        corrected = correct_article(version1.pk, "修正已发布内容", actor)
        version1.refresh_from_db()
        article.refresh_from_db()
        version2 = corrected.article_version
        self.assertEqual(version1.status, ArticleStatus.CORRECTED)
        self.assertEqual((version2.version_number, version2.status), (2, ArticleStatus.DRAFT))
        self.assertEqual(article.current_version_id, version2.pk)
        self.assertEqual(article.published_version_id, version1.pk)
        self.assertEqual(article.status, ArticleStatus.DRAFT)
        self.assertEqual(version2.claim_links.count(), version1.claim_links.count())
        version1.title = "覆盖历史正文"
        with self.assertRaises(ValidationError):
            version1.save()
        version2.title = "修订后的标题"
        version2.save(update_fields=["title"])
        persist_content_gate(version2, {"article_mode": "BRIEF"})
        self.assertTrue(submit_article_for_review(version2.pk, "提交第二版", actor).changed)
        self.assertTrue(AuditLog.objects.filter(action="ARTICLE_CORRECTED").exists())

    def test_retraction_is_audited_and_idempotent(self):
        actor, article, version, *_ = self.publish_fixture()
        first = retract_article(version.pk, "来源正式撤回", actor)
        original_time = first.article.retracted_at
        second = retract_article(version.pk, "重复撤回", actor)
        second.article.refresh_from_db()
        self.assertEqual((first.changed, second.changed), (True, False))
        self.assertEqual((second.article.status, second.article_version.status), (ArticleStatus.RETRACTED, ArticleStatus.RETRACTED))
        self.assertEqual(second.article.retracted_at, original_time)
        self.assertEqual(AuditLog.objects.filter(action="ARTICLE_RETRACTED").count(), 1)

    def test_admin_status_and_review_fields_cannot_be_directly_edited(self):
        actor, article, version, *_ = self.make_fixture()
        request = RequestFactory().get("/admin/")
        request.user = actor
        article_admin = ArticleAdmin(Article, admin.site)
        version_admin = ArticleVersionAdmin(ArticleVersion, admin.site)
        review_admin = ReviewAdmin(Review, admin.site)
        link_admin = ArticleVersionClaimAdmin(ArticleVersionClaim, admin.site)
        self.assertIn("status", article_admin.get_readonly_fields(request, article))
        self.assertIn("status", version_admin.get_readonly_fields(request, version))
        self.assertFalse(review_admin.has_add_permission(request))
        self.assertFalse(review_admin.has_delete_permission(request))
        self.assertFalse(link_admin.has_add_permission(request))
        self.assertFalse(link_admin.has_delete_permission(request))

    def test_management_commands_use_service_and_return_json(self):
        actor, _, version, *_ = self.make_fixture()
        output = StringIO()
        call_command(
            "submit_article_review", article_version=version.pk,
            reason="命令提交", actor=actor.username, stdout=output,
        )
        self.assertIn('"changed": true', output.getvalue())
        output = StringIO()
        call_command(
            "approve_article", article_version=version.pk,
            reason="命令批准", actor=actor.username, classification="DISCOVER", stdout=output,
        )
        self.assertIn('"status": "APPROVED"', output.getvalue())
        version.refresh_from_db()
        self.assertEqual(version.classification, ArticleClassification.DISCOVER)


class ArticleReviewConcurrencyTests(ArticleReviewFixtureMixin, TransactionTestCase):
    def _parallel(self, function, version_id, actor, reason):
        def invoke():
            close_old_connections()
            try:
                return function(version_id, reason, get_user_model().objects.get(pk=actor.pk))
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(lambda _: invoke(), range(2)))

    def test_concurrent_approve_has_one_effective_transition(self):
        actor, _, version, *_ = self.make_fixture("concurrent-approve")
        submit_article_for_review(version.pk, "提交", actor)
        results = self._parallel(approve_article, version.pk, actor, "并发批准")
        self.assertEqual(sorted(result.changed for result in results), [False, True])
        self.assertEqual(AuditLog.objects.filter(action="ARTICLE_REVIEW_APPROVED").count(), 1)
        self.assertEqual(Review.objects.filter(decision=ReviewDecision.APPROVED).count(), 1)

    def test_concurrent_publish_has_one_effective_transition(self):
        actor, _, version, *_ = self.approve_fixture("concurrent-publish")
        results = self._parallel(publish_article, version.pk, actor, "并发发布")
        self.assertEqual(sorted(result.changed for result in results), [False, True])
        self.assertEqual(AuditLog.objects.filter(action="ARTICLE_PUBLISHED").count(), 1)
        version.refresh_from_db()
        self.assertEqual(version.status, ArticleStatus.PUBLISHED)
