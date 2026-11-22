"""Real isolated PostgreSQL/WASM support SQL; never a live database."""

import os
import unittest

from services.partner_store import PartnerStore, ProjectConflict, ProjectNotFound
from services.partner_support import PartnerSupportStore
from test_partner_store_postgres import RegistryBridge


ADMIN = 6910097562


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class PlatformSupportPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.registry = PartnerStore(lambda: self.sql)
        self.support = PartnerSupportStore(lambda: self.sql, operator_ids=[ADMIN])
        self.first = await self.registry.create_project(42, "support-first-1", "first-support", "First VPN")
        self.second = await self.registry.create_project(99, "support-second-1", "second-support", "Second VPN")

    async def test_operator_lists_projects_without_owner_impersonation_or_credentials(self):
        await self.registry.save_bot_connection(self.first["id"], 42, 1, 123456, "FirstSupportBot", "cipher:" + "x" * 100)
        items = (await self.support.list_projects(ADMIN))["items"]
        self.assertEqual({row["id"] for row in items}, {self.first["id"], self.second["id"]})
        self.assertEqual({row["owner_tg_id"] for row in items}, {42, 99})
        self.assertNotIn("cipher:", str(items))
        self.assertNotIn("encrypted_token", str(items))
        with self.assertRaises(ProjectNotFound):
            await self.support.get_project(self.first["id"], 42)
        with self.assertRaises(ProjectNotFound):
            await self.registry.get_project(self.second["id"], ADMIN)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 2)

    async def test_edit_preserves_owner_and_audits_real_actor_in_same_transaction(self):
        changed = await self.support.update_project(self.first["id"], ADMIN, 1, {"title": "Assisted VPN", "price_kopecks": 21900})
        self.assertEqual((changed["owner_tg_id"], changed["version"], changed["price_kopecks"]), (42, 2, 21900))
        audit = await self.sql.fetchrow("SELECT owner_tg_id,project_version,details FROM wl_project_audit WHERE project_id=$1 AND action='settings_updated'", self.first["id"])
        self.assertEqual((audit["owner_tg_id"], audit["project_version"]), (42, 2))
        self.assertEqual(audit["details"], {"fields": ["price_kopecks", "title"], "actor_tg_id": ADMIN, "context": "platform_support"})
        self.assertEqual((await self.registry.get_project(self.second["id"], 99))["version"], 1)
        with self.assertRaises(ProjectConflict):
            await self.support.update_project(self.first["id"], ADMIN, 1, {"title": "Stale edit"})
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_audit WHERE action='settings_updated'"), 1)

    async def test_failed_audit_rolls_back_settings_and_preserves_owner_version(self):
        self.sql.fail_audit = True
        with self.assertRaisesRegex(RuntimeError, "injected audit failure"):
            await self.support.update_project(self.first["id"], ADMIN, 1, {"title": "Must roll back", "price_kopecks": 99900})
        self.sql.fail_audit = False
        saved = await self.registry.get_project(self.first["id"], 42)
        self.assertEqual((saved["title"], saved["version"], saved["price_kopecks"], saved["owner_tg_id"]), ("First VPN", 1, 19900, 42))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_audit WHERE action='settings_updated'"), 0)
        # A subsequent valid operation still works and records one support actor.
        await self.support.update_project(self.first["id"], ADMIN, 1, {"title": "Recovered"})
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_audit WHERE action='settings_updated'"), 1)
