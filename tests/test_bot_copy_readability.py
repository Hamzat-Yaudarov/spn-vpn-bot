import unittest

from handlers import smart_assistant, start, subscription
from handlers.referral import earning_screen
from services.connection_instructions import ANDROID_PLATFORM, build_connection_instruction


def assert_balanced_quotes(test_case: unittest.TestCase, text: str) -> None:
    test_case.assertEqual(text.count("<blockquote>"), text.count("</blockquote>"), text)


class BotCopyReadabilityTests(unittest.TestCase):
    def test_primary_screens_use_telegram_quotes(self):
        texts = [
            start.build_main_menu()[0],
            start.build_main_menu(welcome=True)[0],
            subscription._subscription_invoice_text(
                {"kind": "regular", "days": 30},
                200,
            ),
            build_connection_instruction(
                ANDROID_PLATFORM,
                support_url="https://t.me/support",
                subscription_url="https://sub.example/key",
            ),
            smart_assistant._response_for_intent("buy")[0],
        ]

        for text in texts:
            self.assertIn("<blockquote>", text)
            assert_balanced_quotes(self, text)

    def test_tariff_copy_is_factual_not_pushy(self):
        text = subscription._subscription_invoice_text(
            {"kind": "bypass", "days": 30},
            300,
        ).lower()

        self.assertNotIn("лучший", text)
        self.assertNotIn("рекомендуем", text)
        self.assertNotIn("успей", text)

    def test_referral_screen_is_short_and_structured(self):
        text, _ = earning_screen(
            {"current_balance": 120, "active_referrals": 2, "total_earned": 240},
            "https://t.me/example?start=ref_1",
        )

        assert_balanced_quotes(self, text)
        self.assertLess(len(text), 800)
        self.assertIn("35%", text)
        self.assertIn("15%", text)


if __name__ == "__main__":
    unittest.main()
