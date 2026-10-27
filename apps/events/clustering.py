import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum


_CJK = "\u3400-\u4dbf\u4e00-\u9fff"
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}a-zA-Z0-9])|(?<=[a-zA-Z0-9])\s+(?=[{_CJK}])")
_SOURCE_SUFFIX = re.compile(
    r"\s*[!！?？。]*\s*(?:--|——|—|\|)\s*[^—|]{0,24}(?:媒体|新闻网|新闻|日报|电视台|广播)$",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[!！?？,，.。;；:：‘’“”\"'()（）\[\]【】{}<>《》&|/\\—–_-]+")
_NUMBER = re.compile(r"(?<![a-z])\d+(?:\.\d+)?(?:%|bp|bps|个基点)?", re.IGNORECASE)
_CORPORATE_ENTITY = re.compile(
    r"\b([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,6}\s+"
    r"(?:(?i:Bancshares|Bancorp|Bank|Holdings|Group|Corporation|Corp|Inc|Ltd|LLC|"
    r"Commission|Agency|University|Institute|Ministry)))\b"
)
_CJK_ENTITY = re.compile(
    rf"([{_CJK}A-Za-z0-9]{{2,30}}(?:公司|集团|银行|央行|委员会|政府|大学|研究院|研究所|机构))"
)
_LEADING_SUBJECT = re.compile(
    r"^(.+?)\s+(?:announces?|launches?|unveils?|releases?|introduces?|approves?|"
    r"acquires?|merges?|addresses?|signs?|opens?|closes?|cuts?|raises?|warns?|reports?|confirms?)\b",
    re.IGNORECASE,
)
_PRODUCT_OBJECT = re.compile(
    r"\b(?:launches?|unveils?|releases?|introduces?)\s+(?:the\s+|new\s+)*([^:;|]+)$",
    re.IGNORECASE,
)
_PRODUCT_MARKER = re.compile(
    r"\b(?:iphone|ipad|pixel|galaxy|model|chip|gpu|processor|vehicle|car|suv|sedan|"
    r"platform|operating system|os)\b",
    re.IGNORECASE,
)
_PROPER_CONNECTORS = {"a", "an", "and", "for", "of", "the", "to"}
_CJK_SPEECH_EVENT = re.compile(
    rf"^(?P<speaker>[{_CJK}]{{2,20}}?)(?:在|出席)"
    rf"(?P<venue>[{_CJK}a-z0-9]{{4,100}}(?:会议|会晤|论坛|峰会|大会|仪式|座谈会|研讨会|发布会|记者会))"
    r"(?:的(?:重要|主旨)?讲话(?:全文)?|并(?:发表|作)(?:重要|主旨)?讲话(?:全文)?)$"
)
_NAVIGATION_ONLY_TITLES = {"更多", "查看更多", "more"}

REGION_GROUPS = {
    "china": {"中国", "中国大陆", "china", "chinese"},
    "united-states": {"美国", "美联储", "usa", "u.s.", "united states", "federal reserve", "fed"},
    "european-union": {"欧盟", "欧洲央行", "eu", "european union", "ecb"},
    "united-kingdom": {"英国", "uk", "u.k.", "united kingdom", "bank of england"},
    "japan": {"日本", "日本央行", "japan", "bank of japan", "boj"},
    "france": {"法国", "france", "french"},
    "germany": {"德国", "germany", "german"},
    "india": {"印度", "india", "indian"},
    "canada": {"加拿大", "canada", "canadian"},
    "australia": {"澳大利亚", "australia", "australian"},
    "brazil": {"巴西", "brazil", "brazilian"},
    "russia": {"俄罗斯", "russia", "russian"},
    "ukraine": {"乌克兰", "ukraine", "ukrainian"},
}
ENTITY_GROUPS = {
    "openai": {"openai"},
    "google": {"google", "谷歌", "alphabet"},
    "apple": {"apple", "苹果"},
    "microsoft": {"microsoft", "微软"},
    "meta": {"meta", "facebook"},
    "amazon": {"amazon", "亚马逊"},
    "tesla": {"tesla", "特斯拉"},
    "spacex": {"spacex"},
    "biden": {"biden", "拜登"},
    "trump": {"trump", "特朗普"},
    "musk": {"elon musk", "musk", "马斯克"},
}


class ClusterAction(StrEnum):
    ASSIGN_EXISTING = "ASSIGN_EXISTING"
    CREATE_NEW = "CREATE_NEW"
    NO_ACTION = "NO_ACTION"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass(frozen=True)
class MatchMetrics:
    trigram: float
    token_overlap: float
    time_score: float
    conflict_penalty: float
    final_confidence: float

    def as_dict(self):
        return {
            "trigram": round(self.trigram, 4),
            "token_overlap": round(self.token_overlap, 4),
            "time_score": round(self.time_score, 4),
            "conflict_penalty": round(self.conflict_penalty, 4),
            "final_confidence": round(self.final_confidence, 4),
        }


@dataclass(frozen=True)
class CandidateMatch:
    event: object
    metrics: MatchMetrics
    method: str
    conflicts: tuple[str, ...] = ()

    def as_dict(self):
        return {
            "event_id": self.event.pk,
            "title": self.event.title,
            "method": self.method,
            "conflicts": list(self.conflicts),
            **self.metrics.as_dict(),
        }


@dataclass(frozen=True)
class ClusterDecision:
    action: ClusterAction
    event: object | None
    confidence: float
    method: str
    reason: str
    candidates: tuple[CandidateMatch, ...] = field(default_factory=tuple)


def normalize_event_title(title: str | None) -> str:
    if not title:
        return ""
    value = unicodedata.normalize("NFKC", html.unescape(str(title))).strip()
    value = _SOURCE_SUFFIX.sub("", value)
    value = _PUNCTUATION.sub(" ", value).casefold()
    value = re.sub(r"\s+", " ", value).strip()
    previous = None
    while previous != value:
        previous = value
        value = _SPACE_BETWEEN_CJK.sub("", value)
    return value


def is_navigation_only_title(title: str | None) -> bool:
    """Match standalone list-navigation labels without blocking real headlines."""
    return normalize_event_title(title) in _NAVIGATION_ONLY_TITLES


def event_cluster_key(normalized_title: str) -> str:
    return hashlib.sha256(normalized_title.encode("utf-8")).hexdigest() if normalized_title else ""


def mixed_language_tokens(title: str | None) -> set[str]:
    normalized = normalize_event_title(title)
    tokens = set(re.findall(r"[a-z]+[a-z0-9]*(?:[._-][a-z0-9]+)*|\d+(?:\.\d+)?", normalized))
    for run in re.findall(rf"[{_CJK}]+", normalized):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def token_overlap(left: str | None, right: str | None) -> float:
    left_tokens = mixed_language_tokens(left)
    right_tokens = mixed_language_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def speech_event_signature(title: str | None) -> tuple[str, str] | None:
    """Identify deterministic variants of one speaker's address at one named event."""
    match = _CJK_SPEECH_EVENT.fullmatch(normalize_event_title(title))
    if not match:
        return None
    return match.group("speaker"), match.group("venue")


def _matched_groups(title: str, groups: dict[str, set[str]]) -> set[str]:
    folded = unicodedata.normalize("NFKC", title or "").casefold()
    return {
        group
        for group, aliases in groups.items()
        if any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded) for alias in aliases)
    }


