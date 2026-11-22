from datetime import datetime, timedelta, timezone
import unittest

from services.partner_runtime import RuntimeConfigurationError
from services.partner_runtime_catalog import StagingAddonCatalog


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
SNAPSHOT = {
    "traffic_packages": {"test": {"gb": 25, "price": "12.50"}},
    "device_rules": {
        "price_per_30_days": 30,
        "minimum_price": 1,
        "discount_step_percent": 10,
        "max_discount_percent": 20,
    },
}


class StagingCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_fixture_not_fake_zero_or_maximum(self):
        source = StagingAddonCatalog()
        self.assertIsNone((await source.summary())["traffic_packages"])
        result = await source.quote({}, now=NOW)
        self.assertEqual(result["traffic_status"], "not_loaded")
        self.assertEqual(result["devices"]["addon_status"], "not_loaded")

    async def test_subscription_specific_proration_discount_and_maximum(self):
        source = StagingAddonCatalog(SNAPSHOT)
        result = await source.quote(
            {"expires_at": NOW + timedelta(days=15), "device_limit": 8}, now=NOW
        )
        self.assertEqual(result["traffic_packages"][0]["price_kopecks"], 1250)
        packages = result["devices"]["packages"]
        self.assertEqual([item["count"] for item in packages], [1, 2])
        self.assertEqual([item["price_kopecks"] for item in packages], [1500, 2700])
        self.assertEqual(
            (await source.summary())["device_options"]["addon_status"], "quote_required"
        )

    async def test_expired_and_full_devices_distinguished(self):
        source = StagingAddonCatalog(SNAPSHOT)
        result = await source.quote({"expires_at": NOW, "device_limit": 3}, now=NOW)
        self.assertEqual(result["traffic_status"], "expired")
        self.assertEqual(result["devices"]["addon_status"], "expired")
        full = await source.quote(
            {"expires_at": NOW + timedelta(days=1), "device_limit": 10}, now=NOW
        )
        self.assertEqual(full["devices"]["addon_status"], "max_limit")
        self.assertEqual(full["traffic_status"], "available")

    async def test_snapshot_is_copied_and_invalid_prices_fail_closed(self):
        source = StagingAddonCatalog(SNAPSHOT)
        summary = await source.summary()
        summary["traffic_packages"]["test"]["price"] = "0"
        self.assertEqual(
            (await source.summary())["traffic_packages"]["test"]["price"], "12.50"
        )
        for snapshot in (
            {"traffic_packages": {}, "device_rules": {"price_per_30_days": -1}},
            {
                "traffic_packages": {"bad": {"gb": 1, "price": 0.5}},
                "device_rules": None,
            },
        ):
            with self.assertRaises(RuntimeConfigurationError):
                StagingAddonCatalog(snapshot)

    async def test_invalid_subscription_unknown_clock_never_leak_inputs(self):
        source = StagingAddonCatalog(SNAPSHOT)
        for row in (
            {"expires_at": "private secret", "device_limit": 3},
            {"expires_at": NOW, "device_limit": True},
        ):
            with self.assertRaises(RuntimeConfigurationError) as raised:
                await source.quote(row, now=NOW)
            self.assertNotIn("private secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
