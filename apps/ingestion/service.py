import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlsplit
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.sources.models import Source

from .errors import ErrorCode, IngestionError
from .feed import FeedEntry, extract_article_body, parse_feed, parse_html_list, parse_sitemap
from .http import SafeHttpClient
from .models import RawItem, RawItemStatus
from .security import validate_remote_url
from .source_scope import apply_source_scope
from .urls import normalize_url


logger = logging.getLogger("signaltoday.ingestion")


@dataclass(frozen=True)
class NormalizedEntry:
    external_id: str
    url: str
    canonical_url: str
    title: str
    published_at: object
    content_updated_at: object
    content_hash: str
    raw_metadata: dict


@dataclass(frozen=True)
class IngestionStats:
    entries: int = 0
    created: int = 0
    duplicates: int = 0
    failed: int = 0
    updated: int = 0

    def as_dict(self):
        return {
            "entries": self.entries, "created": self.created, "duplicates": self.duplicates,
            "failed": self.failed, "updated": self.updated,
        }


def build_content_hash(title, canonical_url, published_at):
    published = ""
    if published_at:
        published = published_at.astimezone(dt_timezone.utc).isoformat()
    material = "\n".join((" ".join(title.split()), canonical_url, published))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_entry(entry: FeedEntry) -> NormalizedEntry:
    canonical_url = normalize_url(entry.url)
    return NormalizedEntry(
        external_id=entry.external_id.strip()[:512],
        url=entry.url.strip()[:2048],
        canonical_url=canonical_url[:2048],
        title=" ".join(entry.title.split())[:500],
        published_at=entry.published_at,
        content_updated_at=entry.content_updated_at,
        content_hash=build_content_hash(entry.title, canonical_url, entry.published_at),
        raw_metadata={
            **entry.metadata,
            "summary": entry.summary,
            "body": entry.summary,
            "body_source": "feed_summary" if entry.summary else "",
            "content_status": "READY" if entry.summary else "INSUFFICIENT_CONTENT",
        },
    )


