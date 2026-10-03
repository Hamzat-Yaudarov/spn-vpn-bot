import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from aiogram import Router
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
from services.referral_program import parse_amount, validate_details, share_link, referral_link
from test_payment_recovery_postgres import Bridge


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
        self.assertIn('вознаграждение', payload['text'][0])

    def test_screen_actions_and_honest_progress(self):
        text, keyboard = referral.earning_screen({'current_balance':1400, 'total_earned':2000, 'active_referrals':3}, 'https://t.me/test?start=ref_123')
        self.assertIn('осталось 100.00 ₽', text)
        self.assertIn('1 500 ₽', text)
        self.assertNotIn('5000', text)
        self.assertEqual(keyboard.inline_keyboard[1][0].copy_text.text, 'https://t.me/test?start=ref_123')

    @unittest.skipUnless(TestClient, 'Optional httpx test client is not installed')
    def test_admin_routes_require_authentication(self):
        app = FastAPI()
        app.include_router(admin_web.router)
        client = TestClient(app)
        self.assertEqual(client.get('/admin/api/withdrawals').status_code, 401)
        self.assertEqual(client.post('/admin/api/withdrawals/referral/1', json={'status':'completed','confirm_paid':True}).status_code, 401)


class FlowTests(unittest.IsolatedAsyncioTestCase):
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
        await store.resolve_request('referral', row['id'], 'rejected', 999, 'Неверный банк')
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
        with patch.object(store, 'enqueue', new=AsyncMock(side_effect=RuntimeError('injected'))):
            with self.assertRaises(RuntimeError):
                await db.add_referral_earning(123,456,'regular_1m',300)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings'), 2)


if __name__ == '__main__':
    unittest.main()
