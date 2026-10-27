import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum

from apps.ai_content.models import EvidenceHintStatus
from apps.ai_content.services import validate_evidence_hint
from apps.claims.models import EvidenceStance, EvidenceType, VerificationStatus


class ContentUnitType(str, Enum):
    FACT = "FACT"
    EXPLANATION = "EXPLANATION"
    BACKGROUND = "BACKGROUND"
    EXAMPLE = "EXAMPLE"
    SPECULATION = "SPECULATION"


class ImpactLevel(str, Enum):
    DIRECT = "DIRECT"
    POTENTIAL = "POTENTIAL"
    LIMITED = "LIMITED"
    NONE_ESTABLISHED = "NONE_ESTABLISHED"


class GroundingStatus(str, Enum):
    PASS = "PASS"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    FAIL = "FAIL"


class EstablishmentStatus(str, Enum):
    ESTABLISHED = "ESTABLISHED"
    NONE_ESTABLISHED = "NONE_ESTABLISHED"


class ExampleStatus(str, Enum):
    PROVIDED = "PROVIDED"
    EMPTY = "EMPTY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ExampleSemanticType(str, Enum):
    EVENT_RESTATEMENT = "EVENT_RESTATEMENT"
    ABSTRACT_ANALOGY = "ABSTRACT_ANALOGY"
    HYPOTHETICAL_GENERALIZATION = "HYPOTHETICAL_GENERALIZATION"
    UNSUPPORTED_REAL_WORLD_EXAMPLE = "UNSUPPORTED_REAL_WORLD_EXAMPLE"
    EMPTY_OR_NOT_APPLICABLE = "EMPTY_OR_NOT_APPLICABLE"


class GroundingReason(str, Enum):
    GROUNDING_EMPTY = "GROUNDING_EMPTY"
    FACT_NO_CLAIM = "FACT_NO_CLAIM"
    FACT_NO_SUPPORTING_EVIDENCE = "FACT_NO_SUPPORTING_EVIDENCE"
    FACT_EVIDENCE_INVALID = "FACT_EVIDENCE_INVALID"
    FACT_NEW_ENTITY = "FACT_NEW_ENTITY"
    FACT_NEW_NUMBER = "FACT_NEW_NUMBER"
    FACT_NEW_DATE = "FACT_NEW_DATE"
    FACT_NEW_LOCATION = "FACT_NEW_LOCATION"
    FACT_NEW_CAUSALITY = "FACT_NEW_CAUSALITY"
    FACT_SCOPE_EXPANSION = "FACT_SCOPE_EXPANSION"
    FACT_CERTAINTY_EXPANSION = "FACT_CERTAINTY_EXPANSION"
    FACT_NEW_HEALTH_IMPACT = "FACT_NEW_HEALTH_IMPACT"
    FACT_NEW_AUDIENCE = "FACT_NEW_AUDIENCE"
    FACT_UNSUPPORTED_ASSERTION = "FACT_UNSUPPORTED_ASSERTION"
    ESTABLISHMENT_STATUS_MISMATCH = "ESTABLISHMENT_STATUS_MISMATCH"
    EXPLANATION_NEW_FACT = "EXPLANATION_NEW_FACT"
    EXPLANATION_CAUSALITY_EXPANSION = "EXPLANATION_CAUSALITY_EXPANSION"
    EXPLANATION_SCOPE_EXPANSION = "EXPLANATION_SCOPE_EXPANSION"
    BACKGROUND_NOT_SUPPORTED = "BACKGROUND_NOT_SUPPORTED"
    EXAMPLE_NOT_MARKED = "EXAMPLE_NOT_MARKED"
    EXAMPLE_INCOMPLETE = "EXAMPLE_INCOMPLETE"
    EXAMPLE_HYPOTHETICAL_GENERALIZATION = "EXAMPLE_HYPOTHETICAL_GENERALIZATION"
    EXAMPLE_REAL_WORLD_UNSUPPORTED = "EXAMPLE_REAL_WORLD_UNSUPPORTED"
    SPECULATION_NOT_ALLOWED = "SPECULATION_NOT_ALLOWED"
    IMPACT_NOT_ESTABLISHED = "IMPACT_NOT_ESTABLISHED"
    UNSUPPORTED_ADVICE = "UNSUPPORTED_ADVICE"
    GROUNDING_PASS = "GROUNDING_PASS"


