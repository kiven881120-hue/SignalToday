from django.test import SimpleTestCase

from apps.ingestion.errors import ErrorCode, IngestionError
from apps.ingestion.security import validate_remote_url
from apps.ingestion.urls import normalize_url


PUBLIC_IP = "93.184.216.34"


def resolver_for(*addresses):
    return lambda hostname, port: addresses


class UrlNormalizationTests(SimpleTestCase):
    def test_normalizes_scheme_host_default_port_and_empty_path(self):
        self.assertEqual(normalize_url("HTTPS://Example.COM:443#part"), "https://example.com/")

    def test_idn_is_converted_to_punycode(self):
        self.assertEqual(normalize_url("https://bücher.example/x"), "https://xn--bcher-kva.example/x")

    def test_removes_only_known_tracking_parameters_and_fragment(self):
        result = normalize_url("https://EXAMPLE.com/a?id=123&utm_source=x&utm_medium=y#part")
        self.assertEqual(result, "https://example.com/a?id=123")

    def test_preserves_unknown_query_parameters(self):
        self.assertEqual(normalize_url("https://example.com/?ref=abc&id=1"), "https://example.com/?ref=abc&id=1")


class SsrfProtectionTests(SimpleTestCase):
    def assert_rejected(self, url, addresses):
        with self.assertRaises(IngestionError) as caught:
            validate_remote_url(url, resolver=resolver_for(*addresses))
        self.assertEqual(caught.exception.code, ErrorCode.SECURITY_REJECTED)

    def test_accepts_public_http_target(self):
        target = validate_remote_url("https://example.com/feed", resolver=resolver_for(PUBLIC_IP))
        self.assertEqual(target.addresses, (PUBLIC_IP,))

    def test_rejects_localhost_hostname(self):
        self.assert_rejected("http://localhost/feed", ("127.0.0.1",))

    def test_rejects_loopback_ipv4(self):
        self.assert_rejected("http://127.0.0.1/feed", ("127.0.0.1",))

    def test_rejects_rfc1918_ipv4_ranges(self):
        for address in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with self.subTest(address=address):
                self.assert_rejected("https://example.com/feed", (address,))

    def test_rejects_ipv6_loopback_and_private(self):
        for address in ("::1", "fc00::1", "fd12::1"):
            with self.subTest(address=address):
                self.assert_rejected("https://example.com/feed", (address,))

    def test_rejects_link_local_and_metadata(self):
        for address in ("169.254.1.2", "fe80::1", "169.254.169.254", "100.100.100.200"):
            with self.subTest(address=address):
                self.assert_rejected("https://example.com/feed", (address,))

    def test_rejects_if_any_dns_answer_is_private(self):
        self.assert_rejected("https://example.com/feed", (PUBLIC_IP, "10.0.0.2"))

    def test_rejects_metadata_hostname(self):
        self.assert_rejected("http://metadata.google.internal/feed", (PUBLIC_IP,))

    def test_rejects_non_http_schemes(self):
        for scheme in ("file", "ftp", "gopher", "data"):
            with self.subTest(scheme=scheme):
                self.assert_rejected(f"{scheme}://example.com/feed", (PUBLIC_IP,))
