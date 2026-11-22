"""Owner-only pricing and invoice recovery, entirely offline and side-effect free."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import unittest
from uuid import UUID, uuid4

from services.partner_checkout import CheckoutError, InvoiceRecoveryRequired, PartnerPayment, PaymentMismatch
from services.partner_enrollment_store import EnrollmentConflict, PartnerEnrollmentStore
from services.partner_entry_checkout import PartnerEntryCheckoutService
from services.partner_entry_pricing import (
    ENTRY_FEE_KOPECKS, ENTRY_PILOT_OWNER_ID, ENTRY_PILOT_PRICE_KOPECKS,
    EntryPricingError, EntryPricingPolicy,
)
from test_partner_store import RecordingConnection


OWNER = ENTRY_PILOT_OWNER_ID
NORMAL, PILOT = ENTRY_FEE_KOPECKS, ENTRY_PILOT_PRICE_KOPECKS
NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def order_row(*, owner=OWNER, price=PILOT, **changes):
    return {"id": uuid4(), "project_id": uuid4(), "owner_tg_id": owner, "request_key": uuid4(),
            "bot_id": 200002, "purpose": "entry", "price_kopecks": price, "currency": "RUB",
            "status": "created", "provider_payment_id": "invoice", "confirmation_url": "",
            "created_at": NOW, "paid_at": None, "launched_at": None, **changes}


class EntryPricingPolicyTests(unittest.TestCase):
    def test_default_and_enabled_price_affect_only_exact_owner(self):
        self.assertEqual((NORMAL, PILOT, OWNER), (999000, 1000, 6910097562))
        off, on = EntryPricingPolicy(), EntryPricingPolicy(True)
        for actor in (1, 42, OWNER - 1, OWNER, OWNER + 1, 2**63 - 1):
            self.assertEqual(off.price_for(actor), NORMAL)
            self.assertEqual(on.price_for(actor), PILOT if actor == OWNER else NORMAL)
            self.assertEqual(on.is_pilot_for(actor), actor == OWNER)
        for actor in (True, False, None, str(OWNER), float(OWNER)):
            self.assertFalse(on.is_pilot_for(actor))

    def test_frozen_policy_strict_flag_and_owner_validation(self):
        policy = EntryPricingPolicy()
        with self.assertRaises(FrozenInstanceError):
            policy.owner_pilot_enabled = True
        for flag in (1, 0, None, "true", [], {}):
            with self.assertRaises(EntryPricingError): EntryPricingPolicy(flag)
        for actor in (True, None, str(OWNER), 0, -1, 2**63):
            with self.assertRaises(EntryPricingError): policy.price_for(actor)

    def test_saved_order_validation_is_flag_independent_and_never_mutates_history(self):
        for enabled in (False, True):
            policy = EntryPricingPolicy(enabled)
            for owner, price in ((OWNER, PILOT), (OWNER, NORMAL), (42, NORMAL), (42, PILOT)):
                row = order_row(owner=owner, price=price)
                original = deepcopy(row)
                policy.validate_saved_order(row)
                self.assertEqual(row, original)

    def test_saved_order_rejects_invalid_owner_or_arbitrary_amount(self):
        for row in ({}, {"owner_tg_id": OWNER}, order_row(owner=True),
                    order_row(price=True), order_row(price=1000.0), order_row(price="1000"),
                    order_row(price=99900), order_row(price=0), order_row(price=-1)):
            for policy in (EntryPricingPolicy(), EntryPricingPolicy(True)):
                with self.assertRaises(EntryPricingError): policy.validate_saved_order(row)

    def test_new_invoice_requires_current_policy_without_repricing_saved_order(self):
        for enabled, owner, price, allowed in (
            (False, OWNER, PILOT, False), (True, OWNER, PILOT, True),
            (False, OWNER, NORMAL, True), (True, OWNER, NORMAL, False),
            (True, 42, NORMAL, True), (False, 42, NORMAL, True), (True, 42, PILOT, False),
        ):
            row = order_row(owner=owner, price=price)
            original = deepcopy(row)
            with self.subTest(enabled=enabled, owner=owner, price=price):
                if allowed: EntryPricingPolicy(enabled).validate_new_invoice(row)
                else:
                    with self.assertRaises(EntryPricingError): EntryPricingPolicy(enabled).validate_new_invoice(row)
                self.assertEqual(row, original)


class EnrollmentPricingContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_and_launch_lease_keep_identical_immutable_policy(self):
        policy = EntryPricingPolicy(True)
        conn = RecordingConnection(rows=[{"id": uuid4(), "owner_tg_id": OWNER, "bot_id": 200002, "status": "draft"}])
        store = PartnerEnrollmentStore(lambda: conn, entry_pricing=policy)
        self.assertIs(store.entry_pricing, policy)
        async with store.acquire_launch_lock(uuid4()) as locked:
            self.assertIs(locked.entry_pricing, policy)
        with self.assertRaises(TypeError): PartnerEnrollmentStore(lambda: conn, entry_pricing=True)

    async def test_new_order_price_is_computed_by_server_and_mismatch_does_not_insert(self):
        for owner, expected in ((OWNER, PILOT), (42, NORMAL)):
            project, key = uuid4(), uuid4()
            project_row = {"id": project, "owner_tg_id": owner, "status": "draft", "bot_id": 200002}
            def inserted(sql, args):
                self.assertEqual(args[-1], expected)
                return order_row(id=args[0], project_id=args[1], owner=owner, request_key=key, price=args[-1])
            conn = RecordingConnection(rows=[project_row, None, inserted])
            result = await PartnerEnrollmentStore(lambda: conn, entry_pricing=EntryPricingPolicy(True)).create_entry_order(project, owner, key, expected)
            self.assertEqual(result["price_kopecks"], expected)
        conn = RecordingConnection(rows=[project_row, None])
        with self.assertRaises(EnrollmentConflict):
            await PartnerEnrollmentStore(lambda: conn, entry_pricing=EntryPricingPolicy(True)).create_entry_order(project, owner, key, PILOT)
        self.assertFalse(any("INSERT" in sql for _, sql, _ in conn.calls))

    async def test_same_key_uses_saved_price_across_policy_switch_not_a_new_price(self):
        for saved_price, policy, expected, allowed in (
            (PILOT, EntryPricingPolicy(), PILOT, True), (PILOT, EntryPricingPolicy(), NORMAL, False),
            (NORMAL, EntryPricingPolicy(True), NORMAL, True), (NORMAL, EntryPricingPolicy(True), PILOT, False),
        ):
            row = order_row(price=saved_price)
            conn = RecordingConnection(rows=[{"id": row["project_id"]}, row])
            store = PartnerEnrollmentStore(lambda: conn, entry_pricing=policy)
            if allowed:
                result = await store.create_entry_order(row["project_id"], OWNER, row["request_key"], expected)
                self.assertEqual(result["price_kopecks"], saved_price)
            else:
                with self.assertRaises(EnrollmentConflict):
                    await store.create_entry_order(row["project_id"], OWNER, row["request_key"], expected)
            self.assertFalse(any(sql.lstrip().startswith(("INSERT", "UPDATE")) for _, sql, _ in conn.calls))

    async def test_verified_amount_matches_saved_snapshot_and_never_credits_partner_ledger(self):
        row = order_row()
        conn = RecordingConnection(rows=[row, {**row, "status": "paid_pending_launch", "paid_at": NOW}])
        result = await PartnerEnrollmentStore(lambda: conn).record_verified_payment(row["project_id"], row["id"], "invoice", PILOT, "RUB", now=NOW)
        self.assertEqual(result["price_kopecks"], PILOT)
        self.assertEqual(result["status"], "paid_pending_launch")
        self.assertTrue(any("INSERT INTO wl_launch_jobs" in sql for _, sql, _ in conn.calls))
        self.assertFalse(any("wl_revenue_ledger" in sql or "wl_subscriptions" in sql for _, sql, _ in conn.calls))
        for saved, amount in ((NORMAL, PILOT), (PILOT, NORMAL), (PILOT, 1)):
            row = order_row(price=saved)
            conn = RecordingConnection(rows=[row])
            with self.assertRaises(EnrollmentConflict):
                await PartnerEnrollmentStore(lambda: conn).record_verified_payment(row["project_id"], row["id"], "invoice", amount, "RUB")
            self.assertFalse(any(op == "execute" for op, _, _ in conn.calls))

    async def test_invalid_discounted_amount_is_denied_before_any_payment_effect(self):
        row = order_row(owner=42, price=999)
        conn = RecordingConnection(rows=[row])
        with self.assertRaises(EnrollmentConflict):
            await PartnerEnrollmentStore(lambda: conn, entry_pricing=EntryPricingPolicy(True)).record_verified_payment(row["project_id"], row["id"], "invoice", PILOT, "RUB")
        self.assertFalse(any(op == "execute" for op, _, _ in conn.calls))


class MemoryStore:
    def __init__(self, *, owner=OWNER, price=PILOT, policy=None):
        self.entry_pricing = policy if policy is not None else EntryPricingPolicy(True)
        self.row = order_row(owner=owner, price=price, provider_payment_id=None)
        self.row["id"], self.row["project_id"] = str(self.row["id"]), str(self.row["project_id"])
        self.args = self.row["project_id"], owner, self.row["id"]
        self.writes = []

    async def get_entry_order(self, *args):
        if args != self.args: raise LookupError("wrong owner/project/order")
        return dict(self.row)

    get_checkout_order = get_entry_order

    async def save_invoice(self, project, owner, order, payment, url):
        self.writes.append("invoice")
        self.row.update(provider_payment_id=payment, confirmation_url=url)
        return dict(self.row)

    async def record_verified_payment(self, project, order, payment, amount, currency, *, now):
        if payment != self.row["provider_payment_id"] or amount != self.row["price_kopecks"] or currency != "RUB":
            raise AssertionError("verified snapshot mismatch")
        self.writes.append("payment")
        self.row.update(status="paid_pending_launch", paid_at=now)
        return dict(self.row)


class Provider:
    def __init__(self, store):
        self.creates, self.gets = [], []
        row = store.row
        self.payment = PartnerPayment("pilot-invoice", row["id"], row["project_id"], row["owner_tg_id"],
                                      row["price_kopecks"], "RUB", "pending", False, "https://pay.example.test/pilot", purpose="entry")

    async def create_invoice(self, order, *, idempotence_key):
        self.creates.append((dict(order), idempotence_key))
        return self.payment

    async def get_payment(self, payment):
        self.gets.append(payment)
        return self.payment


class Launcher:
    ready = True

    def __init__(self, store): self.store, self.calls = store, []

    async def launch(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.store.row["status"] = "launched"
        return {"launch_state": "launched"}


class EntryPricingCheckoutTests(unittest.IsolatedAsyncioTestCase):
    def ports(self, **kwargs):
        store = MemoryStore(**kwargs)
        provider, launcher = Provider(store), Launcher(store)
        service = PartnerEntryCheckoutService(store, provider, launcher, clock=lambda: NOW)
        return store, provider, launcher, service

    async def test_policy_defaults_to_store_and_invoice_uses_exact_server_price_once(self):
        for owner, price in ((OWNER, PILOT), (42, NORMAL)):
            store, provider, _, service = self.ports(owner=owner, price=price)
            self.assertIs(service.entry_pricing, store.entry_pricing)
            await service.create_checkout(*store.args)
            await service.create_checkout(*store.args)
            self.assertEqual(len(provider.creates), 1)
            row, key = provider.creates[0]
            self.assertEqual((row["price_kopecks"], row["tg_id"], row["purpose"], key), (price, owner, "entry", store.row["id"]))

    async def test_disabled_pilot_blocks_unbound_invoice_but_preserves_saved_price(self):
        store, provider, _, service = self.ports(policy=EntryPricingPolicy())
        with self.assertRaises(CheckoutError): await service.create_checkout(*store.args)
        self.assertEqual(store.row["price_kopecks"], PILOT)
        self.assertEqual(provider.creates, [])
        self.assertEqual(store.writes, [])

    async def test_enabled_pilot_does_not_discount_an_old_unbound_normal_order(self):
        store, provider, _, service = self.ports(price=NORMAL)
        with self.assertRaises(CheckoutError): await service.create_checkout(*store.args)
        self.assertEqual(store.row["price_kopecks"], NORMAL)
        self.assertEqual(provider.creates, [])

    async def test_bound_pilot_invoice_reconciles_after_flag_disabled_and_launch_outage(self):
        store, provider, launcher, service = self.ports()
        await service.create_checkout(*store.args)
        store.entry_pricing = EntryPricingPolicy()
        launcher.ready = False
        restarted = PartnerEntryCheckoutService(store, provider, launcher, clock=lambda: NOW)
        provider.payment = replace(provider.payment, status="succeeded", paid=True)
        result = await restarted.reconcile(*store.args)
        self.assertEqual(result["order"]["price_kopecks"], PILOT)
        self.assertEqual(result["launch_state"], "disabled")
        self.assertEqual(store.writes, ["invoice", "payment"])
        self.assertEqual(len(provider.creates), 1)
        self.assertEqual(provider.gets, ["pilot-invoice"])

    async def test_bound_normal_invoice_reconciles_after_pilot_enabled_without_discount(self):
        store, provider, launcher, service = self.ports(price=NORMAL, policy=EntryPricingPolicy())
        await service.create_checkout(*store.args)
        store.entry_pricing = EntryPricingPolicy(True)
        restarted = PartnerEntryCheckoutService(store, provider, launcher, clock=lambda: NOW)
        provider.payment = replace(provider.payment, status="succeeded", paid=True)
        result = await restarted.reconcile(*store.args)
        self.assertEqual(result["order"]["price_kopecks"], NORMAL)
        self.assertEqual(result["launch_state"], "launched")
        self.assertEqual(len(provider.creates), 1)

    async def test_invalid_discounted_amount_stops_all_privileged_paths(self):
        store, provider, launcher, service = self.ports(owner=42, price=999)
        for method in (service.create_checkout, service.reconcile, service.launch):
            with self.assertRaises(CheckoutError): await method(*store.args)
        self.assertEqual((provider.creates, provider.gets, launcher.calls, store.writes), ([], [], [], []))

    async def test_provider_metadata_currency_and_amount_must_match_saved_pilot_exactly(self):
        store, provider, launcher, service = self.ports()
        original = provider.payment
        for changes in ({"amount_kopecks": NORMAL}, {"amount_kopecks": 1000.0}, {"tg_id": 42},
                        {"purpose": "subscription"}, {"currency": "USD"}, {"order_id": str(uuid4())}):
            provider.payment = replace(original, **changes)
            with self.assertRaises(PaymentMismatch): await service.create_checkout(*store.args)
        self.assertEqual(store.writes, [])
        self.assertEqual(launcher.calls, [])

    async def test_pilot_invoice_keeps_existing_23_hour_recovery_limit(self):
        store, provider, _, service = self.ports()
        service.clock = lambda: NOW + timedelta(hours=23)
        with self.assertRaises(InvoiceRecoveryRequired): await service.create_checkout(*store.args)
        self.assertEqual(provider.creates, [])


if __name__ == "__main__":
    unittest.main()
