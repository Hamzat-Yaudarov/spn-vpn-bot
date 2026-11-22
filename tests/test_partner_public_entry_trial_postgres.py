"""Actual isolated PG/WASM trial migration, money snapshots and schema proof."""
from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

import asyncpg

from services.partner_entry_pricing import EntryPricingPolicy
from services.partner_enrollment_store import EnrollmentConflict, PartnerEnrollmentStore
from services.partner_runtime import OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL, PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL
from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge

ROOT = Path(__file__).resolve().parents[1] / "ops/partner-platform"


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class PublicEntryTrialPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        await self.sql.query((ROOT / "commerce.sql").read_text(), exec=True)
        await self.sql.query((ROOT / "enrollment.sql").read_text(), exec=True)
        await self.sql.query((ROOT / "owner_entry_pilot.sql").read_text(), exec=True)
        self.migration = (ROOT / "public_entry_trial.sql").read_text()
        self.registry = PartnerStore(lambda: self.sql)

    async def migrate(self):
        await self.sql.query(self.migration, exec=True)

    async def project(self, owner):
        suffix = uuid4().hex[:12]
        item = await self.registry.create_project(owner, "trial-" + suffix, "trial-" + suffix, "Trial VPN")
        return await self.registry.save_bot_connection(item["id"], owner, item["version"],
            100000000 + int(suffix[:8], 16), "Trial" + suffix, "cipher:" + "x" * 100)

    def store(self, trial=True):
        return PartnerEnrollmentStore(lambda: self.sql, entry_pricing=EntryPricingPolicy(public_trial_enabled=trial))

    async def test_old_schema_rejects_public_guard_but_both_exact_guards_pass_after_migration(self):
        self.assertFalse((await self.sql.fetchrow(PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL))["price_constraint_safe"])
        self.assertTrue(all(dict(await self.sql.fetchrow(OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL)).values()))
        await self.migrate()
        for query in (PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL, OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL):
            self.assertTrue(all(dict(await self.sql.fetchrow(query)).values()))
        # Reapplying the explicit migration must neither rewrite nor weaken it.
        await self.migrate()
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 0)

    async def test_any_verified_owner_can_create_exact_fee_and_has_one_non_canceled_trial_slot(self):
        await self.migrate()
        for owner in (42, 6910097562):
            first, second = await self.project(owner), await self.project(owner)
            store = self.store()
            order = await store.create_entry_order(first["id"], owner, str(uuid4()), 1000)
            self.assertEqual(order["price_kopecks"], 1000)
            with self.assertRaises(EnrollmentConflict):
                await store.create_entry_order(second["id"], owner, str(uuid4()), 1000)
            self.assertIsNone(await store.get_current_entry_order(second["id"], owner))
            await self.sql.execute("UPDATE wl_entry_orders SET status='canceled' WHERE id=$1", order["id"])
            self.assertEqual((await store.create_entry_order(second["id"], owner, str(uuid4()), 1000))["price_kopecks"], 1000)

    async def test_saved_non_owner_invoice_settles_after_flagoff_without_ledger_or_vpn(self):
        await self.migrate()
        project = await self.project(42)
        store = self.store()
        order = await store.create_entry_order(project["id"], 42, str(uuid4()), 1000)
        await store.save_invoice(project["id"], 42, order["id"], "trial-verified", "https://pay.example.test/trial")
        off = self.store(False)
        paid = await off.record_verified_payment(project["id"], order["id"], "trial-verified", 1000, "RUB",
            now=datetime(2026, 10, 25, tzinfo=timezone.utc))
        self.assertEqual((paid["status"], paid["price_kopecks"]), ("paid_pending_launch", 1000))
        await off.record_verified_payment(project["id"], order["id"], "trial-verified", 1000, "RUB")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 1)
        for table in ("wl_revenue_ledger", "wl_subscriptions", "wl_orders", "wl_activation_jobs"):
            self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM " + table), 0)
        second = await self.project(42)
        new = await off.create_entry_order(second["id"], 42, str(uuid4()), 999000)
        self.assertEqual(new["price_kopecks"], 999000)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("UPDATE wl_entry_orders SET price_kopecks=999000 WHERE id=$1", order["id"])

    async def test_arbitrary_amounts_and_disabled_financial_guard_are_rejected(self):
        await self.migrate()
        project = await self.project(42)
        order = await self.store().create_entry_order(project["id"], 42, str(uuid4()), 1000)
        for field, value in (("price_kopecks", 999000), ("owner_tg_id", 43), ("bot_id", 123)):
            with self.subTest(field=field), self.assertRaises(asyncpg.CheckViolationError):
                await self.sql.execute("UPDATE wl_entry_orders SET " + field + "=$2 WHERE id=$1", order["id"], value)
        await self.sql.execute("ALTER TABLE wl_entry_orders DISABLE TRIGGER wl_entry_financial_snapshot_guard")
        self.assertFalse((await self.sql.fetchrow(PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL))["financial_snapshot_guard_safe"])

    async def test_migration_refuses_unexpected_predecessor_and_rolls_back(self):
        await self.sql.query("ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_orders_price_kopecks_check; "
            "ALTER TABLE wl_entry_orders ADD CONSTRAINT wl_entry_orders_price_kopecks_check CHECK(price_kopecks>0)", exec=True)
        with self.assertRaises(RuntimeError): await self.migrate()
        await self.sql.execute("ROLLBACK")
        self.assertFalse((await self.sql.fetchrow(PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL))["price_constraint_safe"])


if __name__ == "__main__": unittest.main()
