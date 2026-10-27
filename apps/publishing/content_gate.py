import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher

from django.db import transaction

from apps.audit.models import AuditLog

from .grounding import ExampleSemanticType, GroundingStatus, classify_example, validate_article_grounding
from .models import ArticleContentValidation
from .quality import (
    ArticleQualityInput,
    ArticleQualityMode,
    ExampleStatus,
    InformationKind,
    InformationUnit,
    QualitySection,
    WhoStatus,
    WhyStatus,
    evaluate_article_quality,
)


GROUNDING_VALIDATOR_VERSION = "article-grounding-v1.2"
QUALITY_VALIDATOR_VERSION = "article-quality-v1.1"
QUALITY_INPUT_VERSION = "article-quality-input-v2"
_SECTION_FIELDS = (
    ("title", "title"),
    ("summary", "one_sentence_summary"),
    ("what_happened", "what_happened"),
    ("why_it_matters", "why_it_matters"),
    ("who_is_affected", "who_is_affected"),
    ("example", "example"),
)
_KINDS = {
    "title": InformationKind.CORE_FACT,
    "summary": InformationKind.CORE_FACT,
    "what_happened": InformationKind.CORE_FACT,
    "why_it_matters": InformationKind.MEANING,
    "who_is_affected": InformationKind.AFFECTED_PARTY,
    "example": InformationKind.ABSTRACT_ANALOGY,
}

_FACT_DETAIL_PATTERNS = {
    "date": re.compile(
        r"(?:\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*(?:19|20)\d{2}\b|"
        r"(?<!\d)(?:19|20)\d{2}(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)?)",
        re.I,
    ),
    "amount": re.compile(
        r"(?:[$¥￥]\s*\d+(?:[.,]\d+)*(?:万|亿|million|billion)?|"
        r"\d+(?:[.,]\d+)*(?:万|亿|million|billion)?(?:美元|元|人民币|"
        r"dollars?|yuan|rmb))",
        re.I,
    ),
    "percentage": re.compile(r"\d+(?:[.,]\d+)*(?:%|％|个百分点|percent(?:age points?)?)", re.I),
    "people": re.compile(r"\d+(?:[.,]\d+)*(?:万|亿|千|百)?(?:人|名|位|people|persons?)", re.I),
    "location": re.compile(
        r"(?:在|位于|来自|前往|附近的)[\u4e00-\u9fff]{2,12}(?:国|省|市|州|县|区|岛)|"
        r"\b(?:in|at|from|near|across)\s+(?:[A-Z][A-Za-z-]*)"
        r"(?:\s+[A-Z][A-Za-z-]*)*"
    ),
}


class ContentGateError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def content_hash(version):
    payload = {field: getattr(version, field) for field in version.CONTENT_FIELDS}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical(value):
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", normalized)


def _segments(value):
    return tuple(part.strip() for part in re.split(r"[。！？!?；;\n]+", str(value or "")) if part.strip())


def _factual_details(value):
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return {
        f"{kind}:{_canonical(match.group(0))}"
        for kind, pattern in _FACT_DETAIL_PATTERNS.items()
        for match in pattern.finditer(normalized)
    }


def _same_proposition(left, right):
    if not left or not right:
        return False
    left_details = _factual_details(left)
    right_details = _factual_details(right)
    if left_details != right_details and (left_details or right_details):
        return False
    left = _canonical(left)
    right = _canonical(right)
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 8 and shorter in longer and len(shorter) / len(longer) >= 0.55:
        return True
    return SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.72


def _quality_sections(version, controls):
    representatives = []
    serialized = {}
    sections = {}
    for section_key, field in _SECTION_FIELDS:
        text = getattr(version, field)
        units = []
        serialized_units = []
        for segment in _segments(text):
            canonical = _canonical(segment)
            match = next((item for item in representatives if _same_proposition(segment, item[2])), None)
            if match is None:
                key = "proposition:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
                representatives.append((canonical, key, segment))
            else:
                key = match[1]
            kind = _KINDS[section_key]
            if (
                section_key == "why_it_matters"
                and controls["why_status"] == WhyStatus.WHY_NONE_ESTABLISHED.value
            ) or (
                section_key == "who_is_affected"
                and controls["who_status"] == WhoStatus.WHO_NONE_ESTABLISHED.value
            ):
                kind = InformationKind.EPISTEMIC_BOUNDARY
            units.append(InformationUnit(key, kind))
            serialized_units.append({"key": key, "kind": kind.value, "text": segment})
        sections[section_key] = QualitySection(text or None, tuple(units))
        serialized[section_key] = {"text": text or None, "units": serialized_units}
    return sections, serialized


def _controls(output):
    why = output.get("why_status") or (
        "WHY_ESTABLISHED" if output.get("why_it_matters_status") == "ESTABLISHED"
        else "WHY_NONE_ESTABLISHED"
    )
    who = output.get("who_status") or (
        "WHO_ESTABLISHED" if output.get("who_is_affected_status") == "ESTABLISHED"
        else "WHO_NONE_ESTABLISHED"
    )
    example = "EXAMPLE_NONE"
    if output.get("example_type") == "ABSTRACT_ANALOGY":
        example = "EXAMPLE_ABSTRACT_ANALOGY"
    elif output.get("example_status") == "PROVIDED":
        semantic_type = classify_example(output.get("example"))
        example = {
            ExampleSemanticType.ABSTRACT_ANALOGY: "EXAMPLE_ABSTRACT_ANALOGY",
            ExampleSemanticType.EVENT_RESTATEMENT: "EXAMPLE_EVENT_RESTATEMENT",
            ExampleSemanticType.HYPOTHETICAL_GENERALIZATION: "EXAMPLE_HYPOTHETICAL_GENERALIZATION",
            ExampleSemanticType.UNSUPPORTED_REAL_WORLD_EXAMPLE: "EXAMPLE_UNSUPPORTED_REAL_WORLD",
        }.get(semantic_type, "EXAMPLE_NONE")
    return {
        "article_mode": output.get("article_mode", "BRIEF"),
        "why_status": why,
        "who_status": who,
        "example_status": example,
    }


