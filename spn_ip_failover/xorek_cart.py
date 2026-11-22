"""Scoped Xorek Dragon-cart IPv4 orders with durable, non-replayed payments.

The form/cart contract was observed without paying. The payment request follows
ISPsystem Order API and the observed personal-account checkout button; a paid
order has not yet been tested live. Unknown outcomes require manual reconcile.

Primary API references:
https://www.ispsystem.com/docs/b6c/developer-section/working-with-api/order-api
https://www.ispsystem.com/docs/b6c/developer-section/working-with-api/service-management-via-api
"""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import parse_qs, urlsplit
import uuid
from xml.etree import ElementTree

from .provider import Allocation, ProviderError, _unique_object
from .state import Journal, JournalError
from .xorek import ENDPOINT, MAX_IPV4, XorekBilling, numeric_id, rubles


MAX_INPUT = 4096
TERMINAL = {"confirmed", "cancelled"}
PUBLIC_IPV4 = "Public IPv4-address"


class OutcomeUnconfirmed(ProviderError):
    """A nominally unpaid response suggests an order may already exist."""


def require(condition, message):
    if not condition:
        raise ProviderError(message)


def text(parent, path, *, empty=False):
    values = parent.findall(path)
    require(len(values) == 1, "Xorek cart response is missing or duplicates a required field")
    value = (values[0].text or "").strip()
    require(empty or bool(value), "Xorek cart response has an empty required field")
    return value


def one(parent, path):
    values = parent.findall(path)
    require(len(values) == 1, "Xorek cart response has an unexpected element count")
    return values[0]


def request_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value), "Invalid Xorek cart request ID")
    return value


def secure_ancestors(path):
    require(path.is_absolute() and ".." not in path.parts, "Xorek cart paths must be absolute")
    for parent in path.parents:
        info = parent.lstat()
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.geteuid())
                and (not info.st_mode & 0o022 or sticky_root), "Xorek cart path has an unsafe parent")


@dataclass(frozen=True)
class CartSettings:
    journal_path: Path
    max_upfront_rub: Decimal
    max_monthly_rub: Decimal
    servers: dict

    @classmethod
    def from_config(cls, value):
        require(isinstance(value, dict) and set(value) == {"version", "journal_path", "max_upfront_rub", "max_monthly_rub", "servers"}
                and type(value["version"]) is int and value["version"] == 1, "Invalid Xorek cart settings")
        directory = value["journal_path"]
        require(isinstance(directory, str) and Path(directory).is_absolute() and ".." not in Path(directory).parts,
                "Xorek cart journal needs an absolute path")
        servers = value["servers"]
        require(isinstance(servers, dict) and 1 <= len(servers) <= 256, "Pin the Xorek services before ordering")
        pinned = {}
        for server, fields in servers.items():
            server = numeric_id(server)
            require(isinstance(fields, dict) and set(fields) == {"type", "domain"}
                    and fields["type"] == "2955" and fields["domain"] == server + ".xorek.cloud",
                    "Only the observed public IPv4 type and service domain are supported")
            pinned[server] = dict(fields)
        return cls(Path(directory), rubles(value["max_upfront_rub"]), rubles(value["max_monthly_rub"]), pinned)

    def service(self, server):
        server = numeric_id(server)
        require(server in self.servers, "Xorek service is not pinned in cart settings")
        return self.servers[server]


def load_settings(path):
    path = Path(path)
    secure_ancestors(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid in (0, os.geteuid())
                and stat.S_IMODE(info.st_mode) in (0o600, 0o640), "Xorek cart settings must be a protected regular file")
        raw = stream.read(65537)
        require(0 < len(raw) <= 65536, "Xorek cart settings exceed the size limit")
    return CartSettings.from_config(json.loads(raw, object_pairs_hook=_unique_object))


class DragonTransport:
    """Inject the required header before handing a Request to the base transport."""

    def __init__(self, opener):
        self.inner = opener

    def open(self, request, *args, **kwargs):
        require(request.full_url == ENDPOINT and request.get_method() == "POST", "Unexpected Xorek cart destination")
        request.add_header("ISP-Client", "Web-interface")
        return self.inner.open(request, *args, **kwargs)


