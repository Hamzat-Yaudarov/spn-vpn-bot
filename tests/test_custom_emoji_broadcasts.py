import unittest
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import admin, start
from services import custom_emoji


def _entity(custom_id: str, *, entity_type="custom_emoji"):
    return SimpleNamespace(
        type=SimpleNamespace(value=entity_type),
        custom_emoji_id=custom_id,
    )


class CustomEmojiButtonTests(unittest.TestCase):
    def test_button_uses_regular_emoji_until_custom_id_is_configured(self):
        with patch.dict(custom_emoji.WAY_SPN_CUSTOM_EMOJI_IDS, {}, clear=True):
            button = custom_emoji.custom_emoji_button(
                "Купить",
                emoji_key="buy",
                fallback_emoji="🛒",
                callback_data="buy",
            )

        self.assertEqual(button.text, "🛒 Купить")
        self.assertNotIn("icon_custom_emoji_id", button.model_dump(exclude_none=True))

    def test_button_uses_custom_icon_without_duplicate_regular_emoji(self):
        with patch.dict(custom_emoji.WAY_SPN_CUSTOM_EMOJI_IDS, {"buy": "54321"}, clear=True):
            button = custom_emoji.custom_emoji_button(
                "Купить",
                emoji_key="buy",
                fallback_emoji="🛒",
                callback_data="buy",
            )

        payload = button.model_dump(exclude_none=True)
        self.assertEqual(button.text, "Купить")
        self.assertEqual(payload["icon_custom_emoji_id"], "54321")

    def test_main_menu_is_ready_for_custom_icons(self):
        with patch.dict(custom_emoji.WAY_SPN_CUSTOM_EMOJI_IDS, {"buy": "777"}, clear=True):
            _text, keyboard = start.build_main_menu()

        buy_button = keyboard.inline_keyboard[0][0]
        self.assertEqual(buy_button.text, "Купить подписку")
        self.assertEqual(buy_button.model_dump(exclude_none=True)["icon_custom_emoji_id"], "777")


class CustomEmojiBroadcastTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_custom_emoji_from_text_and_media_caption(self):
        message = SimpleNamespace(
            entities=[_entity("100"), _entity("ignored", entity_type="bold")],
            caption_entities=[_entity("200"), _entity("100")],
        )

        self.assertEqual(admin._message_custom_emoji_ids(message), ["100", "200", "100"])

    def test_broadcast_summary_confirms_detected_custom_emoji(self):
        text = admin._broadcast_summary_text("all", [], ["100", "200"])
        self.assertIn("Премиум-эмодзи: <b>2</b>", text)

    async def test_emoji_ids_command_builds_ready_env_mapping_for_all_semantic_icons(self):
        emoji_ids = [str(index) for index in range(1, len(custom_emoji.CUSTOM_EMOJI_KEYS) + 1)]
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            reply_to_message=SimpleNamespace(
                entities=[_entity(custom_id) for custom_id in emoji_ids],
                caption_entities=[],
            ),
            answer=AsyncMock(),
        )

        with patch.object(admin, "is_admin", return_value=True):
            await admin.admin_custom_emoji_ids(message)

        response = message.answer.await_args.args[0]
        expected = dict(zip(custom_emoji.CUSTOM_EMOJI_KEYS, emoji_ids))
        expected_json = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        self.assertIn("Готовая строка для .env", response)
        self.assertIn(expected_json.replace('"', '&quot;'), response)

    async def test_preview_copies_original_message_so_entities_are_preserved(self):
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            bot=SimpleNamespace(copy_message=AsyncMock()),
            answer=AsyncMock(),
        )
        state = AsyncMock()
        state.get_data.return_value = {
            "source_chat_id": 456,
            "source_message_id": 789,
            "selected_buttons": [],
        }

        result = await admin._send_broadcast_preview(callback, state)

        self.assertTrue(result)
        callback.bot.copy_message.assert_awaited_once_with(
            chat_id=123,
            from_chat_id=456,
            message_id=789,
            reply_markup=None,
        )


if __name__ == "__main__":
    unittest.main()
