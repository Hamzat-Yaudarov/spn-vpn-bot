"""Bounded callback acknowledgements; no settings discovery or money writes."""
import asyncio

from services.partner_bot import _identity
from services.partner_security import validate_bot_token


class PartnerCallbackAcknowledger:
    def __init__(self, project_loader, vault, *, sender=None):
        self.loader, self.vault = project_loader, vault
        self.sender = sender or self._send

    async def __call__(self, project, update):
        try:
            # Never acknowledge forwarded, foreign-chat or forged-bot messages.
            identity = _identity(update, project)
            if identity is None or identity[2] is None:
                return
            await asyncio.wait_for(self._ack(project, identity[2]), timeout=2.5)
        except Exception:
            # Acknowledgement is UI-only; never fail a durable purchase or log
            # token-bearing transport exceptions because Telegram is unavailable.
            return

    async def _ack(self, project, callback_id):
        current = await self.loader(project['id'])
        bot_id = project['bot']['id']
        if current['id'] != project['id'] or current['status'] != 'active' or current['bot']['id'] != bot_id:
            return
        token = self.vault.decrypt(project['id'], bot_id, current['encrypted_token'])
        validate_bot_token(token)
        if int(token.split(':', 1)[0]) != bot_id:
            return
        await self.sender(token, callback_id)

    @staticmethod
    async def _send(token, callback_id):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2, connect=1), trust_env=False) as session:
            async with session.post('https://api.telegram.org/bot' + token + '/answerCallbackQuery',
                    json={'callback_query_id': callback_id, 'cache_time': 0}, allow_redirects=False) as response:
                await response.content.read(1024)
