import re
from dataclasses import dataclass
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.claims.models import EvidenceStance, VerificationStatus
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.reviews.models import Review, ReviewDecision

from .eligibility import evaluate_article_generation_eligibility
from .content_gate import ContentGateError, require_content_gate_pass
from .models import (
    Article, ArticleClassification, ArticleStatus, ArticleVersion, ArticleVersionClaim,
)


MAX_REASON_LENGTH = 500


class ArticleReviewError(Exception):
    def __init__(self, message, code="ARTICLE_REVIEW_INVALID"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ArticleReviewResult:
    article: Article
    article_version: ArticleVersion
    changed: bool
    action: str
    job_run: JobRun
    review: Review | None = None


def _reason(value):
    value = str(value or "").strip()
    if not value:
        raise ArticleReviewError("A review reason is required", "ARTICLE_REVIEW_REASON_REQUIRED")
    if len(value) > MAX_REASON_LENGTH:
        raise ArticleReviewError("Review reason exceeds 500 characters", "ARTICLE_REVIEW_REASON_TOO_LONG")
    database_password = settings.DATABASES.get("default", {}).get("PASSWORD", "")
    secrets = (settings.SECRET_KEY, settings.LLM_API_KEY, database_password)
    if any(secret and len(str(secret)) >= 8 and str(secret) in value for secret in secrets):
        raise ArticleReviewError("Review reason contains sensitive configuration", "ARTICLE_REVIEW_REASON_SENSITIVE")
    if re.search(r"(?i)(api[_ -]?key|secret[_ -]?key|password|token)\s*[:=]\s*\S+", value):
        raise ArticleReviewError("Review reason appears to contain a secret", "ARTICLE_REVIEW_REASON_SENSITIVE")
    return value


def _actor(actor):
    if actor is None or not getattr(actor, "pk", None):
        raise ArticleReviewError("A persisted human actor is required", "ARTICLE_REVIEW_ACTOR_REQUIRED")
    return actor


def _classification(value):
    value = str(value or "").strip().upper()
    if value and value not in ArticleClassification.values:
        raise ArticleReviewError(
            "Classification must be MUST_KNOW, DISCOVER, or empty",
            "ARTICLE_CLASSIFICATION_INVALID",
        )
    return value


def _new_job(action, version_id, actor):
    run = JobRun.objects.create(
        job_type=f"ARTICLE_{action}", status=JobStatus.RUNNING,
        started_at=timezone.now(),
        idempotency_key=f"article-review:{action.lower()}:{version_id}:{uuid4().hex}",
    )
    step = JobStep.objects.create(
        job_run=run, step_name=action.lower(), status=JobStatus.RUNNING,
        started_at=timezone.now(),
        input_summary={"article_version_id": version_id, "actor_id": actor.pk},
    )
    return run, step


def _finish_job(run, step, status, *, output=None, error=""):
    now = timezone.now()
    step.status = status
    step.finished_at = now
    step.output_summary = output or {}
    step.error_message = error
    step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])
    run.status = status
    run.finished_at = now
    run.error_message = error
    run.save(update_fields=["status", "finished_at", "error_message"])


def _locked_version(version_id):
    article_id = ArticleVersion.objects.filter(pk=version_id).values_list("article_id", flat=True).first()
    if article_id is None:
        raise ArticleReviewError("ArticleVersion does not exist", "ARTICLE_VERSION_NOT_FOUND")
    article = Article.objects.select_for_update().select_related("event").get(pk=article_id)
    version = ArticleVersion.objects.select_for_update().select_related("article__event").get(pk=version_id)
    if version.article_id != article.pk or article.event_id != version.article.event_id:
        raise ArticleReviewError("ArticleVersion relationship is invalid", "ARTICLE_VERSION_INVALID")
    return article, version


def _require_current(article, version):
    if article.current_version_id != version.pk:
        raise ArticleReviewError("Only the current ArticleVersion may transition", "ARTICLE_VERSION_NOT_CURRENT")


