from uuid import uuid4

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.settings.models import SiteSettings

from .models import ArticleVersion
from .review import ArticleReviewError, approve_article, publish_article, submit_article_for_review
from .risk import assess_article_risk


AUTO_PUBLISH_USERNAME = "signaltoday-auto-publish"
JOB_TYPE = "ARTICLE_AUTO_PUBLISH"


def _system_actor():
    user, created = get_user_model().objects.get_or_create(
        username=AUTO_PUBLISH_USERNAME,
        defaults={"is_active": True, "is_staff": False, "is_superuser": False},
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


def _new_job(article_version_id):
    run = JobRun.objects.create(
        job_type=JOB_TYPE, status=JobStatus.RUNNING, started_at=timezone.now(),
        idempotency_key=f"article-auto-publish:{article_version_id}:{uuid4().hex}",
    )
    step = JobStep.objects.create(
        job_run=run, step_name="evaluate_and_publish", status=JobStatus.RUNNING,
        started_at=timezone.now(), input_summary={"article_version_id": article_version_id},
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


def _audit_decision(version, risk, *, fired, automatic_publish_enabled):
    AuditLog.objects.create(
        action="ARTICLE_AUTO_PUBLISH_EVALUATED", object_type="ArticleVersion",
        object_id=str(version.pk),
        after_data={
            "article_id": version.article_id, "article_version_id": version.pk,
            "risk_level": risk.risk_level, "reason_code": risk.reason_code,
            "reason": risk.reason, "fired": fired,
            "automatic_publish_enabled": automatic_publish_enabled,
        },
    )


def _run_auto_publish(version, risk, actor):
    reason = (
        f"Automatic publish: risk_level={risk.risk_level}, "
        f"reason_code={risk.reason_code}"
    )
    submit_article_for_review(version.pk, reason, actor)
    approve_article(version.pk, reason, actor)
    return publish_article(version.pk, reason, actor)


def evaluate_and_maybe_auto_publish(article_version_id):
    """Evaluate an ArticleVersion's risk and auto-publish it if eligible.

    Never raises: any failure is captured in its own JobRun/JobStep and
    AuditLog so a successful generation result is never corrupted by this
    separate, later decision.
    """
    run, step = _new_job(article_version_id)
    try:
        version = ArticleVersion.objects.select_related("article").filter(pk=article_version_id).first()
        if version is None:
            _finish_job(run, step, JobStatus.FAILED, error="ArticleVersion does not exist")
            return
        site_settings = SiteSettings.load()
        risk = assess_article_risk(version)
        fired = False
        if site_settings.automatic_publish_enabled and risk.risk_level == "LOW":
            actor = _system_actor()
            try:
                _run_auto_publish(version, risk, actor)
                fired = True
            except ArticleReviewError:
                fired = False
        _audit_decision(
            version, risk, fired=fired,
            automatic_publish_enabled=site_settings.automatic_publish_enabled,
        )
        _finish_job(run, step, JobStatus.SUCCESS, output={
            "article_version_id": article_version_id, "risk_level": risk.risk_level,
            "reason_code": risk.reason_code, "fired": fired,
        })
    except Exception as exc:
        _finish_job(run, step, JobStatus.FAILED, error=str(exc)[:2000])
