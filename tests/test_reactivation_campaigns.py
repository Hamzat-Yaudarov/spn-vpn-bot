import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from config import GB_BYTES
import database
from handlers import reactivation
from services import payment_processing, reactivation_campaigns, subscription_notifications


class _FakeContextSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class ReactivationCandidateTests(unittest.IsolatedAsyncioTestCase):
    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_candidate_queries_require_real_payment_and_global_one_time_claim(self, execute):
        inactive_cutoff = datetime(2026, 7, 1)
        new_user_cutoff = datetime(2026, 7, 24)

        await database.ensure_reactivation_candidates(inactive_cutoff, new_user_cutoff)

        self.assertEqual(execute.await_count, 3)
        winback_query = execute.await_args_list[1].args[0]
        new_user_query = execute.await_args_list[2].args[0]
        for required in (
            "payment_kind = 'subscription'",
            "status = 'paid'",
            "amount > 0",
            "refund_requested_at IS NULL",
            "last_subscription_until <= $1",
            "last_paid_at <= access.last_subscription_until",
            "claimed_offer.status = 'claimed'",
        ):
            self.assertIn(required, winback_query)
        self.assertIn("user_row.created_at <= $1", new_user_query)
        self.assertIn("user_row.accepted_terms = TRUE", new_user_query)
        self.assertIn("payment_kind = 'subscription'", new_user_query)
        self.assertIn("claimed_offer.status = 'claimed'", new_user_query)

    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_due_query_caps_successful_sends_and_skips_blocked_or_active_users(self, execute):
        execute.return_value = []
        day_start = datetime(2026, 8, 27, 21, 0)

        await database.get_reactivation_offers_due(day_start, 7)

        query, params = execute.await_args.args[:2]
        self.assertIn("offer.send_count < $2", query)
        self.assertIn("telegram_delivery_blocked", query)
        self.assertIn("subscription_until >", query)
        self.assertEqual(params, (day_start, 7))


class ReactivationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.reactivation_campaigns.asyncio.sleep", new_callable=AsyncMock)
    @patch("services.reactivation_campaigns.db.clear_reactivation_offer_message", new_callable=AsyncMock)
    @patch("services.reactivation_campaigns.db.retire_reactivation_offers", new_callable=AsyncMock)
    async def test_retired_campaign_deletes_message_without_sending_a_new_one(
        self,
        retire,
        clear_message,
        _sleep,
    ):
        retire.return_value = [{
            "id": 91,
            "tg_id": 123,
            "last_message_id": 700,
        }]
        bot = AsyncMock()

        deleted, pending = await reactivation_campaigns.retire_reactivation_campaigns(bot)

        self.assertEqual(deleted, 1)
        self.assertEqual(pending, 0)
        bot.delete_message.assert_awaited_once_with(123, 700)
        clear_message.assert_awaited_once_with(91, 700)
        bot.send_message.assert_not_awaited()

    @patch("services.reactivation_campaigns.db.clear_reactivation_offer_message", new_callable=AsyncMock)
    @patch("services.reactivation_campaigns.db.retire_reactivation_offers", new_callable=AsyncMock)
    async def test_failed_delete_is_left_for_a_later_retry(self, retire, clear_message):
        retire.return_value = [{
            "id": 92,
            "tg_id": 124,
            "last_message_id": 701,
        }]
        bot = AsyncMock()
        bot.delete_message.side_effect = RuntimeError("temporary Telegram error")

        deleted, pending = await reactivation_campaigns.retire_reactivation_campaigns(bot)

        self.assertEqual(deleted, 0)
        self.assertEqual(pending, 1)
        clear_message.assert_not_awaited()
        bot.send_message.assert_not_awaited()


