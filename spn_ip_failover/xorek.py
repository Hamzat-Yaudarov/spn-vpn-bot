"""XorekCloud BILLmanager inventory and removal commands for CommandProvider."""

import ipaddress
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree

from .provider import Allocation, ProviderError, _unique_object
from .state import Journal, JournalError


ENDPOINT = "https://my.xorek.cloud/billmgr"
MAX_XML = 1024 * 1024
MAX_IPV4 = 10


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SessionExpired(ProviderError):
    """BILLmanager rejected the authentication of a session."""


def _login_redirect(error):
    if error.code not in (301, 302, 303, 307, 308) or error.headers is None:
        return False
    location = error.headers.get("Location")
    if not location:
        return False
    try:
        target = urlsplit(urljoin(ENDPOINT, location))
    except ValueError:
        return False
    params = parse_qs(target.query)
    return (target.scheme == "https" and target.netloc == "my.xorek.cloud" and target.path == "/billmgr"
            and any(params.get(name) == ["logon"] for name in ("func", "startform")))


def _session_error(doc):
    if doc.get("func") == "logon":
        return True
    error = doc.find("error")
    if error is None:
        return False
    kind = error.get("type", "").lower()
    subject = error.get("object", "").lower()
    session_identifiers = {"session_expired", "expired_session", "session_invalid", "invalid_session", "badsession"}
    return (kind in session_identifiers or (kind == "auth" and subject in session_identifiers | {"session"})
            or (kind in ("expired", "invalid", "notfound") and subject == "session"))


def numeric_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ProviderError("XorekCloud needs a numeric BILLmanager ID")
    return value


def field_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,95}", value):
        raise ProviderError("Invalid XorekCloud order field")
    return value


def rubles(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:[.,][0-9]{1,2})?", value):
        raise ProviderError("XorekCloud order price must be a decimal number")
    return Decimal(value.replace(",", "."))


@dataclass(frozen=True)
class OrderSettings:
    function: str
    button_name: str
    server_parameter: str
    quantity_parameter: str | None
    fixed_fields: dict[str, str]
    quote_server_field: str
    quote_quantity_field: str | None
    quote_price_field: str
    quote_currency_field: str
    maximum_rub: Decimal
    journal_path: Path

    @classmethod
    def from_config(cls, data):
        required = {"function", "button_name", "server_parameter", "fixed_fields", "quote_server_field",
                    "quote_price_field", "quote_currency_field", "maximum_rub", "journal_path"}
        optional = {"quantity_parameter", "quote_quantity_field", "single_address_only"}
        if not isinstance(data, dict) or not required <= data.keys() <= required | optional:
            raise ProviderError("Incomplete XorekCloud order configuration")
        function = field_name(data["function"])
        button_name = field_name(data["button_name"])
        if (not function.startswith("service.ip.")
                or re.search(r"(?:delete|remove|move|transfer|check|history|refund|cart|basket|pay)", function, re.I)
                or not re.search(r"(?:order|add|new|buy)", button_name, re.I)):
            raise ProviderError("Expected the verified XorekCloud additional IPv4 order form")
        server_parameter = data["server_parameter"]
        if server_parameter not in ("elid", "plid"):
            raise ProviderError("XorekCloud order must identify the existing VPS")
        count = data.get("quantity_parameter")
        quote_count = data.get("quote_quantity_field")
        if count is None:
            if data.get("single_address_only") is not True:
                raise ProviderError("Verify that this XorekCloud order form adds only one IPv4")
        else:
            count = field_name(count)
            quote_count = field_name(quote_count)
        if quote_count is not None:
            quote_count = field_name(quote_count)
        reserved = {"func", "out", "sok", "auth", "authinfo", "username", "password", "redirect", "elid",
                    "plid", "su", "postpaid", "noexpense", "manual", "reboot", "payment", "pay", "basket",
                    "cost", "price", "amount", "currency", "request_id"}
        if count is not None and count.lower() in reserved:
            raise ProviderError("Invalid XorekCloud order quantity parameter")
        fields = data["fixed_fields"]
        if not isinstance(fields, dict) or len(fields) > 12:
            raise ProviderError("Invalid fixed fields for XorekCloud order form")
        for key, value in fields.items():
            if (field_name(key).lower() in reserved | ({count.lower()} if count else set())
                    or not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:+-]{1,96}", value)):
                raise ProviderError("Invalid fixed field for XorekCloud order form")
        quote_server = field_name(data["quote_server_field"])
        quote_price = field_name(data["quote_price_field"])
        quote_currency = field_name(data["quote_currency_field"])
        quote_fields = {quote_server, quote_price, quote_currency}
        if len(quote_fields) != 3 or quote_count in quote_fields:
            raise ProviderError("XorekCloud quote fields must be distinct")
        directory = data["journal_path"]
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            raise ProviderError("XorekCloud order journal needs an absolute private path")
        return cls(function, button_name, server_parameter, count, dict(fields), quote_server, quote_count,
                   quote_price, quote_currency, rubles(data["maximum_rub"]), Path(directory))

    def request_fields(self, server_id):
        fields = {**self.fixed_fields, self.server_parameter: numeric_id(server_id)}
        if self.quantity_parameter is not None:
            fields[self.quantity_parameter] = "1"
        return fields


