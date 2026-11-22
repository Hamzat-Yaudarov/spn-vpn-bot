"""Callback UI acknowledgement is bounded, tenant-scoped and money-free."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi import FastAPI

from partner_client_web import create_client_router, webhook_secret
from services.partner_callback_ack import PartnerCallbackAcknowledger
from test_partner_client_web import project, callback, update, PROJECT_ID, BOT_ID, WEBHOOK_KEY

TOKEN = str(BOT_ID) + ':' + 'x' * 35


class CallbackAckTests(unittest.IsolatedAsyncioTestCase):
    def setup_ack(self):
        loader = AsyncMock(return_value={**project(), 'encrypted_token': 'cipher'})
        vault, sender = Mock(), AsyncMock()
        vault.decrypt.return_value = TOKEN
        return PartnerCallbackAcknowledger(loader, vault, sender=sender), loader, vault, sender

    async def test_valid_callback_uses_fresh_exact_tenant_token(self):
        ack, loader, vault, sender = self.setup_ack()
        await ack(project(), callback())
        loader.assert_awaited_once_with(PROJECT_ID)
        vault.decrypt.assert_called_once_with(PROJECT_ID, BOT_ID, 'cipher')
        sender.assert_awaited_once_with(TOKEN, 'offline-callback-id')

    async def test_noncallback_or_foreign_chat_never_loads_token(self):
        ack, loader, _, sender = self.setup_ack()
        await ack(project(), update())
        bad = callback(); bad['callback_query']['message']['chat']['id'] = 333
        await ack(project(), bad)
        bad = callback(); bad['callback_query']['message']['from']['id'] += 1
        await ack(project(), bad)
        loader.assert_not_awaited(); sender.assert_not_awaited()

    async def test_inactive_changed_bot_and_wrong_token_do_not_send(self):
        for change in ({'status': 'suspended'}, {'bot': {'id': BOT_ID + 1}}, {'id': 'foreign'}):
            ack, loader, _, sender = self.setup_ack()
            loader.return_value.update(change)
            await ack(project(), callback())
            sender.assert_not_awaited()
        ack, _, vault, sender = self.setup_ack()
        vault.decrypt.return_value = str(BOT_ID + 1) + ':' + 'x' * 35
        await ack(project(), callback()); sender.assert_not_awaited()

    async def test_transport_or_loader_failure_is_not_a_purchase_failure(self):
        for failed in ('loader', 'sender', 'vault'):
            ack, loader, vault, sender = self.setup_ack()
            {'loader': loader, 'sender': sender, 'vault': vault.decrypt}[failed].side_effect = RuntimeError(TOKEN)
            await ack(project(), callback())

    async def test_webhook_ack_runs_in_parallel_and_keeps_visible_screen(self):
        started = asyncio.Event()
        async def ack(project_value, update_value):
            started.set()
        async def handle(*args):
            await asyncio.wait_for(started.wait(), timeout=0.2)
            return [{'method': 'editMessageText', 'payload': {'chat_id': 222, 'message_id': 5, 'text': 'Ready'}}]
        handler = Mock(); handler.handle = handle
        acknowledgement = AsyncMock(side_effect=ack)
        app = FastAPI()
        app.include_router(create_client_router(AsyncMock(return_value=project()), handler,
            secret_key=WEBHOOK_KEY, enabled=True, callback_acknowledger=acknowledgement))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://offline.test') as client:
            path = f'/partner-bots/{PROJECT_ID}/webhook'
            denied = await client.post(path, json=callback())
            self.assertEqual(denied.status_code, 403); acknowledgement.assert_not_awaited()
            result = await client.post(path, json=callback(), headers={
                'X-Telegram-Bot-Api-Secret-Token': webhook_secret(WEBHOOK_KEY, PROJECT_ID, BOT_ID)})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['method'], 'editMessageText')
            self.assertEqual(result.json()['text'], 'Ready')
            self.assertNotIn(TOKEN, result.text)
            acknowledgement.assert_awaited_once()
