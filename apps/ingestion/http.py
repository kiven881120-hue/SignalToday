import random
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import urllib3

from .errors import ErrorCode, IngestionError
from .security import validate_remote_url


ALLOWED_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
    "text/html",
    "application/xhtml+xml",
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes


class SafeHttpClient:
    def __init__(
        self,
        *,
        connect_timeout=5.0,
        read_timeout=15.0,
        max_response_bytes=5 * 1024 * 1024,
        max_redirects=5,
        max_retries=3,
        user_agent="SignalToday/1.0 feed-ingestion",
        resolver=None,
        sleep=time.sleep,
        random_value=random.random,
        pool_factory=None,
    ):
        self.timeout = urllib3.Timeout(connect=connect_timeout, read=read_timeout)
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.resolver = resolver
        self.sleep = sleep
        self.random_value = random_value
        self.pool_factory = pool_factory

    def fetch(self, url: str) -> FetchResult:
        current_url = url
        redirects = 0
        while True:
            target = validate_remote_url(current_url, **({"resolver": self.resolver} if self.resolver else {}))
            response = self._request_with_retry(target)
            if response.status not in REDIRECT_STATUSES:
                return self._consume(response, current_url)
            location = response.headers.get("Location")
            response.release_conn()
            if not location:
                raise IngestionError(ErrorCode.HTTP_ERROR, "Redirect response omitted Location", status_code=response.status)
            redirects += 1
            if redirects > self.max_redirects:
                raise IngestionError(ErrorCode.HTTP_ERROR, "Redirect limit exceeded", status_code=response.status)
            current_url = urljoin(current_url, location)

    def _request_with_retry(self, target):
        for attempt in range(self.max_retries + 1):
            try:
                response = self._request_once(target)
            except (urllib3.exceptions.TimeoutError, TimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise IngestionError(ErrorCode.TIMEOUT, "Feed request timed out") from exc
                self._backoff(attempt)
                continue
            except urllib3.exceptions.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise IngestionError(ErrorCode.NETWORK_ERROR, "Feed network request failed") from exc
                self._backoff(attempt)
                continue
            if response.status in RETRYABLE_STATUSES and attempt < self.max_retries:
                response.release_conn()
                self._backoff(attempt)
                continue
            return response
        raise AssertionError("retry loop exhausted")

    def _request_once(self, target):
        connect_ip = target.addresses[0]
        if self.pool_factory:
            pool = self.pool_factory(target, connect_ip, self.timeout)
        elif target.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(
                connect_ip,
                target.port,
                timeout=self.timeout,
                maxsize=1,
                cert_reqs="CERT_REQUIRED",
                assert_hostname=target.hostname,
                server_hostname=target.hostname,
            )
        else:
            pool = urllib3.HTTPConnectionPool(connect_ip, target.port, timeout=self.timeout, maxsize=1)
        parsed = urlsplit(target.url)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        host_header = f"[{target.hostname}]" if ":" in target.hostname else target.hostname
        default_port = 443 if target.scheme == "https" else 80
        if target.port != default_port:
            host_header = f"{host_header}:{target.port}"
        return pool.request(
            "GET",
            path,
            headers={"Host": host_header, "User-Agent": self.user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, application/xhtml+xml"},
            redirect=False,
            preload_content=False,
            retries=False,
        )

    def _consume(self, response, url):
        try:
            if not 200 <= response.status < 300:
                raise IngestionError(ErrorCode.HTTP_ERROR, "Feed returned a non-success status", status_code=response.status)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ALLOWED_CONTENT_TYPES and not content_type.endswith("+xml"):
                raise IngestionError(ErrorCode.UNSUPPORTED_CONTENT_TYPE, "Feed response is not XML")
            declared_length = response.headers.get("Content-Length")
            if declared_length:
                try:
                    if int(declared_length) > self.max_response_bytes:
                        raise IngestionError(ErrorCode.RESPONSE_TOO_LARGE, "Feed response exceeds size limit")
                except ValueError as exc:
                    raise IngestionError(ErrorCode.NETWORK_ERROR, "Feed returned invalid Content-Length") from exc
            chunks = []
            size = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise IngestionError(ErrorCode.RESPONSE_TOO_LARGE, "Feed response exceeds size limit")
                chunks.append(chunk)
            return FetchResult(url=url, status=response.status, content_type=content_type, body=b"".join(chunks))
        finally:
            response.release_conn()

    def _backoff(self, attempt):
        self.sleep((2**attempt) + self.random_value())