class XorekBilling:
    def __init__(self, session="", username="", password="", order=None, *, auth_mode="session"):
        if auth_mode not in ("session", "authinfo"):
            raise ProviderError("XorekCloud authentication mode must be session or authinfo")
        if auth_mode == "authinfo" and (not username or not password):
            raise ProviderError("Configure XorekCloud BILLmanager credentials for authinfo")
        self.opener = build_opener(NoRedirect(), ProxyHandler({}))
        self.session = session
        self.username = username
        self.password = password
        self.order = order
        self.auth_mode = auth_mode

    def _request(self, function, **params):
        request = Request(ENDPOINT, data=urlencode({"func": function, "out": "xml", **params}).encode(),
                          headers={"Accept": "application/xml", "User-Agent": "SPN-IP-Failover/1"})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(MAX_XML + 1)
        except HTTPError as exc:
            if params.get("auth") and (exc.code == 401 or _login_redirect(exc)):
                raise SessionExpired(f"XorekCloud rejected the session for {function}") from None
            raise ProviderError(f"XorekCloud did not confirm {function}") from None
        except OSError:
            raise ProviderError(f"XorekCloud did not confirm {function}") from None
        if len(raw) > MAX_XML or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", raw, flags=re.IGNORECASE):
            raise ProviderError(f"XorekCloud returned invalid XML for {function}")
        try:
            doc = ElementTree.fromstring(raw.decode("utf-8"))
        except (ElementTree.ParseError, UnicodeDecodeError):
            raise ProviderError(f"XorekCloud returned invalid XML for {function}") from None
        if doc.tag == "doc" and params.get("auth") and _session_error(doc):
            raise SessionExpired(f"XorekCloud rejected the session for {function}")
        if doc.tag != "doc" or doc.find("error") is not None:
            raise ProviderError(f"XorekCloud rejected {function}")
        return doc

    def _login(self):
        if not self.username or not self.password:
            raise ProviderError("Configure XorekCloud BILLmanager session or login credentials")
        auth = self._request("auth", username=self.username, password=self.password).find("auth")
        session = auth.get("id", "") if auth is not None else ""
        if not session:
            raise ProviderError("XorekCloud did not return a BILLmanager session")
        self.session = session

    def _call(self, function, **params):
        if self.auth_mode == "authinfo":
            return self._request(function, authinfo=f"{self.username}:{self.password}", **params)
        if not self.session:
            self._login()
        return self._request(function, auth=self.session, **params)

    def _read(self, function, **params):
        if self.auth_mode == "authinfo":
            return self._call(function, **params)
        try:
            return self._call(function, **params)
        except SessionExpired:
            if not self.username or not self.password:
                raise
        self._login()
        return self._call(function, **params)

    def _inventory(self, server_id):
        doc = self._read("service.ip", elid=numeric_id(server_id))
        if doc.get("func") not in (None, "service.ip") or doc.find("ok") is not None:
            raise ProviderError("XorekCloud returned an unexpected IP inventory")
        allocations = []
        try:
            for row in doc.findall("elem"):
                if len(row.findall("id")) != 1 or len(row.findall("name")) != 1:
                    raise ValueError("missing or duplicate allocation fields")
                address = ipaddress.ip_address(row.findtext("name"))
                if address.version != 4 or not address.is_global:
                    continue
                allocations.append(Allocation(numeric_id(row.findtext("id")), str(address)))
        except ValueError:
            raise ProviderError("XorekCloud returned invalid IP inventory") from None
        if len({a.ref for a in allocations}) != len(allocations) or len({a.ip for a in allocations}) != len(allocations):
            raise ProviderError("XorekCloud returned duplicate IP allocations")
        if not allocations:
            raise ProviderError("XorekCloud returned no public IPv4 addresses for this service")
        return doc, tuple(allocations)

    def inventory(self, server_id) -> tuple[Allocation, ...]:
        return self._inventory(server_id)[1]

    def discover(self, server_id):
        doc = self._read("service.ip", elid=numeric_id(server_id), out="devel")
        metadata = doc.find("metadata")
        if metadata is None or metadata.get("name") != "service.ip":
            raise ProviderError("XorekCloud did not provide service.ip toolbar metadata")
        buttons = []
        for item in metadata.findall("./toolbar//toolbtn"):
            fields = {key: value for key in ("name", "type", "func", "form")
                      if (value := item.get(key)) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,95}", value)}
            if fields.get("func") or fields.get("form"):
                buttons.append(fields)
        return {"buttons": buttons}

    def quote(self, server_id):
        order = self.order
        if order is None:
            raise ProviderError("The XorekCloud paid order form has not been verified")
        buttons = self.discover(server_id)["buttons"]
        if not any(
            button.get("name") == order.button_name and
            (button.get("form") == order.function or
             (button.get("func") == order.function and button.get("type") in ("new", "form")))
            for button in buttons
        ):
            raise ProviderError("XorekCloud no longer exposes the configured IPv4 order form")
        quote = self._read(order.function, **order.request_fields(server_id))
        if (quote.get("func") != order.function or quote.find("ok") is not None or
                any(quote.find(tag) is not None or quote.get(tag) is not None for tag in ("basket", "cart", "redirect"))):
            raise ProviderError("XorekCloud did not return a recognized IPv4 order quote")
        names = [order.quote_server_field, order.quote_price_field, order.quote_currency_field]
        if order.quote_quantity_field is not None:
            names.append(order.quote_quantity_field)
        if any(len(quote.findall(name)) != 1 for name in names):
            raise ProviderError("XorekCloud quote is missing a required field")
        if quote.findtext(order.quote_server_field) != server_id:
            raise ProviderError("XorekCloud quote does not identify the requested VPS")
        if order.quote_quantity_field is not None and quote.findtext(order.quote_quantity_field) != "1":
            raise ProviderError("XorekCloud quote is not for exactly one IPv4")
        if quote.findtext(order.quote_currency_field) != "RUB":
            raise ProviderError("XorekCloud quote is not in RUB")
        price = rubles(quote.findtext(order.quote_price_field))
        if price > order.maximum_rub:
            raise ProviderError("XorekCloud IPv4 quote exceeds the configured RUB ceiling")
        return price

    def allocate(self, server_id, request_id):
        order = self.order
        if order is None:
            raise ProviderError("The XorekCloud paid order form has not been verified")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", request_id):
            raise ProviderError("Invalid XorekCloud order request ID")
        with Journal(order.journal_path) as journal:
            previous = journal.data["servers"].setdefault(numeric_id(server_id), {})
            entry = previous.get(request_id)
            if entry is not None:
                if entry.get("phase") == "confirmed":
                    return Allocation.from_payload(entry["allocation"])
                raise ProviderError("Previous XorekCloud order outcome is unconfirmed; the order was not repeated")
            if any(item.get("phase") not in ("confirmed", "cancelled") for item in previous.values()):
                raise ProviderError("A previous XorekCloud order for this VPS still needs reconciliation")
            price = self.quote(server_id)
            before = set(self.inventory(server_id))
            if len(before) >= MAX_IPV4:
                raise ProviderError("XorekCloud VPS already has the maximum of ten IPv4 addresses")
            previous[request_id] = {"phase": "submitted", "price_rub": str(price),
                                    "before": [a.as_payload() for a in sorted(before, key=lambda a: a.ref)]}
            journal.save()
            reply = self._call(order.function, sok="ok", **order.request_fields(server_id))
            if [child.tag for child in reply] != ["ok"] or any(
                reply.get(tag) is not None for tag in ("basket", "cart", "redirect")
            ):
                raise ProviderError("XorekCloud order confirmation was unexpected; inspect the account")
            allocation = self._verify_order(server_id, before)
            previous[request_id].update(phase="confirmed", allocation=allocation.as_payload())
            journal.save()
            return allocation

    def _verify_order(self, server_id, before):
        last_new = set()
        for attempt in range(8):
            after = set(self.inventory(server_id))
            new = after - before
            if not before <= after or len(after) > MAX_IPV4 or len(new) > 1 or (last_new and new != last_new):
                raise ProviderError("XorekCloud IP inventory changed ambiguously after ordering")
            if new and new == last_new:
                return next(iter(new))
            last_new = new
            if attempt < 7:
                time.sleep(2)
        raise ProviderError("XorekCloud did not confirm a new IPv4; inspect the account")

    def reconcile(self, server_id, request_id, allocation):
        if self.order is None:
            raise ProviderError("Configure the XorekCloud order journal")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", request_id):
            raise ProviderError("Invalid XorekCloud order request ID")
        with Journal(self.order.journal_path) as journal:
            orders = journal.data["servers"].get(numeric_id(server_id), {})
            entry = orders.get(request_id)
            if not entry or entry.get("phase") != "submitted":
                raise ProviderError("XorekCloud has no matching unconfirmed order")
            before = {Allocation.from_payload(row) for row in entry["before"]}
            current = set(self.inventory(server_id))
            if allocation is None and current == before:
                entry["phase"] = "cancelled"
            elif allocation is not None and current == before | {allocation} and allocation not in before:
                entry.update(phase="confirmed", allocation=allocation.as_payload())
            else:
                raise ProviderError("XorekCloud inventory does not match the requested reconciliation")
            journal.save()

    def release(self, server_id, allocation: Allocation):
        row_id = numeric_id(allocation.ref)
        document, before = self._inventory(server_id)
        matches = [a for a in before if a.ref == row_id or a.ip == allocation.ip]
        if not matches:
            return
        if matches != [allocation] or len(before) < 2:
            raise ProviderError("XorekCloud release requires the exact row and another assigned IPv4")
        row = next(item for item in document.findall("elem") if item.findtext("id") == row_id)
        for name in ("is_main", "no_delete"):
            values = row.findall(name)
            value = (values[0].text or "").strip().lower() if len(values) == 1 else ""
            if value in ("on", "true", "yes", "1"):
                raise ProviderError("XorekCloud release refuses a primary or protected IPv4")
            if value not in ("off", "false", "no", "0"):
                raise ProviderError("XorekCloud did not confirm removable non-primary IPv4 flags")
        confirmation = self._call("service.ip.delete", elid=row_id, sok="ok")
        if confirmation.find("ok") is None:
            raise ProviderError("XorekCloud did not confirm the IP removal")
        after = self.inventory(server_id)
        if any(a.ref == row_id or a.ip == allocation.ip for a in after):
            raise ProviderError("XorekCloud still lists the removed IPv4")


