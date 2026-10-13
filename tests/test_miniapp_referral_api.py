"""Native cash/wallet API checks: mocks only, no dotenv or live providers."""
from decimal import Decimal
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException

with patch('dotenv.load_dotenv', return_value=False), patch.dict(os.environ, {'DATABASE_URL': '', 'ADMIN_ID': '999'}):
    import miniapp_referral_api as api
    from services import miniapp_referral as actions, referral_accounting as accounting, referral_store as store


def purchase(key=None, **changes):
    return {'request_key': key or str(uuid4()), 'tariff_code': 'regular_1m',
            'payment_target': 'new', 'subscription_id': None, 'expected_amount': '160.00', **changes}


def withdrawal(key=None, **changes):
    return {'request_key': key or str(uuid4()), 'amount': '1500.00', 'method': 'sbp',
            'bank': 'Bank', 'phone': '+7 (999) 123-45-67', 'address': '', **changes}


def order(key, **changes):
    return {'request_key': key, 'user_id': 123, 'invoice_id': 'wallet-one', 'tariff_code': 'regular_1m',
            'amount': Decimal('160'), 'purchase_mode': 'new', 'subscription_id': None,
            'target_slot_number': 1, 'status': 'held', **changes}


def payment(**changes):
    return {'invoice_id': 'wallet-one', 'tg_id': 123, 'provider': 'referral_balance',
            'status': 'pending', 'amount': Decimal('160'), **changes}


def cash(**changes):
    return {'id': 7, 'status': 'pending', 'amount': Decimal('1500'), 'withdrawal_type': 'sbp',
            'bank_name': 'Bank', 'phone_number': '+79991234567', 'usdt_address': None,
            'decision_note': None, **changes}


class NativeReferralAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = object()
        self.auth = AsyncMock(return_value={'id': 123})
        app = FastAPI()
        app.include_router(api.create_router(self.auth, lambda: self.bot))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='https://offline.test'))
        self.lookup = self.enterContext(patch.object(accounting, 'get_balance_purchase', AsyncMock(return_value=None)))
        self.reserve = self.enterContext(patch.object(accounting, 'reserve_balance_purchase', AsyncMock()))
        self.price = self.enterContext(patch.object(actions, 'current_price', AsyncMock(return_value={'price': 160})))
        self.payment = self.enterContext(patch.object(actions.db, 'get_payment_by_invoice', AsyncMock(return_value=payment())))
        self.reconcile = self.enterContext(patch.object(actions, 'reconcile_payment', AsyncMock(return_value=SimpleNamespace(status='pending'))))
        self.summary = self.enterContext(patch.object(actions, 'build_payment_success_summary', AsyncMock(return_value={'title': 'Готово'})))
        self.create_cash = self.enterContext(patch.object(store, 'create_request', AsyncMock(return_value=cash())))
        self.get_cash = self.enterContext(patch.object(store, 'get_request', AsyncMock(return_value=cash())))

    async def post(self, kind, payload):
        return await self.client.post(f'/miniapp/api/referral/{kind}', json=payload)

    async def test_authentication_precedes_body_and_all_four_operations(self):
        self.auth.side_effect = HTTPException(401, 'Invalid Telegram authorization')
        key = str(uuid4())
        for method, path in (('post', 'purchases'), ('get', f'purchases/{key}'),
                             ('post', 'withdrawals'), ('get', f'withdrawals/{key}')):
            response = await self.client.request(method, f'/miniapp/api/referral/{path}', content='invalid-json')
            self.assertEqual(response.status_code, 401)
        self.reserve.assert_not_awaited()
        self.create_cash.assert_not_awaited()
        self.lookup.assert_not_awaited()

    async def test_discounted_server_price_and_atomic_selection_are_authoritative(self):
        body = purchase()
        self.reserve.return_value = order(body['request_key'])
        response = await self.post('purchases', body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'request_key': body['request_key'], 'invoice_id': 'wallet-one',
            'status': 'pending', 'amount': '160.00', 'summary': None})
        self.reserve.assert_awaited_once_with(123, body['request_key'], 'regular_1m', Decimal('160'),
            purchase_mode='new', subscription_id=None, validate_selection=True)
        self.price.assert_awaited_once_with(200, product_type='subscription', code='regular_1m', plan_kind='regular')
        self.reconcile.assert_awaited_once_with(self.bot, 'wallet-one', expected_tg_id=123)

    async def test_price_changed_before_hold(self):
        response = await self.post('purchases', purchase(expected_amount='200'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'price_changed')
        self.assertEqual(response.json()['detail']['amount'], '160.00')
        self.reserve.assert_not_awaited()
        self.reconcile.assert_not_awaited()

    async def test_saved_order_uses_original_price_without_revalidation_or_repricing(self):
        body = purchase()
        self.lookup.return_value = order(body['request_key'])
        self.price.side_effect = RuntimeError('must not read changed tariff')
        response = await self.post('purchases', body)
        self.assertEqual(response.status_code, 200)
        self.price.assert_not_awaited()
        self.reserve.assert_not_awaited()
        self.reconcile.assert_awaited_once()

    async def test_concurrent_original_order_wins_discount_change_race(self):
        body = purchase(expected_amount='200.00')
        self.lookup.side_effect = [None, order(body['request_key'], amount=Decimal('200'))]
        self.payment.return_value = payment(amount=Decimal('200'))
        response = await self.post('purchases', body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['amount'], '200.00')
        self.reserve.assert_not_awaited()
        self.reconcile.assert_awaited_once()

    async def test_saved_key_cannot_change_any_immutable_client_field(self):
        key = str(uuid4())
        self.lookup.return_value = order(key)
        for changes in ({'expected_amount': '150'}, {'tariff_code': 'bypass_1m'},
                        {'payment_target': 'renew', 'subscription_id': 4}):
            response = await self.post('purchases', purchase(key, **changes))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['detail']['code'], 'request_conflict')
        self.reserve.assert_not_awaited()
        self.price.assert_not_awaited()
        self.reconcile.assert_not_awaited()

    async def test_invalid_purchase_payload_never_prices_or_reserves(self):
        bad = [None, [], purchase(request_key='bad'), purchase(tariff_code=None),
               purchase(payment_target='other'), purchase(subscription_id=True), purchase(subscription_id=5),
               purchase(payment_target='renew'), purchase(payment_target='renew', subscription_id='5'),
               purchase(expected_amount=160), purchase(expected_amount='NaN'), purchase(expected_amount='1.001'),
               purchase(expected_amount='0'), purchase(expected_amount='-1'), purchase(owner_id=456), purchase(target_slot_number=1)]
        for value in bad:
            with self.subTest(value=value):
                response = await self.post('purchases', value)
                self.assertEqual(response.status_code, 400)
        self.price.assert_not_awaited()
        self.reserve.assert_not_awaited()

    async def test_unknown_tariff_rejected(self):
        response = await self.post('purchases', purchase(tariff_code='free_test'))
        self.assertEqual(response.status_code, 400)
        self.reserve.assert_not_awaited()

    async def test_selection_failure_and_insufficient_balance_return_no_success(self):
        self.reserve.side_effect = ValueError('Подписка недоступна для продления.')
        response = await self.post('purchases', purchase(payment_target='renew', subscription_id=4))
        self.assertEqual(response.status_code, 400)
        self.reconcile.assert_not_awaited()
        self.reserve.side_effect = accounting.InsufficientBalance('-10', '160')
        response = await self.post('purchases', purchase())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'insufficient_balance')
        self.assertEqual(response.json()['detail']['available'], '-10.00')
        self.assertEqual(response.json()['detail']['amount'], '160.00')

    async def test_foreign_global_key_is_generic_conflict(self):
        self.reserve.side_effect = accounting.RequestConflict('Номер запроса уже использован для другой оплаты.')
        response = await self.post('purchases', purchase())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'request_conflict')
        self.assertNotIn('wallet-one', response.text)
        self.reconcile.assert_not_awaited()

    async def test_saved_transport_failure_returns_recoverable_pending_and_no_raw_error(self):
        body = purchase()
        self.reserve.return_value = order(body['request_key'])
        self.reconcile.side_effect = RuntimeError('private-token-phone-secret')
        with self.assertLogs(actions.logger, level='WARNING') as logs:
            response = await self.post('purchases', body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'pending')
        self.assertNotIn('private-token', response.text + ''.join(logs.output))
        self.lookup.return_value = order(body['request_key'])
        self.reconcile.side_effect = None
        self.payment.return_value = payment(status='paid')
        result = await self.client.get(f'/miniapp/api/referral/purchases/{body["request_key"]}')
        self.assertEqual(result.json()['status'], 'paid')
        self.assertEqual(result.json()['summary'], {'title': 'Готово'})
        self.assertEqual(self.reserve.await_count, 1)

    async def test_get_owner_not_found_does_not_provision(self):
        key = str(uuid4())
        self.lookup.return_value = None
        self.get_cash.return_value = None
        for kind in ('purchases', 'withdrawals'):
            response = await self.client.get(f'/miniapp/api/referral/{kind}/{key}')
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()['detail']['code'], 'not_found')
        self.lookup.assert_awaited_once_with(123, key)
        self.get_cash.assert_awaited_once_with(123, key)
        self.reconcile.assert_not_awaited()

    async def test_cancelled_order_does_not_reconcile(self):
        key = str(uuid4())
        self.lookup.return_value = order(key, status='cancelled')
        response = await self.client.get(f'/miniapp/api/referral/purchases/{key}')
        self.assertEqual(response.json()['status'], 'cancelled')
        self.reconcile.assert_not_awaited()
        self.summary.assert_not_awaited()

    async def test_bot_not_ready_never_creates_hold_but_get_saved_still_works(self):
        self.bot = None
        response = await self.post('purchases', purchase())
        self.assertEqual(response.status_code, 503)
        self.reserve.assert_not_awaited()
        key = str(uuid4())
        self.lookup.return_value = order(key)
        response = await self.client.get(f'/miniapp/api/referral/purchases/{key}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'pending')
        self.reconcile.assert_not_awaited()

    async def test_withdrawal_uses_normalized_bot_validators_and_masks(self):
        body = withdrawal(amount='1 500,00')
        response = await self.post('withdrawals', body)
        self.assertEqual(response.status_code, 200)
        self.create_cash.assert_awaited_once_with('referral', 123, Decimal('1500'), 'sbp', body['request_key'],
            bank='Bank', phone='+79991234567', address='')
        result = response.json()
        self.assertEqual(result['number'], 'R-7')
        self.assertEqual(result['bank'], 'Bank')
        self.assertNotIn('+79991234567', response.text)
        self.assertTrue(result['phone'].endswith('67'))
        self.assertIsNone(result['address'])
        self.reconcile.assert_not_awaited()

    async def test_usdt_is_manual_ruble_request_with_masked_address(self):
        address = 'T' + '1' * 33
        self.create_cash.return_value = cash(withdrawal_type='usdt', bank_name=None, phone_number=None, usdt_address=address)
        response = await self.post('withdrawals', withdrawal(method='usdt', bank='', phone='', address=address))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['amount'], '1500.00')
        self.assertEqual(response.json()['method'], 'usdt')
        self.assertEqual(response.json()['address'], 'T111…1111')
        self.assertNotIn(address, response.text)

    async def test_invalid_withdrawal_payload_never_reserves(self):
        invalid = [withdrawal(amount='1499.99'), withdrawal(amount='NaN'), withdrawal(amount='Infinity'),
            withdrawal(amount='1500.001'), withdrawal(amount=1500), withdrawal(amount='1000000001'),
            withdrawal(bank=''), withdrawal(phone='79991234567'), withdrawal(phone=[]), withdrawal(bank=None),
            withdrawal(method='card'), withdrawal(method='usdt', address='bad'), withdrawal(request_key='bad'),
            withdrawal(user_id=456), None, []]
        for body in invalid:
            with self.subTest(body=body):
                response = await self.post('withdrawals', body)
                self.assertEqual(response.status_code, 400)
        self.create_cash.assert_not_awaited()

    async def test_owned_status_includes_admin_note_but_not_unmasked_requisites(self):
        self.get_cash.return_value = cash(status='rejected', decision_note='Исправьте реквизиты')
        response = await self.client.get(f'/miniapp/api/referral/withdrawals/{uuid4()}')
        self.assertEqual(response.json()['status'], 'rejected')
        self.assertEqual(response.json()['decision_note'], 'Исправьте реквизиты')
        self.assertNotIn('+79991234567', response.text)
        self.assertNotIn('referrer_id', response.text)

    async def test_store_conflicts_and_insufficient_balance_remain_compatible(self):
        self.create_cash.side_effect = accounting.RequestConflict('Номер запроса уже использован для другой заявки.')
        response = await self.post('withdrawals', withdrawal())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'request_conflict')
        self.create_cash.side_effect = accounting.InsufficientBalance('1400', '1500')
        response = await self.post('withdrawals', withdrawal())
        self.assertEqual(response.json()['detail']['available'], '1400.00')

    async def test_unknown_errors_are_safe_and_malformed_json_is_400(self):
        self.create_cash.side_effect = RuntimeError('dsn-token-phone-secret')
        with self.assertLogs(api.logger, level='WARNING') as logs:
            response = await self.post('withdrawals', withdrawal())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('dsn-token', response.text + ''.join(logs.output))
        response = await self.client.post('/miniapp/api/referral/withdrawals', content='invalid-json')
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
