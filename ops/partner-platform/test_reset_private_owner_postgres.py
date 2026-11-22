"""Actual isolated PG/WASM reset SQL, never a live database or Telegram."""
from contextlib import asynccontextmanager
import importlib.util
import os
from pathlib import Path
import unittest
from uuid import uuid4

from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge


ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("owner_reset_pg", ROOT / "reset_private_owner.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Bridge(RegistryBridge):
    fail_delete = False

    @asynccontextmanager
    async def transaction(self, **kwargs):
        await super().execute("BEGIN")
        try:
            yield
        except BaseException:
            await super().execute("ROLLBACK")
            raise
        else:
            await super().execute("COMMIT")

    async def execute(self, sql, *args):
        if sql.startswith("DELETE "):
            if self.fail_delete and '"wl_projects"' in sql:
                raise RuntimeError("Injected final deletion failure")
            rows = await self.fetch(sql + " RETURNING 1", *args)
            return "DELETE " + str(len(rows))
        return await super().execute(sql, *args)


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Isolated PG/WASM not configured")
class ResetPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await Bridge().start()
        self.addAsyncCleanup(self.sql.close)
        for name in ("commerce", "enrollment", "entitlements", "traffic", "notifications",
                     "subscriptions", "finance", "management", "operations", "branding",
                     "panel_identity", "public_checkout", "managed_bots", "owner_entry_pilot",
                     "entry_launch_notifications"):
            await self.sql.query((ROOT / (name + ".sql")).read_text(), exec=True)
        self.store = PartnerStore(lambda: self.sql)
        p = await self.store.create_project(module.OWNER, "old-test-create", "old-test-vpn", "Тест")
        self.pid, self.oid = p["id"], str(uuid4())
        await self.store.save_bot_connection(self.pid, module.OWNER, p["version"], 123456789, "OldTestBot", "cipher:" + "x" * 100)
        await self.sql.execute("""INSERT INTO wl_entry_orders(id,project_id,owner_tg_id,request_key,bot_id,price_kopecks,
            status,provider_payment_id,confirmation_url,paid_at,launched_at)
            VALUES($1,$2,$3,$4,123456789,1000,'launched','old-real-payment','https://example.test/pay',now(),now())""",
            self.oid, self.pid, module.OWNER, str(uuid4()))
        await self.sql.execute("""INSERT INTO wl_launch_jobs(project_id,order_id,owner_tg_id,bot_id,status,
            completed_at,webhook_url,secret_fingerprint) VALUES($1,$2,$3,123456789,'complete',now(),
            'https://example.test/partner-bots/test/webhook',$4)""", self.pid, self.oid, module.OWNER, "a" * 64)
        await self.sql.execute("UPDATE wl_projects SET status='active' WHERE id=$1", self.pid)
        await self.sql.execute("INSERT INTO wl_partner_outbox(project_id,order_id,owner_tg_id,kind) VALUES($1,$2,$3,'entry_paid')",
                               self.pid, self.oid, module.OWNER)
        await self.sql.execute("INSERT INTO wl_manager_identities(tg_id) VALUES($1)", module.OWNER)
        await self.sql.execute("INSERT INTO wl_customers(id,project_id,tg_id) VALUES($1,$2,$3)", str(uuid4()), self.pid, module.OWNER)
        sid = str(uuid4())
        await self.sql.execute("INSERT INTO wl_subscriptions(id,project_id,tg_id) VALUES($1,$2,$3)", sid, self.pid, module.OWNER)
        await self.sql.execute("""INSERT INTO wl_orders(id,project_id,tg_id,subscription_id,request_key,request_fingerprint,price_kopecks)
            VALUES($1,$2,$3,$4,$5,$6,19900)""", str(uuid4()), self.pid, module.OWNER, sid, str(uuid4()), "f" * 64)
        await self.sql.execute("INSERT INTO wl_campaign_attributions(project_id,tg_id) VALUES($1,$2)", self.pid, module.OWNER)

    async def test_all_fk_ordered_deletes_and_new_pilot_slot_schema_unchanged(self):
        before = await module.current_rows(self.sql)
        self.assertEqual(set(before), module.TABLES)
        module.validate_tables(before)
        await module.clear_records(self.sql, before)
        self.assertTrue(all(not rows for rows in (await module.current_rows(self.sql)).values()))
        # No index/trigger is changed: a fresh confirmed project can use 10 RUB.
        p = await self.store.create_project(module.OWNER, "new-test-create", "new-test-vpn", "Новый VPN")
        await self.store.save_bot_connection(p["id"], module.OWNER, p["version"], 987654321, "NewTestBot", "cipher:" + "y" * 100)
        await self.sql.execute("""INSERT INTO wl_entry_orders(id,project_id,owner_tg_id,request_key,bot_id,price_kopecks)
            VALUES($1,$2,$3,$4,987654321,1000)""", str(uuid4()), p["id"], module.OWNER, str(uuid4()))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM pg_trigger WHERE tgname='wl_revenue_ledger_append_only'"), 1)

    async def test_late_snapshot_change_rejected_without_deletion(self):
        before = await module.current_rows(self.sql)
        await self.sql.execute("UPDATE wl_projects SET title='Изменено' WHERE id=$1", self.pid)
        with self.assertRaises(module.ResetError):
            await module.clear_records(self.sql, before)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 1)

    async def test_delete_failure_rolls_back_entire_graph(self):
        before = await module.current_rows(self.sql)
        self.sql.fail_delete = True
        with self.assertRaises(RuntimeError):
            await module.clear_records(self.sql, before)
        after = await module.current_rows(self.sql)
        self.assertTrue(module.same_rows(before, after))


if __name__ == "__main__":
    unittest.main()