class ReactivationActivationTests(unittest.IsolatedAsyncioTestCase):
    async def _activate(self, offer_type: str, days: int, traffic_gb: int):
        fixed_expiry = datetime(2026, 9, 3, 15, 0)
        offer_row = {
            "offer_type": offer_type,
            "trial_expires_at": fixed_expiry,
            "last_message_id": 800,
        }
        subscription = {
            "id": 55,
            "type_index": 2,
            "remnawave_username": "tg_123_bypass_2",
        }

        with (
            patch("services.reactivation_campaigns.db.db_execute", new_callable=AsyncMock, return_value={"offer_type": offer_type}),
            patch(
                "services.reactivation_campaigns.db.prepare_reactivation_offer_claim",
                new_callable=AsyncMock,
                return_value={"offer": offer_row, "subscription": subscription},
            ) as prepare,
            patch("services.reactivation_campaigns.aiohttp.TCPConnector"),
            patch("services.reactivation_campaigns.aiohttp.ClientSession", return_value=_FakeContextSession()),
            patch(
                "services.reactivation_campaigns.remnawave_get_or_create_user",
                new_callable=AsyncMock,
                return_value=("uuid-55", "tg_123_bypass_2"),
            ) as get_or_create,
            patch(
                "services.reactivation_campaigns.remnawave_update_user_profile",
                new_callable=AsyncMock,
                return_value=True,
            ) as update_profile,
            patch("services.reactivation_campaigns.remnawave_reset_user_traffic", new_callable=AsyncMock, return_value=True) as reset,
            patch("services.reactivation_campaigns.remnawave_set_subscription_expiry", new_callable=AsyncMock, return_value=True) as set_expiry,
            patch(
                "services.reactivation_campaigns.remnawave_get_subscription_url",
                new_callable=AsyncMock,
                return_value="https://sub.wayspn.online/free-key",
            ),
            patch(
                "services.reactivation_campaigns.db.finalize_reactivation_offer_claim",
                new_callable=AsyncMock,
                return_value=True,
            ) as finalize,
        ):
            result = await reactivation_campaigns.activate_reactivation_offer(91, 123)

        prepare.assert_awaited_once_with(91, 123, days)
        self.assertFalse(get_or_create.await_args.kwargs["extend_if_exists"])
        self.assertEqual(get_or_create.await_args.kwargs["traffic_limit_bytes"], traffic_gb * GB_BYTES)
        self.assertEqual(get_or_create.await_args.kwargs["traffic_limit_strategy"], "NO_RESET")
        self.assertEqual(update_profile.await_args.kwargs["traffic_limit_bytes"], traffic_gb * GB_BYTES)
        self.assertEqual(update_profile.await_args.kwargs["traffic_limit_strategy"], "NO_RESET")
        reset.assert_awaited_once_with(ANY, "uuid-55")
        set_expiry.assert_awaited_once_with(ANY, "uuid-55", fixed_expiry)
        self.assertEqual(finalize.await_args.args[6], traffic_gb * GB_BYTES)
        self.assertEqual(finalize.await_args.args[8], days)
        self.assertEqual(result["expires_at"], fixed_expiry)
        self.assertEqual(result["traffic_gb"], traffic_gb)

    async def test_winback_offer_uses_7_days_and_50_gb(self):
        await self._activate("winback_7d", 7, 50)

    async def test_new_user_offer_uses_1_day_and_10_gb(self):
        await self._activate("new_user_1d", 1, 10)

    @patch("services.reactivation_campaigns.db.finalize_reactivation_offer_claim", new_callable=AsyncMock)
    @patch("services.reactivation_campaigns.remnawave_get_or_create_user", new_callable=AsyncMock)
    @patch("services.reactivation_campaigns.aiohttp.TCPConnector")
    @patch("services.reactivation_campaigns.aiohttp.ClientSession", return_value=_FakeContextSession())
    @patch("services.reactivation_campaigns.db.prepare_reactivation_offer_claim", new_callable=AsyncMock)
    @patch("services.reactivation_campaigns.db.db_execute", new_callable=AsyncMock)
    async def test_remnawave_failure_does_not_finalize_claim(
        self,
        execute,
        prepare,
        _client_session,
        _connector,
        get_or_create,
        finalize,
    ):
        execute.return_value = {"offer_type": "new_user_1d"}
        prepare.return_value = {
            "offer": {"trial_expires_at": datetime.utcnow() + timedelta(days=1)},
            "subscription": {"id": 56, "type_index": 1, "remnawave_username": None},
        }
        get_or_create.return_value = (None, None)

        result = await reactivation_campaigns.activate_reactivation_offer(92, 124)

        self.assertEqual(result["error"], "remnawave_unavailable")
        finalize.assert_not_awaited()


class ReactivationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.subscription_notifications.db.has_open_reactivation_offer", new_callable=AsyncMock)
    @patch("services.subscription_notifications.db.get_user_subscriptions", new_callable=AsyncMock)
    @patch("services.subscription_notifications.db.db_execute", new_callable=AsyncMock)
    async def test_generic_expired_notice_is_suppressed_while_offer_is_open(
        self,
        execute,
        get_subscriptions,
        has_offer,
    ):
        execute.return_value = [{"tg_id": 123}]
        get_subscriptions.return_value = [{
            "generation": "v2",
            "is_visible": True,
            "is_renewable": True,
            "subscription_until": datetime.utcnow() - timedelta(days=40),
        }]
        has_offer.return_value = True
        bot = AsyncMock()

        await subscription_notifications._send_notifications_for_expired(bot)

        bot.send_message.assert_not_awaited()

    @patch("services.payment_processing.cancel_reactivation_offers_after_purchase", new_callable=AsyncMock)
    @patch("services.payment_processing.db.release_user_lock", new_callable=AsyncMock)
    @patch("services.payment_processing.db.acquire_user_lock", new_callable=AsyncMock, return_value=True)
    @patch("services.payment_processing.db.get_payment_by_invoice", new_callable=AsyncMock)
    async def test_already_processed_payment_still_cancels_open_offer(
        self,
        get_payment,
        _acquire,
        _release,
        cancel_offer,
    ):
        get_payment.return_value = {
            "status": "paid",
            "payment_kind": "subscription",
        }

        result = await payment_processing.process_paid_payment(None, 123, "invoice-1", "bypass_1m")

        self.assertTrue(result)
        cancel_offer.assert_awaited_once_with(None, 123)


class ReactivationHandlerTests(unittest.IsolatedAsyncioTestCase):
    @patch("handlers.reactivation.activate_reactivation_offer", new_callable=AsyncMock)
    @patch("handlers.reactivation.db.release_user_lock", new_callable=AsyncMock)
    @patch("handlers.reactivation.db.acquire_user_lock", new_callable=AsyncMock, return_value=False)
    async def test_second_parallel_click_is_rejected(self, _acquire, release, activate):
        callback = SimpleNamespace(
            data="reactivation_claim:91",
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(id=123)),
            bot=AsyncMock(),
        )

        await reactivation.process_reactivation_claim(callback)

        callback.answer.assert_awaited_once_with("Активация уже выполняется", show_alert=True)
        activate.assert_not_awaited()
        release.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
