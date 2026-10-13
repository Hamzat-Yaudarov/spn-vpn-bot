"""Real ASGI route wiring/auth/cache checks, with all storage/actions mocked."""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode
import unittest
from unittest.mock import AsyncMock, patch

import httpx

with patch('dotenv.load_dotenv', return_value=False), patch.dict(os.environ, {'DATABASE_URL':'', 'ADMIN_ID':'999'}):
    import webhooks
    from services import miniapp_referral as actions


TOKEN = '123456789:offline-fixture-token'
KEY = '6de2e2ce-1100-4d4e-8aef-d77fbfdf447c'


def signed_user(user_id=123):
    values = {'auth_date':str(int(time.time())), 'user':json.dumps({'id':user_id, 'first_name':'Тест'}, separators=(',', ':'))}
    checked = '\n'.join(f'{key}={value}' for key, value in sorted(values.items()))
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    values['hash'] = hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class NativeRouteWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(webhooks, 'BOT_TOKEN', TOKEN))
        self.create_user = self.enterContext(patch.object(webhooks.db, 'create_user', new=AsyncMock()))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=webhooks.app), base_url='https://offline.test'))

    async def test_no_website_cookie_or_bearer_can_authorize_native_money_routes(self):
        for verb, suffix in [('GET',f'purchases/{KEY}'), ('GET',f'withdrawals/{KEY}'), ('POST','purchases'), ('POST','withdrawals')]:
            with (
                self.subTest(verb=verb, route=suffix),
                patch.object(actions, 'create_purchase', new=AsyncMock()) as buy,
                patch.object(actions, 'create_withdrawal', new=AsyncMock()) as withdraw,
                patch.object(actions, 'get_purchase', new=AsyncMock()) as get_buy,
                patch.object(actions, 'get_withdrawal', new=AsyncMock()) as get_withdraw,
            ):
                response = await self.client.request(verb, '/miniapp/api/referral/'+suffix,
                    headers={'Authorization':'Bearer not-telegram', 'Cookie':'wayspn_web_session=not-telegram'}, json={})
                self.assertEqual(response.status_code, 401)
                self.assertIn('no-store', response.headers['cache-control'])
                for operation in (buy, withdraw, get_buy, get_withdraw):
                    operation.assert_not_awaited()
        self.create_user.assert_not_awaited()

    async def test_gets_use_only_signed_telegram_owner_and_do_not_cache_private_receipts(self):
        for suffix, function in [('purchases', 'get_purchase'), ('withdrawals', 'get_withdrawal')]:
            with self.subTest(suffix=suffix), patch.object(actions, function, new=AsyncMock(return_value={'request_key':KEY,'status':'pending','amount':'1500.00'})) as action:
                bot = object()
                with patch.object(webhooks, '_bot', bot):
                    response = await self.client.get(f'/miniapp/api/referral/{suffix}/{KEY}?user_id=456',
                        headers={'Authorization':'tma '+signed_user()})
                self.assertEqual(response.status_code, 200)
                self.assertIn('no-store', response.headers['cache-control'])
                self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
                expected = (123,KEY,bot) if suffix == 'purchases' else (123,KEY)
                action.assert_awaited_once_with(*expected)
        self.create_user.assert_awaited_with(123,'Тест')

    async def test_runtime_bot_provider_and_auth_are_not_frozen_at_import(self):
        payload = {'request_key':KEY,'tariff_code':'regular_1m','payment_target':'new','subscription_id':None,'expected_amount':'160.00'}
        with patch.object(actions,'create_purchase',new=AsyncMock(return_value={'status':'pending'})) as action:
            bots = [object(),object()]
            for bot in bots:
                with patch.object(webhooks,'_bot',bot):
                    response = await self.client.post('/miniapp/api/referral/purchases',json=payload,
                        headers={'Authorization':'tma '+signed_user()})
                self.assertEqual(response.status_code,200)
            self.assertEqual([call.args[2] for call in action.await_args_list],bots)

    async def test_forged_initdata_is_rejected_before_database_or_money_action(self):
        signed = signed_user().replace('auth_date=', 'auth_date=0')
        with patch.object(actions,'create_withdrawal',new=AsyncMock()) as action:
            response = await self.client.post('/miniapp/api/referral/withdrawals',json={},headers={'Authorization':'tma '+signed})
        self.assertEqual(response.status_code,401)
        self.create_user.assert_not_awaited()
        action.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