@dataclass(frozen=True)
class Quote:
    lineitem: str
    server: str
    total_ipv4: int
    upfront: Decimal
    monthly: Decimal

    def record(self):
        return {"lineitem_id": self.lineitem, "server_id": self.server, "total_ipv4": self.total_ipv4,
                "upfront_rub": str(self.upfront), "monthly_rub": str(self.monthly)}


def dragon_doc(document, function, *, metadata=False):
    require(document.tag == "doc" and document.get("func") == function
            and document.get("theme") in ("/manimg/dragon/", "dragon")
            and document.find("error") is None, "Xorek did not return the observed Dragon response")
    if metadata:
        element = one(document, "metadata")
        require(element.get("name") == function and element.get("type") == "form", "Xorek returned an unknown form")


def no_existing_order(document):
    if any(document.find(".//" + name) is not None for name in ("billorder", "payment_id", "redirect")):
        raise OutcomeUnconfirmed("An unpaid Xorek response already refers to an order or payment")


def list_rows(document, name):
    return one(document, "list[@name='" + name + "']").findall("elem")


def price(parent, path):
    element = one(parent, path)
    require(text(element, "currency") == "RUB", "Xorek cart price is not in RUB")
    return rubles(text(element, "cost"))


def totals(document, expected):
    for name in ("prices", "totals"):
        rows = list_rows(document, name)
        require(len(rows) == 1 and price(rows[0], "price/price") == expected,
                "Xorek cart totals disagree or include unrecognized charges")


def cart_items(document):
    dragon_doc(document, "cart", metadata=True)
    no_existing_order(document)
    lists = document.findall("list[@name='lineitems']")
    confirmations = document.findall("ok")
    if confirmations:
        require(len(confirmations) == 1 and confirmations[0].get("type") == "form"
                and (confirmations[0].text or "").strip() == "func=cart.empty" and not lists,
                "Unknown cart redirect or confirmation")
        return {}
    require(len(lists) == 1, "Xorek cart did not provide line items or the known empty-cart response")
    result = {}
    for row in lists[0].findall("elem"):
        lineitem = numeric_id(text(row, "id"))
        require(lineitem not in result, "Xorek cart has duplicate line-item IDs")
        result[lineitem] = row
    require(len(result) <= 128, "Xorek cart has too many line items")
    return result


def configuration(row, total_ipv4):
    values = one(row, "configuration").findall("elem")
    require(len(values) == 1 and text(values[0], "configuration_name") == PUBLIC_IPV4
            and text(values[0], "configuration_value") == f"{total_ipv4} pcs.",
            "Cart item does not add exactly one public IPv4 to this VPS")
    return values[0]


def owned_row(row, lineitem, server, total_ipv4):
    require(text(row, "id") == lineitem and text(row, "elid") == server, "Cart item belongs to another request or VPS")
    configuration(row, total_ipv4)
    button = one(row, "cross/button")
    require(button.get("name") == "delete" and button.get("type") == "ok" and button.get("key") == "id"
            and not any(button.get(key) for key in ("func", "href", "url")), "Unknown scoped cart deletion button")


def cart_quote(document, lineitem, server, before_count):
    items = cart_items(document)
    require(set(items) == {lineitem}, "Payment requires the cart to contain only this request's item")
    row = items[lineitem]
    owned_row(row, lineitem, server, before_count + 1)
    monthly = price(configuration(row, before_count + 1), "configuration_price/price")
    upfront = price(row, "price/price")
    require(text(one(row, "price/price"), "period") == "1", "Xorek IPv4 order is not monthly")
    totals(document, upfront)
    return Quote(lineitem, server, before_count + 1, upfront, monthly)


