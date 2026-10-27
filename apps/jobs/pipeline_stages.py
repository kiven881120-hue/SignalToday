from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from django.core.management import call_command
from apps.ai_content.costs import check_budget
from apps.ai_content.models import ClaimCandidate
from apps.claims.models import Claim
from apps.events.models import Event
from apps.events.service import EventClusteringService
from apps.ingestion.errors import ErrorCode, IngestionError
from apps.ingestion.models import RawItem
from apps.ingestion.service import FeedIngestionService
from apps.jobs.contracts import JobStatus
from apps.publishing.digest import generate_daily_digest
from apps.publishing.eligibility import evaluate_article_generation_eligibility
from apps.publishing.generation import ArticleGenerationError, generate_article
from apps.publishing.grounding import GroundingStatus, validate_article_grounding
from apps.publishing.models import Article, ArticleStatus, Digest, DigestItem
from apps.reviews.models import Review, ReviewDecision
from apps.scoring.evaluation import EventScoringService
from apps.sources.models import Source

from .models import JobRun
from .pipeline import IntegrityStageError, StageOutcome, TransientStageError


def _budget_gate():
    decision = check_budget(0, essential=False)
    if not decision.allowed:
        return StageOutcome(
            status=(
                JobStatus.PAUSED_BUDGET_EXCEEDED
                if decision.status == "PAUSED_BUDGET_EXCEEDED"
                else JobStatus.PAUSED
            ),
            metadata={
                "budget_status": decision.status,
                "spent": str(decision.spent),
                "projected": str(decision.projected),
            },
            error="LLM budget gate blocked low-priority work",
        )
    return None


def _ingestion_error_code(exc):
    if exc.code == ErrorCode.TIMEOUT:
        return "TIMEOUT"
    if exc.code == ErrorCode.NETWORK_ERROR:
        return "NETWORK_ERROR"
    if exc.code == ErrorCode.HTTP_ERROR and exc.status_code == 429:
        return "HTTP_429"
    if exc.code == ErrorCode.HTTP_ERROR and exc.status_code and exc.status_code >= 500:
        return "HTTP_5XX"
    return str(exc.code)


def source_ingestion(business_date):
    sources = list(Source.objects.filter(active=True).order_by("identifier", "pk"))
    results = []
    for source in sources:
        base_key = f"pipeline:{business_date.isoformat()}:ingestion:{source.identifier or source.pk}"
        previous = JobRun.objects.filter(idempotency_key__startswith=base_key).order_by("-pk").first()
        if previous and previous.status == JobStatus.SUCCESS:
            key = previous.idempotency_key
        elif previous:
            key = f"{base_key}:retry:{JobRun.objects.filter(idempotency_key__startswith=base_key).count()}"
        else:
            key = base_key
        try:
            run, stats = FeedIngestionService().ingest_source(source, idempotency_key=key)
            results.append({"source_id": source.pk, "status": run.status, **stats.as_dict()})
        except IngestionError as exc:
            results.append({
                "source_id": source.pk,
                "status": JobStatus.FAILED,
                "error_code": _ingestion_error_code(exc),
            })
    succeeded = sum(item["status"] == JobStatus.SUCCESS for item in results)
    failed = len(results) - succeeded
    if results and not succeeded:
        code = results[0].get("error_code", "STAGE_ERROR")
        if code in {"TIMEOUT", "NETWORK_ERROR", "HTTP_429", "HTTP_5XX"}:
            raise TransientStageError("All active sources failed temporarily", code=code)
        return StageOutcome(JobStatus.FAILED, len(sources), 0, {"sources": results}, "All active sources failed")
    return StageOutcome(
        JobStatus.SUCCESS,
        len(sources),
        sum(item.get("created", 0) for item in results),
        {"sources": results, "failed_source_count": failed},
    )


def event_clustering(business_date):
    queryset = RawItem.objects.filter(event__isnull=True).order_by("discovered_at", "pk")[:1000]
    stats, _, run = EventClusteringService().run(queryset)
    return StageOutcome(
        JobStatus.SUCCESS,
        stats.input_count,
        stats.new_events + stats.auto_assigned,
        {
            "business_date": business_date.isoformat(),
            "service_job_run_id": run.pk,
            "candidate_failure_count": stats.errors,
            **stats.as_dict(),
        },
    )


