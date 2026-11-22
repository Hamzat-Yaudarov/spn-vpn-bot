from decimal import Decimal
from dataclasses import replace
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import UUID

from spn_ip_failover.onecent import OneCentBilling, Service, Settings, execute
from spn_ip_failover import onecent
from spn_ip_failover.budget import AccountBudget, BudgetDenied, BudgetSettings
from spn_ip_failover.onecent_fields import Address, Quote
from spn_ip_failover.provider import Allocation, ProviderError
from spn_ip_failover.state import Journal


SERVER = "00000000-0000-4000-8000-000000000001"
PRIMARY = "1.1.1.1"
EXTRA = "8.8.4.4"
NEW = "9.9.9.9"
VARIANT = "reviewed-test-variant"


def paid_proof(request, **updates):
    return {"format": 1, "basis": "guarded_paid_post_v1", "request_id": request, "service_id": SERVER,
            "variant_id": VARIANT, "idempotency_key": OneCentBilling.idempotency_key(SERVER, request),
            "method": "POST", "path": f"/api/services/{SERVER}/ipv4/add", "http_status": 200,
            "pending": False, "address": None, "amount_rub": "16.13", "response_sha256": "a" * 64, **updates}


def release_proof(ip, key, *, index=2, **updates):
    return {"format": 1, "basis": "guarded_release_v1", "service_id": SERVER, "idempotency_key": key,
            "method": "DELETE", "path": f"/api/services/{SERVER}/ipv4/{index}", "http_status": 200,
            "released": ip, "refunded_rub": "15.00", "response_sha256": "b" * 64, **updates}


class SettingsTests(unittest.TestCase):
    def test_exclusive_inventory_policy_is_explicit_per_service_and_unknown_modes_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = {"name": "vps-test", "max_upfront_rub": "20.00", "max_monthly_rub": "100.00"}
            config = {"profile": str(root / "profile"), "journal": str(root / "journal"),
                      "servers": {SERVER: service}}
            path = root / "settings.json"
            path.write_text(json.dumps(config))
            self.assertIsNone(Settings.load(path).servers[SERVER].ownership_policy)
            for mode in (onecent.OWNERSHIP_POLICY, True, "inventory_delta", "receipt_ip"):
                service["ownership_policy"] = mode
                path.write_text(json.dumps(config))
                if mode == onecent.OWNERSHIP_POLICY:
                    self.assertEqual(Settings.load(path).servers[SERVER].ownership_policy, mode)
                else:
                    with self.subTest(mode=mode), self.assertRaises(ValueError):
                        Settings.load(path)

    def test_browser_channel_allows_only_default_or_official_chrome(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "profile": str(root / "profile"), "journal": str(root / "journal"),
                "servers": {SERVER: {"name": "vps-test", "max_upfront_rub": "0.00", "max_monthly_rub": "0.00"}},
            }
            path = root / "settings.json"
            for channel in (None, "chrome"):
                path.write_text(json.dumps({**config, "browser_channel": channel}))
                self.assertEqual(Settings.load(path).browser_channel, channel)
            for channel in ("chrome-beta", "../../browser", ["chrome"], False):
                path.write_text(json.dumps({**config, "browser_channel": channel}))
                with self.subTest(channel=channel), self.assertRaises(ValueError):
                    Settings.load(path)

    def test_shared_budget_config_is_optional_but_never_shares_order_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"profile": str(root / "profile"), "journal": str(root / "orders"),
                      "servers": {SERVER: {"name": "vps-test", "max_upfront_rub": "3000.00", "max_monthly_rub": "300.00"}}}
            path = root / "settings.json"
            path.write_text(json.dumps(config))
            self.assertIsNone(Settings.load(path).budget)
            budget = {"journal": str(root / "account-budget"), "max_daily_rub": "3000.00", "timezone": "Europe/Saratov"}
            path.write_text(json.dumps({**config, "budget": budget}))
            settings = Settings.load(path)
            self.assertEqual(settings.budget.maximum_daily, Decimal("3000.00"))
            self.assertEqual(settings.budget.timezone, "Europe/Saratov")
            for alias in (config["profile"], config["journal"]):
                path.write_text(json.dumps({**config, "budget": {**budget, "journal": alias}}))
                with self.subTest(alias=alias), self.assertRaises(ValueError):
                    Settings.load(path)


