import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs
from xml.etree.ElementTree import Element, SubElement, fromstring

from spn_ip_failover.provider import Allocation, ProviderError
from spn_ip_failover.xorek import ENDPOINT
from spn_ip_failover.xorek_cart import (
    CartSettings, OutcomeUnconfirmed, XorekCart, cart_quote, checkout_quote, execute, load_settings,
)


SERVER = "2078852"
LINE = "383981"
BEFORE = [Allocation("101", "8.8.8.8"), Allocation("102", "1.1.1.1")]
NEW = Allocation("103", "9.9.9.9")


def document(function, metadata=False):
    root = Element("doc", {"func": function, "theme": "/manimg/dragon/"})
    if metadata:
        SubElement(root, "metadata", {"name": function, "type": "form"})
    # A fixture-only secret proves that raw API authentication fields never enter the journal.
    SubElement(SubElement(root, "tparams"), "authinfo").text = "DO-NOT-PERSIST-THIS-FIXTURE-SECRET"
    return root


def child(parent, tag, value=None, **attrs):
    item = SubElement(parent, tag, attrs)
    item.text = value
    return item


def money(parent, name, cost="120.00", period=None):
    value = child(child(parent, name), "price")
    child(value, "currency", "RUB")
    child(value, "cost", cost)
    if period is not None:
        child(value, "period", period)


def config(parent, count=3, cost="120.00"):
    item = child(child(parent, "configuration"), "elem")
    child(item, "configuration_name", "Public IPv4-address ")
    child(item, "configuration_value", f"{count} pcs.")
    money(item, "configuration_price", cost)


def totals(root, cost="120.00"):
    for name in ("prices", "totals"):
        money(child(child(root, "list", name=name), "elem"), "price", cost)


def cart(rows=()):
    root = document("cart", True)
    if not rows:
        child(root, "ok", "func=cart.empty", type="form", sametab="yes")
        return root
    listing = child(root, "list", name="lineitems")
    for row in rows:
        item = child(listing, "elem")
        child(item, "id", row["id"])
        child(item, "elid", row["server"])
        child(child(item, "main"), "name", "Edit addons of fixture service")
        config(item, row.get("count", 3), row.get("cost", "120.00"))
        money(item, "price", row.get("cost", "120.00"), "1")
        child(child(item, "cross"), "button", name="delete", type="ok", key="id")
    totals(root)
    return root


def toolbar(server=SERVER):
    root = document("service.ip")
    child(root, "plid", server)
    metadata = child(root, "metadata", name="service.ip", type="list")
    child(child(metadata, "toolbar"), "toolbtn", name="new", func="service.ip.edit", type="new")
    return root


def form(server=SERVER):
    root = document("service.ip.edit", True)
    for name, value in {"plid": server, "domain": server + ".xorek.cloud", "type": "2955", "count": "1",
                        "reboot": "off", "needreboot": "", "maxcount": "6"}.items():
        child(root, name, value)
    child(child(root, "slist", name="type"), "val", "Public IPv4-address ", key="2955")
    metadata = root.find("metadata")
    layout = child(metadata, "form")
    child(layout, "slider", name="count", min="1", max="6")
    child(layout, "input", name="reboot", type="checkbox")
    child(child(layout, "buttons"), "button", name="basket", type="ok")
    return root


def staged(server=SERVER):
    root = document("service.ip.edit")
    child(root, "lineitem.id", LINE)
    child(root, "ok", "func=cart", type="form")
    nested = child(root, "doc", func="service.ip.edit", theme="/manimg/dragon/")
    for name, value in {"plid": server, "domain": server + ".xorek.cloud", "type": "2955", "count": "1", "reboot": "off",
                        "needreboot": "", "manualprocessing": "", "manual": "off", "noexpense": "off", "postpaid": "off"}.items():
        child(nested, name, value)
    return root


