"""Actual isolated PostgreSQL/WASM Managed Bots state and recovery tests."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

from cryptography.fernet import Fernet

from services.partner_managed_bots import ManagedBotStore, PartnerManagedBotService, ManagedBotConflict
from services.partner_security import TokenVault
from services.partner_store import PartnerStore
from test_partner_managed_bots import event, MANAGER, TENANT, NOW
from test_partner_store_postgres import RegistryBridge


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class ManagedBotPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start(); self.addAsyncCleanup(self.sql.close)
        base = Path(__file__).resolve().parents[1] / "ops/partner-platform"
        for name in ("enrollment.sql", "managed_bots.sql"):
            await self.sql.query((base / name).read_text(), exec=True)
        self.registry, self.store = PartnerStore(lambda: self.sql), ManagedBotStore(lambda: self.sql)
        self.project = await self.project_for(42)
        self.pid = self.project["id"]
        from unittest.mock import AsyncMock
        self.telegram = AsyncMock()
        async def me(token):
            if token == MANAGER: return {"id": 100001, "is_bot": True, "username": "ManagerBot", "can_manage_bots": True}
            return {"id": int(token.split(":")[0]), "is_bot": True, "username": "TenantBot", "first_name": "Выбранное имя VPN"}
        self.telegram.get_me.side_effect = me
        self.telegram.get_managed_bot_token.return_value = TENANT
        self.now = NOW
        self.service = PartnerManagedBotService(self.store, TokenVault((Fernet.generate_key().decode(),)),
            MANAGER, "ManagerBot", self.telegram, enabled=True, allowed_user_ids=[42], clock=lambda: self.now)

    async def project_for(self, owner, title="Мой VPN"):
        suffix = uuid4().hex[:12]
        return await self.registry.create_project(owner, "request-" + suffix, "bot-" + suffix, title)

    async def request(self): return await self.service.request_creation(self.pid, 42, self.project["version"])

    async def test_one_owner_pending_draft_and_exact_bot_binding_idempotency(self):
        await self.request(); await self.request()
        other = await self.project_for(42)
        with self.assertRaises(ManagedBotConflict):
            await self.service.request_creation(other["id"], 42, other["version"])
        self.assertTrue(await self.service.handle_update(event()))
        saved = await self.registry.get_project(self.pid, 42)
        self.assertEqual(saved["bot"]["id"], 200002)
        self.assertEqual(saved["title"], "Выбранное имя VPN")
        self.assertFalse(await self.service.handle_update(event()))
        self.assertEqual(self.telegram.get_managed_bot_token.await_count, 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_managed_bot_requests"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 0)

    async def test_owner_mismatch_or_existing_other_project_bot_never_hijacked(self):
        other = await self.project_for(99)
        await self.registry.save_bot_connection(other["id"], 99, other["version"], 200002, "ExistingBot", "cipher:" + "e" * 90)
        await self.request()
        self.assertFalse(await self.service.handle_update(event()))
        self.telegram.get_managed_bot_token.assert_not_awaited()
        self.assertIsNone((await self.registry.get_project(self.pid, 42))["bot"]["id"])
        self.assertEqual((await self.registry.get_project(other["id"], 99))["bot"]["id"], 200002)

    async def test_title_changed_during_creation_is_preserved(self):
        await self.request()
        await self.registry.update_project(self.pid, 42, self.project["version"], {"title": "Моё явное название"})
        self.assertTrue(await self.service.handle_update(event()))
        self.assertEqual((await self.registry.get_project(self.pid, 42))["title"], "Моё явное название")

    async def test_reserved_identity_survives_restart_network_failure_and_expiry(self):
        await self.request()
        self.telegram.get_managed_bot_token.side_effect = RuntimeError("private remote error")
        with self.assertRaises(RuntimeError): await self.service.handle_update(event())
        self.assertEqual(await self.sql.fetchval("SELECT bot_id FROM wl_managed_bot_requests WHERE project_id=$1", self.pid), 200002)
        self.now += timedelta(hours=1)
        self.telegram.get_managed_bot_token.side_effect = None
        self.assertTrue(await self.service.retry_pending(self.pid, 42))
        self.assertEqual((await self.registry.get_project(self.pid, 42))["bot"]["id"], 200002)

    async def test_same_bot_rotation_preserves_active_paid_identity_and_title(self):
        await self.request(); await self.service.handle_update(event())
        saved = await self.registry.get_project(self.pid, 42)
        await self.sql.execute("UPDATE wl_projects SET status='active' WHERE id=$1", self.pid)
        self.telegram.get_managed_bot_token.return_value = "200002:" + "n" * 35
        self.assertFalse(await self.service.handle_update(event(owner=99, update_id=16)))
        self.assertTrue(await self.service.handle_update(event(update_id=16)))
        after = await self.registry.get_project(self.pid, 42)
        self.assertEqual((after["owner_tg_id"], after["bot"]["id"], after["status"], after["title"]),
                         (42, 200002, "active", saved["title"]))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 0)

    async def test_expired_unclaimed_request_does_not_accept_late_event(self):
        await self.request(); self.now += timedelta(hours=1)
        self.assertFalse(await self.service.handle_update(event()))
        self.telegram.get_managed_bot_token.assert_not_awaited()

    async def test_current_token_can_refresh_after_weeklong_update_id_reset(self):
        await self.request(); await self.service.handle_update(event(update_id=200))
        self.assertFalse(await self.service.handle_update(event(update_id=199)))
        self.now += timedelta(days=8)
        self.assertTrue(await self.service.handle_update(event(update_id=1)))
        self.assertEqual(await self.sql.fetchval("SELECT last_update_id FROM wl_managed_bot_requests WHERE project_id=$1", self.pid), 1)


if __name__ == "__main__": unittest.main()
