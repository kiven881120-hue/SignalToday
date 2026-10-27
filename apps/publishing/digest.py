import hashlib
import re
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.reviews.models import ReviewDecision
from apps.scoring.models import Score

from .models import (
    Article, ArticleStatus, ArticleVersion, Digest, DigestCategory, DigestItem,
    DigestStatus,
)
from .review import ArticleReviewError, validate_article_for_publish


JOB_TYPE = "DAILY_DIGEST"
STEPS = (
    "load_eligible_articles", "dedupe_events", "rank_articles",
    "classify_articles", "build_digest", "finalize_digest",
)
MAX_ITEMS = 12

RECOMMENDATION_MUST_KNOW = "MUST_KNOW"
RECOMMENDATION_PERSONALIZED = "PERSONALIZED"
RECOMMENDATION_DISCOVER = "DISCOVER"
RECOMMENDATION_TOPIC = "TOPIC"


class DailyDigestError(Exception):
    pass


@dataclass(frozen=True)
class DigestCandidate:
    article: Article
    article_version: ArticleVersion
    score: Score
    category: str
    recommendation_reason: str
    must_know: bool
    recommendation_lane: str


@dataclass(frozen=True)
class DigestExclusion:
    article_id: int
    article_version_id: int
    reason_code: str


@dataclass(frozen=True)
class DailyDigestResult:
    digest: Digest | None
    job_run: JobRun | None
    changed: bool
    dry_run: bool
    candidates: tuple[DigestCandidate, ...]
    selected: tuple[DigestCandidate, ...]
    exclusions: tuple[DigestExclusion, ...]


def _lock_date(digest_date):
    if connection.vendor != "postgresql":
        return
    digest = hashlib.sha256(f"daily-digest:{digest_date.isoformat()}".encode()).digest()
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def _reason(score):
    bits = []
    if score.importance >= 8:
        bits.append("重要性高")
    if score.credibility >= 8:
        bits.append("来源可靠")
    if score.information_gain >= 8:
        bits.append("信息增量高")
    if score.user_relevance >= 8:
        bits.append("现实关联度高")
    if score.freshness >= 8:
        bits.append("信息较新")
    if score.recommendation_level == "FOCUS":
        return "、".join(bits or ["综合信息价值较高"]) + "。"
    return "、".join(bits or ["已通过事实与发布审核，按当前优先级展示"]) + "。"


def classify_recommendation_lane(
    score, *, has_explicit_interest=False, has_potential_interest=False
):
    """Separate recommendation purpose from the unchanged aggregate Score."""
    if (
        score.recommendation_level == "FOCUS"
        and score.final_score >= 8
        and score.importance >= 8
        and (score.user_relevance >= 7 or score.long_term_impact >= 7)
    ):
        return RECOMMENDATION_MUST_KNOW
    if has_explicit_interest:
        return RECOMMENDATION_PERSONALIZED
    if has_potential_interest or (
        score.information_gain >= 7 and score.user_relevance < 7
    ):
        return RECOMMENDATION_DISCOVER
    return RECOMMENDATION_TOPIC


def _category(article, score, *, has_explicit_interest=False, has_potential_interest=False):
    lane = classify_recommendation_lane(
        score,
        has_explicit_interest=has_explicit_interest,
        has_potential_interest=has_potential_interest,
    )
    if lane == RECOMMENDATION_PERSONALIZED:
        return DigestCategory.PERSONALIZED, lane
    if lane == RECOMMENDATION_DISCOVER:
        return DigestCategory.DISCOVER, lane
    version = article.published_version
    text = " ".join([
        article.event.title or "", version.title if version else "",
        version.what_happened if version else "",
    ]).casefold()
    mappings = (
        (DigestCategory.AI_TECH, ("ai", "software", "technology", "computing", "developer", "robotics")),
        (DigestCategory.AUTO, ("automotive", "vehicle", "ev", "automobile", "汽车", "电动车")),
        (DigestCategory.FINANCE, ("finance", "economy", "market", "banking", "monetary", "财经", "经济")),
        (DigestCategory.SCIENCE, ("science", "space", "research", "科学", "航天")),
        (DigestCategory.EDUCATION, ("education", "school", "university", "教育", "学校")),
        (DigestCategory.HEALTH, ("health", "medicine", "public health", "健康", "医疗")),
        (DigestCategory.WORLD, ("international", "geopolitics", "foreign affairs", "global", "世界", "国际")),
    )

    def contains(keyword):
        if keyword.isascii() and keyword.replace(" ", "").isalpha():
            return re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", text) is not None
        return keyword in text

    for category, keywords in mappings:
        if any(contains(keyword) for keyword in keywords):
            return category, lane
    return DigestCategory.GENERAL, lane


