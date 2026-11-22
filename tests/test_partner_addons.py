"""Main-price parity with injected pricing only; no application config/env."""
from datetime import datetime, timedelta, timezone
import subprocess
import sys
import unittest

from test_partner_catalog import (
    ROOT, existing_device_helper_with_injected_config, main_pricing_without_loading_env,
)
from services.partner_addons import MainPartnerAddonCatalog


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


class AddonAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = main_pricing_without_loading_env()
        self.helper = existing_device_helper_with_injected_config(self.config)
        self.discount_calls = []
        self.loaded = 0

        async def discounts():
            self.loaded += 1
            return ["injected-test-discount"]

        def calculate(price, rules, **kwargs):
            self.discount_calls.append((price, rules, kwargs))
            return {"price": str(price)}

        self.adapter = MainPartnerAddonCatalog(
            config_module=self.config, discounts_loader=discounts,
            discount_calculator=calculate,
            device_option_provider=self.helper.miniapp_device_addon_options,
        )
        self.sub = {"expires_at": NOW + timedelta(days=30), "device_limit": 3}

    async def test_actual_main_bypass_prices_and_no_constructor_io(self):
        self.assertEqual(self.loaded, 0)
        quote = await self.adapter(self.sub, now=NOW)
        self.assertEqual(self.loaded, 1)
        self.assertEqual([p["price_kopecks"] for p in quote["devices"]["packages"]], [10000, 19000, 27000])
        self.assertEqual([p["price_kopecks"] for p in quote["traffic_packages"]], [2400, 4500, 7900, 14900, 28900])
        self.assertEqual(self.sub, {"expires_at": NOW + timedelta(days=30), "device_limit": 3})
        self.assertTrue(all(c[2]["plan_kind"] == "bypass" and c[2]["product_type"] == "traffic"
                            for c in self.discount_calls))

    async def test_proration_maximum_and_expired_subscription(self):
        quote = await self.adapter({**self.sub, "expires_at": NOW + timedelta(days=1)}, now=NOW)
        self.assertEqual([p["price_kopecks"] for p in quote["devices"]["packages"]], [1000, 1000, 1000])
        quote = await self.adapter({**self.sub, "device_limit": 9}, now=NOW)
        self.assertEqual([p["count"] for p in quote["devices"]["packages"]], [1])
        quote = await self.adapter({**self.sub, "device_limit": 10}, now=NOW)
        self.assertEqual(quote["devices"]["addon_status"], "max_limit")
        quote = await self.adapter({**self.sub, "expires_at": NOW}, now=NOW)
        self.assertEqual(quote["devices"]["addon_status"], "expired")
        self.assertEqual(quote["traffic_status"], "expired")
        self.assertEqual(quote["traffic_packages"], [])

    async def test_exact_discount_price_and_no_fake_limits(self):
        self.adapter.discount_calculator = lambda *args, **kwargs: {"price": "19.20"}
        quote = await self.adapter(self.sub, now=NOW)
        self.assertEqual(quote["traffic_packages"][0]["price_kopecks"], 1920)
        for limit in (True, 0, 2, 11, "3", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                await self.adapter({**self.sub, "device_limit": limit}, now=NOW)
        with self.assertRaises(ValueError):
            await self.adapter(self.sub, now=NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            await self.adapter({**self.sub, "expires_at": NOW.replace(tzinfo=None)}, now=NOW)

    async def test_missing_catalog_or_discount_failure_never_invents_price(self):
        self.adapter.discount_calculator = lambda *args, **kwargs: {"price": "NaN"}
        with self.assertRaises(ValueError):
            await self.adapter(self.sub, now=NOW)

    def test_import_does_not_load_working_settings_or_database(self):
        result = subprocess.run([sys.executable, "-c", """
import sys
class Reject:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'config','database','dotenv','main'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, Reject())
from services.partner_addons import MainPartnerAddonCatalog
MainPartnerAddonCatalog()
"""], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
