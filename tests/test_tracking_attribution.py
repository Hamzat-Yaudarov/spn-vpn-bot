import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import admin_web
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

    @patch("database.db_execute", new_callable=AsyncMock)
    async def test_admin_link_list_counts_only_real_paid_purchases(self, execute):
        execute.return_value = [
            {
                "code": "blogger_1",
                "unique_buyers": 3,
                "purchases_count": 7,
                "revenue": 2100,
            }
        ]

        rows = await database.list_tracking_links_with_stats()

        self.assertEqual(rows[0]["unique_buyers"], 3)
        self.assertEqual(rows[0]["purchases_count"], 7)
        query = execute.await_args.args[0]
        self.assertIn("COUNT(DISTINCT tg_id) AS unique_buyers", query)
        self.assertIn("COUNT(*) AS purchases_count", query)
        self.assertIn("status = 'paid'", query)
        self.assertIn("provider IN ('cryptobot', 'yookassa')", query)
        self.assertIn("payment_kind IN ('subscription', 'traffic_package', 'device_addon')", query)
        self.assertIn("amount > 0", query)
        self.assertIn("refund_requested_at IS NULL", query)


class AdminTrackingApiTests(unittest.IsolatedAsyncioTestCase):
    @patch("admin_web.db.list_tracking_links_with_stats", new_callable=AsyncMock)
    async def test_links_api_exposes_buyer_and_purchase_counts(self, list_stats):
        list_stats.return_value = [
            {
                "code": "blogger_1",
                "unique_buyers": 3,
                "purchases_count": 7,
            },
            {
                "code": "empty_link",
                "unique_buyers": None,
                "purchases_count": None,
            },
        ]

        response = await admin_web.admin_links(_=admin_web.ADMIN_ID)

        self.assertEqual(response["items"][0]["unique_buyers"], 3)
        self.assertEqual(response["items"][0]["purchases_count"], 7)
        self.assertEqual(response["items"][1]["unique_buyers"], 0)
        self.assertEqual(response["items"][1]["purchases_count"], 0)

    def test_admin_table_renders_new_tracking_columns(self):
        admin_js = (Path(__file__).parents[1] / "static" / "admin" / "assets" / "admin.js").read_text()

        self.assertIn("Уникальные покупатели", admin_js)
        self.assertIn("Покупки", admin_js)
        self.assertIn("l.unique_buyers", admin_js)
        self.assertIn("l.purchases_count", admin_js)


if __name__ == "__main__":
    unittest.main()
