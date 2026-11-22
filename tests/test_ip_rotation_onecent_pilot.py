import base64
from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover.budget import AccountBudget, BudgetSettings
from spn_ip_failover.onecent import OneCentBilling, Service, Settings
from spn_ip_failover.onecent_fields import Address, Quote
from spn_ip_failover.provider import ProviderError
from spn_ip_failover.state import Journal


SCRIPT = Path(__file__).resolve().parents[1] / "ops/ip-rotation-five/pilot_onecent.py"
SPEC = importlib.util.spec_from_file_location("onecent_pilot", SCRIPT)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)
SERVICE = "f5c0287a-636e-4981-95c7-80b24c9559c0"
OTHER = "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d"
PRIMARY = "8.8.8.8"
VARIANT = "observed-pool-variant"


def settings(root):
    return Settings(
        root / "profile", root / "orders",
        {service: Service(row["service_name"], Decimal("3000.00"), Decimal("300.00"), frozenset())
         for service, row in pilot.SERVICES.items()},
        budget=BudgetSettings(root / "budget", Decimal("3000.00")),
    )


def response(*, raw=b'{"unknown_vendor_field":"fixture-private-value"}', status=200):
    item = Mock()
    item.url = pilot.ORIGIN + "/api/services/" + SERVICE + "/ipv4/add"
    item.request.url = item.url
    item.request.method = "POST"
    item.request.post_data_json = {"variantId": VARIANT}
    item.status = status
    item.header_value.return_value = str(len(raw))
    item.body.return_value = raw
    return item


