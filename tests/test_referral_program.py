import os
import unittest
from decimal import Decimal
from datetime import datetime
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
try:
    from fastapi.testclient import TestClient
except RuntimeError:  # httpx is a test-only dependency, not needed by the bot.
    TestClient = None
from fastapi import FastAPI
import admin_web
import database as db
from handlers import referral
from handlers.withdrawals import install_withdrawal_handlers
from services import referral_store as store
from services import custom_emoji
from services.image_handler import edit_message_text
from services.referral_program import RULES, SHARE_TEXT, parse_amount, validate_details, share_link, referral_link
from test_payment_recovery_postgres import Bridge


def assert_telegram_copy(test, text, max_chars=4096):
    """Validate the small HTML subset used here without sending real messages."""
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.plain = [], []

        def handle_starttag(self, tag, attrs):
            test.assertIn(tag, ('b', 'code', 'blockquote'))
            test.assertFalse(attrs)
            if tag == 'blockquote':
                test.assertNotIn('blockquote', self.stack)
            if tag == 'code':
                test.assertEqual(self.stack, [])
            self.stack.append(tag)

        def handle_endtag(self, tag):
            test.assertTrue(self.stack)
            test.assertEqual(self.stack.pop(), tag)

        def handle_data(self, data):
            self.plain.append(data)

    parser = Parser()
    parser.feed(text)
    parser.close()
    test.assertEqual(parser.stack, [])
    test.assertLessEqual(len(''.join(parser.plain).encode('utf-16-le')) // 2, max_chars)


class ValidationTests(unittest.TestCase):
    def test_new_minimum_and_local_number_format(self):
        self.assertEqual(parse_amount('1 500,50'), Decimal('1500.50'))
        self.assertEqual(parse_amount('1500'), Decimal('1500'))
        for value in ('1499.99', '-1500', 'NaN', 'Infinity', 'sNaN', '1500.001', '', None, '1e1000'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_amount(value)

    def test_details(self):
        self.assertEqual(validate_details('sbp', 'Банк', '+7 (999) 123-45-67')[1], '+79991234567')
        validate_details('usdt', address='T' + '1' * 33)
        validate_details('usdt', address='0x' + 'a' * 40)
        for method, details in [('wire', {}), ('sbp', {'bank':'Банк','phone':'79991234567'}), ('usdt', {'address':'x'*34})]:
            with self.assertRaises(ValueError):
                validate_details(method, **details)

    def test_sharing_has_personal_link_and_disclosure(self):
        link = referral_link('@WaySPN_robot', 123)
        payload = parse_qs(urlparse(share_link(link)).query)
        self.assertEqual(payload['url'], [link])
        self.assertEqual(payload['text'], [SHARE_TEXT])
        self.assertIn('я получу бонус', payload['text'][0])
        self.assertNotIn('<blockquote>', payload['text'][0])

    def test_screen_actions_and_honest_progress(self):
        text, keyboard = referral.earning_screen({'current_balance':1400, 'total_earned':2000, 'active_referrals':3}, 'https://t.me/test?start=ref_123')
        self.assertIn('осталось 100.00 ₽', text)
        self.assertIn('1 500 ₽', text)
        self.assertNotIn('5000', text)
        self.assertEqual(keyboard.inline_keyboard[1][0].copy_text.text, 'https://t.me/test?start=ref_123')
        self.assertEqual(text.count('<blockquote>'), 2)
        self.assertIn('35%', text)
        self.assertIn('15%', text)
        self.assertIn('впервые', text)
        assert_telegram_copy(self, text, 800)

    def test_ready_balance_and_escaped_personal_link(self):
        link = 'https://t.me/test?start=ref_123&test=<hello>'
        text, keyboard = referral.earning_screen({'current_balance':1500, 'total_earned':2000, 'active_referrals':3}, link)
        self.assertIn('Уже можно вывести деньги.', text)
        self.assertNotIn('До вывода осталось', text)
        self.assertIn('&amp;test=&lt;hello&gt;', text)
        self.assertEqual(keyboard.inline_keyboard[1][0].copy_text.text, link)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]
        self.assertIn('referral_spend', callbacks)
        self.assertIn('referral_withdraw', callbacks)
        assert_telegram_copy(self, text, 800)

    def test_rules_keep_rates_example_and_payout_conditions(self):
        for value in ('35%', '15%', '105 ₽', '45 ₽', '1 500 ₽', 'скидкой', 'после проверки заявки', 'впервые'):
            self.assertIn(value, RULES)
        assert_telegram_copy(self, RULES, 800)

    def test_earning_buttons_do_not_inherit_people_premium_icon(self):
        icons = {'invite':'111', 'buy':'222', 'bank_card':'333', 'home':'444', 'back':'555'}
        link = 'https://t.me/test?start=ref_123'
        with patch.object(custom_emoji, 'WAY_SPN_CUSTOM_EMOJI_IDS', icons):
            _, keyboard = referral.earning_screen({'current_balance':1500, 'total_earned':2000, 'active_referrals':3}, link)
        buttons = [button.model_dump(exclude_none=True) for row in keyboard.inline_keyboard for button in row]
        self.assertTrue(all(button.get('icon_custom_emoji_id') != icons['invite'] for button in buttons))
        for index in (0, 1, 4, 5):
            self.assertNotIn('icon_custom_emoji_id', buttons[index])
        self.assertEqual(buttons[0]['text'], '📨 Пригласить друга')
        self.assertEqual(parse_qs(urlparse(buttons[0]['url']).query)['url'], [link])
        self.assertEqual(buttons[1]['copy_text']['text'], link)
        self.assertEqual(buttons[2]['icon_custom_emoji_id'], icons['buy'])
        self.assertEqual(buttons[2]['callback_data'], 'referral_spend')
        self.assertEqual(buttons[3]['icon_custom_emoji_id'], icons['bank_card'])

    @unittest.skipUnless(TestClient, 'Optional httpx test client is not installed')
    def test_admin_routes_require_authentication(self):
        app = FastAPI()
        app.include_router(admin_web.router)
        client = TestClient(app)
        self.assertEqual(client.get('/admin/api/withdrawals').status_code, 401)
        self.assertEqual(client.post('/admin/api/withdrawals/referral/1', json={'status':'completed','confirm_paid':True}).status_code, 401)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_updates_one_screen_including_return_and_withdrawal_form(self):
        message = SimpleNamespace(photo=None, answer=AsyncMock(), edit_text=AsyncMock(), delete=AsyncMock())
        callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=123), answer=AsyncMock(), bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='test'))))
        state = AsyncMock()
        stats = {'current_balance':2000, 'total_earned':2000, 'active_referrals':3}
        with patch.object(db, 'get_referral_stats', new=AsyncMock(return_value=stats)), patch.object(store, 'history', new=AsyncMock(return_value=[])):
            await referral.process_referral(callback, state)
            await referral.rules(callback, state)
            await referral.process_referral(callback, state)
            await referral.spend(callback, state)
            await referral.withdraw_start(callback, state)
            callback.data = 'referral_withdraw_sbp'
            router = Router()
            install_withdrawal_handlers(router, 'referral')
            start = next(h.callback for h in router.callback_query.handlers if h.callback.__name__ == 'start')
            await start(callback, state)
            callback.data = 'referral_history:0'
            await referral.show_history(callback, state)
        self.assertEqual(message.edit_text.await_count, 7)
        message.answer.assert_not_awaited()
        message.delete.assert_not_awaited()

    async def test_photo_menu_is_replaced_but_start_command_still_sends_a_screen(self):
        message = SimpleNamespace(photo=[object()], answer=AsyncMock(), delete=AsyncMock(), edit_text=AsyncMock())
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='test')))
        stats = {'current_balance':2000, 'total_earned':2000, 'active_referrals':3}
        with patch.object(db, 'get_referral_stats', new=AsyncMock(return_value=stats)):
            await referral.send_earning_screen(message, 123, bot, edit=True)
            message.delete.assert_awaited_once()
            message.answer.assert_awaited_once()
            message.edit_text.assert_not_awaited()
            user_message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock(), delete=AsyncMock())
            await referral.send_earning_screen(user_message, 123, bot)
        user_message.answer.assert_awaited_once()
        user_message.edit_text.assert_not_awaited()
        user_message.delete.assert_not_awaited()

    async def test_repeat_click_does_not_create_a_new_message_and_other_errors_surface(self):
        method = EditMessageText(chat_id=123, message_id=1, text='screen')
        message = SimpleNamespace(photo=None, answer=AsyncMock(), edit_text=AsyncMock(side_effect=TelegramBadRequest(method=method, message='Bad Request: message is not modified')))
        await edit_message_text(message, 'screen', referral.back_keyboard())
        message.answer.assert_not_awaited()
        message.edit_text.side_effect = TelegramBadRequest(method=method, message='Bad Request: message to edit not found')
        with self.assertRaises(TelegramBadRequest):
            await edit_message_text(message, 'screen', referral.back_keyboard())

    async def test_history_pagination_does_not_use_people_premium_icon(self):
        rows = [dict(kind='earning', amount=Decimal('10'), detail='regular_1m', created_at=datetime(2026,10,4)) for _ in range(11)]
        for program in ('referral', 'partner'):
            callback = SimpleNamespace(data=f'{program}_history:10', answer=AsyncMock(), message=SimpleNamespace(photo=None, edit_text=AsyncMock()), from_user=SimpleNamespace(id=123))
            with patch.object(custom_emoji, 'WAY_SPN_CUSTOM_EMOJI_IDS', {'invite':'111','back':'555'}), patch.object(store, 'history', new=AsyncMock(return_value=rows)):
                await referral.show_history(callback, AsyncMock())
            keyboard = callback.message.edit_text.call_args.kwargs['reply_markup']
            pagination = [button.model_dump(exclude_none=True) for button in keyboard.inline_keyboard[0]]
            self.assertEqual([button['text'] for button in pagination], ['Новее', 'Ранее'])
            self.assertEqual([button['callback_data'] for button in pagination], [f'{program}_history:0',f'{program}_history:20'])
            self.assertTrue(all('icon_custom_emoji_id' not in button for button in pagination))

    async def test_spending_and_withdrawal_screens_use_valid_quotes(self):
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(photo=None, edit_text=AsyncMock()), from_user=SimpleNamespace(id=123))
        state = AsyncMock()
        await referral.rules(callback, state)
        await referral.spend(callback, state)
        with patch.object(db, 'get_referral_stats', new=AsyncMock(return_value={'current_balance':2000})):
            await referral.withdraw_start(callback, state)
        for call in callback.message.edit_text.call_args_list:
            self.assertIn('<blockquote>', call.args[0])
            assert_telegram_copy(self, call.args[0], 900)

    async def test_both_withdrawal_forms_keep_minimum_and_manual_review(self):
        for program, prefix in (('referral', 'referral'), ('partner', 'partnership')):
            router = Router()
            install_withdrawal_handlers(router, program)
            start = next(h.callback for h in router.callback_query.handlers if h.callback.__name__ == 'start')
            amount = next(h.callback for h in router.message.handlers if h.callback.__name__ == 'amount')
            for method in ('sbp', 'usdt'):
                callback = SimpleNamespace(data=f'{prefix}_withdraw_{method}', from_user=SimpleNamespace(id=123), answer=AsyncMock(), message=SimpleNamespace(photo=None, edit_text=AsyncMock()))
                message = SimpleNamespace(text='1500', from_user=SimpleNamespace(id=123), answer=AsyncMock())
                state = AsyncMock()
                state.get_data.return_value = {'withdrawal_method':method}
                with patch.object(db, 'get_referral_stats', new=AsyncMock(return_value={'current_balance':2000})), patch.object(db, 'get_partner_stats', new=AsyncMock(return_value={'current_balance':2000})):
                    await start(callback, state)
                    await amount(message, state)
                text = callback.message.edit_text.call_args.args[0]
                self.assertIn('1 500 ₽', text)
                self.assertIn('после проверки заявки', text)
                assert_telegram_copy(self, text, 500)
                next_text = message.answer.call_args.args[0]
                assert_telegram_copy(self, next_text, 500)
                if method == 'usdt':
                    for term in ('TRC-20', 'ERC-20', 'рублях', 'курс', 'сумму в USDT'):
                        self.assertIn(term, next_text)

    async def test_duplicate_final_message_does_not_submit_twice(self):
        router = Router()
        install_withdrawal_handlers(router, 'referral')
        finish = next(h.callback for h in router.message.handlers if h.callback.__name__ == 'finish')
        data = {'withdrawal_request_key':'unit-request', 'withdrawal_amount':'1500', 'withdrawal_method':'sbp', 'bank_name':'Bank'}
        state = AsyncMock()
        state.get_data.side_effect = lambda: dict(data)
        state.clear.side_effect = data.clear
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), text='+79991234567', answer=AsyncMock())
        with patch.object(store, 'create_request', new=AsyncMock(return_value={'id':1,'amount':Decimal('1500')})) as create:
            await finish(message, state)
            await finish(message, state)
        create.assert_awaited_once()
        accepted = message.answer.call_args_list[0].args[0]
        self.assertIn('Заявка принята', accepted)
        self.assertIn('R-1', accepted)
        self.assertIn('отложена на выплату', accepted)
        assert_telegram_copy(self, accepted, 500)


