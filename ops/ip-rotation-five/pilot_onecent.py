#!/usr/bin/env python3
"""One explicitly approved bounded IPv4 order; stop for receipt review.

Run on the RF controller as spn-failover with systemd credentials. Default is
a local plan, without reading credentials, opening a browser or making an
order. This program deliberately NEVER claims inventory-delta ownership and
has no assignment, promotion, release or primary-address deletion operation.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import replace
from decimal import Decimal
import hashlib
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import traceback
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from spn_ip_failover import dynamic_relay_worker
from spn_ip_failover.budget import BudgetDenied
from spn_ip_failover.onecent import OneCentBilling, Settings, browser
from spn_ip_failover.onecent_auth import OneCentLogin
from spn_ip_failover.onecent_fields import Quote
from spn_ip_failover.onecent_ui import OneCentPage, ORIGIN
from spn_ip_failover.provider import ProviderError, _unique_object
from spn_ip_failover.provider_gateway import run_json
from spn_ip_failover.remna import Remnawave


REQUEST_KEY = "five-vps-pilot-20261116-001"
UPFRONT_CAP = Decimal("100.00")
AMENDED_UPFRONT_CAP = Decimal("248.39")
AMENDED_SERVICE = "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d"
AMENDED_PRIMARY = "87.251.16.241"
MONTHLY_CAP = Decimal("300.00")
DAILY_CAP = Decimal("3000.00")
CAPTURE_LIMIT = 256 * 1024
RECEIPT_WAIT_MS = 20000
PILOT_DIRECTORY = Path("/var/lib/spn-ip-failover/onecent-pilot-20261116")
ACCOUNT_CONFIG = Path("/etc/spn-ip-failover/onecent.json")
SELECTED_CONFIG = Path("/etc/spn-ip-failover/selected.json")
REFRESH_CONFIG = Path("/var/lib/spn-ip-failover/refresh.json")
AUTH_PATH = "/api/auth/sign-in/email"
FIVE_SERVICE_IDS = frozenset({
    "f5c0287a-636e-4981-95c7-80b24c9559c0",
    "21e508f0-2ef2-47da-aee7-15dc8c66eb15",
    "9b0316d5-fa83-4bf8-b6dc-8202b66622c9",
    "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d",
    "4d6f358a-e75f-4dd8-a635-c6ad320f2732",
})
SERVICES = {
    "21e508f0-2ef2-47da-aee7-15dc8c66eb15": {
        "node_uuid": "6caf57c3-e03d-48ac-80e5-c51ba2fbd62a",
        "host_uuid": "8a26ad46-2a93-49c0-995d-81014fbe8fce",
        "service_name": "vds100269.1cent.network",
    },
    "f5c0287a-636e-4981-95c7-80b24c9559c0": {
        "node_uuid": "5c472650-1f93-470a-abdb-0caadef059b7",
        "host_uuid": "7737ba07-6d54-435c-8c01-aea4fe1369b9",
        "service_name": "srv-mwfa434s",
    },
    "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d": {
        "node_uuid": "9f63b074-8b8a-4655-a2b8-02177a7c3c0f",
        "host_uuid": "f07a16c5-5111-49d0-9801-f728135aa528",
        "service_name": "vds56962.1cent.network",
    },
}


class ReceiptReviewRequired(ProviderError):
    """Dispatch may have happened: the immutable receipt needs manual review."""


def canonical_ip(value):
    if not isinstance(value, str):
        raise ValueError("Expected a public IPv4")
    address = IPv4Address(value)
    if (str(address) != value or not address.is_global or address.is_multicast
            or address.is_reserved):
        raise ValueError("Expected a canonical public IPv4")
    return value


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def approved_cap(service, primary, upfront_cap, *, billing_only=False):
    if not isinstance(upfront_cap, Decimal) or upfront_cap not in (UPFRONT_CAP, AMENDED_UPFRONT_CAP):
        raise ValueError("Only the explicit 100.00 or 248.39 RUB pilot limit is allowed")
    if upfront_cap == AMENDED_UPFRONT_CAP and (service != AMENDED_SERVICE
            or primary != AMENDED_PRIMARY or billing_only is not True):
        raise ValueError("The amended approval applies only to the unchanged FI2 billing-only pilot")
    return upfront_cap


def present(path):
    """A dangling symlink or any other filesystem object is not absence."""
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    return True


def controlled_file(path, maximum, *, own_private=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected a fixed absolute input")
    for parent in reversed(path.parents):
        meta = parent.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in (0, os.geteuid())
                or meta.st_mode & 0o022):
            raise ValueError("Input parents are not controlled")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                or meta.st_uid not in (0, os.geteuid()) or meta.st_mode & 0o027
                or own_private and (meta.st_uid != os.geteuid()
                                    or stat.S_IMODE(meta.st_mode) != 0o600)):
            raise ValueError("Input permissions are not controlled")
        raw = stream.read(maximum + 1)
    if not 0 < len(raw) <= maximum:
        raise ValueError("Input exceeds the bounded size")
    return raw


class PilotStore:
    """Global one-shot fence, separate from the shared financial journals."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def prepare(self):
        if not self.directory.is_absolute() or ".." in self.directory.parts:
            raise ValueError("Pilot directory must be absolute")
        for parent in reversed(self.directory.parents):
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                    or info.st_mode & 0o022):
                raise ValueError("Pilot parents are not controlled")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        meta = self.directory.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid()
                or stat.S_IMODE(meta.st_mode) != 0o700):
            raise ValueError("Pilot directory must be owned/private")

    def create(self, name, value):
        if name not in ("intent.json", "dispatch.json", "response.json", "capture-error.json", "limit-amendment.json"):
            raise ValueError("Unknown pilot artifact")
        self.prepare()
        # Never overwrite even an identical marker: a second run is not a
        # replay permission for the one approved paid operation.
        descriptor = os.open(self.directory / name,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def arm(self, service, primary):
        if service not in SERVICES:
            raise ValueError("Pilot service is not explicitly approved")
        self.create("intent.json", {
            "format": 1, "request_key": REQUEST_KEY, "service_id": service,
            "expected_primary": canonical_ip(primary), "upfront_cap_rub": "100.00",
            "monthly_cap_rub": "300.00", "keep_current_primary": True,
        })

    def read(self, name):
        if name not in ("intent.json", "limit-amendment.json"):
            raise ValueError("Unknown pilot input artifact")
        self.prepare()
        return controlled_file(self.directory / name, 65536, own_private=True)

    def unsubmitted_amendment(self, settings, service, primary, *, upfront_cap, billing_only):
        approved_cap(service, primary, upfront_cap, billing_only=billing_only)
        if upfront_cap != AMENDED_UPFRONT_CAP or settings.budget is None:
            raise ValueError("Resume requires the explicit amended price and the existing shared budget")
        for name in ("dispatch.json", "response.json", "capture-error.json"):
            if present(self.directory / name):
                raise ReceiptReviewRequired("A dispatch/capture marker prevents any pilot resume")
        states = (settings.journal / "state.json", settings.budget.journal / "state.json")
        if any(present(path) for path in states):
            raise ReceiptReviewRequired("Any financial state file prevents the unsubmitted pilot resume")
        raw = self.read("intent.json")
        intent = json.loads(raw, object_pairs_hook=_unique_object)
        expected = {"format": 1, "request_key": REQUEST_KEY, "service_id": service,
                    "expected_primary": primary, "upfront_cap_rub": "100.00",
                    "monthly_cap_rub": "300.00", "keep_current_primary": True}
        if (intent != expected or type(intent.get("format")) is not int
                or intent.get("keep_current_primary") is not True):
            raise ReceiptReviewRequired("The original immutable 100 RUB pilot intent does not match")
        return {"format": 1, "request_key": REQUEST_KEY, "service_id": service,
                "expected_primary": primary, "old_upfront_cap_rub": "100.00",
                "new_upfront_cap_rub": "248.39", "billing_only_without_guest": True,
                "intent_sha256": hashlib.sha256(raw).hexdigest(),
                "orders_state": str(states[0]), "budget_state": str(states[1])}

    def resume_unsubmitted(self, settings, service, primary, *, upfront_cap, billing_only):
        amendment = self.unsubmitted_amendment(settings, service, primary,
                                              upfront_cap=upfront_cap, billing_only=billing_only)
        try:
            self.create("limit-amendment.json", amendment)
        except FileExistsError:
            if self.read("limit-amendment.json") != encoded(amendment):
                raise ReceiptReviewRequired("The immutable pilot limit amendment differs") from None
        # Recheck every absence and the original intent even on an exact reuse.
        if self.unsubmitted_amendment(settings, service, primary, upfront_cap=upfront_cap,
                                      billing_only=billing_only) != amendment:
            raise ReceiptReviewRequired("Pilot pre-dispatch evidence changed")


def limited_settings(settings, service, *, upfront_cap=UPFRONT_CAP, expected_primary=None, billing_only=False):
    approved_cap(service, expected_primary, upfront_cap, billing_only=billing_only)
    if service not in SERVICES or service not in settings.servers:
        raise ValueError("Pilot service is not in the pinned billing scope")
    if settings.servers[service].name != SERVICES[service]["service_name"]:
        raise ValueError("Pilot service name differs")
    if (settings.budget is None or not Decimal("0.00") < settings.budget.maximum_daily <= DAILY_CAP
            or settings.budget.timezone != "Europe/Saratov"):
        raise ValueError("Pilot needs the existing shared bounded daily budget")
    original = settings.servers[service]
    bounded = replace(original,
                      maximum_upfront=min(original.maximum_upfront, upfront_cap),
                      maximum_monthly=min(original.maximum_monthly, MONTHLY_CAP),
                      existing_removable=frozenset())
    return replace(settings, servers={service: bounded})


def validate_selection(value, service):
    if (not isinstance(value, dict) or value.get("format") != 1
            or value.get("auto_purchase_enabled") is not False
            or value.get("bypasses_excluded") is not True
            or not isinstance(value.get("servers"), list)
            or len(value["servers"]) != 5
            or {row.get("service_id") for row in value["servers"] if isinstance(row, dict)} != FIVE_SERVICE_IDS):
        raise ValueError("Closed five-host pilot scope was not confirmed")
    rows = [row for row in value["servers"] if isinstance(row, dict) and row.get("service_id") == service]
    if len(rows) != 1 or any(rows[0].get(key) != SERVICES[service][key]
                             for key in ("node_uuid", "host_uuid", "service_name")):
        raise ValueError("Pilot node/host/service binding differs")


def validate_live_target(panel, service, primary, *, capture_only=False):
    expected = SERVICES[service]
    node = panel.read_node(expected["node_uuid"])
    if (node.get("uuid") != expected["node_uuid"] or node.get("address") != canonical_ip(primary)
            or node.get("isDisabled") is not False or node.get("isConnected") is not True):
        raise ValueError("Pilot node does not use the confirmed current primary")
    host = panel.read_host(expected["host_uuid"])
    host_address = canonical_ip(host.get("address"))
    if ((not capture_only and host_address != primary)
            or host.get("port") != 443 or host.get("isDisabled") is not False
            or host.get("nodes") != [expected["node_uuid"]]):
        raise ValueError("Pilot host/current primary binding differs")
    # A capture-only purchase never updates this host or uses its advertised
    # address. A manually edited host need not block a scoped extra-IP test;
    # the real guest + provider primary are still independently checked.
    return {"host_address": host_address, "host_address_drift": host_address != primary}


def guest_readiness(service, primary, *, run=run_json):
    primary = canonical_ip(primary)
    private = Path("/var/lib/spn-ip-failover/.ssh")
    command = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
               "-o", "GlobalKnownHostsFile=/dev/null",
               "-o", "UserKnownHostsFile=" + str(private / "guest_known_hosts"),
               "-o", "HostKeyAlias=" + service, "-o", "ConnectTimeout=15",
               "-i", str(private / ("guest_" + service + "_ed25519")), "root@" + primary]
    # Keep the public logical peer and immutable host-key alias, but use the
    # already verified private reverse channel when the public IP is RF-blocked.
    port = {"21e508f0-2ef2-47da-aee7-15dc8c66eb15": 23002,
            "f5c0287a-636e-4981-95c7-80b24c9559c0": 23001,
            "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d": 23004}[service]
    command[-1:-1] = ["-o", "HostName=127.0.0.1", "-p", str(port)]
    if run(command, {"operation": "check", "server_id": service}) != {"ok": True}:
        raise ProviderError("Pilot guest readiness was not confirmed")


