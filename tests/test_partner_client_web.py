"""Offline tests of the deliberately unmounted partner webhook boundary.

ASGITransport calls only an in-process FastAPI app. No token is used, no
Telegram API operation is sent, and no external database is opened.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from partner_client_web import MAX_UPDATE_BYTES, create_client_router, webhook_secret
from services.partner_bot import PartnerBotHandler, render_purchase


PROJECT_ID = "244bc71c-2ce8-4d7a-b72f-420424db2507"
OTHER_PROJECT_ID = "132e380c-e519-4d50-885a-58e4dfb35a49"
WEBHOOK_KEY = b"test-only-webhook-key-never-a-bot-token-32bytes"
BOT_ID = 90001


def project(**changes):
    return {
        "id": PROJECT_ID, "owner_tg_id": 777, "title": "Test VPN", "price_kopecks": 19900,
        "status": "active", "brand": {}, "support_url": "", "news_url": "",
        "bot": {"id": BOT_ID, "username": "offline_partner_bot"}, **changes,
    }


def update(**changes):
    return {"update_id": 100, "message": {
        "message_id": 5, "from": {"id": 222, "is_bot": False},
        "chat": {"id": 222, "type": "private"}, "text": "/start",
    }, **changes}


def callback(data="menu"):
    return {"update_id": 101, "callback_query": {
        "id": "offline-callback-id", "from": {"id": 222, "is_bot": False}, "data": data,
        "message": {"message_id": 5, "from": {"id": BOT_ID, "is_bot": True},
                    "chat": {"id": 222, "type": "private"}},
    }}


class WebhookSecretTests(unittest.TestCase):
    def test_secret_is_project_bot_and_key_specific(self):
        result = webhook_secret(WEBHOOK_KEY, PROJECT_ID, BOT_ID)
        self.assertEqual(len(result), 64)
        self.assertEqual(result, webhook_secret(WEBHOOK_KEY, PROJECT_ID.upper(), BOT_ID))
        self.assertNotEqual(result, webhook_secret(WEBHOOK_KEY, OTHER_PROJECT_ID, BOT_ID))
        self.assertNotEqual(result, webhook_secret(WEBHOOK_KEY, PROJECT_ID, BOT_ID + 1))
        self.assertNotEqual(result, webhook_secret(b"x" * 32, PROJECT_ID, BOT_ID))
        self.assertNotIn(WEBHOOK_KEY.decode(), result)

    def test_secret_configuration_fails_closed(self):
        for key in [b"", b"x" * 31, b"x" * 513, "x" * 32, None]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                webhook_secret(key, PROJECT_ID, BOT_ID)
        for bot_id in [True, 0, -1, 2**63, "90001", None]:
            with self.subTest(bot_id=bot_id), self.assertRaises(ValueError):
                webhook_secret(WEBHOOK_KEY, PROJECT_ID, bot_id)
        with self.assertRaises(ValueError):
            webhook_secret(WEBHOOK_KEY, "not-a-project", BOT_ID)


class PartnerClientWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loader = AsyncMock(return_value=project())
        self.handler = AsyncMock()
        self.handler.handle.return_value = [{"method": "sendMessage", "payload": {"chat_id": 222, "text": "Hello"}}]
        self.client = await self.make_client()
        self.path = f"/partner-bots/{PROJECT_ID}/webhook"
        self.secret = webhook_secret(WEBHOOK_KEY, PROJECT_ID, BOT_ID)

    async def make_client(self, *, enabled=True, key=WEBHOOK_KEY, handler=None):
        app = FastAPI()
        app.include_router(create_client_router(self.loader, handler or self.handler, secret_key=key, enabled=enabled))
        return await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://offline.test"))

    async def post(self, body=None, *, secret=None, client=None, **kwargs):
        return await (client or self.client).post(self.path, json=update() if body is None else body,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret or self.secret}, **kwargs)

    def assert_no_secrets(self, response):
        for secret in [WEBHOOK_KEY.decode(), self.secret, "fake-bot-token", "SECRET-EXCEPTION"]:
            self.assertNotIn(secret, response.text)

    async def test_disabled_route_never_loads_registry_or_handles_update(self):
        client = await self.make_client(enabled=False)
        response = await self.post(client=client)
        self.assertEqual(response.status_code, 404)
        self.loader.assert_not_awaited()
        self.handler.handle.assert_not_awaited()
        self.assert_no_secrets(response)

    async def test_invalid_project_and_malformed_missing_auth_precede_database(self):
        response = await self.client.post("/partner-bots/invalid/webhook", json=update())
        self.assertEqual(response.status_code, 404)
        for secret in [None, "", "x", "a" * 63, "a" * 65, "G" * 64, "0" * 63 + "!", "A" * 64]:
            headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
            response = await self.client.post(self.path, content="not even JSON", headers=headers)
            self.assertEqual(response.status_code, 403, secret)
            self.assert_no_secrets(response)
        self.loader.assert_not_awaited()
        self.handler.handle.assert_not_awaited()

    async def test_valid_format_wrong_project_or_bot_secret_never_handles_or_reads_customer(self):
        for supplied in ["0" * 64, webhook_secret(WEBHOOK_KEY, OTHER_PROJECT_ID, BOT_ID),
                         webhook_secret(WEBHOOK_KEY, PROJECT_ID, BOT_ID + 1)]:
            response = await self.post(secret=supplied)
            self.assertEqual(response.status_code, 403)
            self.assert_no_secrets(response)
        self.handler.handle.assert_not_awaited()
        self.assertEqual(self.loader.await_count, 3)
        self.assertTrue(all(call.args == (PROJECT_ID,) for call in self.loader.await_args_list))

    async def test_active_project_and_matching_loaded_identity_are_required(self):
        for changes in [{"id": OTHER_PROJECT_ID}, {"status": "draft"}, {"status": "suspended"},
                        {"status": "closed"}, {"bot": {"id": None}}, {"bot": {"id": True}},
                        {"bot": {"id": "90001"}}, {"bot": None}]:
            self.loader.return_value = project(**changes)
            response = await self.post()
            self.assertEqual(response.status_code, 503, changes)
            self.assert_no_secrets(response)
        self.handler.handle.assert_not_awaited()
        self.loader.return_value = project(bot={"id": BOT_ID + 1})
        response = await self.post()
        self.assertEqual(response.status_code, 403)
        self.handler.handle.assert_not_awaited()

    async def test_short_secret_configuration_and_registry_failures_are_redacted(self):
        client = await self.make_client(key=b"too-short")
        response = await self.post(client=client)
        self.assertEqual(response.status_code, 503)
        self.assert_no_secrets(response)
        self.loader.side_effect = LookupError("SECRET-EXCEPTION fake-bot-token")
        response = await self.post()
        self.assertEqual(response.status_code, 404)
        self.assert_no_secrets(response)
        self.loader.side_effect = RuntimeError("SECRET-EXCEPTION postgres://user:password@host")
        response = await self.post()
        self.assertEqual(response.status_code, 503)
        self.assert_no_secrets(response)
        self.handler.handle.assert_not_awaited()

    async def test_valid_private_message_flows_to_handler_and_returns_one_api_operation(self):
        body = update()
        response = await self.post(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"method": "sendMessage", "chat_id": 222, "text": "Hello"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.loader.assert_awaited_once_with(PROJECT_ID)
        self.handler.handle.assert_awaited_once_with(project(), body)
        self.assert_no_secrets(response)

    async def test_callback_response_prefers_visible_screen_over_acknowledgement(self):
        self.handler.handle.return_value = [
            {"method": "answerCallbackQuery", "payload": {"callback_query_id": "x"}},
            {"method": "editMessageText", "payload": {"chat_id": 222, "message_id": 5, "text": "Menu"}},
        ]
        response = await self.post(callback())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["method"], "editMessageText")
        self.assertNotIn("callback_query_id", response.json())
        self.assertNotIn("payload", response.json())
        self.assertNotIn("token", response.json())

    async def test_empty_handler_results_acknowledge_supported_but_ignored_updates(self):
        self.handler.handle.return_value = []
        response = await self.post({"update_id": 102, "my_chat_member": {}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    async def test_content_type_json_shape_encoding_and_update_id_validation(self):
        for content_type in ["text/plain", "application/x-www-form-urlencoded", ""]:
            response = await self.client.post(self.path, content=json.dumps(update()), headers={
                "X-Telegram-Bot-Api-Secret-Token": self.secret, "Content-Type": content_type,
            })
            self.assertEqual(response.status_code, 415)
            self.assert_no_secrets(response)
        for content in ["{", "null", "[]", "{}", '{"update_id":true}', '{"update_id":-1}',
                        '{"update_id":"100"}', '{"update_id":9223372036854775808}', b"\xff"]:
            response = await self.client.post(self.path, content=content, headers={
                "X-Telegram-Bot-Api-Secret-Token": self.secret, "Content-Type": "application/json",
            })
            self.assertEqual(response.status_code, 400, content)
            self.assert_no_secrets(response)
        self.handler.handle.assert_not_awaited()
        response = await self.client.post(self.path, content=json.dumps(update()), headers={
            "X-Telegram-Bot-Api-Secret-Token": self.secret, "Content-Type": "application/json; charset=utf-8",
        })
        self.assertEqual(response.status_code, 200)

    async def test_body_limit_is_enforced_for_single_or_streamed_chunks(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": self.secret, "Content-Type": "application/json"}
        response = await self.client.post(self.path, content=b" " * (MAX_UPDATE_BYTES + 1), headers=headers)
        self.assertEqual(response.status_code, 413)

        async def body():
            for _ in range(5):
                yield b" " * (MAX_UPDATE_BYTES // 4)

        response = await self.client.post(self.path, content=body(), headers=headers)
        self.assertEqual(response.status_code, 413)
        self.handler.handle.assert_not_awaited()
        self.assert_no_secrets(response)

    async def test_bad_handler_commands_and_exceptions_fail_503_without_reflection(self):
        for value in [
            [{"method": "deleteMessage", "payload": {"chat_id": 222}}],
            [{"method": "sendMessage", "payload": {"token": "fake-bot-token"}}],
            [{"method": "sendMessage", "payload": {"method": "sendMessage"}}],
            [{"method": "sendMessage", "payload": "bad"}],
            [{"payload": {"text": "bad"}}],
            "bad",
        ]:
            self.handler.handle.return_value = value
            response = await self.post()
            self.assertEqual(response.status_code, 503, value)
            self.assert_no_secrets(response)
        self.handler.handle.side_effect = RuntimeError("SECRET-EXCEPTION fake-bot-token")
        response = await self.post()
        self.assertEqual(response.status_code, 503)
        self.assert_no_secrets(response)

    async def test_actual_handler_accepts_only_authenticated_private_customer_identity(self):
        store = AsyncMock()
        client = await self.make_client(handler=PartnerBotHandler(store))
        response = await self.post(client=client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["method"], "sendMessage")
        self.assertEqual(response.json()["chat_id"], 222)
        self.assertIn("Test VPN", response.json()["text"])
        store.register_customer.assert_awaited_once_with(PROJECT_ID, 222, referrer_tg_id=None)
        store.reset_mock()
        bad = update()
        bad["message"]["chat"]["id"] = 777
        for body in [bad, update(message={**update()["message"], "chat": {"id": 222, "type": "group"}}),
                     update(message={**update()["message"], "from": {"id": 222, "is_bot": True}})]:
            response = await self.post(body, client=client)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True})
        store.register_customer.assert_not_awaited()

    async def test_actual_handler_storage_failure_is_not_acknowledged_as_success(self):
        store = AsyncMock()
        store.register_customer.side_effect = RuntimeError("SECRET-EXCEPTION fake-bot-token DB unavailable")
        client = await self.make_client(handler=PartnerBotHandler(store))
        response = await self.post(client=client)
        self.assertEqual(response.status_code, 503)
        self.assert_no_secrets(response)

    async def test_actual_order_write_failure_returns_retryable_503(self):
        store = AsyncMock()
        store.create_order.side_effect = RuntimeError("SECRET-EXCEPTION fake-bot-token order write failed")
        client = await self.make_client(handler=PartnerBotHandler(store))
        review = render_purchase(project())
        confirmation = review["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        response = await self.post(callback(confirmation), client=client)
        self.assertEqual(response.status_code, 503)
        self.assert_no_secrets(response)
        store.create_order.assert_awaited_once()
        args = store.create_order.await_args.args
        self.assertEqual(args[:2], (PROJECT_ID, 222))
        self.assertEqual(args[3], 19900)

    async def test_invalid_callback_is_friendly_not_retryable_storage_failure(self):
        store = AsyncMock()
        client = await self.make_client(handler=PartnerBotHandler(store))
        for data in ["pay:not-a-uuid:19900", "renew:not-a-uuid", "check:not-a-uuid", "resume:bad"]:
            response = await self.post(callback(data), client=client)
            self.assertEqual(response.status_code, 200, data)
            self.assertEqual(response.json()["method"], "editMessageText")
            self.assertIn("Не удалось", response.json()["text"])
            self.assertNotIn(data, response.json()["text"])
        store.create_order.assert_not_awaited()
        store.get_order.assert_not_awaited()

    async def test_actual_handler_ignores_forwarded_business_and_group_identity(self):
        store = AsyncMock()
        client = await self.make_client(handler=PartnerBotHandler(store))
        bad_updates = [
            update(message={**update()["message"], "forward_origin": {"type": "user", "sender_user": {"id": 777}}}),
            update(message={**update()["message"], "forward_from": {"id": 777}}),
            update(message={**update()["message"], "business_connection_id": "x"}),
            update(message={**update()["message"], "sender_chat": {"id": 777}}),
            update(message={**update()["message"], "chat": {"id": 222, "type": "group"}}),
        ]
        wrong_bot_callback = callback()
        wrong_bot_callback["callback_query"]["message"]["from"]["id"] = BOT_ID + 1
        bad_updates.append(wrong_bot_callback)
        for body in bad_updates:
            response = await self.post(body, client=client)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True})
        store.register_customer.assert_not_awaited()

    async def test_actual_callback_returns_one_private_edit_not_two_operations(self):
        store = AsyncMock()
        client = await self.make_client(handler=PartnerBotHandler(store))
        response = await self.post(callback(), client=client)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["method"], "editMessageText")
        self.assertEqual(result["chat_id"], 222)
        self.assertEqual(result["message_id"], 5)
        self.assertNotIn("callback_query_id", result)
        self.assertNotIn("token", result)
        store.register_customer.assert_awaited_once_with(PROJECT_ID, 222, referrer_tg_id=None)


if __name__ == "__main__":
    unittest.main()
