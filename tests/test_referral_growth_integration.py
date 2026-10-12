"""Local integration contracts; optional PostgreSQL/WASM, no live services."""
import os
import asyncio
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import database as db
from handlers import subscription
from services import payment_reconciliation as reconciliation
from services import referral_accounting as accounting, referral_store as store


def callback(key='nonce'):
    return SimpleNamespace(data=f'pay_referral_balance:{key}', from_user=SimpleNamespace(id=123),
                           bot=AsyncMock(), message=AsyncMock(), answer=AsyncMock())


class WalletCheckoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_order_uses_fixed_quote_and_common_activation(self):
        cb, state = callback(), AsyncMock()
        state.get_data.return_value = {'wallet_request_key': 'nonce', 'wallet_amount': '160',
                                      'tariff_code': 'regular_1m', 'purchase_mode': 'new', 'type_index': 2}
        with (patch.object(accounting, 'get_balance_purchase', AsyncMock(return_value=None)),
              patch.object(accounting, 'reserve_balance_purchase', AsyncMock(return_value={'status': 'held', 'invoice_id': 'wallet'})) as reserve,
              patch.object(db, 'get_referral_stats', AsyncMock(return_value={'current_balance': 200})),
              patch.object(subscription, 'current_price', AsyncMock()) as price,
              patch.object(subscription, 'reconcile_payment', AsyncMock(return_value=reconciliation.ReconciliationResult('wallet', 'paid'))) as activate,
              patch.object(subscription, 'edit_text_with_photo', AsyncMock())):
            await subscription.process_pay_referral_balance(cb, state)
        price.assert_not_awaited()
        self.assertEqual(reserve.await_args.args, (123, 'nonce', 'regular_1m', Decimal('160')))
        self.assertEqual(reserve.await_args.kwargs['target_slot_number'], 2)
        activate.assert_awaited_once_with(cb.bot, 'wallet', expected_tg_id=123)
        state.clear.assert_awaited_once()

    async def test_saved_order_resumes_after_fsm_restart_without_new_spend(self):
        for result_status in ('paid', 'pending'):
            cb, state = callback(), AsyncMock()
            state.get_data.return_value = {}
            with (patch.object(accounting, 'get_balance_purchase', AsyncMock(return_value={'status': 'held', 'invoice_id': 'saved'})) as get,
                  patch.object(accounting, 'reserve_balance_purchase', AsyncMock()) as reserve,
                  patch.object(db, 'get_referral_stats', AsyncMock()) as stats,
                  patch.object(subscription, 'reconcile_payment', AsyncMock(return_value=reconciliation.ReconciliationResult('saved', result_status))),
                  patch.object(subscription, 'edit_text_with_photo', AsyncMock()) as edit):
                await subscription.process_pay_referral_balance(cb, state)
            get.assert_awaited_once_with(123, 'nonce')
            reserve.assert_not_awaited()
            stats.assert_not_awaited()
            if result_status == 'pending':
                buttons = [b for row in edit.await_args.args[2].inline_keyboard for b in row]
                self.assertIn('pay_referral_balance:nonce', [b.callback_data for b in buttons])
                self.assertIn('платить ещё раз не нужно', edit.await_args.args[1])

    async def test_wrong_old_nonce_or_unknown_order_never_reserves(self):
        cb, state = callback('other'), AsyncMock()
        state.get_data.return_value = {'wallet_request_key': 'nonce', 'wallet_amount': '160', 'tariff_code': 'regular_1m'}
        with (patch.object(accounting, 'get_balance_purchase', AsyncMock(return_value=None)),
              patch.object(accounting, 'reserve_balance_purchase', AsyncMock()) as reserve):
            await subscription.process_pay_referral_balance(cb, state)
        reserve.assert_not_awaited()
        self.assertIn('устарел', cb.answer.await_args.args[0])

    async def test_debt_popup_and_legacy_generic_checkout_issue_nothing(self):
        for available in (-50, 200):
            cb, state = callback(), AsyncMock()
            cb.data = 'pay_referral_balance'
            state.get_data.return_value = {'wallet_amount': '160', 'tariff_code': 'regular_1m'}
            with (patch.object(db, 'get_referral_stats', AsyncMock(return_value={'current_balance': available})),
                  patch.object(accounting, 'reserve_balance_purchase', AsyncMock()) as reserve):
                await subscription.process_pay_referral_balance(cb, state)
            reserve.assert_not_awaited()
            self.assertIn('Не хватает' if available < 0 else 'устарел', cb.answer.await_args.args[0])

    async def test_failure_keeps_durable_retry_button(self):
        cb, state = callback(), AsyncMock()
        state.get_data.return_value = {}
        with (patch.object(accounting, 'get_balance_purchase', AsyncMock(return_value={'status': 'held', 'invoice_id': 'saved'})),
              patch.object(subscription, 'reconcile_payment', AsyncMock(side_effect=RuntimeError('injected'))),
              patch.object(subscription, 'edit_text_with_photo', AsyncMock()) as edit):
            await subscription.process_pay_referral_balance(cb, state)
        self.assertIn('повторит активацию автоматически', edit.await_args.args[1])
        buttons = [b for row in edit.await_args.args[2].inline_keyboard for b in row]
        self.assertIn('pay_referral_balance:nonce', [b.callback_data for b in buttons])