def checkout_quote(document, quote):
    dragon_doc(document, "cartorder.create.confirm", metadata=True)
    no_existing_order(document)
    require(document.find("ok") is None, "Checkout read unexpectedly returned a confirmation")
    for name, expected in {"elid": quote.lineitem, "paymethod_id": "0", "paymethod_select": "0",
                           "store_paymethod": "off", "button_type": "local", "bootstrap_allowed": "off",
                           "cant_go_to_paymethod": "no", "hide_profile_select": "yes"}.items():
        require(text(document, name) == expected, "Checkout no longer matches the observed personal-account form")
    for name in ("newwindow", "order_inactive", "promocode", "applied_promocodes"):
        require(text(document, name, empty=True) == "", "Checkout introduces an unexpected option")
    require(not document.findall("profile_id") or text(document, "profile_id", empty=True) == "",
            "A separate payer is required by this checkout")
    buttons = document.findall(".//metadata//button[@name='order_local']")
    require(1 <= len(buttons) <= 2 and all(button.get("type") == "ok"
            and not any(button.get(key) for key in ("func", "href", "url", "action")) for button in buttons),
            "Checkout has an unknown personal-account payment button")
    selected = one(document, "slist[@name='paymethod_select']/val[@key='0']")
    require(selected.find("disabled_hint") is None and selected.find("default_action_button") is None,
            "Personal-account payment is not available")
    information = list_rows(document, "general_info")
    require(len(information) == 1 and text(information[0], "position_count") == "1 item", "Checkout contains other positions")
    rows = list_rows(document, "details")
    require(len(rows) == 1 and text(rows[0], "count") == "1pcs", "Checkout contains an unexpected item count")
    item = one(rows[0], "item")
    location = urlsplit(item.get("href", ""))
    require(not location.scheme and not location.netloc and not location.path and not location.fragment
            and parse_qs(location.query, strict_parsing=True) == {"func": ["vds"], "filter": ["on"], "id": [quote.server]},
            "Checkout item points outside the pinned VPS")
    require(price(rows[0], "price/price") == quote.upfront
            and price(configuration(rows[0], quote.total_ipv4), "configuration_price/price") == quote.monthly,
            "Checkout price differs from the staged cart quote")
    totals(document, quote.upfront)


