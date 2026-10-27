import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from apps.claims.models import ClaimType, EvidenceStance
from apps.scoring.rules import DIMENSIONS


MAX_RAW_OUTPUT_CHARS = 20000

CLAIM_EXTRACTION_SCHEMA_V1 = {
    "title": "claim_extraction_v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_text", "claim_type", "confidence", "evidence_hints"],
                "properties": {
                    "claim_text": {"type": "string", "minLength": 1, "maxLength": 500},
                    "claim_type": {"type": "string", "enum": list(ClaimType.values)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_hints": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["raw_item_id", "quote"],
                            "properties": {
                                "raw_item_id": {"type": "integer", "minimum": 1},
                                "quote": {"type": "string", "minLength": 1, "maxLength": 2000},
                            },
                        },
                    },
                },
            },
        }
    },
}

CLAIM_EXTRACTION_SCHEMA = {
    "title": "claim_extraction_v2",
    "type": "object",
    "additionalProperties": False,
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "claim_text", "claim_type", "confidence", "importance_hint",
                    "importance_reason", "evidence_hints",
                ],
                "properties": {
                    "claim_text": {"type": "string", "minLength": 1, "maxLength": 500},
                    "claim_type": {"type": "string", "enum": list(ClaimType.values)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "importance_hint": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                    "importance_reason": {"type": ["string", "null"], "maxLength": 500},
                    "evidence_hints": CLAIM_EXTRACTION_SCHEMA_V1["properties"]["claims"]["items"]["properties"]["evidence_hints"],
                },
            },
        }
    },
}

CLAIM_EXTRACTION_SCHEMA_V3 = {
    "title": "claim_extraction_v3",
    "type": "object",
    "additionalProperties": False,
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "claim_text", "claim_type", "confidence", "reason",
                    "evidence_hints",
                ],
                "properties": {
                    "claim_text": {"type": "string", "minLength": 1, "maxLength": 500},
                    "claim_type": {"type": "string", "enum": list(ClaimType.values)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "importance_hint": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                    "importance_reason": {"type": ["string", "null"], "maxLength": 500},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                    "evidence_hints": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["raw_item_id", "quote", "hint_type"],
                            "properties": {
                                "raw_item_id": {"type": "integer", "minimum": 1},
                                "quote": {"type": "string", "minLength": 1, "maxLength": 2000},
                                "hint_type": {"type": "string", "enum": list(EvidenceStance.values)},
                            },
                        },
                    },
                },
            },
        }
    },
}

