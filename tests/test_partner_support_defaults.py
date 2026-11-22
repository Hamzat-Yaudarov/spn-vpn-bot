"""Customer support defaults on branded pages/profiles, never live data."""
from copy import deepcopy
import unittest

from partner_subscription_web import render_subscription_page
from services.partner_brand_defaults import DEFAULT_CUSTOMER_SUPPORT_URL
from services.partner_subscriptions import PartnerLinkSettings, PartnerSubscriptionLinks, profile_headers
from test_partner_subscriptions import context, KEY


class CustomerSupportDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.links = PartnerSubscriptionLinks(PartnerLinkSettings("partners.example.test", KEY))
        self.quota = {"used_bytes": 0, "total_bytes": 300 * 1024**3,
                      "remaining_bytes": 300 * 1024**3}

    def test_empty_customer_support_is_consistent_on_page_and_client_profile(self):
        for value in ("", None):
            with self.subTest(value=value):
                row = context(support_url=value)
                before = deepcopy(row)
                html = render_subscription_page(row, self.links, "ready", self.quota).body.decode()
                headers = profile_headers(row, self.links, self.quota)
                self.assertIn('href="https://t.me/wayspn_support"', html)
                self.assertEqual(headers["support-url"], DEFAULT_CUSTOMER_SUPPORT_URL)
                self.assertNotIn("vpnislam", html)
                self.assertEqual(row, before)

    def test_custom_partner_support_overrides_default_without_main_brand_leaking(self):
        row = context(support_url="https://t.me/PartnerCustomSupport")
        html = render_subscription_page(row, self.links, "ready", self.quota).body.decode()
        headers = profile_headers(row, self.links, self.quota)
        self.assertIn('href="https://t.me/PartnerCustomSupport"', html)
        self.assertEqual(headers["support-url"], row["support_url"])
        self.assertNotIn("wayspn_support", html)
        self.assertNotIn("vpnislam", html)

    def test_invalid_custom_url_still_rejected_not_silently_trusted(self):
        for value in ("javascript:alert(1)", "http://insecure.test", "https://user:secret@private.test"):
            with self.subTest(value=value):
                row = context(support_url=value)
                with self.assertRaises(ValueError):
                    render_subscription_page(row, self.links, "ready", self.quota)
                with self.assertRaises(ValueError):
                    profile_headers(row, self.links, self.quota)


if __name__ == "__main__":
    unittest.main()
