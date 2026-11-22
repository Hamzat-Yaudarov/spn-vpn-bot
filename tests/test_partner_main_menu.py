"""Owner-only SPN entry into the separate platform; no network, database or .env."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import start


def franchise_button(user_id=999, *, welcome=False):
    _, keyboard = start.build_main_menu(user_id, welcome=welcome)
    return next(
        button for row in keyboard.inline_keyboard for button in row
        if button.text == "🚀 Создать свой VPN"
    )


class PartnerMainMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        admin_patch = patch.object(start, "ADMIN_ID", 999)
        admin_patch.start()
        self.addCleanup(admin_patch.stop)

    def test_explicit_username_opens_separate_bot_not_legacy_partnership(self):
        for username in ("ManagerBot", "@ManagerBot"):
            with self.subTest(username=username), patch.object(
                start, "PARTNER_MANAGER_BOT_USERNAME", username
            ):
                button = franchise_button()
                self.assertEqual(button.url, "https://t.me/ManagerBot?start=spn")
                self.assertIsNone(button.callback_data)
                self.assertIsNone(button.web_app)

    def test_missing_or_malformed_username_has_no_fallback_destination(self):
        for username in ("", "tiny", "@", "https://t.me/ManagerBot", "ManagerBot?start=evil", "@@ManagerBot", "Manager Bot", "1ManagerBot", "ManagerBot/evil"):
            with self.subTest(username=username), patch.object(
                start, "PARTNER_MANAGER_BOT_USERNAME", username
            ):
                button = franchise_button()
                self.assertIsNone(button.url)
                self.assertIsNone(button.web_app)
                self.assertEqual(button.callback_data, "franchise_unavailable")

    def test_franchise_entry_is_hidden_for_every_non_admin_menu(self):
        for user_id in (None, 0, 42, "999", True):
            for welcome in (False, True):
                for username in ("ManagerBot", ""):
                    with self.subTest(user_id=user_id, welcome=welcome, username=username), patch.object(
                        start, "PARTNER_MANAGER_BOT_USERNAME", username
                    ):
                        _, keyboard = start.build_main_menu(user_id, welcome=welcome)
                        buttons = [button for row in keyboard.inline_keyboard for button in row]
                        self.assertNotIn("🚀 Создать свой VPN", [button.text for button in buttons])
                        self.assertNotIn("franchise_unavailable", [button.callback_data for button in buttons])

    def test_invalid_or_unconfigured_admin_id_never_grants_franchise_access(self):
        for admin_id in (None, 0, -1, "999", True, 999.0):
            for welcome in (False, True):
                with self.subTest(admin_id=admin_id, welcome=welcome), patch.object(start, "ADMIN_ID", admin_id):
                    _, keyboard = start.build_main_menu(admin_id, welcome=welcome)
                    self.assertNotIn(
                        "🚀 Создать свой VPN",
                        [button.text for row in keyboard.inline_keyboard for button in row],
                    )

    def test_admin_welcome_menu_keeps_the_franchise_entry(self):
        with patch.object(start, "PARTNER_MANAGER_BOT_USERNAME", "ManagerBot"):
            self.assertEqual(franchise_button(welcome=True).url, "https://t.me/ManagerBot?start=spn")

    async def test_unavailable_callback_only_explains_preparation_to_admin(self):
        callback = SimpleNamespace(from_user=SimpleNamespace(id=999), answer=AsyncMock())
        await start.franchise_unavailable(callback)
        callback.answer.assert_awaited_once()
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("готовится", callback.answer.await_args.args[0])
        self.assertNotIn("15%", callback.answer.await_args.args[0])

    async def test_stale_unavailable_callback_does_not_reveal_entry_to_non_admin(self):
        for user_id in (None, 0, 42, "999", True):
            with self.subTest(user_id=user_id):
                callback = SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())
                await start.franchise_unavailable(callback)
                callback.answer.assert_awaited_once_with("Недоступно.", show_alert=True)

    async def test_unavailable_callback_fails_closed_without_identity_or_valid_admin(self):
        callback = SimpleNamespace(answer=AsyncMock())
        await start.franchise_unavailable(callback)
        callback.answer.assert_awaited_once_with("Недоступно.", show_alert=True)
        for admin_id in (None, 0, -1, "999", True, 999.0):
            with self.subTest(admin_id=admin_id), patch.object(start, "ADMIN_ID", admin_id):
                callback = SimpleNamespace(from_user=SimpleNamespace(id=admin_id), answer=AsyncMock())
                await start.franchise_unavailable(callback)
                callback.answer.assert_awaited_once_with("Недоступно.", show_alert=True)

    def test_existing_purchase_and_referral_routes_are_unchanged(self):
        _, keyboard = start.build_main_menu(42)
        routes = {button.text: button.callback_data for row in keyboard.inline_keyboard for button in row}
        self.assertEqual(routes["🛒 Купить подписку"], "buy_subscription")
        self.assertEqual(routes["🔑 Мои подписки"], "my_subscriptions")
        self.assertEqual(routes["💰 Заработать"], "referral")


if __name__ == "__main__":
    unittest.main()
