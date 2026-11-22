"""Isolated real PostgreSQL/WASM entitlement SQL; never working SPN data."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

import asyncpg

from test_partner_store_postgres import RegistryBridge
from services.partner_store import PartnerStore
from services.partner_commerce_store import (
    PartnerCommerceStore, CommerceConflict, CommerceNotFound, SalesUnavailable,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
BASE = 300 * 1024**3


async def quotes(sub, *, now):
    return {"traffic_status": "available", "traffic_packages": [
        {"id": "gb_10", "gb": 10, "price_kopecks": 2400}],
        "devices": {"addon_status": "available", "packages": [
            {"count": 1, "price_kopecks": 10000, "days": 30},
            {"count": 3, "price_kopecks": 27000, "days": 30}]}}


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class EntitlementPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        for name in ("commerce.sql", "entitlements.sql"):
            await self.sql.query((ROOT / "ops/partner-platform" / name).read_text(), exec=True)
        self.registry = PartnerStore(lambda: self.sql)
        self.store = PartnerCommerceStore(lambda: self.sql, quotes, entitlements_enabled=True)
        project = await self.registry.create_project(42, "entitlement-setup-1", "entitlement-vpn", "VPN")
        self.pid = project["id"]
        await self.registry.save_bot_connection(self.pid, 42, 1, 123456, "EntitlementBot", "cipher:" + "x" * 100)
        await self.sql.execute("UPDATE wl_projects SET status='active' WHERE id=$1", self.pid)

    async def subscription(self, tg=100, *, now=NOW):
        await self.store.register_customer(self.pid, tg)
        existing = await self.store.list_subscriptions(self.pid, tg)
        order = await self.store.create_order(self.pid, tg, str(uuid4()), 19900,
            subscription_id=existing[0]["id"] if existing else None)
        await self.pay(order, now=now)
        await self.complete(order)
        return (await self.store.list_subscriptions(self.pid, tg))[0]

    async def pay(self, order, *, now=NOW):
        payment = "payment-" + order["id"]
        await self.store.save_invoice(self.pid, order["tg_id"], order["id"], payment, "https://pay.example.test/" + payment)
        return await self.store.record_verified_payment(self.pid, order["id"], payment, order["price_kopecks"], "RUB", now=now)

    async def complete(self, order):
        await self.store.get_activation(self.pid, order["id"])
        return await self.store.complete_activation(self.pid, order["id"], remote_user_id="remote-1",
            subscription_url="https://sub.example.test/credential")

    async def addon(self, sub, purpose="traffic", package="gb_10", price=2400, key=None, now=NOW):
        return await self.store.create_addon_order(self.pid, sub["tg_id"], key or str(uuid4()), price,
            subscription_id=sub["id"], purpose=purpose, package_id=package, now=now)

    async def test_migration_reapply_constraints_and_disabled_capability(self):
        await self.sql.query((ROOT / "ops/partner-platform/entitlements.sql").read_text(), exec=True)
        sub = await self.subscription()
        order = await self.addon(sub)
        self.assertEqual(order["price_kopecks"], 2400)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("UPDATE wl_orders SET addon_quantity=NULL WHERE id=$1", order["id"])
        disabled = PartnerCommerceStore(lambda: self.sql, quotes)
        with self.assertRaises(SalesUnavailable):
            await disabled.get_addon_catalog(self.pid, 100, sub["id"], now=NOW)
        async with self.store.acquire_activation_lock(self.pid, sub["id"]) as lease:
            self.assertTrue(lease.entitlements_enabled)
            self.assertEqual((await lease.get_addon_catalog(self.pid, 100, sub["id"], now=NOW))["traffic_packages"][0]["price_kopecks"], 2400)

    async def test_traffic_payment_once_renewal_preserves_purchased_gb(self):
        sub = await self.subscription()
        order = await self.addon(sub)
        await self.pay(order)
        await self.pay(order)  # Canonical duplicate, not a second grant/ledger.
        activation = await self.store.get_activation(self.pid, order["id"])
        self.assertEqual((activation["traffic_limit_bytes"], activation["device_limit"]), (BASE+10*1024**3, 3))
        self.assertEqual(activation["expires_at"], NOW+timedelta(days=30))
        await self.complete(order)
        renewed = await self.subscription(now=NOW+timedelta(days=1))
        self.assertEqual(renewed["traffic_limit_bytes"], BASE+10*1024**3)
        self.assertEqual(renewed["expires_at"], NOW+timedelta(days=60))
        ledger = await self.sql.fetchrow("SELECT * FROM wl_revenue_ledger WHERE order_id=$1", order["id"])
        self.assertEqual((ledger["partner_kopecks"], ledger["platform_kopecks"]), (1680, 720))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger WHERE order_id=$1", order["id"]), 1)

    async def test_quote_price_changed_request_and_foreign_identity_rejected(self):
        sub = await self.subscription()
        key = str(uuid4())
        with self.assertRaises(CommerceConflict):
            await self.addon(sub, price=1)
        with self.assertRaises(CommerceConflict):
            await self.addon(sub, package="not-in-approved-catalogue")
        order = await self.addon(sub, key=key)
        self.assertEqual(await self.addon(sub, key=key), order)
        with self.assertRaises(CommerceConflict):
            await self.addon(sub, key=key, purpose="devices", package="1", price=10000)
        with self.assertRaises(CommerceNotFound):
            await self.store.create_addon_order(self.pid, 999, str(uuid4()), 2400,
                subscription_id=sub["id"], purpose="traffic", package_id="gb_10", now=NOW)
        await self.sql.execute("UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid)
        self.assertEqual(await self.addon(sub, key=key), order)
        with self.assertRaises(SalesUnavailable):
            await self.store.get_checkout_order(self.pid, 100, order["id"], now=NOW)

    async def test_device_grant_original_expiry_not_extended_by_renewal(self):
        sub = await self.subscription()
        order = await self.addon(sub, purpose="devices", package="3", price=27000)
        await self.pay(order)
        await self.complete(order)
        renewed = await self.subscription(now=NOW+timedelta(days=1))
        self.assertEqual(renewed["device_limit"], 6)
        rows = await self.store.queue_expired_device_activations(now=NOW+timedelta(days=31))
        self.assertEqual(len(rows), 1)
        job = await self.store.get_entitlement_activation(self.pid, rows[0]["job_id"])
        self.assertEqual((job["kind"], job["device_limit"], job["expires_at"]), ("device_expiry", 3, NOW+timedelta(days=60)))
        await self.store.complete_entitlement_activation(self.pid, job["job_id"], remote_user_id="remote-1", subscription_url="https://sub.example.test/credential")
        self.assertEqual(await self.store.queue_expired_device_activations(now=NOW+timedelta(days=32)), [])
        self.assertEqual((await self.store.list_subscriptions(self.pid, 100))[0]["device_limit"], 3)

    async def test_late_device_payment_retained_for_manual_review_no_free_grant(self):
        sub = await self.subscription()
        order = await self.addon(sub, purpose="devices", package="1", price=10000)
        await self.subscription(now=NOW+timedelta(days=1))  # Deadline now60, quoted device term still30.
        paid = await self.pay(order, now=NOW+timedelta(days=31))
        self.assertEqual(paid["activation_issue"], "addon_expired")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_device_grants"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger WHERE order_id=$1", order["id"]), 1)
        self.assertFalse(any(row["id"] == order["id"] for row in await self.store.list_recoverable_orders()))

    async def test_first_friend_subscription_only_bonus_bank_and_idempotent_application(self):
        await self.store.register_customer(self.pid, 100)
        await self.store.register_customer(self.pid, 200, 100)
        friend = await self.subscription(200)
        summary = await self.store.get_referral_summary(self.pid, 100)
        self.assertEqual((summary["invited_count"], summary["qualified_count"], summary["pending_days"]), (1, 1, 7))
        self.assertIsNone(await self.store.queue_referral_bonus_activation(self.pid, 100, now=NOW))
        self.assertEqual(await self.store.list_bonus_customers(), [])  # Ineligible banks cannot starve eligible work.
        addon = await self.addon(friend)
        await self.pay(addon)
        await self.complete(addon)
        await self.subscription(200)
        self.assertEqual((await self.store.get_referral_summary(self.pid, 100))["earned_days"], 7)
        parent = await self.subscription(100)
        self.assertEqual((await self.store.list_bonus_customers())[0]["tg_id"], 100)
        job = await self.store.queue_referral_bonus_activation(self.pid, 100, now=NOW)
        self.assertEqual((job["expires_at"], job["traffic_limit_bytes"], job["device_limit"]), (NOW+timedelta(days=37), BASE, 3))
        self.assertIsNone(await self.store.queue_referral_bonus_activation(self.pid, 100, now=NOW))
        readback = await self.store.get_entitlement_activation(self.pid, job["job_id"])
        self.assertEqual(readback["remote_user_id"], "remote-1")
        await self.store.mark_entitlement_activation_retry(self.pid, job["job_id"])
        self.assertEqual((await self.store.get_entitlement_activation(self.pid, job["job_id"]))["expires_at"], job["expires_at"])
        await self.store.complete_entitlement_activation(self.pid, job["job_id"], remote_user_id="remote-1", subscription_url="https://sub.example.test/credential")
        summary = await self.store.get_referral_summary(self.pid, 100)
        self.assertEqual((summary["applied_days"], summary["pending_days"]), (7, 0))

    async def test_shared_fifo_prevents_bonus_and_paid_purchase_overwriting_each_other(self):
        parent = await self.subscription()
        await self.store.register_customer(self.pid, 200, 100)
        await self.subscription(200)
        bonus = await self.store.queue_referral_bonus_activation(self.pid, 100, now=NOW)
        renewal = await self.store.create_order(self.pid, 100, str(uuid4()), 19900, subscription_id=parent["id"])
        await self.pay(renewal)
        with self.assertRaises(CommerceConflict):
            await self.store.get_activation(self.pid, renewal["id"])
        await self.store.complete_entitlement_activation(self.pid, bonus["job_id"], remote_user_id="remote-1", subscription_url="https://sub.example.test/credential")
        activation = await self.store.get_activation(self.pid, renewal["id"])
        self.assertEqual(activation["expires_at"], NOW+timedelta(days=67))
        await self.complete(renewal)
        self.assertEqual((await self.store.list_subscriptions(self.pid, 100))[0]["expires_at"], NOW+timedelta(days=67))

    async def test_payment_transaction_rolls_back_all_grants_ledger_and_bonus_on_outbox_failure(self):
        await self.store.register_customer(self.pid, 100)
        await self.store.register_customer(self.pid, 200, 100)
        order = await self.store.create_order(self.pid, 200, str(uuid4()), 19900)
        await self.store.save_invoice(self.pid, 200, order["id"], "payment-rollback", "https://pay.example.test/rollback")
        execute = self.sql.execute

        async def fail_outbox(sql, *args):
            if "INSERT INTO wl_customer_outbox" in sql:
                raise RuntimeError("local injected failure")
            return await execute(sql, *args)

        self.sql.execute = fail_outbox
        with self.assertRaises(RuntimeError):
            await self.store.record_verified_payment(self.pid, order["id"], "payment-rollback", 19900, "RUB", now=NOW)
        self.sql.execute = execute
        self.assertEqual((await self.store.get_order(self.pid, 200, order["id"]))["status"], "created")
        for table in ("wl_revenue_ledger", "wl_activation_jobs", "wl_referral_bonus_grants"):
            self.assertEqual(await self.sql.fetchval(f"SELECT count(*) FROM {table}"), 0)
        self.assertIsNone((await self.store.list_subscriptions(self.pid, 200))[0]["desired_expires_at"])
