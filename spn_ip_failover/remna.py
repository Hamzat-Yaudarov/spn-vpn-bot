"""Remnawave node and host operations used by IP failover."""

from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


class RemnawaveError(RuntimeError):
    """A panel request failed or returned an unexpected response."""


class RemnawaveDriftError(RemnawaveError):
    """A resource changed since the caller recorded its expected state."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Remnawave:
    """Manage only the Remnawave resources identified by the caller."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 15):
        if not isinstance(base_url, str) or any(c.isspace() or ord(c) < 32 for c in base_url):
            raise ValueError("Remnawave requires an HTTPS origin or an HTTPS /api URL")
        try:
            base = urlsplit(base_url)
            valid_port = base.port is None or 0 < base.port <= 65535
        except ValueError:
            raise ValueError("Invalid Remnawave API URL") from None
        if (
            base.scheme != "https"
            or not base.hostname
            or base.username is not None
            or base.password is not None
            or base.query
            or base.fragment
            or base.path.rstrip("/") not in ("", "/api")
            or not valid_port
        ):
            raise ValueError("Remnawave requires an HTTPS origin or an HTTPS /api URL")
        if not isinstance(token, str) or not token.isascii() or not token.isprintable() or any(c.isspace() for c in token):
            raise ValueError("Remnawave API token is missing or invalid")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Remnawave timeout must be positive")

        self._base = f"https://{base.netloc}/api"
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(self._base + path, data=data, headers=headers, method=method)

        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise RemnawaveError("Remnawave response exceeds the size limit")
            envelope = json.loads(raw)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RemnawaveError(f"Remnawave {method} returned HTTP {exc.code}") from None
        except (OSError, http.client.HTTPException):
            raise RemnawaveError(f"Remnawave {method} connection failed") from None
        except (ValueError, RecursionError):
            raise RemnawaveError("Remnawave returned invalid JSON") from None
        if not isinstance(envelope, dict) or "response" not in envelope:
            raise RemnawaveError("Remnawave response is missing its response field")
        return envelope["response"]

    @staticmethod
    def _uuid(value: str) -> str:
        try:
            return str(UUID(value))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Expected a Remnawave resource UUID") from None

    @staticmethod
    def _validate(resource: Any, kind: str, uuid: str) -> dict[str, Any]:
        if (
            not isinstance(resource, dict)
            or resource.get("uuid") != uuid
            or not isinstance(resource.get("address"), str)
            or not resource["address"]
            or type(resource.get("isDisabled")) is not bool
            or (kind == "nodes" and type(resource.get("isConnected")) is not bool)
        ):
            raise RemnawaveError(f"Unexpected Remnawave resource from /{kind}")
        return resource

    def _read(self, kind: str, uuid: str) -> dict[str, Any]:
        uuid = self._uuid(uuid)
        return self._validate(self._request("GET", f"/{kind}/{uuid}"), kind, uuid)

    def read_node(self, uuid: str) -> dict[str, Any]:
        return self._read("nodes", uuid)

    def read_host(self, uuid: str) -> dict[str, Any]:
        return self._read("hosts", uuid)

    def _inventory(self, kind: str) -> list[dict[str, Any]]:
        rows = self._request("GET", f"/{kind}")
        if not isinstance(rows, list):
            raise RemnawaveError(f"Unexpected Remnawave collection from /{kind}")
        resources = []
        seen = set()
        for row in rows:
            try:
                uuid = self._uuid(row.get("uuid") if isinstance(row, dict) else None)
            except ValueError:
                raise RemnawaveError(f"Invalid UUID in Remnawave collection /{kind}") from None
            if uuid in seen:
                raise RemnawaveError(f"Duplicate UUID in Remnawave collection /{kind}")
            seen.add(uuid)
            resources.append(self._validate(row, kind, uuid))
        return resources

    def list_nodes(self) -> list[dict[str, Any]]:
        return self._inventory("nodes")

    def list_hosts(self) -> list[dict[str, Any]]:
        return self._inventory("hosts")

    def _replace_address(self, kind: str, uuid: str, expected: str, replacement: str) -> bool:
        if not all(isinstance(value, str) and value for value in (expected, replacement)):
            raise ValueError("Expected and replacement addresses must be nonempty strings")
        current = self._read(kind, uuid)
        if current["address"] == replacement:
            return False
        if current["address"] != expected:
            raise RemnawaveDriftError(f"Remnawave /{kind} address changed since it was recorded")

        self._request("PATCH", f"/{kind}", {"uuid": current["uuid"], "address": replacement})
        actual = self._read(kind, uuid)
        if actual["address"] != replacement or actual["isDisabled"] != current["isDisabled"]:
            raise RemnawaveDriftError(f"Remnawave /{kind} verification differs from the requested update")
        return True

    def replace_node_address(self, uuid: str, expected: str, replacement: str) -> bool:
        return self._replace_address("nodes", uuid, expected, replacement)

    def replace_host_address(self, uuid: str, expected: str, replacement: str) -> bool:
        return self._replace_address("hosts", uuid, expected, replacement)

    def set_host_disabled(
        self, uuid: str, expected_bool: bool, replacement_bool: bool, *, expected_address: str | None = None,
        expected_port: int | None = None, expected_inbound: str | None = None,
    ) -> bool:
        if type(expected_bool) is not bool or type(replacement_bool) is not bool:
            raise ValueError("Remnawave host status must be boolean")

        def matches(record):
            return (
                (expected_address is None or record["address"] == expected_address)
                and (expected_port is None or record.get("port") == expected_port)
                and (expected_inbound is None
                     or (record.get("inbound") or {}).get("configProfileInboundUuid") == expected_inbound)
            )

        current = self.read_host(uuid)
        if not matches(current):
            raise RemnawaveDriftError("Remnawave host endpoint changed since it was measured")
        if current["isDisabled"] == replacement_bool:
            return False
        if current["isDisabled"] != expected_bool:
            raise RemnawaveDriftError("Remnawave host status changed since it was recorded")

        self._request("PATCH", "/hosts", {"uuid": current["uuid"], "isDisabled": replacement_bool})
        actual = self.read_host(uuid)
        if actual["isDisabled"] != replacement_bool or actual["address"] != current["address"] or not matches(actual):
            raise RemnawaveDriftError("Remnawave host verification differs from the requested update")
        return True
