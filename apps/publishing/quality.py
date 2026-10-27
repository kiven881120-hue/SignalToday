from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class ArticleQualityMode(str, Enum):
    STANDARD = "STANDARD"
    BRIEF = "BRIEF"


class QualityStatus(str, Enum):
    PASS = "PASS"
    TOO_THIN = "TOO_THIN"
    FAIL = "FAIL"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"


class InformationGainStatus(str, Enum):
    PASS = "PASS"
    TOO_THIN = "TOO_THIN"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class QualityLevel(str, Enum):
    LEVEL_0 = "LEVEL_0"
    LEVEL_1 = "LEVEL_1"
    LEVEL_2 = "LEVEL_2"
    LEVEL_3 = "LEVEL_3"
    LEVEL_4 = "LEVEL_4"
    LEVEL_5 = "LEVEL_5"


class WhyStatus(str, Enum):
    WHY_ESTABLISHED = "WHY_ESTABLISHED"
    WHY_NONE_ESTABLISHED = "WHY_NONE_ESTABLISHED"


class WhoStatus(str, Enum):
    WHO_ESTABLISHED = "WHO_ESTABLISHED"
    WHO_NONE_ESTABLISHED = "WHO_NONE_ESTABLISHED"


class ExampleStatus(str, Enum):
    EXAMPLE_ABSTRACT_ANALOGY = "EXAMPLE_ABSTRACT_ANALOGY"
    EXAMPLE_NONE = "EXAMPLE_NONE"
    EXAMPLE_EVENT_RESTATEMENT = "EXAMPLE_EVENT_RESTATEMENT"
    EXAMPLE_UNSUPPORTED_REAL_WORLD = "EXAMPLE_UNSUPPORTED_REAL_WORLD"
    EXAMPLE_HYPOTHETICAL_GENERALIZATION = "EXAMPLE_HYPOTHETICAL_GENERALIZATION"
    EXAMPLE_FUTURE_PREDICTION = "EXAMPLE_FUTURE_PREDICTION"


class InformationKind(str, Enum):
    CORE_FACT = "CORE_FACT"
    MEANING = "MEANING"
    IMPACT_SCOPE = "IMPACT_SCOPE"
    AFFECTED_PARTY = "AFFECTED_PARTY"
    USEFUL_EXPLANATION = "USEFUL_EXPLANATION"
    CONTEXT_MECHANISM = "CONTEXT_MECHANISM"
    EPISTEMIC_BOUNDARY = "EPISTEMIC_BOUNDARY"
    ABSTRACT_ANALOGY = "ABSTRACT_ANALOGY"


class SectionAction(str, Enum):
    KEEP = "KEEP"
    OMIT = "OMIT"
    REVISE = "REVISE"


class QualityReason(str, Enum):
    QUALITY_PASS = "QUALITY_PASS"
    GROUNDING_NOT_PASS = "GROUNDING_NOT_PASS"
    CORE_FACT_MISSING = "CORE_FACT_MISSING"
    NO_GAIN_BEYOND_TITLE = "NO_GAIN_BEYOND_TITLE"
    STANDARD_MEANING_GAIN_MISSING = "STANDARD_MEANING_GAIN_MISSING"
    WHY_REPEATS_CORE = "WHY_REPEATS_CORE"
    WHO_REPEATS_CORE = "WHO_REPEATS_CORE"
    EXAMPLE_EVENT_RESTATEMENT = "EXAMPLE_EVENT_RESTATEMENT"
    EXAMPLE_UNSUPPORTED_REAL_WORLD = "EXAMPLE_UNSUPPORTED_REAL_WORLD"
    EXAMPLE_HYPOTHETICAL_GENERALIZATION = "EXAMPLE_HYPOTHETICAL_GENERALIZATION"
    EXAMPLE_FUTURE_PREDICTION = "EXAMPLE_FUTURE_PREDICTION"
    EXAMPLE_ANALOGY_NO_GAIN = "EXAMPLE_ANALOGY_NO_GAIN"
    STATUS_TEXT_MISMATCH = "STATUS_TEXT_MISMATCH"
    SEMANTIC_ANNOTATION_REQUIRED = "SEMANTIC_ANNOTATION_REQUIRED"


