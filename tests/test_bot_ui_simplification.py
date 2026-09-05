import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import ADMIN_ID
from handlers import smart_assistant, start, subscription


def _callback(data: str = ""):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=123, username="tester"),
        message=SimpleNamespace(chat=SimpleNamespace(id=123)),
        answer=AsyncMock(),
    )


def _button_texts(keyboard):
    return [button.text for row in keyboard.inline_keyboard for button in row]


class MainMenuSimplificationTests(unittest.IsolatedAsyncioTestCase):
    def test_main_menu_has_only_five_short_actions(self):
        text, keyboard = start.build_main_menu()
        labels = _button_texts(keyboard)

        self.assertIn("Way SPN", text)
        self.assertEqual(labels, [
            "🛒 Купить подписку",
            "🔑 Мои подписки",
            "📲 Как подключить",
            "🆘 Помощь",
            "⋯ Ещё",
        ])
        self.assertTrue(all(len(label) <= 24 for label in labels))
        self.assertNotIn("Личный кабинет", " ".join(labels))
        self.assertNotIn("Купить ГБ", " ".join(labels))

    def test_welcome_menu_tells_new_user_what_to_press(self):
        text, keyboard = start.build_main_menu(welcome=True)

        self.assertIn("Всё готово", text)
        self.assertIn("Купить подписку", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "buy_subscription")

    @patch("handlers.start.db.is_partner", new_callable=AsyncMock, return_value=False)
    async def test_more_menu_contains_secondary_actions(self, _is_partner):
        _text, keyboard = await start.build_more_menu(123)
        labels = _button_texts(keyboard)

        self.assertEqual(labels, [
            "📱 Личный кабинет",
            "📢 Новости",
            "👥 Пригласить друга",
            "← Назад",
        ])
        self.assertTrue(all(len(label) <= 24 for label in labels))

    @patch("handlers.start.db.is_partner", new_callable=AsyncMock, return_value=True)
    async def test_admin_and_partner_actions_only_appear_in_more(self, _is_partner):
        _text, keyboard = await start.build_more_menu(ADMIN_ID)
        labels = _button_texts(keyboard)

        self.assertIn("🤝 Партнёрство", labels)
        self.assertIn("🛠 Админ-панель", labels)


class PurchaseSimplificationTests(unittest.IsolatedAsyncioTestCase):
    @patch("handlers.subscription._show_new_subscription_type_choice", new_callable=AsyncMock)
    @patch("handlers.subscription.db.get_renewable_subscriptions", new_callable=AsyncMock, return_value=[])
    @patch("handlers.subscription.db.get_visible_subscriptions", new_callable=AsyncMock, return_value=[])
    async def test_first_purchase_skips_intermediate_hub(self, _visible, _renewable, show_types):
        state = AsyncMock()

        await subscription._show_subscriptions_hub(_callback("buy_subscription"), state)

        show_types.assert_awaited_once()
        self.assertEqual(show_types.await_args.kwargs["back_callback"], "back_to_menu")

    @patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock)
    @patch("handlers.subscription.db.get_active_discounts", new_callable=AsyncMock, return_value=[])
    async def test_tariff_buttons_contain_only_period_and_price(self, _discounts, edit):
        state = AsyncMock()
        state.get_data.return_value = {"plan_kind": "bypass", "purchase_mode": "new"}

        await subscription._show_tariff_selection(_callback(), state, "Выберите срок")

        keyboard = edit.await_args.args[2]
        labels = _button_texts(keyboard)
        self.assertIn("30 дней — 300₽", labels)
        self.assertIn("90 дней — 800₽", labels)
        self.assertNotIn("устройств", " ".join(labels))
        self.assertTrue(all(len(label) <= 32 for label in labels))

    @patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock)
    @patch("handlers.subscription.db.get_active_discounts", new_callable=AsyncMock)
    async def test_discounted_tariff_uses_crossed_out_old_price_without_arrow(self, discounts, edit):
        discounts.return_value = [{
            "id": 1,
            "name": "20%",
            "discount_type": "percent",
            "value": 20,
            "target_type": "bypass",
            "target_code": None,
        }]
        state = AsyncMock()
        state.get_data.return_value = {"plan_kind": "bypass", "purchase_mode": "new"}

        await subscription._show_tariff_selection(_callback(), state, "Выберите срок")

        labels = _button_texts(edit.await_args.args[2])
        month_label = next(label for label in labels if label.startswith("30 дней"))
        self.assertEqual(month_label, "30 дней — 3̶0̶0̶₽ 240₽")
        self.assertNotIn("→", month_label)

    async def test_bonus_payment_button_only_appears_when_balance_is_enough(self):
        callback = _callback("tariff_regular_1m")
        state = AsyncMock()
        state.get_data.return_value = {
            "purchase_mode": "new",
            "target_slot_number": 1,
            "target_subscription_id": None,
        }

        for balance, expected in ((199, False), (200, True)):
            with (
                patch("handlers.subscription.current_price", new=AsyncMock(return_value={"price": 200})),
                patch("handlers.subscription.db.get_referral_stats", new=AsyncMock(return_value={"current_balance": balance})),
                patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock) as edit,
            ):
                await subscription.process_tariff_choice(callback, state)
                labels = _button_texts(edit.await_args.args[2])
                self.assertEqual("💰 Бонусный баланс" in labels, expected)


