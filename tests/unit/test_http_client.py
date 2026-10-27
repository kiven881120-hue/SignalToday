import urllib3
from django.test import SimpleTestCase

from apps.ingestion.errors import ErrorCode, IngestionError
from apps.ingestion.http import SafeHttpClient


PUBLIC_IP = "93.184.216.34"


class FakeResponse:
    def __init__(self, status=200, body=b"<rss/>", headers=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/rss+xml"}
        self.body = body
        self.read_count = 0
        self.released = False

    def read(self, size):
        if self.read_count:
            return b""
        self.read_count += 1
        return self.body

    def release_conn(self):
        self.released = True


class FakePool:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(outcomes, **kwargs):
    pool = FakePool(outcomes)
    def resolver(host, port):
        if host == "127.0.0.1":
            return ("127.0.0.1",)
        return (PUBLIC_IP,)

    client = SafeHttpClient(
        resolver=resolver,
        pool_factory=lambda target, ip, timeout: pool,
        sleep=lambda seconds: None,
        random_value=lambda: 0,
        **kwargs,
    )
    return client, pool


class SafeHttpClientTests(SimpleTestCase):
    def test_fetches_supported_content_type(self):
        client, _ = make_client([FakeResponse()])
        self.assertEqual(client.fetch("https://example.com/feed").body, b"<rss/>")

    def test_rejects_unsupported_content_type(self):
        client, _ = make_client([FakeResponse(headers={"Content-Type": "application/json"})])
        with self.assertRaises(IngestionError) as caught:
            client.fetch("https://example.com/feed")
        self.assertEqual(caught.exception.code, ErrorCode.UNSUPPORTED_CONTENT_TYPE)

    def test_rejects_streamed_response_over_limit(self):
        client, _ = make_client([FakeResponse(body=b"123456")], max_response_bytes=5)
        with self.assertRaises(IngestionError) as caught:
            client.fetch("https://example.com/feed")
        self.assertEqual(caught.exception.code, ErrorCode.RESPONSE_TOO_LARGE)

    def test_rejects_declared_response_over_limit(self):
        response = FakeResponse(headers={"Content-Type": "text/xml", "Content-Length": "100"})
        client, _ = make_client([response], max_response_bytes=5)
        with self.assertRaises(IngestionError) as caught:
            client.fetch("https://example.com/feed")
        self.assertEqual(caught.exception.code, ErrorCode.RESPONSE_TOO_LARGE)

    def test_timeout_retries_then_reports_timeout(self):
        timeout = urllib3.exceptions.ReadTimeoutError(None, "/feed", "timed out")
        client, pool = make_client([timeout, timeout, timeout, timeout])
        with self.assertRaises(IngestionError) as caught:
            client.fetch("https://example.com/feed")
        self.assertEqual(caught.exception.code, ErrorCode.TIMEOUT)
        self.assertEqual(pool.calls, 4)

    def test_429_and_5xx_are_retried(self):
        for status in (429, 503):
            with self.subTest(status=status):
                client, pool = make_client([FakeResponse(status=status), FakeResponse()])
                self.assertEqual(client.fetch("https://example.com/feed").status, 200)
                self.assertEqual(pool.calls, 2)

    def test_401_and_403_are_not_retried(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client, pool = make_client([FakeResponse(status=status)])
                with self.assertRaises(IngestionError) as caught:
                    client.fetch("https://example.com/feed")
                self.assertEqual(caught.exception.code, ErrorCode.HTTP_ERROR)
                self.assertEqual(pool.calls, 1)

    def test_redirect_is_revalidated_and_private_target_rejected(self):
        response = FakeResponse(status=302, headers={"Location": "http://127.0.0.1/private"})
        client, pool = make_client([response])
        with self.assertRaises(IngestionError) as caught:
            client.fetch("https://example.com/feed")
        self.assertEqual(caught.exception.code, ErrorCode.SECURITY_REJECTED)
        self.assertEqual(pool.calls, 1)

    def test_redirect_limit(self):
        responses = [FakeResponse(status=302, headers={"Location": "/next"}) for _ in range(2)]
        client, _ = make_client(responses, max_redirects=1)
        with self.assertRaises(IngestionError) as caught:
            client.fetch("https://example.com/feed")
        self.assertEqual(caught.exception.code, ErrorCode.HTTP_ERROR)
