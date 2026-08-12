import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from handlers import callbacks
from services import channel_bonus


def _callback(status: str = "member"):
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status=status)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=777),
        delete=AsyncMock(),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123, username="new_user"),
        message=message,
        bot=bot,
        answer=AsyncMock(),
    )


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


class NewUserCohortTests(unittest.IsolatedAsyncioTestCase):
    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_create_user_marks_only_explicit_new_cohort(self, execute):
        await database.create_user(
            123,
            "new_user",
            require_news_channel_onboarding=True,
        )

        query, params = execute.await_args.args[:2]
        self.assertIn("news_channel_onboarding_required", query)
        self.assertEqual(params[-1], True)
        update_clause = query.split("ON CONFLICT", 1)[1]
        self.assertNotIn("news_channel_onboarding_required =", update_clause)

    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_non_start_creation_is_not_added_to_new_cohort(self, execute):
        await database.create_user(124, "admin_created")

        self.assertEqual(execute.await_args.args[1][-1], False)


class ChannelBonusPreparationTests(unittest.IsolatedAsyncioTestCase):
    @patch("database.get_pool", new_callable=AsyncMock)
    async def test_pending_subscription_stays_hidden_until_activation(self, get_pool):
        connection = SimpleNamespace()
        connection.transaction = lambda: _AsyncContext()
        connection.fetch = AsyncMock(return_value=[])
        connection.execute = AsyncMock()
        connection.fetchrow = AsyncMock(side_effect=[
            {
                "news_channel_onboarding_required": True,
                "news_channel_bonus_claimed_at": None,
                "news_channel_bonus_subscription_id": None,
            },
            {"id": 55, "subscription_until": datetime.utcnow() + timedelta(days=1)},
        ])
        get_pool.return_value = _Pool(connection)

        await database.prepare_news_channel_bonus_subscription(123)

        insert_query = connection.fetchrow.await_args_list[1].args[0]
        insert_args = connection.fetchrow.await_args_list[1].args
        self.assertIn("FALSE, 'bypass'", insert_query)
        self.assertIn("'v2', FALSE, FALSE", insert_query)
        self.assertIn("TRUE, $5, 0, 0, $5", insert_query)
        self.assertEqual(insert_args[5], database.BYPASS_BASE_TRAFFIC_GB * database.GB_BYTES)
        self.assertEqual(insert_args[7], database.BYPASS_HWID_DEVICE_LIMIT)

    @patch("database.get_pool", new_callable=AsyncMock)
    async def test_retry_refreshes_bonus_to_full_day(self, get_pool):
        old_expiry = datetime.utcnow() + timedelta(minutes=5)
        refreshed = {"id": 55, "subscription_until": datetime.utcnow() + timedelta(days=1)}
        connection = SimpleNamespace()
        connection.transaction = lambda: _AsyncContext()
        connection.fetch = AsyncMock(return_value=[])
        connection.execute = AsyncMock()
        connection.fetchrow = AsyncMock(side_effect=[
            {
                "news_channel_onboarding_required": True,
                "news_channel_bonus_claimed_at": None,
                "news_channel_bonus_subscription_id": 55,
            },
            {"id": 55, "subscription_until": old_expiry},
            refreshed,
        ])
        get_pool.return_value = _Pool(connection)

        await database.prepare_news_channel_bonus_subscription(123)

        update_args = connection.fetchrow.await_args_list[2].args
        update_query = update_args[0]
        self.assertGreater(update_args[3], datetime.utcnow() + timedelta(hours=23, minutes=55))
        self.assertIn("plan_kind = 'bypass'", update_query)
        self.assertIn("type_index = $9", update_query)
        self.assertIn("traffic_enabled = TRUE", update_query)
        self.assertEqual(update_args[6], database.BYPASS_BASE_TRAFFIC_GB * database.GB_BYTES)
        self.assertEqual(update_args[8], database.BYPASS_HWID_DEVICE_LIMIT)
        self.assertEqual(update_args[9], 1)

    @patch("database.get_pool", new_callable=AsyncMock)
    async def test_retry_chooses_free_bypass_index(self, get_pool):
        connection = SimpleNamespace()
        connection.transaction = lambda: _AsyncContext()
        connection.fetch = AsyncMock(return_value=[{"type_index": 1}])
        connection.execute = AsyncMock()
        connection.fetchrow = AsyncMock(side_effect=[
            {
                "news_channel_onboarding_required": True,
                "news_channel_bonus_claimed_at": None,
                "news_channel_bonus_subscription_id": 55,
            },
            {
                "id": 55,
                "plan_kind": "regular",
                "type_index": 1,
                "subscription_until": datetime.utcnow() + timedelta(minutes=5),
            },
            {"id": 55, "plan_kind": "bypass", "type_index": 2},
        ])
        get_pool.return_value = _Pool(connection)

        subscription = await database.prepare_news_channel_bonus_subscription(123)

        self.assertEqual(subscription["type_index"], 2)
        update_args = connection.fetchrow.await_args_list[2].args
        self.assertEqual(update_args[9], 2)


class ChannelBonusActivationTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.channel_bonus.db.finalize_news_channel_bonus", new_callable=AsyncMock)
    @patch("services.channel_bonus.remnawave_get_subscription_url", new_callable=AsyncMock)
    @patch("services.channel_bonus.remnawave_set_subscription_expiry", new_callable=AsyncMock)
    @patch("services.channel_bonus.remnawave_get_or_create_user", new_callable=AsyncMock)
    @patch("services.channel_bonus.db.prepare_news_channel_bonus_subscription", new_callable=AsyncMock)
    async def test_bonus_uses_fixed_expiry_and_never_extends_existing_user(
        self,
        prepare,
        get_or_create,
        set_expiry,
        get_subscription_url,
        finalize,
    ):
        expires_at = datetime.utcnow() + timedelta(days=1)
        prepare.return_value = {
            "id": 55,
            "type_index": 1,
            "remnawave_username": None,
            "subscription_until": expires_at,
        }
        get_or_create.return_value = ("uuid-55", "tg_123_bypass_1")
        set_expiry.return_value = True
        get_subscription_url.return_value = "https://sub.wayspn.online/trial-key"
        finalize.return_value = True

        subscription_url = await channel_bonus.activate_news_channel_bonus(123)

        self.assertEqual(subscription_url, "https://sub.wayspn.online/trial-key")
        self.assertFalse(get_or_create.await_args.kwargs["extend_if_exists"])
        self.assertEqual(
            get_or_create.await_args.kwargs["traffic_limit_bytes"],
            channel_bonus.BYPASS_BASE_TRAFFIC_GB * channel_bonus.GB_BYTES,
        )
        self.assertEqual(
            get_or_create.await_args.kwargs["active_internal_squads"],
            [channel_bonus.BYPASS_SQUAD_UUID],
        )
        self.assertEqual(
            get_or_create.await_args.kwargs["hwid_device_limit"],
            channel_bonus.BYPASS_HWID_DEVICE_LIMIT,
        )
        set_expiry.assert_awaited_once_with(
            get_or_create.await_args.args[0],
            "uuid-55",
            expires_at,
        )
        get_subscription_url.assert_awaited_once_with(
            get_or_create.await_args.args[0],
            "uuid-55",
        )
        finalize.assert_awaited_once_with(
            123,
            55,
            "uuid-55",
            "tg_123_bypass_1",
            expires_at,
            channel_bonus.BYPASS_SQUAD_UUID,
        )


class ChannelBonusFinalizationTests(unittest.IsolatedAsyncioTestCase):
    @patch("database.sync_primary_subscription_to_user", new_callable=AsyncMock)
    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_claim_is_guarded_and_does_not_reuse_legacy_gift_flag(self, execute, sync_primary):
        execute.side_effect = [{"finalized": True}, {"finalized": False}]
        expires_at = datetime.utcnow() + timedelta(days=1)

        first = await database.finalize_news_channel_bonus(
            123, 55, "uuid-55", "tg_123_bypass_1", expires_at, "bypass-squad"
        )
        second = await database.finalize_news_channel_bonus(
            123, 55, "uuid-55", "tg_123_bypass_1", expires_at, "bypass-squad"
        )

        self.assertTrue(first)
        self.assertFalse(second)
        query = execute.await_args_list[0].args[0]
        self.assertIn("news_channel_bonus_claimed_at IS NULL", query)
        self.assertIn("is_visible = TRUE", query)
        self.assertNotIn("gift_received", query)
        sync_primary.assert_awaited_once_with(123)


class ChannelCheckHandlerTests(unittest.IsolatedAsyncioTestCase):
    @patch("handlers.callbacks.send_news_channel_offer", new_callable=AsyncMock)
    @patch("handlers.callbacks.db.needs_news_channel_onboarding", new_callable=AsyncMock)
    async def test_not_subscribed_deletes_prompt_and_sends_it_again(self, needs, send_offer):
        needs.return_value = True
        callback = _callback(status="left")
        state = AsyncMock()

        await callbacks.process_check_news_channel(callback, state)

        callback.message.delete.assert_awaited_once()
        send_offer.assert_awaited_once_with(callback.bot, 777, retry=True)
        state.clear.assert_not_awaited()

    @patch("handlers.callbacks.pending_challenge_for_user", new_callable=AsyncMock)
    @patch("handlers.callbacks.activate_news_channel_bonus", new_callable=AsyncMock)
    @patch("handlers.callbacks.db.release_user_lock", new_callable=AsyncMock)
    @patch("handlers.callbacks.db.acquire_user_lock", new_callable=AsyncMock)
    @patch("handlers.callbacks.db.needs_news_channel_onboarding", new_callable=AsyncMock)
    async def test_subscribed_user_gets_trial_key_instruction_without_opening_menu(
        self,
        needs,
        acquire_lock,
        release_lock,
        activate,
        pending_challenge,
    ):
        needs.return_value = True
        acquire_lock.return_value = True
        activate.return_value = "https://sub.wayspn.online/trial-key?a=1&b=2"
        pending_challenge.return_value = None
        callback = _callback(status="member")
        state = AsyncMock()

        await callbacks.process_check_news_channel(callback, state)

        callback.message.delete.assert_awaited_once()
        activate.assert_awaited_once_with(123)
        release_lock.assert_awaited_once_with(123)
        state.clear.assert_awaited_once()
        sent = callback.bot.send_message.await_args
        self.assertEqual(sent.args[0], 777)
        self.assertIn("Пробная подписка с антиглушилкой", sent.args[1])
        self.assertIn(
            "https://sub.wayspn.online/trial-key?a=1&amp;b=2",
            sent.args[1],
        )
        self.assertIn("Happ Plus", sent.args[1])
        self.assertIn("INCY", sent.args[1])
        self.assertIn("@wayspn_support", sent.args[1])
        keyboard = sent.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(keyboard[0][0].callback_data, "back_to_menu")
        self.assertEqual(keyboard[1][0].url, "https://t.me/wayspn_support")


if __name__ == "__main__":
    unittest.main()
