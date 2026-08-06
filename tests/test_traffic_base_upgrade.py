import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import customer_web
from config import BYPASS_BASE_TRAFFIC_GB, BYPASS_TARIFFS, GB_BYTES
from services import traffic_resets
from services.traffic_periods import build_traffic_period_state, rebase_traffic_limit_bytes


class _FakeContextSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class TrafficBaseUpgradeTests(unittest.TestCase):
    def test_bypass_catalog_uses_200_gb_base(self):
        self.assertEqual(BYPASS_BASE_TRAFFIC_GB, 200)
        self.assertEqual(BYPASS_TARIFFS["bypass_1m"]["base_gb"], 200)
        self.assertEqual(BYPASS_TARIFFS["bypass_3m"]["base_gb"], 200)

    def test_paid_traffic_is_preserved_when_base_grows(self):
        limit = rebase_traffic_limit_bytes(
            current_base_bytes=150 * GB_BYTES,
            current_limit_bytes=160 * GB_BYTES,
            carried_bytes=0,
            paid_bytes=10 * GB_BYTES,
            new_base_bytes=200 * GB_BYTES,
        )
        self.assertEqual(limit, 210 * GB_BYTES)

    def test_observed_extra_is_preserved_for_stale_rows(self):
        limit = rebase_traffic_limit_bytes(
            current_base_bytes=150 * GB_BYTES,
            current_limit_bytes=160 * GB_BYTES,
            carried_bytes=0,
            paid_bytes=0,
            new_base_bytes=200 * GB_BYTES,
        )
        self.assertEqual(limit, 210 * GB_BYTES)

    def test_rebase_is_idempotent(self):
        limit = rebase_traffic_limit_bytes(
            current_base_bytes=200 * GB_BYTES,
            current_limit_bytes=210 * GB_BYTES,
            carried_bytes=0,
            paid_bytes=10 * GB_BYTES,
            new_base_bytes=200 * GB_BYTES,
        )
        self.assertEqual(limit, 210 * GB_BYTES)

    def test_active_subscription_keeps_paid_and_carried_traffic(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        state = build_traffic_period_state(
            {
                "subscription_until": now + timedelta(days=20),
                "base_traffic_bytes": 150 * GB_BYTES,
                "carried_traffic_bytes": 5 * GB_BYTES,
                "current_paid_traffic_bytes": 10 * GB_BYTES,
                "current_period_limit_bytes": 165 * GB_BYTES,
                "traffic_reset_at": now + timedelta(days=10),
            },
            "bypass",
            now,
        )
        self.assertEqual(state.base_bytes, 200 * GB_BYTES)
        self.assertEqual(state.carried_bytes, 5 * GB_BYTES)
        self.assertEqual(state.paid_bytes, 10 * GB_BYTES)
        self.assertEqual(state.limit_bytes, 215 * GB_BYTES)

    def test_new_cycle_starts_with_200_gb(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        state = build_traffic_period_state(
            {"subscription_until": now - timedelta(seconds=1)},
            "bypass",
            now,
        )
        self.assertEqual(state.base_bytes, 200 * GB_BYTES)
        self.assertEqual(state.limit_bytes, 200 * GB_BYTES)

    def test_runtime_ui_uses_catalog_base_and_keeps_150_gb_addon(self):
        root = Path(__file__).parents[1]
        miniapp = (root / "static/miniapp/assets/app.js").read_text()
        site = (root / "static/site/assets/site.js").read_text()
        admin = (root / "static/admin/assets/admin.js").read_text()

        self.assertIn("bypassBaseGb()", miniapp)
        self.assertNotIn("150 ГБ в месяц", miniapp)
        self.assertIn("item.base_gb", site)
        self.assertNotIn('"150 ГБ включено"', site)
        self.assertIn('["gb_150","150 ГБ"]', admin)

    def test_website_catalog_exposes_200_gb_base(self):
        catalog = customer_web._catalog([])
        self.assertEqual(catalog["bypass"][0]["base_gb"], 200)


class TrafficLimitSyncTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.traffic_resets.aiohttp.TCPConnector")
    @patch("services.traffic_resets.aiohttp.ClientSession", return_value=_FakeContextSession())
    @patch("services.traffic_resets.remnawave_reset_user_traffic", new_callable=AsyncMock)
    @patch("services.traffic_resets.remnawave_update_user_profile", new_callable=AsyncMock)
    @patch("services.traffic_resets.db.mark_traffic_limit_synced", new_callable=AsyncMock)
    @patch("services.traffic_resets.db.get_active_device_addon_count", new_callable=AsyncMock)
    @patch("services.traffic_resets.db.get_bypass_subscriptions_for_limit_sync", new_callable=AsyncMock)
    async def test_210_gb_is_synced_without_resetting_used_traffic(
        self,
        get_subscriptions,
        get_addons,
        mark_synced,
        update_profile,
        reset_traffic,
        _client_session,
        _connector,
    ):
        get_subscriptions.return_value = [{
            "id": 55,
            "tg_id": 123,
            "plan_kind": "bypass",
            "remnawave_uuid": "uuid-55",
            "current_period_limit_bytes": 210 * GB_BYTES,
            "base_traffic_bytes": 200 * GB_BYTES,
        }]
        get_addons.return_value = 0
        update_profile.return_value = True

        await traffic_resets.process_pending_traffic_limit_sync()

        self.assertEqual(update_profile.await_args.kwargs["traffic_limit_bytes"], 210 * GB_BYTES)
        self.assertEqual(update_profile.await_args.kwargs["traffic_limit_strategy"], "NO_RESET")
        reset_traffic.assert_not_awaited()
        mark_synced.assert_awaited_once_with(55)


if __name__ == "__main__":
    unittest.main()