def execute(billing, request):
    if not isinstance(request, dict):
        raise ProviderError("Invalid XorekCloud command")
    operation = request.get("operation")
    server_id = numeric_id(request.get("server_id"))
    base = {"operation", "server_id"}
    if operation == "inventory" and request.keys() == base:
        return {"allocations": [entry.as_payload() for entry in billing.inventory(server_id)]}
    if operation == "discover" and request.keys() == base:
        return billing.discover(server_id)
    if operation == "quote" and request.keys() == base:
        return {"price_rub": str(billing.quote(server_id)), "quantity": 1}
    if operation == "check" and request.keys() == base:
        billing.quote(server_id)
        billing.inventory(server_id)
        return {"ok": True}
    if operation == "release" and request.keys() == base | {"id", "ip"}:
        billing.release(server_id, Allocation(request["id"], request["ip"]))
        return {"ok": True}
    if operation == "allocate" and request.keys() == base | {"request_id"}:
        return billing.allocate(server_id, request["request_id"]).as_payload()
    if operation == "reconcile" and request.keys() == base | {"request_id", "allocation"}:
        allocation = request["allocation"]
        billing.reconcile(server_id, request["request_id"],
                          Allocation.from_payload(allocation) if allocation is not None else None)
        return {"ok": True}
    raise ProviderError("Unsupported XorekCloud command")