def claim_analysis(business_date):
    budget = _budget_gate()
    if budget:
        return budget
    before = ClaimCandidate.objects.count()
    event_count = Event.objects.filter(raw_items__isnull=False).distinct().count()
    output = StringIO()
    call_command("analyze_events", purpose="claims", limit=1000, stdout=output)
    service_run = JobRun.objects.filter(job_type="LLM_EVENT_ANALYSIS").order_by("-pk").first()
    after = ClaimCandidate.objects.count()
    status = service_run.status if service_run else JobStatus.FAILED
    return StageOutcome(
        status,
        event_count,
        after - before,
        {
            "business_date": business_date.isoformat(),
            "command": "analyze_events --purpose claims",
            "service_job_run_id": service_run.pk if service_run else None,
        },
        service_run.error_message if service_run and status != JobStatus.SUCCESS else "",
    )


def claim_evaluation(business_date):
    claims = Claim.objects.order_by("pk")
    stats, _, run = __import__(
        "apps.claims.evaluation", fromlist=["ClaimEvaluationService"]
    ).ClaimEvaluationService().run(claims)
    status = JobStatus.SUCCESS if not stats["errors"] else JobStatus.FAILED
    return StageOutcome(
        status,
        stats["input_count"],
        stats["input_count"] - stats["errors"],
        {"business_date": business_date.isoformat(), "service_job_run_id": run.pk, **stats},
        "Claim evaluation had errors" if stats["errors"] else "",
    )


def importance_review_gate(business_date):
    proposed = ClaimCandidate.objects.filter(status="PROPOSED").count()
    missing = Claim.objects.filter(importance__isnull=True).count()
    total = Claim.objects.count()
    return StageOutcome(
        JobStatus.SUCCESS,
        total + proposed,
        total - missing,
        {
            "proposed_candidates": proposed,
            "claims_missing_importance": missing,
            "deferred_for_human_review": proposed + missing,
        },
    )


def scoring(business_date):
    events = Event.objects.filter(claims__isnull=False).distinct().order_by("pk")
    stats, _, run = EventScoringService().run(events)
    status = JobStatus.SUCCESS if not stats["error_count"] else JobStatus.FAILED
    return StageOutcome(
        status,
        stats["input_count"],
        stats["scored_count"],
        {"business_date": business_date.isoformat(), "service_job_run_id": run.pk, **stats},
        "Scoring had errors" if stats["error_count"] else "",
    )


def article_generation(business_date):
    budget = _budget_gate()
    if budget:
        return budget
    event_ids = [
        event.pk for event in Event.objects.order_by("pk")
        if evaluate_article_generation_eligibility(event.pk).eligible
        and not Article.objects.filter(event=event).exists()
    ]
    created = failures = 0
    details = []
    for event_id in event_ids:
        try:
            result = generate_article(event_id)
            created += int(bool(result.article and not result.reused))
            details.append({"event_id": event_id, "status": result.job_run.status})
        except ArticleGenerationError as exc:
            failures += 1
            details.append({"event_id": event_id, "status": JobStatus.FAILED, "error": str(exc)[:300]})
    status = JobStatus.FAILED if event_ids and failures == len(event_ids) else JobStatus.SUCCESS
    return StageOutcome(
        status,
        len(event_ids),
        created,
        {"candidates": details, "candidate_failure_count": failures},
        "All Article candidates failed" if status == JobStatus.FAILED else "",
    )


def article_validation(business_date):
    versions = [
        article.current_version
        for article in Article.objects.select_related("current_version").filter(status=ArticleStatus.DRAFT)
        if article.current_version_id
    ]
    failures = []
    for version in versions:
        grounding = validate_article_grounding(version)
        if grounding.status != GroundingStatus.PASS:
            failures.append({
                "article_version_id": version.pk,
                "gate": "GROUNDING",
                "status": grounding.status.value,
                "reason_codes": list(grounding.reason_codes),
            })
        else:
            failures.append({
                "article_version_id": version.pk,
                "gate": "FACT_DRIFT_QUALITY",
                "status": "MANUAL_REVIEW_REQUIRED",
                "reason_codes": ["NO_PERSISTED_QUALITY_DECISION"],
            })
    return StageOutcome(
        JobStatus.SUCCESS,
        len(versions),
        len(versions) - len(failures),
        {
            "failures": failures,
            "policy": "Grounding is recomputed; Fact Drift and Quality require an existing validated workflow or human review",
        },
    )