class BrowserUI:
    def __init__(self):
        self.rows = {PRIMARY: True, EXTRA: False}
        self.current_quote = Quote(Decimal("16.13"), Decimal("100.00"))
        self.quotes = 0
        self.orders = 0
        self.deletions = []
        self.primary_deletions = []
        self.added = {NEW: False}
        self.release_fails = False

    def open(self):
        pass

    def inventory(self):
        return [Address(ip, primary) for ip, primary in self.rows.items()]

    def open_quote(self):
        self.quotes += 1
        return self.current_quote

    def close_quote(self):
        pass

    def order_variant_id(self):
        return VARIANT

    def submit_order(self, quote, *, request_id, idempotency_key):
        assert quote == self.current_quote
        assert idempotency_key == OneCentBilling.idempotency_key(SERVER, request_id)
        self.orders += 1
        self.rows.update(self.added)
        return paid_proof(request_id, idempotency_key=idempotency_key, amount_rub=f"{quote.upfront_rub:.2f}")

    def remove_additional(self, ip, *, before_submit, idempotency_key, expected_inventory):
        assert str(UUID(idempotency_key)) == idempotency_key
        assert expected_inventory == self.rows
        before_submit()
        self.deletions.append(ip)
        if self.release_fails:
            raise ProviderError("unknown confirmation")
        self.rows.pop(ip)
        return release_proof(ip, idempotency_key)

    def remove_primary(self, ip, replacement, *, before_submit, idempotency_key, expected_inventory):
        assert str(UUID(idempotency_key)) == idempotency_key
        assert expected_inventory == self.rows
        before_submit()
        self.primary_deletions.append((ip, replacement))
        if self.release_fails:
            raise ProviderError("unknown confirmation")
        self.rows = {replacement: True}
        return release_proof(ip, idempotency_key, index=0)


class BillingTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.settings = Settings(directory / "profile", directory / "journal", {
            SERVER: Service("vps-test", Decimal("20"), Decimal("100"), frozenset(), onecent.OWNERSHIP_POLICY),
        }, budget=BudgetSettings(directory / "daily-budget", Decimal("3000.00")))
        self.ui = BrowserUI()
        self.billing = OneCentBilling(self.settings, SERVER, self.ui, sleep=lambda seconds: None)

    def test_missing_exclusive_mutation_policy_disables_new_purchases_and_readiness(self):
        settings = replace(self.settings, servers={SERVER: replace(self.settings.servers[SERVER], ownership_policy=None)})
        billing = OneCentBilling(settings, SERVER, self.ui, sleep=lambda _: None)
        with self.assertRaises(BudgetDenied):
            billing.allocate("not-exclusive")
        with self.assertRaises(BudgetDenied):
            execute(billing, {"operation": "check", "server_id": SERVER})
        self.assertEqual(self.ui.orders, 0)
        self.assertEqual(self.ui.quotes, 0)
        self.assertFalse((self.settings.budget.journal / "state.json").exists())

    def test_every_protected_role_or_extra_address_drift_holds_the_paid_flow(self):
        normal = {PRIMARY: True, EXTRA: False, NEW: False}
        cases = ({PRIMARY: True, NEW: False},
                 {PRIMARY: False, EXTRA: False, NEW: True},
                 {**normal, "8.8.8.8": False},
                 {**normal, NEW: True},
                 [normal, {PRIMARY: True, EXTRA: False, "8.8.8.8": False}],
                 [normal, {PRIMARY: True, EXTRA: False}])
        for index, changes in enumerate(cases):
            with self.subTest(index=index), TemporaryDirectory() as directory:
                root = Path(directory)
                settings = replace(self.settings, profile=root / "profile", journal=root / "orders",
                                   budget=BudgetSettings(root / "budget", Decimal("3000.00")))
                ui = BrowserUI()
                snapshots = iter(changes if isinstance(changes, list) else [changes])
                ui.open = lambda: setattr(ui, "rows", dict(next(snapshots)))
                billing = OneCentBilling(settings, SERVER, ui, sleep=lambda _: None)
                with self.assertRaises(ProviderError):
                    billing.allocate("drift")
                order = Journal(settings.journal).snapshot()["servers"][SERVER]["orders"]["drift"]
                self.assertEqual(order["phase"], "submitting")
                self.assertIn("paid_response", order)
                self.assertNotIn("ownership", order)
                self.assertNotIn("ip", order)
                self.assertEqual(billing.account_budget().record(SERVER, "drift")["phase"], "submitting")
                with self.assertRaises(ProviderError):
                    billing.allocate("second-drift")
                self.assertEqual(ui.orders, 1)

    def test_preexisting_manual_additional_address_is_never_adopted(self):
        self.ui.added = {EXTRA: False}
        with self.assertRaises(ProviderError):
            self.billing.allocate("manual-is-not-new")
        with self.assertRaises(ProviderError):
            self.billing.release(Allocation(EXTRA, EXTRA))
        self.assertEqual(self.ui.deletions, [])

    def test_inventory_quote_and_one_order_are_independent(self):
        base = {"server_id": SERVER}
        inventory = execute(self.billing, {**base, "operation": "inventory"})
        self.assertEqual(len(inventory["allocations"]), 2)
        quote = execute(self.billing, {**base, "operation": "quote"})
        self.assertEqual(quote, {"quantity": 1, "upfront_rub": "16.13", "monthly_rub": "100.00"})
        self.assertEqual(self.ui.orders, 0)
        self.assertEqual(self.billing.allocate("new-request"), Allocation(NEW, NEW))
        self.assertEqual(self.billing.allocate("new-request"), Allocation(NEW, NEW))
        self.assertEqual(self.ui.orders, 1)

    def test_upfront_monthly_and_address_caps_prevent_submit(self):
        self.ui.current_quote = Quote(Decimal("20.01"), Decimal("100.00"))
        with self.assertRaises(ProviderError):
            self.billing.allocate("expensive-now")
        self.ui.current_quote = Quote(Decimal("16.13"), Decimal("100.01"))
        with self.assertRaises(ProviderError):
            self.billing.allocate("expensive-renewal")
        self.ui.rows.update({f"8.8.8.{n}": False for n in range(1, 9)})
        with self.assertRaises(ProviderError):
            self.billing.allocate("cap")
        self.assertEqual(self.ui.orders, 0)

    def test_ambiguous_purchase_is_not_repeated_after_restart(self):
        self.ui.added = {}
        with self.assertRaises(ProviderError):
            self.billing.allocate("maybe-purchased")
        state = Journal(self.settings.journal).snapshot()
        self.assertEqual(state["servers"][SERVER]["orders"]["maybe-purchased"]["phase"], "submitting")
        restarted = OneCentBilling(self.settings, SERVER, self.ui, sleep=lambda seconds: None)
        for request in ("maybe-purchased", "second-while-uncertain"):
            with self.assertRaises(ProviderError):
                restarted.allocate(request)
        self.assertEqual(self.ui.orders, 1)
        self.ui.rows[NEW] = False
        with self.assertRaises(ProviderError):
            restarted.reconcile("maybe-purchased", Allocation(NEW, NEW))
        self.assertEqual(self.ui.orders, 1)

    def test_unique_inventory_addition_without_guarded_paid_response_never_proves_ownership(self):
        original = self.ui.submit_order

        def no_receipt(quote, *, request_id, idempotency_key):
            original(quote, request_id=request_id, idempotency_key=idempotency_key)
            return None

        self.ui.submit_order = no_receipt
        with self.assertRaises(ProviderError):
            self.billing.allocate("no-receipt")
        order = Journal(self.settings.journal).snapshot()["servers"][SERVER]["orders"]["no-receipt"]
        self.assertEqual(order["phase"], "submitting")
        self.assertNotIn("ip", order)
        self.assertNotIn("paid_response", order)
        self.assertNotIn("ownership", order)
        self.assertEqual(self.billing.account_budget().record(SERVER, "no-receipt")["phase"], "submitting")
        for target in (None, Allocation(NEW, NEW)):
            with self.assertRaises(ProviderError):
                self.billing.reconcile("no-receipt", target)
        with self.assertRaises(ProviderError):
            self.billing.release(Allocation(NEW, NEW))
        restarted = OneCentBilling(self.settings, SERVER, self.ui, sleep=lambda _: None)
        for request_id in ("no-receipt", "another-while-unknown"):
            with self.assertRaises(ProviderError):
                restarted.allocate(request_id)
        self.assertEqual(self.ui.orders, 1)
        self.assertEqual(self.ui.deletions, [])

    def test_explicit_exclusive_paid_flow_records_inventory_basis_not_a_provider_receipt_ip(self):
        self.ui.added = {"8.8.8.8": False}
        self.assertEqual(self.billing.allocate("other-delta"), Allocation("8.8.8.8", "8.8.8.8"))
        order = Journal(self.settings.journal).snapshot()["servers"][SERVER]["orders"]["other-delta"]
        self.assertEqual(order["phase"], "confirmed")
        self.assertNotIn("receipt", order)
        self.assertNotIn("ip", order["paid_response"])
        self.assertIsNone(order["paid_response"]["address"])
        self.assertEqual(order["ownership"]["basis"], onecent.OWNERSHIP_POLICY)
        self.assertIsNone(order["ownership"]["provider_response_address"])
        self.assertEqual(order["ownership"]["before"], {PRIMARY: True, EXTRA: False})
        self.assertEqual(order["ownership"]["after"], {PRIMARY: True, EXTRA: False, "8.8.8.8": False})

    def test_paid_response_requires_exact_request_service_key_amount_and_observed_schema(self):
        entry = {"request_id": "proof", "variant_id": VARIANT, "upfront_rub": "16.13",
                 "idempotency_key": self.billing.idempotency_key(SERVER, "proof")}
        for change in ({"format": True}, {"format": 2}, {"request_id": "other"},
                       {"service_id": "00000000-0000-4000-8000-000000000002"},
                       {"variant_id": "different"}, {"address": NEW}, {"pending": True},
                       {"idempotency_key": ""}, {"http_status": 201}, {"method": "GET"},
                       {"amount_rub": "16.14"}, {"amount_rub": 16.13}, {"amount_rub": "16.130"},
                       {"response_sha256": "b" * 63}, {"extra": "unreviewed"}):
            with self.subTest(change=change), self.assertRaises(ProviderError):
                self.billing.paid_response(paid_proof("proof", **change), entry)
        self.assertEqual(self.billing.paid_response(paid_proof("proof"), entry), paid_proof("proof"))

    def test_mismatched_paid_response_holds_both_journals_and_is_never_retried(self):
        original = self.ui.submit_order

        def mismatched(quote, *, request_id, idempotency_key):
            result = original(quote, request_id=request_id, idempotency_key=idempotency_key)
            return {**result, "variant_id": "different"}

        self.ui.submit_order = mismatched
        with self.assertRaises(ProviderError):
            self.billing.allocate("wrong-proof")
        order = Journal(self.settings.journal).snapshot()["servers"][SERVER]["orders"]["wrong-proof"]
        self.assertEqual(order["phase"], "submitting")
        self.assertNotIn("paid_response", order)
        self.assertEqual(self.billing.account_budget().record(SERVER, "wrong-proof")["phase"], "submitting")
        restarted = OneCentBilling(self.settings, SERVER, self.ui, sleep=lambda _: None)
        with self.assertRaises(ProviderError):
            restarted.allocate("wrong-proof")
        self.assertEqual(self.ui.orders, 1)

    def test_reviewed_paid_response_never_invents_ownership_or_replaces_saved_proof(self):
        original = self.ui.submit_order

        def lost(quote, *, request_id, idempotency_key):
            original(quote, request_id=request_id, idempotency_key=idempotency_key)
            raise ProviderError("response capture needs independent review")

        self.ui.submit_order = lost
        with self.assertRaises(ProviderError):
            self.billing.allocate("reviewed")
        with self.assertRaises(ProviderError):
            self.billing.record_paid_response("reviewed", paid_proof("reviewed", variant_id="different"))
        self.billing.record_paid_response("reviewed", paid_proof("reviewed"))
        self.billing.record_paid_response("reviewed", paid_proof("reviewed"))
        with self.assertRaises(ProviderError):
            self.billing.record_paid_response("reviewed", paid_proof("reviewed", response_sha256="b" * 64))
        order = Journal(self.settings.journal).snapshot()["servers"][SERVER]["orders"]["reviewed"]
        self.assertEqual(order["phase"], "submitting")
        self.assertEqual(order["paid_response"], paid_proof("reviewed"))
        with self.assertRaises(ProviderError):
            self.billing.reconcile("reviewed", Allocation(NEW, NEW))
        with self.assertRaises(ProviderError):
            self.billing.allocate("reviewed")
        self.assertEqual(self.ui.orders, 1)

    def test_paid_response_is_durable_before_inventory_but_crash_does_not_adopt_a_later_delta(self):
        def interrupted(before):
            order = Journal(self.settings.journal).snapshot()["servers"][SERVER]["orders"]["proof-first"]
            self.assertEqual(order["paid_response"], paid_proof("proof-first"))
            self.assertNotIn("ownership", order)
            self.assertEqual(order["phase"], "submitting")
            self.assertEqual(self.billing.account_budget().record(SERVER, "proof-first")["phase"], "submitting")
            raise ProviderError("interrupted before inventory confirmation")

        self.billing.wait_for_purchase = interrupted
        with self.assertRaises(ProviderError):
            self.billing.allocate("proof-first")
        restarted = OneCentBilling(self.settings, SERVER, self.ui, sleep=lambda _: None)
        with self.assertRaises(ProviderError):
            restarted.reconcile("proof-first", Allocation(NEW, NEW))
        self.assertEqual(self.ui.orders, 1)

    def test_crash_after_financial_confirmation_recovers_only_from_durable_complete_flow_evidence(self):
        original = AccountBudget.transition

        def transition(budget, server_id, request_id, phase):
            original(budget, server_id, request_id, phase)
            if phase == "confirmed":
                raise OSError("crash between journals")

        with patch.object(AccountBudget, "transition", autospec=True, side_effect=transition), self.assertRaises(OSError):
            self.billing.allocate("confirmed-budget")
        order = Journal(self.settings.journal).snapshot()["servers"][SERVER]["orders"]["confirmed-budget"]
        self.assertEqual(order["phase"], "submitting")
        self.assertEqual(order["paid_response"], paid_proof("confirmed-budget"))
        self.assertEqual(order["ownership"]["ip"], NEW)
        self.assertEqual(self.billing.account_budget().record(SERVER, "confirmed-budget")["phase"], "confirmed")
        restarted = OneCentBilling(self.settings, SERVER, self.ui, sleep=lambda _: None)
        restarted.reconcile("confirmed-budget", Allocation(NEW, NEW))
        self.assertEqual(restarted.allocate("confirmed-budget"), Allocation(NEW, NEW))
        self.assertEqual(self.ui.orders, 1)

    def test_historical_delta_only_confirmation_cannot_replay_delete_or_authorize_primary_replacement(self):
        self.billing.allocate("historical")
        with Journal(self.settings.journal) as journal:
            del journal.data["servers"][SERVER]["orders"]["historical"]["ownership"]
            journal.save()
        del self.ui.rows[EXTRA]
        with self.assertRaises(ProviderError):
            self.billing.allocate("historical")
        with self.assertRaises(ProviderError):
            self.billing.release(Allocation(NEW, NEW))
        with self.assertRaises(ProviderError):
            self.billing.release(Allocation(PRIMARY, PRIMARY), replacement=Allocation(NEW, NEW))
        self.assertEqual(self.ui.orders, 1)
        self.assertEqual(self.ui.deletions, [])
        self.assertEqual(self.ui.primary_deletions, [])

    def test_valid_flow_evidence_cannot_replay_under_a_different_journal_request_key(self):
        self.billing.allocate("original")
        with Journal(self.settings.journal) as journal:
            entries = journal.data["servers"][SERVER]["orders"]
            entries["other-request"] = dict(entries["original"])
            journal.save()
        with self.assertRaises(ProviderError):
            self.billing.allocate("other-request")
        self.assertEqual(self.ui.orders, 1)

    def test_releases_only_confirmed_additional_addresses(self):
        self.billing.allocate("new-request")
        for ip in (PRIMARY, EXTRA):
            with self.assertRaises(ProviderError):
                self.billing.release(Allocation(ip, ip))
        self.billing.release(Allocation(NEW, NEW))
        self.billing.release(Allocation(NEW, NEW))
        self.assertEqual(self.ui.deletions, [NEW])
        self.assertEqual(self.ui.rows, {PRIMARY: True, EXTRA: False})

    def test_unknown_removal_is_never_clicked_again(self):
        self.billing.allocate("new-request")
        self.ui.release_fails = True
        for _ in range(2):
            with self.assertRaises(ProviderError):
                self.billing.release(Allocation(NEW, NEW))
        self.assertEqual(self.ui.deletions, [NEW])
        pending = Journal(self.settings.journal).snapshot()["servers"][SERVER]["releases"][NEW]
        self.assertEqual(str(UUID(pending["idempotency_key"])), pending["idempotency_key"])
        self.assertEqual(pending["before"], {PRIMARY: True, EXTRA: False, NEW: False})
        self.assertEqual(pending["expected"], {PRIMARY: True, EXTRA: False})
        del self.ui.rows[NEW]
        self.billing.release(Allocation(NEW, NEW))

    def test_release_key_and_expected_inventory_are_durable_before_the_delete_call(self):
        self.billing.allocate("remove-key")
        original = self.ui.remove_additional
        observed = []

        def remove(ip, *, before_submit, idempotency_key, expected_inventory):
            def durable_marker():
                before_submit()
                pending = Journal(self.settings.journal).snapshot()["servers"][SERVER]["releases"][ip]
                self.assertEqual(pending["idempotency_key"], idempotency_key)
                self.assertEqual(pending["target_ip"], NEW)
                observed.append(idempotency_key)
            return original(ip, before_submit=durable_marker, idempotency_key=idempotency_key,
                            expected_inventory=expected_inventory)

        self.ui.remove_additional = remove
        self.billing.release(Allocation(NEW, NEW))
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.ui.rows, {PRIMARY: True, EXTRA: False})
        state = Journal(self.settings.journal).snapshot()["servers"][SERVER]
        evidence = state["release_evidence"][observed[0]]
        self.assertEqual(state["releases"][NEW], "released")
        self.assertEqual(evidence["phase"], "released")
        self.assertEqual(evidence["response"]["released"], NEW)
        self.assertEqual(evidence["response"]["refunded_rub"], "15.00")

    def test_delete_response_rejects_other_service_target_key_path_or_unknown_refund(self):
        key = "00000000-0000-4000-8000-000000000002"
        for change in ({"format": True}, {"service_id": "other"}, {"released": EXTRA},
                       {"idempotency_key": "other"}, {"http_status": 201}, {"method": "POST"},
                       {"path": f"/api/services/{SERVER}/ipv4/2?other=1"},
                       {"refunded_rub": "NaN"}, {"refunded_rub": 15}, {"extra": "unreviewed"}):
            with self.subTest(change=change), self.assertRaises(ProviderError):
                self.billing.release_response(release_proof(NEW, key, **change), NEW, key)

    def test_unknown_delete_response_is_held_without_reclick_and_preserves_gross_budget(self):
        self.billing.allocate("delete-unknown")
        original = self.ui.remove_additional

        def unknown(ip, *, before_submit, idempotency_key, expected_inventory):
            original(ip, before_submit=before_submit, idempotency_key=idempotency_key,
                     expected_inventory=expected_inventory)
            return None

        self.ui.remove_additional = unknown
        gross = self.billing.account_budget().summary()
        with self.assertRaises(ProviderError):
            self.billing.release(Allocation(NEW, NEW))
        pending = Journal(self.settings.journal).snapshot()["servers"][SERVER]["releases"][NEW]
        self.assertIn("idempotency_key", pending)
        self.assertNotIn("response", pending)
        self.billing.release(Allocation(NEW, NEW))
        self.assertEqual(self.ui.deletions, [NEW])
        self.assertEqual(self.billing.account_budget().summary(), gross)

    def test_retire_primary_requires_exactly_one_confirmed_replacement(self):
        self.billing.allocate("new-request")
        old, current = Allocation(PRIMARY, PRIMARY), Allocation(NEW, NEW)
        with self.assertRaises(ProviderError):
            self.billing.release(old, replacement=current)
        self.assertEqual(self.ui.primary_deletions, [])
        del self.ui.rows[EXTRA]
        request = {"operation": "retire", "server_id": SERVER,
                   "old": old.as_payload(), "current": current.as_payload()}
        self.assertEqual(execute(self.billing, request), {"ok": True})
        self.assertEqual(execute(self.billing, request), {"ok": True})
        self.assertEqual(self.ui.primary_deletions, [(PRIMARY, NEW)])
        self.assertEqual(self.ui.rows, {NEW: True})

    def test_interrupted_primary_removal_checks_promotion_before_acknowledgement(self):
        del self.ui.rows[EXTRA]
        self.billing.allocate("new-request")
        old, current = Allocation(PRIMARY, PRIMARY), Allocation(NEW, NEW)
        self.ui.release_fails = True
        with self.assertRaises(ProviderError):
            self.billing.release(old, replacement=current)
        self.ui.rows = {NEW: False}
        with self.assertRaises(ProviderError):
            self.billing.release(old, replacement=current)
        self.ui.rows = {NEW: True}
        self.billing.release(old, replacement=current)
        self.assertEqual(self.ui.primary_deletions, [(PRIMARY, NEW)])

    def test_failure_before_delete_dispatch_can_be_retried(self):
        self.billing.allocate("new-request")
        original = self.ui.remove_additional

        def preflight_failure(*args, **kwargs):
            raise ProviderError("page preflight failed")

        self.ui.remove_additional = preflight_failure
        with self.assertRaises(ProviderError):
            self.billing.release(Allocation(NEW, NEW))
        self.ui.remove_additional = original
        self.billing.release(Allocation(NEW, NEW))
        self.assertEqual(self.ui.deletions, [NEW])

    def test_absent_or_zero_daily_budget_never_clicks_a_purchase(self):
        for budget in (None, BudgetSettings(self.settings.budget.journal, Decimal("0.00"))):
            billing = OneCentBilling(replace(self.settings, budget=budget), SERVER, self.ui, sleep=lambda _: None)
            with self.subTest(budget=budget), self.assertRaises(BudgetDenied):
                billing.allocate("disabled")
        self.assertEqual(self.ui.orders, 0)
        self.assertEqual(self.ui.quotes, 0)

    def test_budget_is_reserved_and_marked_before_purchase_click(self):
        original = self.ui.submit_order
        observed = []
        def submit(quote, *, request_id, idempotency_key):
            row = self.billing.account_budget().record(SERVER, "budget-first")
            observed.append((row["phase"], row["amount_rub"]))
            order = Journal(self.settings.journal).snapshot()["servers"][SERVER]["orders"][request_id]
            self.assertEqual(order["idempotency_key"], idempotency_key)
            self.assertEqual(order["ownership_policy"], onecent.OWNERSHIP_POLICY)
            return original(quote, request_id=request_id, idempotency_key=idempotency_key)
        self.ui.submit_order = submit
        self.billing.allocate("budget-first")
        self.assertEqual(observed, [("submitting", "16.13")])
        self.assertEqual(self.billing.account_budget().record(SERVER, "budget-first")["phase"], "confirmed")

    def test_unknown_submit_holds_budget_across_restart(self):
        def lost(_quote, *, request_id, idempotency_key):
            self.ui.orders += 1
            raise ProviderError("lost paid response")
        self.ui.submit_order = lost
        with self.assertRaises(ProviderError):
            self.billing.allocate("unknown-response")
        budget = self.billing.account_budget()
        self.assertEqual(budget.record(SERVER, "unknown-response")["phase"], "submitting")
        restarted = OneCentBilling(self.settings, SERVER, self.ui, sleep=lambda _: None)
        with self.assertRaises(ProviderError):
            restarted.allocate("unknown-response")
        self.assertEqual(self.ui.orders, 1)
        self.assertEqual(budget.summary()["used_rub"], "16.13")

    def test_ip_deletion_or_refund_does_not_restore_gross_daily_budget(self):
        self.billing.allocate("paid")
        before = self.billing.account_budget().summary()
        self.billing.release(Allocation(NEW, NEW))
        after = self.billing.account_budget().summary()
        self.assertEqual(after, before)
        self.assertEqual(after["used_rub"], "16.13")

    def test_daily_cap_denial_happens_before_order_submit(self):
        billing = OneCentBilling(replace(self.settings, budget=BudgetSettings(
            self.settings.budget.journal, Decimal("16.12"))), SERVER, self.ui, sleep=lambda _: None)
        with self.assertRaises(BudgetDenied):
            billing.allocate("too-much-for-day")
        self.assertEqual(self.ui.orders, 0)

    def test_unchanged_inventory_cannot_cancel_a_dispatched_unknown_order(self):
        self.ui.added = {}
        with self.assertRaises(ProviderError):
            self.billing.allocate("no-result")
        with self.assertRaises(ProviderError):
            self.billing.reconcile("no-result", None)
        self.assertEqual(self.billing.account_budget().summary()["used_rub"], "16.13")
        self.assertEqual(self.billing.account_budget().record(SERVER, "no-result")["phase"], "submitting")

    def test_orphan_reservation_can_be_reconciled_but_never_refunds_today(self):
        budget = self.billing.account_budget()
        budget.reserve(SERVER, "before-order-journal", Decimal("16.13"))
        self.billing.reconcile("before-order-journal", None)
        self.assertEqual(budget.record(SERVER, "before-order-journal")["phase"], "no_purchase")
        self.assertEqual(budget.summary()["used_rub"], "16.13")

    def test_reserved_budget_marker_proves_an_existing_order_never_dispatched(self):
        budget = self.billing.account_budget()
        request = "before-budget-dispatch"
        key = budget.reserve(SERVER, request, Decimal("16.13"))
        with Journal(self.settings.journal) as journal:
            self.billing.orders(journal)["orders"][request] = {
                "phase": "submitting", "request_id": request, "before": dict(self.ui.rows),
                "variant_id": VARIANT,
                "budget": {"journal": str(self.settings.budget.journal.resolve()), "key": key},
            }
            journal.save()
        self.billing.reconcile(request, None)
        self.assertEqual(budget.record(SERVER, request)["phase"], "no_purchase")
        order = Journal(self.settings.journal).snapshot()["servers"][SERVER]["orders"][request]
        self.assertEqual(order["phase"], "cancelled")
        self.assertEqual(self.ui.orders, 0)
        self.assertEqual(budget.summary()["used_rub"], "16.13")

    def test_changed_budget_config_is_not_a_new_dispatch_permission(self):
        self.billing.allocate("already-paid")
        restarted = OneCentBilling(replace(self.settings, budget=None), SERVER, self.ui, sleep=lambda _: None)
        with self.assertRaises(ProviderError) as caught:
            restarted.allocate("already-paid")
        self.assertNotIsInstance(caught.exception, BudgetDenied)
        self.assertEqual(self.ui.orders, 1)

    def run_main(self, settings, request):
        @contextmanager
        def browser(_settings):
            yield object()
        stream = SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
        with patch.object(onecent.Settings, "load", return_value=settings), \
                patch.object(onecent, "browser", browser), \
                patch("spn_ip_failover.onecent_ui.OneCentPage", return_value=self.ui), \
                patch.object(onecent.sys, "stdin", stream), \
                patch.dict(onecent.os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as output, \
                redirect_stderr(io.StringIO()):
            status = onecent.main([])
        return status, output.getvalue()

    def test_budget_denial_is_machine_readable_definitely_not_submitted(self):
        status, output = self.run_main(replace(self.settings, budget=None),
                                      {"operation": "allocate", "server_id": SERVER, "request_id": "disabled"})
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output), {"error": "not_submitted", "reason": "budget_limit"})
        self.assertEqual(self.ui.orders, 0)

    def test_lost_ui_response_is_not_mislabeled_as_budget_denial(self):
        def lost(_quote, *, request_id, idempotency_key):
            self.ui.orders += 1
            raise ProviderError("paid result unknown")
        self.ui.submit_order = lost
        status, output = self.run_main(self.settings,
                                      {"operation": "allocate", "server_id": SERVER, "request_id": "unknown"})
        self.assertEqual(status, 1)
        self.assertEqual(output, "")
        self.assertEqual(self.ui.orders, 1)

    def test_unknown_reconcile_cannot_erase_a_confirmed_gross_charge(self):
        self.ui.added = {}
        with self.assertRaises(ProviderError):
            self.billing.allocate("ambiguous")
        self.billing.account_budget().transition(SERVER, "ambiguous", "confirmed")
        with self.assertRaises(ProviderError):
            self.billing.reconcile("ambiguous", None)
        self.assertEqual(self.billing.account_budget().summary()["used_rub"], "16.13")


if __name__ == "__main__":
    unittest.main()
