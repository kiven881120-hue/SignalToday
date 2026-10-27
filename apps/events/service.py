import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity
from django.db import DatabaseError, connection, transaction
from django.db.models import Max, Min
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.ingestion.models import RawItem
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep

from .clustering import (
    CandidateMatch,
    ClusterAction,
    ClusterDecision,
    calculate_metrics,
    detect_conflicts,
    event_cluster_key,
    is_navigation_only_title,
    normalize_event_title,
    speech_event_signature,
    token_overlap,
)
from .models import ClusterMethod, Event


logger = logging.getLogger("signaltoday.clustering")
CLUSTER_LOCK_ID = 8_392_023_031


@dataclass
class ClusterStats:
    input_count: int = 0
    new_events: int = 0
    auto_assigned: int = 0
    manual_review: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self):
        return self.__dict__.copy()


def _item_time(raw_item):
    return raw_item.published_at or raw_item.discovered_at


def _time_score(raw_item, event):
    item_time = _item_time(raw_item)
    event_time = event.occurred_at or event.first_seen_at
    if not item_time or not event_time:
        return 0.5
    hours = abs((item_time - event_time).total_seconds()) / 3600
    window = settings.EVENT_CLUSTER_TIME_WINDOW_HOURS
    return max(0.0, 1.0 - (hours / window))


def find_candidate_events(raw_item):
    normalized = normalize_event_title(raw_item.title)
    if not normalized:
        return []
    anchor = _item_time(raw_item)
    window = timedelta(hours=settings.EVENT_CLUSTER_TIME_WINDOW_HOURS)
    queryset = Event.objects.all()
    if anchor:
        queryset = queryset.filter(first_seen_at__gte=anchor - window, first_seen_at__lte=anchor + window)
    queryset = (
        queryset.filter(normalized_title__trigram_similar=normalized)
        .annotate(trigram=TrigramSimilarity("normalized_title", normalized))
        .filter(trigram__gte=settings.EVENT_CLUSTER_CANDIDATE_THRESHOLD)
        .order_by("-trigram", "id")[:20]
    )
    try:
        with transaction.atomic():
            return list(queryset)
    except DatabaseError as exc:
        logger.error("Event candidate query unavailable: %s", exc.__class__.__name__)
        return None


def decide_cluster(raw_item) -> ClusterDecision:
    if is_navigation_only_title(raw_item.title):
        return ClusterDecision(ClusterAction.NO_ACTION, None, 0.0, "", "navigation_only_title")
    normalized = normalize_event_title(raw_item.title)
    if not normalized:
        return ClusterDecision(ClusterAction.NO_ACTION, None, 0.0, "", "empty_title")
    events = find_candidate_events(raw_item)
    if events is None:
        return ClusterDecision(ClusterAction.NO_ACTION, None, 0.0, "", "candidate_query_unavailable")
    candidates = []
    for event in events:
        event_normalized = event.normalized_title or normalize_event_title(event.title)
        conflicts = detect_conflicts(raw_item.title, event.title)
        overlap = token_overlap(normalized, event_normalized)
        trigram = float(event.trigram)
        exact = normalized == event_normalized
        item_speech = speech_event_signature(raw_item.title)
        event_speech = speech_event_signature(event.title)
        structural_match = item_speech is not None and item_speech == event_speech
        metrics = calculate_metrics(
            trigram,
            overlap,
            _time_score(raw_item, event),
            conflicts,
            exact=exact,
            structural_match=structural_match,
        )
        if exact:
            method = ClusterMethod.EXACT_TITLE
        elif overlap > trigram:
            method = ClusterMethod.TOKEN_OVERLAP
        else:
            method = ClusterMethod.HIGH_SIMILARITY
        candidates.append(CandidateMatch(event, metrics, method, conflicts))

    eligible = [candidate for candidate in candidates if not candidate.conflicts]
    eligible.sort(key=lambda candidate: (-candidate.metrics.final_confidence, candidate.event.pk))
    ordered = tuple(sorted(candidates, key=lambda candidate: (-candidate.metrics.final_confidence, candidate.event.pk)))
    if eligible:
        best = eligible[0]
        if best.metrics.final_confidence >= settings.EVENT_CLUSTER_AUTO_THRESHOLD:
            return ClusterDecision(
                ClusterAction.ASSIGN_EXISTING,
                best.event,
                best.metrics.final_confidence,
                best.method,
                "high_confidence_candidate",
                ordered,
            )
        if best.metrics.final_confidence >= settings.EVENT_CLUSTER_REVIEW_THRESHOLD:
            return ClusterDecision(
                ClusterAction.MANUAL_REVIEW,
                best.event,
                best.metrics.final_confidence,
                best.method,
                "medium_confidence_candidate",
                ordered,
            )
    reason = "conflicting_candidates" if any(candidate.conflicts for candidate in candidates) else "no_reasonable_candidate"
    return ClusterDecision(ClusterAction.CREATE_NEW, None, 1.0, ClusterMethod.NEW_EVENT, reason, ordered)


def update_event_bounds(event):
    bounds = event.raw_items.aggregate(
        first=Min(Coalesce("published_at", "discovered_at")),
        last=Max(Coalesce("published_at", "discovered_at")),
    )
    if bounds["first"] is not None:
        Event.objects.filter(pk=event.pk).update(first_seen_at=bounds["first"], last_seen_at=bounds["last"])
        event.first_seen_at = bounds["first"]
        event.last_seen_at = bounds["last"]


