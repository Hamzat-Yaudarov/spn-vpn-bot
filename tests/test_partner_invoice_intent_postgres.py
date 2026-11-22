"""Explicit intent/cancel/crash phase interleavings on isolated real SQL/WASM.

No real merchant, Telegram or VPN access. The single connection cannot prove
independent native PG row-lock races; these tests cover saved-state boundaries.
"""

from dataclasses import replace
from datetime import timedelta
import os
import unittest
from uuid import uuid4

import test_partner_entitlements_postgres as fixtures
from services.partner_checkout import (
    CheckoutError,
    InvoiceRecoveryRequired,
    PartnerCheckoutService,
    PartnerPayment,
    recover_partner_orders_once,
)
from services.partner_commerce_store import (
    CommerceConflict,
    CommerceNotFound,
    PartnerCommerceStore,
    SalesUnavailable,
)


ROOT, NOW, quotes = fixtures.ROOT, fixtures.NOW, fixtures.quotes


class Merchant:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.lost_response = False
        self.hook = None

    async def create_invoice(self, order, *, idempotence_key):
        self.calls.append((idempotence_key, dict(order)))
        payment = self.rows.setdefault(
            order["id"],
            PartnerPayment(
                "payment-" + order["id"],
                order["id"],
                order["project_id"],
                order["tg_id"],
                order["price_kopecks"],
                "RUB",
                "pending",
                False,
                "https://pay.example.test/" + order["id"],
                purpose=order.get("purpose", "subscription"),
            ),
        )
        if self.hook:
            await self.hook()
        if self.lost_response:
            self.lost_response = False
            raise TimeoutError("simulated accepted invoice response lost")
        return payment

    async def get_payment(self, payment_id):
        return next(row for row in self.rows.values() if row.id == payment_id)


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class InvoiceIntentPostgresTests(unittest.IsolatedAsyncioTestCase):
    subscription = fixtures.EntitlementPostgresTests.subscription
    pay = fixtures.EntitlementPostgresTests.pay
    complete = fixtures.EntitlementPostgresTests.complete

    async def asyncSetUp(self):
        await fixtures.EntitlementPostgresTests.asyncSetUp(self)
        for name in ("traffic.sql", "subscriptions.sql", "operations.sql"):
            await self.sql.query(
                (ROOT / "ops/partner-platform" / name).read_text(), exec=True
            )
        self.store = self.restarted()
        self.merchant = Merchant()
        self.now = NOW
        self.checkout = PartnerCheckoutService(
            self.store, self.merchant, clock=lambda: self.now
        )

    def restarted(self):
        return PartnerCommerceStore(
            lambda: self.sql,
            quotes,
            entitlements_enabled=True,
            traffic_cycles_enabled=True,
            invoice_intents_enabled=True,
        )

    async def order(self, tg=100):
        await self.store.register_customer(self.pid, tg)
        sub = await self.store.list_subscriptions(self.pid, tg)
        return await self.store.create_order(
            self.pid,
            tg,
            str(uuid4()),
            19900,
            subscription_id=(
                sub[0]["id"] if sub and sub[0]["expires_at"] is not None else None
            ),
        )

    async def test_explicit_flags_and_repeated_migration_no_timestamp_rewrite(self):
        with self.assertRaises(TypeError):
            PartnerCommerceStore(lambda: self.sql, invoice_intents_enabled=True)
        disabled = PartnerCommerceStore(lambda: self.sql)
        order = await self.order()
        with self.assertRaises(SalesUnavailable):
            await disabled.begin_invoice_creation(self.pid, 100, order["id"], now=NOW)
        first = await self.store.begin_invoice_creation(
            self.pid, 100, order["id"], now=NOW
        )
        await self.sql.query(
            (ROOT / "ops/partner-platform/operations.sql").read_text(), exec=True
        )
        again = await self.restarted().begin_invoice_creation(
            self.pid, 100, order["id"], now=NOW + timedelta(hours=1)
        )
        self.assertEqual(first["invoice_requested_at"], NOW)
        self.assertEqual(again["invoice_requested_at"], NOW)

    async def test_intent_is_committed_before_provider_and_cancel_is_blocked(self):
        order = await self.order()

        async def hook():
            saved = await self.store.get_order(self.pid, 100, order["id"])
            self.assertEqual(saved["invoice_requested_at"], NOW)
            self.assertIsNone(saved["provider_payment_id"])
            with self.assertRaises(CommerceConflict):
                await self.restarted().cancel_uninvoiced_order(
                    self.pid, 100, order["id"]
                )

        self.merchant.hook = hook
        result = await self.checkout.create_checkout(self.pid, 100, order["id"])
        self.assertEqual(result["status"], "created")
        self.assertIn("confirmation_url", result)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 0
        )

    async def test_cancel_before_first_intent_and_canceled_between_preflight_and_intent_no_provider(
        self,
    ):
        order = await self.order()
        original = self.store.get_checkout_order

        async def preflight(*args, **kwargs):
            result = await original(*args, **kwargs)
            await self.restarted().cancel_uninvoiced_order(self.pid, 100, order["id"])
            return result

        self.store.get_checkout_order = preflight
        result = await self.checkout.create_checkout(self.pid, 100, order["id"])
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(self.merchant.calls, [])
        canceled = await self.store.cancel_uninvoiced_order(self.pid, 100, order["id"])
        self.assertIsNone(canceled["invoice_requested_at"])
        new = await self.order()
        self.assertNotEqual(new["id"], order["id"])

    async def test_lost_create_response_restart_reuses_original_key_parameters_and_one_commission(
        self,
    ):
        order = await self.order()
        self.merchant.lost_response = True
        with self.assertRaises(CheckoutError):
            await self.checkout.create_checkout(self.pid, 100, order["id"])
        saved = await self.store.get_order(self.pid, 100, order["id"])
        self.assertEqual(saved["invoice_requested_at"], NOW)
        self.assertIsNone(saved["provider_payment_id"])
        with self.assertRaises(CommerceConflict):
            await self.store.cancel_uninvoiced_order(self.pid, 100, order["id"])
        await self.sql.execute(
            "UPDATE wl_projects SET price_kopecks=29900,status='suspended' WHERE id=$1",
            self.pid,
        )
        self.store = self.restarted()
        self.checkout = PartnerCheckoutService(
            self.store, self.merchant, clock=lambda: NOW + timedelta(minutes=1)
        )
        result = await recover_partner_orders_once(self.store, self.checkout)
        self.assertEqual((result["checked"], result["errors"]), (1, 0))
        self.assertEqual(
            [row[0] for row in self.merchant.calls], [order["id"], order["id"]]
        )
        self.assertEqual(
            [row[1]["price_kopecks"] for row in self.merchant.calls], [19900, 19900]
        )
        self.merchant.rows[order["id"]] = replace(
            self.merchant.rows[order["id"]], paid=True, status="succeeded"
        )
        await self.checkout.reconcile(self.pid, 100, order["id"])
        await self.checkout.reconcile(self.pid, 100, order["id"])
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1
        )
        self.assertEqual(
            (await self.store.get_order(self.pid, 100, order["id"]))["status"],
            "paid_pending_activation",
        )

    async def test_crash_after_intent_before_provider_recovered_within_window_only(
        self,
    ):
        order = await self.order()
        await self.store.begin_invoice_creation(self.pid, 100, order["id"], now=NOW)
        self.now = NOW + timedelta(hours=22, minutes=59)
        result = await recover_partner_orders_once(self.store, self.checkout)
        self.assertEqual((result["checked"], result["errors"]), (1, 0))
        self.assertEqual(len(self.merchant.calls), 1)
        self.assertEqual(self.merchant.calls[0][0], order["id"])

    async def test_stale_unbound_intent_never_recreated_or_released_automatically(self):
        order = await self.order()
        await self.store.begin_invoice_creation(self.pid, 100, order["id"], now=NOW)
        self.now = NOW + timedelta(hours=23)
        with self.assertRaises(InvoiceRecoveryRequired):
            await self.checkout.create_checkout(self.pid, 100, order["id"])
        self.assertEqual(
            (await recover_partner_orders_once(self.store, self.checkout))["checked"], 0
        )
        with self.assertRaises(CommerceConflict):
            await self.store.cancel_uninvoiced_order(self.pid, 100, order["id"])
        self.assertEqual(self.merchant.calls, [])
        self.assertEqual(
            (await self.store.get_order(self.pid, 100, order["id"]))[
                "invoice_requested_at"
            ],
            NOW,
        )

    async def test_old_unattempted_draft_has_fresh_first_attempt_window(self):
        order = await self.order()
        await self.sql.execute(
            "UPDATE wl_orders SET created_at=$2 WHERE id=$1",
            order["id"],
            NOW - timedelta(days=10),
        )
        await self.checkout.create_checkout(self.pid, 100, order["id"])
        self.assertEqual(len(self.merchant.calls), 1)
        self.assertEqual(
            (await self.store.get_order(self.pid, 100, order["id"]))[
                "invoice_requested_at"
            ],
            NOW,
        )

    async def test_tenant_and_customer_scope_before_provider_or_cancel(self):
        order = await self.order()
        for operation in (
            self.checkout.create_checkout(self.pid, 999, order["id"]),
            self.store.cancel_uninvoiced_order(str(uuid4()), 100, order["id"]),
            self.store.begin_invoice_creation(self.pid, 999, order["id"], now=NOW),
        ):
            with self.assertRaises(CommerceNotFound):
                await operation
        self.assertEqual(self.merchant.calls, [])

    async def test_new_addon_intent_rechecks_expiry_under_lock_after_preflight(self):
        sub = await self.subscription()
        order = await self.store.create_addon_order(
            self.pid,
            100,
            str(uuid4()),
            10000,
            subscription_id=sub["id"],
            purpose="devices",
            package_id="1",
            now=NOW,
        )
        await self.store.get_checkout_order(self.pid, 100, order["id"], now=NOW)
        with self.assertRaises(CommerceConflict):
            await self.store.begin_invoice_creation(
                self.pid, 100, order["id"], now=NOW + timedelta(days=31)
            )
        self.assertIsNone(
            (await self.store.get_order(self.pid, 100, order["id"]))[
                "invoice_requested_at"
            ]
        )
