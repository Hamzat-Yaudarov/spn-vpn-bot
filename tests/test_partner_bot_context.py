"""Fresh one-query navigation, not an authorization or money-state cache."""
import asyncio
import json
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import FastAPI

from partner_client_web import create_client_router, webhook_secret
from services.partner_bot import PartnerBotHandler
from services.partner_management import ManagementDenied
from services.partner_store import BotUpdateContext, InvalidProject, PartnerStore
from test_partner_bot import PROJECT_ID, buttons, callback, payload, project
from test_partner_store import RecordingConnection, project_row


KEY = b"offline-context-webhook-key-32-bytes-minimum"


def row(**changes):
    return project_row(**{
        "id": UUID(PROJECT_ID), "owner_tg_id": 777, "bot_id": 90001,
        "bot_username": "partner_test_bot", "status": "active",
        "customer_registered": True, "attribution_frozen": True, "staff_role": None,
        **changes,
    })


class BotContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_navigation_is_one_fresh_select_and_no_registration_or_role_query(self):
        for action in ("menu", "help", "connect", "connect:android", "connect:iphone"):
            with self.subTest(action=action):
                conn = RecordingConnection(rows=[row()])
                registry = PartnerStore(lambda: conn)
                store, management = AsyncMock(), AsyncMock()
                loaded = await registry.get_bot_update_context(PROJECT_ID, 222)
                result = await PartnerBotHandler(store, management=management).handle(loaded, callback(action))
                self.assertTrue(payload(result)["text"])
                self.assertEqual(len(conn.calls), 1)
                self.assertEqual(conn.calls[0][0], "fetchrow")
                self.assertEqual(conn.calls[0][2], (UUID(PROJECT_ID), 222))
                self.assertIn("m.active", conn.calls[0][1])
                self.assertNotIn("encrypted_token", conn.calls[0][1])
                self.assertNotIn("FOR UPDATE", conn.calls[0][1])
                store.register_customer.assert_not_awaited()
                management.register_customer.assert_not_awaited()
                management.authorize.assert_not_awaited()
                self.assertNotIn("secret-must-never-leak", str(result))

    async def test_no_user_hint_never_creates_a_receipt(self):
        conn = RecordingConnection(rows=[row()])
        result = await PartnerStore(lambda: conn).get_bot_update_context(PROJECT_ID)
        self.assertNotIn("_bot_update_context", result)
        self.assertEqual(conn.calls[0][2], (UUID(PROJECT_ID), None))
        for bad in (True, 0, -1, "222", 2**63):
            with self.assertRaises(InvalidProject):
                await PartnerStore(lambda: conn).get_bot_update_context(PROJECT_ID, bad)
        self.assertEqual(len(conn.calls), 1)

    async def test_new_or_legacy_customer_keeps_locked_registration_and_referral_arguments(self):
        for registered, frozen in ((False, False), (True, False), (False, True)):
            conn = RecordingConnection(rows=[row(customer_registered=registered, attribution_frozen=frozen)])
            loaded = await PartnerStore(lambda: conn).get_bot_update_context(PROJECT_ID, 222)
            management = AsyncMock()
            await PartnerBotHandler(AsyncMock(), management=management).handle(loaded, callback("connect"))
            management.register_customer.assert_awaited_once_with(
                PROJECT_ID, 222, referrer_tg_id=None, campaign_code=None,
            )

    async def test_forged_or_cross_identity_receipts_never_skip_registration_or_staff_read(self):
        base = project(status="active", version=1)
        valid = BotUpdateContext(PROJECT_ID, 90001, 222, 1, True, True, "owner")
        for receipt in (
            {"registered": True, "staff_role": "owner"},
            replace(valid, project_id="132e380c-e519-4d50-885a-58e4dfb35a49"),
            replace(valid, bot_id=90002), replace(valid, user_id=333),
            replace(valid, project_version=2),
        ):
            management = AsyncMock()
            management.authorize.side_effect = ManagementDenied()
            result = await PartnerBotHandler(AsyncMock(), management=management).handle(
                {**base, "_bot_update_context": receipt}, callback("menu"),
            )
            management.register_customer.assert_awaited_once()
            management.authorize.assert_awaited_once()
            self.assertFalse(any(b.get("callback_data") == "admin" for b in buttons(payload(result))))

    async def test_staff_revocation_and_price_change_are_fresh_on_next_click(self):
        conn = RecordingConnection(rows=[row(staff_role="support"), row(staff_role=None, price_kopecks=29900)])
        registry = PartnerStore(lambda: conn)
        management = AsyncMock()
        handler = PartnerBotHandler(AsyncMock(), management=management)
        first = await registry.get_bot_update_context(PROJECT_ID, 222)
        result = await handler.handle(first, callback("menu"))
        self.assertTrue(any(b.get("callback_data") == "admin" for b in buttons(payload(result))))
        current = await registry.get_bot_update_context(PROJECT_ID, 222)
        result = await handler.handle(current, callback("menu"))
        self.assertFalse(any(b.get("callback_data") == "admin" for b in buttons(payload(result))))
        self.assertEqual(current["price_kopecks"], 29900)
        self.assertEqual(len(conn.calls), 2)
        management.authorize.assert_not_awaited()

    async def test_buy_existing_subscription_does_not_repeat_same_request_reads(self):
        store = AsyncMock()
        store.list_subscriptions.return_value = [{
            "id": "c7bbbd4b-17b5-43c4-99e1-d7cad9b372ea",
            "project_id": PROJECT_ID, "tg_id": 222,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
            "subscription_url": "https://sub.example.test/private-link",
        }]
        store.get_pending_order.return_value = None
        loaded = await PartnerStore(lambda: RecordingConnection(rows=[row()])).get_bot_update_context(PROJECT_ID, 222)
        result = await PartnerBotHandler(store).handle(loaded, callback("buy"))
        self.assertIn("Подписка активна", payload(result)["text"])
        store.list_subscriptions.assert_awaited_once_with(PROJECT_ID, 222)
        store.get_pending_order.assert_awaited_once_with(PROJECT_ID, 222)
        store.register_customer.assert_not_awaited()

    async def test_empty_subscription_list_skips_pending_read_and_keeps_exact_user_project(self):
        async def subscriptions(*args):
            self.assertEqual(args, (PROJECT_ID, 222))
            return []
        store = AsyncMock()
        store.list_subscriptions.side_effect = subscriptions
        loaded = await PartnerStore(lambda: RecordingConnection(rows=[row()])).get_bot_update_context(PROJECT_ID, 222)
        result = await PartnerBotHandler(store).handle(loaded, callback("subscriptions"))
        self.assertIn("У вас пока нет подписки", payload(result)["text"])
        store.list_subscriptions.assert_awaited_once()
        store.get_pending_order.assert_not_awaited()

    async def test_operator_link_requires_configured_exact_operator_and_only_opens_central_bot(self):
        handler = PartnerBotHandler(AsyncMock(), operator_ids=(222,), manager_username="central_test_bot")
        result = await handler.handle(project(), callback("menu"))
        link = next(b for b in buttons(payload(result)) if b["text"] == "🛠 Управление проектом")
        self.assertEqual(link["url"], f"https://t.me/central_test_bot?start=support_{PROJECT_ID}")
        self.assertNotIn("web_app", link)
        self.assertFalse(any(b.get("callback_data") == "admin" for b in buttons(payload(result))))
        result = await handler.handle(project(), callback("menu", user_id=333))
        self.assertFalse(any(b["text"] == "🛠 Управление проектом" for b in buttons(payload(result))))

    async def test_route_uses_context_only_after_bounded_json_and_still_verifies_full_identity(self):
        loader = AsyncMock()
        context_loader = AsyncMock(return_value=project(status="active", version=1))
        handler = AsyncMock()
        handler.handle.return_value = []
        app = FastAPI()
        app.include_router(create_client_router(loader, handler, secret_key=KEY, enabled=True,
                                               context_loader=context_loader))
        import httpx
        path = f"/partner-bots/{PROJECT_ID}/webhook"
        headers = {"X-Telegram-Bot-Api-Secret-Token": webhook_secret(KEY, PROJECT_ID, 90001)}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://offline.test") as client:
            result = await client.post(path, json=callback("menu"), headers=headers)
            self.assertEqual(result.status_code, 200)
            context_loader.assert_awaited_once_with(PROJECT_ID, 222)
            loader.assert_not_awaited()
            handler.handle.assert_awaited_once()
            handler.handle.reset_mock()
            result = await client.post(path, json=callback("menu"), headers={
                "X-Telegram-Bot-Api-Secret-Token": "0" * 64,
            })
            self.assertEqual(result.status_code, 403)
            handler.handle.assert_not_awaited()

    async def test_three_delayed_db_roundtrips_become_one_for_registered_menu(self):
        delay = 0.04
        async def delayed(*args, **kwargs):
            await asyncio.sleep(delay)

        management = AsyncMock()
        management.register_customer.side_effect = delayed
        management.authorize.side_effect = delayed
        handler = PartnerBotHandler(AsyncMock(), management=management)
        started = time.perf_counter()
        await delayed()  # Old standalone project fetch.
        await handler.handle(project(version=1), callback("menu"))
        old = time.perf_counter() - started
        conn = RecordingConnection(rows=[row()])
        original = conn.fetchrow
        async def delayed_fetch(*args):
            await delayed()
            return await original(*args)
        conn.fetchrow = delayed_fetch
        started = time.perf_counter()
        current = await PartnerStore(lambda: conn).get_bot_update_context(PROJECT_ID, 222)
        await handler.handle(current, callback("menu"))
        new = time.perf_counter() - started
        self.assertLess(new, old / 2, (old, new))
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(management.register_customer.await_count, 1)
        self.assertEqual(management.authorize.await_count, 1)


class CallbackBodyLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_asgi_body_is_flushed_before_slow_ack_finishes(self):
        ack_started, allow_ack_finish, body_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def ack(*args):
            ack_started.set()
            await allow_ack_finish.wait()
        async def handle(*args):
            await ack_started.wait()
            return [{"method": "editMessageText", "payload": {"chat_id": 222, "message_id": 10, "text": "Ready"}}]
        handler = AsyncMock()
        handler.handle.side_effect = handle
        app = FastAPI()
        app.include_router(create_client_router(
            AsyncMock(return_value=project(status="active")), handler,
            secret_key=KEY, enabled=True, callback_acknowledger=ack,
        ))
        raw = json.dumps(callback("menu")).encode()
        messages = []
        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                body_sent.set()
        path = f"/partner-bots/{PROJECT_ID}/webhook"
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"x-telegram-bot-api-secret-token",
                webhook_secret(KEY, PROJECT_ID, 90001).encode())],
            "server": ("offline.test", 443), "client": ("127.0.0.1", 1234), "root_path": "",
        }
        request = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(body_sent.wait(), 0.25)
            self.assertFalse(request.done())  # The response still owns UI-only background work.
            body = next(m["body"] for m in messages if m["type"] == "http.response.body")
            self.assertEqual(json.loads(body)["text"], "Ready")
        finally:
            allow_ack_finish.set()
            await asyncio.wait_for(request, 0.25)


if __name__ == "__main__":
    unittest.main()