def audit_assignment(raw_item, old_event_id, new_event_id, *, method, confidence, reason, job_run=None, actor=None):
    AuditLog.objects.create(
        actor=actor,
        action="RAW_ITEM_EVENT_ASSIGNED",
        object_type="RawItem",
        object_id=str(raw_item.pk),
        before_data={"event_id": old_event_id},
        after_data={
            "raw_item_id": raw_item.pk,
            "old_event_id": old_event_id,
            "new_event_id": new_event_id,
            "method": method,
            "confidence": round(float(confidence), 4) if confidence is not None else None,
            "reason": reason,
            "job_run_id": job_run.pk if job_run else None,
            "timestamp": timezone.now().isoformat(),
        },
    )


def assign_raw_item(raw_item, event, *, method, confidence, reason, job_run=None, actor=None):
    old_event_id = raw_item.event_id
    if old_event_id == event.pk:
        return False
    raw_item.event = event
    raw_item.save(update_fields=["event", "updated_at"])
    event.cluster_method = method
    event.cluster_confidence = confidence
    event.save(update_fields=["cluster_method", "cluster_confidence", "updated_at"])
    update_event_bounds(event)
    if old_event_id:
        old_event = Event.objects.filter(pk=old_event_id).first()
        if old_event:
            update_event_bounds(old_event)
    audit_assignment(
        raw_item,
        old_event_id,
        event.pk,
        method=method,
        confidence=confidence,
        reason=reason,
        job_run=job_run,
        actor=actor,
    )
    return True


def cluster_raw_item(raw_item, *, job_run=None):
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [CLUSTER_LOCK_ID])
        locked = RawItem.objects.select_for_update().get(pk=raw_item.pk)
        if locked.event_id:
            return ClusterDecision(ClusterAction.NO_ACTION, locked.event, 1.0, "", "already_assigned")
        decision = decide_cluster(locked)
        if decision.action == ClusterAction.CREATE_NEW:
            normalized = normalize_event_title(locked.title)
            timestamp = _item_time(locked) or timezone.now()
            event = Event.objects.create(
                title=locked.title,
                normalized_title=normalized,
                cluster_key=event_cluster_key(normalized),
                cluster_method=ClusterMethod.NEW_EVENT,
                cluster_confidence=1.0,
                first_seen_at=timestamp,
                last_seen_at=timestamp,
            )
            assign_raw_item(
                locked,
                event,
                method=ClusterMethod.NEW_EVENT,
                confidence=1.0,
                reason=decision.reason,
                job_run=job_run,
            )
            return ClusterDecision(decision.action, event, 1.0, ClusterMethod.NEW_EVENT, decision.reason, decision.candidates)
        if decision.action == ClusterAction.ASSIGN_EXISTING:
            assign_raw_item(
                locked,
                decision.event,
                method=decision.method,
                confidence=decision.confidence,
                reason=decision.reason,
                job_run=job_run,
            )
        return decision


class EventClusteringService:
    def run(self, queryset, *, dry_run=False):
        raw_items = list(queryset.select_related("source")[:])
        stats = ClusterStats(input_count=len(raw_items))
        if dry_run:
            decisions = [(item, decide_cluster(item)) for item in raw_items]
            for _, decision in decisions:
                if decision.action == ClusterAction.CREATE_NEW:
                    stats.new_events += 1
                elif decision.action == ClusterAction.ASSIGN_EXISTING:
                    stats.auto_assigned += 1
                elif decision.action == ClusterAction.MANUAL_REVIEW:
                    stats.manual_review += 1
                else:
                    stats.skipped += 1
            return stats, decisions, None

        job_run = JobRun.objects.create(
            job_type="EVENT_CLUSTER",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            idempotency_key=f"event-cluster:{uuid4()}",
        )
        step = JobStep.objects.create(
            job_run=job_run,
            step_name="cluster",
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
            input_summary={"raw_item_count": len(raw_items)},
        )
        acquired = False
        decisions = []
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [CLUSTER_LOCK_ID])
                acquired = cursor.fetchone()[0]
            if not acquired:
                raise RuntimeError("Another event clustering job is running")
            for item in raw_items:
                try:
                    decision = cluster_raw_item(item, job_run=job_run)
                    decisions.append((item, decision))
                    if decision.action == ClusterAction.CREATE_NEW:
                        stats.new_events += 1
                    elif decision.action == ClusterAction.ASSIGN_EXISTING:
                        stats.auto_assigned += 1
                    elif decision.action == ClusterAction.MANUAL_REVIEW:
                        stats.manual_review += 1
                    else:
                        stats.skipped += 1
                except Exception:
                    stats.errors += 1
                    logger.exception("Event clustering failed raw_item_id=%s", item.pk)
            final_status = JobStatus.FAILED if stats.errors else JobStatus.SUCCESS
            step.status = final_status
            step.finished_at = timezone.now()
            step.output_summary = stats.as_dict()
            step.error_message = "One or more RawItems failed" if stats.errors else ""
            step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])
            job_run.status = final_status
            job_run.finished_at = timezone.now()
            job_run.error_message = step.error_message
            job_run.save(update_fields=["status", "finished_at", "error_message"])
            return stats, decisions, job_run
        except Exception as exc:
            step.status = JobStatus.FAILED
            step.finished_at = timezone.now()
            step.error_message = str(exc)
            step.output_summary = stats.as_dict()
            step.save(update_fields=["status", "finished_at", "error_message", "output_summary"])
            job_run.status = JobStatus.FAILED
            job_run.finished_at = timezone.now()
            job_run.error_message = str(exc)
            job_run.save(update_fields=["status", "finished_at", "error_message"])
            raise
        finally:
            if acquired:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [CLUSTER_LOCK_ID])