@unittest.skipUnless(os.environ.get('PGLITE_MODULE'), 'Optional isolated in-memory PostgreSQL')
class IntegratedSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_payment_recovery_postgres import Bridge as BaseBridge

        class Bridge(BaseBridge):
            def transaction(self, **kwargs):
                return super().transaction(**kwargs)

            async def execute(self, sql, *params):
                if sql in (accounting.SCHEMA, store.SCHEMA):
                    for statement in sql.split(';'):
                        if statement.strip():
                            await super().execute(statement)
                else:
                    await super().execute(sql, *params)

        self.sql = await Bridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.enterContext(patch.object(db, '_pool', self.sql))
        await self.sql.execute(store.SCHEMA)
        await accounting.initialize(self.sql)
        await self.sql.execute('INSERT INTO users(tg_id) VALUES(123),(456),(789)')

    async def test_atomic_first_registration_and_saved_entry_intent(self):
        self.assertTrue(await db.create_user(1001, 'new', 123))
        self.assertFalse(await db.create_user(1001, 'changed', 456))
        self.assertEqual(await self.sql.fetchval('SELECT referrer_id FROM users WHERE tg_id=1001'), 123)
        for tgid, inviter in ((1002, 1002), (1003, 999999), (1004, 9223372036854775808)):
            self.assertTrue(await db.create_user(tgid, 'new', inviter))
            self.assertIsNone(await self.sql.fetchval('SELECT referrer_id FROM users WHERE tg_id=$1', tgid))
        await db.create_user(1005, 'existing')
        await db.create_user(1005, 'changed', 123)
        self.assertIsNone(await self.sql.fetchval('SELECT referrer_id FROM users WHERE tg_id=1005'))
        await db.set_referral_entry_intent(1001, 'earn_withdraw')
        self.assertEqual(await db.get_referral_entry_intent(1001), 'earn_withdraw')
        self.assertEqual(await db.pop_referral_entry_intent(1001), 'earn_withdraw')
        self.assertIsNone(await db.pop_referral_entry_intent(1001))
        with self.assertRaises(ValueError):
            await db.set_referral_entry_intent(1001, 'arbitrary')

    async def test_stats_include_actual_registrations_cash_buyers_holds_and_reversals(self):
        await self.sql.execute('UPDATE users SET referrer_id=123 WHERE tg_id IN(456,789)')
        await self.sql.execute("INSERT INTO payments(tg_id,tariff_code,amount,provider,invoice_id,status) VALUES(456,'regular_1m',160,'yookassa','cash','paid')")
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share,is_first_purchase) VALUES(123,456,'regular_1m',2000,700,TRUE)")
        await accounting.reserve_balance_purchase(123, 'one', 'regular_1m', 160, target_slot_number=1)
        stats = await db.get_referral_stats(123)
        self.assertEqual(stats['registered_referrals'], 2)
        self.assertEqual(stats['active_referrals'], 1)
        self.assertEqual(stats['total_earned'], 700)
        self.assertEqual(stats['held_wallet_amount'], 160)
        self.assertEqual(stats['current_balance'], 540)
        history = await store.history(123)
        for row in history:
            self.assertNotIn('invoice_id', row)
            self.assertNotIn('referred_user_id', row)
            self.assertNotIn('username', row)

    async def test_wallet_verification_survives_selected_target_but_refuses_unfunded(self):
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share,is_first_purchase) VALUES(123,456,'regular_1m',2000,700,TRUE)")
        hold = await accounting.reserve_balance_purchase(123, 'trusted', 'regular_1m', 160)
        invoice = hold['invoice_id']
        payment = await db.get_payment_by_invoice(invoice)
        self.assertEqual(await reconciliation._provider_state(payment), ('paid', Decimal('160')))
        from config import TARIFFS
        from services import payment_activation
        sub = await payment_activation.reserve(123, invoice, TARIFFS['regular_1m'])
        changed = await db.get_payment_by_invoice(invoice)
        self.assertEqual(changed['subscription_id'], sub['id'])
        self.assertEqual(await reconciliation._provider_state(changed), ('paid', Decimal('160')))
        changed['subscription_id'] = 999
        self.assertEqual((await reconciliation._provider_state(changed))[0], 'unfunded_wallet_order')
        payment['invoice_id'] = 'not-funded'
        self.assertEqual((await reconciliation._provider_state(payment))[0], 'unfunded_wallet_order')

    async def test_simultaneous_checkers_issue_one_wallet_subscription_and_one_debit(self):
        from services import payment_processing as processing
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share,is_first_purchase) VALUES(123,456,'regular_1m',2000,700,TRUE)")
        hold = await accounting.reserve_balance_purchase(123, 'racing-click', 'regular_1m', 160)
        invoice, bot = hold['invoice_id'], AsyncMock()
        started, release = asyncio.Event(), asyncio.Event()

        async def remote(*args, **kwargs):
            started.set()
            await release.wait()
            return ('f30a18ee-68d3-4919-8b78-8615391bc94b', 'test')

        with (patch.object(processing, 'remnawave_get_or_create_user', AsyncMock(side_effect=remote)) as issue,
              patch.object(processing, 'remnawave_set_subscription_expiry', AsyncMock(return_value=True)),
              patch.object(processing, 'remnawave_get_subscription_url', AsyncMock(return_value='https://example.test/key')),
              patch.object(processing, '_cancel_reactivation_safely', AsyncMock())):
            automatic = asyncio.create_task(reconciliation.reconcile_payment(bot, invoice, expected_tg_id=123))
            try:
                await asyncio.wait_for(started.wait(), 10)
                manual = await reconciliation.reconcile_payment(bot, invoice, expected_tg_id=123)
                self.assertEqual(manual.reason, 'activation_pending')
            finally:
                release.set()
            self.assertEqual((await automatic).status, 'paid')
            self.assertEqual((await reconciliation.reconcile_payment(bot, invoice, expected_tg_id=123)).status, 'paid')
        issue.assert_awaited_once()
        bot.send_message.assert_awaited_once()
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM subscriptions WHERE is_active'), 1)
        self.assertEqual(await self.sql.fetchval("SELECT COUNT(*) FROM referral_withdrawals WHERE status='completed'"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT COUNT(*) FROM referral_payment_settlements WHERE outcome='internal_balance'"), 1)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings WHERE payment_id IS NOT NULL'), 0)
        self.assertEqual((await db.get_referral_stats(123))['current_balance'], 540)
