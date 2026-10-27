import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings

from apps.ingestion.errors import ErrorCode, IngestionError

from .models import SourceTier, SourceType


IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_FIELDS = {
    "identifier", "name", "feed_url", "domain", "organization", "source_type",
    "country", "language", "tier", "reliability", "active", "crawl_policy", "license_state",
    "source_group", "feed_type", "category", "notes", "compliance_note",
}


@dataclass(frozen=True)
class SourceDefinition:
    identifier: str
    name: str
    feed_url: str
    domain: str
    organization: str
    source_group: str
    feed_type: str
    category: str
    notes: str
    compliance_note: str
    source_type: str
    country: str
    language: str
    tier: str
    reliability: Decimal
    active: bool
    crawl_policy: dict
    license_state: str


def load_source_definitions(directory: Path | None = None) -> list[SourceDefinition]:
    directory = directory or settings.BASE_DIR / "config" / "sources"
    definitions = []
    identifiers = set()
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IngestionError(ErrorCode.VALIDATION_ERROR, f"Invalid source config: {path.name}") from exc
        definition = validate_source_definition(payload, filename=path.name)
        if definition.identifier in identifiers:
            raise IngestionError(ErrorCode.VALIDATION_ERROR, "Duplicate source identifier in configuration")
        identifiers.add(definition.identifier)
        definitions.append(definition)
    return definitions


def validate_source_definition(payload, *, filename="source config") -> SourceDefinition:
    if not isinstance(payload, dict) or set(payload) != REQUIRED_FIELDS:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: fields do not match schema")
    identifier = payload["identifier"]
    if not isinstance(identifier, str) or not IDENTIFIER_PATTERN.fullmatch(identifier):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid identifier")
    string_fields = REQUIRED_FIELDS - {"reliability", "active", "crawl_policy"}
    if any(not isinstance(payload[field], str) for field in string_fields):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: text field has invalid type")
    nonempty_fields = string_fields - {"country", "source_group", "notes"}
    if any(not payload[field].strip() for field in nonempty_fields):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: required text field is empty")
    if not isinstance(payload["active"], bool) or not isinstance(payload["crawl_policy"], dict):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: active/crawl_policy has invalid type")
    try:
        reliability = Decimal(str(payload["reliability"]))
    except InvalidOperation as exc:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid reliability") from exc
    if not reliability.is_finite() or reliability < 0 or reliability > 1:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: reliability must be 0..1")
    if payload["source_type"] not in SourceType.values or payload["tier"] not in SourceTier.values:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid source type or tier")
    if payload["feed_type"] not in {"RSS", "ATOM", "SITEMAP", "HTML"}:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid feed type")
    if len(payload["country"]) not in {0, 2} or len(payload["language"]) > 16:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid country or language")
    parsed = urlsplit(payload["feed_url"])
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid feed URL")
    domain = payload["domain"].rstrip(".").lower()
    if parsed.hostname.rstrip(".").lower() != domain:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: domain does not match feed URL")
    if payload["crawl_policy"].get("feed_only") is not True:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: feed_only must be true")
    collection_method = payload["crawl_policy"].get("collection_method", payload["feed_type"].lower())
    if collection_method not in {"rss", "atom", "sitemap", "html"}:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: unsupported collection method")
    if "first_import_limit" in payload["crawl_policy"]:
        limit = payload["crawl_policy"]["first_import_limit"]
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid first import limit")
    policy = payload["crawl_policy"]
    allow_prefixes = policy.get("path_allow_prefixes", [])
    deny_prefixes = policy.get("path_deny_prefixes", [])
    for field, prefixes in (("path allowlist", allow_prefixes), ("path denylist", deny_prefixes)):
        if not isinstance(prefixes, list) or any(
            not isinstance(prefix, str) or not prefix.startswith("/") or prefix in {"", "/"}
            for prefix in prefixes
        ):
            raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid {field}")
    allow_patterns = policy.get("path_allow_patterns", [])
    if not isinstance(allow_patterns, list) or any(
        not isinstance(pattern, str) or not pattern or len(pattern) > 500
        for pattern in allow_patterns
    ):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid path patterns")
    try:
        for pattern in allow_patterns:
            re.compile(pattern)
    except re.error as exc:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid path pattern") from exc
    freshness_days = policy.get("source_freshness_days")
    if freshness_days is not None and (
        not isinstance(freshness_days, int) or isinstance(freshness_days, bool) or not 1 <= freshness_days <= 90
    ):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid source freshness")
    scope_limit = policy.get("source_scope_limit")
    if scope_limit is not None and (
        not isinstance(scope_limit, int) or isinstance(scope_limit, bool) or not 1 <= scope_limit <= 1000
    ):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid source scope limit")
    if allow_patterns and not allow_prefixes:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: path patterns require prefixes")
    if collection_method == "sitemap" and allow_prefixes and (
        freshness_days is None or scope_limit is None
    ):
        raise IngestionError(
            ErrorCode.VALIDATION_ERROR,
            f"{filename}: scoped sitemap requires freshness and limit",
        )
    fetch_article_body = payload["crawl_policy"].get("fetch_article_body", False)
    article_body_limit = payload["crawl_policy"].get("article_body_limit", 0)
    if not isinstance(fetch_article_body, bool):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid article body setting")
    if not isinstance(article_body_limit, int) or not 0 <= article_body_limit <= 20:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: invalid article body limit")
    if fetch_article_body != (article_body_limit > 0):
        raise IngestionError(ErrorCode.VALIDATION_ERROR, f"{filename}: article body settings must be enabled together")
    return SourceDefinition(
        identifier=identifier,
        name=payload["name"],
        feed_url=payload["feed_url"],
        domain=domain,
        organization=payload["organization"],
        source_group=payload["source_group"],
        feed_type=payload["feed_type"],
        category=payload["category"],
        notes=payload["notes"],
        compliance_note=payload["compliance_note"],
        source_type=payload["source_type"],
        country=payload["country"].upper(),
        language=payload["language"].lower(),
        tier=payload["tier"],
        reliability=reliability,
        active=payload["active"],
        crawl_policy=payload["crawl_policy"],
        license_state=payload["license_state"],
    )
