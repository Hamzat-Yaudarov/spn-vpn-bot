import unittest
from unittest.mock import AsyncMock, patch

import database


class PaymentTrackingAttributionTests(unittest.IsolatedAsyncioTestCase):
    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_recent_active_click_wins(self, execute):
        execute.return_value = {"code": "ig_d01"}

        code = await database.get_payment_tracking_code(1001)

        self.assertEqual(code, "ig_d01")
        self.assertEqual(execute.await_count, 1)
        query, params = execute.await_args.args[:2]
        self.assertIn("tracking_link_clicks", query)
        self.assertEqual(params, (1001, database.TRACKING_ATTRIBUTION_DAYS))

    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_first_touch_is_used_without_recent_click(self, execute):
        execute.side_effect = [None, {"tracking_code": "yt_d03"}]

        code = await database.get_payment_tracking_code(1002)

        self.assertEqual(code, "yt_d03")
        self.assertEqual(execute.await_count, 2)


class TrackingStatsTests(unittest.IsolatedAsyncioTestCase):
    @patch("database.db_execute", new_callable=AsyncMock)
    @patch("database.get_tracking_link", new_callable=AsyncMock)
    async def test_subscription_kpis_are_separate_from_all_payments(self, get_link, execute):
        get_link.return_value = {"code": "tt_d04", "is_active": True}
        execute.side_effect = [
            {"total_clicks": 12, "unique_clicks": 10, "new_clicks": 8},
            {"count": 8},
            {
                "paid_payments": 5,
                "paid_subscriptions": 3,
                "new_subscriptions": 2,
                "unique_payers": 2,
                "revenue": 1124,
                "subscription_revenue": 1000,
            },
            [{"tariff_code": "regular_1m", "payment_kind": "subscription"}],
        ]

        stats = await database.get_tracking_link_stats("TT_D04")

        self.assertEqual(stats["paid_payments"], 5)
        self.assertEqual(stats["paid_subscriptions"], 3)
        self.assertEqual(stats["new_subscriptions"], 2)
        self.assertEqual(stats["unique_payers"], 2)
        self.assertEqual(stats["subscription_revenue"], 1000.0)
        get_link.assert_awaited_once_with("tt_d04")


if __name__ == "__main__":
    unittest.main()
