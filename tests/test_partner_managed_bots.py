"""Managed Bot ports, trusted updates and manager compatibility; no real I/O."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI
import httpx

from partner_manager_web import create_manager_router
from services.partner_manager import PartnerManagerHandler, PartnerManagerBootstrap, manager_webhook_secret
from services.partner_managed_bots import (PartnerManagedBotService, ManagedBotUnavailable,
                                         event_identity, initial_name, TelegramManagedBotAdapter)
from services.partner_security import TokenVault
from test_partner_runtime_manager import update as start_update


MANAGER = "100001:" + "m" * 35
TENANT = "200002:" + "t" * 35
NOW = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
PID = str(uuid4())
ORIGIN = "https://manager.example.test"


def event(owner=42, bot=200002, update_id=15):
    return {"update_id": update_id, "managed_bot": {
        "user": {"id": owner, "is_bot": False, "first_name": "Owner"},
        "bot": {"id": bot, "is_bot": True, "username": "TenantBot", "first_name": "Новый VPN"}}}


class ManagedBotServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = AsyncMock()
        self.store.begin_request.return_value = {"title": "Мой VPN", "expires_at": NOW, "status": "pending"}
        self.job = {"project_id": PID, "owner_tg_id": 42, "bot_id": 200002, "update_id": 15}
        self.store.claim_event.return_value = self.job
        self.telegram = AsyncMock()
        async def me(token):
            return ({"id": 100001, "is_bot": True, "username": "ManagerBot", "can_manage_bots": True}
                    if token == MANAGER else {"id": 200002, "is_bot": True, "username": "TenantBot", "first_name": "Новый VPN"})
        self.telegram.get_me.side_effect = me
        self.telegram.get_managed_bot_token.return_value = TENANT
        self.vault = TokenVault((Fernet.generate_key().decode(),))
        self.service = PartnerManagedBotService(self.store, self.vault, MANAGER, "ManagerBot",
            self.telegram, enabled=True, allowed_user_ids=[42], clock=lambda: NOW)

    async def test_constructor_available_disabled_perform_no_io(self):
        service = PartnerManagedBotService(self.store, self.vault, MANAGER, "", self.telegram)
        self.assertIs(service.available, False)
        self.assertIs(self.service.available, True)
        self.telegram.get_me.assert_not_awaited()
        self.store.begin_request.assert_not_awaited()
        self.assertNotIn(MANAGER, repr(self.service))

    async def test_request_checks_manager_mode_and_returns_context_free_official_link(self):
        result = await self.service.request_creation(PID, 42, 1)
        self.assertEqual(result["creation_url"], "https://t.me/newbot/ManagerBot?name=%D0%9C%D0%BE%D0%B9%20VPN")
        self.assertNotIn(PID, result["creation_url"])
        for token in (MANAGER, TENANT): self.assertNotIn(token, str(result))
        self.telegram.get_managed_bot_token.assert_not_awaited()
        self.store.begin_request.assert_awaited_once()

    async def test_outside_allowlist_and_management_mode_off_cannot_start(self):
        with self.assertRaises(ManagedBotUnavailable): await self.service.request_creation(PID, 99, 1)
        self.telegram.get_me.assert_not_awaited()
        self.telegram.get_me.side_effect = None
        self.telegram.get_me.return_value = {"id": 100001, "is_bot": True, "username": "ManagerBot", "can_manage_bots": False}
        with self.assertRaises(ManagedBotUnavailable): await self.service.request_creation(PID, 42, 1)
        self.store.begin_request.assert_not_awaited()

    async def test_verified_event_fetches_token_only_with_manager_credential_and_encrypts(self):
        self.assertTrue(await self.service.handle_update(event()))
        self.telegram.get_managed_bot_token.assert_awaited_once_with(MANAGER, 200002)
        self.telegram.get_me.assert_awaited_once_with(TENANT)
        args, kwargs = self.store.complete_binding.await_args
        self.assertEqual(args[0], self.job)
        self.assertEqual(args[1], "TenantBot")
        self.assertEqual(self.vault.decrypt(PID, 200002, args[2]), TENANT)
        self.assertEqual(kwargs["first_name"], "Новый VPN")
        self.assertNotIn(TENANT, str(args[:2]))

    async def test_wrong_token_identity_and_foreign_owner_never_bind(self):
        self.assertFalse(await self.service.handle_update(event(owner=99)))
        self.store.claim_event.assert_not_awaited()
        self.telegram.get_managed_bot_token.return_value = "300003:" + "x" * 35
        with self.assertRaises(ManagedBotUnavailable): await self.service.handle_update(event())
        self.store.complete_binding.assert_not_awaited()

    async def test_no_pending_draft_and_duplicate_are_noop_before_external_calls(self):
        self.store.claim_event.return_value = None
        self.assertFalse(await self.service.handle_update(event()))
        self.telegram.get_managed_bot_token.assert_not_awaited()

    async def test_event_parsing_rejects_forged_message_fields_and_bad_ids(self):
        for data in (event(owner=True), event(bot=True), event(update_id=True), event(update_id=-1), {}, {"managed_bot": {}}):
            self.assertIsNone(event_identity(data))
        shared = {"update_id": 16, "message": {"from": {"id": 42, "is_bot": False}, "chat": {"id": 42, "type": "private"},
            "managed_bot_created": {"bot": event()["managed_bot"]["bot"]}}}
        self.assertEqual(event_identity(shared), (42, 200002, 16))
        for key in ("forward_origin", "forward_from", "sender_chat", "via_bot", "business_connection_id"):
            bad = deepcopy(shared); bad["message"][key] = {}
            self.assertIsNone(event_identity(bad))
        for name in ("", "x\nVPN", "x\tVPN", "x\u202eVPN", "x" * 65): self.assertIsNone(initial_name(name))
        self.assertEqual(initial_name(" Свой VPN "), "Свой VPN")

    async def test_manager_handler_independently_enforces_its_allowlist(self):
        service = SimpleNamespace(available=True, handle_update=AsyncMock())
        handler = PartnerManagerHandler(100001, ORIGIN, username="ManagerBot", manager_allowed_user_ids=[42], managed_service=service)
        self.assertIsNone(await handler.handle(event(owner=99)))
        service.handle_update.assert_not_awaited()
        self.assertIsNone(await handler.handle(event()))
        service.handle_update.assert_awaited_once()
        for command in ("/start", "/help"):
            incoming = start_update(); incoming["message"]["text"] = command
            response = await handler.handle(incoming)
            self.assertIn("https://t.me/vpnislam", str(response))

    async def test_manager_webhook_rejects_forged_update_before_token_lookup(self):
        handler = PartnerManagerHandler(100001, ORIGIN, username="ManagerBot", manager_allowed_user_ids=[42], managed_service=self.service)
        app = FastAPI(); key = b"h" * 32
        app.include_router(create_manager_router(handler, bot_id=100001, secret_key=key, webhook_origin=ORIGIN, enabled=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            self.assertEqual((await client.post("/partner-manager/webhook", json=event())).status_code, 403)
            self.telegram.get_managed_bot_token.assert_not_awaited()
            response = await client.post("/partner-manager/webhook", json=event(), headers={"X-Telegram-Bot-Api-Secret-Token": manager_webhook_secret(key, 100001)})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(TENANT, response.text)

    async def test_adapter_only_uses_documented_bot_id_parameter(self):
        adapter = TelegramManagedBotAdapter()
        adapter._call = AsyncMock(return_value=TENANT)
        self.assertEqual(await adapter.get_managed_bot_token(MANAGER, 200002), TENANT)
        adapter._call.assert_awaited_once_with(MANAGER, "getManagedBotToken", {"user_id": 200002})


class ManagedBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_requires_management_mode_and_exact_update_types(self):
        telegram = AsyncMock()
        telegram.get_me.return_value = {"id": 100001, "is_bot": True, "username": "ManagerBot", "can_manage_bots": False}
        service = PartnerManagerBootstrap(token=MANAGER, webhook_origin=ORIGIN, webhook_key=b"w" * 32,
            telegram=telegram, enabled=True, username="ManagerBot", managed_bots_enabled=True)
        self.assertEqual(await service.install(), {"state": "management_mode_required"})
        telegram.set_manager_webhook.assert_not_awaited()
        telegram.get_me.return_value["can_manage_bots"] = True
        telegram.get_webhook_info.side_effect = [{"url": "", "allowed_updates": []},
            {"url": ORIGIN + "/partner-manager/webhook", "allowed_updates": ["message", "managed_bot"]}]
        self.assertEqual(await service.install(), {"state": "installed"})
        self.assertIs(telegram.set_manager_webhook.await_args.kwargs["managed_bots_enabled"], True)


if __name__ == "__main__": unittest.main()
