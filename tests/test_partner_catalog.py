"""Partner catalogue tests: no .env, database, network, or live bot startup."""

import ast
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.partner_catalog import (
    BASE_DEVICE_LIMIT,
    BASE_TRAFFIC_GB,
    BILLING_DAYS,
    ENTRY_FEE_KOPECKS,
    MAX_PRICE_KOPECKS,
    MIN_PRICE_KOPECKS,
    RESERVED_SLUGS,
    catalog,
    load_main_addon_catalog,
    parse_price_kopecks,
    project_hostname,
    split_revenue,
    validate_base_domain,
    validate_slug,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 18, 12)


def main_pricing_without_loading_env():
    """Read only actual literal pricing assignments, never import config.py."""
    names = {
        "BYPASS_HWID_DEVICE_LIMIT", "REGULAR_HWID_DEVICE_LIMIT",
        "DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT", "DEVICE_ADDON_PACKAGES",
        "DEVICE_ADDON_PRICE_PER_30_DAYS", "DEVICE_ADDON_MIN_PRICE",
        "DEVICE_ADDON_DISCOUNT_STEP_PERCENT", "DEVICE_ADDON_MAX_DISCOUNT_PERCENT",
        "BYPASS_TRAFFIC_PACKAGES",
    }
    values = {}
    for assignment in ast.parse((ROOT / "config.py").read_text()).body:
        if not isinstance(assignment, ast.Assign):
            continue
        for target in assignment.targets:
            if isinstance(target, ast.Name) and target.id in names:
                values[target.id] = ast.literal_eval(assignment.value)
    if set(values) != names:
        raise AssertionError("Actual main add-on configuration changed; review adapter parity")
    return SimpleNamespace(**values)