@dataclass(frozen=True)
class InformationUnit:
    """A stable semantic proposition key supplied by a trusted, explainable producer."""

    key: str
    kind: InformationKind

    def __post_init__(self):
        if not str(self.key).strip():
            raise ValueError("InformationUnit.key must not be blank")


@dataclass(frozen=True)
class QualitySection:
    text: str | None = None
    units: tuple[InformationUnit, ...] = ()

    @property
    def present(self):
        return bool(str(self.text or "").strip())


@dataclass(frozen=True)
class ArticleQualityInput:
    article_mode: ArticleQualityMode
    grounding_status: str
    title: QualitySection
    summary: QualitySection
    what_happened: QualitySection
    why_it_matters: QualitySection = QualitySection()
    who_is_affected: QualitySection = QualitySection()
    example: QualitySection = QualitySection()
    why_status: WhyStatus = WhyStatus.WHY_NONE_ESTABLISHED
    who_status: WhoStatus = WhoStatus.WHO_NONE_ESTABLISHED
    example_status: ExampleStatus = ExampleStatus.EXAMPLE_NONE
    upstream_score: Decimal | None = None
    upstream_priority: bool = False


@dataclass(frozen=True)
class SectionQualityResult:
    section_key: str
    action: SectionAction
    novel_unit_keys: tuple[str, ...]
    repeated_unit_keys: tuple[str, ...]
    contribution_kinds: tuple[str, ...]


@dataclass(frozen=True)
class ArticleQualityResult:
    quality_status: QualityStatus
    quality_level: QualityLevel
    article_mode: ArticleQualityMode
    information_gain_score: int
    information_gain_status: InformationGainStatus
    why_status: WhyStatus
    who_status: WhoStatus
    example_status: ExampleStatus
    grounding_status: str
    reasons: tuple[str, ...]
    failed_checks: tuple[str, ...]
    section_results: tuple[SectionQualityResult, ...]
    upstream_score: Decimal | None

    @property
    def quality_gate_passed(self):
        return self.quality_status == QualityStatus.PASS


_GAIN_WEIGHTS = {
    InformationKind.CORE_FACT: 10,
    InformationKind.MEANING: 25,
    InformationKind.IMPACT_SCOPE: 25,
    InformationKind.AFFECTED_PARTY: 20,
    InformationKind.USEFUL_EXPLANATION: 25,
    InformationKind.CONTEXT_MECHANISM: 25,
    InformationKind.EPISTEMIC_BOUNDARY: 5,
    InformationKind.ABSTRACT_ANALOGY: 10,
}
_STANDARD_GAIN_KINDS = {
    InformationKind.MEANING,
    InformationKind.IMPACT_SCOPE,
    InformationKind.AFFECTED_PARTY,
    InformationKind.USEFUL_EXPLANATION,
    InformationKind.CONTEXT_MECHANISM,
}
_FORBIDDEN_EXAMPLE_REASONS = {
    ExampleStatus.EXAMPLE_EVENT_RESTATEMENT: QualityReason.EXAMPLE_EVENT_RESTATEMENT,
    ExampleStatus.EXAMPLE_UNSUPPORTED_REAL_WORLD: QualityReason.EXAMPLE_UNSUPPORTED_REAL_WORLD,
    ExampleStatus.EXAMPLE_HYPOTHETICAL_GENERALIZATION:
        QualityReason.EXAMPLE_HYPOTHETICAL_GENERALIZATION,
    ExampleStatus.EXAMPLE_FUTURE_PREDICTION: QualityReason.EXAMPLE_FUTURE_PREDICTION,
}


