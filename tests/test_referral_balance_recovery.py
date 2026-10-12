"""Wallet recovery checks use mocks or isolated SQL only, never live providers."""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

with patch('dotenv.load_dotenv', return_value=False), patch.dict(os.environ, {'DATABASE_URL': '', 'ADMIN_ID': '999'}):
    from services import referral_balance_payments as recovery


def row(number):
    return {'id': number, 'invoice_id': f'wallet-{number}', 'tg_id': 100 + number}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_only_common_reconciliation_owner_and_rotating_keyset(self):
        bot = object()
        with (patch.object(recovery.accounting, 'pending_balance_purchases', new=AsyncMock(return_value=[row(31), row(32)])) as pending,
              patch.object(recovery, 'reconcile_payment', new=AsyncMock()) as reconcile):
            self.assertEqual(await recovery.recover_once(bot, after_id=30), 32)
        pending.assert_awaited_once_with(30, after_id=30)
        self.assertEqual(reconcile.await_args_list[0].args, (bot, 'wallet-31'))
        self.assertEqual(reconcile.await_args_list[0].kwargs, {'expected_tg_id': 131})
        self.assertEqual(reconcile.await_args_list[1].kwargs, {'expected_tg_id': 132})

    async def test_tail_wraps_once_and_no_holds_returns_zero(self):
        with (patch.object(recovery.accounting, 'pending_balance_purchases', new=AsyncMock(side_effect=[[], [row(1)]])) as pending,
              patch.object(recovery, 'reconcile_payment', new=AsyncMock())):
            self.assertEqual(await recovery.recover_once(None, after_id=99), 1)
        self.assertEqual([call.kwargs['after_id'] for call in pending.await_args_list], [99, 0])
        with patch.object(recovery.accounting, 'pending_balance_purchases', new=AsyncMock(return_value=[])) as pending:
            self.assertEqual(await recovery.recover_once(None), 0)
        self.assertEqual(pending.await_count, 1)

    async def test_bad_old_hold_does_not_starve_later_and_logs_no_raw_error(self):
        with (patch.object(recovery.accounting, 'pending_balance_purchases', new=AsyncMock(return_value=[row(1), row(2)])),
              patch.object(recovery, 'reconcile_payment', new=AsyncMock(side_effect=[RuntimeError('sensitive-secret'), None])) as reconcile,
              self.assertLogs(recovery.logger, level='WARNING') as logs):
            self.assertEqual(await recovery.recover_once(None), 2)
        self.assertEqual(reconcile.await_count, 2)
        self.assertIn('RuntimeError', logs.output[0])
        self.assertNotIn('sensitive-secret', '\n'.join(logs.output))
        self.assertNotIn('wallet-1', '\n'.join(logs.output))

    async def test_batch_size_is_bounded(self):
        with patch.object(recovery.accounting, 'pending_balance_purchases', new=AsyncMock(return_value=[])) as pending:
            await recovery.recover_once(None, limit=100000)
        pending.assert_awaited_once_with(30, after_id=0)

    async def test_cancellation_propagates_without_attempting_later_holds(self):
        with (patch.object(recovery.accounting, 'pending_balance_purchases', new=AsyncMock(return_value=[row(1), row(2)])),
              patch.object(recovery, 'reconcile_payment', new=AsyncMock(side_effect=asyncio.CancelledError())) as reconcile):
            with self.assertRaises(asyncio.CancelledError):
                await recovery.recover_once(None)
        self.assertEqual(reconcile.await_count, 1)

    async def test_loop_keeps_cursor_and_sleeps_after_scan_failure(self):
        with (patch.object(recovery, 'recover_once', new=AsyncMock(side_effect=[30, RuntimeError('secret')])) as once,
              patch.object(recovery.asyncio, 'sleep', new=AsyncMock(side_effect=[None, asyncio.CancelledError()])) as sleep,
              self.assertLogs(recovery.logger, level='WARNING') as logs):
            with self.assertRaises(asyncio.CancelledError):
                await recovery.run_recovery_loop(None)
        self.assertEqual([call.kwargs['after_id'] for call in once.await_args_list], [0, 30])
        self.assertTrue(all(call.args == (60.0,) for call in sleep.await_args_list))
        self.assertNotIn('secret', '\n'.join(logs.output))

    async def test_loop_rejects_unsafe_poll_intervals(self):
        for interval in (0, 10, 29, 61, float('nan'), float('inf')):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                await recovery.run_recovery_loop(None, interval=interval)


@unittest.skipUnless(os.environ.get('PGLITE_MODULE'), 'Optional isolated PostgreSQL/WASM environment')
class SQLRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotating_page_reaches_later_holds_and_excludes_cancelled_or_paid(self):
        import database as db
        from services import referral_accounting as accounting, referral_store as store
        from test_payment_recovery_postgres import Bridge
        sql = await Bridge().start()
        self.addAsyncCleanup(sql.close)
        self.enterContext(patch.object(db, '_pool', sql))
        for schema in (store.SCHEMA, accounting.SCHEMA):
            for statement in schema.split(';'):
                if statement.strip():
                    await sql.execute(statement)
        await sql.execute('INSERT INTO users(tg_id) VALUES(123),(456)')
        await sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(123,456,'regular_1m',100000,10000)")
        holds = []
        for number in range(35):
            holds.append(await accounting.reserve_balance_purchase(123, f'fair-{number}', 'regular_1m', 200))
        await accounting.cancel_balance_purchase(123, holds[0]['invoice_id'])
        paid = await sql.fetchrow('SELECT * FROM payments WHERE invoice_id=$1', holds[1]['invoice_id'])
        async with sql.transaction():
            await accounting.settle_subscription(sql, paid)
            await sql.execute("UPDATE payments SET status='paid' WHERE id=$1", paid['id'])
        seen = []

        async def deferred(bot, invoice, *, expected_tg_id):
            seen.append(invoice)
            if invoice == holds[2]['invoice_id']:
                raise RuntimeError('deferred old hold')

        with patch.object(recovery, 'reconcile_payment', new=deferred):
            cursor = await recovery.recover_once(None)
            cursor = await recovery.recover_once(None, after_id=cursor)
            self.assertEqual(len(seen), 33)
            self.assertNotIn(holds[0]['invoice_id'], seen)
            self.assertNotIn(holds[1]['invoice_id'], seen)
            self.assertIn(holds[34]['invoice_id'], seen)
            wrapped = await recovery.recover_once(None, after_id=cursor)
        self.assertLess(wrapped, cursor)
        self.assertEqual(await sql.fetchval("SELECT COUNT(*) FROM referral_wallet_reservations WHERE status='held'"), 33)


if __name__ == '__main__':
    unittest.main()
