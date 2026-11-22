"""After-launch notice contracts; all Telegram/database ports are local fakes."""
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from services.partner_entry_notifications import (
    ENTRY_CREATED_BUTTON, ENTRY_CREATED_TEXT, EntryLaunchNotificationService,
    EntryLaunchNotificationStore, TelegramEntryLaunchNotificationAdapter,
)
from services.partner_notifications import NotificationResult
from test_partner_remnawave import Factory, Response


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
MANAGER = "987654:" + "y" * 32


def row(**changes):
    return {"id": 1, "project_id": str(uuid4()), "order_id": str(uuid4()),
            "owner_tg_id": 42, "recipient_id": 42, "price_kopecks": 999000,
            "bot_id": 123456, "bot_username": "OwnedTenantBot", "channel": "partner",
            "lease_token": str(uuid4()), **changes}


class EntryLaunchNoticeTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, notice=None, enabled=True):
        store = type("Store", (), {"enabled": True})()
        store.enqueue_completed = AsyncMock(return_value=1)
        store.claim = AsyncMock(side_effect=[notice or row(), None])
        store.begin_send = AsyncMock(return_value=True)
        store.finish = AsyncMock(return_value=True)
        sender = type("Sender", (), {})()
        sender.verify = AsyncMock()
        sender.send = AsyncMock(return_value=NotificationResult("sent", 99))
        service = EntryLaunchNotificationService(store, sender, manager_bot_token=MANAGER, enabled=enabled, clock=lambda: NOW)
        return service, store, sender

    async def test_disabled_has_no_database_or_telegram_side_effects(self):
        service, store, sender = self.service(enabled=False)
        self.assertFalse((await service.recover_once())["enabled"])
        store.enqueue_completed.assert_not_awaited()
        store.claim.assert_not_awaited()
        sender.verify.assert_not_awaited()
        sender.send.assert_not_awaited()
        forbidden = lambda: self.fail("disabled store must not acquire a database")
        self.assertEqual(await EntryLaunchNotificationStore(forbidden).enqueue_completed(), 0)

    async def test_owner_receives_only_manager_bot_message_and_server_resolved_tenant_url(self):
        service, store, sender = self.service()
        counts = await service.recover_once(limit=2)
        self.assertEqual((counts["queued"], counts["checked"], counts["sent"]), (1, 1, 1))
        sender.verify.assert_awaited_once_with(MANAGER, 987654)
        sender.send.assert_awaited_once_with(MANAGER, bot_id=987654, chat_id=42,
            text=ENTRY_CREATED_TEXT, bot_url="https://t.me/OwnedTenantBot")
        store.begin_send.assert_awaited_once()
        self.assertEqual(store.finish.await_args.args[1], NotificationResult("sent", 99))

    async def test_invalid_owner_price_or_username_never_sends(self):
        for changes in ({"recipient_id": 99}, {"owner_tg_id": True},
                        {"price_kopecks": 0}, {"price_kopecks": 999}, {"price_kopecks": 1001},
                        {"price_kopecks": True}, {"price_kopecks": 1000.0},
                        {"bot_username": "https://evil.example/token"}, {"bot_username": "Bad/Name"}):
            service, store, sender = self.service(notice=row(**changes))
            counts = await service.recover_once(limit=1)
            self.assertEqual(counts["blocked"], 1)
            sender.verify.assert_not_awaited()
            sender.send.assert_not_awaited()
            store.begin_send.assert_not_awaited()

    async def test_saved_public_trial_receipt_notifies_its_exact_owner_after_flag_changes(self):
        # Delivery reads a committed paid/launched outbox item, not the current
        # pricing switch. Historical 10 RUB orders are valid for every owner.
        notice = row(owner_tg_id=42, recipient_id=42, price_kopecks=1000)
        service, store, sender = self.service(notice=notice)
        counts = await service.recover_once(limit=1)
        self.assertEqual((counts['sent'], counts['blocked']), (1, 0))
        sender.send.assert_awaited_once_with(MANAGER, bot_id=987654, chat_id=42,
            text=ENTRY_CREATED_TEXT, bot_url='https://t.me/OwnedTenantBot')
        self.assertEqual(store.begin_send.await_args.args[0], notice)
        for changes in ({'recipient_id': 99}, {'owner_tg_id': 99}):
            service, store, sender = self.service(notice={**notice, **changes})
            counts = await service.recover_once(limit=1)
            self.assertEqual(counts['blocked'], 1)
            sender.verify.assert_not_awaited()
            sender.send.assert_not_awaited()
            store.begin_send.assert_not_awaited()

    async def test_verify_failure_retries_before_send_intent_and_unknown_send_is_retained(self):
        service, store, sender = self.service()
        sender.verify.side_effect = RuntimeError("private-token-must-not-leak")
        counts = await service.recover_once(limit=1)
        self.assertEqual(counts["retry"], 1)
        store.begin_send.assert_not_awaited()
        sender.send.assert_not_awaited()
        service, store, sender = self.service()
        sender.send.side_effect = TimeoutError("private-token-must-not-leak")
        counts = await service.recover_once(limit=1)
        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual(store.finish.await_args.args[1], NotificationResult("uncertain"))
        self.assertNotIn("private", str(counts))

    async def test_lost_ack_does_not_perform_a_second_send_within_pass(self):
        service, store, sender = self.service()
        store.finish.side_effect = RuntimeError("lost database acknowledgement")
        counts = await service.recover_once(limit=2)
        self.assertEqual(counts["errors"], 1)
        sender.send.assert_awaited_once()

    async def test_adapter_sends_fixed_url_keyboard_and_checks_private_chat_readback(self):
        body = {"ok": True, "result": {"message_id": 99, "chat": {"id": 42, "type": "private"},
                                          "from": {"id": 987654, "is_bot": True}}}
        factory = Factory([Response(body)])
        result = await TelegramEntryLaunchNotificationAdapter(session_factory=factory).send(
            MANAGER, bot_id=987654, chat_id=42, text=ENTRY_CREATED_TEXT, bot_url="https://t.me/OwnedTenantBot")
        self.assertEqual(result, NotificationResult("sent", 99))
        request = factory.requests[0]
        self.assertEqual(request[0], "POST")
        self.assertFalse(request[2]["allow_redirects"])
        self.assertEqual(request[2]["json"]["reply_markup"], {"inline_keyboard": [[{
            "text": ENTRY_CREATED_BUTTON, "url": "https://t.me/OwnedTenantBot"}]]})
        self.assertEqual(len(factory.requests), 1)

    async def test_adapter_known_rate_limit_vs_unknown_delivery_and_unsafe_url(self):
        for response, expected in ((Response({"ok": False, "error_code": 429, "parameters": {"retry_after": 90}}, 429), "retry"),
                                   (Response({"ok": False, "error_code": 403}, 403), "blocked"),
                                   (TimeoutError(), "uncertain")):
            factory = Factory([response])
            result = await TelegramEntryLaunchNotificationAdapter(session_factory=factory).send(
                MANAGER, bot_id=987654, chat_id=42, text=ENTRY_CREATED_TEXT, bot_url="https://t.me/OwnedTenantBot")
            self.assertEqual(result.state, expected)
        factory = Factory([])
        result = await TelegramEntryLaunchNotificationAdapter(session_factory=factory).send(
            MANAGER, bot_id=987654, chat_id=42, text=ENTRY_CREATED_TEXT, bot_url="https://t.me.evil.example/OwnedTenantBot")
        self.assertEqual(result.state, "uncertain")
        self.assertEqual(factory.requests, [])


if __name__ == "__main__": unittest.main()