def check_guest_for_mode(service, primary, *, billing_only=False):
    """An explicit operator-approved exception applies to capture-only billing.

    It does not enable the controller, assign an address, promote an endpoint,
    or authorize deletion. Normal pilot and production purchase guards remain.
    """
    if not billing_only:
        guest_readiness(service, primary)


def same_paid_response(response, service, variant):
    expected = ORIGIN + "/api/services/" + service + "/ipv4/add"
    try:
        return (response.url == expected and response.request.url == expected
                and response.request.method == "POST"
                and response.request.post_data_json == {"variantId": variant})
    except Exception:
        return False


def receipt_record(response, service, primary, variant):
    """Record the exact response, without guessing any allocation/order fields."""
    if not same_paid_response(response, service, variant):
        raise ValueError("Response does not belong to the permitted POST")
    status = response.status
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError("Invalid HTTP status")
    result = {
        "format": 1, "request_key": REQUEST_KEY, "service_id": service,
        "expected_primary": primary, "http_status": status,
        "request": {"method": "POST", "path": "/api/services/" + service + "/ipv4/add",
                    "json": {"variantId": variant}},
        "idempotency_header_captured": False,
        "ownership_confirmed": False, "requires_receipt_schema_review": True,
    }
    # No cookie, Authorization, Set-Cookie or other credential headers are
    # read, stored or printed. The observed paid response is a private artifact.
    length = response.header_value("content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,12}", length):
            raise ValueError("Invalid response length")
        if int(length) > CAPTURE_LIMIT:
            return {**result, "body_omitted": "content_length_exceeds_limit"}
    raw = response.body()
    if not isinstance(raw, bytes):
        raise ValueError("Unexpected response body type")
    if len(raw) > CAPTURE_LIMIT:
        return {**result, "body_omitted": "body_exceeds_limit"}
    return {**result, "body_bytes": len(raw), "body_sha256": hashlib.sha256(raw).hexdigest(),
            "body_base64": base64.b64encode(raw).decode("ascii")}


