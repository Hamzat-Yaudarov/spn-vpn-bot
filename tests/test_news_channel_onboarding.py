import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from handlers import callbacks, start


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


class ChannelOnboardingCompletionTests(unittest.IsolatedAsyncioTestCase):
    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_completion_does_not_create_or_activate_subscription(self, execute):
        execute.return_value = {"completed": True}

        completed = await database.complete_news_channel_onboarding(123)

        self.assertTrue(completed)
        query, params = execute.await_args.args[:2]
        self.assertEqual(params, (123,))
        self.assertIn("news_channel_onboarding_required = FALSE", query)
        self.assertIn("news_channel_bonus_subscription_id = NULL", query)
        self.assertNotIn("INSERT INTO subscriptions", query)
        self.assertNotIn("is_active = TRUE", query)

    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_only_abandoned_hidden_pending_record_can_be_removed(self, execute):
        execute.return_value = {"completed": True}

        await database.complete_news_channel_onboarding(123)

        query = execute.await_args.args[0]
        self.assertIn("DELETE FROM subscriptions", query)
        self.assertIn("is_active = FALSE", query)
        self.assertIn("is_visible = FALSE", query)
        self.assertIn("remnawave_uuid IS NULL", query)


class ChannelOfferTests(unittest.IsolatedAsyncioTestCase):
    async def test_offer_does_not_promise_trial_or_gift(self):
        bot = AsyncMock()

        await start.send_news_channel_offer(bot, 777)

        text = bot.send_message.await_args.args[1].lower()
        self.assertNotIn("день подписки", text)
        self.assertNotIn("пробн", text)
        self.assertNotIn("подар", text)
        self.assertIn("я подписался", text)
        keyboard = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[1][0].text, "✅ Я подписался")


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
    @patch("handlers.callbacks.show_main_menu", new_callable=AsyncMock)
    @patch("handlers.callbacks.db.complete_news_channel_onboarding", new_callable=AsyncMock)
    @patch("handlers.callbacks.db.needs_news_channel_onboarding", new_callable=AsyncMock)
    async def test_subscribed_user_returns_to_main_menu_without_trial(
        self,
        needs,
        complete,
        show_main_menu,
        pending_challenge,
    ):
        needs.return_value = True
        complete.return_value = True
        pending_challenge.return_value = None
        callback = _callback(status="member")
        state = AsyncMock()

        await callbacks.process_check_news_channel(callback, state)

        callback.message.delete.assert_awaited_once()
        complete.assert_awaited_once_with(123)
        state.clear.assert_awaited_once()
        show_main_menu.assert_awaited_once_with(callback.message, 123, welcome=True)
        callback.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