def build_quality_input(version, output, *, grounding_status):
    controls = _controls(output)
    sections, serialized_sections = _quality_sections(version, controls)
    quality_input = ArticleQualityInput(
        article_mode=ArticleQualityMode(controls["article_mode"]),
        grounding_status=grounding_status,
        title=sections["title"], summary=sections["summary"],
        what_happened=sections["what_happened"],
        why_it_matters=sections["why_it_matters"],
        who_is_affected=sections["who_is_affected"], example=sections["example"],
        why_status=WhyStatus(controls["why_status"]),
        who_status=WhoStatus(controls["who_status"]),
        example_status=ExampleStatus(controls["example_status"]),
    )
    serialized = {
        "version": QUALITY_INPUT_VERSION,
        "producer": "deterministic-proposition-dedup-v2",
        "controls": controls,
        "sections": serialized_sections,
    }
    return quality_input, serialized


def evaluate_content_gate(version, output):
    grounding = validate_article_grounding(version)
    quality_input, quality_input_data = build_quality_input(
        version, output, grounding_status=grounding.status.value
    )
    quality = evaluate_article_quality(quality_input)
    grounding_data = {
        "status": grounding.status.value,
        "reason_codes": list(grounding.reason_codes),
        "failed_units": [
            {
                "section": item.content_unit.section_key,
                "status": item.status.value,
                "reason_codes": list(item.reason_codes),
            }
            for item in grounding.content_unit_results if not item.passed
        ],
    }
    quality_data = {
        "status": quality.quality_status.value,
        "level": quality.quality_level.value,
        "mode": quality.article_mode.value,
        "information_gain_score": quality.information_gain_score,
        "information_gain_status": quality.information_gain_status.value,
        "reasons": list(quality.reasons),
        "failed_checks": list(quality.failed_checks),
        "sections": [
            {
                "section": item.section_key,
                "action": item.action.value,
                "novel_unit_keys": list(item.novel_unit_keys),
                "repeated_unit_keys": list(item.repeated_unit_keys),
            }
            for item in quality.section_results
        ],
    }
    return grounding_data, quality_input_data, quality_data


@transaction.atomic
def persist_content_gate(version, output):
    grounding, quality_input, quality = evaluate_content_gate(version, output)
    defaults = {
        "content_hash": content_hash(version),
        "grounding_status": grounding["status"],
        "grounding_validator_version": GROUNDING_VALIDATOR_VERSION,
        "grounding_result": grounding,
        "quality_status": quality["status"],
        "quality_validator_version": QUALITY_VALIDATOR_VERSION,
        "quality_input": quality_input,
        "quality_result": quality,
        "content_quality": output.get("content_quality", "") or "",
    }
    gate = (
        ArticleContentValidation.objects.select_for_update()
        .filter(article_version=version)
        .first()
    )
    before_data = {}
    if gate is None:
        gate = ArticleContentValidation.objects.create(article_version=version, **defaults)
        changed = True
    else:
        before_data = {
            field: getattr(gate, field)
            for field in defaults
        }
        changed = any(before_data[field] != value for field, value in defaults.items())
        if changed:
            for field, value in defaults.items():
                setattr(gate, field, value)
            gate.save(update_fields=(*defaults.keys(), "validated_at"))
    if changed:
        AuditLog.objects.create(
            action="ARTICLE_CONTENT_GATE_VALIDATED",
            object_type="ArticleVersion", object_id=str(version.pk),
            before_data=before_data,
            after_data={
                "article_id": version.article_id, "article_version_id": version.pk,
                "content_validation_id": gate.pk,
                "grounding_status": gate.grounding_status,
                "grounding_validator_version": gate.grounding_validator_version,
                "quality_status": gate.quality_status,
                "quality_validator_version": gate.quality_validator_version,
                "content_hash": gate.content_hash,
            },
        )
    return gate


def require_content_gate_pass(version):
    """Require a current factual validation; quality remains review context."""
    try:
        gate = version.content_validation
    except ArticleContentValidation.DoesNotExist:
        raise ContentGateError("ArticleVersion has no content validation", "ARTICLE_CONTENT_GATE_MISSING") from None
    if (
        gate.grounding_validator_version != GROUNDING_VALIDATOR_VERSION
        or gate.quality_validator_version != QUALITY_VALIDATOR_VERSION
        or gate.quality_input.get("version") != QUALITY_INPUT_VERSION
    ):
        raise ContentGateError("ArticleVersion content validation is stale", "ARTICLE_CONTENT_GATE_STALE")
    controls = gate.quality_input.get("controls") or {}
    grounding, _, _quality = evaluate_content_gate(version, controls)
    if gate.content_hash != content_hash(version):
        raise ContentGateError("ArticleVersion changed after validation", "ARTICLE_CONTENT_GATE_STALE")
    if grounding["status"] != GroundingStatus.PASS.value or gate.grounding_status != GroundingStatus.PASS.value:
        raise ContentGateError("Article Grounding gate did not pass", "ARTICLE_GROUNDING_BLOCKED")
    return gate