def _append(values, reason):
    value = reason.value if isinstance(reason, Enum) else str(reason)
    if value not in values:
        values.append(value)


def _section_result(section_key, section, seen):
    novel = []
    repeated = []
    kinds = []
    local_keys = set()
    for unit in section.units:
        if unit.key in seen or unit.key in local_keys:
            repeated.append(unit.key)
        else:
            novel.append(unit.key)
            kinds.append(unit.kind.value)
        local_keys.add(unit.key)
    seen.update(local_keys)
    action = SectionAction.KEEP if section.present and novel else (
        SectionAction.REVISE if section.present else SectionAction.OMIT
    )
    return SectionQualityResult(
        section_key=section_key,
        action=action,
        novel_unit_keys=tuple(novel),
        repeated_unit_keys=tuple(repeated),
        contribution_kinds=tuple(kinds),
    )


def _contract_reasons(value):
    reasons = []
    sections = (
        ("title", value.title),
        ("summary", value.summary),
        ("what_happened", value.what_happened),
        ("why_it_matters", value.why_it_matters),
        ("who_is_affected", value.who_is_affected),
        ("example", value.example),
    )
    for _, section in sections:
        if section.present != bool(section.units):
            _append(reasons, QualityReason.SEMANTIC_ANNOTATION_REQUIRED)
    if value.why_status == WhyStatus.WHY_ESTABLISHED and not value.why_it_matters.present:
        _append(reasons, QualityReason.STATUS_TEXT_MISMATCH)
    if value.who_status == WhoStatus.WHO_ESTABLISHED and not value.who_is_affected.present:
        _append(reasons, QualityReason.STATUS_TEXT_MISMATCH)
    example_present = value.example.present
    if value.example_status == ExampleStatus.EXAMPLE_NONE and example_present:
        _append(reasons, QualityReason.STATUS_TEXT_MISMATCH)
    if value.example_status != ExampleStatus.EXAMPLE_NONE and not example_present:
        _append(reasons, QualityReason.STATUS_TEXT_MISMATCH)
    return reasons


