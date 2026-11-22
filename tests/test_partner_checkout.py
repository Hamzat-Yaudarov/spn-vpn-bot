"""Checkout ports only. No .env, merchant, Telegram, working DB or VPN calls."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from services.partner_checkout import (
    BillingUnavailable,
    CheckoutError,
    InvoiceRecoveryRequired,
    PartnerCheckoutService,
    PartnerPayment,
    PaymentMismatch,
    ProvisionedSubscription,
)


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self):
        self.project, self.sub, self.order_id = str(uuid4()), str(uuid4()), str(uuid4())
        self.row = {
            "id": self.order_id,
            "project_id": self.project,
            "subscription_id": self.sub,
            "tg_id": 42,
            "price_kopecks": 19900,
            "status": "created",
            "created_at": NOW,
            "paid_at": None,
            "provider_payment_id": None,
            "confirmation_url": "",
        }
        self.ledger = 0
        self.deadline = None
        self.lock_count = 0
        self.retry_count = 0
        self.fail_bind = False
        self.fail_complete_once = False
        self.lock = asyncio.Lock()

    async def get_order(self, project, tg, order):
        if (project, tg, order) != (self.project, 42, self.order_id):
            raise LookupError("foreign")
        return dict(self.row)

    async def save_invoice(self, project, tg, order, payment_id, url):
        await self.get_order(project, tg, order)
        if self.fail_bind:
            self.fail_bind = False
            raise RuntimeError("DB outage with secret")
        self.row.update(provider_payment_id=payment_id, confirmation_url=url)
        return dict(self.row)

    async def record_verified_payment(
        self, project, order, payment_id, amount, currency, *, now
    ):
        assert self.row["provider_payment_id"] == payment_id
        assert amount == 19900 and currency == "RUB"
        if self.row["status"] == "created":
            self.row.update(status="paid_pending_activation", paid_at=now)
            self.deadline = now + timedelta(days=30)
            self.ledger += 1
        return dict(self.row)

    @asynccontextmanager
    async def acquire_activation_lock(self, project, sub):
        async with self.lock:
            self.lock_count += 1
            yield

    async def get_activation(self, project, order):
        return {
            "subscription_id": self.sub,
            "remote_username": "wl_" + self.sub.replace("-", ""),
            "expires_at": self.deadline,
            "traffic_limit_bytes": 300 * 1024**3,
            "device_limit": 3,
        }

    async def complete_activation(
        self, project, order, *, remote_user_id, subscription_url
    ):
        if self.fail_complete_once:
            self.fail_complete_once = False
            raise RuntimeError("crash after remote success")
        self.row["status"] = "active"
        return {"id": self.sub}  # Real store returns subscription, not order.

    async def mark_activation_retry(self, project, order):
        self.retry_count += 1

    async def cancel_verified_invoice(self, project, order, payment_id):
        assert payment_id == self.row["provider_payment_id"]
        if self.row["status"] == "created":
            self.row["status"] = "canceled"
        return dict(self.row)


class FakeProvider:
    def __init__(self, store):
        self.payment = PartnerPayment(
            "invoice-1",
            store.order_id,
            store.project,
            42,
            19900,
            "RUB",
            "pending",
            False,
            "https://pay.example.test/invoice",
        )
        self.created = []
        self.checked = []

    async def create_invoice(self, order, *, idempotence_key):
        self.created.append((idempotence_key, order["price_kopecks"]))
        return self.payment

    async def get_payment(self, invoice):
        self.checked.append(invoice)
        return self.payment


class FakeProvisioner:
    def __init__(self):
        self.applied = []
        self.fail_once = False

    async def ensure_subscription(self, activation):
        self.applied.append(dict(activation))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("private Remnawave URL/token")
        return ProvisionedSubscription(
            "remote-1",
            activation["remote_username"],
            "https://sub.example.test/private-subscription",
        )


class CheckoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.provider = FakeProvider(self.store)
        self.provisioner = FakeProvisioner()
        self.service = PartnerCheckoutService(
            self.store, self.provider, self.provisioner, clock=lambda: NOW
        )
        self.args = (self.store.project, 42, self.store.order_id)

    async def pay(self):
        await self.service.create_checkout(*self.args)
        self.provider.payment = replace(
            self.provider.payment, status="succeeded", paid=True
        )
        return await self.service.reconcile(*self.args)

    async def test_disabled_billing_does_not_create_remote_invoice(self):
        service = PartnerCheckoutService(self.store)
        with self.assertRaises(BillingUnavailable):
            await service.create_checkout(*self.args)
        self.assertEqual(self.provider.created, [])
        self.assertEqual(self.store.ledger, 0)

    async def test_cancelled_activation_keeps_paid_job_and_records_retry_before_propagating(
        self,
    ):
        await self.service.create_checkout(*self.args)
        self.provider.payment = replace(
            self.provider.payment, status="succeeded", paid=True
        )
        started = asyncio.Event()

        async def slow(activation):
            started.set()
            await asyncio.sleep(10)

        self.provisioner.ensure_subscription = slow
        task = asyncio.create_task(self.service.reconcile(*self.args))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            (self.store.row["status"], self.store.ledger, self.store.retry_count),
            ("paid_pending_activation", 1, 1),
        )

    async def test_pending_checkout_retry_same_saved_order(self):
        first = await self.service.create_checkout(*self.args)
        second = await self.service.create_checkout(*self.args)
        self.assertEqual(first, second)
        self.assertEqual(self.provider.created, [(self.store.order_id, 19900)])
        self.assertNotIn("provider_payment_id", first)
        self.assertNotIn("tg_id", first)
        self.assertEqual(self.store.ledger, 0)

    async def test_lost_binding_response_reuses_provider_idempotence_key(self):
        self.store.fail_bind = True
        with self.assertRaises(CheckoutError) as error:
            await self.service.create_checkout(*self.args)
        self.assertNotIn("secret", str(error.exception))
        await self.service.create_checkout(*self.args)
        self.assertEqual(
            [key for key, _ in self.provider.created], [self.store.order_id] * 2
        )

    async def test_old_unknown_invoice_not_recreated_after_idempotency_window(self):
        self.store.row["created_at"] = NOW - timedelta(hours=23)
        with self.assertRaises(InvoiceRecoveryRequired):
            await self.service.create_checkout(*self.args)
        self.assertEqual(self.provider.created, [])

    async def test_owned_order_checked_before_provider(self):
        for args in (
            (str(uuid4()), 42, self.store.order_id),
            (self.store.project, 99, self.store.order_id),
            (self.store.project, 42, str(uuid4())),
        ):
            with self.assertRaises(LookupError):
                await self.service.create_checkout(*args)
            with self.assertRaises(LookupError):
                await self.service.reconcile(*args)
        self.assertEqual(self.provider.created, [])
        self.assertEqual(self.provider.checked, [])

    async def test_payment_metadata_amount_currency_and_identity_must_match(self):
        original = self.provider.payment
        for changes in (
            {"order_id": str(uuid4())},
            {"project_id": str(uuid4())},
            {"tg_id": 99},
            {"amount_kopecks": 19899},
            {"amount_kopecks": 19900.0},
            {"currency": "USD"},
            {"paid": 1},
            {"confirmation_url": "https://user:pass@pay.example.test"},
        ):
            self.provider.payment = replace(original, **changes)
            with self.assertRaises(PaymentMismatch):
                await self.service.create_checkout(*self.args)
        self.assertEqual(self.store.ledger, 0)
        self.assertIsNone(self.store.row["provider_payment_id"])

    async def test_pending_waiting_capture_and_unpaid_succeeded_do_not_grant(self):
        await self.service.create_checkout(*self.args)
        for status, paid in (
            ("pending", False),
            ("waiting_for_capture", True),
            ("succeeded", False),
        ):
            self.provider.payment = replace(
                self.provider.payment, status=status, paid=paid
            )
            self.assertEqual(
                (await self.service.reconcile(*self.args))["status"], "created"
            )
        self.assertEqual(self.store.ledger, 0)
        self.assertEqual(self.provisioner.applied, [])

    async def test_canceled_payment_has_no_pay_link_or_grant(self):
        await self.service.create_checkout(*self.args)
        self.provider.payment = replace(
            self.provider.payment, status="canceled", paid=False
        )
        result = await self.service.reconcile(*self.args)
        self.assertEqual(result["payment_status"], "canceled")
        self.assertNotIn("confirmation_url", result)
        self.assertEqual(self.store.ledger, 0)

    async def test_canonical_lookup_not_create_response_confirms_payment(self):
        self.provider.payment = replace(
            self.provider.payment, status="succeeded", paid=True
        )
        result = await self.service.create_checkout(*self.args)
        self.assertEqual(result["status"], "active")
        self.assertEqual(self.provider.checked, ["invoice-1"])
        self.assertEqual(self.store.ledger, 1)

    async def test_paid_once_active_recheck_does_not_extend_or_reapply(self):
        first = await self.pay()
        deadline = self.store.deadline
        second = await self.service.reconcile(*self.args)
        self.assertEqual(first, second)
        self.assertEqual(self.store.ledger, 1)
        self.assertEqual(self.store.deadline, deadline)
        self.assertEqual(len(self.provisioner.applied), 1)

    async def test_remna_failure_keeps_paid_reservation_and_recovers_same_expiry(self):
        self.provisioner.fail_once = True
        result = await self.pay()
        self.assertEqual(result["status"], "paid_pending_activation")
        self.assertIn("Повторно платить не нужно", result["message"])
        self.assertNotIn("confirmation_url", result)
        deadline = self.store.deadline
        service = PartnerCheckoutService(
            self.store, None, self.provisioner, clock=lambda: NOW + timedelta(days=1)
        )
        self.assertEqual((await service.reconcile(*self.args))["status"], "active")
        self.assertEqual(self.store.deadline, deadline)
        self.assertEqual(self.store.ledger, 1)
        self.assertEqual(
            [row["expires_at"] for row in self.provisioner.applied],
            [deadline, deadline],
        )

    async def test_crash_after_remote_success_before_db_commit_is_safe(self):
        self.store.fail_complete_once = True
        result = await self.pay()
        self.assertEqual(result["status"], "paid_pending_activation")
        await self.service.reconcile(*self.args)
        self.assertEqual(self.store.ledger, 1)
        self.assertEqual(self.provisioner.applied[0], self.provisioner.applied[1])

    async def test_parallel_reconcile_rechecks_under_activation_lock(self):
        await self.service.create_checkout(*self.args)
        self.provider.payment = replace(
            self.provider.payment, status="succeeded", paid=True
        )
        results = await asyncio.gather(
            self.service.reconcile(*self.args), self.service.reconcile(*self.args)
        )
        self.assertTrue(all(row["status"] == "active" for row in results))
        self.assertEqual(self.store.ledger, 1)
        self.assertEqual(len(self.provisioner.applied), 1)

    async def test_missing_provisioner_does_not_fake_activation(self):
        self.service.provisioner = None
        result = await self.pay()
        self.assertEqual(result["status"], "paid_pending_activation")
        self.assertEqual(self.store.ledger, 1)
