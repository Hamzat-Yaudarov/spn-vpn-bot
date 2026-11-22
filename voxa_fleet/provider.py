"""Small, fail-closed Voxa Customer API client.

Contract: https://my.voxa.host/api/v1/guide.md and /openapi.json (2026-11-17).
Only the success envelope is removed; order detail retains ``data.order``.
No request, including an idempotent create, is automatically retried. After an
unknown mutation outcome, reconcile orders/VPS before the caller decides to act.
"""

from __future__ import annotations

import ipaddress
import http.client
import json
import math
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


API_BASE = "https://my.voxa.host/api/v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 128 * 1024
MAX_PAGES = 500
_CODES = frozenset({
    "BAD_REQUEST", "UNAUTHORIZED", "FORBIDDEN", "SCOPE_REQUIRED", "PERMISSION_DENIED",
    "ACCOUNT_SUSPENDED", "INVALID_JSON", "ACTION_NOT_ALLOWED", "ACTION_UNKNOWN",
    "FEATURE_DISABLED", "NOT_FOUND", "CONFLICT", "RENEWAL_PENDING", "RATE_LIMITED",
    "ACTION_RATE_LIMITED", "PROVIDER_ERROR", "PROVIDER_UNAVAILABLE", "UPSTREAM_ERROR",
    "UNAVAILABLE", "INTERNAL_ERROR", "ERROR", "LOCATION_SOLD_OUT", "NO_CAPACITY",
    "LOCATION_UNAVAILABLE", "PRODUCT_UNAVAILABLE", "SOLD_OUT", "UNKNOWN_PROJECT",
    "PROJECT_SCOPE_MISMATCH", "WITHDRAWAL_WAIVER_REQUIRED", "TERMS_NOT_ACCEPTED",
})


class ProviderError(RuntimeError):
    """Safe diagnostic, never the provider body or a transport exception string.

    ``outcome_unknown`` on a mutation means it may already have been applied.
    HTTP 4xx (including 429) is a definitive rejection, not a lost write.
    ``retry_after`` is seconds, advisory only; this client never retries.
    """

    def __init__(self, message, *, code="PROVIDER_ERROR", status=None,
                 retry_after=None, outcome_unknown=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.outcome_unknown = bool(outcome_unknown)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not send Authorization or a mutation body even to a same-host redirect.
        return None


def _uuid(value, field="id"):
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ValueError(f"{field} must be a UUID")
    parsed = uuid.UUID(value)
    if parsed.int == 0:
        raise ValueError(f"{field} must be a nonzero UUID")
    return str(parsed)


def _project(value):
    if not isinstance(value, str):
        raise ValueError("project_id must be a UUID or project slug")
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        return _uuid(value, "project_id")
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) and len(value) <= 40:
        return value
    raise ValueError("project_id must be a UUID or project slug")


def _header_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or not all(
            33 <= ord(character) <= 126 for character in value):
        raise ValueError("idempotency_key must contain 1-255 header-safe characters")
    return value


def _hostname(value):
    if not isinstance(value, str) or len(value) > 253 or "." not in value:
        raise ValueError("hostname must be a unique FQDN")
    if any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
           for label in value.split(".")):
        raise ValueError("hostname must be a unique FQDN")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("hostname must be a unique FQDN, not an IP address")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError("non-finite JSON value")


def _retry_after(headers, payload):
    value = headers.get("Retry-After") if headers is not None else None
    if value is not None:
        try:
            if str(value).isascii() and str(value).isdigit():
                return int(value)
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    value = payload.get("retry_after_seconds") if isinstance(payload, dict) else None
    return value if type(value) is int and value >= 0 else None


def _shape_error(*, mutation=False, status=None, retry_after=None):
    return ProviderError("Voxa returned an invalid or incomplete response",
                         code="INVALID_RESPONSE", status=status,
                         outcome_unknown=mutation, retry_after=retry_after)


