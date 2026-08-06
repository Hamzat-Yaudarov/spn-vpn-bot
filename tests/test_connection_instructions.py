import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import callbacks, subscription
from services.connection_instructions import (
    ANDROID_APP_URL,
    ANDROID_PLATFORM,
    IPHONE_APP_URL,
    IPHONE_PLATFORM,
    build_connection_instruction,
    connection_app_button_text,
    connection_app_url,
)


class ConnectionInstructionTests(unittest.TestCase):
    def test_android_uses_happ_google_play_page(self):
        self.assertEqual(
            connection_app_url(ANDROID_PLATFORM),
            "https://play.google.com/store/apps/details?id=com.happproxy",
        )
        self.assertEqual(connection_app_url(ANDROID_PLATFORM), ANDROID_APP_URL)
        self.assertIn("Happ Plus", connection_app_button_text(ANDROID_PLATFORM))
        self.assertIn("Google Play", connection_app_button_text(ANDROID_PLATFORM))

    def test_iphone_uses_incy_app_store_page(self):
        self.assertEqual(
            connection_app_url(IPHONE_PLATFORM),
            "https://apps.apple.com/ru/app/incy/id6756943388",
        )
        self.assertEqual(connection_app_url(IPHONE_PLATFORM), IPHONE_APP_URL)
        self.assertIn("INCY", connection_app_button_text(IPHONE_PLATFORM))
        self.assertIn("App Store", connection_app_button_text(IPHONE_PLATFORM))

    def test_general_instructions_are_platform_specific(self):
        android = build_connection_instruction(
            ANDROID_PLATFORM,
            support_url="https://t.me/support",
        )
        iphone = build_connection_instruction(
            IPHONE_PLATFORM,
            support_url="https://t.me/support",
        )

        self.assertIn("Подключение на Android", android)
        self.assertIn("Happ Plus", android)
        self.assertNotIn("INCY", android)
        self.assertIn("Подключение на iPhone", iphone)
        self.assertIn("INCY", iphone)
        self.assertNotIn("Happ Plus", iphone)

    def test_subscription_instruction_contains_escaped_key(self):
        text = build_connection_instruction(
            IPHONE_PLATFORM,
            support_url="https://t.me/support",
            subscription_url="https://sub.example/key?a=1&b=2",
        )

        self.assertIn(
            "<code>https://sub.example/key?a=1&amp;b=2</code>",
            text,
        )
        self.assertNotIn("Открой <b>🔐 Мои подписки</b>", text)

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(ValueError):
            build_connection_instruction("windows", support_url="https://t.me/support")


class ConnectionInstructionHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _callback(data: str):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=1001),
            answer=AsyncMock(),
        )

    async def test_general_connection_flow_starts_with_platform_choice(self):
        callback = self._callback("instruction_connect")
        state = AsyncMock()

        with patch("handlers.callbacks.edit_text_with_photo", new_callable=AsyncMock) as edit:
            await callbacks.process_instruction_connect(callback, state)

        keyboard = edit.await_args.args[2]
        callback_data = [row[0].callback_data for row in keyboard.inline_keyboard]
        self.assertIn("instruction_connect_android", callback_data)
        self.assertIn("instruction_connect_iphone", callback_data)
        state.clear.assert_awaited_once()

    async def test_instruction_section_immediately_offers_both_platforms(self):
        callback = self._callback("how_to_connect")
        state = AsyncMock()

        with patch("handlers.callbacks.edit_text_with_photo", new_callable=AsyncMock) as edit:
            await callbacks.process_how_to_connect(callback, state)

        keyboard = edit.await_args.args[2]
        callback_data = [row[0].callback_data for row in keyboard.inline_keyboard]
        self.assertIn("instruction_connect_android", callback_data)
        self.assertIn("instruction_connect_iphone", callback_data)
        self.assertIn("instruction_buy", callback_data)

    async def test_android_handler_attaches_google_play_button(self):
        callback = self._callback("instruction_connect_android")
        state = AsyncMock()

        with patch("handlers.callbacks.edit_text_with_photo", new_callable=AsyncMock) as edit:
            await callbacks.process_instruction_connect_platform(callback, state)

        text, keyboard = edit.await_args.args[1:3]
        self.assertIn("Happ Plus", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].url, ANDROID_APP_URL)

    async def test_subscription_instruction_starts_with_platform_choice(self):
        callback = self._callback("subscription_instruction_17")
        visible_subscription = {"generation": "v2", "is_visible": True}

        with (
            patch("handlers.subscription.db.get_subscription_by_id", new=AsyncMock(return_value=visible_subscription)),
            patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock) as edit,
        ):
            await subscription._show_subscription_instruction(callback, 17)

        keyboard = edit.await_args.args[2]
        callback_data = [row[0].callback_data for row in keyboard.inline_keyboard]
        self.assertIn("subscription_instruction_android_17", callback_data)
        self.assertIn("subscription_instruction_iphone_17", callback_data)

    async def test_iphone_subscription_instruction_attaches_app_store_button(self):
        callback = self._callback("subscription_instruction_iphone_17")
        visible_subscription = {"generation": "v2", "is_visible": True}

        with (
            patch("handlers.subscription.db.get_subscription_by_id", new=AsyncMock(return_value=visible_subscription)),
            patch(
                "handlers.subscription._get_subscription_access_data",
                new=AsyncMock(return_value=("https://sub.example/key", "30д", None)),
            ),
            patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock) as edit,
        ):
            await subscription._show_subscription_instruction_platform(callback, 17, IPHONE_PLATFORM)

        text, keyboard = edit.await_args.args[1:3]
        self.assertIn("INCY", text)
        self.assertIn("<code>https://sub.example/key</code>", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].url, IPHONE_APP_URL)


if __name__ == "__main__":
    unittest.main()
