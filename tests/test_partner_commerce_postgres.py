"""Optional isolated actual PG/WASM checkout SQL tests; no live config/DB/network.

Requires PGLITE_MODULE and optional PGLITE_NODE as the stage-one PG tests. A
single bridge cannot prove cross-process PostgreSQL connection race behavior.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

import asyncpg

from test_partner_store_postgres import RegistryBridge
from services.partner_store import PartnerStore
from services.partner_commerce_store import (
    PartnerCommerceStore, CommerceConflict, CommerceNotFound, InvalidCommerce,
    SalesUnavailable, TRAFFIC_LIMIT_BYTES,
)


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM module is not configured")
class PartnerCommercePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.schema = (Path(__file__).resolve().parents[1] / "ops/partner-platform/commerce.sql").read_text()
        await self.sql.query(self.schema, exec=True)
        self.registry = PartnerStore(lambda: self.sql)
        self.store = PartnerCommerceStore(lambda: self.sql)
        self.project = await self.registry.create_project(42, "create-test-123", "commerce-test", "Мой VPN")
        self.pid = self.project["id"]
        self.now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)

    async def activate_project(self):
        await self.registry.save_bot_connection(self.pid, 42, 1, 123456, "CommerceBot", "cipher:" + "x" * 100)
        await self.sql.execute("UPDATE wl_projects SET status='active' WHERE id=$1", self.pid)

    async def order(self, tg=100, *, price=19900, subscription_id=None, key=None):
        await self.store.register_customer(self.pid, tg)
        return await self.store.create_order(self.pid, tg, key or str(uuid4()), price, subscription_id=subscription_id)

    async def paid(self, order, *, now=None, payment=None):
        payment = payment or "p-" + order["id"]
        await self.store.save_invoice(self.pid, order["tg_id"], order["id"], payment, "https://pay.example.org/" + payment)
        return await self.store.record_verified_payment(self.pid, order["id"], payment, order["price_kopecks"], "RUB", now=now or self.now)

    async def test_schema_reapply_and_tenant_foreign_keys(self):
        await self.sql.query(self.schema, exec=True)
        await self.store.register_customer(self.pid, 100)
        foreign = await self.registry.create_project(99, "other-create-123", "other-commerce", "Другой VPN")
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.sql.execute("INSERT INTO wl_subscriptions(id,project_id,tg_id) VALUES($1,$2,100)", str(uuid4()), foreign["id"])
        with self.assertRaises(CommerceNotFound):
            await self.store.get_customer(foreign["id"], 100)
        for method in (self.store.list_orders, self.store.list_customers, self.store.overview):
            with self.assertRaises(CommerceNotFound):
                await method(self.pid, 99)

    async def test_customer_referrer_first_registration_only(self):
        await self.store.register_customer(self.pid, 100)
        with self.assertRaises(InvalidCommerce):
            await self.store.register_customer(self.pid, 200, 200)
        with self.assertRaises(InvalidCommerce):
            await self.store.register_customer(self.pid, 200, 999)
        child = await self.store.register_customer(self.pid, 200, 100)
        self.assertEqual(child["referrer_tg_id"], 100)
        await self.store.register_customer(self.pid, 300)
        self.assertEqual((await self.store.register_customer(self.pid, 200, 300))["referrer_tg_id"], 100)
        self.assertIsNone((await self.store.register_customer(self.pid, 300, 100))["referrer_tg_id"])

    async def test_sales_gate_fixed_price_and_persistent_idempotency(self):
        await self.store.register_customer(self.pid, 100)
        key = str(uuid4())
        with self.assertRaises(SalesUnavailable):
            await self.store.create_order(self.pid, 100, key, 19900)
        await self.activate_project()
        with self.assertRaises(CommerceConflict):
            await self.store.create_order(self.pid, 100, key, 29900)
        first = await self.store.create_order(self.pid, 100, key, 19900)
        await self.sql.execute("UPDATE wl_projects SET price_kopecks=29900,status='suspended' WHERE id=$1", self.pid)
        restarted = PartnerCommerceStore(lambda: self.sql)
        self.assertEqual(first, await restarted.create_order(self.pid, 100, key, 19900))
        with self.assertRaises(CommerceConflict):
            await restarted.create_order(self.pid, 100, key, 29900)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 1)
        sub = (await self.store.list_subscriptions(self.pid, 100))[0]
        self.assertIsNone(sub["expires_at"])
        self.assertIsNone(sub["desired_expires_at"])
        self.assertEqual(sub["traffic_limit_bytes"], TRAFFIC_LIMIT_BYTES)
        self.assertEqual(sub["device_limit"], 3)

    async def test_invoice_binding_and_owner_scope(self):
        await self.activate_project()
        order = await self.order()
        with self.assertRaises(CommerceNotFound):
            await self.store.get_order(self.pid, 999, order["id"])
        with self.assertRaises(CommerceConflict):
            await self.store.record_verified_payment(self.pid, order["id"], "unbound", 19900, "RUB", now=self.now)
        invoice = await self.store.save_invoice(self.pid, 100, order["id"], "provider-1", "")
        self.assertEqual(invoice["confirmation_url"], "")
        self.assertEqual(invoice, await self.store.save_invoice(self.pid, 100, order["id"], "provider-1", ""))
        with self.assertRaises(CommerceConflict):
            await self.store.save_invoice(self.pid, 100, order["id"], "provider-2", "https://pay.example.org")
        other = await self.order(200)
        with self.assertRaises(CommerceConflict):
            await self.store.save_invoice(self.pid, 200, other["id"], "provider-1", "")
        with self.assertRaises(CommerceConflict):
            await self.store.record_verified_payment(self.pid, order["id"], "provider-1", 29900, "RUB", now=self.now)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 0)

    async def test_paid_atomic_once_retry_activation_and_restart(self):
        await self.activate_project()
        order = await self.order()
        first = await self.paid(order)
        self.assertEqual(first["status"], "paid_pending_activation")
        self.assertEqual(first, await self.paid(order, now=self.now + timedelta(days=2)))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_activation_jobs"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_customer_outbox"), 1)
        ledger = await self.sql.fetchrow("SELECT * FROM wl_revenue_ledger")
        self.assertEqual((ledger["partner_kopecks"], ledger["platform_kopecks"]), (13930, 5970))
        job = await self.store.get_activation(self.pid, order["id"])
        self.assertEqual(job["expires_at"], self.now + timedelta(days=30))
        self.assertEqual(job["remote_username"], "wl_" + order["subscription_id"].replace("-", ""))
        await self.store.mark_activation_retry(self.pid, order["id"])
        sub = (await self.store.list_subscriptions(self.pid, 100))[0]
        self.assertIsNone(sub["expires_at"])
        self.store = PartnerCommerceStore(lambda: self.sql)
        self.assertEqual((await self.store.get_activation(self.pid, order["id"]))["expires_at"], job["expires_at"])
        done = await self.store.complete_activation(self.pid, order["id"], remote_user_id="remote-123", subscription_url="https://sub.example.org/private")
        self.assertEqual(done["expires_at"], job["expires_at"])
        again = await self.store.complete_activation(self.pid, order["id"], remote_user_id="remote-123", subscription_url="https://sub.example.org/private")
        self.assertEqual(done, again)
        self.assertEqual((await self.store.get_order(self.pid, 100, order["id"]))["status"], "active")

    async def test_subscription_renewal_order_and_absolute_deadlines(self):
        await self.activate_project()
        first = await self.order()
        with self.assertRaises(CommerceConflict):
            await self.order()
        with self.assertRaises(CommerceConflict):
            await self.order(subscription_id=first["subscription_id"])
        await self.paid(first)
        second = await self.order(subscription_id=first["subscription_id"])
        await self.paid(second, now=self.now + timedelta(days=1))
        with self.assertRaises(CommerceConflict):
            await self.store.get_activation(self.pid, second["id"])
        with self.assertRaises(CommerceConflict):
            await self.store.complete_activation(self.pid, second["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
        await self.store.complete_activation(self.pid, first["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
        job2 = await self.store.get_activation(self.pid, second["id"])
        self.assertEqual(job2["expires_at"], self.now + timedelta(days=60))
        await self.store.complete_activation(self.pid, second["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
        # Retrying the old successful job must never shorten a newer subscription.
        sub = await self.store.complete_activation(self.pid, first["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
        self.assertEqual(sub["expires_at"], job2["expires_at"])

    async def test_atomic_rollback_if_notification_insert_fails(self):
        await self.activate_project()
        order = await self.order()
        await self.store.save_invoice(self.pid, 100, order["id"], "p1", "")
        original_execute = self.sql.execute

        async def failing_execute(sql, *params):
            if "INSERT INTO wl_customer_outbox" in sql:
                raise RuntimeError("injected outbox failure")
            return await original_execute(sql, *params)
        self.sql.execute = failing_execute
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await self.store.record_verified_payment(self.pid, order["id"], "p1", 19900, "RUB", now=self.now)
        self.sql.execute = original_execute
        self.assertEqual((await self.store.get_order(self.pid, 100, order["id"]))["status"], "created")
        self.assertIsNone((await self.store.list_subscriptions(self.pid, 100))[0]["desired_expires_at"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_activation_jobs"), 0)
        await self.store.record_verified_payment(self.pid, order["id"], "p1", 19900, "RUB", now=self.now)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1)

    async def test_overview_actual_paid_money_pending_and_utc_periods(self):
        await self.activate_project()
        first = await self.order()
        await self.paid(first, now=self.now-timedelta(days=8))
        await self.store.complete_activation(self.pid, first["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
        second = await self.order(subscription_id=first["subscription_id"])
        await self.paid(second, now=self.now)
        await self.order(200)  # An unpaid order contributes no revenue.
        result = await self.store.overview(self.pid, 42, now=self.now)
        self.assertEqual(result["customers"], 2)
        self.assertEqual(result["active_subscriptions"], 1)
        self.assertEqual(result["pending_activations"], 1)
        self.assertEqual(result["timezone"], "UTC")
        self.assertEqual(result["periods"]["today"]["sales"], 1)
        self.assertEqual(result["periods"]["days7"]["revenue_kopecks"], 19900)
        self.assertEqual(result["periods"]["days30"]["revenue_kopecks"], 39800)
        self.assertNotIn("subscription_url", str(await self.store.list_customers(self.pid, 42)))

    async def test_lost_response_pending_recovery_and_earlier_created_paid_later(self):
        await self.activate_project()
        self.assertIsNone(await self.store.get_pending_order(self.pid, 100))
        first = await self.order()
        self.assertEqual((await self.store.get_pending_order(self.pid, 100))["id"], first["id"])
        self.assertIsNone(await self.store.get_pending_order(self.pid, 999))
        await self.paid(first)
        await self.store.complete_activation(self.pid, first["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
        self.assertIsNone(await self.store.get_pending_order(self.pid, 100))
        earlier_created = await self.order(subscription_id=first["subscription_id"])
        later_created = await self.order(subscription_id=first["subscription_id"])
        await self.paid(later_created)
        await self.paid(earlier_created)
        # Sequence is payment order, not invoice creation order.
        with self.assertRaises(CommerceConflict):
            await self.store.get_activation(self.pid, earlier_created["id"])
        await self.store.complete_activation(self.pid, later_created["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
        job = await self.store.get_activation(self.pid, earlier_created["id"])
        self.assertEqual(job["expires_at"], self.now+timedelta(days=90))
        done = await self.store.complete_activation(self.pid, earlier_created["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
        self.assertEqual(done["expires_at"], job["expires_at"])
        self.assertIsNone(await self.store.get_pending_order(self.pid, 100))

    async def test_suspended_after_invoice_keeps_verified_money_without_grant(self):
        await self.activate_project()
        first = await self.order()
        await self.store.save_invoice(self.pid, 100, first["id"], "frozen-invoice", "")
        await self.sql.execute("UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid)
        paid = await self.store.record_verified_payment(self.pid, first["id"], "frozen-invoice", 19900, "RUB", now=self.now)
        self.assertEqual(paid["status"], "paid_pending_activation")
        self.assertIsNone((await self.store.list_subscriptions(self.pid, 100))[0]["expires_at"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1)
        with self.assertRaises(SalesUnavailable):
            await self.store.get_activation(self.pid, first["id"])
        with self.assertRaises(SalesUnavailable):
            await self.order(subscription_id=first["subscription_id"])

    async def test_verified_cancellation_reopens_unpaid_slot_without_reusing_invoice(self):
        await self.activate_project()
        first = await self.order()
        await self.store.save_invoice(self.pid, 100, first["id"], "canceled-provider-1", "")
        with self.assertRaises(CommerceConflict):
            await self.store.cancel_verified_invoice(self.pid, first["id"], "wrong-provider")
        canceled = await self.store.cancel_verified_invoice(self.pid, first["id"], "canceled-provider-1")
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(canceled, await self.store.cancel_verified_invoice(self.pid, first["id"], "canceled-provider-1"))
        self.assertIsNone(await self.store.get_pending_order(self.pid, 100))
        with self.assertRaises(CommerceConflict):
            await self.store.record_verified_payment(self.pid, first["id"], "canceled-provider-1", 19900, "RUB", now=self.now)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 0)
        second = await self.order()
        self.assertEqual(second["subscription_id"], first["subscription_id"])
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_subscriptions"), 1)
        with self.assertRaises(CommerceConflict):
            await self.store.save_invoice(self.pid, 100, second["id"], "canceled-provider-1", "")
        with self.assertRaises(CommerceConflict):
            await self.order()  # The new unpaid order blocks another invoice.
        await self.paid(second, payment="new-provider-2")
        kept = await self.store.cancel_verified_invoice(self.pid, second["id"], "new-provider-2")
        self.assertEqual(kept["status"], "paid_pending_activation")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1)

    async def test_recovery_worklist_bounded_private_and_paid_first(self):
        await self.activate_project()
        unbound = await self.order(100)
        bound = await self.order(200)
        await self.store.save_invoice(self.pid, 200, bound["id"], "pending-bound", "https://pay.example.org/private")
        paid = await self.order(300)
        await self.paid(paid)
        done = await self.order(400)
        await self.paid(done)
        await self.store.complete_activation(self.pid, done["id"], remote_user_id="r", subscription_url="https://sub.example.org/private")
        canceled = await self.order(500)
        await self.store.save_invoice(self.pid, 500, canceled["id"], "canceled", "")
        await self.store.cancel_verified_invoice(self.pid, canceled["id"], "canceled")
        work = await self.store.list_recoverable_orders()
        self.assertEqual([r["id"] for r in work], [paid["id"], bound["id"]])
        self.assertEqual(set(work[0]), {"project_id", "tg_id", "id"})
        self.assertEqual(await self.store.list_recoverable_orders(limit=1), work[:1])
        self.assertEqual((await self.store.get_order(self.pid, 100, unbound["id"]))["status"], "created")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 2)

    async def test_activation_lease_uses_held_session_for_all_business_operations(self):
        await self.activate_project()
        first = await self.order()
        await self.paid(first)
        async with self.store.acquire_activation_lock(self.pid, first["subscription_id"]) as locked:
            self.assertEqual((await locked.get_order(self.pid, 100, first["id"]))["status"], "paid_pending_activation")
            job = await locked.get_activation(self.pid, first["id"])
            result = await locked.complete_activation(self.pid, first["id"], remote_user_id="r", subscription_url="https://sub.example.org/u")
            self.assertEqual(result["expires_at"], job["expires_at"])
        with self.assertRaises(RuntimeError):
            await locked.get_order(self.pid, 100, first["id"])