def _require_content_gate(version):
    try:
        return require_content_gate_pass(version)
    except ContentGateError as exc:
        raise ArticleReviewError(str(exc), exc.code) from exc


def _audit(action, article, version, actor, reason, run, old_status, new_status, review=None, extra=None):
    payload = {
        "article_id": article.pk, "article_version_id": version.pk,
        "event_id": article.event_id, "old_status": old_status,
        "new_status": new_status, "actor_id": actor.pk, "reason": reason,
        "timestamp": timezone.now().isoformat(), "review_id": review.pk if review else None,
        "job_run_id": run.pk,
    }
    if extra:
        payload.update(extra)
    AuditLog.objects.create(
        actor=actor, action=action, object_type="ArticleVersion",
        object_id=str(version.pk), before_data={"status": old_status}, after_data=payload,
    )


def _execute(action, version_id, reason, actor, transition):
    reason = _reason(reason)
    actor = _actor(actor)
    run, step = _new_job(action, version_id, actor)
    try:
        with transaction.atomic():
            article, version, changed, review = transition(version_id, reason, actor, run)
        _finish_job(run, step, JobStatus.SUCCESS, output={
            "article_id": article.pk, "article_version_id": version.pk,
            "changed": changed, "status": version.status,
            "review_id": review.pk if review else None,
        })
        return ArticleReviewResult(article, version, changed, action, run, review)
    except Exception as exc:
        _finish_job(run, step, JobStatus.FAILED, error=str(exc)[:2000])
        gate_error = isinstance(exc, ContentGateError) or (
            isinstance(exc, ArticleReviewError)
            and exc.code.startswith((
                "ARTICLE_CONTENT_GATE", "ARTICLE_GROUNDING", "ARTICLE_CLAIM",
                "ARTICLE_EVIDENCE", "ARTICLE_SCORE",
            ))
        )
        if gate_error:
            AuditLog.objects.create(
                actor=actor, action="ARTICLE_CONTENT_GATE_BLOCKED",
                object_type="ArticleVersion", object_id=str(version_id),
                after_data={
                    "article_version_id": version_id, "job_run_id": run.pk,
                    "transition": action, "reason_code": exc.code,
                },
            )
        if isinstance(exc, ArticleReviewError):
            raise
        if isinstance(exc, ContentGateError):
            raise ArticleReviewError(str(exc), exc.code) from exc
        raise ArticleReviewError(str(exc)) from exc


def _submit(version_id, reason, actor, run):
    article, version = _locked_version(version_id)
    _require_current(article, version)
    pending = Review.objects.select_for_update().filter(
        article_version=version, decision=ReviewDecision.PENDING
    ).order_by("-pk").first()
    if article.status == version.status == ArticleStatus.IN_REVIEW and pending:
        return article, version, False, pending
    if article.status != ArticleStatus.DRAFT or version.status != ArticleStatus.DRAFT:
        raise ArticleReviewError("Only a DRAFT current version may be submitted", "ARTICLE_TRANSITION_INVALID")
    validate_article_for_publish(article, version)
    pending = Review.objects.create(
        article_version=version, reviewer=actor, risk_level=article.risk_level,
        decision=ReviewDecision.PENDING, comment=reason,
    )
    version.status = ArticleStatus.IN_REVIEW
    version.save(update_fields=["status"])
    article.status = ArticleStatus.IN_REVIEW
    article.save(update_fields=["status", "updated_at"])
    _audit("ARTICLE_SUBMITTED_FOR_REVIEW", article, version, actor, reason, run, ArticleStatus.DRAFT, ArticleStatus.IN_REVIEW, pending)
    return article, version, True, pending


def submit_article_for_review(article_version_id, reason, actor):
    return _execute("SUBMIT_REVIEW", article_version_id, reason, actor, _submit)