class ReceiptPage(OneCentPage):
    def __init__(self, *args, store, expected_primary, preserved_additional=(),
                 upfront_cap=UPFRONT_CAP, billing_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.store, self.primary = store, canonical_ip(expected_primary)
        self.upfront_cap = approved_cap(self.server_id, self.primary, upfront_cap, billing_only=billing_only)
        self.expected_inventory = {self.primary: True}
        for address in preserved_additional:
            address = canonical_ip(address)
            if address in self.expected_inventory:
                raise ValueError("Duplicate preserved pilot address")
            self.expected_inventory[address] = False
        self.captured_responses = 0
        self.capture_error = None

    def inventory(self):
        rows = super().inventory()
        if {row.ip: row.primary for row in rows} != self.expected_inventory:
            raise ProviderError("Pilot protected existing IPv4 inventory changed")
        return rows

    def remove_primary(self, *_args, **_kwargs):
        raise ProviderError("The capture-only pilot cannot delete a primary address")

    def remove_additional(self, *_args, **_kwargs):
        raise ProviderError("The capture-only pilot cannot delete any address")

    def submit_order(self, expected_quote, *, request_id=REQUEST_KEY):
        if request_id != REQUEST_KEY:
            raise ProviderError("Only the one approved pilot request is allowed")
        if (not isinstance(expected_quote, Quote) or expected_quote.upfront_rub > self.upfront_cap
                or expected_quote.monthly_rub > MONTHLY_CAP or self._quote_variant is None
                or self._quote_kind != "current" or self._payment_sent):
            raise ProviderError("Pilot paid step is not bounded/confirmed")
        variant = self._quote_variant.id
        self.store.create("dispatch.json", {
            "format": 1, "request_key": REQUEST_KEY, "service_id": self.server_id,
            "expected_primary": self.primary, "upfront_rub": str(expected_quote.upfront_rub),
            "monthly_rub": str(expected_quote.monthly_rub), "variant_id": variant,
            "ownership_confirmed": False,
        })

        seen_responses = set()

        def observed(response):
            if not same_paid_response(response, self.server_id, variant):
                return
            if id(response) in seen_responses:
                return
            seen_responses.add(id(response))
            self.captured_responses += 1
            try:
                if self.captured_responses != 1:
                    raise ValueError("Multiple paid responses require manual review")
                self.store.create("response.json", receipt_record(response, self.server_id, self.primary, variant))
            except Exception as exc:
                self.capture_error = type(exc).__name__
                try:
                    self.store.create("capture-error.json", {
                        "format": 1, "request_key": REQUEST_KEY, "service_id": self.server_id,
                        "error_type": self.capture_error, "ownership_confirmed": False,
                    })
                except Exception:
                    pass

        self.page.on("response", observed)
        try:
            super().submit_order(expected_quote)
        finally:
            # A UI can close optimistically, or its click/wait can time out
            # while the permitted POST is still in flight. Pump one bounded
            # response wait before detaching the observer, never another click.
            if self._payment_sent and self.captured_responses == 0 and self.capture_error is None:
                try:
                    late = self.page.wait_for_event(
                        "response",
                        predicate=lambda response: same_paid_response(response, self.server_id, variant),
                        timeout=RECEIPT_WAIT_MS,
                    )
                    if self.captured_responses == 0:
                        observed(late)
                except Exception as exc:
                    self.capture_error = type(exc).__name__
                    try:
                        self.store.create("capture-error.json", {
                            "format": 1, "request_key": REQUEST_KEY, "service_id": self.server_id,
                            "error_type": self.capture_error, "stage": "response_deadline",
                            "ownership_confirmed": False,
                        })
                    except Exception:
                        pass
            # Even with an HTTP200 and a visible single inventory delta, do not
            # return to OneCentBilling.allocate's delta-only ownership path.
            # Its durable order and gross-budget reservation remain submitting.
            try:
                self.page.remove_listener("response", observed)
            finally:
                raise ReceiptReviewRequired("Pilot dispatch outcome needs immutable receipt review; do not repeat")


def allow_login_only(page, email, password):
    count = {"auth_posts": 0}

    def guard(route):
        request = route.request
        location = urlsplit(request.url)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            route.continue_()
            return
        same_origin = (location.scheme == "https" and location.hostname == "bill.1cent.host"
                       and location.port in (None, 443) and location.username is None
                       and location.password is None and not location.query and not location.fragment)
        try:
            correct = request.post_data_json == {"email": email, "password": password}
        except Exception:
            correct = False
        if (same_origin and request.method == "POST" and location.path == AUTH_PATH
                and correct and count["auth_posts"] == 0):
            count["auth_posts"] += 1
            route.continue_()
        else:
            route.abort()
    page.route("**/*", guard)
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=sorted(SERVICES), default=next(iter(SERVICES)))
    parser.add_argument("--expected-primary")
    parser.add_argument("--execute-one-approved-order", action="store_true")
    parser.add_argument("--billing-only-without-guest", action="store_true",
                        help="Explicitly approved capture-only billing exception")
    parser.add_argument("--approved-upfront-rub", choices=("100.00", "248.39"), default="100.00")
    parser.add_argument("--resume-unsubmitted-at-approved-price", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute_one_approved_order:
        print(json.dumps({"plan_only": True, "request_key": REQUEST_KEY, "service_id": args.service,
                          "upfront_cap_rub": args.approved_upfront_rub, "monthly_cap_rub": "300.00",
                          "keep_primary": True, "network_used": False, "credentials_read": False,
                          "can_assign_or_delete_addresses": False}))
        return 0
    report = {"request_key": REQUEST_KEY, "service_id": args.service, "ownership_confirmed": False}
    try:
        report["stage"] = "local_inputs"
        if os.geteuid() == 0 or not sys.platform.startswith("linux"):
            raise ValueError("Run only as the controller service user")
        primary = canonical_ip(args.expected_primary)
        upfront_cap = approved_cap(args.service, primary, Decimal(args.approved_upfront_rub),
                                   billing_only=args.billing_only_without_guest)
        if args.resume_unsubmitted_at_approved_price != (upfront_cap == AMENDED_UPFRONT_CAP):
            raise ValueError("The amended price requires the explicit unsubmitted-resume option")
        store = PilotStore(PILOT_DIRECTORY)
        store.prepare()
        if present(store.directory / "intent.json") and not args.resume_unsubmitted_at_approved_price:
            raise ReceiptReviewRequired("The one approved pilot has already been armed")
        controlled_file(ACCOUNT_CONFIG, 65536)
        settings = limited_settings(Settings.load(ACCOUNT_CONFIG), args.service, upfront_cap=upfront_cap,
                                    expected_primary=primary, billing_only=args.billing_only_without_guest)
        if args.resume_unsubmitted_at_approved_price:
            store.unsubmitted_amendment(settings, args.service, primary, upfront_cap=upfront_cap,
                                        billing_only=args.billing_only_without_guest)
        selection = json.loads(controlled_file(SELECTED_CONFIG, 65536), object_pairs_hook=_unique_object)
        validate_selection(selection, args.service)
        refresh = json.loads(controlled_file(REFRESH_CONFIG, 65536, own_private=True), object_pairs_hook=_unique_object)
        panel = Remnawave(refresh["panel_url"], dynamic_relay_worker.credential_token())
        report["stage"] = "live_target"
        report.update(validate_live_target(panel, args.service, primary, capture_only=True))
        report["billing_only_without_guest"] = args.billing_only_without_guest
        report["stage"] = "guest_readiness"
        check_guest_for_mode(args.service, primary, billing_only=args.billing_only_without_guest)
        report["stage"] = "credential_files"
        credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
        email = controlled_file(credentials / "onecent_email", 4096).decode().removesuffix("\n")
        password = controlled_file(credentials / "onecent_password", 4096).decode().removesuffix("\n")
        # A previous pilot invocation on either approved VPS is never silently
        # resumed, changed to another VPS, or treated as another paid allowance.
        with browser(settings) as page:
            report["stage"] = "service_page"
            allow_login_only(page, email, password)
            login = OneCentLogin(page, email, password, settings.journal / "authentication")
            ui = ReceiptPage(page, args.service, SERVICES[args.service]["service_name"],
                             login=login, store=store, expected_primary=primary,
                             upfront_cap=upfront_cap, billing_only=args.billing_only_without_guest,
                             preserved_additional=("191.217.252.35",)
                             if args.service == "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d" else ())
            ui.open()
            ui.inventory()
            report["stage"] = "final_binding"
            current_binding = validate_live_target(panel, args.service, primary, capture_only=True)
            if current_binding["host_address"] != report["host_address"]:
                raise ValueError("Host changed during the capture-only pilot")
            check_guest_for_mode(args.service, primary, billing_only=args.billing_only_without_guest)
            if args.resume_unsubmitted_at_approved_price:
                store.resume_unsubmitted(settings, args.service, primary, upfront_cap=upfront_cap,
                                         billing_only=args.billing_only_without_guest)
            else:
                store.arm(args.service, primary)
            report["stage"] = "quote_and_order"
            billing = OneCentBilling(settings, args.service, ui)
            billing.allocate(REQUEST_KEY)
            raise ProviderError("Pilot must never accept delta-only allocation ownership")
    except ReceiptReviewRequired:
        report["status"] = "receipt_review_required"
        print(json.dumps(report))
        return 2
    except BudgetDenied:
        report["status"] = "stopped_by_pre_dispatch_budget_or_price"
        print(json.dumps(report))
        return 3
    except (Exception, KeyboardInterrupt) as exc:
        # Browser/HTTP exception strings may contain account or request data.
        report.update(status="unconfirmed_do_not_repeat", error_type=type(exc).__name__)
        report["error_location"] = [Path(frame.filename).name + ":" + str(frame.lineno)
                                    for frame in traceback.extract_tb(exc.__traceback__)[-4:]]
        print(json.dumps(report))
        return 1


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    for stop in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(stop, interrupted)
    raise SystemExit(main())
