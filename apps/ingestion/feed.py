import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .errors import ErrorCode, IngestionError


SUMMARY_LIMIT = 2000
BLOCKED_TAGS = {"script", "style", "iframe", "form", "object", "embed"}
ARTICLE_BLOCKED_TAGS = BLOCKED_TAGS | {"nav", "header", "footer", "aside", "noscript", "svg", "template"}
ARTICLE_CONTAINER_TAGS = {"article", "main", "div", "section"}
ARTICLE_BREAK_TAGS = ARTICLE_CONTAINER_TAGS | {"blockquote", "br", "h1", "h2", "h3", "h4", "li", "p"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
ARTICLE_POSITIVE_TOKENS = {"article", "content", "detail", "editor", "main", "post", "story", "text", "view"}
ARTICLE_NEGATIVE_TOKENS = {
    "author", "breadcrumb", "byline", "copyright", "date", "footer", "header", "menu", "meta",
    "nav", "related", "share", "sidebar", "social", "toolbar",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.blocked_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in BLOCKED_TAGS:
            self.blocked_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in BLOCKED_TAGS and self.blocked_depth:
            self.blocked_depth -= 1

    def handle_data(self, data):
        if not self.blocked_depth:
            self.parts.append(data)


class _ListLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a" and self._href is None:
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_data(self, data):
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, clean_summary(" ".join(self._parts), limit=500)))
            self._href = None
            self._parts = []


class _ArticleBodyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocked_depth = 0
        self.containers = []
        self.frames = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        tokens = set(re.findall(r"[a-z0-9]+", f'{attributes.get("id", "")} {attributes.get("class", "")}'.lower()))
        starts_block = tag in ARTICLE_BLOCKED_TAGS or (
            tag not in {"html", "body"} and bool(tokens & ARTICLE_NEGATIVE_TOKENS)
        )
        if starts_block:
            self.blocked_depth += 1
        if tag in ARTICLE_BREAK_TAGS:
            self._append_break()
        priority = 0
        if not self.blocked_depth and tag in ARTICLE_CONTAINER_TAGS:
            if tag == "article":
                priority = 4
            elif "editor" in tokens or "article" in tokens:
                priority = 3
            elif tag == "main" or tokens & {"content", "post", "story", "text", "view"}:
                priority = 2
            elif "detail" in tokens:
                priority = 1
        container = {"priority": priority, "parts": []} if priority else None
        if container:
            self.containers.append(container)
        if tag not in VOID_TAGS:
            self.frames.append({"tag": tag, "container": container, "starts_block": starts_block})
        elif starts_block:
            self.blocked_depth -= 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ARTICLE_BREAK_TAGS:
            self._append_break()
        match = next((index for index in range(len(self.frames) - 1, -1, -1) if self.frames[index]["tag"] == tag), None)
        if match is None:
            return
        for frame in self.frames[match:]:
            if frame["starts_block"] and self.blocked_depth:
                self.blocked_depth -= 1
        del self.frames[match:]

    def handle_data(self, data):
        if self.blocked_depth or not data.strip():
            return
        for frame in self.frames:
            container = frame["container"]
            if container is not None:
                container["parts"].append(data)

    def _append_break(self):
        if self.blocked_depth:
            return
        for frame in self.frames:
            container = frame["container"]
            if container is not None:
                container["parts"].append("\n\n")