def main():
    try:
        data = sys.stdin.buffer.read(16_385)
        if len(data) > 16_384:
            raise ProviderError("XorekCloud command is too large")
        request = json.loads(data, object_pairs_hook=_unique_object)
        config = os.environ.get("XOREK_BILLMANAGER_ORDER_CONFIG")
        order = None
        if config and isinstance(request, dict) and request.get("operation") in ("quote", "allocate", "check", "reconcile"):
            with Path(config).open("rb") as stream:
                raw = stream.read(16_385)
            if len(raw) > 16_384:
                raise ProviderError("XorekCloud order configuration is too large")
            order = OrderSettings.from_config(json.loads(raw, object_pairs_hook=_unique_object))
        billing = XorekBilling(os.environ.get("XOREK_BILLMANAGER_SESSION", ""),
                               os.environ.get("XOREK_BILLMANAGER_USERNAME", ""),
                               os.environ.get("XOREK_BILLMANAGER_PASSWORD", ""), order=order,
                               auth_mode=os.environ.get("XOREK_BILLMANAGER_AUTH_MODE", "session"))
        print(json.dumps(execute(billing, request)))
    except ProviderError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, RecursionError):
        print("Invalid XorekCloud command", file=sys.stderr)
        return 1
    except (OSError, JournalError):
        print("XorekCloud order configuration or journal could not be read or saved", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
