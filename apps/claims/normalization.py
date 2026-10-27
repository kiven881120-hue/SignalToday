import html
import re
import unicodedata


def normalize_claim_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", html.unescape(str(value)))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def normalize_evidence_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", html.unescape(str(value)))
    return re.sub(r"\s+", " ", normalized).strip()