@dataclass(frozen=True)
class ContentUnit:
    text: str
    content_type: ContentUnitType
    claim_ids: tuple[int, ...] = ()
    evidence_ids: tuple[int, ...] = ()
    section_key: str = ""
    impact_level: ImpactLevel | None = None
    establishment_status: EstablishmentStatus | None = None
    example_status: ExampleStatus | None = None
    example_semantic_type: ExampleSemanticType | None = None
    establishment_status_valid: bool = True


@dataclass(frozen=True)
class GroundingReference:
    claim_id: int
    evidence_id: int
    claim_text: str
    evidence_text: str
    source_name: str = ""
    claim_supported: bool = True
    evidence_supports: bool = True
    relationship_valid: bool = True
    quote_valid: bool = True
    source_valid: bool = True

    @property
    def valid(self):
        return all((
            self.claim_supported,
            self.evidence_supports,
            self.relationship_valid,
            self.quote_valid,
            self.source_valid,
        ))

    @property
    def source_text(self):
        return " ".join((self.claim_text, self.evidence_text, self.source_name))


@dataclass(frozen=True)
class ContentUnitResult:
    content_unit: ContentUnit
    status: GroundingStatus
    reason_codes: tuple[str, ...]

    @property
    def passed(self):
        return self.status == GroundingStatus.PASS


@dataclass(frozen=True)
class GroundingResult:
    status: GroundingStatus
    reason_codes: tuple[str, ...]
    content_unit_results: tuple[ContentUnitResult, ...]

    @property
    def passed(self):
        return self.status == GroundingStatus.PASS


_NUMBER_RE = re.compile(r"(?<![\w])\d+(?:[.,]\d+)*(?:%|万|亿|千|百|million|billion)?", re.I)
_NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})(?:[-/.年](0?[1-9]|1[0-2])"
    r"(?:[-/.月](0?[1-9]|[12]\d|3[01])日?)?)?(?!\d)"
)
_CHINESE_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})年(0?[1-9]|1[0-2])月(0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_ENGLISH_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s*,?\s*"
    r"((?:19|20)\d{2})\b",
    re.I,
)
_ENGLISH_DATE_RE_REVERSED = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s*,?\s*((?:19|20)\d{2})\b",
    re.I,
)
_MONTH_NUMBERS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}
_ENGLISH_ENTITY_RE = re.compile(r"\b(?:[A-Z]{2,}|[A-Z][a-z]+)(?:\s+(?:[A-Z]{2,}|[A-Z][a-z]+))*\b")
_CHINESE_ENTITY_RE = re.compile(
    r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,14}(?:国防部|委员会|研究院|实验室|大学|公司|集团|政府|医院))"
    r"(?=$|[\s、，,。；;和与及]|发布|宣布|表示|提出|确认|决定|计划|将|已|正在)"
)
_ENGLISH_LOCATION_RE = re.compile(
    r"\b(?:in|at|from|near|across)\s+((?:[A-Z][A-Za-z-]*)(?:\s+[A-Z][A-Za-z-]*)*)"
)
_CHINESE_LOCATION_RE = re.compile(
    r"(?:在|位于|来自|前往|附近的)([\u4e00-\u9fff]{2,12}(?:国|省|市|州|县|区|岛))"
)