class XorekCart(XorekBilling):
    def __init__(self, settings, session="", username="", password="", *, auth_mode="authinfo"):
        super().__init__(session, username, password, auth_mode=auth_mode)
        self.settings = settings
        self.opener = DragonTransport(self.opener)

    def _request(self, function, **params):
        require("theme" not in params, "Xorek Dragon theme is fixed by the adapter")
        return super()._request(function, theme="dragon", **params)

    def inventory(self, server_id):
        self.settings.service(server_id)
        return super().inventory(server_id)

    def cart(self):
        return self._read("cart", out="devel")

    def validate_form(self, server):
        fields = self.settings.service(server)
        toolbar = self._read("service.ip", elid=server, out="devel")
        require(toolbar.tag == "doc" and toolbar.get("func") == "service.ip"
                and text(toolbar, "plid") == server, "IP toolbar does not identify the pinned VPS")
        metadata = one(toolbar, "metadata")
        require(metadata.get("name") == "service.ip", "Unknown IP toolbar")
        buttons = metadata.findall(".//toolbar//toolbtn[@name='new']")
        require(len(buttons) == 1 and buttons[0].get("func") == "service.ip.edit" and buttons[0].get("type") == "new",
                "The observed additional-IP form is no longer exposed")
        form = self._read("service.ip.edit", plid=server, out="devel")
        dragon_doc(form, "service.ip.edit", metadata=True)
        no_existing_order(form)
        require(form.find("ok") is None, "IP form read unexpectedly confirmed a mutation")
        for name, expected in {"plid": server, "type": fields["type"], "domain": fields["domain"], "count": "1", "reboot": "off"}.items():
            require(text(form, name) == expected, "Additional-IP form differs from its pinned contract")
        require(text(form, "needreboot", empty=True) == "", "Additional-IP form requires a VPS reboot")
        maximum = text(form, "maxcount")
        require(re.fullmatch(r"[1-9][0-9]{0,2}", maximum), "Additional-IP form has no usable quantity limit")
        choice = one(form, "slist[@name='type']/val[@key='" + fields["type"] + "']")
        require((choice.text or "").strip() == PUBLIC_IPV4, "Configured IP type is not public IPv4")
        slider = one(form, ".//metadata//slider[@name='count']")
        require(slider.get("min") == "1" and slider.get("max") == maximum, "Unknown additional-IP quantity control")
        button = one(form, ".//metadata//buttons/button[@name='basket']")
        require(button.get("type") == "ok" and not any(button.get(key) for key in ("func", "href", "url")),
                "Unknown add-to-cart button")
        require(one(form, ".//metadata//input[@name='reboot']").get("type") == "checkbox", "Unknown reboot control")

    def check(self, server):
        self.settings.service(server)
        before = set(self.inventory(server))
        require(len(before) < MAX_IPV4, "Xorek VPS already has ten IPv4 addresses")
        self.validate_form(server)
        require(not cart_items(self.cart()), "Xorek cart must be empty before an automatic IPv4 order")

    def journal(self):
        secure_ancestors(self.settings.journal_path)
        return Journal(self.settings.journal_path)

    @staticmethod
    def pending(data):
        return any(entry.get("phase") not in TERMINAL for orders in data["servers"].values() for entry in orders.values())

    def preflight(self, server):
        require(not cart_items(self.cart()), "Xorek cart contains unrelated or unreconciled items")
        before = set(self.inventory(server))
        require(1 <= len(before) < MAX_IPV4, "Xorek VPS has no room under the ten-address limit")
        self.validate_form(server)
        require(set(self.inventory(server)) == before and not cart_items(self.cart()), "Inventory or cart changed during preflight")
        return before

    def stage(self, journal, entry, server):
        fields = self.settings.service(server)
        reply = self._call("service.ip.edit", plid=server, type=fields["type"], domain=fields["domain"],
                           count="1", reboot="off", clicked_button="basket", sok="ok")
        dragon_doc(reply, "service.ip.edit")
        no_existing_order(reply)
        confirmation = one(reply, "ok")
        require(confirmation.get("type") == "form" and (confirmation.text or "").strip() == "func=cart",
                "Staging did not return the observed unpaid-cart confirmation")
        lineitem = numeric_id(text(reply, "lineitem.id"))
        nested = one(reply, "doc")
        dragon_doc(nested, "service.ip.edit")
        for name, expected in {"plid": server, "type": fields["type"], "domain": fields["domain"], "count": "1", "reboot": "off"}.items():
            require(text(nested, name) == expected, "Staged item does not match its requested VPS and quantity")
        require(text(nested, "needreboot", empty=True) == "", "Staging unexpectedly requires a reboot")
        for name in ("manual", "noexpense", "postpaid"):
            require(text(nested, name) == "off", "Staging changed a forbidden billing option")
        require(text(nested, "manualprocessing", empty=True) == "", "Staging requires manual provisioning")
        entry.update(phase="staged", lineitem_id=lineitem)
        journal.save()
        before = {Allocation.from_payload(row) for row in entry["before"]}
        if set(self.inventory(server)) != before:
            entry["phase"] = "staging_inventory_changed"
            journal.save()
            raise OutcomeUnconfirmed("IP inventory changed during unpaid staging")

    def read_quote(self, server, entry):
        quote = cart_quote(self.cart(), entry["lineitem_id"], server, len(entry["before"]))
        checkout_quote(self._read("cartorder.create.confirm", elid=quote.lineitem, out="devel"), quote)
        return quote

    def within_caps(self, quote):
        return quote.upfront <= self.settings.max_upfront_rub and quote.monthly <= self.settings.max_monthly_rub

    def cleanup(self, journal, entry, server):
        require(entry["phase"] == "staged", "Only a known unpaid draft can be cleaned automatically")
        lineitem = entry["lineitem_id"]
        items = cart_items(self.cart())
        if lineitem not in items:
            entry.update(phase="cancelled", cancellation="draft_already_absent")
            journal.save()
            return
        owned_row(items[lineitem], lineitem, server, len(entry["before"]) + 1)
        foreign = {key: hashlib.sha256(ElementTree.tostring(row)).hexdigest() for key, row in items.items() if key != lineitem}
        entry["phase"] = "cleanup_submitted"
        journal.save()
        # The observed cross-button uses id. 'selected' was tested and did not delete.
        reply = self._call("cart", clicked_button="delete", id=lineitem, sok="ok")
        dragon_doc(reply, "cart")
        no_existing_order(reply)
        confirmation = one(reply, "ok")
        require(confirmation.get("type") == "form"
                and (confirmation.text or "").strip() in ("func=cart.empty", "func=cart"), "Cart cleanup confirmation is unknown")
        after = cart_items(self.cart())
        remaining = {key: hashlib.sha256(ElementTree.tostring(row)).hexdigest() for key, row in after.items()}
        require(remaining == foreign, "Own draft removal or preservation of other cart items was not confirmed")
        entry.update(phase="cancelled", cancellation="unpaid_draft_removed")
        journal.save()

    def payment_reply(self, reply):
        dragon_doc(reply, "cartorder.create.confirm")
        require(reply.find("redirect") is None, "Payment returned an unexpected redirect")
        order = numeric_id(text(reply, "billorder"))
        payments = reply.findall("payment_id")
        require(len(payments) <= 1, "Payment returned duplicate payment IDs")
        if payments:
            numeric_id((payments[0].text or "").strip())
        confirmations = reply.findall("ok")
        require(len(confirmations) <= 1, "Payment returned unknown confirmations")
        if confirmations:
            confirmation = confirmations[0]
            destination = (confirmation.text or "").strip()
            require(confirmation.get("type") == "form"
                    and destination in (f"func=cartorder&elid={order}", f"?func=cartorder&elid={order}"),
                    "Payment confirmation is not the known internal order form")
        return order

    def verify_allocation(self, server, before):
        previous = None
        for attempt in range(10):
            current = set(self.inventory(server))
            new = current - before
            require(before <= current and len(current) <= MAX_IPV4 and len(new) <= 1,
                    "IP inventory changed ambiguously after payment")
            if new:
                require(previous is None or current == previous, "New allocation changed between confirmation reads")
                if current == previous:
                    return next(iter(new))
                previous = current
            elif previous is not None:
                raise ProviderError("New allocation disappeared during confirmation")
            if attempt < 9:
                time.sleep(2)
        raise ProviderError("Payment did not confirm one stable new IPv4; reconcile this request")

    def perform_order(self, server, identifier, *, quote_only=False):
        self.settings.service(server)
        identifier = request_id(identifier)
        with self.journal() as journal:
            orders = journal.data["servers"].setdefault(server, {})
            existing = orders.get(identifier)
            if existing is not None:
                if existing.get("phase") == "confirmed" and not quote_only:
                    allocated = Allocation.from_payload(existing["allocation"])
                    current = self.inventory(server)
                    require([row for row in current if row.ref == allocated.ref or row.ip == allocated.ip] == [allocated],
                            "Previously confirmed IPv4 is no longer assigned exactly")
                    return allocated
                raise ProviderError("Xorek cart request already exists; no staging or payment was repeated")
            require(not self.pending(journal.data), "A previous account-wide Xorek cart request needs reconciliation")
            before = self.preflight(server)
            entry = {"phase": "staging", "quote_only": quote_only,
                     "before": [row.as_payload() for row in sorted(before, key=lambda row: row.ref)]}
            orders[identifier] = entry
            journal.save()
            self.stage(journal, entry, server)  # Never retried, even after a timeout.
            try:
                quote = self.read_quote(server, entry)
                entry["quote"] = quote.record()
                journal.save()
                if quote_only:
                    result = {"quantity": 1, "upfront_rub": str(quote.upfront), "monthly_rub": str(quote.monthly),
                              "within_caps": self.within_caps(quote)}
                    self.cleanup(journal, entry, server)
                    return result
                require(self.within_caps(quote), "Xorek IPv4 quote exceeds configured upfront or monthly RUB limits")
                require(set(self.inventory(server)) == before, "Inventory changed before payment")
                # Re-read the exact cart and checkout immediately before recording payment intent.
                require(self.read_quote(server, entry) == quote, "Xorek quote changed before payment")
                entry["phase"] = "paying"
                journal.save()
                reply = self._call("cartorder.create.confirm", elid=quote.lineitem, paymethod_id="0",
                                   store_paymethod="off", clicked_button="order_local", sok="ok")
                entry["billorder"] = self.payment_reply(reply)
                journal.save()
                allocated = self.verify_allocation(server, before)
                require(not cart_items(self.cart()), "A draft remains after payment; reconcile before another order")
                entry.update(phase="confirmed", allocation=allocated.as_payload())
                journal.save()
                return allocated
            except OutcomeUnconfirmed:
                if entry["phase"] == "staged":
                    entry["phase"] = "outcome_unconfirmed"
                    journal.save()
                raise
            except (ProviderError, OSError, ValueError, JournalError):
                if entry["phase"] == "staged":
                    self.cleanup(journal, entry, server)
                raise

    def allocate(self, server, identifier):
        return self.perform_order(server, identifier)

    def quote(self, server, identifier=None):
        return self.perform_order(server, "quote-" + uuid.uuid4().hex if identifier is None else identifier, quote_only=True)

    def reconcile(self, server, identifier, allocated=None, *, no_purchase=False):
        self.settings.service(server)
        identifier = request_id(identifier)
        require((allocated is None and no_purchase is True) or (isinstance(allocated, Allocation) and no_purchase is False),
                "Reconcile must explicitly choose no_purchase or one allocation")
        with self.journal() as journal:
            entry = journal.data["servers"].get(server, {}).get(identifier)
            require(entry and entry.get("phase") not in TERMINAL, "No matching pending Xorek cart request")
            require(not cart_items(self.cart()), "Remove/review remaining cart drafts before manual reconcile")
            before = {Allocation.from_payload(row) for row in entry["before"]}
            first, second = set(self.inventory(server)), set(self.inventory(server))
            require(first == second and len(first) <= MAX_IPV4, "Reconcile inventory was not stable")
            if no_purchase:
                require(first == before, "Inventory does not confirm the declared no-purchase outcome")
                entry.update(phase="cancelled", cancellation="manual_no_purchase")
            else:
                require(allocated not in before and first == before | {allocated}, "Inventory does not match manual allocation reconciliation")
                entry.update(phase="confirmed", allocation=allocated.as_payload(), reconciliation="manual_allocation")
            journal.save()

    def release(self, server, allocated):
        self.settings.service(server)
        with self.journal() as journal:
            require(not any(entry.get("phase") not in TERMINAL for entry in journal.data["servers"].get(server, {}).values()),
                    "Reconcile the pending IPv4 order before releasing this VPS's addresses")
            super().release(server, allocated)


