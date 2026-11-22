"""Entry fee orchestration tests with explicit in-memory ports; no network/DB."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from services.partner_checkout import BillingUnavailable, CheckoutError, InvoiceRecoveryRequired, PartnerPayment, PaymentMismatch
from services.partner_entry_checkout import PartnerEntryCheckoutService, public_entry_order


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


class Store:
    def __init__(self):
        self.project, self.order = str(uuid4()), str(uuid4())
        self.row = {"id": self.order, "project_id": self.project, "owner_tg_id": 42,
                    "price_kopecks": 999000, "purpose": "entry", "status": "created",
                    "created_at": NOW, "paid_at": None, "provider_payment_id": None,
                    "confirmation_url": "", "needs_webhook_replace": False}
        self.paid_count = 0
        self.fail_bind_once = False

    async def get_entry_order(self, project, owner, order):
        if (project, owner, order) != (self.project, 42, self.order):
            raise LookupError("foreign entry")
        return dict(self.row)

    async def get_checkout_order(self, project, owner, order):
        return await self.get_entry_order(project, owner, order)

    async def save_invoice(self, project, owner, order, invoice, url):
        await self.get_entry_order(project, owner, order)
        if self.fail_bind_once:
            self.fail_bind_once = False
            raise RuntimeError("private SQL secret")
        self.row.update(provider_payment_id=invoice, confirmation_url=url)
        return dict(self.row)

    async def record_verified_payment(self, project, order, invoice, amount, currency, *, now):
        assert invoice == self.row["provider_payment_id"] and amount == 999000 and currency == "RUB"
        if self.row["status"] == "created":
            self.paid_count += 1
            self.row.update(status="paid_pending_launch", paid_at=now)
        return dict(self.row)

    async def cancel_verified_invoice(self, project, order, invoice):
        assert invoice == self.row["provider_payment_id"]
        if self.row["status"] == "created":
            self.row["status"] = "canceled"
        return dict(self.row)


class Provider:
    def __init__(self, store):
        self.payment = PartnerPayment("entry-invoice", store.order, store.project, 42, 999000,
            "RUB", "pending", False, "https://pay.example.test/entry", purpose="entry")
        self.creates, self.gets = [], []

    async def create_invoice(self, order, *, idempotence_key):
        self.creates.append((dict(order), idempotence_key))
        return self.payment

    async def get_payment(self, payment_id):
        self.gets.append(payment_id)
        return self.payment


class Launcher:
    ready = True

    def __init__(self, store):
        self.store = store
        self.calls = []
        self.state = "launched"

    async def launch(self, project, owner, order, *, replace_existing=False):
        self.calls.append((project, owner, order, replace_existing))
        if self.state == "launched":
            self.store.row["status"] = "launched"
        return {"launch_state": self.state}


class EntryCheckoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store()
        self.provider, self.launcher = Provider(self.store), Launcher(self.store)
        self.service = PartnerEntryCheckoutService(self.store, self.provider, self.launcher, clock=lambda: NOW)
        self.args = self.store.project, 42, self.store.order

    async def pay(self):
        await self.service.create_checkout(*self.args)
        self.provider.payment = replace(self.provider.payment, status="succeeded", paid=True)
        return await self.service.reconcile(*self.args)

    async def test_no_new_entry_fee_without_launch_capability_or_provider(self):
        self.launcher.ready = False
        self.assertFalse(self.service.payments_available)
        with self.assertRaises(BillingUnavailable):
            await self.service.create_checkout(*self.args)
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.store.paid_count, 0)
        self.assertFalse(PartnerEntryCheckoutService(self.store).payments_available)

    async def test_exact_entry_kind_price_and_saved_key(self):
        first = await self.service.create_checkout(*self.args)
        second = await self.service.create_checkout(*self.args)
        self.assertEqual(first, second)
        self.assertEqual(len(self.provider.creates), 1)
        order, key = self.provider.creates[0]
        self.assertEqual((order["purpose"], order["price_kopecks"], order["tg_id"], key),
                         ("entry", 999000, 42, self.store.order))
        self.assertNotIn("owner_tg_id", first["order"])
        self.assertNotIn("provider_payment_id", first["order"])

    async def test_wrong_owner_or_project_stops_before_privileged_ports(self):
        for args in ((str(uuid4()), 42, self.store.order), (self.store.project, 99, self.store.order),
                     (self.store.project, 42, str(uuid4()))):
            for method in (self.service.create_checkout, self.service.reconcile, self.service.launch):
                with self.assertRaises(LookupError):
                    await method(*args)
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.provider.gets, [])
        self.assertEqual(self.launcher.calls, [])

    async def test_unpaid_entry_cannot_launch_and_waiting_capture_does_not_count(self):
        with self.assertRaises(CheckoutError):
            await self.service.launch(*self.args)
        await self.service.create_checkout(*self.args)
        for status, paid in (("pending", False), ("waiting_for_capture", True), ("succeeded", False)):
            self.provider.payment = replace(self.provider.payment, status=status, paid=paid)
            self.assertEqual((await self.service.reconcile(*self.args))["order"]["status"], "created")
        self.assertEqual(self.launcher.calls, [])
        self.assertEqual(self.store.paid_count, 0)

    async def test_subscription_payment_wrong_fee_currency_metadata_or_kind_cannot_pay_entry(self):
        original = self.provider.payment
        for fields in ({"purpose": "subscription"}, {"amount_kopecks": 99900}, {"amount_kopecks": 999000.0},
                       {"currency": "USD"}, {"project_id": str(uuid4())}, {"tg_id": 99},
                       {"order_id": str(uuid4())}, {"paid": 1}):
            self.provider.payment = replace(original, **fields)
            with self.assertRaises(PaymentMismatch):
                await self.service.create_checkout(*self.args)
        self.assertEqual(self.store.paid_count, 0)
        self.assertEqual(self.launcher.calls, [])

    async def test_completed_create_response_still_uses_canonical_lookup(self):
        self.provider.payment = replace(self.provider.payment, status="succeeded", paid=True)
        self.assertEqual((await self.service.create_checkout(*self.args))["order"]["status"], "launched")
        self.assertEqual(self.provider.gets, ["entry-invoice"])
        self.assertEqual(self.store.paid_count, 1)

    async def test_paid_launch_retry_after_restart_does_not_charge_or_credit_again(self):
        self.launcher.state = "retry"
        result = await self.pay()
        self.assertEqual(result["order"]["status"], "paid_pending_launch")
        self.assertNotIn("confirmation_url", result["order"])
        restarted = PartnerEntryCheckoutService(self.store, None, self.launcher)
        self.launcher.state = "launched"
        self.assertEqual((await restarted.reconcile(*self.args))["order"]["status"], "launched")
        await restarted.reconcile(*self.args)
        self.assertEqual(self.store.paid_count, 1)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.launcher.calls), 2)

    async def test_paid_invoice_during_runtime_outage_is_retained_without_second_fee(self):
        await self.service.create_checkout(*self.args)
        self.provider.payment = replace(self.provider.payment, status="succeeded", paid=True)
        self.launcher.ready = False
        result = await self.service.reconcile(*self.args)
        self.assertEqual(result["order"]["status"], "paid_pending_launch")
        self.assertEqual(result["launch_state"], "disabled")
        self.assertEqual(self.store.paid_count, 1)
        self.assertEqual(self.launcher.calls, [])

    async def test_existing_webhook_replacement_requires_explicit_bool_and_no_new_checkout(self):
        self.launcher.state = "needs_webhook_replace"
        self.assertEqual((await self.pay())["launch_state"], "needs_webhook_replace")
        with self.assertRaises(ValueError):
            await self.service.launch(*self.args, replace_existing="true")
        self.launcher.state = "launched"
        await self.service.launch(*self.args, replace_existing=True)
        self.assertTrue(self.launcher.calls[-1][-1])
        self.assertEqual(len(self.provider.creates), 1)

    async def test_ambiguous_invoice_creation_reuses_same_key_then_stops_after_23h(self):
        self.store.fail_bind_once = True
        with self.assertRaises(CheckoutError) as error:
            await self.service.create_checkout(*self.args)
        self.assertNotIn("secret", str(error.exception))
        self.service.clock = lambda: NOW + timedelta(hours=23)
        with self.assertRaises(InvoiceRecoveryRequired):
            await self.service.create_checkout(*self.args)
        self.assertEqual(len(self.provider.creates), 1)
        self.service.clock = lambda: NOW
        await self.service.create_checkout(*self.args)
        self.assertEqual([key for _, key in self.provider.creates], [self.store.order] * 2)

    async def test_canonical_cancel_does_not_launch_or_count_entry(self):
        await self.service.create_checkout(*self.args)
        self.provider.payment = replace(self.provider.payment, status="canceled", paid=False)
        result = await self.service.reconcile(*self.args)
        self.assertEqual(result["order"]["status"], "canceled")
        self.assertNotIn("confirmation_url", result["order"])
        self.assertEqual(self.store.paid_count, 0)
        self.assertEqual(self.launcher.calls, [])

    def test_public_entry_projection_excludes_every_private_field(self):
        result = public_entry_order({**self.store.row, "encrypted_token": "secret", "secret_fingerprint": "hash",
                                     "webhook_url": "https://secret.example", "subscription_id": "not-entry"})
        self.assertEqual(set(result), {"id", "price_kopecks", "status", "created_at", "paid_at", "needs_webhook_replace", "creation_available"})


if __name__ == "__main__":
    unittest.main()