_EXAMPLE_MARKERS = (
    "例如", "比如", "假设", "如果", "可以把", "可以将", "打个比方", "举个例子",
    "for example", "for instance", "suppose", "imagine", "as an analogy",
)
_NONE_ESTABLISHED_MARKERS = (
    "目前没有明确", "尚未明确", "暂未明确", "没有建立明确", "没有证据表明",
    "未说明", "未建立", "无法建立", "尚未确定",
    "no clear", "not established", "no established", "no evidence of",
)
_NONE_ESTABLISHED_PATTERNS = (
    re.compile(r"(?:已核实|现有)?证据.{0,8}(?:未|无法|没有).{0,10}(?:确定|确认|说明)"),
    re.compile(r"没有足够证据.{0,10}(?:确定|确认|说明)"),
    re.compile(r"暂无明确(?:的)?受影响"),
    re.compile(
        r"(?:(?:现有|已核实|所提供的经过验证)?(?:证据|声明)).{0,12}"
        r"(?:未|并未|没有|无法|尚未).{0,18}(?:建立|界定|确定|确认|说明)"
    ),
)
_CONTRAST_MARKERS = ("但是", "但", "然而", "不过", "却", "however", "but", "yet")
_UNCERTAINTY_MARKERS = (
    "可能", "或许", "也许", "预计", "据估计", "may", "might", "could",
    "possibly", "likely", "expected to",
)
_CERTAINTY_MARKERS = (
    "已经证明", "证实了", "必将", "一定会", "确定会", "毫无疑问",
    "proves", "proved", "will definitely", "certainly", "conclusively",
)
_CAUSALITY_MARKERS = (
    "导致", "造成", "引发", "因此", "从而", "使得", "意味着", "这说明",
    "cause", "caused", "causes", "lead to", "leads to", "result in", "results in",
    "prove", "proves", "allow", "allows", "enable", "enables", "disrupt", "disrupted",
)
_ADVICE_MARKERS = (
    "建议", "应当", "应该", "需要减少", "需要避免", "佩戴", "务必", "请勿",
    "should", "must", "recommended", "wear a mask", "avoid outdoor",
)
_HEALTH_MARKERS = (
    "健康", "呼吸", "疾病", "医疗", "感染", "受伤", "死亡", "中毒",
    "health", "respiratory", "disease", "medical", "infection", "injury", "death",
)
_AUDIENCE_MARKERS = (
    "儿童", "老人", "老年人", "孕妇", "患者", "敏感人群", "消费者", "居民",
    "旅客", "乘客", "航空公司", "机场运营方", "普通人", "人们",
    "children", "elderly", "pregnant", "patients", "consumers", "residents",
    "travellers", "travelers", "passengers", "vulnerable groups",
)
_SCOPE_LEVELS = (
    (3, ("所有", "全部", "任何人", "全球", "普遍", "总是", "all", "every", "global", "always")),
    (2, ("大范围", "大量", "广泛", "数千", "many", "widespread", "thousands")),
    (1, ("部分", "有限", "一些", "some", "limited")),
)

# These bilingual concept families catch material additions without requiring literal
# source copying. They deliberately cover high-risk and observed regression concepts.
_CONCEPTS = {
    "contract": ("contract", "合同"),
    "award": ("award", "授予"),
    "future": ("future", "未来"),
    "mission": ("mission", "task", "任务"),
    "participation": ("participat", "compete", "参与", "竞标"),
    "engine": ("engine", "发动机"),
    "failure": ("failure", "fault", "故障"),
    "health": ("health", "respiratory", "健康", "呼吸"),
    "risk": ("risk", "threat", "danger", "风险", "威胁", "危险"),
    "mask": ("mask", "口罩"),
    "particles": ("particle", "particulate", "颗粒物"),
    "outdoor_activity": ("outdoor", "户外活动"),
    "material_transport": ("freight", "cargo", "物资运输"),
    "reroute": ("reroute", "divert", "改道", "绕飞"),
    "delay": ("delay", "延误"),
    "cancel": ("cancel", "取消"),
}

_EMPTY_EXAMPLE_MARKERS = (
    "没有合适的例子", "无合适例子", "不适用", "无法进一步确定其他影响",
    "no suitable example", "not applicable",
)
_GENERALIZATION_MARKERS = (
    "类似", "以后", "未来", "下一次", "再次发生", "一般会", "通常会",
    "similar event", "in the future", "next time", "generally",
)
_CURRENT_EVENT_MARKERS = (
    "这次事件", "本次事件", "这次火山喷发", "本次火山喷发", "报告的这次事件",
    "this event", "this eruption",
)
_ANALOGY_MARKERS = (
    "可以把", "可以将", "理解为", "想象成", "打个比方", "如果把", "像是",
    "understand it as", "imagine it as", "as an analogy",
)
_GENERAL_MECHANISM_RE = re.compile(
    r"(?:说明|表明|意味着).{0,24}(?:可以|能够|会|可能|将).{0,24}(?:影响|导致|造成|引发)"
)