def _load_candidates(digest_date):
    candidates = []
    exclusions = []
    seen_events = set()
    historical_version_ids = set(
        DigestItem.objects.values_list("article_version_id", flat=True)
    )
    with transaction.atomic():
        articles = Article.objects.filter(
            status=ArticleStatus.PUBLISHED,
            current_version_id=F("published_version_id"),
            published_version__status=ArticleStatus.PUBLISHED,
            published_version__isnull=False,
            published_at__isnull=False,
            event__isnull=False,
        ).select_related("event", "published_version").prefetch_related(
            "published_version__reviews"
        ).order_by("pk")
        for article in articles:
            if article.published_version_id in historical_version_ids:
                exclusions.append(DigestExclusion(
                    article_id=article.pk,
                    article_version_id=article.published_version_id,
                    reason_code="EXCLUDED_ALREADY_IN_HISTORICAL_DIGEST",
                ))
                continue
            if article.event_id in seen_events:
                continue
            if not any(
                review.decision == ReviewDecision.APPROVED
                for review in article.published_version.reviews.all()
            ):
                continue
            try:
                eligibility, _ = validate_article_for_publish(article, article.published_version)
            except ArticleReviewError:
                continue
            score = Score.objects.filter(pk=eligibility.score_id, event=article.event).first()
            if score is None or score.status != "CALCULATED":
                continue
            if article.published_at and article.published_at.date() > digest_date:
                continue
            candidates.append((article, score))
            seen_events.add(article.event_id)
    candidates.sort(key=lambda item: (
        0 if item[1].recommendation_level == "FOCUS" else 1,
        -item[1].final_score,
        -(item[0].published_at.timestamp() if item[0].published_at else 0),
        item[0].pk,
    ))
    output = []
    for article, score in candidates:
        category, lane = _category(article, score)
        output.append(DigestCandidate(
            article, article.published_version, score, category,
            _reason(score), lane == RECOMMENDATION_MUST_KNOW, lane,
        ))
    must_indexes = [index for index, candidate in enumerate(output) if candidate.must_know][:3]
    candidates = tuple(
        DigestCandidate(
            item.article, item.article_version, item.score,
            DigestCategory.MUST_KNOW if index in must_indexes else item.category,
            item.recommendation_reason, index in must_indexes,
            RECOMMENDATION_MUST_KNOW if index in must_indexes else item.recommendation_lane,
        )
        for index, item in enumerate(output)
    )
    return candidates, tuple(exclusions)


def _steps(run, event_date):
    JobStep.objects.bulk_create([
        JobStep(job_run=run, step_name=name, input_summary={"digest_date": event_date.isoformat()})
        for name in STEPS
    ])


def _step_done(step, output):
    step.status = JobStatus.SUCCESS
    step.finished_at = timezone.now()
    step.output_summary = output
    step.save(update_fields=["status", "finished_at", "output_summary"])


def _skip(step, reason):
    step.status = JobStatus.CANCELLED
    step.finished_at = timezone.now()
    step.output_summary = {"skipped": True, "reason": reason}
    step.save(update_fields=["status", "finished_at", "output_summary"])


def _start(step):
    step.status = JobStatus.RUNNING
    step.started_at = timezone.now()
    step.save(update_fields=["status", "started_at"])


def _finish(run, status=JobStatus.SUCCESS, error=""):
    run.status = status
    run.finished_at = timezone.now()
    run.error_message = error
    run.save(update_fields=["status", "finished_at", "error_message"])


