import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import ADMIN_ID
from handlers import callbacks, smart_assistant, start, subscription


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
    def test_main_menu_has_requested_rows(self):
        text, keyboard = start.build_main_menu()
        labels = _button_texts(keyboard)

        self.assertIn("Way SPN", text)
        self.assertEqual([[b.text for b in row] for row in keyboard.inline_keyboard], [
            ["📱 Личный кабинет"],
            ["🛒 Купить подписку", "🔑 Мои подписки"],
            ["💰 Заработать"],
            ["📲 Как подключиться"],
            ["🆘 Помощь", "📢 Новости"],
        ])
        self.assertTrue(all(len(label) <= 24 for label in labels))
        self.assertNotIn("Купить ГБ", " ".join(labels))
        self.assertIsNotNone(keyboard.inline_keyboard[0][0].web_app)
        self.assertEqual(keyboard.inline_keyboard[1][0].callback_data, "buy_subscription")
        self.assertEqual(keyboard.inline_keyboard[1][1].callback_data, "my_subscriptions")
        self.assertEqual(keyboard.inline_keyboard[-1][1].url, start.news_channel_url())

    def test_welcome_menu_tells_new_user_what_to_press(self):
        text, keyboard = start.build_main_menu(welcome=True)

        self.assertIn("Всё готово", text)
        self.assertIn("Купить подписку", text)
        self.assertEqual(keyboard.inline_keyboard[1][0].callback_data, "buy_subscription")

    def test_admin_panel_only_appears_for_admin(self):
        with patch.object(start, 'ADMIN_ID', 999):
            for user_id in (None, 123, 999):
                _, keyboard = start.build_main_menu(user_id)
                labels = _button_texts(keyboard)
                self.assertEqual('🛠 Админ-панель' in labels, user_id == 999)
                if user_id == 999:
                    self.assertEqual(keyboard.inline_keyboard[-1][0].web_app.url, start.ADMIN_PANEL_URL)

    async def test_start_and_callback_return_preserve_admin_menu(self):
        with patch.object(start, 'ADMIN_ID', 999):
            message = SimpleNamespace(from_user=SimpleNamespace(id=999))
            with patch.object(start, 'send_text_with_photo', new_callable=AsyncMock) as send:
                await start.show_main_menu(message)
            self.assertIn('🛠 Админ-панель', _button_texts(send.await_args.args[2]))
            callback = _callback('back_to_menu')
            callback.from_user.id = 999
            state = AsyncMock()
            with patch.object(callbacks, 'edit_text_with_photo', new_callable=AsyncMock) as edit:
                await callbacks.back_to_menu(callback, state)
            self.assertIn('🛠 Админ-панель', _button_texts(edit.await_args.args[2]))
            state.clear.assert_awaited_once()

    @patch("handlers.start.db.is_partner", new_callable=AsyncMock, return_value=False)
    async def test_more_menu_contains_secondary_actions(self, _is_partner):
        _text, keyboard = await start.build_more_menu(123)
        labels = _button_texts(keyboard)

        self.assertEqual(labels, [
            "📱 Личный кабинет",
            "📢 Новости",
            "← Назад",
        ])
        self.assertTrue(all(len(label) <= 24 for label in labels))

    @patch("handlers.start.db.is_partner", new_callable=AsyncMock, return_value=True)
    async def test_legacy_more_menu_keeps_partner_access(self, _is_partner):
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

    async def test_referral_payment_button_is_always_available_for_new_and_renewal(self):
        callback = _callback("tariff_regular_1m")
        state = AsyncMock()
        state.get_data.return_value = {
            "purchase_mode": "new",
            "target_slot_number": 1,
            "target_subscription_id": None,
        }

        for mode in ('new', 'renew'):
            state.get_data.return_value['purchase_mode'] = mode
            with (
                patch("handlers.subscription.current_price", new=AsyncMock(return_value={"price": 200})),
                patch("handlers.subscription.db.get_referral_stats", new=AsyncMock()) as stats,
                patch("handlers.subscription.edit_text_with_photo", new_callable=AsyncMock) as edit,
            ):
                await subscription.process_tariff_choice(callback, state)
                labels = _button_texts(edit.await_args.args[2])
                self.assertIn("💰 Реферальный баланс", labels)
                buttons = [b for row in edit.await_args.args[2].inline_keyboard for b in row]
                self.assertEqual(sum(b.callback_data == 'pay_referral_balance' for b in buttons), 1)
                stats.assert_not_awaited()

    async def test_insufficient_referral_balance_shows_alert_without_starting_purchase(self):
        for balance in (0, 199):
            callback = _callback('pay_referral_balance')
            state = AsyncMock()
            state.get_data.return_value = {'tariff_code':'regular_1m', 'purchase_mode':'new'}
            with (
                patch('handlers.subscription.current_price', new=AsyncMock(return_value={'price':200})),
                patch('handlers.subscription.db.acquire_user_lock', new=AsyncMock(return_value=True)),
                patch('handlers.subscription.db.release_user_lock', new=AsyncMock()) as release,
                patch('handlers.subscription.db.get_referral_stats', new=AsyncMock(return_value={'current_balance':balance})),
                patch('handlers.subscription._get_or_create_target_subscription_for_direct_flow', new=AsyncMock()) as purchase,
                patch('handlers.subscription.db.spend_referral_balance_for_subscription', new=AsyncMock()) as spend,
            ):
                await subscription.process_pay_referral_balance(callback, state)
            callback.answer.assert_awaited_once()
            alert = callback.answer.await_args
            self.assertTrue(alert.kwargs['show_alert'])
            self.assertIn(f'Ваш баланс: {balance:.2f} ₽', alert.args[0])
            self.assertIn(f'Не хватает: {200-balance:.2f} ₽', alert.args[0])
            self.assertLessEqual(len(alert.args[0]), 200)
            purchase.assert_not_awaited()
            spend.assert_not_awaited()
            state.clear.assert_not_awaited()
            release.assert_awaited_once_with(123)


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
    async def test_active_subscription_card_has_requested_rows_without_delete(
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
        self.assertEqual([[b.text for b in row] for row in edit.await_args.args[2].inline_keyboard], [
            ['📲 Подключить'],
            ['🔄 Продлить'],
            ['📱 Устройства'],
            ['➕ Докупить устройства', '📦 Докупить ГБ'],
            ['← Назад'],
        ])
        self.assertTrue(all(len(label) <= 24 for label in labels))

    async def test_delete_button_uses_effective_expiry_and_no_date_is_not_expired(self):
        base = dict(id=7, tg_id=123, generation='v2', is_visible=True, is_renewable=True,
                    plan_kind='regular', type_index=1, remnawave_uuid=None)
        future = datetime.now(timezone.utc) + timedelta(days=10)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        for local, effective, expired in ((future, past, True), (past, future, False), (None, None, False)):
            with (
                self.subTest(local=local, effective=effective),
                patch.object(subscription.db, 'get_subscription_by_id', new=AsyncMock(return_value={**base, 'subscription_until':local})),
                patch.object(subscription.db, 'get_active_device_addon_count', new=AsyncMock(return_value=0)),
                patch.object(subscription, '_get_subscription_access_data', new=AsyncMock(return_value=('https://sub.example/key', '1д', effective))),
                patch.object(subscription, 'available_device_addon_packages', return_value=[]),
                patch.object(subscription, 'edit_text_with_photo', new_callable=AsyncMock) as edit,
            ):
                await subscription._show_subscription_card(_callback(), 7)
            rows = edit.await_args.args[2].inline_keyboard
            self.assertEqual('🗑 Удалить' in _button_texts(edit.await_args.args[2]), expired)
            self.assertEqual(rows[-1][0].callback_data, 'my_subscriptions')
            if expired:
                self.assertEqual(rows[-2][0].callback_data, 'delete_subscription_7')


class SubscriptionNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_renewal_tariff_back_returns_to_selected_subscription(self):
        state = AsyncMock()
        state.get_data.return_value = {'purchase_mode':'renew', 'plan_kind':'bypass', 'target_subscription_id':7}
        with (
            patch.object(subscription.db, 'get_active_discounts', new=AsyncMock(return_value=[])),
            patch.object(subscription, 'edit_text_with_photo', new_callable=AsyncMock) as edit,
        ):
            await subscription._show_tariff_selection(_callback(), state, 'Продление')
        self.assertEqual(edit.await_args.args[2].inline_keyboard[-1][0].callback_data, 'subscription_view_7')
        with patch.object(subscription, '_show_subscription_card', new_callable=AsyncMock) as card:
            callback = _callback('subscription_view_7')
            await subscription.process_subscription_view(callback, state)
        card.assert_awaited_once_with(callback, 7)
        state.clear.assert_awaited_once()

    async def test_direct_gb_purchase_back_returns_to_selected_subscription(self):
        state = AsyncMock()
        record = dict(id=7, tg_id=123, slot_number=1, generation='v2', is_visible=True, is_renewable=True, plan_kind='bypass')
        with (
            patch.object(subscription.db, 'get_subscription_by_id', new=AsyncMock(return_value=record)) as get,
            patch.object(subscription.db, 'get_active_discounts', new=AsyncMock(return_value=[])),
            patch.object(subscription, 'edit_text_with_photo', new_callable=AsyncMock) as edit,
        ):
            await subscription.process_gb_subscription_choice(_callback('gb_sub_7'), state)
        get.assert_awaited_once_with(7, 123)
        state.update_data.assert_awaited_once_with(gb_subscription_id=7)
        self.assertEqual(edit.await_args.args[2].inline_keyboard[-1][0].callback_data, 'subscription_view_7')

    async def test_gb_payment_methods_back_keeps_subscription_context(self):
        state = AsyncMock()
        state.get_data.return_value = {'gb_subscription_id':7}
        code = next(iter(subscription.BYPASS_TRAFFIC_PACKAGES))
        with (
            patch.object(subscription, 'current_price', new=AsyncMock(return_value={'price':100})),
            patch.object(subscription, 'edit_text_with_photo', new_callable=AsyncMock) as edit,
        ):
            await subscription.process_gb_package_choice(_callback(f'gb_package_{code}'), state)
        self.assertEqual(edit.await_args.args[2].inline_keyboard[-1][0].callback_data, 'gb_sub_7')

    async def test_stale_gb_package_click_does_not_open_payment_methods(self):
        state = AsyncMock()
        state.get_data.return_value = {}
        code = next(iter(subscription.BYPASS_TRAFFIC_PACKAGES))
        callback = _callback(f'gb_package_{code}')
        with patch.object(subscription, 'edit_text_with_photo', new_callable=AsyncMock) as edit:
            await subscription.process_gb_package_choice(callback, state)
        edit.assert_not_awaited()
        callback.answer.assert_awaited_once_with('Выберите подписку заново.', show_alert=True)

    async def test_subscription_invoice_back_keeps_renewal_context_for_both_providers(self):
        for provider in ('cryptobot', 'yookassa'):
            for reused in (False, True):
                with self.subTest(provider=provider, reused=reused):
                    state = AsyncMock()
                    state.get_data.return_value = {'tariff_code':'regular_1m', 'purchase_mode':'renew',
                        'target_subscription_id':7, 'target_slot_number':1}
                    callback = _callback(f'pay_{provider}')
                    callback.bot = SimpleNamespace()
                    invoice = {'invoice_id':123, 'status':'active', 'bot_invoice_url':'https://pay.example/invoice'}
                    payment = {'id':'test-payment', 'status':'pending', 'confirmation':{'confirmation_url':'https://pay.example/payment'}}
                    with (
                        patch.object(subscription, 'current_price', new=AsyncMock(return_value={'price':200})),
                        patch.object(subscription.db, 'get_active_payment_for_user_and_tariff', new=AsyncMock(return_value='existing' if reused else None)),
                        patch.object(subscription.db, 'create_payment', new_callable=AsyncMock) as create,
                        patch.object(subscription, 'get_invoice_status', new=AsyncMock(return_value=invoice)),
                        patch.object(subscription, 'get_payment_status', new=AsyncMock(return_value=payment)),
                        patch.object(subscription, 'create_cryptobot_invoice', new=AsyncMock(return_value=invoice)),
                        patch.object(subscription, 'create_yookassa_payment', new=AsyncMock(return_value=payment)),
                        patch.object(subscription, 'edit_text_with_photo', new_callable=AsyncMock) as edit,
                    ):
                        await getattr(subscription, f'process_pay_{provider}')(callback, state)
                    self.assertEqual(edit.await_args.args[2].inline_keyboard[-1][0].callback_data, 'subscription_view_7')
                    self.assertEqual(create.await_count, 0 if reused else 1)
                    if not reused:
                        self.assertEqual(create.await_args.kwargs['subscription_id'], 7)
                        self.assertEqual(create.await_args.kwargs['payment_target'], 'renew')
                    state.clear.assert_awaited_once()

    async def test_active_or_unknown_expiry_cannot_open_delete_confirmation(self):
        for until in (None, datetime.now(timezone.utc) + timedelta(days=1)):
            callback = _callback('delete_subscription_7')
            with (
                patch.object(subscription.db, 'get_subscription_by_id', new=AsyncMock(return_value={
                    'id':7, 'tg_id':123, 'generation':'v2', 'is_visible':True, 'subscription_until':until})),
                patch.object(subscription, 'edit_text_with_photo', new_callable=AsyncMock) as edit,
            ):
                await subscription.process_delete_subscription_request(callback, AsyncMock())
            edit.assert_not_awaited()
            callback.answer.assert_awaited_once_with('Удалить можно только истёкшую подписку.', show_alert=True)

    async def test_delete_confirmation_requires_expired_even_for_old_button(self):
        callback = _callback('delete_subscription_confirm_7')
        with patch.object(subscription, 'delete_subscription_everywhere', new=AsyncMock(
            side_effect=subscription.SubscriptionActiveError('Удалить можно только истёкшую подписку.'))) as delete:
            await subscription.process_delete_subscription_confirm(callback, AsyncMock())
        delete.assert_awaited_once_with(7, tg_id=123, actor='user_bot', require_expired=True)
        callback.answer.assert_awaited_once_with('Удалить можно только истёкшую подписку.', show_alert=True)


class SmartAssistantSimplificationTests(unittest.TestCase):
    def test_refund_intent_sends_user_to_support_without_refund_button(self):
        _text, keyboard = smart_assistant._response_for_intent("refund")
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertNotIn("refund_start", [button.callback_data for button in buttons])
        self.assertTrue(any(button.url for button in buttons))


if __name__ == "__main__":
    unittest.main()