def _approve(version_id, reason, actor, run, classification):
    article, version = _locked_version(version_id)
    _require_current(article, version)
    approved = Review.objects.select_for_update().filter(
        article_version=version, decision=ReviewDecision.APPROVED
    ).order_by("-reviewed_at", "-pk").first()
    if article.status == version.status == ArticleStatus.APPROVED and approved:
        return article, version, False, approved
    if article.status != ArticleStatus.IN_REVIEW or version.status != ArticleStatus.IN_REVIEW:
        raise ArticleReviewError("Only an IN_REVIEW current version may be approved", "ARTICLE_TRANSITION_INVALID")
    validate_article_for_publish(article, version)
    review = Review.objects.select_for_update().filter(
        article_version=version, decision=ReviewDecision.PENDING
    ).order_by("-pk").first()
    if review is None:
        raise ArticleReviewError("A pending Review is required", "ARTICLE_REVIEW_MISSING")
    review.reviewer = actor
    review.decision = ReviewDecision.APPROVED
    review.comment = reason
    review.reviewed_at = timezone.now()
    review.save(update_fields=["reviewer", "decision", "comment", "reviewed_at"])
    if classification:
        version.classification = classification
        version.classified_by = actor
        version.classified_at = timezone.now()
    version.status = ArticleStatus.APPROVED
    version.save(update_fields=["status", "classification", "classified_by", "classified_at"])
    article.status = ArticleStatus.APPROVED
    article.save(update_fields=["status", "updated_at"])
    _audit(
        "ARTICLE_REVIEW_APPROVED", article, version, actor, reason, run,
        ArticleStatus.IN_REVIEW, ArticleStatus.APPROVED, review,
        extra={"classification": version.classification or None},
    )
    return article, version, True, review


def approve_article(article_version_id, reason, actor, classification=None):
    classification = _classification(classification)
    return _execute(
        "REVIEW_APPROVE", article_version_id, reason, actor,
        lambda version_id, reason, actor, run: _approve(
            version_id, reason, actor, run, classification
        ),
    )


def _reject(version_id, reason, actor, run):
    article, version = _locked_version(version_id)
    _require_current(article, version)
    rejected = Review.objects.select_for_update().filter(
        article_version=version, decision=ReviewDecision.REJECTED
    ).order_by("-reviewed_at", "-pk").first()
    if article.status == version.status == ArticleStatus.DRAFT and rejected:
        return article, version, False, rejected
    if article.status != ArticleStatus.IN_REVIEW or version.status != ArticleStatus.IN_REVIEW:
        raise ArticleReviewError("Only an IN_REVIEW current version may be rejected", "ARTICLE_TRANSITION_INVALID")
    review = Review.objects.select_for_update().filter(
        article_version=version, decision=ReviewDecision.PENDING
    ).order_by("-pk").first()
    if review is None:
        raise ArticleReviewError("A pending Review is required", "ARTICLE_REVIEW_MISSING")
    review.reviewer = actor
    review.decision = ReviewDecision.REJECTED
    review.comment = reason
    review.reviewed_at = timezone.now()
    review.save(update_fields=["reviewer", "decision", "comment", "reviewed_at"])
    version.status = ArticleStatus.DRAFT
    version.save(update_fields=["status"])
    article.status = ArticleStatus.DRAFT
    article.save(update_fields=["status", "updated_at"])
    _audit("ARTICLE_REVIEW_REJECTED", article, version, actor, reason, run, ArticleStatus.IN_REVIEW, ArticleStatus.DRAFT, review)
    return article, version, True, review


def reject_article(article_version_id, reason, actor):
    return _execute("REVIEW_REJECT", article_version_id, reason, actor, _reject)


