import json
import unittest
from unittest.mock import ANY, AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

import utils
from services import notification_delivery, remnawave, subscription_notifications, traffic_resets


class _FakeResponse:
    def __init__(self, status: int, body: dict | str):
        self.status = status
        self._body = json.dumps(body) if isinstance(body, dict) else body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self._body


class _FakeRemnawaveSession:
    def __init__(self, factory):
        self._factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def patch(self, *args, **kwargs):
        self._factory.patch_calls += 1
        return _FakeResponse(self._factory.status, self._factory.body)


class _FakeRemnawaveSessionFactory:
    def __init__(self, status: int, body: dict | str):
        self.status = status
        self.body = body
        self.patch_calls = 0

    def __call__(self, *args, **kwargs):
        return _FakeRemnawaveSession(self)


class _FakeContextSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class RemnawaveMissingUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_a025_is_success_only_for_idempotent_cleanup(self):
        factory = _FakeRemnawaveSessionFactory(
            404,
            {"message": "User not found", "errorCode": "A025"},
        )

        with patch("services.remnawave.aiohttp.ClientSession", side_effect=factory):
            result = await remnawave.remnawave_update_user_profile(
                object(),
                "missing-uuid",
                traffic_limit_bytes=0,
                traffic_limit_strategy="NO_RESET",
                missing_user_is_success=True,
            )

        self.assertTrue(result)
        self.assertEqual(factory.patch_calls, 1)

    async def test_a025_remains_failure_for_normal_updates(self):
        factory = _FakeRemnawaveSessionFactory(
            404,
            {"message": "User not found", "errorCode": "A025"},
        )

        with (
            patch("services.remnawave.aiohttp.ClientSession", side_effect=factory),
            patch("utils.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await remnawave.remnawave_update_user_profile(
                object(),
                "missing-uuid",
                expire_at=None,
            )

        self.assertFalse(result)
        self.assertEqual(factory.patch_calls, utils.API_RETRY_ATTEMPTS)

    async def test_unknown_404_is_not_treated_as_success(self):
        factory = _FakeRemnawaveSessionFactory(
            404,
            {"message": "Not found", "errorCode": "UNKNOWN"},
        )

        with (
            patch("services.remnawave.aiohttp.ClientSession", side_effect=factory),
            patch("utils.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await remnawave.remnawave_update_user_profile(
                object(),
                "unknown-uuid",
                missing_user_is_success=True,
            )

        self.assertFalse(result)
        self.assertEqual(factory.patch_calls, utils.API_RETRY_ATTEMPTS)


class LegacyCleanupTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.traffic_resets.aiohttp.TCPConnector")
    @patch("services.traffic_resets.aiohttp.ClientSession", return_value=_FakeContextSession())
    @patch("services.traffic_resets.db.mark_legacy_subscription_limit_removed", new_callable=AsyncMock)
    @patch("services.traffic_resets.remnawave_update_user_profile", new_callable=AsyncMock)
    @patch("services.traffic_resets.db.get_legacy_subscriptions_pending_limit_removal", new_callable=AsyncMock)
    async def test_missing_legacy_user_is_marked_processed(
        self,
        get_pending,
        update_profile,
        mark_processed,
        _client_session,
        _connector,
    ):
        get_pending.return_value = [
            {"id": 62, "remnawave_uuid": "missing-uuid"},
        ]
        update_profile.return_value = True

        await traffic_resets.process_pending_legacy_limit_removals()

        update_profile.assert_awaited_once_with(
            ANY,
            "missing-uuid",
            traffic_limit_bytes=0,
            traffic_limit_strategy="NO_RESET",
            missing_user_is_success=True,
        )
        mark_processed.assert_awaited_once_with(62)

    @patch("services.traffic_resets.aiohttp.TCPConnector")
    @patch("services.traffic_resets.aiohttp.ClientSession", return_value=_FakeContextSession())
    @patch("services.traffic_resets.db.mark_legacy_subscription_limit_removed", new_callable=AsyncMock)
    @patch("services.traffic_resets.remnawave_update_user_profile", new_callable=AsyncMock)
    @patch("services.traffic_resets.db.get_legacy_subscriptions_pending_limit_removal", new_callable=AsyncMock)
    async def test_transient_failure_stays_pending(
        self,
        get_pending,
        update_profile,
        mark_processed,
        _client_session,
        _connector,
    ):
        get_pending.return_value = [
            {"id": 63, "remnawave_uuid": "temporarily-unavailable-uuid"},
        ]
        update_profile.return_value = False

        await traffic_resets.process_pending_legacy_limit_removals()

        mark_processed.assert_not_awaited()


class NotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.subscription_notifications.is_telegram_delivery_blocked", new_callable=AsyncMock)
    async def test_known_unreachable_chat_is_skipped(self, is_blocked):
        is_blocked.return_value = True
        bot = AsyncMock()

        result = await subscription_notifications._send_message(bot, 1001, "test", None)

        self.assertFalse(result)
        bot.send_message.assert_not_awaited()

    @patch("services.subscription_notifications.mark_telegram_delivery_blocked", new_callable=AsyncMock)
    @patch("services.subscription_notifications.is_telegram_delivery_blocked", new_callable=AsyncMock)
    async def test_chat_not_found_is_blocked_until_next_start(self, is_blocked, mark_blocked):
        is_blocked.return_value = False
        bot = AsyncMock()
        bot.send_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=1002, text="test"),
            message="Bad Request: chat not found",
        )

        result = await subscription_notifications._send_message(bot, 1002, "test", None)

        self.assertFalse(result)
        mark_blocked.assert_awaited_once_with(1002)

    @patch("services.notification_delivery.db.db_execute", new_callable=AsyncMock)
    async def test_start_can_clear_delivery_block(self, execute):
        await notification_delivery.clear_telegram_delivery_blocked(1003)

        query, params = execute.await_args.args[:2]
        self.assertIn("DELETE FROM notification_state", query)
        self.assertEqual(
            params,
            (1003, notification_delivery.TELEGRAM_DELIVERY_BLOCKED_NOTIFICATION_TYPE),
        )


if __name__ == "__main__":
    unittest.main()