def _fail(run, active_step, exc):
    now = timezone.now()
    error = str(exc)[:2000]
    if active_step:
        active_step.status = JobStatus.FAILED
        active_step.finished_at = now
        active_step.error_message = error
        active_step.save(update_fields=["status", "finished_at", "error_message"])
    for step in run.steps.filter(status=JobStatus.PENDING):
        _skip(step, "UPSTREAM_FAILURE")
    _finish(run, JobStatus.FAILED, error)


def _stored_selection(digest):
    selected = []
    for item in digest.items.select_related(
        "article", "article__event", "article_version"
    ).order_by("rank"):
        score = Score.objects.filter(
            event=item.article.event, rule_version__active=True
        ).order_by("-rule_version__created_at", "-rule_version_id").first()
        if score:
            selected.append(DigestCandidate(
                item.article, item.article_version, score, item.category,
                item.recommendation_reason, item.category == DigestCategory.MUST_KNOW,
                (
                    RECOMMENDATION_MUST_KNOW
                    if item.category == DigestCategory.MUST_KNOW
                    else RECOMMENDATION_PERSONALIZED
                    if item.category == DigestCategory.PERSONALIZED
                    else RECOMMENDATION_DISCOVER
                    if item.category == DigestCategory.DISCOVER
                    else RECOMMENDATION_TOPIC
                ),
            ))
    return tuple(selected)


def _acquire_run(digest_date, rebuild):
    key = f"daily-digest:{digest_date.isoformat()}"
    with transaction.atomic():
        _lock_date(digest_date)
        digest, created = Digest.objects.get_or_create(
            digest_date=digest_date, defaults={"status": DigestStatus.DRAFT}
        )
        digest = Digest.objects.select_for_update().get(pk=digest.pk)
        base_run = JobRun.objects.filter(idempotency_key=key).first()
        if digest.status == DigestStatus.READY:
            if rebuild:
                raise DailyDigestError("A READY Digest cannot be rebuilt")
            return digest, base_run, False, created
        if JobRun.objects.filter(
            job_type=JOB_TYPE, idempotency_key__startswith=key, status=JobStatus.RUNNING
        ).exists():
            raise DailyDigestError("Daily Digest generation is already running")
        run_key = key if base_run is None else f"{key}:rebuild:{uuid4().hex}"
        run = JobRun.objects.create(
            job_type=JOB_TYPE, status=JobStatus.RUNNING, started_at=timezone.now(),
            idempotency_key=run_key,
        )
        _steps(run, digest_date)
        if created:
            AuditLog.objects.create(
                action="DIGEST_CREATED", object_type="Digest", object_id=str(digest.pk),
                after_data={
                    "digest_id": digest.pk, "digest_date": digest_date.isoformat(),
                    "job_run_id": run.pk,
                },
            )
        return digest, run, True, created