def _normalized_facts(values) -> set[str]:
    return {normalize_event_title(value) for value in values if normalize_event_title(value)}


def _corporate_entities(title: str) -> set[str]:
    return _normalized_facts([
        *_CORPORATE_ENTITY.findall(title),
        *_CJK_ENTITY.findall(title),
    ])


def _looks_like_proper_subject(value: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z0-9&.'-]*", value)
    meaningful = [word for word in words if word.casefold() not in _PROPER_CONNECTORS]
    return len(meaningful) >= 2 and all(word[0].isupper() for word in meaningful)


def _leading_subject(title: str) -> set[str]:
    match = _LEADING_SUBJECT.search(title.strip())
    if not match:
        return set()
    subject = match.group(1).strip(" ,:;-")
    if _looks_like_proper_subject(subject) or _CJK_ENTITY.fullmatch(subject):
        return _normalized_facts([subject])
    return set()


def _product_objects(title: str) -> set[str]:
    match = _PRODUCT_OBJECT.search(title.strip())
    if not match or not _PRODUCT_MARKER.search(match.group(1)):
        return set()
    return _normalized_facts([match.group(1).strip(" ,:;-")])


def _has_disjoint_facts(left: set[str], right: set[str]) -> bool:
    return bool(left and right and left.isdisjoint(right))


def detect_conflicts(left: str | None, right: str | None) -> tuple[str, ...]:
    left = left or ""
    right = right or ""
    conflicts = []
    left_numbers = set(_NUMBER.findall(unicodedata.normalize("NFKC", left).casefold()))
    right_numbers = set(_NUMBER.findall(unicodedata.normalize("NFKC", right).casefold()))
    if left_numbers != right_numbers and (left_numbers or right_numbers):
        conflicts.append("different_numbers_or_models")
    for label, groups in (("different_regions", REGION_GROUPS), ("different_entities", ENTITY_GROUPS)):
        left_groups = _matched_groups(left, groups)
        right_groups = _matched_groups(right, groups)
        if left_groups and right_groups and left_groups.isdisjoint(right_groups):
            conflicts.append(label)
    if _has_disjoint_facts(_corporate_entities(left), _corporate_entities(right)):
        conflicts.append("different_named_entities")
    if _has_disjoint_facts(_leading_subject(left), _leading_subject(right)):
        conflicts.append("different_subject_entities")
    if _has_disjoint_facts(_product_objects(left), _product_objects(right)):
        conflicts.append("different_products")
    return tuple(conflicts)


def calculate_metrics(trigram, overlap, time_score, conflicts, *, exact=False, structural_match=False) -> MatchMetrics:
    penalty = 1.0 if conflicts else 0.0
    confidence = (0.55 * trigram) + (0.30 * overlap) + (0.15 * time_score)
    if exact and not conflicts:
        confidence = 1.0
    elif structural_match and not conflicts:
        confidence = max(confidence, 0.95)
    elif trigram >= 0.65 and overlap >= 0.80 and not conflicts:
        confidence = max(confidence, 0.90)
    confidence = max(0.0, min(1.0, confidence - penalty))
    return MatchMetrics(trigram, overlap, time_score, penalty, confidence)
