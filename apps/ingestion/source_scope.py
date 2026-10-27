from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
import re
from urllib.parse import unquote, urlsplit

from django.utils import timezone


@dataclass(frozen=True)
class SourceScopeStats:
    input_count: int = 0
    allowed_by_path: int = 0
    denied_by_path: int = 0
    denied_stale: int = 0
    denied_missing_time: int = 0
    denied_limit: int = 0

    @property
    def kept_count(self):
        return self.allowed_by_path - self.denied_stale - self.denied_missing_time - self.denied_limit

    @property
    def filtered_count(self):
        return self.input_count - self.kept_count

    def as_dict(self):
        return {
            "input_count": self.input_count,
            "allowed_by_path": self.allowed_by_path,
            "denied_by_path": self.denied_by_path,
            "denied_stale": self.denied_stale,
            "denied_missing_time": self.denied_missing_time,
            "denied_limit": self.denied_limit,
            "kept_count": self.kept_count,
            "filtered_count": self.filtered_count,
        }


def _normalized_path(url):
    path = unquote(urlsplit(url).path or "/")
    return "/" + "/".join(part for part in path.split("/") if part)


def _matches_detail_prefix(path, prefix):
    normalized = "/" + prefix.strip("/")
    return path.startswith(f"{normalized}/")


def source_scope_reason(entry, policy, *, now=None):
    """Return the deterministic reason an entry is excluded, or None when it is in scope."""
    prefixes = policy.get("path_allow_prefixes", [])
    if not prefixes:
        return None

    path = _normalized_path(entry.canonical_url)
    denied = policy.get("path_deny_prefixes", [])
    if any(path == f"/{prefix.strip('/')}" or _matches_detail_prefix(path, prefix) for prefix in denied):
        return "path_denied"
    if not any(_matches_detail_prefix(path, prefix) for prefix in prefixes):
        return "path_denied"
    patterns = policy.get("path_allow_patterns", [])
    if patterns and not any(re.fullmatch(pattern, path) for pattern in patterns):
        return "path_denied"

    freshness_days = policy.get("source_freshness_days")
    if freshness_days is not None:
        updated_at = entry.content_updated_at
        if updated_at is None:
            return "missing_content_updated_at"
        current_time = now or timezone.now()
        if updated_at < current_time - timedelta(days=freshness_days):
            return "stale_content_updated_at"
        if updated_at > current_time + timedelta(days=1):
            return "stale_content_updated_at"
    return None


def apply_source_scope(entries, policy, *, now=None):
    """Apply collection-entry scope; this is separate from downstream 14-day selection."""
    if not policy.get("path_allow_prefixes"):
        return entries, SourceScopeStats(input_count=len(entries), allowed_by_path=len(entries))

    allowed = []
    denied_by_path = denied_stale = denied_missing_time = 0
    for entry in entries:
        reason = source_scope_reason(entry, policy, now=now)
        if reason == "path_denied":
            denied_by_path += 1
        elif reason == "stale_content_updated_at":
            denied_stale += 1
        elif reason == "missing_content_updated_at":
            denied_missing_time += 1
        else:
            allowed.append(entry)

    allowed_by_path = len(entries) - denied_by_path
    limit = policy.get("source_scope_limit")
    denied_limit = max(0, len(allowed) - limit) if limit is not None else 0
    if limit is not None:
        allowed.sort(
            key=lambda entry: entry.content_updated_at
            or entry.published_at
            or datetime.min.replace(tzinfo=dt_timezone.utc),
            reverse=True,
        )
        allowed = allowed[:limit]
    return allowed, SourceScopeStats(
        input_count=len(entries),
        allowed_by_path=allowed_by_path,
        denied_by_path=denied_by_path,
        denied_stale=denied_stale,
        denied_missing_time=denied_missing_time,
        denied_limit=denied_limit,
    )
