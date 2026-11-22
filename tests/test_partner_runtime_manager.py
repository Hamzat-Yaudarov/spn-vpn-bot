"""Central-manager entry, never tenant-signed authorization or real Telegram."""

import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from partner_client_web import webhook_secret
from partner_manager_web import create_manager_router
from services.partner_manager import (
    PartnerManagerBootstrap,
    PartnerManagerHandler,
    TelegramManagerAdapter,
    manager_webhook_secret,
)
from services.partner_runtime import build_runtime
from services.partner_runtime_catalog import StagingAddonCatalog
from test_partner_runtime import TOKEN, staging


BOT_ID = int(TOKEN.split(":", 1)[0])
KEY = b"m" * 32
ORIGIN = "https://manager.example.com"


def update(**changes):
    result = {
        "update_id": 15,
        "message": {
            "message_id": 2,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/start",
        },
    }
    result.update(changes)
    return result


class ManagerHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_start_fixed_webapp_url_and_no_grant_or_token(self):
        handler = PartnerManagerHandler(BOT_ID, ORIGIN, username="ManagerBot")
        command = await handler.handle(update())
        self.assertEqual(command["method"], "sendMessage")
        self.assertEqual(command["chat_id"], 42)
        button = command["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"], ORIGIN + "/partners")
        self.assertNotIn(TOKEN, str(command))
        self.assertNotIn("initData", str(command))
        for condition in (
            "9 990 ₽ один раз", "70%", "30%", "под своим брендом",
            "Оплатите подключение", "в вашем боте",
        ):
            self.assertIn(condition, command["text"])
        for unnecessary in ("VPN", "ГБ", "антиглушилка", "обход", "устройства", "199 ₽"):
            self.assertNotIn(unnecessary, command["text"])
        self.assertEqual(button["text"], "Создать свой сервис")
        self.assertNotIn("15%", command["text"])
        self.assertNotIn("доход гарантирован", command["text"].lower())
        incoming = update()
        incoming["message"]["text"] = "/start owner_999999"
        self.assertEqual((await handler.handle(incoming))["chat_id"], 42)

    async def test_rejects_group_forward_business_bot_sender_and_impersonation(self):
        handler = PartnerManagerHandler(BOT_ID, ORIGIN)
        invalid = []
        for field in (
            "forward_origin",
            "via_bot",
            "business_connection_id",
            "sender_chat",
        ):
            row = update()
            row["message"][field] = {"id": 123}
            invalid.append(row)
        row = update()
        row["message"]["chat"]["type"] = "group"
        invalid.append(row)
        row = update()
        row["message"]["chat"]["id"] = 777
        invalid.append(row)
        row = update()
        row["message"]["from"]["is_bot"] = True
        invalid.append(row)
        row = update()
        row["message"]["from"]["id"] = True
        invalid.append(row)
        invalid.append(update(edited_message={}))
        for row in invalid:
            with self.subTest(row=row):
                self.assertIsNone(await handler.handle(row))

    async def test_commands_only_addressed_to_manager_no_arbitrary_callback_or_emptytext(
        self,
    ):
        handler = PartnerManagerHandler(BOT_ID, ORIGIN, username="ManagerBot")
        for text in ("", " ", "/unknown", "/start@OtherBot", "<script>alert(1)</script>"):
            incoming = update()
            incoming["message"]["text"] = text
            self.assertIsNone(await handler.handle(incoming))
        incoming = update()
        incoming["message"]["text"] = "/start@managerbot"
        self.assertIsNotNone(await handler.handle(incoming))

    async def test_secret_namespace_not_interchangeable_with_tenant_bots(self):
        manager = manager_webhook_secret(KEY, BOT_ID)
        tenant = webhook_secret(KEY, "00000000-0000-4000-8000-000000000001", BOT_ID)
        self.assertNotEqual(manager, tenant)


class ManagerWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handler = PartnerManagerHandler(BOT_ID, ORIGIN, username="ManagerBot")
        app = FastAPI()
        app.include_router(
            create_manager_router(
                handler,
                bot_id=BOT_ID,
                secret_key=KEY,
                webhook_origin=ORIGIN,
                enabled=True,
            )
        )
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
        )
        self.headers = {
            "x-telegram-bot-api-secret-token": manager_webhook_secret(KEY, BOT_ID)
        }

    async def test_verified_webhook_returns_only_central_webapp_button(self):
        response = await self.client.post(
            "/partner-manager/webhook", headers=self.headers, json=update()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chat_id"], 42)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_wrong_secret_host_forwardedhost_and_query_rejected(self):
        for headers, path, status in (
            (
                {"x-telegram-bot-api-secret-token": "a" * 64},
                "/partner-manager/webhook",
                403,
            ),
            (
                {
                    **self.headers,
                    "host": "tenant.example.com",
                    "x-forwarded-host": "manager.example.com",
                },
                "/partner-manager/webhook",
                404,
            ),
            (self.headers, "/partner-manager/webhook?owner=42", 404),
        ):
            response = await self.client.post(path, headers=headers, json=update())
            self.assertEqual(response.status_code, status, response.text)

    async def test_duplicate_headers_and_json_rejected(self):
        secret = manager_webhook_secret(KEY, BOT_ID)
        cases = [
            [
                ("x-telegram-bot-api-secret-token", secret),
                ("x-telegram-bot-api-secret-token", secret),
                ("content-type", "application/json"),
            ],
            [
                ("x-telegram-bot-api-secret-token", secret),
                ("host", "manager.example.com"),
                ("host", "manager.example.com"),
                ("content-type", "application/json"),
            ],
            [
                ("x-telegram-bot-api-secret-token", secret),
                ("content-type", "application/json"),
                ("content-type", "application/json"),
            ],
        ]
        for headers in cases:
            response = await self.client.post(
                "/partner-manager/webhook",
                headers=headers,
                content=json.dumps(update()),
            )
            self.assertIn(response.status_code, {403, 404, 415})
        response = await self.client.post(
            "/partner-manager/webhook",
            headers={**self.headers, "content-type": "application/json"},
            content='{"update_id":1,"update_id":2}',
        )
        self.assertEqual(response.status_code, 400)

    async def test_size_contenttype_invalid_update_and_disabled_default(self):
        response = await self.client.post(
            "/partner-manager/webhook", headers=self.headers, content="x"
        )
        self.assertEqual(response.status_code, 415)
        response = await self.client.post(
            "/partner-manager/webhook", headers=self.headers, json={"update_id": True}
        )
        self.assertEqual(response.status_code, 400)
        response = await self.client.post(
            "/partner-manager/webhook",
            headers=self.headers,
            json={"update_id": 1, "x": "a" * 70_000},
        )
        self.assertEqual(response.status_code, 413)
        app = FastAPI()
        app.include_router(create_manager_router(None, bot_id=BOT_ID))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            self.assertEqual(
                (await client.post("/partner-manager/webhook")).status_code, 404
            )


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, *, foreign=""):
        adapter = type("Telegram", (), {})()
        adapter.get_me = AsyncMock(
            return_value={"id": BOT_ID, "is_bot": True, "username": "ManagerBot"}
        )
        adapter.get_webhook_info = AsyncMock(
            side_effect=[
                {"url": foreign},
                {
                    "url": ORIGIN + "/partner-manager/webhook",
                    "allowed_updates": ["message"],
                },
            ]
        )
        adapter.set_manager_webhook = AsyncMock(return_value=True)
        return adapter

    def bootstrap(self, adapter, **changes):
        values = dict(
            token=TOKEN,
            webhook_origin=ORIGIN,
            webhook_key=KEY,
            telegram=adapter,
            enabled=True,
            username="ManagerBot",
        )
        values.update(changes)
        return PartnerManagerBootstrap(**values)

    async def test_disabled_never_installs_or_discovers_anything(self):
        adapter = self.adapter()
        bootstrap = self.bootstrap(adapter, enabled=False)
        self.assertEqual(await bootstrap.install(), {"state": "disabled"})
        adapter.get_me.assert_not_awaited()
        adapter.set_manager_webhook.assert_not_awaited()
        self.assertNotIn(TOKEN, repr(bootstrap))

    async def test_foreign_webhook_cannot_be_overwritten_without_operator_consent(self):
        adapter = self.adapter(foreign="https://foreign.example.com/webhook")
        self.assertEqual(
            (await self.bootstrap(adapter).install())["state"], "needs_webhook_replace"
        )
        adapter.set_manager_webhook.assert_not_awaited()
        adapter = self.adapter(foreign="https://foreign.example.com/webhook")
        self.assertEqual(
            (await self.bootstrap(adapter).install(replace_existing=True))["state"],
            "installed",
        )
        adapter.set_manager_webhook.assert_awaited_once_with(
            TOKEN, origin=ORIGIN, secret_token=manager_webhook_secret(KEY, BOT_ID)
        )

    async def test_lost_set_response_keeps_same_target_and_returns_redacted_retry(self):
        adapter = self.adapter()
        adapter.set_manager_webhook.side_effect = RuntimeError(
            "token must never be echoed"
        )
        result = await self.bootstrap(adapter).install()
        self.assertEqual(result, {"state": "retry"})
        self.assertNotIn("token must", str(result))

    async def test_wrong_identity_username_and_readback_do_not_report_installed(self):
        adapter = self.adapter()
        adapter.get_me.return_value["id"] = BOT_ID + 1
        self.assertEqual((await self.bootstrap(adapter).install())["state"], "retry")
        adapter.set_manager_webhook.assert_not_awaited()
        adapter = self.adapter()
        adapter.get_me.return_value["username"] = "ChangedBot"
        self.assertEqual(
            (await self.bootstrap(adapter).install())["state"],
            "username_requires_review",
        )
        adapter.set_manager_webhook.assert_not_awaited()
        adapter = self.adapter()
        adapter.get_webhook_info.side_effect = [
            {"url": ""},
            {"url": ORIGIN + "/partner-manager/webhook"},
        ]
        self.assertEqual((await self.bootstrap(adapter).install())["state"], "retry")

    async def test_adapter_never_drops_updates_and_only_expected_methods(self):
        adapter = TelegramManagerAdapter()
        adapter._call = AsyncMock(return_value=True)
        await adapter.set_manager_webhook(TOKEN, origin=ORIGIN, secret_token="a" * 64)
        payload = adapter._call.await_args.args[2]
        self.assertEqual(payload["allowed_updates"], ["message"])
        self.assertIs(payload["drop_pending_updates"], False)
        self.assertEqual(payload["url"], ORIGIN + "/partner-manager/webhook")

    async def test_bootstrap_cancellation_is_not_swallowed(self):
        adapter = self.adapter()
        adapter.get_me.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.bootstrap(adapter).install()


class RuntimeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_flag_configuration_and_http_wiring_no_install_on_app_start(self):
        loader = StagingAddonCatalog()
        settings = staging(
            enable_http=True,
            allow_staging_network=True,
            allow_staging_writes=True,
            enable_manager_webhook=True,
            manager_bot_username="ManagerBot",
            webhook_origin=ORIGIN,
            webhook_key_hex=KEY.hex(),
        )
        runtime = build_runtime(settings, lambda: None, addon_loader=loader.quote)
        runtime.manager_bootstrap.install = AsyncMock(
            side_effect=AssertionError("must never install")
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runtime.app()), base_url=ORIGIN
        ) as client:
            response = await client.post(
                "/partner-manager/webhook",
                headers={
                    "x-telegram-bot-api-secret-token": manager_webhook_secret(
                        KEY, BOT_ID
                    )
                },
                json=update(),
            )
            self.assertEqual(response.status_code, 200)
        runtime.manager_bootstrap.install.assert_not_awaited()
        self.assertIn(
            "manager_bot_username_missing",
            staging(enable_manager_webhook=True).preflight()["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