class FeedIngestionService:
    step_names = ("source_validation", "fetch", "parse", "normalize", "content", "dedupe", "persist")

    def __init__(self, http_client=None):
        self.http_client = http_client or SafeHttpClient()

    def ingest_source(self, source: Source, *, dry_run=False, idempotency_key=None):
        key = idempotency_key or f"feed:{source.identifier or source.pk}:{uuid4()}"
        existing = JobRun.objects.filter(idempotency_key=key).first()
        if existing:
            return self._existing_result(existing)

        try:
            with transaction.atomic():
                run = JobRun.objects.create(
                    job_type="feed_ingestion",
                    status=JobStatus.RUNNING,
                    started_at=timezone.now(),
                    idempotency_key=key,
                )
        except IntegrityError:
            return self._existing_result(JobRun.objects.get(idempotency_key=key))
        steps = {name: JobStep.objects.create(job_run=run, step_name=name) for name in self.step_names}
        current_step = steps["source_validation"]
        try:
            self._start(current_step, {"source_id": source.pk, "source_identifier": source.identifier})
            if not source.active or source.crawl_policy.get("feed_only") is not True:
                raise IngestionError(ErrorCode.SECURITY_REJECTED, "Source is inactive or not feed-only")
            validate_remote_url(source.url, **({"resolver": self.http_client.resolver} if self.http_client.resolver else {}))
            self._success(current_step, {"host": source.domain})

            current_step = steps["fetch"]
            self._start(current_step, {"host": source.domain})
            fetched = self.http_client.fetch(source.url)
            self._success(current_step, {"status": fetched.status, "bytes": len(fetched.body)})

            current_step = steps["parse"]
            self._start(current_step, {"bytes": len(fetched.body)})
            collection_method = source.crawl_policy.get("collection_method", "rss")
            if collection_method == "sitemap":
                entries = parse_sitemap(fetched.body)
            elif collection_method == "html":
                entries = parse_html_list(fetched.body, source.url)
            else:
                entries = parse_feed(fetched.body)
            self._success(current_step, {"entry_count": len(entries)})

            current_step = steps["normalize"]
            self._start(current_step, {"entry_count": len(entries)})
            normalized = [normalize_entry(entry) for entry in entries]
            normalized, scope_stats = apply_source_scope(normalized, source.crawl_policy)
            normalized = self._limit_initial_entries(source, normalized)
            self._success(current_step, {
                "entry_count": len(normalized),
                "source_scope": scope_stats.as_dict(),
            })

            current_step = steps["content"]
            self._start(current_step, {"entry_count": len(normalized)})
            normalized, content_stats = self._enrich_content(source, normalized)
            self._success(current_step, content_stats)

            current_step = steps["dedupe"]
            self._start(current_step, {"entry_count": len(normalized)})
            existing_count = sum(self._find_existing(source, entry) is not None for entry in normalized)
            self._success(current_step, {"existing_count": existing_count})

            current_step = steps["persist"]
            self._start(current_step, {"entry_count": len(normalized), "dry_run": dry_run})
            stats = self._persist(source, normalized, dry_run=dry_run)
            self._success(current_step, stats.as_dict())
            run.status = JobStatus.SUCCESS
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "finished_at"])
            logger.info(
                "Feed ingestion completed source_id=%s host=%s entries=%s created=%s updated=%s duplicates=%s failed=%s",
                source.pk, source.domain, stats.entries, stats.created, stats.updated, stats.duplicates, stats.failed,
            )
            return run, stats
        except Exception as exc:
            error = exc if isinstance(exc, IngestionError) else IngestionError(ErrorCode.DATABASE_ERROR, "Ingestion failed")
            self._fail(current_step, f"{error.code}: {error}")
            run.status = JobStatus.FAILED
            run.finished_at = timezone.now()
            run.error_message = f"{error.code}: {error}"
            run.save(update_fields=["status", "finished_at", "error_message"])
            logger.warning("Feed ingestion failed source_id=%s host=%s code=%s", source.pk, source.domain, error.code)
            if error is exc:
                raise
            raise error from exc

    def _persist(self, source, entries, *, dry_run):
        created = duplicates = failed = updated = 0
        seen = set()
        with transaction.atomic():
            for entry in entries:
                identity = (entry.external_id, entry.canonical_url, entry.content_hash)
                existing = self._find_existing(source, entry)
                if identity in seen:
                    duplicates += 1
                    continue
                if existing:
                    if self._update_existing_content(existing, entry, dry_run=dry_run):
                        updated += 1
                    else:
                        duplicates += 1
                    seen.add(identity)
                    continue
                seen.add(identity)
                if dry_run:
                    created += 1
                    continue
                try:
                    with transaction.atomic():
                        RawItem.objects.create(
                            source=source,
                            external_id=entry.external_id,
                            url=entry.url,
                            canonical_url=entry.canonical_url,
                            title=entry.title,
                            published_at=entry.published_at,
                            content_updated_at=entry.content_updated_at,
                            discovered_at=timezone.now(),
                            content_hash=entry.content_hash,
                            raw_metadata=entry.raw_metadata,
                            status=RawItemStatus.DISCOVERED,
                        )
                    created += 1
                except IntegrityError:
                    if self._find_existing(source, entry):
                        duplicates += 1
                    else:
                        failed += 1
                        raise IngestionError(ErrorCode.DATABASE_ERROR, "RawItem database constraint failed")
        return IngestionStats(
            entries=len(entries), created=created, duplicates=duplicates, failed=failed, updated=updated,
        )

    def _enrich_content(self, source, entries):
        limit = int(source.crawl_policy.get("article_body_limit", 0))
        enabled = source.crawl_policy.get("fetch_article_body") is True and limit > 0
        stats = {"attempted": 0, "available": 0, "insufficient": 0, "failed": 0}
        enriched = []
        for index, entry in enumerate(entries):
            metadata = dict(entry.raw_metadata)
            parse_failed = False
            if enabled and index < limit:
                stats["attempted"] += 1
                try:
                    if (urlsplit(entry.url).hostname or "").rstrip(".").lower() != source.domain:
                        raise IngestionError(ErrorCode.SECURITY_REJECTED, "Article URL is outside the Source domain")
                    fetched = self.http_client.fetch(entry.url)
                    if (urlsplit(fetched.url).hostname or "").rstrip(".").lower() != source.domain:
                        raise IngestionError(ErrorCode.SECURITY_REJECTED, "Article redirect left the Source domain")
                    if fetched.content_type not in {"text/html", "application/xhtml+xml"}:
                        raise IngestionError(ErrorCode.UNSUPPORTED_CONTENT_TYPE, "Article response is not HTML")
                    body = extract_article_body(fetched.body, title=entry.title)
                    if body:
                        metadata.update(
                            body=body, body_source="article_html", content_status="READY",
                            body_length=len(body),
                        )
                    elif not metadata.get("body"):
                        parse_failed = True
                except IngestionError:
                    stats["failed"] += 1
                    parse_failed = not bool(metadata.get("body"))
            if metadata.get("body"):
                stats["available"] += 1
                metadata.setdefault("body_length", len(metadata["body"]))
                metadata["content_status"] = "READY"
            else:
                stats["insufficient"] += 1
                metadata.update(
                    body="", body_source="",
                    content_status="PARSE_FAILED" if parse_failed else "INSUFFICIENT_CONTENT",
                    body_length=0,
                )
            enriched.append(replace(entry, raw_metadata=metadata))
        return enriched, stats

    @staticmethod
    def _update_existing_content(existing, entry, *, dry_run):
        incoming = entry.raw_metadata
        current = existing.raw_metadata or {}
        if incoming.get("body_source") != "article_html" or incoming.get("body") == current.get("body"):
            return False
        if not dry_run:
            existing.raw_metadata = {**current, **incoming}
            existing.save(update_fields=["raw_metadata", "updated_at"])
        return True

    @staticmethod
    def _find_existing(source, entry):
        query = Q(content_hash=entry.content_hash) | Q(canonical_url=entry.canonical_url)
        if entry.external_id:
            query |= Q(external_id=entry.external_id)
        return RawItem.objects.filter(query, source=source).first()

    @staticmethod
    def _limit_initial_entries(source, entries):
        """Keep first production activation bounded to recent, small metadata-only intake."""
        policy = source.crawl_policy
        if RawItem.objects.filter(source=source).exists() or "first_import_limit" not in policy:
            return entries
        cutoff = timezone.now() - timedelta(days=int(policy.get("first_import_days", 7)))

        def entry_time(entry):
            return entry.published_at or entry.content_updated_at

        recent = [entry for entry in entries if entry_time(entry) and entry_time(entry) >= cutoff]
        recent.sort(key=entry_time, reverse=True)
        return recent[: int(policy["first_import_limit"])]

    @staticmethod
    def _existing_result(run):
        if run.status != JobStatus.SUCCESS:
            raise IngestionError(ErrorCode.DATABASE_ERROR, "Idempotent job already exists without a successful result")
        persist_step = run.steps.filter(step_name="persist").first()
        output = persist_step.output_summary if persist_step else {}
        fields = IngestionStats.__dataclass_fields__
        return run, IngestionStats(**{field: output.get(field, 0) for field in fields})

    @staticmethod
    def _start(step, summary):
        step.status = JobStatus.RUNNING
        step.started_at = timezone.now()
        step.input_summary = summary
        step.save(update_fields=["status", "started_at", "input_summary"])

    @staticmethod
    def _success(step, summary):
        step.status = JobStatus.SUCCESS
        step.finished_at = timezone.now()
        step.output_summary = summary
        step.save(update_fields=["status", "finished_at", "output_summary"])

    @staticmethod
    def _fail(step, message):
        step.status = JobStatus.FAILED
        step.finished_at = timezone.now()
        step.error_message = message
        step.save(update_fields=["status", "finished_at", "error_message"])