def _normalized(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _contains_any(text, markers):
    folded = _normalized(text)
    return any(marker.casefold() in folded for marker in markers)


def infer_establishment_status(text):
    none_established = _contains_any(text, _NONE_ESTABLISHED_MARKERS) or any(
        pattern.search(str(text or "")) for pattern in _NONE_ESTABLISHED_PATTERNS
    )
    # A negative preface must not mask a later positive assertion.
    if none_established and not _contains_any(text, _CONTRAST_MARKERS):
        return EstablishmentStatus.NONE_ESTABLISHED
    return EstablishmentStatus.ESTABLISHED


def classify_example(text):
    value = str(text or "").strip()
    if not value or _contains_any(value, _EMPTY_EXAMPLE_MARKERS):
        return ExampleSemanticType.EMPTY_OR_NOT_APPLICABLE
    if value.endswith(("?", "？")):
        return ExampleSemanticType.UNSUPPORTED_REAL_WORLD_EXAMPLE
    if _contains_any(value, _GENERALIZATION_MARKERS) or _GENERAL_MECHANISM_RE.search(value):
        return ExampleSemanticType.HYPOTHETICAL_GENERALIZATION
    if _contains_any(value, _ANALOGY_MARKERS):
        return ExampleSemanticType.ABSTRACT_ANALOGY
    if _contains_any(value, _CURRENT_EVENT_MARKERS):
        return ExampleSemanticType.EVENT_RESTATEMENT
    return ExampleSemanticType.UNSUPPORTED_REAL_WORLD_EXAMPLE


def _date_matches(text):
    matches = []
    occupied = []
    for match in _CHINESE_DATE_RE.finditer(text):
        value = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        matches.append((match.span(), value))
        occupied.append(match.span())
    for match in _ENGLISH_DATE_RE.finditer(text):
        month = _MONTH_NUMBERS[match.group(1).replace(".", "").casefold()]
        value = f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}"
        matches.append((match.span(), value))
        occupied.append(match.span())
    for match in _ENGLISH_DATE_RE_REVERSED.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        month = _MONTH_NUMBERS[match.group(2).replace(".", "").casefold()]
        value = f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(1)):02d}"
        matches.append((match.span(), value))
        occupied.append(match.span())
    for match in _NUMERIC_DATE_RE.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        value = f"{int(match.group(1)):04d}"
        if match.group(2):
            value += f"-{int(match.group(2)):02d}"
        if match.group(3):
            value += f"-{int(match.group(3)):02d}"
        matches.append((match.span(), value))
    return matches


def _numbers(text):
    date_spans = [span for span, _ in _date_matches(text)]
    return {
        match.group(0).replace(",", "").casefold()
        for match in _NUMBER_RE.finditer(text)
        if not any(match.start() < end and match.end() > start for start, end in date_spans)
    }


def _dates(text):
    return {value for _, value in _date_matches(text)}