def existing_device_helper_with_injected_config(config):
    """Execute the real helper with a pricing-only config, not application env."""
    spec = importlib.util.spec_from_file_location(
        "_partner_catalog_main_device_helper", ROOT / "services" / "device_addons.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"config": config}):
        spec.loader.exec_module(module)
    return module


def subscription(**changes):
    return {"generation": "v2", "is_visible": True, "is_renewable": True,
            "plan_kind": "bypass", "hwid_device_limit": 3,
            "subscription_until": NOW + timedelta(days=30), **changes}


class ImportAndPriceTests(unittest.TestCase):
    def test_module_import_does_not_import_config_or_application(self):
        script = """
import sys
class RejectApplicationImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'config', 'dotenv', 'database', 'webhooks'}:
            raise AssertionError('Application imported: ' + fullname)
sys.meta_path.insert(0, RejectApplicationImports())
from services.partner_catalog import catalog
assert catalog(19900)['products'][0]['base_device_limit'] == 3
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ruble_prices_use_exact_kopecks(self):
        for value, expected in [("199", 19_900), ("199.0", 19_900),
                                ("199.01", 19_901), ("249.90", 24_990),
                                ("100000", MAX_PRICE_KOPECKS)]:
            with self.subTest(value=value):
                self.assertEqual(parse_price_kopecks(value), expected)

    def test_bad_or_underpriced_inputs_fail_closed(self):
        for value in [True, False, None, 199, 199.0, float("nan"),
                      "NaN", "Infinity", "inf", "1e9", "+199", "-199", "199,00",
                      "199.000", "0199", " 199", "199 ", "199\n", "١٩٩", "１９９",
                      "0", "198.99", "100000.01", "9" * 100_000]:
            with self.subTest(value=str(value)[:40]):
                with self.assertRaises(ValueError):
                    parse_price_kopecks(value)

    def test_revenue_split_is_exact_half_up_and_covers_addons(self):
        self.assertEqual(split_revenue(19_900), {"partner_kopecks": 13_930, "platform_kopecks": 5_970})
        self.assertEqual(split_revenue(19_901), {"partner_kopecks": 13_931, "platform_kopecks": 5_970})
        self.assertEqual(split_revenue(5), {"partner_kopecks": 4, "platform_kopecks": 1})
        for amount in range(0, 10_000):
            split = split_revenue(amount)
            self.assertEqual(sum(split.values()), amount)
            self.assertLessEqual(abs(split["partner_kopecks"] * 100 - amount * 70), 50)
        for amount in [-1, True, 1.5, "19900", MAX_PRICE_KOPECKS + 1]:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                split_revenue(amount)


class DomainTests(unittest.TestCase):
    def test_canonical_label_and_one_level_hostname(self):
        self.assertEqual(validate_slug("My-VPN"), "my-vpn")
        self.assertEqual(validate_slug("abc"), "abc")
        self.assertEqual(validate_slug("a" * 32), "a" * 32)
        self.assertEqual(validate_base_domain("Partners.Example.COM"), "partners.example.com")
        self.assertEqual(project_hostname("My-VPN", "Partners.Example.COM"), "my-vpn.partners.example.com")

    def test_reserved_and_ambiguous_slug_rejected(self):
        for value in list(RESERVED_SLUGS) + ["ADMIN", "ab", "a" * 33, "-abc", "abc-",
                                            "a_b", "foo.bar", "abc\n", "abc\rHost: x",
                                            "//example", "a/b", "a:b", "a@b", "привет",
                                            "xn--vpn", "ＶＰＮ", "a\u200bb", None, 123]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_slug(value)

    def test_base_rejects_urls_ips_wildcards_ports_and_host_injection(self):
        for value in ["https://example.com", "example.com:443", "example.com/",
                      "user@example.com", "*.example.com", "example.com?x=1",
                      "example.com#admin", "example.com.", ".example.com", "a..example.com",
                      "example.com\r\nHost:evil.com", " example.com", "example.com ",
                      "localhost", "127.0.0.1", "[::1]", "::1", "123.123",
                      "xn--e1afmkfd.xn--p1ai", "пример.рф", "exаmple.com",
                      "a" * 64 + ".com", None, 123]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_base_domain(value)

    def test_fully_qualified_host_length_checked_after_adding_slug(self):
        domain = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 60])
        self.assertEqual(len(domain), 252)
        self.assertEqual(validate_base_domain(domain), domain)
        with self.assertRaises(ValueError):
            project_hostname("abc", domain)


class CatalogTests(unittest.TestCase):
    def test_exact_product_terms_and_no_unknown_prices(self):
        result = catalog(MIN_PRICE_KOPECKS)
        self.assertEqual((BILLING_DAYS, BASE_TRAFFIC_GB, BASE_DEVICE_LIMIT, ENTRY_FEE_KOPECKS),
                         (30, 300, 3, 999_000))
        self.assertEqual(len(result["products"]), 1)
        self.assertEqual(result["products"][0], {
            "id": "unified_30d", "title": "Подписка на 30 дней", "days": 30,
            "price_kopecks": 19_900, "min_price_kopecks": 19_900,
            "base_traffic_gb": 300, "base_device_limit": 3,
            "ordinary_servers": True, "bypass_servers": True,
        })
        self.assertEqual(result["revenue_share"], {"partner_percent": 70, "platform_percent": 30})
        self.assertEqual(result["addons"]["traffic_status"], "not_loaded")
        self.assertEqual(result["addons"]["devices"]["addon_status"], "not_loaded")
        self.assertEqual(result["addons"]["devices"]["packages"], [])
        self.assertEqual(catalog(25_000)["products"][0]["price_kopecks"], 25_000)

    def test_main_traffic_prices_are_explicitly_supplied_and_copied(self):
        prices = {"gb_10": {"gb": 10, "price": 24}}
        result = catalog(19_900, prices)
        self.assertEqual(result["addons"]["traffic_packages"], [
            {"id": "gb_10", "gb": 10, "price_kopecks": 2_400, "title": "+10 ГБ"}])
        prices["gb_10"]["price"] = 999
        self.assertEqual(result["addons"]["traffic_packages"][0]["price_kopecks"], 2_400)
        for invalid in [True, 19_899, 19_900.0, MAX_PRICE_KOPECKS + 1]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                catalog(invalid)

    def test_exact_main_discount_strings_supported_for_traffic_and_devices(self):
        for value, expected in [(24, 2400), ("24", 2400), ("24.0", 2400),
                                ("19.20", 1920), ("0.01", 1)]:
            with self.subTest(value=value):
                devices = {"addon_status": "available", "packages": [
                    {"count": 1, "price": value, "original_price": "24.0",
                     "discount_percent": 20, "days": 30}]}
                result = catalog(19_900, {"gb_10": {"gb": 10, "price": value}}, devices)
                self.assertEqual(result["addons"]["traffic_packages"][0]["price_kopecks"], expected)
                self.assertEqual(result["addons"]["devices"]["packages"][0]["price_kopecks"], expected)
                self.assertEqual(result["addons"]["devices"]["packages"][0]["original_price_kopecks"], 2400)

    def test_bad_main_decimal_strings_and_floats_rejected_for_all_addons(self):
        for value in [24.0, 19.2, float("nan"), "NaN", "Infinity", "19.201",
                      "24.000", "1e2", "+24", "-24", " 24", "24 ", "24\n",
                      "19,20", "024", "0", "0.00", "100000.01", True, None]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    catalog(19_900, {"gb_10": {"gb": 10, "price": value}})
                with self.assertRaises(ValueError):
                    catalog(19_900, device_options={"addon_status": "available", "packages": [
                        {"count": 1, "price": value, "original_price": 24,
                         "discount_percent": 20, "days": 30}]})
                with self.assertRaises(ValueError):
                    catalog(19_900, device_options={"addon_status": "available", "packages": [
                        {"count": 1, "price": 20, "original_price": value,
                         "discount_percent": 20, "days": 30}]})

    def test_bad_addons_rejected_without_inventing_zero_or_max_status(self):
        for traffic in [[], {"gb": {"gb": True, "price": 24}},
                        {"<script>": {"gb": 10, "price": 24}},
                        {"gb": {"gb": 10, "price": 24.0}},
                        {"gb": {"gb": 10, "price": 0}}]:
            with self.subTest(traffic=traffic), self.assertRaises(ValueError):
                catalog(19_900, traffic)
        for devices in [{}, {"addon_status": "available", "packages": []},
                        {"addon_status": "max_limit", "packages": [{"count": 1}]},
                        {"addon_status": "wrong", "packages": []}]:
            with self.subTest(devices=devices), self.assertRaises(ValueError):
                catalog(19_900, device_options=devices)
        self.assertEqual(catalog(19_900, {})["addons"]["traffic_status"], "unavailable")


class ActualMainAddonParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = main_pricing_without_loading_env()
        cls.main = existing_device_helper_with_injected_config(cls.config)

    def adapter(self, **kwargs):
        return load_main_addon_catalog(
            self.config, now=NOW,
            device_option_provider=self.main.miniapp_device_addon_options, **kwargs)

    def test_actual_main_base_limits_and_all_traffic_prices_match(self):
        self.assertEqual(self.config.BYPASS_HWID_DEVICE_LIMIT, 3)
        self.assertEqual(self.config.REGULAR_HWID_DEVICE_LIMIT, 5)
        loaded = self.adapter()
        quote = catalog(19_900, loaded["traffic_packages"], loaded["device_options"])
        self.assertEqual(quote["addons"]["devices"]["addon_status"], "quote_required")
        for package in quote["addons"]["traffic_packages"]:
            actual = self.config.BYPASS_TRAFFIC_PACKAGES[package["id"]]
            self.assertEqual((package["gb"], package["price_kopecks"]),
                             (actual["gb"], actual["price"] * 100))

    def test_same_bypass_helper_no_regular_price_fallback(self):
        loaded = self.adapter(subscription=subscription(plan_kind="regular"))
        packages = catalog(19_900, loaded["traffic_packages"], loaded["device_options"])["addons"]["devices"]["packages"]
        self.assertEqual([p["count"] for p in packages], [1, 2, 3])
        for package in packages:
            expected = self.main.calculate_device_addon_price(
                "bypass", package["count"], NOW + timedelta(days=30), now=NOW)
            cheaper_regular = self.main.calculate_device_addon_price(
                "regular", package["count"], NOW + timedelta(days=30), now=NOW)
            self.assertEqual(package["price_kopecks"], expected["price"] * 100)
            self.assertNotEqual(package["price_kopecks"], cheaper_regular["price"] * 100)
            self.assertEqual(package["days"], expected["days"])
            self.assertEqual(package["discount_percent"], expected["discount_percent"])

    def test_real_expiry_and_existing_paid_device_limit_preserved(self):
        for changes, status, counts in [
            ({"hwid_device_limit": 9}, "available", [1]),
            ({"hwid_device_limit": 10}, "max_limit", []),
            ({"subscription_until": NOW}, "expired", []),
            ({"is_renewable": False}, "unavailable", []),
            ({"subscription_until": NOW + timedelta(hours=2)}, "available", [1, 2, 3]),
        ]:
            with self.subTest(changes=changes):
                loaded = self.adapter(subscription=subscription(**changes))
                normalized = loaded["validated"]["devices"]
                self.assertEqual(normalized["addon_status"], status)
                self.assertEqual([p["count"] for p in normalized["packages"]], counts)
                if counts and "subscription_until" in changes:
                    self.assertEqual(normalized["packages"][0]["days"], 1)
                    self.assertEqual(normalized["packages"][0]["price_kopecks"], self.config.DEVICE_ADDON_MIN_PRICE * 100)

    def test_changed_main_base_or_missing_catalog_fails_closed(self):
        for changes in [{"BYPASS_HWID_DEVICE_LIMIT": 5},
                        {"DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT": 11},
                        {"BYPASS_TRAFFIC_PACKAGES": None}]:
            config = SimpleNamespace(**{**vars(self.config), **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                load_main_addon_catalog(config)


if __name__ == "__main__":
    unittest.main()