@unittest.skipUnless(os.environ.get('PGLITE_MODULE'), 'Requires isolated PGlite database')
class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await Bridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.enterContext(patch.object(db, '_pool', self.sql))
        self.enterContext(patch.object(store, 'ADMIN_ID', 999))
        for statement in store.SCHEMA.split(';'):
            if statement.strip():
                await self.sql.execute(statement)
        await self.sql.execute('INSERT INTO users(tg_id) VALUES(123),(456),(999)')
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(123,456,'regular_1m',20000,2000)")

    async def request(self, key='test', amount='1500'):
        return await store.create_request('referral', 123, amount, 'sbp', key, bank='Bank <test>', phone='+79991234567')

    async def test_reserved_balance_duplicate_and_insufficient_funds(self):
        first = await self.request()
        repeat = await self.request()
        self.assertEqual(first['id'], repeat['id'])
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 1)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_outbox'), 1)
        self.assertIn('&lt;test&gt;', await self.sql.fetchval('SELECT body FROM referral_outbox'))
        stats = await db.get_referral_stats(123)
        self.assertEqual(stats['current_balance'], 500)
        with self.assertRaises(ValueError):
            await self.request('second')
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 1)

    async def test_reject_releases_funds_only_once_and_completion_never_transfers(self):
        row = await self.request()
        await store.resolve_request('referral', row['id'], 'rejected', 999, 'Банк <test> & номер')
        notice = await self.sql.fetchval("SELECT body FROM referral_outbox WHERE event_key=$1", f'resolved:referral:{row["id"]}')
        self.assertIn('Банк &lt;test&gt; &amp; номер', notice)
        self.assertIn('деньги снова на балансе', notice)
        assert_telegram_copy(self, notice)
        await store.resolve_request('referral', row['id'], 'rejected', 999, 'Повтор')
        self.assertEqual((await db.get_referral_stats(123))['current_balance'], 2000)
        with self.assertRaises(ValueError):
            await store.resolve_request('referral', row['id'], 'completed', 999)
        second = await self.request('second')
        await store.resolve_request('referral', second['id'], 'completed', 999)
        self.assertEqual((await db.get_referral_stats(123))['current_balance'], 500)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM referral_outbox WHERE event_key LIKE 'resolved:%'"), 2)

    async def test_creation_and_outbox_are_atomic(self):
        with patch.object(store, 'enqueue', new=AsyncMock(side_effect=RuntimeError('injected'))):
            with self.assertRaises(RuntimeError):
                await self.request()
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 0)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawal_meta'), 0)

    async def test_only_owner_history_and_admin_can_resolve(self):
        row = await self.request()
        self.assertEqual(await store.history(456), [])
        self.assertEqual(len(await store.history(123)), 2)
        with self.assertRaises(PermissionError):
            await store.resolve_request('referral', row['id'], 'completed', 123)
        rows = await store.list_requests()
        self.assertEqual(rows[0]['user_id'], 123)
        self.assertEqual(rows[0]['notification_attempts'], 0)
        self.assertEqual(len(await store.list_requests('all')), 1)

    async def test_partner_reservation_uses_pending_too(self):
        await self.sql.execute('INSERT INTO partnerships(tg_id,percentage,agreement_accepted) VALUES(123,20,TRUE)')
        await self.sql.execute("INSERT INTO partner_earnings(partner_id,user_id,tariff_code,amount,partner_share) VALUES(123,456,'regular_1m',10000,2000)")
        row = await store.create_request('partner',123,'1500','usdt','partner-one',address='T'+'1'*33)
        self.assertEqual((await db.get_partner_stats(123))['current_balance'], 500)
        await store.resolve_request('partner',row['id'],'rejected',999,'Ошибка адреса')
        self.assertEqual((await db.get_partner_stats(123))['current_balance'], 2000)

    async def test_notification_failure_is_retried_not_lost(self):
        await self.request()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('fake network error')))
        await store.deliver_once(bot)
        row = await self.sql.fetchrow('SELECT * FROM referral_outbox')
        self.assertEqual(row['attempts'], 1)
        self.assertIsNone(row['delivered_at'])
        self.assertIsNone(row['lease_until'])
        self.assertEqual(row['last_error'], 'RuntimeError')
        await store.deliver_once(bot)
        self.assertEqual(bot.send_message.await_count, 1)
        await self.sql.execute("UPDATE referral_outbox SET next_attempt_at=now()-interval '1 second'")
        bot.send_message.side_effect = None
        await store.deliver_once(bot)
        self.assertIsNotNone(await self.sql.fetchval('SELECT delivered_at FROM referral_outbox'))
        await store.deliver_once(bot)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_earning_and_notification_transaction(self):
        await db.add_referral_earning(123,456,'regular_1m',300,is_first_purchase=True)
        row = await self.sql.fetchrow('SELECT * FROM referral_earnings ORDER BY id DESC LIMIT 1')
        self.assertEqual(row['referral_share'], Decimal('105'))
        self.assertTrue(await self.sql.fetchval('SELECT first_payment FROM users WHERE tg_id=456'))
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_outbox'), 1)
        notice = await self.sql.fetchval('SELECT body FROM referral_outbox')
        self.assertIn('+105.00 ₽', notice)
        self.assertIn('35% с первой', notice)
        assert_telegram_copy(self, notice, 300)
        with patch.object(store, 'enqueue', new=AsyncMock(side_effect=RuntimeError('injected'))):
            with self.assertRaises(RuntimeError):
                await db.add_referral_earning(123,456,'regular_1m',300)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings'), 2)


if __name__ == '__main__':
    unittest.main()
