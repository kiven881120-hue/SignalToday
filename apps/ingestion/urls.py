from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import ErrorCode, IngestionError


TRACKING_PARAMETERS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
}


def normalize_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, "Invalid entry URL") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, "Entry URL must use HTTP(S)")
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise IngestionError(ErrorCode.VALIDATION_ERROR, "Invalid international hostname") from exc
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    host = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    netloc = host if port is None or default_port else f"{host}:{port}"
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in TRACKING_PARAMETERS],
        doseq=True,
    )
    return urlunsplit((scheme, netloc, parsed.path or "/", query, ""))