def generate_daily_digest(digest_date, *, dry_run=False, rebuild=False):
    if isinstance(digest_date, str):
        digest_date = date.fromisoformat(digest_date)
    if dry_run and rebuild:
        raise DailyDigestError("--dry-run and --rebuild cannot be combined")
    candidates, exclusions = _load_candidates(digest_date)
    selected = candidates[:MAX_ITEMS]
    if dry_run:
        return DailyDigestResult(
            None, None, False, True, candidates, selected, exclusions
        )
    digest, run, acquired, _ = _acquire_run(digest_date, rebuild)
    if not acquired:
        return DailyDigestResult(
            digest, run, False, False, candidates, _stored_selection(digest),
            exclusions,
        )
    active_step = None
    try:
        # Recalculate after acquiring the date lock so the persisted selection is current.
        candidates, exclusions = _load_candidates(digest_date)
        selected = candidates[:MAX_ITEMS]
        with transaction.atomic():
            digest = Digest.objects.select_for_update().get(pk=digest.pk)
            locked_candidates = []
            concurrent_exclusions = []
            for item in candidates:
                article = Article.objects.select_for_update().get(pk=item.article.pk)
                version = ArticleVersion.objects.select_for_update().get(
                    pk=item.article_version.pk
                )
                if DigestItem.objects.filter(article_version_id=version.pk).exists():
                    concurrent_exclusions.append(DigestExclusion(
                        article_id=article.pk,
                        article_version_id=version.pk,
                        reason_code="EXCLUDED_ALREADY_IN_HISTORICAL_DIGEST",
                    ))
                    continue
                if (
                    article.status != ArticleStatus.PUBLISHED
                    or article.current_version_id != version.pk
                    or article.published_version_id != version.pk
                    or version.article_id != article.pk
                    or version.status != ArticleStatus.PUBLISHED
                    or not version.reviews.filter(
                        decision=ReviewDecision.APPROVED
                    ).exists()
                ):
                    raise DailyDigestError(
                        "Article published version changed during Digest generation"
                    )
                validate_article_for_publish(article, version)
                locked_candidates.append(DigestCandidate(
                    article, version, item.score, item.category,
                    item.recommendation_reason, item.must_know, item.recommendation_lane,
                ))
            candidates = tuple(locked_candidates)
            selected = candidates[:MAX_ITEMS]
            exclusions = tuple(exclusions) + tuple(concurrent_exclusions)
            stages = [
                ("load_eligible_articles", {"candidate_count": len(candidates)}),
                ("dedupe_events", {"selected_event_count": len({item.article.event_id for item in selected})}),
                ("rank_articles", {"selected_count": len(selected)}),
                ("classify_articles", {"categories": {category: sum(item.category == category for item in selected) for category in DigestCategory.values}}),
            ]
            for name, output in stages:
                active_step = run.steps.get(step_name=name)
                _start(active_step)
                _step_done(active_step, output)
            active_step = run.steps.get(step_name="build_digest")
            _start(active_step)
            if rebuild:
                old_items = list(digest.items.all())
                for item in old_items:
                    AuditLog.objects.create(
                        action="DIGEST_ITEM_REMOVED", object_type="DigestItem", object_id=str(item.pk),
                        after_data={
                            "digest_id": digest.pk, "article_id": item.article_id,
                            "article_version_id": item.article_version_id,
                            "job_run_id": run.pk,
                        },
                    )
                digest.items.all().delete()
                AuditLog.objects.create(
                    action="DIGEST_REBUILT", object_type="Digest", object_id=str(digest.pk),
                    after_data={"job_run_id": run.pk, "removed_count": len(old_items)},
                )
            digest.status = DigestStatus.DRAFT
            digest.save(update_fields=["status", "updated_at"])
            for rank, item in enumerate(selected, 1):
                digest_item = DigestItem.objects.create(
                    digest=digest, article=item.article,
                    article_version=item.article_version,
                    category=item.category, rank=rank,
                    recommendation_reason=item.recommendation_reason,
                )
                AuditLog.objects.create(
                    action="DIGEST_ITEM_CREATED", object_type="DigestItem", object_id=str(digest_item.pk),
                    after_data={
                        "digest_id": digest.pk, "article_id": item.article.pk,
                        "article_version_id": item.article_version.pk,
                        "event_id": item.article.event_id, "rank": rank,
                        "category": item.category, "reason": item.recommendation_reason,
                        "job_run_id": run.pk,
                    },
                )
            _step_done(active_step, {"digest_id": digest.pk, "item_count": len(selected)})
            active_step = run.steps.get(step_name="finalize_digest")
            _start(active_step)
            digest.status = DigestStatus.READY
            digest.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(
                action="DIGEST_READY", object_type="Digest", object_id=str(digest.pk),
                after_data={
                    "digest_id": digest.pk, "digest_date": digest_date.isoformat(),
                    "item_count": len(selected), "job_run_id": run.pk,
                },
            )
            _step_done(active_step, {"digest_id": digest.pk, "status": digest.status})
        _finish(run)
        return DailyDigestResult(
            digest, run, True, False, candidates, selected, exclusions
        )
    except Exception as exc:
        _fail(run, active_step, exc)
        if isinstance(exc, DailyDigestError):
            raise
        raise DailyDigestError(str(exc)) from exc
