"""Offline notification transport/worker contracts, no Telegram calls."""

from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from test_partner_remnawave import Factory, Response
from services.partner_notifications import (
    NotificationResult,
    NotificationTransportError,
    PartnerNotificationService,
    TelegramNotificationAdapter,
)

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
TOKEN = "123456:" + "x" * 32
MANAGER = "987654:" + "y" * 32


def sent_body(chat_id=100, bot_id=123456):
    return {
        "ok": True,
        "result": {
            "message_id": 99,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": bot_id, "is_bot": True},
        },
    }


class NotificationAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_verified_then_fixed_host_send_and_private_readback(self):
        factory = Factory(
            [
                Response({"ok": True, "result": {"id": 123456, "is_bot": True}}),
                Response(sent_body()),
            ]
        )
        adapter = TelegramNotificationAdapter(session_factory=factory)
        self.assertEqual(factory.requests, [])
        await adapter.verify(TOKEN, 123456)
        result = await adapter.send(
            TOKEN, bot_id=123456, chat_id=100, text="Оплата получена."
        )
        self.assertEqual(result, NotificationResult("sent", 99))
        self.assertEqual([r[0] for r in factory.requests], ["GET", "POST"])
        self.assertTrue(
            all(
                r[1].startswith("https://api.telegram.org/bot")
                and not r[2]["allow_redirects"]
                for r in factory.requests
            )
        )
        self.assertTrue(all(not opt["trust_env"] for opt in factory.options))
        self.assertNotIn(TOKEN, repr(adapter))

    async def test_known_rejection_vs_ambiguous_delivery_not_retried_by_adapter(self):
        cases = [
            (
                Response(
                    {"ok": False, "error_code": 429, "parameters": {"retry_after": 90}},
                    429,
                ),
                "retry",
            ),
            (Response({"ok": False, "error_code": 403}, 403), "blocked"),
            (Response({"ok": False, "error_code": 500}, 500), "uncertain"),
            (TimeoutError(TOKEN), "uncertain"),
            (Response(sent_body(chat_id=999)), "uncertain"),
            (Response({}, 302), "uncertain"),
        ]
        for response, state in cases:
            factory = Factory([response])
            adapter = TelegramNotificationAdapter(session_factory=factory)
            result = await adapter.send(
                TOKEN, bot_id=123456, chat_id=100, text="Оплата получена."
            )
            self.assertEqual(result.state, state)
            self.assertEqual(len(factory.requests), 1)
            self.assertNotIn(TOKEN, str(result))

    async def test_bad_bot_and_invalid_json_cannot_be_confirmed_delivered(self):
        adapter = TelegramNotificationAdapter(session_factory=Factory([]))
        with self.assertRaises(NotificationTransportError):
            await adapter.verify(TOKEN, 555555)
        for raw in (b'{"ok":true,"ok":false}', b"x" * 65537, b"[]"):
            factory = Factory([Response({}, raw=raw)])
            result = await TelegramNotificationAdapter(session_factory=factory).send(
                TOKEN, bot_id=123456, chat_id=100, text="Оплата."
            )
            self.assertEqual(result.state, "uncertain")


class NotificationWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row = {
            "id": 1,
            "project_id": str(uuid4()),
            "order_id": str(uuid4()),
            "recipient_id": 100,
            "bot_id": 123456,
            "encrypted_token": "local-ciphertext",
            "price_kopecks": 19900,
            "purpose": "subscription",
            "order_status": "paid_pending_activation",
            "activation_issue": None,
            "channel": "customer",
            "lease_token": str(uuid4()),
        }
        self.store = type("Store", (), {"enabled": True})()
        self.store.claim = AsyncMock(side_effect=[self.row, None])
        self.store.begin_send = AsyncMock(return_value=True)
        self.store.finish = AsyncMock(return_value=True)
        self.sender = type("Sender", (), {})()
        self.sender.verify = AsyncMock()
        self.sender.send = AsyncMock(return_value=NotificationResult("sent", 99))
        self.vault = Mock()
        self.vault.decrypt.return_value = TOKEN

    def service(self, **kwargs):
        return PartnerNotificationService(
            self.store,
            self.sender,
            vault=self.vault,
            enabled=True,
            clock=lambda: NOW,
            **kwargs
        )

    async def test_disabled_service_never_reads_queue_or_sends(self):
        result = await PartnerNotificationService(
            self.store, self.sender, vault=self.vault
        ).recover_once()
        self.assertFalse(result["enabled"])
        self.store.claim.assert_not_awaited()
        self.sender.send.assert_not_awaited()

    async def test_correct_tenant_and_verified_bot_send_only_after_intent(self):
        async def send(*args, **kwargs):
            self.store.begin_send.assert_awaited_once()
            return NotificationResult("sent", 99)

        self.sender.send.side_effect = send
        result = await self.service().recover_once(limit=2)
        self.assertEqual((result["sent"], result["errors"]), (1, 0))
        self.vault.decrypt.assert_called_once_with(
            self.row["project_id"], 123456, "local-ciphertext"
        )
        self.sender.verify.assert_awaited_once_with(TOKEN, 123456)
        text = self.sender.send.await_args.kwargs["text"]
        self.assertIn("Оплата 199 ₽ получена", text)
        self.assertNotIn("пригла", text.lower())
        self.assertNotIn("активна", text.lower())

    async def test_preflight_failure_can_retry_but_never_sends(self):
        self.sender.verify.side_effect = TimeoutError(TOKEN)
        result = await self.service().recover_once(limit=1)
        self.assertEqual(result["retry"], 1)
        self.store.begin_send.assert_not_awaited()
        self.sender.send.assert_not_awaited()
        self.assertEqual(self.store.finish.await_args.args[1].state, "retry")

    async def test_lost_post_response_is_uncertain_not_a_new_send(self):
        self.sender.send.side_effect = TimeoutError(TOKEN)
        result = await self.service().recover_once(limit=1)
        self.assertEqual(result["uncertain"], 1)
        self.assertEqual(self.store.finish.await_args.args[1].state, "uncertain")

    async def test_expired_lease_and_wrong_ciphertext_never_send(self):
        self.store.begin_send.return_value = False
        await self.service().recover_once(limit=1)
        self.sender.send.assert_not_awaited()
        self.store.claim = AsyncMock(return_value=self.row)
        self.vault.decrypt.side_effect = ValueError("private ciphertext")
        result = await self.service().recover_once(limit=1)
        self.assertEqual(result["blocked"], 1)
        self.sender.send.assert_not_awaited()

    async def test_sql_failure_after_send_does_not_trigger_another_send(self):
        self.store.finish.side_effect = RuntimeError("DB down")
        result = await self.service().recover_once(limit=1)
        self.assertEqual(result["errors"], 1)
        self.sender.send.assert_awaited_once()