def _request_revision(version_id, reason, actor, run):
    article, version = _locked_version(version_id)
    _require_current(article, version)
    if article.status != ArticleStatus.IN_REVIEW or version.status != ArticleStatus.IN_REVIEW:
        raise ArticleReviewError(
            "Only an IN_REVIEW current version may request revision",
            "ARTICLE_TRANSITION_INVALID",
        )
    review = Review.objects.select_for_update().filter(
        article_version=version, decision=ReviewDecision.PENDING
    ).order_by("-pk").first()
    if review is None:
        raise ArticleReviewError("A pending Review is required", "ARTICLE_REVIEW_MISSING")
    review.reviewer = actor
    review.decision = ReviewDecision.NEEDS_EDIT
    review.comment = reason
    review.reviewed_at = timezone.now()
    review.save(update_fields=["reviewer", "decision", "comment", "reviewed_at"])
    version.status = ArticleStatus.DRAFT
    version.save(update_fields=["status"])
    article.status = ArticleStatus.DRAFT
    article.save(update_fields=["status", "updated_at"])
    _audit(
        "ARTICLE_REVIEW_REVISION_REQUESTED", article, version, actor, reason, run,
        ArticleStatus.IN_REVIEW, ArticleStatus.DRAFT, review,
    )
    return article, version, True, review


def request_article_revision(article_version_id, reason, actor):
    return _execute("REVIEW_REVISION", article_version_id, reason, actor, _request_revision)


def validate_article_for_publish(article, version):
    if version.article_id != article.pk or version.article.event_id != article.event_id:
        raise ArticleReviewError("ArticleVersion relationship is invalid", "ARTICLE_VERSION_INVALID")
    if article.current_version_id != version.pk:
        raise ArticleReviewError("ArticleVersion is not current", "ARTICLE_VERSION_NOT_CURRENT")
    links = list(
        ArticleVersionClaim.objects.select_for_update()
        .filter(article_version=version).select_related("claim__event").order_by("pk")
    )
    if not links:
        raise ArticleReviewError("ArticleVersion has no Claim links", "ARTICLE_CLAIM_INVALID")
    for link in links:
        claim = link.claim
        if claim.event_id != article.event_id or claim.verification_status != VerificationStatus.SUPPORTED:
            raise ArticleReviewError("ArticleVersion contains an invalid Claim", "ARTICLE_CLAIM_INVALID")
    eligibility = evaluate_article_generation_eligibility(article.event_id)
    if not eligibility.eligible:
        if "SCORE" in eligibility.reason_code:
            code = "ARTICLE_SCORE_INVALID"
        elif "CLAIM" in eligibility.reason_code and "TRACEABLE" not in eligibility.reason_code:
            code = "ARTICLE_CLAIM_INVALID"
        else:
            code = "ARTICLE_EVIDENCE_INVALID"
        raise ArticleReviewError(
            f"Publication eligibility failed: {eligibility.reason_code}", code
        )
    allowed_claims = set(eligibility.eligible_claim_ids)
    allowed_evidence = set(eligibility.eligible_evidence_ids)
    for link in links:
        claim = link.claim
        if claim.pk not in allowed_claims:
            raise ArticleReviewError("ArticleVersion contains an ineligible Claim", "ARTICLE_CLAIM_INVALID")
        if not claim.evidence.filter(
            pk__in=allowed_evidence, stance=EvidenceStance.SUPPORTS
        ).exists():
            raise ArticleReviewError("Article Claim has no valid SUPPORTS Evidence", "ARTICLE_EVIDENCE_INVALID")
    _require_content_gate(version)
    return eligibility, links