def _canonical_name(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", _normalized(value))


_ENTITY_ALIASES = (
    ("美国证券交易委员会", "美国证交会", "securitiesandexchangecommission", "sec"),
    ("国家统计局", "nationalbureauofstatisticsofchina"),
    ("国家发展和改革委员会", "nationaldevelopmentandreformcommissionofchina", "ndrc"),
    (
        "federaldepositinsurancecorporation",
        "联邦存款保险公司",
        "联邦存款保险委员会",
        "美国联邦存款保险公司",
        "美国联邦存款保险委员会",
        "fdic",
    ),
    (
        "commodityfuturestradingcommission",
        "商品期货交易委员会",
        "cftc",
    ),
)

_GENERIC_CHINESE_ENTITY_RE = re.compile(
    r"^(?:及其|其|该|上述|这些)?(?:控制的|旗下的|关联的|拥有的|所属的)?"
    r"(?:约|至少|超过|近)?(?:\d+|[一二两三四五六七八九十百]+)家(?:相关|关联|下属)?公司$"
)


def _canonical_entity(value):
    canonical = _canonical_name(value)
    for aliases in _ENTITY_ALIASES:
        for alias in aliases:
            if canonical == alias or canonical == f"the{alias}":
                return aliases[0]
            if len(alias) > 4 and alias in canonical:
                return aliases[0]
    return canonical


def _entities(text):
    english = set(_ENGLISH_ENTITY_RE.findall(text))
    chinese = {
        value for value in _CHINESE_ENTITY_RE.findall(text)
        if not _GENERIC_CHINESE_ENTITY_RE.fullmatch(value)
    }
    entities = {
        _canonical_entity(value) for value in english | chinese if _canonical_entity(value)
    }
    canonical_text = _canonical_name(text)
    normalized_text = _normalized(text)
    for aliases in _ENTITY_ALIASES:
        if any(
            (len(alias) > 4 and alias in canonical_text)
            or (len(alias) <= 4 and re.search(rf"\b{re.escape(alias)}\b", normalized_text))
            for alias in aliases
        ):
            entities.add(aliases[0])
    return entities

_LOCATION_ALIASES = (
    ("月球南极地区", "月球南极", "lunarsouthpoleregion", "lunarsouthpole"),
)


def _unsupported_entities(text, source_text):
    source_canonical = _canonical_name(source_text)
    source_entities = _entities(source_text)
    unsupported = set()
    for entity in _entities(text):
        if entity in source_entities or entity in source_canonical:
            continue
        unsupported.add(entity)
    return unsupported


def _locations(text):
    values = set(_ENGLISH_LOCATION_RE.findall(text)) | set(_CHINESE_LOCATION_RE.findall(text))
    return {_canonical_name(value) for value in values if _canonical_name(value)}


def _unsupported_locations(text, source_text):
    source_canonical = _canonical_name(source_text)
    unsupported = set()
    for location in _locations(text):
        if location in source_canonical:
            continue
        if any(
            location in aliases and any(alias in source_canonical for alias in aliases)
            for aliases in _LOCATION_ALIASES
        ):
            continue
        unsupported.add(location)
    return unsupported


def _concepts(text):
    folded = _normalized(text)
    return {
        name for name, terms in _CONCEPTS.items()
        if any(term.casefold() in folded for term in terms)
    }


def _scope_level(text):
    folded = _normalized(text)
    return max((level for level, terms in _SCOPE_LEVELS if any(term in folded for term in terms)), default=0)


def _append(reasons, reason):
    value = reason.value if isinstance(reason, GroundingReason) else str(reason)
    if value not in reasons:
        reasons.append(value)


def _reference_reasons(unit, references):
    reasons = []
    if unit.content_type == ContentUnitType.BACKGROUND:
        return reasons
    if unit.content_type == ContentUnitType.SPECULATION:
        return reasons
    if unit.impact_level == ImpactLevel.NONE_ESTABLISHED:
        return reasons

    if not unit.claim_ids:
        if unit.content_type == ContentUnitType.FACT:
            _append(reasons, GroundingReason.FACT_NO_CLAIM)
        elif unit.content_type == ContentUnitType.EXPLANATION:
            _append(reasons, GroundingReason.EXPLANATION_NEW_FACT)
        else:
            _append(reasons, GroundingReason.EXAMPLE_REAL_WORLD_UNSUPPORTED)
        return reasons

    claim_refs = [reference for reference in references if reference.claim_id in unit.claim_ids]
    if {reference.claim_id for reference in claim_refs} != set(unit.claim_ids):
        _append(reasons, GroundingReason.FACT_NO_CLAIM)
    if unit.evidence_ids:
        selected = [reference for reference in claim_refs if reference.evidence_id in unit.evidence_ids]
        if {reference.evidence_id for reference in selected} != set(unit.evidence_ids):
            _append(reasons, GroundingReason.FACT_EVIDENCE_INVALID)
    else:
        selected = claim_refs
    if not selected:
        _append(reasons, GroundingReason.FACT_NO_SUPPORTING_EVIDENCE)
    elif any(not reference.valid for reference in selected):
        _append(reasons, GroundingReason.FACT_EVIDENCE_INVALID)
    return reasons


def _semantic_reasons(unit, references):
    reasons = []
    valid = [
        reference for reference in references
        if reference.valid
        and reference.claim_id in unit.claim_ids
        and (not unit.evidence_ids or reference.evidence_id in unit.evidence_ids)
    ]
    source_text = " ".join(reference.source_text for reference in valid)
    none_established = unit.impact_level == ImpactLevel.NONE_ESTABLISHED

    new_dates = _dates(unit.text) - _dates(source_text)
    new_numbers = _numbers(unit.text) - _numbers(source_text) - new_dates
    if new_dates:
        _append(reasons, GroundingReason.FACT_NEW_DATE)
    if new_numbers:
        _append(reasons, GroundingReason.FACT_NEW_NUMBER)
    if _unsupported_entities(unit.text, source_text):
        _append(reasons, GroundingReason.FACT_NEW_ENTITY)
    if _unsupported_locations(unit.text, source_text):
        _append(reasons, GroundingReason.FACT_NEW_LOCATION)

    # A validated absence boundary makes no positive causal, impact, or scope
    # assertion. New entities, locations, dates, and numbers remain guarded.
    if none_established:
        return reasons

    source_concepts = _concepts(source_text)
    new_concepts = _concepts(unit.text) - source_concepts
    if "health" in new_concepts or (
        _contains_any(unit.text, _HEALTH_MARKERS) and not _contains_any(source_text, _HEALTH_MARKERS)
    ):
        _append(reasons, GroundingReason.FACT_NEW_HEALTH_IMPACT)
    if not none_established and (
        _contains_any(unit.text, _AUDIENCE_MARKERS)
        and not _contains_any(source_text, _AUDIENCE_MARKERS)
    ):
        _append(reasons, GroundingReason.FACT_NEW_AUDIENCE)
    if _contains_any(unit.text, _ADVICE_MARKERS) and not _contains_any(source_text, _ADVICE_MARKERS):
        _append(reasons, GroundingReason.UNSUPPORTED_ADVICE)

    if new_concepts:
        if unit.content_type == ContentUnitType.EXPLANATION:
            _append(reasons, GroundingReason.EXPLANATION_NEW_FACT)
        elif unit.content_type == ContentUnitType.EXAMPLE:
            _append(reasons, GroundingReason.EXAMPLE_REAL_WORLD_UNSUPPORTED)
        elif unit.content_type == ContentUnitType.FACT:
            _append(reasons, GroundingReason.FACT_UNSUPPORTED_ASSERTION)

    source_has_causality = _contains_any(source_text, _CAUSALITY_MARKERS)
    if _contains_any(unit.text, _CAUSALITY_MARKERS) and not source_has_causality:
        if unit.content_type == ContentUnitType.FACT:
            _append(reasons, GroundingReason.FACT_NEW_CAUSALITY)
        elif unit.content_type == ContentUnitType.EXPLANATION:
            _append(reasons, GroundingReason.EXPLANATION_CAUSALITY_EXPANSION)
        elif unit.content_type == ContentUnitType.EXAMPLE and new_concepts:
            _append(reasons, GroundingReason.EXAMPLE_REAL_WORLD_UNSUPPORTED)

    if _scope_level(unit.text) > _scope_level(source_text):
        reason = (
            GroundingReason.EXPLANATION_SCOPE_EXPANSION
            if unit.content_type == ContentUnitType.EXPLANATION
            else GroundingReason.FACT_SCOPE_EXPANSION
        )
        _append(reasons, reason)
    if (
        _contains_any(source_text, _UNCERTAINTY_MARKERS)
        and _contains_any(unit.text, _CERTAINTY_MARKERS)
    ):
        _append(reasons, GroundingReason.FACT_CERTAINTY_EXPANSION)
    unsupported_uncertainty = (
        _contains_any(unit.text, _UNCERTAINTY_MARKERS)
        and not _contains_any(source_text, _UNCERTAINTY_MARKERS)
    )
    if unsupported_uncertainty:
        if unit.content_type == ContentUnitType.FACT:
            _append(reasons, GroundingReason.SPECULATION_NOT_ALLOWED)
        elif unit.content_type == ContentUnitType.EXPLANATION and (
            new_concepts or _contains_any(unit.text, _HEALTH_MARKERS + _AUDIENCE_MARKERS)
        ):
            _append(reasons, GroundingReason.SPECULATION_NOT_ALLOWED)
        elif (
            unit.content_type == ContentUnitType.EXAMPLE
            and unit.example_semantic_type == ExampleSemanticType.HYPOTHETICAL_GENERALIZATION
        ):
            _append(reasons, GroundingReason.SPECULATION_NOT_ALLOWED)

    if unit.impact_level in {ImpactLevel.DIRECT, ImpactLevel.POTENTIAL, ImpactLevel.LIMITED}:
        output_has_impact = _contains_any(unit.text, _HEALTH_MARKERS + _AUDIENCE_MARKERS)
        source_has_impact = _contains_any(source_text, _HEALTH_MARKERS + _AUDIENCE_MARKERS)
        if output_has_impact and not source_has_impact:
            _append(reasons, GroundingReason.IMPACT_NOT_ESTABLISHED)
    return reasons


def validate_content_units(content_units, references):
    if not content_units:
        return GroundingResult(
            GroundingStatus.FAIL,
            (GroundingReason.GROUNDING_EMPTY.value,),
            (),
        )
    unit_results = []
    for unit in content_units:
        if unit.content_type == ContentUnitType.EXAMPLE:
            unit = replace(
                unit,
                example_semantic_type=unit.example_semantic_type or classify_example(unit.text),
            )
        reasons = _reference_reasons(unit, references)
        if not unit.establishment_status_valid:
            _append(reasons, GroundingReason.ESTABLISHMENT_STATUS_MISMATCH)
        if unit.content_type == ContentUnitType.BACKGROUND:
            _append(reasons, GroundingReason.BACKGROUND_NOT_SUPPORTED)
        elif unit.content_type == ContentUnitType.SPECULATION:
            _append(reasons, GroundingReason.SPECULATION_NOT_ALLOWED)
        else:
            if unit.content_type == ContentUnitType.EXAMPLE:
                if unit.example_semantic_type == ExampleSemanticType.HYPOTHETICAL_GENERALIZATION:
                    _append(reasons, GroundingReason.EXAMPLE_HYPOTHETICAL_GENERALIZATION)
                elif (
                    unit.example_semantic_type == ExampleSemanticType.UNSUPPORTED_REAL_WORLD_EXAMPLE
                    and unit.text.strip().endswith(("?", "？"))
                ):
                    _append(reasons, GroundingReason.EXAMPLE_INCOMPLETE)
                elif (
                    unit.example_semantic_type == ExampleSemanticType.UNSUPPORTED_REAL_WORLD_EXAMPLE
                    and not _contains_any(unit.text, _EXAMPLE_MARKERS)
                ):
                    _append(reasons, GroundingReason.EXAMPLE_NOT_MARKED)
            for reason in _semantic_reasons(unit, references):
                _append(reasons, reason)

        severe = GroundingReason.FACT_EVIDENCE_INVALID.value in reasons
        status = (
            GroundingStatus.FAIL if severe
            else GroundingStatus.REVISION_REQUIRED if reasons
            else GroundingStatus.PASS
        )
        codes = tuple(reasons or [GroundingReason.GROUNDING_PASS.value])
        unit_results.append(ContentUnitResult(unit, status, codes))

    if any(result.status == GroundingStatus.FAIL for result in unit_results):
        status = GroundingStatus.FAIL
    elif any(result.status == GroundingStatus.REVISION_REQUIRED for result in unit_results):
        status = GroundingStatus.REVISION_REQUIRED
    else:
        status = GroundingStatus.PASS
    reason_codes = []
    for result in unit_results:
        for reason in result.reason_codes:
            if reason != GroundingReason.GROUNDING_PASS.value and reason not in reason_codes:
                reason_codes.append(reason)
    if not reason_codes:
        reason_codes.append(GroundingReason.GROUNDING_PASS.value)
    return GroundingResult(status, tuple(reason_codes), tuple(unit_results))


def article_content_units(content, claim_ids, evidence_ids):
    """Adapt ArticleVersion-like content into deterministic grounding units."""
    shared = {"claim_ids": claim_ids, "evidence_ids": evidence_ids}
    fields = (
        ("title", ContentUnitType.FACT, None),
        ("one_sentence_summary", ContentUnitType.FACT, None),
        ("what_happened", ContentUnitType.FACT, None),
        ("why_it_matters", ContentUnitType.EXPLANATION, None),
        ("who_is_affected", ContentUnitType.EXPLANATION, ImpactLevel.DIRECT),
        ("example", ContentUnitType.EXAMPLE, None),
    )
    units = []
    for field, content_type, impact_level in fields:
        if isinstance(content, dict):
            value = content.get(field)
            if field == "one_sentence_summary" and value is None:
                value = content.get("summary")
        else:
            value = getattr(content, field, "")
        text = str(value or "").strip()
        if not text:
            continue
        establishment_status = None
        establishment_status_valid = True
        example_status = None
        example_semantic_type = None
        if field in {"why_it_matters", "who_is_affected"}:
            explicit = content.get(f"{field}_status") if isinstance(content, dict) else None
            inferred = infer_establishment_status(text)
            establishment_status = EstablishmentStatus(explicit) if explicit else inferred
            establishment_status_valid = not explicit or establishment_status == inferred
            if inferred == EstablishmentStatus.NONE_ESTABLISHED:
                impact_level = ImpactLevel.NONE_ESTABLISHED
        elif field == "example":
            explicit = content.get("example_status") if isinstance(content, dict) else None
            example_status = ExampleStatus(explicit) if explicit else ExampleStatus.PROVIDED
            example_semantic_type = classify_example(text)
        units.append(ContentUnit(
            text=text,
            content_type=content_type,
            section_key=field,
            impact_level=impact_level,
            establishment_status=establishment_status,
            example_status=example_status,
            example_semantic_type=example_semantic_type,
            establishment_status_valid=establishment_status_valid,
            **shared,
        ))
    return tuple(units)


def article_grounding_references(article_version_or_id):
    """Return the validated Claim/Evidence reference set for an ArticleVersion."""
    from apps.publishing.models import ArticleVersion

    version_id = getattr(article_version_or_id, "pk", article_version_or_id)
    version = ArticleVersion.objects.select_related("article__event").get(pk=version_id)
    links = list(
        version.claim_links.select_related("claim__event").prefetch_related(
            "claim__evidence__raw_item__source", "claim__evidence__source"
        ).order_by("display_order", "pk")
    )
    references = []
    for link in links:
        claim = link.claim
        for evidence in claim.evidence.all().order_by("pk"):
            _, quote_status, _ = validate_evidence_hint(
                version.article.event,
                claim.statement,
                {"raw_item_id": evidence.raw_item_id, "quote": evidence.evidence_text},
            )
            references.append(GroundingReference(
                claim_id=claim.pk,
                evidence_id=evidence.pk,
                claim_text=claim.statement,
                evidence_text=evidence.evidence_text,
                source_name=evidence.source.name,
                claim_supported=claim.verification_status == VerificationStatus.SUPPORTED,
                evidence_supports=evidence.stance == EvidenceStance.SUPPORTS,
                relationship_valid=(
                    claim.event_id == version.article.event_id
                    and evidence.raw_item.event_id == version.article.event_id
                ),
                quote_valid=quote_status == EvidenceHintStatus.VALID,
                source_valid=(
                    evidence.source_id == evidence.raw_item.source_id
                    and evidence.source.active
                    and evidence.evidence_type in EvidenceType.values
                ),
            ))
    return version, tuple(links), tuple(references)


def validate_article_content_grounding(content, source_version_or_id):
    """Validate candidate Article content against an existing version's references."""
    _, links, references = article_grounding_references(source_version_or_id)
    claim_ids = tuple(link.claim_id for link in links)
    evidence_ids = tuple(reference.evidence_id for reference in references)
    return validate_content_units(
        article_content_units(content, claim_ids, evidence_ids), references
    )


def validate_article_grounding(article_version_or_id):
    """Validate an existing ArticleVersion without writing models or audit records."""
    version, links, references = article_grounding_references(article_version_or_id)
    claim_ids = tuple(link.claim_id for link in links)
    evidence_ids = tuple(reference.evidence_id for reference in references)
    units = article_content_units(version, claim_ids, evidence_ids)
    return validate_content_units(units, references)
