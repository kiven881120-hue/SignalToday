import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import ErrorCode, IngestionError


METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data.ec2.internal",
}
METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}


@dataclass(frozen=True)
class SafeTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and ip not in METADATA_IPS


def resolve_hostname(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise IngestionError(ErrorCode.NETWORK_ERROR, "DNS resolution failed") from exc
    return tuple(dict.fromkeys(record[4][0] for record in records))


def validate_remote_url(url: str, *, resolver=resolve_hostname) -> SafeTarget:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise IngestionError(ErrorCode.SECURITY_REJECTED, "Invalid remote URL") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise IngestionError(ErrorCode.SECURITY_REJECTED, "Only HTTP(S) URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise IngestionError(ErrorCode.SECURITY_REJECTED, "URL must contain a plain hostname")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname in METADATA_HOSTS:
        raise IngestionError(ErrorCode.SECURITY_REJECTED, "Hostname is not an allowed remote target")
    port = port or (443 if scheme == "https" else 80)
    addresses = resolver(hostname, port)
    if not addresses or any(not is_public_address(address) for address in addresses):
        raise IngestionError(ErrorCode.SECURITY_REJECTED, "DNS result contains a non-public address")
    return SafeTarget(url=url, scheme=scheme, hostname=hostname, port=port, addresses=addresses)