def article_review_gate(business_date):
    pending_reviews = Review.objects.filter(decision=ReviewDecision.PENDING).count()
    in_review = Article.objects.filter(status=ArticleStatus.IN_REVIEW).count()
    approved = Article.objects.filter(status=ArticleStatus.APPROVED).count()
    draft = Article.objects.filter(status=ArticleStatus.DRAFT).count()
    return StageOutcome(
        JobStatus.SUCCESS,
        Article.objects.count(),
        approved,
        {
            "approved_articles": approved,
            "pending_reviews": pending_reviews,
            "in_review_articles": in_review,
            "draft_articles": draft,
            "deferred_for_human_review": draft + in_review,
            "scheduler_approved_count": 0,
        },
    )


def publish_gate(business_date):
    awaiting_authorization = Article.objects.filter(status=ArticleStatus.APPROVED).count()
    published = Article.objects.filter(status=ArticleStatus.PUBLISHED).count()
    return StageOutcome(
        JobStatus.SUCCESS,
        Article.objects.count(),
        published,
        {
            "published_articles": published,
            "awaiting_publish_authorization": awaiting_authorization,
            "deferred_for_human_authorization": awaiting_authorization,
            "scheduler_published_count": 0,
        },
    )


def _illegal_digest_items():
    invalid = []
    valid_snapshot_statuses = {ArticleStatus.PUBLISHED, ArticleStatus.CORRECTED}
    for item in DigestItem.objects.select_related("article_version"):
        if (
            item.article_version.article_id != item.article_id
            or item.article_version.status not in valid_snapshot_statuses
        ):
            invalid.append(item.pk)
    return invalid


def daily_digest(business_date):
    invalid = _illegal_digest_items()
    if invalid:
        raise IntegrityStageError(f"Illegal DigestItem version binding: {invalid}")
    dry_run = generate_daily_digest(business_date, dry_run=True, rebuild=False)
    budget = check_budget(0, essential=True)
    existing = Digest.objects.filter(digest_date=business_date).first()
    metadata = {
        "dry_run_first": True,
        "candidate_count": len(dry_run.candidates),
        "selected_count": len(dry_run.selected),
        "article_versions": [item.article_version.pk for item in dry_run.selected],
        "scores": [str(item.score.final_score) for item in dry_run.selected],
        "categories": [item.category for item in dry_run.selected],
        "recommendations": [item.score.recommendation_level for item in dry_run.selected],
        "budget_status": budget.status,
        "existing_status": existing.status if existing else None,
        "illegal_digest_item_count": 0,
    }
    result = generate_daily_digest(business_date, rebuild=False)
    metadata.update({"digest_id": result.digest.pk, "changed": result.changed})
    return StageOutcome(JobStatus.SUCCESS, len(dry_run.candidates), len(result.selected), metadata)


def public_health_check(business_date):
    paths = (
        "/health/", "/", "/today/", f"/digest/{business_date.isoformat()}/",
    )
    statuses = {}
    for path in paths:
        try:
            with urlopen(f"http://127.0.0.1:8000{path}", timeout=5) as response:
                statuses[path] = response.status
        except HTTPError as exc:
            statuses[path] = exc.code
        except (URLError, TimeoutError) as exc:
            raise TransientStageError(str(exc), code="DEPENDENCY_UNAVAILABLE") from exc
    failed = {path: status for path, status in statuses.items() if status not in {200, 301}}
    if failed:
        return StageOutcome(JobStatus.FAILED, len(paths), len(paths) - len(failed), {"statuses": statuses}, "Public Web health check failed")
    return StageOutcome(JobStatus.SUCCESS, len(paths), len(paths), {"statuses": statuses})
