"""Isolated accounting checks: never load .env or connect to a live database.

Optional SQL coverage: set PGLITE_MODULE to an already-installed local module.
The bridge uses only ephemeral in-memory PostgreSQL/WASM, never DATABASE_URL.
"""
from decimal import Decimal
from datetime import timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

with patch('dotenv.load_dotenv', return_value=False), patch.dict(os.environ, {'DATABASE_URL': '', 'ADMIN_ID': '999'}):
    import database as db
    from services import referral_accounting as accounting, referral_store as store


def payment(**changes):
    return {'id': 1, 'tg_id': 456, 'invoice_id': 'one', 'tariff_code': 'regular_1m',
            'amount': Decimal('200'), 'provider': 'yookassa', 'status': 'pending',
            'payment_kind': 'subscription', **changes}


def mock_conn():
    conn = MagicMock()
    for name in ('fetch', 'fetchval', 'fetchrow', 'execute'):
        setattr(conn, name, AsyncMock())
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    return conn


def mock_pool(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


class PureAccountingTests(unittest.TestCase):
    def test_half_up_money_and_provider_qualification(self):
        self.assertEqual(accounting.calculate_share('300', True), Decimal('105.00'))
        self.assertEqual(accounting.calculate_share('300'), Decimal('45.00'))
        self.assertEqual(accounting.calculate_share('0.10', True), Decimal('0.04'))
        self.assertEqual(accounting.calculate_share('0.10'), Decimal('0.02'))
        for invalid in ('NaN', 'Infinity', '-1', '0', '1.001', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                accounting.money(invalid)
        self.assertTrue(accounting.qualified_payment(payment()))
        self.assertTrue(accounting.qualified_payment(payment(provider='cryptobot')))
        for changes in ({'provider': 'referral_balance'}, {'provider': 'manual'},
                        {'payment_kind': 'traffic_package'}, {'payment_kind': 'device_addon'},
                        {'amount': Decimal('0')}):
            with self.subTest(changes=changes):
                self.assertFalse(accounting.qualified_payment(payment(**changes)))


class MockAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_commission_invoice_link_and_outbox_share_caller_transaction(self):
        conn = mock_conn()
        payer = {'tg_id': 456, 'referrer_id': 123, 'first_payment': True}
        conn.fetchrow.side_effect = [None, payer, {'outcome': 'credited', 'earning_id': 17}]
        conn.fetch.return_value = [{'tg_id': 123, 'referrer_id': None}, payer]
        conn.fetchval.side_effect = [False, 17]
        with patch.object(store, 'enqueue', new=AsyncMock()) as enqueue:
            result = await accounting.settle_subscription(conn, payment(amount=Decimal('0.10')))
        self.assertEqual(result['earning_id'], 17)
        earning = conn.fetchval.await_args_list[1]
        self.assertIn('payment_id', earning.args[0])
        self.assertEqual(earning.args[5:], (Decimal('0.04'), True, 1))
        self.assertIn("status='paid'", conn.fetchval.await_args_list[0].args[0])
        self.assertIn("provider IN ('cryptobot','yookassa')", conn.fetchval.await_args_list[0].args[0])
        self.assertEqual(conn.fetch.await_args.args[1], [123, 456])
        conn.transaction.assert_not_called()
        enqueue.assert_awaited_once()
        self.assertEqual(enqueue.await_args.args[1], 'earning:17')

    async def test_no_referrer_outcome_durable_and_retry_never_retro_rewards(self):
        conn = mock_conn()
        payer = {'tg_id': 456, 'referrer_id': None}
        conn.fetchrow.side_effect = [None, payer, {'outcome': 'no_referrer', 'is_first_purchase': True}]
        conn.fetch.return_value = [payer]
        conn.fetchval.return_value = False
        result = await accounting.settle_subscription(conn, payment())
        self.assertEqual(result['outcome'], 'no_referrer')
        repeat = mock_conn()
        repeat.fetchrow.return_value = result
        self.assertEqual(await accounting.settle_subscription(repeat, payment()), result)
        repeat.fetch.assert_not_awaited()
        repeat.fetchval.assert_not_awaited()
        self.assertEqual(repeat.fetchrow.await_count, 1)

    async def test_paid_without_settlement_is_excluded_never_backcredited(self):
        conn = mock_conn()
        conn.fetchrow.side_effect = [None, {'outcome': 'legacy_excluded'}]
        result = await accounting.settle_subscription(conn, payment(status='paid'))
        self.assertEqual(result['outcome'], 'legacy_excluded')
        conn.fetchval.assert_not_awaited()
        conn.fetch.assert_not_awaited()

    async def test_migration_marks_existing_paid_once_but_not_pending(self):
        conn = mock_conn()
        conn.fetchval.side_effect = [accounting.CUTOVER_KEY, None]
        await accounting.initialize(conn)
        await accounting.initialize(conn)
        marks = [call.args[0] for call in conn.execute.await_args_list
                 if 'SELECT id,invoice_id,tg_id,amount' in call.args[0]]
        self.assertEqual(len(marks), 1)
        self.assertIn("WHERE status='paid'", marks[0])
        self.assertNotIn('created_at', marks[0])

    async def test_negative_balance_is_not_clamped_and_holds_reduce_available(self):
        conn = mock_conn()
        conn.fetchrow.return_value = {'earned': Decimal('100'), 'reversed': Decimal('30'),
                                     'withdrawn': Decimal('90'), 'held': Decimal('20')}
        self.assertEqual(await accounting.balance(conn, 123), Decimal('-40'))

    async def test_admin_validation_before_database_access(self):
        with patch.object(store, 'ADMIN_ID', 0), patch.object(db, 'get_pool', new=AsyncMock()) as get_pool:
            with self.assertRaisesRegex(ValueError, 'ADMIN_ID'):
                await store.initialize()
            get_pool.assert_not_awaited()
        with patch.object(accounting, 'ADMIN_ID', 999), patch.object(db, 'get_pool', new=AsyncMock()) as get_pool:
            with self.assertRaises(PermissionError):
                await accounting.confirm_refund(123, 'one', 200, 'actual-transfer')
            with self.assertRaises(ValueError):
                await accounting.confirm_refund(999, 'one', 0, 'actual-transfer')
            get_pool.assert_not_awaited()

    async def test_wallet_duplicate_cannot_change_amount_or_owner(self):
        conn = mock_conn()
        conn.fetchrow.side_effect = [{'tg_id': 123}, {
            'user_id': 123, 'tariff_code': 'regular_1m', 'amount': Decimal('200'),
            'purchase_mode': 'new', 'subscription_id': None, 'target_slot_number': 1}]
        with patch.object(db, 'get_pool', new=AsyncMock(return_value=mock_pool(conn))):
            with self.assertRaisesRegex(ValueError, 'другой оплаты'):
                await accounting.reserve_balance_purchase(123, 'same', 'regular_1m', '300', target_slot_number=1)
        conn.execute.assert_not_awaited()

    async def test_optional_views_never_initialize_database_and_fail_soft(self):
        with patch.object(db, '_pool', None), patch.object(db, 'get_pool', new=AsyncMock()) as get_pool:
            self.assertFalse(await accounting.record_view(123, 'telegram'))
            get_pool.assert_not_awaited()
        with patch.object(db, '_pool', object()), patch.object(db, 'get_pool', new=AsyncMock(side_effect=RuntimeError('injected'))):
            self.assertFalse(await accounting.record_view(123, 'miniapp'))

    async def test_repeat_is_15_percent_and_invalid_referrer_never_credit(self):
        conn = mock_conn()
        payer = {'tg_id': 456, 'referrer_id': 123, 'first_payment': False}
        conn.fetchrow.side_effect = [None, payer, {'outcome': 'credited'}]
        conn.fetch.return_value = [{'tg_id': 123}, payer]
        conn.fetchval.side_effect = [True, 17]
        with patch.object(store, 'enqueue', new=AsyncMock()):
            await accounting.settle_subscription(conn, payment())
        self.assertEqual(conn.fetchval.await_args_list[1].args[5], Decimal('30.00'))
        self.assertFalse(conn.fetchval.await_args_list[1].args[6])
        for referrer in (456, 987):
            bad = mock_conn()
            payer = {'tg_id': 456, 'referrer_id': referrer}
            bad.fetchrow.side_effect = [None, payer, {'outcome': 'invalid_referrer'}]
            bad.fetch.return_value = [payer]
            bad.fetchval.return_value = False
            with patch.object(store, 'enqueue', new=AsyncMock()) as enqueue:
                result = await accounting.settle_subscription(bad, payment())
            self.assertEqual(result['outcome'], 'invalid_referrer')
            self.assertEqual(bad.fetchval.await_count, 1)
            enqueue.assert_not_awaited()

    async def test_refund_cap_checked_before_audited_insert(self):
        conn = mock_conn()
        conn.fetchrow.side_effect = [payment(status='paid'), None, None]
        conn.fetch.return_value = [{'tg_id': 456}]
        conn.fetchval.return_value = Decimal('160')
        with patch.object(accounting, 'ADMIN_ID', 999), patch.object(db, 'get_pool', new=AsyncMock(return_value=mock_pool(conn))):
            with self.assertRaisesRegex(ValueError, 'превышает оплату'):
                await accounting.confirm_refund(999, 'one', 50, 'actual-return', 'Manually sent')
        self.assertEqual(conn.fetchrow.await_count, 3)
        self.assertFalse(any('INSERT INTO' in call.args[0] for call in conn.execute.await_args_list))

    async def test_partial_refund_uses_cumulative_share_target_and_negative_debt_notice(self):
        conn = mock_conn()
        earning = {'id': 17, 'referrer_id': 123, 'referral_share': Decimal('0.04')}
        refund = {'id': 7, 'payment_id': 1, 'amount': Decimal('0.03')}
        conn.fetchrow.side_effect = [payment(status='paid', amount=Decimal('0.10')), None, earning, refund]
        conn.fetch.return_value = [{'tg_id': 123}, {'tg_id': 456}]
        conn.fetchval.side_effect = [Decimal('0.03'), Decimal('0.01')]
        with (patch.object(accounting, 'ADMIN_ID', 999),
              patch.object(db, 'get_pool', new=AsyncMock(return_value=mock_pool(conn))),
              patch.object(accounting, 'balance', new=AsyncMock(return_value=Decimal('-1'))),
              patch.object(accounting, '_refund_result', new=AsyncMock(return_value=refund)),
              patch.object(store, 'enqueue', new=AsyncMock()) as enqueue):
            await accounting.confirm_refund(999, 'one', '0.03', 'actual-return', 'Manually sent')
        reversal = next(call for call in conn.execute.await_args_list if 'INSERT INTO referral_earning_reversals' in call.args[0])
        self.assertEqual(reversal.args[-1], Decimal('0.01'))
        self.assertIn('отрицательный остаток', enqueue.await_args.args[3])
        self.assertFalse(any('UPDATE referral_withdrawals' in call.args[0] for call in conn.execute.await_args_list))

    async def test_cash_completion_rechecks_debt_under_user_then_withdrawal_lock(self):
        conn = mock_conn()
        row = {'id': 1, 'referrer_id': 123, 'amount': Decimal('1500'), 'status': 'pending'}
        conn.fetchrow.side_effect = [row, {'tg_id': 123}, row]
        with (patch.object(store, 'ADMIN_ID', 999),
              patch.object(db, 'get_pool', new=AsyncMock(return_value=mock_pool(conn))),
              patch.object(accounting, 'balance', new=AsyncMock(return_value=Decimal('-0.01')))):
            with self.assertRaisesRegex(ValueError, 'заблокирована'):
                await store.resolve_request('referral', 1, 'completed', 999)
        self.assertIn('users', conn.fetchrow.await_args_list[1].args[0])
        self.assertIn('FOR UPDATE', conn.fetchrow.await_args_list[1].args[0])
        self.assertIn('referral_withdrawals', conn.fetchrow.await_args_list[2].args[0])
        conn.execute.assert_not_awaited()

    async def test_wallet_hold_cannot_be_cancelled_after_activation_started(self):
        conn = mock_conn()
        conn.fetchrow.side_effect = [payment(provider='referral_balance'), {'status': 'held'}]
        conn.fetch.return_value = [{'tg_id': 456}]
        conn.fetchval.return_value = 'one'
        with patch.object(db, 'get_pool', new=AsyncMock(return_value=mock_pool(conn))):
            with self.assertRaisesRegex(ValueError, 'Активация уже началась'):
                await accounting.cancel_balance_purchase(456, 'one')
        conn.execute.assert_not_awaited()


class WalletHandlerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_nonce_retry_after_fsm_clear_never_reprices_or_reserves_again(self):
        from handlers import subscription
        from services.payment_reconciliation import ReconciliationResult
        callback = SimpleNamespace(data='pay_referral_balance:saved-key', bot=object(),
                                   from_user=SimpleNamespace(id=456), answer=AsyncMock())
        state = AsyncMock()
        state.get_data.return_value = {}
        with (patch.object(accounting, 'get_balance_purchase', new=AsyncMock(return_value={
                  'status': 'held', 'invoice_id': 'wallet-saved'})) as lookup,
              patch.object(accounting, 'reserve_balance_purchase', new=AsyncMock()) as reserve,
              patch.object(subscription, 'current_price', new=AsyncMock()) as price,
              patch.object(subscription, 'reconcile_payment', new=AsyncMock(return_value=ReconciliationResult('wallet-saved', 'paid'))) as reconcile,
              patch.object(subscription, 'edit_text_with_photo', new=AsyncMock())):
            await subscription.process_pay_referral_balance(callback, state)
        lookup.assert_awaited_once_with(456, 'saved-key')
        reconcile.assert_awaited_once_with(callback.bot, 'wallet-saved', expected_tg_id=456)
        reserve.assert_not_awaited()
        price.assert_not_awaited()
        state.clear.assert_awaited_once()

    async def test_other_or_stale_nonce_and_generic_old_button_cannot_create_order(self):
        from handlers import subscription
        for callback_data, saved_nonce in (('pay_referral_balance:foreign-key', 'own-key'),
                                           ('pay_referral_balance', None)):
            callback = SimpleNamespace(data=callback_data, bot=object(),
                                       from_user=SimpleNamespace(id=456), answer=AsyncMock())
            state = AsyncMock()
            state.get_data.return_value = {'tariff_code': 'regular_1m', 'wallet_amount': '200',
                                          'wallet_request_key': saved_nonce}
            with (patch.object(accounting, 'get_balance_purchase', new=AsyncMock(return_value=None)),
                  patch.object(accounting, 'reserve_balance_purchase', new=AsyncMock()) as reserve,
                  patch.object(db, 'get_referral_stats', new=AsyncMock(return_value={'current_balance': 2000})),
                  patch.object(subscription, 'reconcile_payment', new=AsyncMock()) as reconcile):
                await subscription.process_pay_referral_balance(callback, state)
            reserve.assert_not_awaited()
            reconcile.assert_not_awaited()
            self.assertTrue(callback.answer.await_args.kwargs['show_alert'])


@unittest.skipUnless(os.environ.get('PGLITE_MODULE'), 'Optional isolated in-memory PostgreSQL environment')
class PostgreSQLAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_payment_recovery_postgres import Bridge as BaseBridge

        class Bridge(BaseBridge):
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
        self.enterContext(patch.object(store, 'ADMIN_ID', 999))
        self.enterContext(patch.object(accounting, 'ADMIN_ID', 999))
        await self.sql.execute(store.SCHEMA)
        await self.sql.execute('INSERT INTO users(tg_id,referrer_id) VALUES(123,NULL),(456,123),(789,NULL),(999,NULL)')
        await accounting.initialize(self.sql)

    async def insert_payment(self, invoice='one', **changes):
        values = payment(invoice_id=invoice, **changes)
        return await self.sql.fetchrow('''INSERT INTO payments(tg_id,tariff_code,amount,provider,invoice_id,status,payment_kind)
            VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *''', values['tg_id'], values['tariff_code'],
            values['amount'], values['provider'], invoice, values['status'], values['payment_kind'])

    async def pay(self, row):
        async with self.sql.transaction():
            locked = await self.sql.fetchrow('SELECT * FROM payments WHERE id=$1 FOR UPDATE', row['id'])
            result = await accounting.settle_subscription(self.sql, locked)
            await self.sql.execute("UPDATE payments SET status='paid' WHERE id=$1", row['id'])
            return result

    async def test_first_repeat_ignore_flag_and_addons_wallet(self):
        await self.sql.execute('UPDATE users SET first_payment=TRUE WHERE tg_id=456')
        addon = await self.insert_payment('addon', payment_kind='traffic_package')
        await self.pay(addon)
        await self.pay(await self.insert_payment('one', amount=Decimal('300')))
        await self.pay(await self.insert_payment('two', amount=Decimal('200')))
        rows = await self.sql.fetch('SELECT * FROM referral_earnings ORDER BY id')
        self.assertEqual([row['referral_share'] for row in rows], [Decimal('105'), Decimal('30')])
        self.assertEqual([row['is_first_purchase'] for row in rows], [True, False])
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_outbox'), 2)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_payment_settlements'), 3)

    async def test_atomic_rollback_dedup_and_no_retroactive_referrer(self):
        row = await self.insert_payment()
        with patch.object(store, 'enqueue', new=AsyncMock(side_effect=RuntimeError('injected'))):
            with self.assertRaises(RuntimeError):
                await self.pay(row)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings'), 0)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_payment_settlements'), 0)
        self.assertFalse(await self.sql.fetchval('SELECT first_payment FROM users WHERE tg_id=456'))
        await self.pay(row)
        await self.pay(row)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings'), 1)
        no_ref = await self.insert_payment('no-ref', tg_id=789)
        await self.pay(no_ref)
        await self.sql.execute('UPDATE users SET referrer_id=123 WHERE tg_id=789')
        await self.pay(no_ref)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings'), 1)

    async def test_cutover_preserves_legacy_and_old_pending_can_settle(self):
        await self.sql.execute('DELETE FROM referral_accounting_migrations')
        old_paid = await self.insert_payment('old-paid', status='paid')
        pending = await self.insert_payment('old-pending')
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(123,456,'regular_1m',200,70)")
        await accounting.initialize(self.sql)
        self.assertEqual((await self.pay(old_paid))['outcome'], 'legacy_excluded')
        await self.pay(pending)
        await accounting.initialize(self.sql)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings'), 2)
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('100'))
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings WHERE payment_id IS NULL'), 1)

    async def test_confirmed_partial_refunds_dedup_cap_and_rounding(self):
        await self.pay(await self.insert_payment(amount=Decimal('0.10')))
        first = await accounting.confirm_refund(999, 'one', '0.03', 'return-1', 'Actual transfer')
        repeat = await accounting.confirm_refund(999, 'one', '0.03', 'return-1', 'Actual transfer')
        self.assertEqual(first['id'], repeat['id'])
        await accounting.confirm_refund(999, 'one', '0.03', 'return-2', 'Actual transfer')
        await accounting.confirm_refund(999, 'one', '0.04', 'return-3', 'Actual transfer')
        self.assertEqual(await self.sql.fetchval('SELECT SUM(amount) FROM referral_earning_reversals'), Decimal('0.04'))
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('0'))
        with self.assertRaises(ValueError):
            await accounting.confirm_refund(999, 'one', '0.01', 'return-over', 'Actual transfer')
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_confirmed_refunds'), 3)

    async def test_requested_refund_is_not_reversal_and_legacy_never_guessed(self):
        await self.pay(await self.insert_payment())
        await self.sql.execute("UPDATE payments SET refund_requested_at=now(),refund_status='requested' WHERE invoice_id='one'")
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('70'))
        legacy = await self.insert_payment('legacy', status='paid')
        await self.pay(legacy)
        result = await accounting.confirm_refund(999, 'legacy', '200', 'actual-legacy', 'Returned manually')
        self.assertTrue(result['legacy_review_required'])
        self.assertEqual(result['reversed_amount'], Decimal('0'))
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('70'))
        self.assertTrue(any(row['kind'] == 'unlinked_confirmed_refund'
                            for row in (await accounting.review_report())['items']))

    async def test_wallet_and_withdrawal_share_hold_and_complete_exactly_once(self):
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(123,456,'regular_1m',10000,2000)")
        hold = await accounting.reserve_balance_purchase(123, 'wallet-1', 'regular_1m', 200, target_slot_number=1)
        again = await accounting.reserve_balance_purchase(123, 'wallet-1', 'regular_1m', 200, target_slot_number=1)
        self.assertEqual(hold['invoice_id'], again['invoice_id'])
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('1800'))
        request = await store.create_request('referral', 123, 1500, 'sbp', 'cash-1', bank='Bank', phone='+79991234567')
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('300'))
        with self.assertRaises(ValueError):
            await accounting.reserve_balance_purchase(123, 'wallet-2', 'regular_1m', 400)
        wallet = await self.sql.fetchrow('SELECT * FROM payments WHERE invoice_id=$1', hold['invoice_id'])
        await self.pay(wallet)
        await self.pay(wallet)
        self.assertEqual(await self.sql.fetchval("SELECT COUNT(*) FROM referral_withdrawals WHERE withdrawal_type LIKE 'subscription_%'"), 1)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings WHERE payment_id IS NOT NULL'), 0)
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('300'))
        await store.resolve_request('referral', request['id'], 'rejected', 999, 'Not paid')
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('1800'))

    async def test_refund_freezes_pending_cash_but_new_earnings_offset_debt(self):
        await self.pay(await self.insert_payment(amount=Decimal('5000')))
        withdrawal = await store.create_request('referral', 123, 1500, 'sbp', 'cash', bank='Bank', phone='+79991234567')
        await accounting.confirm_refund(999, 'one', 5000, 'returned', 'Actual return')
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('-1500'))
        with self.assertRaisesRegex(ValueError, 'заблокирована'):
            await store.resolve_request('referral', withdrawal['id'], 'completed', 999)
        self.assertEqual(await self.sql.fetchval('SELECT status FROM referral_withdrawals'), 'pending')
        await self.pay(await self.insert_payment('two', amount=Decimal('10000')))
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('0'))
        await store.resolve_request('referral', withdrawal['id'], 'completed', 999)
        self.assertEqual(await self.sql.fetchval('SELECT status FROM referral_withdrawals'), 'completed')
        history = await store.history(123)
        self.assertTrue(any(row['kind'] == 'reversal' and row['amount'] == 1750 for row in history))

    async def test_wallet_cancel_only_before_activation_and_views_observed(self):
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(123,456,'regular_1m',2000,400)")
        hold = await accounting.reserve_balance_purchase(123, 'cancel', 'regular_1m', 200)
        await accounting.cancel_balance_purchase(123, hold['invoice_id'])
        await accounting.cancel_balance_purchase(123, hold['invoice_id'])
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('400'))
        self.assertTrue(await accounting.record_view(123, 'telegram'))
        self.assertTrue(await accounting.record_view(123, 'miniapp'))
        stats = await accounting.funnel_stats()
        self.assertEqual(stats['views'], 2)
        self.assertEqual(stats['unique_viewers'], 1)
        self.assertEqual(stats['attributed_registrations'], 1)
        self.assertNotIn('shares', stats)

    async def test_common_wallet_activation_paid_commit_failure_recovers_without_double_spend(self):
        from services import payment_processing as processing, payment_reconciliation as reconciliation
        from test_payment_recovery_postgres import LOCAL
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(456,789,'regular_1m',10000,2000)")
        hold = await accounting.reserve_balance_purchase(456, 'wallet-recovery', 'bypass_1m', 300, target_slot_number=1)
        with (patch.object(processing, '_cancel_reactivation_safely', new=AsyncMock()),
              patch.object(processing, 'remnawave_get_or_create_user', new=AsyncMock(return_value=(LOCAL, 'tg_456_bypass_1'))) as remote,
              patch.object(processing, 'remnawave_set_subscription_expiry', new=AsyncMock(return_value=True)) as expiry,
              patch.object(processing, 'remnawave_get_subscription_url', new=AsyncMock(return_value='https://example.test/key')),
              patch.object(processing, 'remnawave_reset_user_traffic', new=AsyncMock(return_value=True))):
            self.sql.fail_paid = True
            self.assertEqual((await reconciliation.reconcile_payment(None, hold['invoice_id'], expected_tg_id=456)).status, 'pending')
            self.assertEqual(await self.sql.fetchval('SELECT status FROM payments WHERE invoice_id=$1', hold['invoice_id']), 'pending')
            self.assertEqual(await self.sql.fetchval('SELECT status FROM referral_wallet_reservations'), 'held')
            self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 0)
            self.assertEqual(await accounting.balance(self.sql, 456), Decimal('1700'))
            reserved_until = await self.sql.fetchval('SELECT expires_at FROM payment_subscription_activations')
            with self.assertRaises(ValueError):
                await accounting.cancel_balance_purchase(456, hold['invoice_id'])
            self.sql.fail_paid = False
            self.assertEqual((await reconciliation.reconcile_payment(None, hold['invoice_id'], expected_tg_id=456)).status, 'paid')
            self.assertEqual((await reconciliation.reconcile_payment(None, hold['invoice_id'], expected_tg_id=456)).status, 'paid')
            self.assertEqual(await self.sql.fetchval('SELECT status FROM referral_wallet_reservations'), 'consumed')
            self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 1)
            self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings WHERE payment_id IS NOT NULL'), 0)
            self.assertEqual(await accounting.balance(self.sql, 456), Decimal('1700'))
            self.assertEqual(await self.sql.fetchval('SELECT subscription_until FROM subscriptions'), reserved_until)
            self.assertEqual(remote.await_count, 2)
            self.assertTrue(all(call.kwargs['extend_if_exists'] is False for call in remote.await_args_list))
            # The JSON/WASM bridge serializes PG timestamps at JS millisecond
            # precision; actual asyncpg retains all stored microseconds.
            self.assertLess(abs(expiry.await_args_list[0].args[-1] - expiry.await_args_list[1].args[-1]), timedelta(milliseconds=1))

    async def test_forged_wallet_provider_and_changed_hold_details_never_provision(self):
        from services import payment_reconciliation as reconciliation
        forged = await self.insert_payment('forged', provider='referral_balance')
        with patch.object(reconciliation, 'process_paid_payment', new=AsyncMock()) as activate:
            result = await reconciliation.reconcile_payment(None, forged['invoice_id'], expected_tg_id=456)
            self.assertEqual(result.reason, 'unfunded_wallet_order')
            activate.assert_not_awaited()
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(456,789,'regular_1m',2000,700)")
        hold = await accounting.reserve_balance_purchase(456, 'trusted', 'regular_1m', 200, target_slot_number=1)
        saved = await self.sql.fetchrow('SELECT * FROM payments WHERE invoice_id=$1', hold['invoice_id'])
        self.assertEqual((await reconciliation._provider_state(saved))[0], 'paid')
        for changes in ({'tg_id': 789}, {'amount': Decimal('201')}, {'tariff_code': 'bypass_1m'},
                        {'payment_kind': 'traffic_package'}, {'payment_target': 'renew'},
                        {'subscription_id': 987654}, {'target_slot_number': 2}):
            with self.subTest(changes=changes):
                self.assertEqual((await reconciliation._provider_state({**saved, **changes}))[0], 'unfunded_wallet_order')
        with patch.object(reconciliation, 'process_paid_payment', new=AsyncMock()) as activate:
            result = await reconciliation.reconcile_payment(None, hold['invoice_id'], expected_tg_id=789)
            self.assertEqual(result.reason, 'owner_mismatch')
            activate.assert_not_awaited()

    async def test_new_wallet_order_retry_authorizes_only_durable_moved_target(self):
        from services import payment_activation as activation, payment_reconciliation as reconciliation
        from config import TARIFFS
        from test_payment_recovery_postgres import LOCAL
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(456,789,'regular_1m',2000,700)")
        existing = await self.sql.fetchrow('''INSERT INTO subscriptions(tg_id,slot_number,type_index,plan_kind,
            generation,is_visible,is_renewable,remnawave_uuid,subscription_until)
            VALUES(456,1,1,'regular','v2',TRUE,TRUE,$1,now()+interval '10 days') RETURNING *''', LOCAL)
        hold = await accounting.reserve_balance_purchase(456, 'relocate', 'regular_1m', 200, target_slot_number=1)
        prepared = await activation.reserve(456, hold['invoice_id'], TARIFFS['regular_1m'])
        self.assertEqual(prepared['type_index'], 2)
        payment_row = await self.sql.fetchrow('SELECT * FROM payments WHERE invoice_id=$1', hold['invoice_id'])
        self.assertEqual((await reconciliation._provider_state(payment_row))[0], 'paid')
        forged = {**payment_row, 'subscription_id': existing['id'], 'target_slot_number': 1}
        self.assertEqual((await reconciliation._provider_state(forged))[0], 'unfunded_wallet_order')
        self.assertEqual((await accounting.get_balance_purchase(456, 'relocate'))['subscription_id'], None)
        with self.assertRaises(ValueError):
            await accounting.cancel_balance_purchase(456, hold['invoice_id'])

    async def test_refund_debt_rejects_new_spend_but_does_not_release_committed_hold(self):
        from services import payment_reconciliation as reconciliation
        await self.sql.execute('UPDATE users SET referrer_id=456 WHERE tg_id=789')
        await self.pay(await self.insert_payment('source', tg_id=789, amount=Decimal('1000')))
        hold = await accounting.reserve_balance_purchase(456, 'committed', 'regular_1m', 200, target_slot_number=1)
        await accounting.confirm_refund(999, 'source', 1000, 'source-return', 'Actually returned')
        self.assertEqual(await accounting.balance(self.sql, 456), Decimal('-200'))
        with self.assertRaises(ValueError):
            await accounting.reserve_balance_purchase(456, 'new-after-debt', 'regular_1m', 200)
        saved = await self.sql.fetchrow('SELECT * FROM payments WHERE invoice_id=$1', hold['invoice_id'])
        self.assertEqual((await reconciliation._provider_state(saved))[0], 'paid')
        await self.pay(saved)
        self.assertEqual(await accounting.balance(self.sql, 456), Decimal('-200'))
        self.assertEqual((await accounting.get_balance_purchase(456, 'committed'))['status'], 'consumed')

    async def test_renewal_checkout_with_storage_slot_different_from_index_can_retry(self):
        from handlers import subscription
        from services import payment_activation as activation, payment_reconciliation as reconciliation
        from config import TARIFFS
        from test_payment_recovery_postgres import LOCAL
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(456,789,'regular_1m',2000,700)")
        existing = await self.sql.fetchrow('''INSERT INTO subscriptions(tg_id,slot_number,type_index,plan_kind,
            generation,is_visible,is_renewable,remnawave_uuid,subscription_until)
            VALUES(456,7,1,'regular','v2',TRUE,TRUE,$1,now()+interval '10 days') RETURNING *''', LOCAL)
        callback = SimpleNamespace(data=f'renew_subscription_{existing["id"]}',
                                   from_user=SimpleNamespace(id=456), answer=AsyncMock())
        state = AsyncMock()
        with patch.object(subscription, '_show_tariff_selection', new=AsyncMock()):
            await subscription.process_subscription_renew(callback, state)
        selection = state.update_data.await_args.kwargs
        hold = await accounting.reserve_balance_purchase(456, 'renew-retry', 'regular_1m', 200,
            purchase_mode=selection['purchase_mode'], subscription_id=selection['target_subscription_id'],
            target_slot_number=selection['target_slot_number'])
        before = await self.sql.fetchrow('SELECT * FROM payments WHERE invoice_id=$1', hold['invoice_id'])
        self.assertEqual((await reconciliation._provider_state(before))[0], 'paid')
        await activation.reserve(456, hold['invoice_id'], TARIFFS['regular_1m'])
        retry = await self.sql.fetchrow('SELECT * FROM payments WHERE invoice_id=$1', hold['invoice_id'])
        self.assertEqual(retry['target_slot_number'], 1)
        self.assertEqual((await reconciliation._provider_state(retry))[0], 'paid')


if __name__ == '__main__':
    unittest.main()
