"""Actual isolated PostgreSQL/WASM execution of the fresh webhook SELECT."""
import os
from pathlib import Path
import unittest

from services.partner_management import PartnerManagementStore
from services.partner_store import PartnerStore, ProjectNotFound
from test_partner_store_postgres import RegistryBridge


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PG/WASM is not configured")
class BotUpdateContextPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        root = Path(__file__).resolve().parents[1]
        for name in ("commerce", "entitlements", "traffic", "management"):
            await self.sql.query((root / f"ops/partner-platform/{name}.sql").read_text(), exec=True)
        self.registry = PartnerStore(lambda: self.sql)
        self.management = PartnerManagementStore(lambda: self.sql)
        project = await self.registry.create_project(42, "context-request-123", "context-test", "Контекст VPN")
        self.pid = project["id"]
        await self.registry.save_bot_connection(self.pid, 42, 1, 123456, "ContextBot", "cipher:" + "x" * 100)
        await self.sql.execute("UPDATE wl_projects SET status='active' WHERE id=$1", self.pid)

    async def test_real_select_keeps_user_project_role_and_first_attribution_exact(self):
        new = await self.registry.get_bot_update_context(self.pid, 100)
        receipt = new["_bot_update_context"]
        self.assertFalse(receipt.registered)
        self.assertFalse(receipt.attribution_frozen)
        self.assertIsNone(receipt.staff_role)
        await self.management.register_customer(self.pid, 100)
        await self.sql.execute("INSERT INTO wl_manager_identities(tg_id) VALUES(100)")
        await self.sql.execute("INSERT INTO wl_project_members(project_id,tg_id,role) VALUES($1,100,'support')", self.pid)
        current = await self.registry.get_bot_update_context(self.pid, 100)
        receipt = current["_bot_update_context"]
        self.assertTrue(receipt.matches(current, 100))
        self.assertTrue(receipt.registered)
        self.assertTrue(receipt.attribution_frozen)
        self.assertEqual(receipt.staff_role, "support")
        foreign = (await self.registry.get_bot_update_context(self.pid, 101))["_bot_update_context"]
        self.assertFalse(foreign.registered)
        self.assertIsNone(foreign.staff_role)
        await self.sql.execute("UPDATE wl_project_members SET active=false WHERE project_id=$1 AND tg_id=100", self.pid)
        self.assertIsNone((await self.registry.get_bot_update_context(self.pid, 100))["_bot_update_context"].staff_role)
        self.assertNotIn("encrypted_token", current)

    async def test_suspended_project_is_not_usable_and_legacy_customer_is_not_frozen(self):
        await self.sql.execute("INSERT INTO wl_customers(id,project_id,tg_id) VALUES(gen_random_uuid(),$1,100)", self.pid)
        current = (await self.registry.get_bot_update_context(self.pid, 100))["_bot_update_context"]
        self.assertTrue(current.registered)
        self.assertFalse(current.attribution_frozen)
        await self.sql.execute("UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid)
        with self.assertRaises(ProjectNotFound):
            await self.registry.get_bot_update_context(self.pid, 100)


if __name__ == "__main__":
    unittest.main()