class SubscriptionSimplificationTests(unittest.IsolatedAsyncioTestCase):
    @patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock)
    @patch("handlers.subscription.db.get_bot_visible_subscriptions", new_callable=AsyncMock, return_value=[])
    async def test_empty_subscriptions_is_a_normal_screen(self, _subscriptions, edit):
        callback = _callback("my_subscriptions")
        state = AsyncMock()

        await subscription._show_my_subscriptions_type_choice(callback, state)

        text, keyboard = edit.await_args.args[1:3]
        self.assertIn("нет подписок", text.lower())
        self.assertIn("🛒 Купить подписку", _button_texts(keyboard))
        callback.answer.assert_not_awaited()

    @patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock)
    @patch("handlers.subscription.db.get_bot_visible_subscriptions", new_callable=AsyncMock)
    async def test_all_subscription_types_are_shown_in_one_short_list(self, get_subscriptions, edit):
        get_subscriptions.return_value = [
            {
                "id": 1,
                "plan_kind": "bypass",
                "type_index": 1,
                "slot_number": 1,
                "generation": "v2",
                "is_visible": True,
                "subscription_until": datetime.utcnow() + timedelta(days=10),
            },
            {
                "id": 2,
                "plan_kind": "regular",
                "type_index": 2,
                "slot_number": 2,
                "generation": "v2",
                "is_visible": True,
                "subscription_until": datetime.utcnow() - timedelta(days=1),
            },
        ]

        await subscription._show_my_subscriptions_type_choice(_callback(), AsyncMock())

        labels = _button_texts(edit.await_args.args[2])
        subscription_labels = labels[:2]
        self.assertIn("Антиглушилка", subscription_labels[0])
        self.assertIn("Обычная", subscription_labels[1])
        self.assertTrue(all(len(label) <= 32 for label in subscription_labels))

    @patch("handlers.subscription.available_device_addon_packages", return_value=[{"count": 1}])
    @patch("handlers.subscription.db.get_active_device_addon_count", new_callable=AsyncMock, return_value=0)
    @patch("handlers.subscription._get_subscription_access_data", new_callable=AsyncMock)
    @patch("handlers.subscription.db.get_subscription_by_id", new_callable=AsyncMock)
    @patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock)
    async def test_subscription_card_keeps_all_actions_but_shortens_them(
        self,
        edit,
        get_subscription,
        get_access,
        _addons,
        _packages,
    ):
        expires_at = datetime.utcnow() + timedelta(days=10)
        get_subscription.return_value = {
            "id": 7,
            "tg_id": 123,
            "plan_kind": "bypass",
            "type_index": 1,
            "slot_number": 1,
            "generation": "v2",
            "is_visible": True,
            "is_renewable": True,
            "subscription_until": expires_at,
            "remnawave_uuid": None,
            "hwid_device_limit": 3,
            "current_period_limit_bytes": 200 * 1024 ** 3,
            "last_known_used_traffic_bytes": 10 * 1024 ** 3,
            "traffic_reset_at": expires_at,
        }
        get_access.return_value = ("https://sub.example/key", "10д", expires_at)

        await subscription._show_subscription_card(_callback(), 7, back_callback="my_subscriptions")

        labels = _button_texts(edit.await_args.args[2])
        for expected in (
            "📲 Подключить",
            "📱 Устройства",
            "🔄 Продлить",
            "➕ Добавить устройства",
            "📦 Докупить ГБ",
            "🗑 Удалить",
            "← Назад",
        ):
            self.assertIn(expected, labels)
        self.assertTrue(all(len(label) <= 24 for label in labels))


class SmartAssistantSimplificationTests(unittest.TestCase):
    def test_refund_intent_sends_user_to_support_without_refund_button(self):
        _text, keyboard = smart_assistant._response_for_intent("refund")
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertNotIn("refund_start", [button.callback_data for button in buttons])
        self.assertTrue(any(button.url for button in buttons))


if __name__ == "__main__":
    unittest.main()