def _decode_html(body: bytes) -> str:
    head = body[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", head, re.I)
    charset = (match.group(1).lower() if match else "utf-8").replace("gb2312", "gb18030")
    if charset not in {"utf-8", "gb18030", "gbk", "big5"}:
        charset = "utf-8"
    return body.decode(charset, errors="replace")


def extract_article_body(body: bytes, *, title="") -> str:
    """Extract a complete sanitized article body without metadata fallbacks."""
    parser = _ArticleBodyParser()
    try:
        parser.feed(_decode_html(body))
        parser.close()
    except ValueError as exc:
        raise IngestionError(ErrorCode.PARSE_ERROR, "Malformed article HTML") from exc
    candidates = []
    normalized_title = re.sub(r"\s+", " ", title or "").strip().casefold()
    title_prefix = normalized_title.rstrip(". …")
    for container in parser.containers:
        paragraphs = []
        for part in re.split(r"(?:\s*\n\s*){2,}", "".join(container["parts"])):
            value = re.sub(r"\s+", " ", part).strip()
            folded = value.casefold()
            is_title = folded == normalized_title or (
                normalized_title.endswith(("...", "…")) and folded.startswith(title_prefix) and len(value) <= 500
            )
            is_metadata = bool(
                re.fullmatch(r"(?:收藏|相关阅读|\[?\s*打印\s*\]?|字号[:：]?.*)", value)
                or re.fullmatch(r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?(?:\s+来源[:：].*)?", value)
                or re.fullmatch(r"发布时间[:：].*", value)
                or re.fullmatch(r"来源[:：].*", value)
                or re.fullmatch(r"[（(]?责任编辑[:：]?.*[）)]?", value)
            )
            if not value or is_title or is_metadata:
                continue
            paragraphs.append(value)
        text = "\n\n".join(paragraphs)
        if text:
            candidates.append((container["priority"], len(text), text))
    return max(candidates, default=(0, 0, ""))[2]

def clean_summary(value: str, limit: int = SUMMARY_LIMIT) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


@dataclass(frozen=True)
class FeedEntry:
    external_id: str
    url: str
    title: str
    published_at: datetime | None
    summary: str
    content_updated_at: datetime | None = None
    metadata: dict = field(default_factory=dict)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _children(element, name):
    return [child for child in element if _local_name(child.tag) == name]


def _first_text(element, *names):
    for name in names:
        for child in _children(element, name):
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def parse_feed(body: bytes) -> list[FeedEntry]:
    try:
        root = ElementTree.fromstring(body)
    except (DefusedXmlException, ElementTree.ParseError, ValueError) as exc:
        raise IngestionError(ErrorCode.PARSE_ERROR, "Unsafe or malformed feed XML") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        channel = next(iter(_children(root, "channel")), None)
        if channel is None:
            raise IngestionError(ErrorCode.PARSE_ERROR, "RSS feed has no channel")
        return [_parse_rss_item(item) for item in _children(channel, "item")]
    if root_name == "feed":
        return [_parse_atom_entry(item) for item in _children(root, "entry")]
    raise IngestionError(ErrorCode.PARSE_ERROR, "Unsupported XML feed format")


def parse_sitemap(body: bytes) -> list[FeedEntry]:
    """Parse sitemap URL metadata without fetching linked pages or article bodies."""
    try:
        root = ElementTree.fromstring(body)
    except (DefusedXmlException, ElementTree.ParseError, ValueError) as exc:
        raise IngestionError(ErrorCode.PARSE_ERROR, "Unsafe or malformed sitemap XML") from exc

    if _local_name(root.tag) != "urlset":
        raise IngestionError(ErrorCode.PARSE_ERROR, "Unsupported sitemap format")
    entries = []
    for item in _children(root, "url"):
        url = _first_text(item, "loc")
        if not url:
            raise IngestionError(ErrorCode.VALIDATION_ERROR, "Sitemap URL entry requires loc")
        parsed = urlsplit(url)
        segment = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]).replace("-", " ").replace("_", " ").strip()
        title = " ".join(segment.split()) or parsed.hostname or url
        entries.append(
            FeedEntry(
                external_id=url,
                url=url,
                title=title[:500],
                published_at=None,
                content_updated_at=_parse_date(_first_text(item, "lastmod")),
                summary="",
                metadata={"feed_format": "sitemap"},
            )
        )
    return entries


def parse_html_list(body: bytes, base_url: str) -> list[FeedEntry]:
    """Extract same-site list links only; never fetches linked pages or body content."""
    parser = _ListLinkParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
    except ValueError as exc:
        raise IngestionError(ErrorCode.PARSE_ERROR, "Malformed HTML list") from exc
    base = urlsplit(base_url)
    entries = []
    seen = set()
    for href, title in parser.links:
        if not href or not title:
            continue
        url = urljoin(base_url, href)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != base.hostname:
            continue
        if url in seen or parsed.path in {"", "/"} or parsed.fragment:
            continue
        seen.add(url)
        entries.append(
            FeedEntry(
                external_id=url,
                url=url,
                title=title,
                published_at=None,
                summary="",
                metadata={"feed_format": "html-list"},
            )
        )
    return entries


def _parse_rss_item(item) -> FeedEntry:
    url = _first_text(item, "link")
    title = _first_text(item, "title")
    if not url or not title:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, "RSS item requires title and link")
    return FeedEntry(
        external_id=_first_text(item, "guid") or url,
        url=url,
        title=title[:500],
        published_at=_parse_date(_first_text(item, "pubdate", "date")),
        summary=clean_summary(_first_text(item, "description", "summary")),
        metadata={"feed_format": "rss2"},
    )


def _parse_atom_entry(item) -> FeedEntry:
    link = ""
    for candidate in _children(item, "link"):
        if candidate.attrib.get("rel", "alternate") == "alternate" and candidate.attrib.get("href"):
            link = candidate.attrib["href"].strip()
            break
    title = _first_text(item, "title")
    if not link or not title:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, "Atom entry requires title and alternate link")
    return FeedEntry(
        external_id=_first_text(item, "id") or link,
        url=link,
        title=title[:500],
        published_at=_parse_date(_first_text(item, "published")),
        content_updated_at=_parse_date(_first_text(item, "updated")),
        summary=clean_summary(_first_text(item, "summary")),
        metadata={"feed_format": "atom"},
    )
