"""Native financial regressions on ephemeral PostgreSQL/WASM, no providers."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

with patch('dotenv.load_dotenv', return_value=False), patch.dict(os.environ, {'DATABASE_URL': '', 'ADMIN_ID': '999'}):
    import database as db
    from services import miniapp_referral as actions, referral_accounting as accounting, referral_store as store
    from services import payment_activation as activation, payment_processing as processing


@unittest.skipUnless(os.environ.get('PGLITE_MODULE'), 'Optional isolated in-memory PostgreSQL/WASM environment')
class NativeFinancialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_payment_recovery_postgres import Bridge
        self.sql = await Bridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.enterContext(patch.object(db, '_pool', self.sql))
        self.enterContext(patch.object(store, 'ADMIN_ID', 999))
        self.enterContext(patch.object(accounting, 'ADMIN_ID', 999))
        for schema in (store.SCHEMA, accounting.SCHEMA):
            for statement in schema.split(';'):
                if statement.strip():
                    await self.sql.execute(statement)
        await self.sql.execute('INSERT INTO users(tg_id,referrer_id) VALUES(123,NULL),(456,123),(999,NULL)')
        await self.sql.execute("INSERT INTO referral_earnings(referrer_id,referred_user_id,tariff_code,amount,referral_share) VALUES(123,456,'regular_1m',100000,20000)")
        self.bot = AsyncMock()
        self.enterContext(patch.object(actions, 'current_price', AsyncMock(return_value={'price': 160})))
        self.enterContext(patch.object(processing, '_cancel_reactivation_safely', AsyncMock()))
        self.remote = self.enterContext(patch.object(processing, 'remnawave_get_or_create_user',
            AsyncMock(return_value=('f30a18ee-68d3-4919-8b78-8615391bc94b', 'tg_123_regular_1'))))
        self.enterContext(patch.object(processing, 'remnawave_set_subscription_expiry', AsyncMock(return_value=True)))
        self.enterContext(patch.object(processing, 'remnawave_get_subscription_url', AsyncMock(return_value='https://offline.test/key')))
        self.enterContext(patch.object(processing, 'remnawave_reset_user_traffic', AsyncMock(return_value=True)))

    def body(self, key=None, **changes):
        return {'request_key': key or str(uuid4()), 'tariff_code': 'regular_1m', 'payment_target': 'new',
                'subscription_id': None, 'expected_amount': '160.00', **changes}

    async def hold(self, key=None, **changes):
        options = {'tariff_code': 'regular_1m', 'amount': '160', 'purchase_mode': 'new',
                   'subscription_id': None, 'validate_selection': True, **changes}
        return await accounting.reserve_balance_purchase(123, key or str(uuid4()), **options)

    async def cash(self, key=None, **changes):
        options = {'amount': '1500', 'method': 'sbp', 'bank': 'Bank', 'phone': '+79991234567',
                   'address': '', **changes}
        return await store.create_request('referral', 123, request_key=key or str(uuid4()), **options)

    async def subscription(self, index, **changes):
        options = {'kind': 'regular', 'generation': 'v2', 'visible': True, 'renewable': True,
                   'owner': 123, **changes}
        slot = await self.sql.fetchval('SELECT COALESCE(MAX(slot_number),0)+10 FROM subscriptions WHERE tg_id=$1', options['owner'])
        return await self.sql.fetchrow('''INSERT INTO subscriptions
            (tg_id,slot_number,type_index,plan_kind,generation,is_visible,is_renewable)
            VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *''', options['owner'], slot, index,
            options['kind'], options['generation'], options['visible'], options['renewable'])

    async def test_cash_retry_normalized_immutable_payload_and_global_owner_collision(self):
        key = str(uuid4())
        first = await self.cash(key, bank=' Bank ', phone='+7 (999) 123-45-67')
        repeat = await self.cash(key, amount='1 500,00')
        self.assertEqual(first['id'], repeat['id'])
        for changed in ({'amount': '1501'}, {'bank': 'Other'}, {'phone': '+79991234568'},
                        {'method': 'usdt', 'address': 'T' + '1' * 33}):
            with self.subTest(changed=changed), self.assertRaises(accounting.RequestConflict):
                await self.cash(key, **changed)
        with self.assertRaises(accounting.RequestConflict):
            await store.create_request('referral', 456, '1500', 'sbp', key, bank='Bank', phone='+79991234567')
        await self.sql.execute('INSERT INTO partnerships(tg_id,percentage,agreement_accepted) VALUES(123,20,TRUE)')
        with self.assertRaises(accounting.RequestConflict):
            await store.create_request('partner', 123, '1500', 'sbp', key, bank='Bank', phone='+79991234567')
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 1)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM partner_withdrawals'), 0)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_outbox'), 1)

    async def test_owned_cash_lookup_note_and_status_survive_bot_admin_decision(self):
        key = str(uuid4())
        row = await self.cash(key)
        self.assertIsNone(await store.get_request(456, key))
        await store.resolve_request('referral', row['id'], 'rejected', 999, 'Проверьте номер')
        lookup = await store.get_request(123, key)
        self.assertEqual(lookup['decision_note'], 'Проверьте номер')
        response = await actions.get_withdrawal(123, key)
        self.assertEqual(response['status'], 'rejected')
        self.assertEqual(response['decision_note'], 'Проверьте номер')
        self.assertNotIn('+79991234567', str(response))
        self.assertEqual((await self.cash(key))['id'], row['id'])
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('20000'))

    async def test_wallet_selection_checks_every_renewal_eligibility_and_type_index(self):
        invalid = [await self.subscription(1, owner=456), await self.subscription(2, generation='v1'),
                   await self.subscription(3, visible=False), await self.subscription(4, renewable=False),
                   await self.subscription(5, kind='bypass'), await self.subscription(0)]
        for sub in invalid:
            with self.subTest(sub=sub['id']), self.assertRaises(ValueError):
                await self.hold(purchase_mode='renew', subscription_id=sub['id'])
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_wallet_reservations'), 0)
        good = await self.subscription(2)
        saved = await self.hold(purchase_mode='renew', subscription_id=good['id'])
        self.assertEqual(saved['target_slot_number'], 2)
        self.assertNotEqual(saved['target_slot_number'], good['slot_number'])

    async def test_new_purchase_capacity_includes_unplaced_holds_but_not_other_kind(self):
        for index in range(1, 5):
            await self.subscription(index)
        hold = await self.hold()
        self.assertEqual(hold['target_slot_number'], 5)
        with self.assertRaisesRegex(ValueError, 'лимит'):
            await self.hold()
        bypass = await self.hold(tariff_code='bypass_1m', amount='300')
        self.assertEqual(bypass['target_slot_number'], 1)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_wallet_reservations'), 2)

    async def test_activated_visible_pending_hold_not_double_counted_or_old_slot_used(self):
        for index in range(1, 4):
            await self.subscription(index)
        held = await self.hold()
        target = await activation.reserve(123, held['invoice_id'], {'kind': 'regular', 'days': 30})
        self.assertEqual(target['type_index'], 4)
        # A trusted activation may select a different slot from the original
        # preference. Capacity must count its current visible target only once.
        await self.sql.execute('UPDATE referral_wallet_reservations SET target_slot_number=1 WHERE id=$1', held['id'])
        fifth = await self.hold()
        self.assertEqual(fifth['target_slot_number'], 5)
        with self.assertRaisesRegex(ValueError, 'лимит'):
            await self.hold()

    async def test_same_wallet_key_preserves_original_target_and_saved_price_after_activation(self):
        key = str(uuid4())
        held = await self.hold(key)
        await activation.reserve(123, held['invoice_id'], {'kind': 'regular', 'days': 30})
        repeated = await self.hold(key)
        self.assertEqual(repeated['invoice_id'], held['invoice_id'])
        self.assertEqual(repeated['subscription_id'], None)
        with self.assertRaises(accounting.RequestConflict):
            await self.hold(key, amount='200')
        with self.assertRaises(accounting.RequestConflict):
            await accounting.reserve_balance_purchase(456, key, 'regular_1m', '160', validate_selection=True)
        self.assertIsNone(await accounting.get_balance_purchase(456, key))
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM payments'), 1)

    async def test_cash_and_wallet_concurrent_requests_cannot_overspend(self):
        await self.sql.execute('UPDATE referral_earnings SET referral_share=1600')
        # The in-memory bridge has one connection. Serialize acquire like a
        # single-connection asyncpg pool while allowing requests to race above
        # it; the actual SQL balance/user/key locks still execute in each txn.
        lock = asyncio.Lock()
        bridge = self.sql

        class Pool:
            def acquire(self):
                class Acquire:
                    async def __aenter__(self):
                        await lock.acquire()
                        return bridge

                    async def __aexit__(self, *args):
                        lock.release()
                return Acquire()

        with patch.object(db, '_pool', Pool()):
            result = await asyncio.gather(self.cash(), self.hold(), return_exceptions=True)
        self.assertEqual(sum(isinstance(row, accounting.InsufficientBalance) for row in result), 1)
        self.assertGreaterEqual(await accounting.balance(self.sql, 123), 0)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals')
            + await self.sql.fetchval('SELECT COUNT(*) FROM referral_wallet_reservations'), 1)

    async def test_cash_outbox_failure_rolls_back_key_and_funds(self):
        key = str(uuid4())
        with patch.object(store, 'enqueue', AsyncMock(side_effect=RuntimeError('offline-injected-failure'))):
            with self.assertRaises(RuntimeError):
                await self.cash(key)
        self.assertIsNone(await store.get_request(123, key))
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('20000'))
        row = await self.cash(key)
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_outbox'), 1)

    async def test_native_activation_and_retries_debit_once_and_create_no_commission(self):
        body = self.body()
        first = await actions.create_purchase(123, body, self.bot)
        self.assertEqual(first['status'], 'paid')
        with patch.object(actions, 'current_price', AsyncMock(side_effect=RuntimeError('do not reprice'))):
            repeat = await actions.create_purchase(123, body, self.bot)
            reloaded = await actions.get_purchase(123, body['request_key'], self.bot)
        self.assertEqual(first['invoice_id'], repeat['invoice_id'])
        self.assertEqual(reloaded['status'], 'paid')
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM subscriptions'), 1)
        self.assertEqual(await self.sql.fetchval("SELECT COUNT(*) FROM referral_withdrawals WHERE withdrawal_type LIKE 'subscription_%'"), 1)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_earnings WHERE payment_id IS NOT NULL'), 0)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM partner_earnings'), 0)
        self.assertEqual(await accounting.balance(self.sql, 123), Decimal('19840'))
        self.remote.assert_awaited_once()

    async def test_native_activation_commit_failure_recovers_same_deadline_and_debit(self):
        body = self.body()
        self.sql.fail_paid = True
        first = await actions.create_purchase(123, body, self.bot)
        self.assertEqual(first['status'], 'pending')
        durable = await self.sql.fetchrow('SELECT * FROM payment_subscription_activations')
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 0)
        self.assertEqual(await self.sql.fetchval("SELECT COUNT(*) FROM referral_wallet_reservations WHERE status='held'"), 1)
        self.sql.fail_paid = False
        done = await actions.get_purchase(123, body['request_key'], self.bot)
        self.assertEqual(done['invoice_id'], first['invoice_id'])
        self.assertEqual(done['status'], 'paid')
        self.assertEqual(await self.sql.fetchval('SELECT subscription_until FROM subscriptions'), durable['expires_at'])
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 1)
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM subscriptions'), 1)

    async def test_native_renewal_adds_one_term_and_stays_owned(self):
        subscription = await self.subscription(1)
        until = datetime.utcnow().replace(microsecond=0) + timedelta(days=20)
        await self.sql.execute('UPDATE subscriptions SET subscription_until=$2 WHERE id=$1', subscription['id'], until)
        body = self.body(payment_target='renew', subscription_id=subscription['id'])
        first = await actions.create_purchase(123, body, self.bot)
        await actions.create_purchase(123, body, self.bot)
        self.assertEqual(first['status'], 'paid')
        self.assertEqual(await self.sql.fetchval('SELECT subscription_until FROM subscriptions WHERE id=$1', subscription['id']), until + timedelta(days=30))
        self.assertEqual(await self.sql.fetchval('SELECT COUNT(*) FROM referral_withdrawals'), 1)
        with self.assertRaises(actions.ActionError) as error:
            await actions.get_purchase(456, body['request_key'], self.bot)
        self.assertEqual(error.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