def execute(billing, request):
    require(isinstance(request, dict), "Invalid Xorek cart command")
    operation = request.get("operation")
    server = numeric_id(request.get("server_id"))
    billing.settings.service(server)
    fields = {"operation", "server_id"}
    if operation == "check" and set(request) == fields:
        billing.check(server)
        return {"ok": True}
    if operation == "inventory" and set(request) == fields:
        return {"allocations": [row.as_payload() for row in billing.inventory(server)]}
    if operation == "quote" and set(request) in (fields, fields | {"request_id"}):
        if "request_id" in request:
            request_id(request["request_id"])
        return billing.quote(server, request.get("request_id"))
    if operation == "allocate" and set(request) == fields | {"request_id"}:
        return billing.allocate(server, request["request_id"]).as_payload()
    if operation == "release" and set(request) == fields | {"id", "ip"}:
        billing.release(server, Allocation(request["id"], request["ip"]))
        return {"ok": True}
    if operation == "reconcile" and set(request) == fields | {"request_id", "no_purchase"}:
        require(request["no_purchase"] is True, "Manual no_purchase must be explicitly true")
        billing.reconcile(server, request["request_id"], no_purchase=True)
        return {"ok": True}
    if operation == "reconcile" and set(request) == fields | {"request_id", "allocation"}:
        require(isinstance(request["allocation"], dict), "Use explicit no_purchase instead of a null allocation")
        billing.reconcile(server, request["request_id"], Allocation.from_payload(request["allocation"]))
        return {"ok": True}
    raise ProviderError("Unsupported Xorek cart command")


def main():
    try:
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        require(0 < len(data) <= MAX_INPUT, "Xorek cart input exceeds its limit")
        request = json.loads(data, object_pairs_hook=_unique_object)
        path = os.environ.get("XOREK_CART_CONFIG")
        require(isinstance(path, str) and bool(path), "Configure the protected Xorek cart settings file")
        settings = load_settings(path)
        billing = XorekCart(settings, os.environ.get("XOREK_BILLMANAGER_SESSION", ""),
                            os.environ.get("XOREK_BILLMANAGER_USERNAME", ""), os.environ.get("XOREK_BILLMANAGER_PASSWORD", ""),
                            auth_mode=os.environ.get("XOREK_BILLMANAGER_AUTH_MODE", "authinfo"))
        print(json.dumps(execute(billing, request), separators=(",", ":")))
        return 0
    except (ProviderError, JournalError, OSError, ValueError, TypeError, KeyError):
        print("Xorek cart action unconfirmed; credentials and raw responses suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
