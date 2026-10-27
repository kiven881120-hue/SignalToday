import hashlib
import json
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun

from .grounding import (
    ContentUnitType,
    GroundingStatus,
    validate_article_content_grounding,
    validate_article_grounding,
)
from .models import Article, ArticleStatus, ArticleVersion, ArticleVersionClaim
from .quality import (
    ArticleQualityInput,
    ArticleQualityMode,
    ExampleStatus,
    InformationGainStatus,
    InformationKind,
    InformationUnit,
    QualityLevel,
    QualitySection,
    QualityStatus,
    WhoStatus,
    WhyStatus,
    evaluate_article_quality,
)


CONTROLLED_ARTICLE_ID = 1
CONTROLLED_SOURCE_VERSION_ID = 3
CONTROLLED_SOURCE_CONTENT_HASH = (
    "d130157e8490a21380acc7be0948d3230010f65973157aef3d33cd958705e690"
)
AUDIT_ACTION = "ARTICLE_VERSION_DOWNGRADED_TO_BRIEF"
JOB_TYPE = "ARTICLE_BRIEF_DOWNGRADE"
DOWNGRADE_REASON = (
    "JOB-90 confirmed reliable evidence only supports a concise brief; "
    "Why and Example provide no independent information gain."
)


class ArticleBriefDowngradeError(Exception):
    def __init__(self, message, code="ARTICLE_BRIEF_DOWNGRADE_FAILED"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ArticleBriefDowngradeResult:
    source_version: ArticleVersion
    target_version: ArticleVersion | None
    grounding: object
    fact_drift: object
    quality: object
    schema_passed: bool
    job_run: JobRun | None = None
    article_mode: ArticleQualityMode = ArticleQualityMode.BRIEF
    why_status: WhyStatus = WhyStatus.WHY_NONE_ESTABLISHED
    who_status: WhoStatus = WhoStatus.WHO_NONE_ESTABLISHED
    example_status: ExampleStatus = ExampleStatus.EXAMPLE_NONE
    dry_run: bool = False
    already_applied: bool = False

    @property
    def ready_for_article_review(self):
        return (
            self.schema_passed
            and self.grounding.status == GroundingStatus.PASS
            and self.fact_drift.status == GroundingStatus.PASS
            and self.quality.quality_status == QualityStatus.PASS
            and self.quality.quality_level == QualityLevel.LEVEL_2
        )


def _validate_actor(actor):
    if actor is None or not getattr(actor, "pk", None) or not actor.is_active:
        raise ArticleBriefDowngradeError(
            "A persistent active human actor is required",
            "ARTICLE_BRIEF_DOWNGRADE_ACTOR_REQUIRED",
        )
    if not actor.__class__._default_manager.filter(pk=actor.pk, is_active=True).exists():
        raise ArticleBriefDowngradeError(
            "Actor is not a persistent active user",
            "ARTICLE_BRIEF_DOWNGRADE_ACTOR_REQUIRED",
        )


def _validate_selector(article_id, source_version_id):
    if (article_id is None) == (source_version_id is None):
        raise ArticleBriefDowngradeError(
            "Specify exactly one of article_id or source_version_id",
            "ARTICLE_BRIEF_DOWNGRADE_SELECTOR_REQUIRED",
        )
    if article_id is not None and article_id != CONTROLLED_ARTICLE_ID:
        raise ArticleBriefDowngradeError(
            "This controlled operation only permits Article1",
            "ARTICLE_BRIEF_DOWNGRADE_WRONG_ARTICLE",
        )
    if source_version_id is not None and source_version_id != CONTROLLED_SOURCE_VERSION_ID:
        raise ArticleBriefDowngradeError(
            "This controlled operation only permits Version3",
            "ARTICLE_BRIEF_DOWNGRADE_WRONG_VERSION",
        )


def _advisory_xact_lock(article_id):
    if connection.vendor == "postgresql":
        digest = hashlib.sha256(
            f"article-brief-downgrade:{article_id}:source:3".encode()
        ).digest()
        lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def _idempotency_key(article_id, source_version_id):
    return (
        f"article-brief-downgrade:article:{article_id}:"
        f"source-version:{source_version_id}"
    )


def _content_hash(version):
    content = {field: getattr(version, field) for field in version.CONTENT_FIELDS}
    encoded = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _source_for_selector(article_id, source_version_id, *, lock=False):
    query = ArticleVersion.objects.select_related("article__event")
    if lock:
        query = query.select_for_update()
    if source_version_id is not None:
        return query.get(pk=source_version_id)
    article_query = Article.objects
    if lock:
        article_query = article_query.select_for_update()
    article = article_query.get(pk=article_id)
    if article.current_version_id != CONTROLLED_SOURCE_VERSION_ID:
        return query.get(pk=CONTROLLED_SOURCE_VERSION_ID)
    return query.get(pk=article.current_version_id)


def _candidate_content(source):
    return {
        "title": source.title,
        "one_sentence_summary": source.one_sentence_summary,
        "what_happened": source.what_happened,
        "why_it_matters": "",
        "who_is_affected": "",
        "example": "",
    }


def _quality_input(source, content, *, mode, source_contract=False):
    core = InformationUnit(
        f"article-version:{source.pk}:core-fact", InformationKind.CORE_FACT
    )
    detail = InformationUnit(
        f"article-version:{source.pk}:verified-detail", InformationKind.CORE_FACT
    )
    boundary = InformationUnit(
        f"article-version:{source.pk}:who-none", InformationKind.EPISTEMIC_BOUNDARY
    )
    optional = {}
    if source_contract:
        optional = {
            "why_it_matters": QualitySection(content["why_it_matters"], (core,)),
            "why_status": WhyStatus.WHY_ESTABLISHED,
            "who_is_affected": QualitySection(
                content["who_is_affected"], (boundary, core)
            ),
            "example": QualitySection(content["example"], (core, detail)),
            "example_status": ExampleStatus.EXAMPLE_EVENT_RESTATEMENT,
        }
    return ArticleQualityInput(
        article_mode=mode,
        grounding_status="PASS",
        title=QualitySection(content["title"], (core,)),
        summary=QualitySection(content["one_sentence_summary"], (core, detail)),
        what_happened=QualitySection(content["what_happened"], (core, detail)),
        why_it_matters=optional.get("why_it_matters", QualitySection()),
        why_status=optional.get("why_status", WhyStatus.WHY_NONE_ESTABLISHED),
        who_is_affected=optional.get("who_is_affected", QualitySection()),
        who_status=WhoStatus.WHO_NONE_ESTABLISHED,
        example=optional.get("example", QualitySection()),
        example_status=optional.get("example_status", ExampleStatus.EXAMPLE_NONE),
    )


def _validate_source(source):
    article = source.article
    if source.pk != CONTROLLED_SOURCE_VERSION_ID or article.pk != CONTROLLED_ARTICLE_ID:
        raise ArticleBriefDowngradeError(
            "Source is not the controlled Article1 Version3",
            "ARTICLE_BRIEF_DOWNGRADE_SOURCE_MISMATCH",
        )
    if _content_hash(source) != CONTROLLED_SOURCE_CONTENT_HASH:
        raise ArticleBriefDowngradeError(
            "Version3 content differs from the JOB-90 decision baseline",
            "ARTICLE_BRIEF_DOWNGRADE_SOURCE_CONTENT_CHANGED",
        )
    if article.status != ArticleStatus.DRAFT or source.status != ArticleStatus.DRAFT:
        raise ArticleBriefDowngradeError(
            "Article1 Version3 must remain DRAFT",
            "ARTICLE_BRIEF_DOWNGRADE_SOURCE_NOT_DRAFT",
        )
    if article.current_version_id != source.pk:
        raise ArticleBriefDowngradeError(
            "Article1 current version must be Version3",
            "ARTICLE_BRIEF_DOWNGRADE_SOURCE_NOT_CURRENT",
        )
    grounding = validate_article_grounding(source)
    if grounding.status != GroundingStatus.PASS:
        raise ArticleBriefDowngradeError(
            "Version3 Grounding must be PASS",
            "ARTICLE_BRIEF_DOWNGRADE_SOURCE_GROUNDING_BLOCKED",
        )
    source_content = {
        **_candidate_content(source),
        "why_it_matters": source.why_it_matters,
        "who_is_affected": source.who_is_affected,
        "example": source.example,
    }
    quality = evaluate_article_quality(_quality_input(
        source,
        source_content,
        mode=ArticleQualityMode.STANDARD,
        source_contract=True,
    ))
    if (
        quality.quality_level != QualityLevel.LEVEL_2
        or quality.information_gain_status != InformationGainStatus.TOO_THIN
    ):
        raise ArticleBriefDowngradeError(
            "Version3 must be LEVEL_2 with TOO_THIN information gain",
            "ARTICLE_BRIEF_DOWNGRADE_SOURCE_QUALITY_BLOCKED",
        )
    return grounding, quality


def _validate_candidate(source, content):
    if content != _candidate_content(source):
        raise ArticleBriefDowngradeError(
            "Brief facts must be an exact subset of Version3",
            "ARTICLE_BRIEF_DOWNGRADE_FACT_SUBSET_BLOCKED",
        )
    candidate = ArticleVersion(
        article=source.article,
        version_number=source.version_number + 1,
        source_note=source.source_note,
        language=source.language,
        status=ArticleStatus.DRAFT,
        **content,
    )
    try:
        candidate.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as exc:
        raise ArticleBriefDowngradeError(
            "Brief candidate failed schema validation",
            "ARTICLE_BRIEF_DOWNGRADE_SCHEMA_BLOCKED",
        ) from exc
    grounding = validate_article_content_grounding(content, source)
    fact_failures = tuple(
        item for item in grounding.content_unit_results
        if item.content_unit.content_type == ContentUnitType.FACT
        and item.status != GroundingStatus.PASS
    )
    if fact_failures:
        raise ArticleBriefDowngradeError(
            "Brief candidate failed Fact Drift",
            "ARTICLE_BRIEF_DOWNGRADE_FACT_DRIFT_BLOCKED",
        )
    if grounding.status != GroundingStatus.PASS:
        raise ArticleBriefDowngradeError(
            "Brief candidate failed Grounding",
            "ARTICLE_BRIEF_DOWNGRADE_GROUNDING_BLOCKED",
        )
    quality = evaluate_article_quality(_quality_input(
        source, content, mode=ArticleQualityMode.BRIEF
    ))
    if (
        quality.quality_status != QualityStatus.PASS
        or quality.quality_level != QualityLevel.LEVEL_2
    ):
        raise ArticleBriefDowngradeError(
            "Brief candidate failed Quality",
            "ARTICLE_BRIEF_DOWNGRADE_QUALITY_BLOCKED",
        )
    return grounding, grounding, quality


def _existing_result(source):
    job_run = JobRun.objects.filter(
        job_type=JOB_TYPE,
        idempotency_key=_idempotency_key(source.article_id, source.pk),
        status=JobStatus.SUCCESS,
    ).first()
    audit = AuditLog.objects.filter(
        action=AUDIT_ACTION,
        object_type="ArticleVersion",
        before_data__source_version_id=source.pk,
    ).order_by("pk").first()
    if not audit and not job_run:
        return None
    if not audit or not job_run:
        raise ArticleBriefDowngradeError(
            "Downgrade idempotency records are incomplete",
            "ARTICLE_BRIEF_DOWNGRADE_INTEGRITY_BLOCKED",
        )
    target_id = audit.after_data.get("target_version_id")
    target = ArticleVersion.objects.filter(pk=target_id, article=source.article).first()
    if not target or source.article.current_version_id != target.pk:
        raise ArticleBriefDowngradeError(
            "Existing downgrade audit does not match current version",
            "ARTICLE_BRIEF_DOWNGRADE_INTEGRITY_BLOCKED",
        )
    if (
        source.article.status != ArticleStatus.DRAFT
        or source.status != ArticleStatus.DRAFT
        or target.status != ArticleStatus.DRAFT
    ):
        raise ArticleBriefDowngradeError(
            "A published or non-draft downgrade cannot be reused",
            "ARTICLE_BRIEF_DOWNGRADE_ALREADY_PUBLISHED",
        )
    expected_content = _candidate_content(source)
    target_content = {field: getattr(target, field) for field in expected_content}
    source_mapping = list(
        source.claim_links.order_by("display_order", "pk").values_list(
            "claim_id", "paragraph_key", "display_order"
        )
    )
    target_mapping = list(
        target.claim_links.order_by("display_order", "pk").values_list(
            "claim_id", "paragraph_key", "display_order"
        )
    )
    if (
        target_content != expected_content
        or target.source_note != source.source_note
        or target.language != source.language
        or target_mapping != source_mapping
        or validate_article_grounding(target).status != GroundingStatus.PASS
    ):
        raise ArticleBriefDowngradeError(
            "Existing Version4 does not match the controlled downgrade",
            "ARTICLE_BRIEF_DOWNGRADE_INTEGRITY_BLOCKED",
        )
    grounding, fact_drift, quality = _validate_candidate(
        source, _candidate_content(source)
    )
    return ArticleBriefDowngradeResult(
        source_version=source,
        target_version=target,
        grounding=grounding,
        fact_drift=fact_drift,
        quality=quality,
        schema_passed=True,
        job_run=job_run,
        already_applied=True,
    )


def downgrade_article_to_brief(*, actor, article_id=None, source_version_id=None, dry_run=False):
    _validate_actor(actor)
    _validate_selector(article_id, source_version_id)
    source = _source_for_selector(article_id, source_version_id)
    existing = _existing_result(source)
    if existing:
        return existing
    _validate_source(source)
    content = _candidate_content(source)
    grounding, fact_drift, quality = _validate_candidate(source, content)
    if dry_run:
        return ArticleBriefDowngradeResult(
            source_version=source,
            target_version=None,
            grounding=grounding,
            fact_drift=fact_drift,
            quality=quality,
            schema_passed=True,
            dry_run=True,
        )

    with transaction.atomic():
        _advisory_xact_lock(source.article_id)
        source = _source_for_selector(article_id, source_version_id, lock=True)
        source.article = Article.objects.select_for_update().get(pk=source.article_id)
        existing = _existing_result(source)
        if existing:
            return existing
        _validate_source(source)
        if ArticleVersion.objects.filter(
            article=source.article,
            version_number=source.version_number + 1,
        ).exists():
            raise ArticleBriefDowngradeError(
                "Version4 already exists without a complete downgrade record",
                "ARTICLE_BRIEF_DOWNGRADE_INTEGRITY_BLOCKED",
            )
        content = _candidate_content(source)
        grounding, fact_drift, quality = _validate_candidate(source, content)
        job_run = JobRun.objects.create(
            job_type=JOB_TYPE,
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            idempotency_key=_idempotency_key(source.article_id, source.pk),
        )
        target = ArticleVersion.objects.create(
            article=source.article,
            version_number=source.version_number + 1,
            source_note=source.source_note,
            language=source.language,
            status=ArticleStatus.DRAFT,
            created_by=actor,
            **content,
        )
        source_links = list(source.claim_links.order_by("display_order", "pk"))
        ArticleVersionClaim.objects.bulk_create([
            ArticleVersionClaim(
                article_version=target,
                claim_id=link.claim_id,
                paragraph_key=link.paragraph_key,
                display_order=link.display_order,
            )
            for link in source_links
        ])
        postcheck = validate_article_grounding(target)
        if postcheck.status != GroundingStatus.PASS:
            raise ArticleBriefDowngradeError(
                "Persisted Version4 failed Grounding",
                "ARTICLE_BRIEF_DOWNGRADE_POSTCHECK_BLOCKED",
            )
        source.article.current_version = target
        source.article.save(update_fields=["current_version", "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action=AUDIT_ACTION,
            object_type="ArticleVersion",
            object_id=str(target.pk),
            before_data={"article_id": source.article_id, "source_version_id": source.pk},
            after_data={
                "article_id": source.article_id,
                "source_version_id": source.pk,
                "target_version_id": target.pk,
                "article_mode": ArticleQualityMode.BRIEF.value,
                "why_status": WhyStatus.WHY_NONE_ESTABLISHED.value,
                "who_status": WhoStatus.WHO_NONE_ESTABLISHED.value,
                "example_status": ExampleStatus.EXAMPLE_NONE.value,
                "reason": DOWNGRADE_REASON,
                "actor_id": actor.pk,
                "job_run_id": job_run.pk,
                "timestamp": timezone.now().isoformat(),
            },
        )
        job_run.status = JobStatus.SUCCESS
        job_run.finished_at = timezone.now()
        job_run.save(update_fields=["status", "finished_at"])
        return ArticleBriefDowngradeResult(
            source_version=source,
            target_version=target,
            grounding=postcheck,
            fact_drift=fact_drift,
            quality=quality,
            schema_passed=True,
            job_run=job_run,
        )
