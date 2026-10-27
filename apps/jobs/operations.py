from datetime import time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone

from apps.ai_content.models import ModelCall, ModelCallStatus
from apps.claims.models import Claim, ClaimEvidence
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.publishing.grounding import validate_article_grounding
from apps.publishing.models import Article, ArticleStatus, ArticleVersion, Digest, DigestItem, DigestStatus
from apps.reviews.models import Review
from apps.scoring.models import Score
from apps.sources.models import Source

from .contracts import JobStatus
from .models import JobRun, JobStep
from .pipeline import _local_now
from .pipeline_registry import PIPELINE_STAGES


def _local_iso(value):
    return timezone.localtime(value).isoformat() if value else None


def _status_counts(queryset, field="status"):
    return dict(queryset.values_list(field).annotate(count=Count("pk")).order_by(field))


def _source_status(source):
    attempts = JobStep.objects.filter(
        step_name="source_validation", input_summary__source_id=source.pk
    ).select_related("job_run").order_by("-created_at", "-pk")
    last_attempt = attempts.first()
    last_success = attempts.filter(job_run__status=JobStatus.SUCCESS).first()
    return {
        "id": source.pk,
        "identifier": source.identifier,
        "active": source.active,
        "last_attempt": _local_iso(last_attempt.job_run.started_at) if last_attempt else None,
        "last_success": _local_iso(last_success.job_run.finished_at) if last_success else None,
        "failure_count": attempts.filter(job_run__status=JobStatus.FAILED).count(),
        "raw_item_count": source.raw_items.count(),
    }


def _consecutive_failures(queryset):
    count = 0
    for status in queryset.order_by("-created_at", "-pk").values_list("status", flat=True):
        if status != JobStatus.FAILED:
            break
        count += 1
    return count


def _pipeline_status(now):
    cutoff = now - timedelta(minutes=settings.SCHEDULER_STALE_JOB_MINUTES)
    rows = []
    for stage in PIPELINE_STAGES:
        runs = JobRun.objects.filter(job_type=stage.job_type)
        success = runs.filter(status=JobStatus.SUCCESS).order_by("-finished_at", "-pk").first()
        failure = runs.filter(status=JobStatus.FAILED).order_by("-finished_at", "-pk").first()
        running = runs.filter(status=JobStatus.RUNNING).order_by("-started_at", "-pk")
        rows.append({
            "stage": stage.name,
            "last_success": _local_iso(success.finished_at) if success else None,
            "last_failure": _local_iso(failure.finished_at) if failure else None,
            "current_running": running.count(),
            "stale_running": running.filter(started_at__lt=cutoff).count(),
            "consecutive_failures": _consecutive_failures(runs),
        })
    return rows


def _article_status():
    statuses = _status_counts(Article.objects.all())
    grounding = {"PASS": 0, "REVISION_REQUIRED": 0, "FAIL": 0, "ERROR": 0}
    for article in Article.objects.select_related("current_version").exclude(current_version=None):
        try:
            result = validate_article_grounding(article.current_version)
            grounding[result.status.value] = grounding.get(result.status.value, 0) + 1
        except Exception:
            grounding["ERROR"] += 1
    return {
        "DRAFT": statuses.get(ArticleStatus.DRAFT, 0),
        "IN_REVIEW": statuses.get(ArticleStatus.IN_REVIEW, 0),
        "APPROVED": statuses.get(ArticleStatus.APPROVED, 0),
        "PUBLISHED": statuses.get(ArticleStatus.PUBLISHED, 0),
        "REVISION_REQUIRED": grounding.get("REVISION_REQUIRED", 0),
        "grounding": grounding,
        "pending_reviews": Review.objects.filter(decision="PENDING").count(),
    }


def _illegal_digest_count():
    valid_snapshot_statuses = {ArticleStatus.PUBLISHED, ArticleStatus.CORRECTED}
    return sum(
        item.article_version.article_id != item.article_id
        or item.article_version.status not in valid_snapshot_statuses
        for item in DigestItem.objects.select_related("article_version")
    )


def _digest_status(business_date, now):
    digest = Digest.objects.filter(digest_date=business_date).first()
    first_score = None
    item_count = 0
    if digest:
        item_count = digest.items.count()
        first = digest.items.select_related("article__event").order_by("rank").first()
        if first:
            score = Score.objects.filter(
                event=first.article.event, status="CALCULATED", rule_version__active=True
            ).order_by("-rule_version__created_at", "-pk").first()
            first_score = str(score.final_score) if score else None
    deadline_missed = (
        business_date == now.date()
        and now.time().replace(tzinfo=None) >= time(8, 5)
        and (digest is None or digest.status != DigestStatus.READY)
    )
    return {
        "exists": digest is not None,
        "id": digest.pk if digest else None,
        "status": digest.status if digest else None,
        "item_count": item_count,
        "first_score": first_score,
        "illegal_version_count": _illegal_digest_count(),
        "deadline_missed": deadline_missed,
    }


def _llm_status(now):
    calls = ModelCall.objects.filter(created_at__date=timezone.localdate(now))
    actual = calls.aggregate(total=Sum("actual_cost"))["total"] or Decimal("0")
    estimated = calls.aggregate(total=Sum("estimated_cost"))["total"] or Decimal("0")
    budget = Decimal(str(settings.LLM_DAILY_BUDGET_CNY))
    statuses = _status_counts(calls)
    return {
        "count": calls.count(),
        "actual_cost": str(actual),
        "estimated_cost": str(estimated),
        "budget_cny": str(budget),
        "budget_percentage": str((actual / budget * 100).quantize(Decimal("0.001")) if budget else 0),
        "timeout": statuses.get(ModelCallStatus.TIMEOUT, 0),
        "failed": statuses.get(ModelCallStatus.FAILED, 0),
        "paused": statuses.get(ModelCallStatus.PAUSED, 0) + statuses.get(ModelCallStatus.PAUSED_BUDGET_EXCEEDED, 0),
    }


def get_operations_status(*, business_date=None, now=None):
    local_now = _local_now(now)
    business_date = business_date or local_now.date()
    return {
        "read_only": True,
        "captured_at": local_now.isoformat(),
        "business_date": business_date.isoformat(),
        "timezone": str(local_now.tzinfo),
        "pipeline_enabled": bool(settings.SCHEDULER_PIPELINE_ENABLED),
        "sources": {
            "active_count": Source.objects.filter(active=True).count(),
            "items": [_source_status(source) for source in Source.objects.order_by("pk")],
        },
        "data": {
            "raw_items": RawItem.objects.count(),
            "events": Event.objects.count(),
            "claims": Claim.objects.count(),
            "claim_evidence": ClaimEvidence.objects.count(),
            "articles": Article.objects.count(),
            "article_versions": ArticleVersion.objects.count(),
        },
        "pipeline": _pipeline_status(local_now),
        "articles": _article_status(),
        "digest": _digest_status(business_date, local_now),
        "llm": _llm_status(local_now),
    }