def _publish(version_id, reason, actor, run):
    article, version = _locked_version(version_id)
    if (
        article.status == version.status == ArticleStatus.PUBLISHED
        and article.published_version_id == version.pk
    ):
        review = version.reviews.filter(decision=ReviewDecision.APPROVED).order_by("-reviewed_at", "-pk").first()
        return article, version, False, review
    _require_current(article, version)
    if article.status != ArticleStatus.APPROVED or version.status != ArticleStatus.APPROVED:
        raise ArticleReviewError("Only an APPROVED current version may be published", "ARTICLE_TRANSITION_INVALID")
    review = Review.objects.select_for_update().filter(
        article_version=version, decision=ReviewDecision.APPROVED
    ).order_by("-reviewed_at", "-pk").first()
    if review is None:
        raise ArticleReviewError("An approved Review is required", "ARTICLE_REVIEW_MISSING")
    eligibility, links = validate_article_for_publish(article, version)
    published_at = timezone.now()
    version.status = ArticleStatus.PUBLISHED
    version.save(update_fields=["status"])
    article.status = ArticleStatus.PUBLISHED
    article.published_version = version
    article.published_at = published_at
    article.save(update_fields=["status", "published_version", "published_at", "updated_at"])
    _audit(
        "ARTICLE_PUBLISHED", article, version, actor, reason, run,
        ArticleStatus.APPROVED, ArticleStatus.PUBLISHED, review,
        {"claim_ids": [link.claim_id for link in links], "eligibility_reason": eligibility.reason_code},
    )
    return article, version, True, review


def publish_article(article_version_id, reason, actor):
    return _execute("PUBLISH", article_version_id, reason, actor, _publish)


def _correct(version_id, reason, actor, run):
    article, version = _locked_version(version_id)
    _require_current(article, version)
    if article.status != ArticleStatus.PUBLISHED or version.status != ArticleStatus.PUBLISHED:
        raise ArticleReviewError("Only a PUBLISHED current version may be corrected", "ARTICLE_TRANSITION_INVALID")
    old_version_id = version.pk
    version.status = ArticleStatus.CORRECTED
    version.save(update_fields=["status"])
    revision = ArticleVersion.objects.create(
        article=article, version_number=article.versions.aggregate(maximum=models.Max("version_number"))["maximum"] + 1,
        title=version.title, one_sentence_summary=version.one_sentence_summary,
        what_happened=version.what_happened, why_it_matters=version.why_it_matters,
        who_is_affected=version.who_is_affected, example=version.example,
        source_note=version.source_note, language=version.language,
        status=ArticleStatus.DRAFT, created_by=actor,
    )
    old_links = list(version.claim_links.order_by("display_order", "pk"))
    ArticleVersionClaim.objects.bulk_create([
        ArticleVersionClaim(
            article_version=revision, claim_id=link.claim_id,
            paragraph_key=link.paragraph_key, display_order=link.display_order,
        )
        for link in old_links
    ])
    article.status = ArticleStatus.DRAFT
    article.current_version = revision
    article.corrected_at = timezone.now()
    article.save(update_fields=["status", "current_version", "corrected_at", "updated_at"])
    _audit(
        "ARTICLE_CORRECTED", article, version, actor, reason, run,
        ArticleStatus.PUBLISHED, ArticleStatus.CORRECTED,
        extra={"new_article_version_id": revision.pk, "article_status": ArticleStatus.DRAFT},
    )
    return article, revision, True, None


def correct_article(article_version_id, reason, actor):
    return _execute("CORRECT", article_version_id, reason, actor, _correct)


def _retract(version_id, reason, actor, run):
    article, version = _locked_version(version_id)
    if article.status == version.status == ArticleStatus.RETRACTED:
        return article, version, False, None
    _require_current(article, version)
    if article.status != ArticleStatus.PUBLISHED or version.status != ArticleStatus.PUBLISHED:
        raise ArticleReviewError("Only a PUBLISHED current version may be retracted", "ARTICLE_TRANSITION_INVALID")
    version.status = ArticleStatus.RETRACTED
    version.save(update_fields=["status"])
    article.status = ArticleStatus.RETRACTED
    article.retracted_at = timezone.now()
    article.save(update_fields=["status", "retracted_at", "updated_at"])
    _audit("ARTICLE_RETRACTED", article, version, actor, reason, run, ArticleStatus.PUBLISHED, ArticleStatus.RETRACTED)
    return article, version, True, None


def retract_article(article_version_id, reason, actor):
    return _execute("RETRACT", article_version_id, reason, actor, _retract)