SCORE_SUGGESTION_SCHEMA = {
    "title": "scoring_suggestion_v1",
    "type": "object",
    "additionalProperties": False,
    "required": [*DIMENSIONS, "overall_reason"],
    "properties": {
        **{
            field: {
                "type": "object",
                "additionalProperties": False,
                "required": ["score", "reason"],
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": 10},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
            }
            for field in DIMENSIONS
        },
        "overall_reason": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
}

ARTICLE_GENERATION_FIELDS = (
    "title", "summary", "what_happened", "why_it_matters",
    "who_is_affected", "example",
)
ARTICLE_GENERATION_SCHEMA = {
    "title": "article_generation_v1",
    "type": "object",
    "additionalProperties": False,
    "required": [*ARTICLE_GENERATION_FIELDS, "claim_ids"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 500},
        "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
        "what_happened": {"type": "string", "minLength": 1, "maxLength": 5000},
        "why_it_matters": {"type": "string", "minLength": 1, "maxLength": 5000},
        "who_is_affected": {"type": "string", "minLength": 1, "maxLength": 5000},
        "example": {"type": "string", "minLength": 1, "maxLength": 5000},
        "claim_ids": {
            "type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        },
    },
}

ARTICLE_CONTENT_V1_1_FIELDS = (
    *ARTICLE_GENERATION_FIELDS,
    "why_it_matters_status", "who_is_affected_status", "example_status",
)
ARTICLE_CONTENT_SCHEMA_V1_1 = {
    "title": "article_content_v1_1",
    "type": "object",
    "additionalProperties": False,
    "required": [*ARTICLE_CONTENT_V1_1_FIELDS, "claim_ids"],
    "properties": {
        **ARTICLE_GENERATION_SCHEMA["properties"],
        "example": {"type": ["string", "null"], "maxLength": 5000},
        "why_it_matters_status": {
            "type": "string", "enum": ["ESTABLISHED", "NONE_ESTABLISHED"],
        },
        "who_is_affected_status": {
            "type": "string", "enum": ["ESTABLISHED", "NONE_ESTABLISHED"],
        },
        "example_status": {
            "type": "string", "enum": ["PROVIDED", "EMPTY", "NOT_APPLICABLE"],
        },
    },
}

ARTICLE_GENERATION_V1_1_FIELDS = (
    "article_mode", "title", "one_sentence", "what_happened",
    "why_status", "why_it_matters", "who_status", "who_affected",
    "example_type", "example",
)
ARTICLE_GENERATION_SCHEMA_V1_1 = {
    "title": "article_generation_v1_1",
    "type": "object",
    "additionalProperties": False,
    "required": [*ARTICLE_GENERATION_V1_1_FIELDS, "claim_ids"],
    "properties": {
        "article_mode": {"type": "string", "enum": ["STANDARD", "BRIEF"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 500},
        "one_sentence": {"type": "string", "minLength": 1, "maxLength": 1000},
        "what_happened": {"type": "string", "minLength": 1, "maxLength": 5000},
        "why_status": {
            "type": "string",
            "enum": ["WHY_ESTABLISHED", "WHY_NONE_ESTABLISHED"],
        },
        "why_it_matters": {"type": ["string", "null"], "maxLength": 5000},
        "who_status": {
            "type": "string",
            "enum": ["WHO_ESTABLISHED", "WHO_NONE_ESTABLISHED"],
        },
        "who_affected": {"type": ["string", "null"], "maxLength": 5000},
        "example_type": {
            "type": "string",
            "enum": ["ABSTRACT_ANALOGY", "NONE"],
        },
        "example": {"type": ["string", "null"], "maxLength": 5000},
        "claim_ids": {
            "type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        },
    },
}

ARTICLE_GENERATION_SCHEMA_V1_2 = {
    **ARTICLE_GENERATION_SCHEMA_V1_1,
    "title": "article_generation_v1_2",
}

ARTICLE_GENERATION_SCHEMA_V1_3 = {
    **ARTICLE_GENERATION_SCHEMA_V1_1,
    "title": "article_generation_v1_3",
    "properties": {
        **ARTICLE_GENERATION_SCHEMA_V1_1["properties"],
        "what_happened": {"type": ["string", "null"], "maxLength": 5000},
    },
}


def article_schema_for_version(schema_version):
    schemas = {
        "article-generation-v1": ARTICLE_GENERATION_SCHEMA,
        "article-content-v1.1": ARTICLE_CONTENT_SCHEMA_V1_1,
        "article-generation-v1.1": ARTICLE_GENERATION_SCHEMA_V1_1,
        "article-generation-v1.2": ARTICLE_GENERATION_SCHEMA_V1_2,
        "article-generation-v1.3": ARTICLE_GENERATION_SCHEMA_V1_3,
    }
    try:
        return schemas[schema_version]
    except KeyError:
        raise ValidationError("Unsupported article schema version") from None


def _number(value, name, minimum, maximum):
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"{name} must be numeric") from None
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _strict_keys(value, required, name):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValidationError(f"{name} has missing or additional fields")


def _validate_claim_text(text, claim_type):
    stripped = text.strip()
    if re.search(r"(^|\n)\s*(?:[-*+]\s|#{1,6}\s|```)", stripped):
        raise ValidationError("claim_text must not contain Markdown headings, bullets, or code blocks")
    if any(unicodedata.category(char) == "So" for char in stripped):
        raise ValidationError("claim_text must not contain emoji or decorative symbols")
    if claim_type != ClaimType.QUOTE and re.search(r"(?:据我判断|可能|应该|预计)", stripped):
        raise ValidationError("speculation is only allowed as an attributed QUOTE")
    return stripped


def parse_claim_output(raw, *, schema_version="claim-extraction-v2"):
    if len(raw) > MAX_RAW_OUTPUT_CHARS:
        raise ValidationError("Model output is too large")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValidationError("Model output is not valid JSON") from None
    _strict_keys(data, ["claims"], "claim output")
    if not isinstance(data["claims"], list) or len(data["claims"]) > 50:
        raise ValidationError("claims must be an array with at most 50 entries")
    parsed = []
    if schema_version == "claim-extraction-v1":
        required = ["claim_text", "claim_type", "confidence", "evidence_hints"]
    elif schema_version == "claim-extraction-v2":
        required = [
            "claim_text", "claim_type", "confidence", "importance_hint",
            "importance_reason", "evidence_hints",
        ]
    elif schema_version == "claim-extraction-v3":
        required = ["claim_text", "claim_type", "confidence", "reason", "evidence_hints"]
        optional = {"importance_hint", "importance_reason"}
    else:
        raise ValidationError("Unsupported claim extraction schema version")
    for item in data["claims"]:
        if schema_version == "claim-extraction-v3":
            if not isinstance(item, dict) or not set(required) <= set(item) or set(item) - set(required) - optional:
                raise ValidationError("claim candidate has missing or additional fields")
        else:
            _strict_keys(item, required, "claim candidate")
        text = item["claim_text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValidationError("claim_text must contain 1 to 500 characters")
        if item["claim_type"] not in ClaimType.values:
            raise ValidationError("claim_type is invalid", code="INVALID_CLAIM_TYPE")
        importance_hint = item.get("importance_hint")
        importance_reason = item.get("importance_reason")
        if schema_version in {"claim-extraction-v2", "claim-extraction-v3"}:
            if importance_hint is not None:
                if isinstance(importance_hint, bool) or not isinstance(importance_hint, int):
                    raise ValidationError("importance_hint must be an integer or null")
                if not 0 <= importance_hint <= 100:
                    raise ValidationError("importance_hint must be between 0 and 100")
                if not isinstance(importance_reason, str) or not importance_reason.strip():
                    raise ValidationError("importance_reason is required when importance_hint is set")
            elif importance_reason is not None and not isinstance(importance_reason, str):
                raise ValidationError("importance_reason must be a string or null")
            if isinstance(importance_reason, str) and len(importance_reason.strip()) > 500:
                raise ValidationError("importance_reason must not exceed 500 characters")
            if schema_version == "claim-extraction-v3" and importance_hint is None and importance_reason is not None:
                raise ValidationError("importance_reason requires a non-null importance_hint")
        if schema_version == "claim-extraction-v3":
            reason = item["reason"]
            if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
                raise ValidationError("reason must contain 1 to 500 characters")
        hints = item["evidence_hints"]
        if not isinstance(hints, list) or len(hints) > 20:
            raise ValidationError("evidence_hints must be an array with at most 20 entries")
        parsed_hints = []
        for hint in hints:
            hint_fields = (
                ["raw_item_id", "quote", "hint_type"]
                if schema_version == "claim-extraction-v3"
                else ["raw_item_id", "quote"]
            )
            _strict_keys(hint, hint_fields, "evidence hint")
            raw_item_id = hint["raw_item_id"]
            quote = hint["quote"]
            if isinstance(raw_item_id, bool) or not isinstance(raw_item_id, int) or raw_item_id < 1:
                raise ValidationError("evidence_hints.raw_item_id must be a positive integer")
            if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
                raise ValidationError("evidence_hints.quote must contain 1 to 2000 characters")
            parsed_hint = {"raw_item_id": raw_item_id, "quote": quote.strip()}
            if schema_version == "claim-extraction-v3":
                if hint["hint_type"] not in EvidenceStance.values:
                    raise ValidationError("evidence_hints.hint_type is invalid")
                parsed_hint["hint_type"] = hint["hint_type"]
            parsed_hints.append(parsed_hint)
        candidate = {
            "claim_text": _validate_claim_text(text, item["claim_type"]),
            "claim_type": item["claim_type"],
            "confidence": _number(item["confidence"], "confidence", Decimal("0"), Decimal("1")),
            "evidence_hints": parsed_hints,
        }
        if schema_version in {"claim-extraction-v2", "claim-extraction-v3"}:
            candidate.update(
                importance_hint=importance_hint,
                importance_reason=importance_reason.strip() if isinstance(importance_reason, str) else None,
            )
        if schema_version == "claim-extraction-v3":
            candidate["reason"] = reason.strip()
        parsed.append(candidate)
    return parsed


def parse_score_output(raw):
    if len(raw) > MAX_RAW_OUTPUT_CHARS:
        raise ValidationError("Model output is too large")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValidationError("Model output is not valid JSON") from None
    _strict_keys(data, [*DIMENSIONS, "overall_reason"], "score output")
    if not isinstance(data["overall_reason"], str) or not 0 < len(data["overall_reason"]) <= 1000:
        raise ValidationError("overall_reason must contain 1 to 1000 characters")
    parsed = {"reasons": {}, "overall_reason": data["overall_reason"]}
    for field in DIMENSIONS:
        item = data[field]
        _strict_keys(item, ["score", "reason"], field)
        if not isinstance(item["reason"], str) or not 0 < len(item["reason"]) <= 1000:
            raise ValidationError(f"{field}.reason must contain 1 to 1000 characters")
        parsed[f"{field}_score"] = _number(item["score"], field, Decimal("0"), Decimal("10"))
        parsed["reasons"][field] = item["reason"]
    return parsed


def parse_article_output(raw, *, schema_version="article-generation-v1"):
    if not isinstance(raw, str) or len(raw) > MAX_RAW_OUTPUT_CHARS:
        raise ValidationError("Model output is too large or not text")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValidationError("Model output is not valid JSON") from None
    if schema_version == "article-generation-v1":
        fields = ARTICLE_GENERATION_FIELDS
    elif schema_version == "article-content-v1.1":
        fields = ARTICLE_CONTENT_V1_1_FIELDS
    elif schema_version in {"article-generation-v1.1", "article-generation-v1.2"}:
        return _parse_article_generation_v1_1(data)
    elif schema_version == "article-generation-v1.3":
        return _parse_article_generation_v1_1(data, allow_empty_body=True)
    else:
        raise ValidationError("Unsupported article schema version")
    _strict_keys(data, [*fields, "claim_ids"], "article output")
    limits = {
        "title": 500, "summary": 1000, "what_happened": 5000,
        "why_it_matters": 5000, "who_is_affected": 5000, "example": 5000,
    }
    parsed = {}
    for field in ARTICLE_GENERATION_FIELDS:
        value = data[field]
        if field == "example" and schema_version == "article-content-v1.1" and value is None:
            parsed[field] = ""
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > limits[field]:
            raise ValidationError(f"{field} must contain 1 to {limits[field]} characters")
        parsed[field] = value.strip()
    if schema_version == "article-content-v1.1":
        allowed = {
            "why_it_matters_status": {"ESTABLISHED", "NONE_ESTABLISHED"},
            "who_is_affected_status": {"ESTABLISHED", "NONE_ESTABLISHED"},
            "example_status": {"PROVIDED", "EMPTY", "NOT_APPLICABLE"},
        }
        for field, values in allowed.items():
            if data[field] not in values:
                raise ValidationError(f"{field} is invalid")
            parsed[field] = data[field]
        if parsed["example_status"] == "PROVIDED" and not parsed["example"]:
            raise ValidationError("A PROVIDED example must contain text")
        if parsed["example_status"] in {"EMPTY", "NOT_APPLICABLE"} and parsed["example"]:
            raise ValidationError("An EMPTY or NOT_APPLICABLE example must be null")
    claim_ids = data["claim_ids"]
    if not isinstance(claim_ids, list) or not 1 <= len(claim_ids) <= 50:
        raise ValidationError("claim_ids must contain 1 to 50 entries")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in claim_ids):
        raise ValidationError("claim_ids must contain positive integers")
    if len(set(claim_ids)) != len(claim_ids):
        raise ValidationError("claim_ids must be unique")
    parsed["claim_ids"] = claim_ids
    return parsed


def _required_or_null(value, *, status, established, none_established, field):
    if status == established:
        if not isinstance(value, str) or not value.strip() or len(value) > 5000:
            raise ValidationError(f"{field} must contain text when {status} is selected")
        return value.strip()
    if status == none_established:
        if value is not None:
            raise ValidationError(f"{field} must be null when {status} is selected")
        return ""
    raise ValidationError(f"{field} status is invalid")


def _parse_article_generation_v1_1(data, *, allow_empty_body=False):
    _strict_keys(data, [*ARTICLE_GENERATION_V1_1_FIELDS, "claim_ids"], "article output")
    allowed = {
        "article_mode": {"STANDARD", "BRIEF"},
        "why_status": {"WHY_ESTABLISHED", "WHY_NONE_ESTABLISHED"},
        "who_status": {"WHO_ESTABLISHED", "WHO_NONE_ESTABLISHED"},
        "example_type": {"ABSTRACT_ANALOGY", "NONE"},
    }
    for field, values in allowed.items():
        if data[field] not in values:
            raise ValidationError(f"{field} is invalid")

    limits = {"title": 500, "one_sentence": 1000, "what_happened": 5000}
    parsed = {"article_mode": data["article_mode"]}
    for field, limit in limits.items():
        value = data[field]
        if field == "what_happened" and allow_empty_body and value is None:
            parsed[field] = ""
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValidationError(f"{field} must contain 1 to {limit} characters")
        target = "summary" if field == "one_sentence" else field
        parsed[target] = value.strip()

    parsed["why_status"] = data["why_status"]
    parsed["why_it_matters"] = _required_or_null(
        data["why_it_matters"],
        status=data["why_status"],
        established="WHY_ESTABLISHED",
        none_established="WHY_NONE_ESTABLISHED",
        field="why_it_matters",
    )
    parsed["who_status"] = data["who_status"]
    parsed["who_is_affected"] = _required_or_null(
        data["who_affected"],
        status=data["who_status"],
        established="WHO_ESTABLISHED",
        none_established="WHO_NONE_ESTABLISHED",
        field="who_affected",
    )
    parsed["example_type"] = data["example_type"]
    example = data["example"]
    if data["example_type"] == "ABSTRACT_ANALOGY":
        if not isinstance(example, str) or not example.strip() or len(example) > 5000:
            raise ValidationError("example must contain text for ABSTRACT_ANALOGY")
        parsed["example"] = example.strip()
    elif example is not None:
        raise ValidationError("example must be null when example_type is NONE")
    else:
        parsed["example"] = ""

    # Grounding consumes the normalized internal statuses, while the public
    # schema keeps the more explicit generation vocabulary.
    parsed["why_it_matters_status"] = (
        "ESTABLISHED" if parsed["why_status"] == "WHY_ESTABLISHED" else "NONE_ESTABLISHED"
    )
    parsed["who_is_affected_status"] = (
        "ESTABLISHED" if parsed["who_status"] == "WHO_ESTABLISHED" else "NONE_ESTABLISHED"
    )
    parsed["example_status"] = (
        "PROVIDED" if parsed["example_type"] == "ABSTRACT_ANALOGY" else "EMPTY"
    )

    claim_ids = data["claim_ids"]
    if not isinstance(claim_ids, list) or not 1 <= len(claim_ids) <= 50:
        raise ValidationError("claim_ids must contain 1 to 50 entries")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in claim_ids):
        raise ValidationError("claim_ids must contain positive integers")
    if len(set(claim_ids)) != len(claim_ids):
        raise ValidationError("claim_ids must be unique")
    parsed["claim_ids"] = claim_ids
    return parsed
