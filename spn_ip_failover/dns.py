"""Update a single Cloudflare A record and verify the result through the API."""

from dataclasses import dataclass
from ipaddress import IPv4Address
import json
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DNSUpdateError(RuntimeError):
    """The DNS read or update could not be verified."""


class DNSConflict(DNSUpdateError):
    """The DNS record differs from its configured identity or expected state."""


@dataclass(frozen=True)
class DNSRecord:
    zone_id: str
    record_id: str
    name: str
    allow_proxied: bool = False

    def __post_init__(self):
        for identifier in (self.zone_id, self.record_id):
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", identifier):
                raise ValueError("Invalid Cloudflare zone or record ID")
        if not isinstance(self.name, str) or not self.name.rstrip("."):
            raise ValueError("A Cloudflare record needs a DNS name")
        if type(self.allow_proxied) is not bool:
            raise ValueError("allow_proxied must be true or false")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _dns_name(name: str) -> str:
    return name.rstrip(".").encode("idna").decode("ascii").lower()


class CloudflareDNS:
    def __init__(self, token: str, *, timeout: float = 10, opener=None):
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise ValueError("A Cloudflare API token is required")
        self._token = token
        self._timeout = timeout
        self._open = opener if opener is not None else build_opener(_RejectRedirects()).open

    def _request(self, record: DNSRecord, method: str, body: dict | None = None) -> dict:
        url = f"https://api.cloudflare.com/client/v4/zones/{record.zone_id}/dns_records/{record.record_id}"
        request = Request(
            url,
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(request, timeout=self._timeout) as response:
                payload = json.load(response)
        except HTTPError as exc:
            exc.close()
            raise DNSUpdateError(f"Cloudflare DNS {method} failed (HTTP {exc.code})") from None
        except (OSError, ValueError):
            raise DNSUpdateError(f"Cloudflare DNS {method} failed or returned an invalid response") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise DNSUpdateError(f"Cloudflare rejected the DNS {method} request")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise DNSUpdateError(f"Cloudflare DNS {method} returned no record")
        return result

    def read_ipv4(self, record: DNSRecord) -> str:
        result = self._request(record, "GET")
        name = result.get("name")
        if (
            result.get("id") != record.record_id
            or result.get("type") != "A"
            or not isinstance(name, str)
            or _dns_name(name) != _dns_name(record.name)
        ):
            raise DNSConflict(f"Cloudflare record does not match the configured A record for {record.name}")
        proxied = result.get("proxied")
        if type(proxied) is not bool:
            raise DNSUpdateError(f"Cloudflare did not report proxy status for {record.name}")
        if proxied and not record.allow_proxied:
            raise DNSConflict(f"Cloudflare proxy is enabled for {record.name}")
        try:
            content = result.get("content")
            if not isinstance(content, str):
                raise ValueError("A record content must be an IPv4 string")
            return str(IPv4Address(content))
        except ValueError:
            raise DNSUpdateError(f"Cloudflare did not return valid IPv4 content for {record.name}") from None

    def replace_ipv4(self, record: DNSRecord, expected: str, replacement: str) -> bool:
        """Compare, change, and verify. Cloudflare's PATCH has no atomic precondition."""
        expected = str(IPv4Address(expected))
        replacement = str(IPv4Address(replacement))
        current = self.read_ipv4(record)
        if current == replacement:
            return False
        if current != expected:
            raise DNSConflict(f"A record {record.name} has an unexpected IPv4 address")
        self._request(record, "PATCH", {"content": replacement})
        if self.read_ipv4(record) != replacement:
            raise DNSConflict(f"A record {record.name} does not match the requested IPv4 after the update")
        return True
