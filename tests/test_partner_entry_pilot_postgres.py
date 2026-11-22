"""Actual isolated PG/WASM owner-entry pilot constraints; never a live DB.

Uses the existing optional PGLITE_MODULE/PGLITE_NODE test bridge. One in-memory
connection tests SQL/transaction behavior, not independent connection races.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

import asyncpg

from services.partner_entry_pricing import EntryPricingPolicy
from services.partner_enrollment_store import EnrollmentConflict, PartnerEnrollmentStore
from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge


PILOT_OWNER = 6910097562
ROOT = Path(__file__).resolve().parents[1] / "ops/partner-platform"


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class EntryPilotPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        await self.sql.query((ROOT / "enrollment.sql").read_text(), exec=True)
        self.migration = (ROOT / "owner_entry_pilot.sql").read_text()
        self.registry = PartnerStore(lambda: self.sql)
        self.project = await self.new_project(PILOT_OWNER)

    async def new_project(self, owner):
        suffix = uuid4().hex[:12]
        return await self.registry.create_project(
            owner, "pilot-" + suffix, "pilot-" + suffix, "Изолированный тест"
        )

    async def migrate(self):
        await self.sql.query(self.migration, exec=True)

    async def insert_order(self, *, project=None, owner=PILOT_OWNER, price=1000):
        project = project or self.project
        order_id = str(uuid4())
        await self.sql.execute(
            """INSERT INTO wl_entry_orders
               (id,project_id,owner_tg_id,request_key,bot_id,price_kopecks)
               VALUES($1,$2,$3,$4,123456,$5)""",
            order_id, project["id"], owner, str(uuid4()), price,
        )
        return await self.sql.fetchrow("SELECT * FROM wl_entry_orders WHERE id=$1", order_id)

    async def failed_migration(self):
        with self.assertRaises(RuntimeError):
            await self.migrate()
        await self.sql.execute("ROLLBACK")

    def enrollment_store(self, *, pilot=False):
        return PartnerEnrollmentStore(
            lambda: self.sql, entry_pricing=EntryPricingPolicy(owner_pilot_enabled=pilot)
        )

    async def connected_project(self, *, owner=PILOT_OWNER, project=None):
        project = project or await self.new_project(owner)
        suffix = uuid4().hex[:10]
        return await self.registry.save_bot_connection(
            project["id"], owner, project["version"],
            100000000 + int(suffix[:8], 16), "Pilot" + suffix, "cipher:" + "x" * 100,
        )

    async def assert_no_subscription_money(self):
        for table in (
            "wl_orders", "wl_customers", "wl_subscriptions",
            "wl_revenue_ledger", "wl_activation_jobs",
        ):
            with self.subTest(table=table):
                self.assertEqual(await self.sql.fetchval(f"SELECT count(*) FROM {table}"), 0)

    async def test_store_enabled_pilot_owner_gets_1000_and_other_owners_get_999000(self):
        await self.migrate()
        pilot_project = await self.connected_project(project=self.project)
        normal_project = await self.connected_project(owner=42)
        store = self.enrollment_store(pilot=True)
        pilot_order = await store.create_entry_order(
            pilot_project["id"], PILOT_OWNER, str(uuid4()), 1000
        )
        normal_order = await store.create_entry_order(
            normal_project["id"], 42, str(uuid4()), 999000
        )
        self.assertEqual((pilot_order["owner_tg_id"], pilot_order["price_kopecks"]), (PILOT_OWNER, 1000))
        self.assertEqual((normal_order["owner_tg_id"], normal_order["price_kopecks"]), (42, 999000))
        self.assertEqual(pilot_order["purpose"], "entry")
        with self.assertRaises(EnrollmentConflict):
            await store.create_entry_order(normal_project["id"], 42, str(uuid4()), 1000)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 2)

    async def test_store_flag_off_restores_new_normal_orders_without_repricing_old_pilot(self):
        await self.migrate()
        first = await self.connected_project(project=self.project)
        pilot_order = await self.enrollment_store(pilot=True).create_entry_order(
            first["id"], PILOT_OWNER, str(uuid4()), 1000
        )
        second = await self.connected_project()
        normal_order = await self.enrollment_store().create_entry_order(
            second["id"], PILOT_OWNER, str(uuid4()), 999000
        )
        self.assertEqual(normal_order["price_kopecks"], 999000)
        self.assertEqual(await self.sql.fetchval(
            "SELECT price_kopecks FROM wl_entry_orders WHERE id=$1", pilot_order["id"]
        ), 1000)
        third = await self.connected_project()
        with self.assertRaises(EnrollmentConflict):
            await self.enrollment_store().create_entry_order(third["id"], PILOT_OWNER, str(uuid4()), 1000)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 2)

    async def test_store_pilot_saved_key_retries_off_only_with_its_immutable_1000(self):
        await self.migrate()
        project = await self.connected_project(project=self.project)
        key = str(uuid4())
        pilot = await self.enrollment_store(pilot=True).create_entry_order(project["id"], PILOT_OWNER, key, 1000)
        off = self.enrollment_store()
        self.assertEqual(await off.create_entry_order(project["id"], PILOT_OWNER, key, 1000), pilot)
        with self.assertRaises(EnrollmentConflict):
            await off.create_entry_order(project["id"], PILOT_OWNER, key, 999000)
        with self.assertRaises(EnrollmentConflict):
            await off.get_checkout_order(project["id"], PILOT_OWNER, pilot["id"])
        self.assertEqual(await off.get_entry_order(project["id"], PILOT_OWNER, pilot["id"]), pilot)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 1)

    async def test_store_normal_saved_key_retries_enabled_only_with_its_immutable_999000(self):
        await self.migrate()
        project = await self.connected_project(project=self.project)
        key = str(uuid4())
        normal = await self.enrollment_store().create_entry_order(project["id"], PILOT_OWNER, key, 999000)
        enabled = self.enrollment_store(pilot=True)
        self.assertEqual(await enabled.create_entry_order(project["id"], PILOT_OWNER, key, 999000), normal)
        with self.assertRaises(EnrollmentConflict):
            await enabled.create_entry_order(project["id"], PILOT_OWNER, key, 1000)
        with self.assertRaises(EnrollmentConflict):
            await enabled.get_checkout_order(project["id"], PILOT_OWNER, normal["id"])
        self.assertEqual(await enabled.get_entry_order(project["id"], PILOT_OWNER, normal["id"]), normal)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 1)

    async def test_store_unique_pilot_slot_rolls_back_second_project_attempt(self):
        await self.migrate()
        first = await self.connected_project(project=self.project)
        second = await self.connected_project()
        store = self.enrollment_store(pilot=True)
        await store.create_entry_order(first["id"], PILOT_OWNER, str(uuid4()), 1000)
        with self.assertRaises(EnrollmentConflict):
            await store.create_entry_order(second["id"], PILOT_OWNER, str(uuid4()), 1000)
        self.assertIsNone(await store.get_current_entry_order(second["id"], PILOT_OWNER))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 1)

    async def test_store_verified_pilot_payment_settles_after_flag_off_without_sale_or_share(self):
        await self.sql.query((ROOT / "commerce.sql").read_text(), exec=True)
        await self.migrate()
        project = await self.connected_project(project=self.project)
        enabled = self.enrollment_store(pilot=True)
        pilot = await enabled.create_entry_order(project["id"], PILOT_OWNER, str(uuid4()), 1000)
        await enabled.save_invoice(
            project["id"], PILOT_OWNER, pilot["id"], "pilot-verified", "https://pay.example.test/pilot"
        )
        off = self.enrollment_store()
        now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        paid = await off.record_verified_payment(project["id"], pilot["id"], "pilot-verified", 1000, "RUB", now=now)
        self.assertEqual((paid["status"], paid["price_kopecks"], paid["paid_at"]), ("paid_pending_launch", 1000, now))
        self.assertEqual(await off.record_verified_payment(
            project["id"], pilot["id"], "pilot-verified", 1000, "RUB", now=now
        ), paid)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 1)
        self.assertEqual(await self.sql.fetchval(
            "SELECT count(*) FROM wl_partner_outbox WHERE kind='entry_paid' AND owner_tg_id=$1", PILOT_OWNER
        ), 1)
        self.assertEqual((await self.registry.get_project(project["id"], PILOT_OWNER))["status"], "draft")
        await self.assert_no_subscription_money()

    async def test_store_pilot_amount_cannot_settle_saved_normal_order_even_when_enabled(self):
        await self.sql.query((ROOT / "commerce.sql").read_text(), exec=True)
        await self.migrate()
        project = await self.connected_project(project=self.project)
        off = self.enrollment_store()
        normal = await off.create_entry_order(project["id"], PILOT_OWNER, str(uuid4()), 999000)
        await off.save_invoice(
            project["id"], PILOT_OWNER, normal["id"], "normal-verified", "https://pay.example.test/normal"
        )
        enabled = self.enrollment_store(pilot=True)
        with self.assertRaises(EnrollmentConflict):
            await enabled.record_verified_payment(project["id"], normal["id"], "normal-verified", 1000, "RUB")
        unchanged = await enabled.get_entry_order(project["id"], PILOT_OWNER, normal["id"])
        self.assertEqual((unchanged["status"], unchanged["price_kopecks"], unchanged["paid_at"]), ("created", 999000, None))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 0)
        await self.assert_no_subscription_money()
        self.assertEqual((await enabled.record_verified_payment(
            project["id"], normal["id"], "normal-verified", 999000, "RUB"
        ))["status"], "paid_pending_launch")
        await self.assert_no_subscription_money()

    async def test_existing_order_and_normal_default_unchanged_and_rerun_safe(self):
        before = await self.insert_order(price=999000)
        await self.migrate()
        self.assertEqual(
            await self.sql.fetchrow("SELECT * FROM wl_entry_orders WHERE id=$1", before["id"]),
            before,
        )
        await self.migrate()
        other = await self.new_project(42)
        normal_id = str(uuid4())
        await self.sql.execute(
            """INSERT INTO wl_entry_orders(id,project_id,owner_tg_id,request_key,bot_id)
               VALUES($1,$2,42,$3,654321)""", normal_id, other["id"], str(uuid4())
        )
        self.assertEqual(
            await self.sql.fetchval("SELECT price_kopecks FROM wl_entry_orders WHERE id=$1", normal_id),
            999000,
        )
        self.assertEqual(await self.sql.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname='wl_entry_financial_snapshot_guard'"
        ), 1)
        self.assertEqual(await self.sql.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname='wl_entry_bot_identity_guard'"
        ), 1)

    async def test_only_exact_owner_can_insert_exact_pilot_price(self):
        await self.migrate()
        self.assertEqual((await self.insert_order())["price_kopecks"], 1000)
        other = await self.new_project(42)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.insert_order(project=other, owner=42)
        for price in (0, 1, 999, 1001, 19900, 999001):
            with self.subTest(price=price), self.assertRaises(asyncpg.CheckViolationError):
                await self.insert_order(project=other, owner=42, price=price)
        self.assertEqual((await self.insert_order(project=other, owner=42, price=999000))["price_kopecks"], 999000)

    async def test_pilot_slot_is_unique_across_projects_and_released_only_on_cancel(self):
        await self.migrate()
        first = await self.insert_order()
        other = await self.new_project(PILOT_OWNER)
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.insert_order(project=other)
        await self.sql.execute("UPDATE wl_entry_orders SET status='canceled' WHERE id=$1", first["id"])
        second = await self.insert_order(project=other)
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.sql.execute("UPDATE wl_entry_orders SET status='created' WHERE id=$1", first["id"])
        self.assertEqual(second["status"], "created")

    async def test_paid_and_launched_orders_keep_pilot_slot_and_state_updates_work(self):
        await self.migrate()
        order = await self.insert_order()
        await self.sql.execute(
            """UPDATE wl_entry_orders SET provider_payment_id='pilot-provider',
               confirmation_url='https://pay.example.test/pilot' WHERE id=$1""", order["id"]
        )
        await self.sql.execute(
            "UPDATE wl_entry_orders SET status='paid_pending_launch',paid_at=now() WHERE id=$1",
            order["id"],
        )
        other = await self.new_project(PILOT_OWNER)
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.insert_order(project=other)
        await self.sql.execute(
            "UPDATE wl_entry_orders SET status='launched',launched_at=now() WHERE id=$1",
            order["id"],
        )
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.insert_order(project=other)
        row = await self.sql.fetchrow("SELECT * FROM wl_entry_orders WHERE id=$1", order["id"])
        self.assertEqual((row["price_kopecks"], row["status"]), (1000, "launched"))
        self.assertIsNotNone(row["paid_at"])
        self.assertIsNotNone(row["launched_at"])

    async def test_financial_snapshot_frozen_before_payment_for_every_named_field(self):
        await self.migrate()
        order = await self.insert_order()
        other = await self.new_project(PILOT_OWNER)
        changes = (
            ("price_kopecks", 999000), ("owner_tg_id", 42),
            ("project_id", other["id"]), ("request_key", str(uuid4())),
            ("bot_id", 654321), ("purpose", "subscription"), ("currency", "USD"),
        )
        for field, value in changes:
            with self.subTest(field=field), self.assertRaises(asyncpg.CheckViolationError) as error:
                await self.sql.execute(f"UPDATE wl_entry_orders SET {field}=$2 WHERE id=$1", order["id"], value)
            self.assertEqual(error.exception.constraint_name, "wl_entry_financial_snapshot_immutable")
        await self.sql.execute(
            """UPDATE wl_entry_orders SET price_kopecks=price_kopecks,owner_tg_id=owner_tg_id,
               project_id=project_id,request_key=request_key,bot_id=bot_id,purpose=purpose,
               currency=currency WHERE id=$1""", order["id"]
        )
        self.assertEqual(await self.sql.fetchrow("SELECT * FROM wl_entry_orders WHERE id=$1", order["id"]), order)

    async def test_normal_price_financial_snapshot_cannot_be_downgraded(self):
        await self.migrate()
        order = await self.insert_order(price=999000)
        with self.assertRaises(asyncpg.CheckViolationError) as error:
            await self.sql.execute("UPDATE wl_entry_orders SET price_kopecks=1000 WHERE id=$1", order["id"])
        self.assertEqual(error.exception.constraint_name, "wl_entry_financial_snapshot_immutable")
        other = await self.new_project(PILOT_OWNER)
        self.assertEqual((await self.insert_order(project=other, price=999000))["price_kopecks"], 999000)

    async def test_existing_bot_identity_guard_still_allows_same_bot_rotation_only(self):
        await self.migrate()
        connected = await self.registry.save_bot_connection(
            self.project["id"], PILOT_OWNER, 1, 123456, "PilotTestBot", "cipher:" + "x" * 100
        )
        await self.insert_order()
        rotated = await self.registry.save_bot_connection(
            self.project["id"], PILOT_OWNER, connected["version"],
            123456, "PilotTestBot", "cipher:" + "y" * 100
        )
        self.assertEqual(rotated["bot"]["id"], 123456)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("UPDATE wl_projects SET bot_id=654321 WHERE id=$1", self.project["id"])

    async def test_missing_original_price_constraint_fails_without_installing_pilot(self):
        await self.sql.execute("ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_orders_price_kopecks_check")
        await self.failed_migration()
        self.assertIsNone(await self.sql.fetchval(
            "SELECT to_regclass('public.wl_entry_orders_one_open_owner_pilot')"
        ))

    async def test_broadened_predecessor_fails_and_transaction_keeps_it_unchanged(self):
        await self.sql.execute("ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_orders_price_kopecks_check")
        await self.sql.execute(
            "ALTER TABLE wl_entry_orders ADD CONSTRAINT wl_entry_orders_price_kopecks_check CHECK(price_kopecks>0)"
        )
        await self.failed_migration()
        self.assertEqual(await self.sql.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='wl_entry_orders_price_kopecks_check'"
        ), "CHECK ((price_kopecks > 0))")
        self.assertIsNone(await self.sql.fetchval("SELECT to_regprocedure('public.wl_guard_entry_financial_snapshot()')"))

    async def test_unvalidated_original_constraint_is_not_accepted(self):
        await self.sql.execute("ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_orders_price_kopecks_check")
        await self.sql.execute(
            "ALTER TABLE wl_entry_orders ADD CONSTRAINT wl_entry_orders_price_kopecks_check CHECK(price_kopecks=999000) NOT VALID"
        )
        await self.failed_migration()
        self.assertFalse(await self.sql.fetchval(
            "SELECT convalidated FROM pg_constraint WHERE conname='wl_entry_orders_price_kopecks_check'"
        ))

    async def test_different_boolean_grouping_is_not_mistaken_for_exact_pilot(self):
        await self.sql.execute("ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_orders_price_kopecks_check")
        await self.sql.execute(
            """ALTER TABLE wl_entry_orders ADD CONSTRAINT wl_entry_orders_price_kopecks_check
               CHECK((price_kopecks=999000 OR price_kopecks=1000) AND owner_tg_id=6910097562)"""
        )
        await self.failed_migration()
        self.assertIsNone(await self.sql.fetchval(
            "SELECT to_regclass('public.wl_entry_orders_one_open_owner_pilot')"
        ))

    async def test_same_named_wrong_index_fails_and_restores_original_constraint(self):
        await self.sql.execute(
            "CREATE INDEX wl_entry_orders_one_open_owner_pilot ON wl_entry_orders(owner_tg_id)"
        )
        await self.failed_migration()
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.insert_order()
        self.assertIsNone(await self.sql.fetchval("SELECT to_regprocedure('public.wl_guard_entry_financial_snapshot()')"))


if __name__ == "__main__":
    unittest.main()