class PilotScopeTests(unittest.TestCase):
    def test_guest_exception_is_explicit_and_capture_only(self):
        with patch.object(pilot, "guest_readiness") as guest:
            pilot.check_guest_for_mode(SERVICE, PRIMARY)
            guest.assert_called_once_with(SERVICE, PRIMARY)
            guest.reset_mock()
            pilot.check_guest_for_mode(SERVICE, PRIMARY, billing_only=True)
            guest.assert_not_called()

    def test_default_plan_has_no_network_or_credential_reads(self):
        output = io.StringIO()
        with patch.object(pilot, "browser") as browser, \
                patch.object(pilot.Settings, "load") as config, \
                patch.object(pilot, "controlled_file") as secret, redirect_stdout(output):
            self.assertEqual(pilot.main([]), 0)
        value = json.loads(output.getvalue())
        self.assertTrue(value["plan_only"])
        self.assertEqual(value["request_key"], pilot.REQUEST_KEY)
        self.assertEqual(value["upfront_cap_rub"], "100.00")
        self.assertFalse(value["network_used"])
        self.assertFalse(value["credentials_read"])
        self.assertFalse(value["can_assign_or_delete_addresses"])
        browser.assert_not_called()
        config.assert_not_called()
        secret.assert_not_called()

    def test_caps_cannot_increase_the_shared_budget_or_service_limits(self):
        with TemporaryDirectory() as directory:
            original = settings(Path(directory))
            limited = pilot.limited_settings(original, SERVICE)
            self.assertEqual(limited.servers[SERVICE].maximum_upfront, Decimal("100.00"))
            self.assertEqual(limited.servers[SERVICE].maximum_monthly, Decimal("300.00"))
            self.assertEqual(set(limited.servers), {SERVICE})
            self.assertIs(limited.budget, original.budget)
            self.assertEqual((limited.profile, limited.journal), (original.profile, original.journal))
            cheaper = replace(original, servers={SERVICE: replace(original.servers[SERVICE],
                              maximum_upfront=Decimal("50.00"), maximum_monthly=Decimal("75.00"))})
            self.assertEqual(pilot.limited_settings(cheaper, SERVICE).servers[SERVICE].maximum_upfront,
                             Decimal("50.00"))
            for value in (replace(original, budget=None),
                          replace(original, budget=BudgetSettings(Path(directory) / "bad", Decimal("3000.01")))):
                with self.assertRaises(ValueError):
                    pilot.limited_settings(value, SERVICE)
            with self.assertRaises(ValueError):
                pilot.limited_settings(original, "00000000-0000-4000-8000-000000000001")

    def test_amended_limit_is_only_for_exact_unchanged_fi2_billing_only_scope(self):
        with TemporaryDirectory() as directory:
            original = settings(Path(directory))
            kwargs = {"upfront_cap": Decimal("248.39"), "expected_primary": pilot.AMENDED_PRIMARY,
                      "billing_only": True}
            limited = pilot.limited_settings(original, OTHER, **kwargs)
            self.assertEqual(limited.servers[OTHER].maximum_upfront, Decimal("248.39"))
            self.assertIs(limited.budget, original.budget)
            cheaper = replace(original, servers={OTHER: replace(original.servers[OTHER],
                              maximum_upfront=Decimal("200.00"))})
            self.assertEqual(pilot.limited_settings(cheaper, OTHER, **kwargs).servers[OTHER].maximum_upfront,
                             Decimal("200.00"))
            for service, changes in ((SERVICE, {}), (OTHER, {"billing_only": False}),
                                     (OTHER, {"expected_primary": PRIMARY}),
                                     (OTHER, {"upfront_cap": Decimal("248.40")})):
                with self.subTest(service=service, changes=changes), self.assertRaises(ValueError):
                    pilot.limited_settings(original, service, **{**kwargs, **changes})

    def test_pinned_scope_requires_the_original_five_and_exact_target_binding(self):
        rows = [{"service_id": service} for service in pilot.FIVE_SERVICE_IDS]
        row = next(row for row in rows if row["service_id"] == SERVICE)
        row.update(pilot.SERVICES[SERVICE])
        value = {"format": 1, "auto_purchase_enabled": False, "bypasses_excluded": True, "servers": rows}
        pilot.validate_selection(value, SERVICE)
        for bad in ({**value, "auto_purchase_enabled": True},
                    {**value, "servers": rows[:-1]},
                    {**value, "bypasses_excluded": False}):
            with self.assertRaises(ValueError):
                pilot.validate_selection(bad, SERVICE)
        row["node_uuid"] = pilot.SERVICES[OTHER]["node_uuid"]
        with self.assertRaises(ValueError):
            pilot.validate_selection(value, SERVICE)

    def test_current_node_and_host_must_use_exact_primary(self):
        metadata = pilot.SERVICES[SERVICE]
        panel = Mock()
        panel.read_node.return_value = {"uuid": metadata["node_uuid"], "address": PRIMARY,
                                       "isDisabled": False, "isConnected": True}
        panel.read_host.return_value = {"address": PRIMARY, "port": 443, "isDisabled": False,
                                       "nodes": [metadata["node_uuid"]]}
        pilot.validate_live_target(panel, SERVICE, PRIMARY)
        panel.read_host.return_value["address"] = "8.8.4.4"
        with self.assertRaises(ValueError):
            pilot.validate_live_target(panel, SERVICE, PRIMARY)
        drift = pilot.validate_live_target(panel, SERVICE, PRIMARY, capture_only=True)
        self.assertTrue(drift["host_address_drift"])
        self.assertEqual(drift["host_address"], "8.8.4.4")
        panel.read_host.return_value["address"] = PRIMARY
        panel.read_node.return_value["isConnected"] = False
        with self.assertRaises(ValueError):
            pilot.validate_live_target(panel, SERVICE, PRIMARY)

    def test_guest_check_uses_only_scoped_key_fixed_alias_and_readiness_rpc(self):
        run = Mock(return_value={"ok": True})
        pilot.guest_readiness(SERVICE, PRIMARY, run=run)
        command, data = run.call_args.args
        self.assertIn("HostKeyAlias=" + SERVICE, command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(command[-1], "root@" + PRIMARY)
        self.assertEqual(data, {"operation": "check", "server_id": SERVICE})
        run.return_value = {"ok": False}
        with self.assertRaises(ProviderError):
            pilot.guest_readiness(SERVICE, PRIMARY, run=run)


class PilotStoreTests(unittest.TestCase):
    def store(self, directory):
        store = pilot.PilotStore(Path(directory))
        # Production verifies all ancestors; test temporary-directory ancestors
        # are outside this test's ownership. Keep the actual private leaf check.
        store.prepare = lambda: None
        store.read = lambda name: (store.directory / name).read_bytes()
        return store

    def resume(self, store, config):
        store.resume_unsubmitted(config, OTHER, pilot.AMENDED_PRIMARY,
                                 upfront_cap=Decimal("248.39"), billing_only=True)

    def test_explicit_price_amendment_preserves_original_intent_and_can_only_reuse_identical_proof(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root)
            config = settings(root)
            store.arm(OTHER, pilot.AMENDED_PRIMARY)
            intent = (root / "intent.json").read_bytes()
            self.resume(store, config)
            path = root / "limit-amendment.json"
            raw, metadata = path.read_bytes(), path.stat()
            value = json.loads(raw)
            self.assertEqual(value["old_upfront_cap_rub"], "100.00")
            self.assertEqual(value["new_upfront_cap_rub"], "248.39")
            self.assertEqual(value["intent_sha256"], hashlib.sha256(intent).hexdigest())
            self.assertEqual(value["request_key"], pilot.REQUEST_KEY)
            self.assertEqual(value["service_id"], OTHER)
            self.assertEqual(value["expected_primary"], pilot.AMENDED_PRIMARY)
            self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.resume(store, config)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_mtime_ns, metadata.st_mtime_ns)
            self.assertEqual((root / "intent.json").read_bytes(), intent)
            self.assertFalse((config.journal / "state.json").exists())
            self.assertFalse((config.budget.journal / "state.json").exists())

    def test_any_dispatch_or_capture_artifact_blocks_price_resume(self):
        for name in ("dispatch.json", "response.json", "capture-error.json"):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                store = self.store(root)
                store.arm(OTHER, pilot.AMENDED_PRIMARY)
                store.create(name, {})
                with self.assertRaises(pilot.ReceiptReviewRequired):
                    self.resume(store, settings(root))
                self.assertFalse((root / "limit-amendment.json").exists())

    def test_even_empty_or_dangling_financial_state_file_blocks_resume_and_amendment_reuse(self):
        for journal in ("orders", "budget"):
            for dangling in (False, True):
                with self.subTest(journal=journal, dangling=dangling), TemporaryDirectory() as directory:
                    root = Path(directory)
                    store = self.store(root)
                    config = settings(root)
                    store.arm(OTHER, pilot.AMENDED_PRIMARY)
                    self.resume(store, config)
                    amendment = (root / "limit-amendment.json").read_bytes()
                    state = root / journal / "state.json"
                    state.parent.mkdir()
                    if dangling:
                        state.symlink_to(root / "missing-state")
                    else:
                        state.touch()
                    with self.assertRaises(pilot.ReceiptReviewRequired):
                        self.resume(store, config)
                    self.assertEqual((root / "limit-amendment.json").read_bytes(), amendment)

    def test_other_intent_or_nonidentical_amendment_cannot_be_resumed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root)
            store.arm(SERVICE, PRIMARY)
            with self.assertRaises(pilot.ReceiptReviewRequired):
                self.resume(store, settings(root))
            self.assertFalse((root / "limit-amendment.json").exists())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root)
            store.arm(OTHER, pilot.AMENDED_PRIMARY)
            store.create("limit-amendment.json", {"different": True})
            before = (root / "limit-amendment.json").read_bytes()
            with self.assertRaises(pilot.ReceiptReviewRequired):
                self.resume(store, settings(root))
            self.assertEqual((root / "limit-amendment.json").read_bytes(), before)

    def test_global_fence_is_private_and_not_reusable_for_another_vps(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            store.arm(SERVICE, PRIMARY)
            marker = Path(directory) / "intent.json"
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            before = marker.read_bytes()
            self.assertEqual(json.loads(before)["request_key"], pilot.REQUEST_KEY)
            for service in (SERVICE, OTHER):
                with self.assertRaises(FileExistsError):
                    store.arm(service, PRIMARY)
            self.assertEqual(marker.read_bytes(), before)

    def test_unknown_artifact_or_service_is_not_written(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            with self.assertRaises(ValueError):
                store.create("../anything", {})
            with self.assertRaises(ValueError):
                store.arm("00000000-0000-4000-8000-000000000001", PRIMARY)
            self.assertEqual(list(Path(directory).iterdir()), [])


class PilotReceiptTests(unittest.TestCase):
    def test_exact_response_is_private_data_not_an_ownership_claim(self):
        item = response()
        record = pilot.receipt_record(item, SERVICE, PRIMARY, VARIANT)
        self.assertEqual(base64.b64decode(record["body_base64"]), item.body.return_value)
        self.assertEqual(record["request"]["json"], {"variantId": VARIANT})
        self.assertEqual(record["http_status"], 200)
        self.assertFalse(record["ownership_confirmed"])
        self.assertTrue(record["requires_receipt_schema_review"])
        self.assertFalse(record["idempotency_header_captured"])
        item.header_value.assert_called_once_with("content-length")
        self.assertFalse(item.request.headers.called)

    def test_wrong_service_origin_method_or_payload_is_never_captured(self):
        for mutate in (
            lambda item: setattr(item, "url", item.url.replace(SERVICE, OTHER)),
            lambda item: setattr(item, "url", item.url.replace("bill.1cent.host", "foreign.invalid")),
            lambda item: setattr(item.request, "method", "GET"),
            lambda item: setattr(item.request, "post_data_json", {"variantId": VARIANT, "address": PRIMARY}),
            lambda item: setattr(item.request, "post_data_json", {"variantId": "chosen-other"}),
        ):
            item = response()
            mutate(item)
            self.assertFalse(pilot.same_paid_response(item, SERVICE, VARIANT))
            with self.assertRaises(ValueError):
                pilot.receipt_record(item, SERVICE, PRIMARY, VARIANT)
            item.body.assert_not_called()

    def test_oversized_header_or_body_is_not_persisted(self):
        item = response()
        item.header_value.return_value = str(pilot.CAPTURE_LIMIT + 1)
        self.assertEqual(pilot.receipt_record(item, SERVICE, PRIMARY, VARIANT)["body_omitted"],
                         "content_length_exceeds_limit")
        item.body.assert_not_called()
        item = response(raw=b"x" * (pilot.CAPTURE_LIMIT + 1))
        item.header_value.return_value = None
        value = pilot.receipt_record(item, SERVICE, PRIMARY, VARIANT)
        self.assertNotIn("body_base64", value)
        self.assertEqual(value["body_omitted"], "body_exceeds_limit")

    def test_unknown_or_failed_http_response_remains_only_a_receipt(self):
        for status in (201, 400, 403, 500):
            value = pilot.receipt_record(response(status=status), SERVICE, PRIMARY, VARIANT)
            self.assertEqual(value["http_status"], status)
            self.assertFalse(value["ownership_confirmed"])


class PilotAllocationTests(unittest.TestCase):
    def page(self, store):
        raw = Mock()
        page = pilot.ReceiptPage(raw, SERVICE, pilot.SERVICES[SERVICE]["service_name"],
                                 store=store, expected_primary=PRIMARY)
        page._quote_variant = SimpleNamespace(id=VARIANT)
        page._quote_kind = "current"
        page._opened_quote = Quote(Decimal("90.00"), Decimal("100.00"))
        return page, raw

    def test_response_capture_stops_before_delta_ownership_and_cannot_repeat(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = pilot.PilotStore(root / "pilot")
            store.directory.mkdir(mode=0o700)
            store.prepare = lambda: None
            store.arm(SERVICE, PRIMARY)
            page, raw = self.page(store)
            billing = OneCentBilling(pilot.limited_settings(settings(root), SERVICE), SERVICE, page)
            billing.inventory = Mock(return_value={PRIMARY: True})
            billing.quote = Mock(return_value=page._opened_quote)
            billing.wait_for_purchase = Mock()

            def submit(_quote):
                handler = raw.on.call_args.args[1]
                handler(response())

            with patch.object(pilot.OneCentPage, "submit_order", side_effect=submit) as paid, \
                    self.assertRaises(pilot.ReceiptReviewRequired):
                billing.allocate(pilot.REQUEST_KEY)
            paid.assert_called_once()
            raw.wait_for_event.assert_not_called()
            billing.wait_for_purchase.assert_not_called()
            order = Journal(billing.settings.journal).snapshot()["servers"][SERVICE]["orders"][pilot.REQUEST_KEY]
            self.assertEqual(order["phase"], "submitting")
            self.assertNotIn("ip", order)
            budget = billing.account_budget().record(SERVICE, pilot.REQUEST_KEY)
            self.assertEqual(budget["phase"], "submitting")
            captured = json.loads((store.directory / "response.json").read_bytes())
            self.assertFalse(captured["ownership_confirmed"])
            self.assertEqual((store.directory / "response.json").stat().st_mode & 0o777, 0o600)
            with patch.object(pilot.OneCentPage, "submit_order") as again, self.assertRaises(ProviderError):
                billing.allocate(pilot.REQUEST_KEY)
            again.assert_not_called()

    def test_lost_reply_still_holds_journals_and_never_uses_inventory_delta(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = pilot.PilotStore(root / "pilot")
            store.directory.mkdir(mode=0o700)
            store.prepare = lambda: None
            store.arm(SERVICE, PRIMARY)
            page, _raw = self.page(store)
            billing = OneCentBilling(pilot.limited_settings(settings(root), SERVICE), SERVICE, page)
            billing.inventory = Mock(return_value={PRIMARY: True})
            billing.quote = Mock(return_value=page._opened_quote)
            billing.wait_for_purchase = Mock()
            with patch.object(pilot.OneCentPage, "submit_order", side_effect=RuntimeError("private fixture")), \
                    self.assertRaises(pilot.ReceiptReviewRequired):
                billing.allocate(pilot.REQUEST_KEY)
            billing.wait_for_purchase.assert_not_called()
            self.assertFalse((store.directory / "response.json").exists())
            self.assertEqual(Journal(billing.settings.journal).snapshot()["servers"][SERVICE]["orders"]
                             [pilot.REQUEST_KEY]["phase"], "submitting")
            self.assertTrue((store.directory / "dispatch.json").exists())

    def test_capture_failures_are_private_and_dispatch_is_not_repeated(self):
        store = Mock()
        store.create.side_effect = lambda name, value: (_ for _ in ()).throw(OSError("private fixture")) \
            if name == "response.json" else None
        page, raw = self.page(store)

        def submit(_quote):
            raw.on.call_args.args[1](response())

        with patch.object(pilot.OneCentPage, "submit_order", side_effect=submit), \
                self.assertRaises(pilot.ReceiptReviewRequired):
            page.submit_order(page._opened_quote)
        self.assertEqual(page.capture_error, "OSError")
        self.assertEqual([call.args[0] for call in store.create.call_args_list],
                         ["dispatch.json", "response.json", "capture-error.json"])
        self.assertEqual(store.create.call_args_list[-1].args[1]["error_type"], "OSError")

    def test_late_response_after_ui_close_or_click_error_is_still_captured(self):
        for click_error in (None, RuntimeError("private click timeout")):
            with self.subTest(click_error=click_error is not None), TemporaryDirectory() as directory:
                store = pilot.PilotStore(Path(directory))
                store.prepare = lambda: None
                page, raw = self.page(store)

                def submit(_quote):
                    page._payment_sent = True
                    if click_error:
                        raise click_error

                def late_event(name, *, predicate, timeout):
                    self.assertEqual((name, timeout), ("response", pilot.RECEIPT_WAIT_MS))
                    item = response()
                    self.assertTrue(predicate(item))
                    raw.on.call_args.args[1](item)
                    return item

                raw.wait_for_event.side_effect = late_event
                with patch.object(pilot.OneCentPage, "submit_order", side_effect=submit) as paid, \
                        self.assertRaises(pilot.ReceiptReviewRequired):
                    page.submit_order(page._opened_quote)
                paid.assert_called_once()
                raw.wait_for_event.assert_called_once()
                self.assertTrue((Path(directory) / "response.json").exists())
                self.assertEqual(page.captured_responses, 1)
                self.assertIsNone(page.capture_error)

    def test_no_response_at_bounded_deadline_is_held_not_retried(self):
        store = Mock()
        page, raw = self.page(store)
        raw.wait_for_event.side_effect = TimeoutError("private fixture must not be persisted")

        def submit(_quote):
            page._payment_sent = True

        with patch.object(pilot.OneCentPage, "submit_order", side_effect=submit) as paid, \
                self.assertRaises(pilot.ReceiptReviewRequired):
            page.submit_order(page._opened_quote)
        paid.assert_called_once()
        self.assertEqual(page.captured_responses, 0)
        self.assertEqual(page.capture_error, "TimeoutError")
        error = store.create.call_args_list[-1].args[1]
        self.assertEqual(error["stage"], "response_deadline")
        self.assertNotIn("private", json.dumps(error))

    def test_matching_wait_result_is_captured_even_if_listener_delivery_is_deferred(self):
        with TemporaryDirectory() as directory:
            store = pilot.PilotStore(Path(directory))
            store.prepare = lambda: None
            page, raw = self.page(store)
            raw.wait_for_event.return_value = response()

            def submit(_quote):
                page._payment_sent = True

            with patch.object(pilot.OneCentPage, "submit_order", side_effect=submit), \
                    self.assertRaises(pilot.ReceiptReviewRequired):
                page.submit_order(page._opened_quote)
            self.assertEqual(page.captured_responses, 1)
            self.assertTrue((Path(directory) / "response.json").exists())

    def test_existing_dispatch_fence_blocks_before_paid_click(self):
        with TemporaryDirectory() as directory:
            store = pilot.PilotStore(Path(directory))
            store.prepare = lambda: None
            store.create("dispatch.json", {"old": "pending"})
            page, _raw = self.page(store)
            with patch.object(pilot.OneCentPage, "submit_order") as paid, self.assertRaises(FileExistsError):
                page.submit_order(page._opened_quote)
            paid.assert_not_called()

    def test_extra_addresses_or_delete_actions_are_never_allowed(self):
        page, _raw = self.page(Mock())
        with patch.object(pilot.OneCentPage, "inventory", return_value=[Address(PRIMARY, True)]):
            self.assertEqual(page.inventory(), [Address(PRIMARY, True)])
        for rows in ([Address(PRIMARY, True), Address("8.8.4.4", False)],
                     [Address("8.8.4.4", True)]):
            with patch.object(pilot.OneCentPage, "inventory", return_value=rows), self.assertRaises(ProviderError):
                page.inventory()
        with self.assertRaises(ProviderError):
            page.remove_primary(PRIMARY, "8.8.4.4")
        with self.assertRaises(ProviderError):
            page.remove_additional("8.8.4.4")

    def test_paid_boundary_rejects_price_above_approval(self):
        page, _raw = self.page(Mock())
        for quote in (Quote(Decimal("100.01"), Decimal("100.00")),
                      Quote(Decimal("90.00"), Decimal("300.01"))):
            with patch.object(pilot.OneCentPage, "submit_order") as paid, self.assertRaises(ProviderError):
                page.submit_order(quote)
            paid.assert_not_called()

    def test_amended_paid_boundary_accepts_only_the_scoped_per_run_limit(self):
        page = pilot.ReceiptPage(Mock(), OTHER, pilot.SERVICES[OTHER]["service_name"], store=Mock(),
                                 expected_primary=pilot.AMENDED_PRIMARY, upfront_cap=Decimal("248.39"),
                                 billing_only=True, preserved_additional=("191.217.252.35",))
        page._quote_variant = SimpleNamespace(id=VARIANT)
        page._quote_kind = "current"
        quote = Quote(Decimal("248.39"), Decimal("100.00"))
        with patch.object(pilot.OneCentPage, "submit_order") as paid, self.assertRaises(pilot.ReceiptReviewRequired):
            page.submit_order(quote)
        paid.assert_called_once_with(quote)
        self.assertEqual(page.expected_inventory,
                         {pilot.AMENDED_PRIMARY: True, "191.217.252.35": False})
        page.store.reset_mock()
        with patch.object(pilot.OneCentPage, "submit_order") as paid, self.assertRaises(ProviderError):
            page.submit_order(Quote(Decimal("248.40"), Decimal("100.00")))
        paid.assert_not_called()
        page.store.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