def checkout(server=SERVER, line=LINE):
    root = document("cartorder.create.confirm", True)
    for name, value in {"elid": line, "paymethod_id": "0", "paymethod_select": "0", "store_paymethod": "off",
                        "button_type": "local", "bootstrap_allowed": "off", "cant_go_to_paymethod": "no",
                        "hide_profile_select": "yes", "newwindow": "", "order_inactive": "", "promocode": "", "applied_promocodes": ""}.items():
        child(root, name, value)
    child(child(root.find("metadata"), "form"), "button", name="order_local", type="ok")
    child(child(root, "slist", name="paymethod_select"), "val", key="0")
    child(child(child(root, "list", name="general_info"), "elem"), "position_count", "1 item")
    row = child(child(root, "list", name="details"), "elem")
    child(row, "item", "Edit addons", href="?func=vds&filter=on&id=" + server)
    child(row, "count", "1pcs")
    money(row, "price")
    config(row)
    totals(root)
    return root


def paid():
    root = document("cartorder.create.confirm")
    child(root, "billorder", "663")
    child(root, "ok", "func=cartorder&elid=663", type="form")
    return root


def inventory(rows):
    root = document("service.ip")
    for index, value in enumerate(rows):
        row = child(root, "elem")
        child(row, "id", value.ref)
        child(row, "name", value.ip)
        child(row, "is_main", "on" if index == 0 else "off")
        child(row, "no_delete", "off")
    return root


class FakeAPI:
    def __init__(self):
        self.assigned = list(BEFORE)
        self.rows = []
        self.calls = []
        self.timeout = None
        self.pay_mutation = None
        self.cart_mutation = None
        self.form_mutation = None
        self.checkout_mutation = None
        self.payment_mutation = None
        self.stage_extra_ip = False

    def request(self, function, **params):
        self.calls.append((function, {key: value for key, value in params.items() if key not in ("auth", "authinfo")}))
        mutate = params.get("sok") == "ok"
        if function == "service.ip" and params.get("out") == "devel":
            return toolbar(params["elid"])
        if function == "service.ip":
            return inventory(self.assigned)
        if function == "service.ip.edit" and not mutate:
            result = form(params["plid"])
            if self.form_mutation:
                self.form_mutation(result)
            return result
        if function == "service.ip.edit":
            self.rows.append({"id": LINE, "server": params["plid"], "count": len(self.assigned) + 1})
            if self.stage_extra_ip:
                self.assigned.append(NEW)
            if self.timeout == "stage":
                raise ProviderError("fixture staging timeout")
            return staged(params["plid"])
        if function == "cart" and not mutate:
            result = cart(self.rows)
            if self.cart_mutation:
                self.cart_mutation(result)
            return result
        if function == "cart":
            if params.get("clicked_button") != "delete" or set(params) - {"auth", "authinfo"} != {"clicked_button", "id", "sok"}:
                raise AssertionError("Unknown or unscoped cart mutation")
            self.rows = [row for row in self.rows if row["id"] != params["id"]]
            if self.timeout == "cleanup":
                raise ProviderError("fixture cleanup timeout")
            result = document("cart")
            child(result, "ok", "func=cart.empty" if not self.rows else "func=cart", type="form")
            return result
        if function == "cartorder.create.confirm" and not mutate:
            result = checkout()
            if self.checkout_mutation:
                self.checkout_mutation(result)
            return result
        if function == "cartorder.create.confirm":
            if self.timeout == "pay-before":
                raise ProviderError("fixture payment timeout")
            self.assigned.append(NEW)
            self.rows = [row for row in self.rows if row["id"] != params["elid"]]
            if self.pay_mutation:
                self.pay_mutation(self)
            if self.timeout == "pay-after":
                raise ProviderError("fixture payment timeout")
            result = paid()
            if self.payment_mutation:
                self.payment_mutation(result)
            return result
        if function == "service.ip.delete":
            self.assigned = [row for row in self.assigned if row.ref != params["elid"]]
            return fromstring("<doc><ok/></doc>")
        raise AssertionError("Unexpected API function")

    def mutations(self, function=None):
        return [(name, fields) for name, fields in self.calls if fields.get("sok") == "ok" and (function is None or name == function)]


class CartTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=str(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(self.directory.cleanup)
        self.config_value = {"version": 1, "journal_path": str(Path(self.directory.name) / "journal"),
                             "max_upfront_rub": "120.00", "max_monthly_rub": "120.00",
                             "servers": {SERVER: {"type": "2955", "domain": SERVER + ".xorek.cloud"},
                                         "22": {"type": "2955", "domain": "22.xorek.cloud"}}}
        self.settings = CartSettings.from_config(self.config_value)
        self.billing = XorekCart(self.settings, username="fixture-account", password="fixture-password")
        self.api = FakeAPI()
        self.billing._request = self.api.request
        self.sleep = patch("spn_ip_failover.xorek_cart.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def state(self):
        return json.loads((self.settings.journal_path / "state.json").read_text())["servers"][SERVER]

    def test_check_is_read_only_and_refuses_unrelated_cart(self):
        self.assertEqual(execute(self.billing, {"operation": "check", "server_id": SERVER}), {"ok": True})
        self.assertEqual(self.api.mutations(), [])
        self.assertFalse(self.settings.journal_path.exists())
        self.api.rows = [{"id": "999", "server": "22", "count": 1}]
        with self.assertRaises(ProviderError):
            self.billing.check(SERVER)
        self.assertEqual(self.api.mutations(), [])

    def test_quote_stages_once_checks_checkout_and_deletes_only_own_unpaid_row_without_payment(self):
        result = self.billing.quote(SERVER, "quote-one")
        self.assertEqual(result, {"quantity": 1, "upfront_rub": "120.00", "monthly_rub": "120.00", "within_caps": True})
        self.assertEqual(self.api.assigned, BEFORE)
        self.assertEqual(self.api.rows, [])
        self.assertEqual([name for name, _ in self.api.mutations()], ["service.ip.edit", "cart"])
        self.assertEqual(self.api.mutations("cart")[0][1], {"clicked_button": "delete", "id": LINE, "sok": "ok"})
        self.assertEqual(self.state()["quote-one"]["phase"], "cancelled")
        serialized = (self.settings.journal_path / "state.json").read_text()
        self.assertNotIn("FIXTURE-SECRET", serialized)
        with self.assertRaisesRegex(ProviderError, "already exists"):
            self.billing.quote(SERVER, "quote-one")
        self.assertEqual(len(self.api.mutations()), 2)

    def test_allocate_stages_then_pays_exactly_once_and_verifies_assignment_twice(self):
        result = self.billing.allocate(SERVER, "rotation-one")
        self.assertEqual(result, NEW)
        self.assertEqual([name for name, _ in self.api.mutations()], ["service.ip.edit", "cartorder.create.confirm"])
        self.assertEqual(self.api.mutations("service.ip.edit")[0][1],
                         {"plid": SERVER, "type": "2955", "domain": SERVER + ".xorek.cloud", "count": "1", "reboot": "off", "clicked_button": "basket", "sok": "ok"})
        self.assertEqual(self.api.mutations("cartorder.create.confirm")[0][1],
                         {"elid": LINE, "paymethod_id": "0", "store_paymethod": "off", "clicked_button": "order_local", "sok": "ok"})
        self.assertEqual(self.state()["rotation-one"]["phase"], "confirmed")
        reads_before = len(self.api.calls)
        self.assertEqual(self.billing.allocate(SERVER, "rotation-one"), NEW)
        self.assertEqual(len(self.api.calls), reads_before + 1)
        self.api.assigned.remove(NEW)
        with self.assertRaisesRegex(ProviderError, "no longer assigned"):
            self.billing.allocate(SERVER, "rotation-one")
        self.assertEqual(len(self.api.mutations("cartorder.create.confirm")), 1)

    def test_journal_records_intent_before_both_mutating_requests(self):
        original = self.api.request
        observed = []

        def verify(function, **params):
            if params.get("sok") == "ok":
                phase = self.state()["rotation-one"]["phase"]
                observed.append((function, phase))
            return original(function, **params)

        self.billing._request = verify
        self.billing.allocate(SERVER, "rotation-one")
        self.assertEqual(observed, [("service.ip.edit", "staging"), ("cartorder.create.confirm", "paying")])

    def test_budget_rejection_removes_own_draft_and_does_not_pay(self):
        for field in ("max_upfront_rub", "max_monthly_rub"):
            with self.subTest(field=field):
                self.setUp()
                values = {**self.config_value, field: "119.99"}
                self.billing.settings = CartSettings.from_config(values)
                with self.assertRaisesRegex(ProviderError, "limits"):
                    self.billing.allocate(SERVER, "over-budget")
                self.assertEqual(self.api.rows, [])
                self.assertEqual(self.api.mutations("cartorder.create.confirm"), [])
                self.assertEqual(self.state()["over-budget"]["phase"], "cancelled")

    def test_read_only_quote_can_show_a_price_above_caps_but_cleans_draft(self):
        self.billing.settings = CartSettings.from_config({**self.config_value, "max_upfront_rub": "0.00"})
        self.assertFalse(self.billing.quote(SERVER, "review-price")["within_caps"])
        self.assertEqual(self.api.rows, [])
        self.assertEqual(self.api.mutations("cartorder.create.confirm"), [])

    def test_staging_timeout_is_never_replayed_and_blocks_other_vps_orders(self):
        self.api.timeout = "stage"
        with self.assertRaises(ProviderError):
            self.billing.allocate(SERVER, "uncertain-stage")
        for server, identifier in ((SERVER, "uncertain-stage"), (SERVER, "another"), ("22", "other-vps")):
            with self.subTest(server=server), self.assertRaises(ProviderError):
                self.billing.allocate(server, identifier)
        self.assertEqual(len(self.api.mutations("service.ip.edit")), 1)
        self.assertEqual(self.state()["uncertain-stage"]["phase"], "staging")
        self.assertEqual(self.api.mutations("cartorder.create.confirm"), [])

    def test_payment_timeout_after_success_never_repeats_and_manual_reconcile_confirms(self):
        self.api.timeout = "pay-after"
        with self.assertRaises(ProviderError):
            self.billing.allocate(SERVER, "uncertain-pay")
        self.assertEqual(self.state()["uncertain-pay"]["phase"], "paying")
        with self.assertRaises(ProviderError):
            self.billing.allocate(SERVER, "uncertain-pay")
        self.billing.reconcile(SERVER, "uncertain-pay", NEW)
        self.assertEqual(self.billing.allocate(SERVER, "uncertain-pay"), NEW)
        self.assertEqual(len(self.api.mutations("cartorder.create.confirm")), 1)

    def test_no_purchase_reconcile_requires_explicit_declaration_empty_cart_and_unchanged_inventory(self):
        self.api.timeout = "pay-before"
        with self.assertRaises(ProviderError):
            self.billing.allocate(SERVER, "uncertain-pay")
        with self.assertRaisesRegex(ProviderError, "explicitly"):
            self.billing.reconcile(SERVER, "uncertain-pay")
        with self.assertRaisesRegex(ProviderError, "cart drafts"):
            self.billing.reconcile(SERVER, "uncertain-pay", no_purchase=True)
        self.api.rows.clear()  # Operator reviewed and removed the unpaid draft.
        self.api.assigned.append(NEW)
        with self.assertRaises(ProviderError):
            self.billing.reconcile(SERVER, "uncertain-pay", no_purchase=True)
        self.api.assigned = list(BEFORE)
        self.billing.reconcile(SERVER, "uncertain-pay", no_purchase=True)
        self.assertEqual(self.state()["uncertain-pay"]["phase"], "cancelled")
        with self.assertRaises(ProviderError):
            self.billing.allocate(SERVER, "uncertain-pay")
        self.assertEqual(len(self.api.mutations("cartorder.create.confirm")), 1)

    def test_cleanup_timeout_never_repeats_delete_and_can_be_reconciled_after_empty_cart(self):
        self.api.timeout = "cleanup"
        with self.assertRaises(ProviderError):
            self.billing.quote(SERVER, "cleanup-timeout")
        self.assertEqual(self.state()["cleanup-timeout"]["phase"], "cleanup_submitted")
        with self.assertRaises(ProviderError):
            self.billing.quote(SERVER, "cleanup-timeout")
        self.billing.reconcile(SERVER, "cleanup-timeout", no_purchase=True)
        self.assertEqual(len(self.api.mutations("cart")), 1)

    def test_unknown_form_or_reboot_request_never_stages(self):
        for mutate in (lambda form: setattr(form.find("reboot"), "text", "on"),
                       lambda form: setattr(form.find("needreboot"), "text", "yes"),
                       lambda form: setattr(form.find("count"), "text", "2"),
                       lambda form: setattr(form.find("type"), "text", "999"),
                       lambda form: setattr(form.find("domain"), "text", "another.xorek.cloud"),
                       lambda form: form.find("metadata").set("name", "other.form")):
            with self.subTest(mutate=mutate):
                self.setUp()
                self.api.form_mutation = mutate
                with self.assertRaises(ProviderError):
                    self.billing.allocate(SERVER, "bad-form")
                self.assertEqual(self.api.mutations(), [])

    def test_ten_ip_cap_is_enforced_before_staging(self):
        self.api.assigned = [Allocation(str(n), f"8.8.8.{n}") for n in range(1, 11)]
        with self.assertRaisesRegex(ProviderError, "ten-address"):
            self.billing.allocate(SERVER, "limit")
        self.assertEqual(self.api.mutations(), [])

    def test_stage_inventory_change_stops_without_payment_or_cart_cleanup(self):
        self.api.stage_extra_ip = True
        with self.assertRaises(OutcomeUnconfirmed):
            self.billing.allocate(SERVER, "unexpected-stage")
        self.assertEqual(self.state()["unexpected-stage"]["phase"], "staging_inventory_changed")
        self.assertEqual([name for name, _ in self.api.mutations()], ["service.ip.edit"])

    def test_existing_order_on_unpaid_checkout_requires_review_without_payment_or_cleanup(self):
        self.api.checkout_mutation = lambda root: child(root, "billorder", "777")
        with self.assertRaises(OutcomeUnconfirmed):
            self.billing.allocate(SERVER, "unexpected-order")
        self.assertEqual(self.state()["unexpected-order"]["phase"], "outcome_unconfirmed")
        self.assertEqual([name for name, _ in self.api.mutations()], ["service.ip.edit"])

    def test_changed_checkout_or_external_href_is_rejected_and_unpaid_draft_cleaned(self):
        for mutate in (lambda root: setattr(root.find("paymethod_id"), "text", "20"),
                       lambda root: root.find("list[@name='details']/elem/item").set("href", "https://elsewhere.invalid/pay"),
                       lambda root: setattr(root.find("list[@name='totals']/elem/price/price/cost"), "text", "121.00")):
            with self.subTest(mutate=mutate):
                self.setUp()
                self.api.checkout_mutation = mutate
                with self.assertRaises(ProviderError):
                    self.billing.allocate(SERVER, "bad-checkout")
                self.assertEqual(self.api.rows, [])
                self.assertEqual(self.api.mutations("cartorder.create.confirm"), [])

    def test_unrecognized_paid_reply_is_not_followed_or_retried(self):
        self.api.payment_mutation = lambda root: setattr(root.find("ok"), "text", "https://elsewhere.invalid/pay")
        with self.assertRaises(ProviderError):
            self.billing.allocate(SERVER, "bad-paid-reply")
        with self.assertRaises(ProviderError):
            self.billing.allocate(SERVER, "bad-paid-reply")
        self.assertEqual(self.state()["bad-paid-reply"]["phase"], "paying")
        self.assertEqual(len(self.api.mutations("cartorder.create.confirm")), 1)
        self.assertTrue(all(name in ("cart", "service.ip", "service.ip.edit", "cartorder.create.confirm") for name, _ in self.api.calls))

    def test_quote_cleanup_preserves_foreign_cart_items_added_after_preflight(self):
        original = self.api.request
        injected = False

        def request(function, **params):
            nonlocal injected
            if function == "cartorder.create.confirm" and not params.get("sok") and not injected:
                self.api.rows.append({"id": "999", "server": "22", "count": 1})
                injected = True
            return original(function, **params)

        self.billing._request = request
        self.billing.quote(SERVER, "quote-with-other-item")
        self.assertEqual(self.api.rows, [{"id": "999", "server": "22", "count": 1}])
        self.assertEqual(self.api.mutations("cart")[0][1]["id"], LINE)

    def test_ambiguous_inventory_after_payment_does_not_confirm_or_repeat_payment(self):
        self.api.pay_mutation = lambda api: api.assigned.remove(BEFORE[0])
        with self.assertRaisesRegex(ProviderError, "ambiguously"):
            self.billing.allocate(SERVER, "inventory-drift")
        self.assertEqual(self.state()["inventory-drift"]["phase"], "paying")
        with self.assertRaises(ProviderError):
            self.billing.allocate(SERVER, "inventory-drift")
        self.assertEqual(len(self.api.mutations("cartorder.create.confirm")), 1)

    def test_confirmation_requires_two_identical_snapshots_not_one(self):
        with patch.object(self.billing, "inventory", side_effect=[BEFORE + [NEW], BEFORE + [Allocation("104", "9.9.9.10")]]):
            with self.assertRaisesRegex(ProviderError, "changed between"):
                self.billing.verify_allocation(SERVER, set(BEFORE))

    def test_base_primary_removal_guard_is_preserved(self):
        with self.assertRaisesRegex(ProviderError, "primary"):
            self.billing.release(SERVER, BEFORE[0])
        self.assertEqual(self.api.mutations(), [])
        self.billing.release(SERVER, BEFORE[1])
        self.assertEqual(self.api.assigned, [BEFORE[0]])

    def test_strict_cli_request_fields_and_reconcile_choice(self):
        for request in ({"operation": "allocate", "server_id": SERVER, "request_id": "one", "postpaid": "on"},
                        {"operation": "quote", "server_id": SERVER, "request_id": ""},
                        {"operation": "quote", "server_id": SERVER, "request_id": None},
                        {"operation": "reconcile", "server_id": SERVER, "request_id": "one", "allocation": None},
                        {"operation": "reconcile", "server_id": SERVER, "request_id": "one", "no_purchase": 1},
                        {"operation": "inventory", "server_id": "999"}):
            with self.subTest(request=request), self.assertRaises(ProviderError):
                execute(self.billing, request)
        self.assertEqual(self.api.mutations(), [])

    def test_config_requires_protected_file_observed_type_domain_and_decimal_caps(self):
        path = Path(self.directory.name) / "cart.json"
        path.write_text(json.dumps(self.config_value))
        path.chmod(0o600)
        self.assertEqual(load_settings(path), self.settings)
        path.chmod(0o666)
        with self.assertRaises(ProviderError):
            load_settings(path)
        for changes in ({"max_upfront_rub": "NaN"}, {"max_monthly_rub": "-1"},
                        {"servers": {SERVER: {"type": "private", "domain": SERVER + ".xorek.cloud"}}}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                CartSettings.from_config({**self.config_value, **changes})


class TransportAndSchemaTests(unittest.TestCase):
    def test_dragon_header_theme_and_authinfo_are_forced_without_mutation_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = CartSettings.from_config({"version": 1, "journal_path": str(Path(directory).resolve() / "journal"),
                                                "max_upfront_rub": "120", "max_monthly_rub": "120",
                                                "servers": {SERVER: {"type": "2955", "domain": SERVER + ".xorek.cloud"}}})
            billing = XorekCart(settings, username="fixture-user", password="fixture-password")
            with patch.object(billing.opener.inner, "open") as opener:
                opener.return_value.__enter__.return_value.read.return_value = b'<doc func="service.ip"><elem><id>1</id><name>8.8.8.8</name></elem></doc>'
                self.assertEqual(billing.inventory(SERVER), (Allocation("1", "8.8.8.8"),))
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, ENDPOINT)
            self.assertEqual(request.get_header("Isp-client"), "Web-interface")
            self.assertEqual(parse_qs(request.data.decode())["theme"], ["dragon"])
            self.assertEqual(parse_qs(request.data.decode())["authinfo"], ["fixture-user:fixture-password"])
            with patch.object(billing.opener.inner, "open", side_effect=TimeoutError) as opener:
                with self.assertRaises(ProviderError):
                    billing._call("cartorder.create.confirm", elid=LINE, sok="ok")
                opener.assert_called_once()

    def test_cart_requires_exact_single_ipv4_increment_currency_totals_and_monthly_period(self):
        row = {"id": LINE, "server": SERVER, "count": 3}
        quote = cart_quote(cart([row]), LINE, SERVER, 2)
        checkout_quote(checkout(), quote)
        for mutate in (lambda root: setattr(root.find("list[@name='lineitems']/elem/configuration/elem/configuration_value"), "text", "4 pcs."),
                       lambda root: setattr(root.find("list[@name='lineitems']/elem/price/price/period"), "text", "12"),
                       lambda root: setattr(root.find("list[@name='lineitems']/elem/price/price/currency"), "text", "USD"),
                       lambda root: setattr(root.find("list[@name='totals']/elem/price/price/cost"), "text", "121"),
                       lambda root: root.find("list[@name='lineitems']/elem/cross/button").set("key", "selected")):
            with self.subTest(mutate=mutate):
                root = cart([row])
                mutate(root)
                with self.assertRaises(ProviderError):
                    cart_quote(root, LINE, SERVER, 2)


if __name__ == "__main__":
    unittest.main()
