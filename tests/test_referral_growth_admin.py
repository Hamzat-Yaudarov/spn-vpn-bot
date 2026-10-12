"""Referral admin checks using mocks only; never load .env or connect to a database."""
import os
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# The application config normally loads .env at import time. These tests don't.
with patch('dotenv.load_dotenv', return_value=False):
    import admin_web
    import database as db
    from handlers import admin
    from services import referral_accounting as accounting
    from services import referral_store as store

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError


def body(**changes):
    return admin_web.RefundReviewBody.model_validate({
        'invoice_id': 'invoice-test', 'amount': '56.00',
        'reference': 'bank-transfer-test', 'note': 'Возврат по обращению клиента', **changes,
    })


def preview(**changes):
    return {'invoice_id': 'invoice-test', 'payment_id': 4, 'buyer_id': 7,
            'actual_paid_rub': Decimal('160.00'), 'refunded_rub': Decimal('20.00'),
            'remaining_rub': Decimal('140.00'), 'linked': True, 'settled': True,
            'manual_review_required': False, **changes}


class AdminValidationTests(unittest.TestCase):
    def test_currency_validation_is_positive_finite_and_cent_precise(self):
        self.assertEqual(body(amount='0.01').amount, Decimal('0.01'))
        for value in ('0', '-0.01', 'NaN', 'sNaN', 'Infinity', '0.001', '1e1000', '1000000000.01'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                body(amount=value)

    def test_invoice_reference_and_reason_are_required(self):
        for field in ('invoice_id', 'reference', 'note'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                body(**{field: '   '})
        for field in ('reference', 'note'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                body(**{field: ' x '})
        with self.assertRaises(ValidationError):
            body(reference='line\ncontrol')
        self.assertEqual(body(invoice_id=' invoice-test ').invoice_id, 'invoice-test')

    def test_confirmation_boolean_is_not_coerced_from_text(self):
        with self.assertRaises(ValidationError):
            admin_web.RefundConfirmBody(**body().model_dump(), review_token='token-test', confirm_transferred='true')

    def test_review_token_accepts_maximum_valid_unicode_text(self):
        review = body(invoice_id='😀' * 160, reference='😀' * 160, note='😀' * 500)
        with patch.object(admin_web, 'BOT_TOKEN', 'local-test-only-secret'):
            token = admin_web._refund_review_token(review, admin_web.ADMIN_ID)
            self.assertGreater(len(token), 4096)
            confirmation = admin_web.RefundConfirmBody(**review.model_dump(), review_token=token, confirm_transferred=True)
            admin_web._check_refund_review(confirmation, admin_web.ADMIN_ID)


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(admin_web.router)
        self.client = TestClient(app)

    def test_every_referral_route_requires_existing_telegram_auth(self):
        for path in ('/referrals/funnel', '/referrals/review'):
            self.assertEqual(self.client.get('/admin/api' + path).status_code, 401)
        data = {'invoice_id': 'invoice-test', 'amount': '56.00', 'reference': 'test-transfer', 'note': 'test reason'}
        self.assertEqual(self.client.post('/admin/api/referrals/refunds/prepare', json=data).status_code, 401)
        data.update(review_token='test-token', confirm_transferred=True)
        self.assertEqual(self.client.post('/admin/api/referrals/refunds/confirm', json=data).status_code, 401)

    def test_nonadmin_is_rejected_before_accounting(self):
        with patch.object(admin_web, 'validate_telegram_init_data', return_value={'id': admin_web.ADMIN_ID + 1}), \
                patch.object(accounting, 'funnel_stats', new_callable=AsyncMock) as funnel:
            result = self.client.get('/admin/api/referrals/funnel', headers={'Authorization': 'tma test-only'})
        self.assertEqual(result.status_code, 403)
        funnel.assert_not_awaited()


class AdminAccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.token_patch = patch.object(admin_web, 'BOT_TOKEN', 'local-unit-test-secret')
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.pool_patch = patch.object(db, 'get_pool', new=AsyncMock(side_effect=AssertionError('No database access in mocked admin tests')))
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)

    def confirmation(self, changes=None, actor=None):
        review = body()
        token = admin_web._refund_review_token(review, admin_web.ADMIN_ID if actor is None else actor)
        return admin_web.RefundConfirmBody(**{**review.model_dump(), 'review_token': token,
                                               'confirm_transferred': True, **(changes or {})})

    async def test_funnel_money_is_serialized_without_losing_cents(self):
        with patch.object(accounting, 'funnel_stats', new=AsyncMock(return_value={'views': 3, 'credited_earnings': Decimal('1234567890.01')})):
            result = await admin_web.admin_referral_funnel(admin_id=admin_web.ADMIN_ID)
        self.assertEqual(result, {'views': 3, 'credited_earnings': '1234567890.01'})

    async def test_review_limit_and_offset_are_bounded_and_read_only(self):
        with patch.object(accounting, 'review_report', new=AsyncMock(return_value={'items': [], 'has_more': False})) as report:
            await admin_web.admin_referral_review(limit=1000, offset=-20, admin_id=admin_web.ADMIN_ID)
        report.assert_awaited_once_with(limit=100, offset=0)

    async def test_payout_list_reports_negative_referral_balance_in_one_query(self):
        rows = [{'program': 'referral', 'status': 'pending', 'current_available_balance': Decimal('-120'), 'payout_blocked': True},
                {'program': 'partner', 'status': 'pending', 'current_available_balance': None, 'payout_blocked': False}]
        conn, pool = MagicMock(), MagicMock()
        conn.fetch = AsyncMock(return_value=rows)
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(store.db, 'get_pool', new=AsyncMock(return_value=pool)):
            result = await store.list_requests('pending', 0, 31)
        self.assertEqual(result, rows)
        conn.fetch.assert_awaited_once()
        query, *parameters = conn.fetch.await_args.args
        self.assertEqual(parameters, ['pending', 31, 0])
        for text in ('SELECT DISTINCT user_id FROM selected', 'referral_earning_reversals',
                     "status IN ('pending','completed')", 'referral_wallet_reservations',
                     "status='held'", 'AS current_available_balance', 'AS payout_blocked'):
            self.assertIn(text, query)
        self.assertIn("w.program='referral' AND w.status='pending'", query)

    async def test_payout_api_preserves_block_information(self):
        rows = [{'program': 'referral', 'status': 'pending', 'current_available_balance': Decimal('-10'), 'payout_blocked': True}]
        with patch.object(store, 'list_requests', new=AsyncMock(return_value=rows)):
            result = await admin_web.admin_withdrawals(admin_id=admin_web.ADMIN_ID)
        self.assertTrue(result['items'][0]['payout_blocked'])
        self.assertEqual(result['items'][0]['current_available_balance'], Decimal('-10'))

    async def test_prepare_only_reads_invoice_and_binds_review(self):
        review = body()
        with patch.object(accounting, 'refund_preview', new=AsyncMock(return_value=preview())) as read, \
                patch.object(accounting, 'confirm_refund', new_callable=AsyncMock) as write:
            result = await admin_web.prepare_referral_refund(review, admin_id=admin_web.ADMIN_ID)
        read.assert_awaited_once_with('invoice-test')
        write.assert_not_awaited()
        self.assertEqual(result['preview']['remaining_rub'], '140.00')
        self.assertEqual(result['expires_in'], 300)
        confirm = admin_web.RefundConfirmBody(**review.model_dump(), review_token=result['review_token'], confirm_transferred=True)
        admin_web._check_refund_review(confirm, admin_web.ADMIN_ID)

    async def test_prepare_rejects_more_than_cumulative_actual_payment_remainder(self):
        with patch.object(accounting, 'refund_preview', new=AsyncMock(return_value=preview(remaining_rub=Decimal('55.99')))), \
                self.assertRaises(HTTPException) as error:
            await admin_web.prepare_referral_refund(body(), admin_id=admin_web.ADMIN_ID)
        self.assertEqual(error.exception.status_code, 409)

    async def test_exact_recorded_transfer_can_be_reviewed_again_after_full_refund(self):
        already = {'reference': 'bank-transfer-test', 'amount': Decimal('56'), 'note': 'Исходная причина'}
        with patch.object(accounting, 'refund_preview', new=AsyncMock(return_value=preview(remaining_rub=Decimal('0'), confirmed_refunds=[already]))):
            result = await admin_web.prepare_referral_refund(body(), admin_id=admin_web.ADMIN_ID)
        self.assertTrue(result['preview']['already_recorded'])
        self.assertEqual(result['preview']['recorded_refund']['note'], 'Исходная причина')

    async def test_recorded_reference_cannot_be_reused_with_a_changed_amount(self):
        already = {'reference': 'bank-transfer-test', 'amount': Decimal('55'), 'note': 'Исходная причина'}
        with patch.object(accounting, 'refund_preview', new=AsyncMock(return_value=preview(confirmed_refunds=[already]))), \
                self.assertRaises(HTTPException) as error:
            await admin_web.prepare_referral_refund(body(), admin_id=admin_web.ADMIN_ID)
        self.assertEqual(error.exception.status_code, 409)

    async def test_prepare_propagates_missing_or_nonexternal_payment_as_conflict(self):
        with patch.object(accounting, 'refund_preview', new=AsyncMock(side_effect=ValueError('Счёт не найден.'))), \
                self.assertRaises(HTTPException) as error:
            await admin_web.prepare_referral_refund(body(), admin_id=admin_web.ADMIN_ID)
        self.assertEqual(error.exception.status_code, 409)

    async def test_confirmation_records_exact_actor_invoice_amount_reference_reason(self):
        request = self.confirmation()
        with patch.object(accounting, 'confirm_refund', new=AsyncMock(return_value={'id': 2, 'amount': Decimal('56.00'), 'legacy_review_required': False})) as confirm:
            result = await admin_web.confirm_referral_refund(request, admin_id=admin_web.ADMIN_ID)
        confirm.assert_awaited_once_with(admin_web.ADMIN_ID, 'invoice-test', Decimal('56.00'), 'bank-transfer-test', 'Возврат по обращению клиента')
        self.assertEqual(result['amount'], '56.00')

    async def test_actual_transfer_confirmation_is_mandatory(self):
        request = self.confirmation({'confirm_transferred': False})
        with patch.object(accounting, 'confirm_refund', new_callable=AsyncMock) as confirm, self.assertRaises(HTTPException) as error:
            await admin_web.confirm_referral_refund(request, admin_id=admin_web.ADMIN_ID)
        self.assertEqual(error.exception.status_code, 400)
        confirm.assert_not_awaited()

    async def test_first_step_cannot_be_skipped_or_reused_for_changed_fields(self):
        for changes in ({'review_token': 'invalid-token'}, {'amount': Decimal('57')},
                        {'invoice_id': 'other-invoice'}, {'reference': 'other-transfer'}, {'note': 'Другая причина'}):
            with self.subTest(changes=changes), patch.object(accounting, 'confirm_refund', new_callable=AsyncMock) as confirm, \
                    self.assertRaises(HTTPException) as error:
                await admin_web.confirm_referral_refund(self.confirmation(changes), admin_id=admin_web.ADMIN_ID)
            self.assertEqual(error.exception.status_code, 409)
            confirm.assert_not_awaited()

    async def test_review_is_bound_to_actor_and_expires(self):
        with patch.object(admin_web.time, 'time', return_value=1000):
            request = self.confirmation()
        for actor, now in ((admin_web.ADMIN_ID + 1, 1001), (admin_web.ADMIN_ID, 1301)):
            with self.subTest(actor=actor, now=now), patch.object(admin_web.time, 'time', return_value=now), \
                    patch.object(accounting, 'confirm_refund', new_callable=AsyncMock) as confirm, self.assertRaises(HTTPException) as error:
                await admin_web.confirm_referral_refund(request, admin_id=actor)
            self.assertEqual(error.exception.status_code, 409)
            confirm.assert_not_awaited()

    async def test_final_recording_rechecks_cap_and_forbids_nonadmin(self):
        for exception, status in ((ValueError('Лимит возврата изменился.'), 409), (PermissionError('Только администратор.'), 403)):
            with self.subTest(status=status), patch.object(accounting, 'confirm_refund', new=AsyncMock(side_effect=exception)), \
                    self.assertRaises(HTTPException) as error:
                await admin_web.confirm_referral_refund(self.confirmation(), admin_id=admin_web.ADMIN_ID)
            self.assertEqual(error.exception.status_code, status)

    async def test_legacy_preview_is_not_silently_hidden(self):
        with patch.object(accounting, 'refund_preview', new=AsyncMock(return_value=preview(linked=False, manual_review_required=True))):
            result = await admin_web.prepare_referral_refund(body(), admin_id=admin_web.ADMIN_ID)
        self.assertTrue(result['preview']['manual_review_required'])

    async def test_bot_command_is_read_only_and_admin_guarded(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=admin.ADMIN_ID + 1), answer=AsyncMock())
        with patch.object(accounting, 'funnel_stats', new=AsyncMock(return_value={'views': 1})) as funnel:
            await admin.admin_referral_stats(message)
            funnel.assert_not_awaited()
            message.from_user.id = admin.ADMIN_ID
            await admin.admin_referral_stats(message)
        funnel.assert_awaited_once()
        text = message.answer.await_args.args[0]
        self.assertIn('не готовая конверсия', text)
        self.assertIn('не переводят деньги', text)


class AdminUiTests(unittest.TestCase):
    def test_ui_has_invoice_review_and_separate_actual_transfer_confirmation(self):
        root = Path(__file__).parents[1]
        markup = (root / 'static/admin/index.html').read_text()
        javascript = (root / 'static/admin/assets/admin.js').read_text()
        for name in ('section-referrals', 'refundForm', 'refundDialog', 'refundActualTransfer', 'confirmRefund', 'referralReviewPagination'):
            self.assertIn(f'id="{name}"', markup)
        self.assertIn("api('/referrals/refunds/prepare'", javascript)
        self.assertIn("api('/referrals/refunds/confirm'", javascript)
        self.assertIn('confirm_transferred:true', javascript)
        self.assertIn('esc(data.reference)', javascript)
        self.assertIn('esc(p.recorded_refund?.note || data.note)', javascript)
        self.assertIn('manual_review_required', javascript)
        self.assertIn('review_token:review.review_token', javascript)
        self.assertIn('не готовая конверсия', markup)


@unittest.skipUnless(os.environ.get('PGLITE_MODULE'), 'Optional isolated PostgreSQL/WASM environment')
class AdminPayoutSqlTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_negative_balance_blocks_only_pending_referral_cash_rows(self):
        from test_payment_recovery_postgres import Bridge
        sql = await Bridge().start()
        self.addAsyncCleanup(sql.close)
        self.enterContext(patch.object(db, '_pool', sql))
        await sql.execute('INSERT INTO users(tg_id) VALUES(123),(456)')
        await sql.execute("INSERT INTO partnerships(tg_id,percentage) VALUES(123,20)")
        payment_id = await sql.fetchval('''INSERT INTO payments(tg_id,tariff_code,amount,provider,invoice_id,status)
            VALUES(456,'regular_1m',6000,'yookassa','original-paid','paid') RETURNING id''')
        earning_id = await sql.fetchval('''INSERT INTO referral_earnings
            (referrer_id,referred_user_id,tariff_code,amount,referral_share,payment_id)
            VALUES(123,456,'regular_1m',6000,2000,$1) RETURNING id''', payment_id)
        await sql.execute('''INSERT INTO referral_withdrawals(referrer_id,amount,withdrawal_type,status)
            VALUES(123,1500,'sbp','pending'),(123,20,'usdt','pending'),
                  (123,300,'subscription_regular_1m','completed'),(123,500,'sbp','rejected')''')
        await sql.execute("INSERT INTO payments(tg_id,tariff_code,amount,provider,invoice_id) VALUES(123,'regular_1m',100,'referral_balance','held-wallet')")
        await sql.execute('''INSERT INTO referral_wallet_reservations
            (user_id,request_key,invoice_id,tariff_code,amount,purchase_mode)
            VALUES(123,'held-request','held-wallet','regular_1m',100,'new')''')
        await sql.execute("INSERT INTO partner_withdrawals(partner_id,amount,withdrawal_type,status) VALUES(123,1500,'sbp','pending')")
        before = await store.list_requests()
        self.assertEqual(len(before), 3)
        for row in before:
            if row['program'] == 'referral':
                self.assertEqual(row['current_available_balance'], Decimal('80'))
                self.assertFalse(row['payout_blocked'])
        refund_id = await sql.fetchval('''INSERT INTO referral_confirmed_refunds
            (payment_id,invoice_id,amount,reference,confirmed_by,note)
            VALUES($1,'original-paid',600,'actual-transfer',999,'Actual return') RETURNING id''', payment_id)
        await sql.execute('''INSERT INTO referral_earning_reversals
            (refund_id,earning_id,payment_id,referrer_id,amount) VALUES($1,$2,$3,123,200)''', refund_id, earning_id, payment_id)
        after = await store.list_requests()
        for row in after:
            if row['program'] == 'referral':
                self.assertEqual(row['current_available_balance'], Decimal('-120'))
                self.assertTrue(row['payout_blocked'])
            else:
                self.assertIsNone(row['current_available_balance'])
                self.assertFalse(row['payout_blocked'])
        # Pagination selects one row, but all pending reservations still count.
        page = await store.list_requests(limit=1, offset=1)
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]['program'], 'referral')
        self.assertEqual(page[0]['current_available_balance'], Decimal('-120'))
        self.assertTrue(page[0]['payout_blocked'])
        completed = await store.list_requests(status='completed')
        self.assertEqual(completed, [])  # Subscription spending is not a cash payout.


if __name__ == '__main__':
    unittest.main()