class VoxaClient:
    """Fixed-origin HTTPS client; injectable ``opener.open(req, timeout=...)`` for tests.

    A supplied project UUID/slug is sent on EVERY request as X-Project-Id.
    Token and TLS state never enter command arguments or diagnostic messages.
    Callers must additionally verify resource project ownership before cancellation.
    """

    def __init__(self, token, project_id=None, timeout=20, opener=None):
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or not all(
                33 <= ord(character) <= 126 for character in token):
            raise ValueError("token must be a nonempty header-safe secret")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self._token = token
        self.project_id = _project(project_id) if project_id is not None else None
        self.timeout = timeout
        if opener is None:
            try:
                import certifi
            except ImportError:
                context = ssl.create_default_context()
            else:
                context = ssl.create_default_context(cafile=certifi.where())
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=context), _NoRedirect())
        if not callable(getattr(opener, "open", None)):
            raise ValueError("opener must provide open(request, timeout=...)")
        self._opener = opener

    def _request(self, method, path, *, query=None, body=None, headers=None):
        mutation = method != "GET"
        url = API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request_headers = {"Authorization": "Bearer " + self._token,
                           "Accept": "application/json", "Accept-Encoding": "identity"}
        if self.project_id is not None:
            request_headers["X-Project-Id"] = self.project_id
        if headers:
            request_headers.update(headers)
        data = None
        if body is not None:
            try:
                data = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise ValueError("order body must be finite JSON data") from None
            if len(data) > MAX_REQUEST_BYTES:
                raise ValueError("request body exceeds the allowed size")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        response = None
        status = None
        response_headers = None
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            status = response.getcode()
            response_headers = response.headers
            if type(status) is not int or not 100 <= status <= 599:
                raise _shape_error(mutation=mutation)
            if 300 <= status < 400:
                raise ProviderError("Voxa redirect refused; credentials were not forwarded",
                                    code="REDIRECT_BLOCKED", status=status,
                                    outcome_unknown=mutation)
            length = response_headers.get("Content-Length")
            if length is not None:
                try:
                    too_large = int(length) > MAX_RESPONSE_BYTES
                except (ValueError, TypeError):
                    too_large = False
                if too_large:
                    raise _shape_error(mutation=mutation and not 400 <= status < 500,
                                       status=status, retry_after=_retry_after(response_headers, None))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise _shape_error(mutation=mutation and not 400 <= status < 500, status=status,
                                   retry_after=_retry_after(response_headers, None))
            try:
                payload = json.loads(raw, object_pairs_hook=_unique_object,
                                     parse_constant=_bad_constant)
            except (ValueError, TypeError, RecursionError, UnicodeError):
                payload = None
            if not 200 <= status < 300:
                code = payload.get("code") if isinstance(payload, dict) else None
                code = code if isinstance(code, str) and code in _CODES else "PROVIDER_ERROR"
                message = f"Voxa request rejected ({code}; HTTP {status})"
                if code == "WITHDRAWAL_WAIVER_REQUIRED":
                    message = "Voxa requires explicit owner consent to the withdrawal waiver"
                raise ProviderError(message, code=code, status=status,
                                    retry_after=_retry_after(response_headers, payload),
                                    outcome_unknown=mutation and not 400 <= status < 500)
            if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(
                    payload.get("data"), (dict, list)):
                raise _shape_error(mutation=mutation, status=status)
            return payload
        except ProviderError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException, ValueError, TypeError,
                OverflowError):
            raise ProviderError("Voxa transport failed; request outcome is unconfirmed",
                                code="TRANSPORT_ERROR", status=status,
                                retry_after=_retry_after(response_headers, None),
                                outcome_unknown=mutation and not (
                                    status is not None and 400 <= status < 500)) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass

    def _get(self, path, query=None):
        return self._request("GET", path, query=query)["data"]

    def identity(self):
        return self._get("/")

    def balance(self):
        return self._get("/account/balance")

    def _collection(self, path, collection, filters=None, *, pagination_required=False):
        result = []
        seen_ids = set()
        expected_pagination = None
        for page in range(1, MAX_PAGES + 1):
            envelope = self._request("GET", path, query={**(filters or {}), "page": page, "limit": 200})
            data = envelope["data"]
            if not isinstance(data, dict) or not isinstance(data.get(collection), list):
                raise _shape_error()
            rows = data[collection]
            for row in rows:
                if not isinstance(row, dict):
                    raise _shape_error()
                # An ignored page filter must never look like a complete inventory.
                identifier = row.get("id")
                if identifier is not None:
                    if not isinstance(identifier, str) or identifier in seen_ids:
                        raise _shape_error()
                    seen_ids.add(identifier)
            result.extend(rows)
            pagination = data.get("pagination", envelope.get("pagination"))
            if pagination is None:
                if pagination_required:
                    raise _shape_error()
                return result  # /products is currently an unpaginated catalogue.
            if not isinstance(pagination, dict):
                raise _shape_error()
            current, limit, total = (pagination.get(key) for key in ("page", "limit", "total"))
            if any(type(value) is not int for value in (current, limit, total)) or (
                    current != page or not 1 <= limit <= 200 or total < 0 or len(rows) > limit):
                raise _shape_error()
            pages = max(1, (total + limit - 1) // limit)
            if expected_pagination is not None and expected_pagination != (limit, total):
                raise _shape_error()
            expected_pagination = (limit, total)
            if "pages" in pagination:
                advertised = pagination["pages"]
                if type(advertised) is not int or advertised not in ({0, 1} if total == 0 else {pages}):
                    raise _shape_error()
            if len(rows) != min(limit, max(0, total - (page - 1) * limit)):
                raise _shape_error()
            if page >= pages:
                if len(result) != total:
                    raise _shape_error()
                return result
        raise ProviderError("Voxa pagination exceeds the safety limit", code="PAGINATION_LIMIT")

    def products(self):
        """Return data.products, collecting every advertised page if pagination appears."""
        return self._collection("/products", "products")

    def product(self, product_id):
        return self._get("/products/" + _uuid(product_id, "product_id"))

    def availability(self, product_id, location=None):
        if location is not None and (not isinstance(location, str) or not location or
                                     len(location) > 255 or not location.isprintable()):
            raise ValueError("location must be a catalogue location value")
        return self._get("/products/" + _uuid(product_id, "product_id") + "/availability",
                         {"location": location} if location is not None else None)

    def orders(self, **filters):
        """Return all data.orders; official orders pagination omits ``pages``."""
        if filters.keys() - {"status", "hostname", "idempotencyKey"}:
            raise ValueError("unsupported orders filter")
        for value in filters.values():
            if not isinstance(value, str) or not 1 <= len(value) <= 255 or not value.isprintable():
                raise ValueError("orders filters must be nonempty printable strings")
        return self._collection("/orders", "orders", filters, pagination_required=True)

    def order(self, order_id):
        return self._get("/orders/" + _uuid(order_id, "order_id"))

    def vps(self, service_id):
        return self._get("/vps/" + _uuid(service_id, "service_id"))

    def create_order(self, body, idempotency_key):
        """Submit one hourly service with caller-provided consent and a stable key.

        Hostname uniqueness belongs to the durable operation journal, not a racy
        preflight lookup. Legal flags are never inserted or changed by this client.
        """
        key = _header_key(idempotency_key)
        if not isinstance(body, Mapping):
            raise ValueError("order body must be an object")
        body = dict(body)
        _uuid(body.get("productId"), "productId")
        if body.get("billingCycle") != "hourly" or "items" in body:
            raise ValueError("only single-product hourly orders are permitted")
        _hostname(body.get("hostname"))
        if body.get("acceptTerms") is not True:
            raise ValueError("explicit owner acceptance of Voxa terms is required")
        if "withdrawalWaiverAccepted" in body and type(body["withdrawalWaiverAccepted"]) is not bool:
            raise ValueError("withdrawalWaiverAccepted must be an explicit boolean")
        if "idempotencyKey" in body and body["idempotencyKey"] != key:
            raise ValueError("body idempotency key conflicts with the saved request key")
        if "projectId" in body:
            project = _project(body["projectId"])
            if self.project_id is not None and project != self.project_id:
                raise ValueError("order projectId conflicts with the scoped project")
        return self._request("POST", "/orders", body=body,
                             headers={"Idempotency-Key": key})["data"]

    def cancel(self, service_id, reason, immediate=True):
        service_id = _uuid(service_id, "service_id")
        if not isinstance(reason, str) or not reason.isprintable() or len(reason) > 2000 or len(reason.split()) < 2:
            raise ValueError("cancellation reason must contain at least two words")
        if type(immediate) is not bool:
            raise ValueError("immediate must be an explicit boolean")
        return self._request("POST", "/services/" + service_id + "/cancel",
                             body={"reason": reason, "immediate": immediate})["data"]


def parse_order_service(data):
    """Return the one service UUID from create.services or detail.order.services.

    Do not confuse an order UUID, an item UUID or an external instance ID with the
    service ID accepted by /vps/{id}. Ambiguity pauses reconciliation.
    """
    try:
        if not isinstance(data, dict) or not isinstance(data.get("order"), dict):
            raise ValueError()
        _uuid(data["order"].get("id"), "order_id")
        sources = []
        if "services" in data:
            sources.append(data["services"])
        if "services" in data["order"]:
            sources.append(data["order"]["services"])
        if not sources:
            raise ValueError()
        ids = []
        for source in sources:
            if not isinstance(source, list) or len(source) != 1 or not isinstance(source[0], dict):
                raise ValueError()
            ids.append(_uuid(source[0].get("id"), "service_id"))
        if len(set(ids)) != 1:
            raise ValueError()
        return ids[0]
    except (ValueError, TypeError, KeyError):
        raise ProviderError("Voxa order has no unambiguous single service UUID",
                            code="AMBIGUOUS_SERVICE") from None


def parse_vps(data):
    """Normalize only official VPS fields; billing ``active`` alone is not running.

    A missing address while provisioning is ``ip=None``, not an invented address.
    primary_ip is authoritative; fallback uses a unique IPv4 public_ip entry.
    """
    try:
        if not isinstance(data, dict):
            raise ValueError()
        service_id = _uuid(data.get("id"), "service_id")
        primary = data.get("primary_ip")
        ip = None
        if primary is not None:
            address = ipaddress.ip_address(primary)
            if address.version == 4:
                ip = str(address)
        if ip is None:
            entries = data.get("ip_addresses", [])
            if not isinstance(entries, list):
                raise ValueError()
            addresses = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError()
                if entry.get("version") == 4 and entry.get("purpose") == "public_ip":
                    addresses.add(str(ipaddress.IPv4Address(entry.get("address"))))
            if len(addresses) > 1:
                raise ValueError()
            ip = next(iter(addresses), None)
        instance = data.get("instance")
        if instance is not None and not isinstance(instance, dict):
            raise ValueError()
        state = instance.get("state") if instance is not None else None
        status = data.get("status")
        project = data.get("project")
        if project is not None and not isinstance(project, dict):
            raise ValueError()
        project_id = _uuid(project["id"], "project_id") if project is not None else None
        return {"service_id": service_id, "ip": ip, "running": status == "active" and state == "running",
                "status": status, "state": state, "project_id": project_id,
                "project_slug": project.get("slug") if project is not None else None}
    except (ValueError, TypeError, KeyError):
        raise ProviderError("Voxa VPS fields are invalid or ambiguous", code="INVALID_VPS") from None