def evaluate_article_quality(value):
    """Evaluate declared semantic gain without inferring meaning from keyword lists."""

    reasons = _contract_reasons(value)
    failed_checks = list(reasons)
    sections = (
        ("title", value.title),
        ("summary", value.summary),
        ("what_happened", value.what_happened),
        ("why_it_matters", value.why_it_matters),
        ("who_is_affected", value.who_is_affected),
        ("example", value.example),
    )
    seen = set()
    section_results = tuple(
        _section_result(section_key, section, seen) for section_key, section in sections
    )
    result_by_section = {item.section_key: item for item in section_results}

    title_core = any(unit.kind == InformationKind.CORE_FACT for unit in value.title.units)
    all_novel = []
    for section_key, section in sections[1:]:
        novel_keys = set(result_by_section[section_key].novel_unit_keys)
        all_novel.extend(unit for unit in section.units if unit.key in novel_keys)
    score = min(100, sum(_GAIN_WEIGHTS[unit.kind] for unit in all_novel))
    novel_kinds = {unit.kind for unit in all_novel}
    core_gain = InformationKind.CORE_FACT in novel_kinds
    standard_gain = bool(novel_kinds & _STANDARD_GAIN_KINDS)

    if not title_core:
        _append(reasons, QualityReason.CORE_FACT_MISSING)
        _append(failed_checks, QualityReason.CORE_FACT_MISSING)

    why = result_by_section["why_it_matters"]
    if value.why_status == WhyStatus.WHY_ESTABLISHED and not (
        set(why.contribution_kinds) & {kind.value for kind in _STANDARD_GAIN_KINDS}
    ):
        _append(reasons, QualityReason.WHY_REPEATS_CORE)
        _append(failed_checks, QualityReason.WHY_REPEATS_CORE)

    who = result_by_section["who_is_affected"]
    if value.who_status == WhoStatus.WHO_ESTABLISHED and not (
        set(who.contribution_kinds)
        & {InformationKind.AFFECTED_PARTY.value, InformationKind.IMPACT_SCOPE.value}
    ):
        _append(reasons, QualityReason.WHO_REPEATS_CORE)
        _append(failed_checks, QualityReason.WHO_REPEATS_CORE)

    forbidden_example = _FORBIDDEN_EXAMPLE_REASONS.get(value.example_status)
    if forbidden_example:
        _append(reasons, forbidden_example)
        _append(failed_checks, forbidden_example)
    elif value.example_status == ExampleStatus.EXAMPLE_ABSTRACT_ANALOGY:
        example_kinds = set(result_by_section["example"].contribution_kinds)
        if InformationKind.ABSTRACT_ANALOGY.value not in example_kinds:
            _append(reasons, QualityReason.EXAMPLE_ANALOGY_NO_GAIN)
            _append(failed_checks, QualityReason.EXAMPLE_ANALOGY_NO_GAIN)

    if value.article_mode == ArticleQualityMode.BRIEF:
        gain_passed = core_gain or standard_gain
    else:
        gain_passed = standard_gain
        if not standard_gain:
            _append(reasons, QualityReason.STANDARD_MEANING_GAIN_MISSING)
            _append(failed_checks, QualityReason.STANDARD_MEANING_GAIN_MISSING)
    if not gain_passed:
        _append(reasons, QualityReason.NO_GAIN_BEYOND_TITLE)
        _append(failed_checks, QualityReason.NO_GAIN_BEYOND_TITLE)

    annotation_unclear = any(
        reason in failed_checks
        for reason in (
            QualityReason.SEMANTIC_ANNOTATION_REQUIRED.value,
            QualityReason.STATUS_TEXT_MISMATCH.value,
        )
    )
    information_gain_status = (
        InformationGainStatus.NEEDS_REVIEW if annotation_unclear
        else InformationGainStatus.PASS if gain_passed
        else InformationGainStatus.TOO_THIN
    )

    if not title_core:
        level = QualityLevel.LEVEL_0
    elif not gain_passed:
        level = QualityLevel.LEVEL_2 if core_gain else QualityLevel.LEVEL_1
    elif value.article_mode == ArticleQualityMode.BRIEF:
        level = QualityLevel.LEVEL_2
    else:
        rich_kinds = novel_kinds & _STANDARD_GAIN_KINDS
        level = QualityLevel.LEVEL_4 if len(rich_kinds) >= 2 else QualityLevel.LEVEL_3
        if level == QualityLevel.LEVEL_4 and value.upstream_priority:
            level = QualityLevel.LEVEL_5

    if value.grounding_status != "PASS":
        _append(reasons, QualityReason.GROUNDING_NOT_PASS)
        _append(failed_checks, QualityReason.GROUNDING_NOT_PASS)
        quality_status = QualityStatus.BLOCKED
    elif annotation_unclear:
        quality_status = QualityStatus.NEEDS_REVIEW
    elif forbidden_example or QualityReason.EXAMPLE_ANALOGY_NO_GAIN.value in failed_checks:
        quality_status = QualityStatus.FAIL
    elif not title_core:
        quality_status = QualityStatus.FAIL
    elif not gain_passed:
        quality_status = QualityStatus.TOO_THIN
    else:
        quality_status = QualityStatus.PASS

    if quality_status == QualityStatus.PASS:
        reasons = [QualityReason.QUALITY_PASS.value]
        failed_checks = []
    return ArticleQualityResult(
        quality_status=quality_status,
        quality_level=level,
        article_mode=value.article_mode,
        information_gain_score=score,
        information_gain_status=information_gain_status,
        why_status=value.why_status,
        who_status=value.who_status,
        example_status=value.example_status,
        grounding_status=value.grounding_status,
        reasons=tuple(reasons),
        failed_checks=tuple(failed_checks),
        section_results=section_results,
        upstream_score=value.upstream_score,
    )
