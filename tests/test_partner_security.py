"""Chunked Telegram response handling, fully local fake transport."""
import asyncio
import json
import unittest
from unittest.mock import patch

from services.partner_security import BotConnectionError, TelegramBotVerifier


TOKEN = "200002:" + "B" * 35


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class Response:
    def __init__(self, chunks, status=200):
        self.status = status
        self.content = Content(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class TelegramVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self, response):
        session = Session(response)
        with patch("aiohttp.ClientSession", return_value=session) as factory:
            result = await TelegramBotVerifier().verify(TOKEN)
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertEqual(session.calls, [(f"https://api.telegram.org/bot{TOKEN}/getMe", {"allow_redirects": False})])
        return result

    async def test_valid_json_split_across_network_chunks(self):
        raw = json.dumps({"ok": True, "result": {"id": 200002, "is_bot": True, "username": "PartnerBot"}}).encode()
        result = await self.verify(Response([raw[:7], raw[7:40], raw[40:]]))
        self.assertEqual(result, {"id": 200002, "username": "PartnerBot"})

    async def test_large_or_incomplete_response_fails_without_echoing_input(self):
        for response in (Response([b"x" * 8192] * 9), Response([b'{"ok":']), Response([], 401)):
            with self.assertRaises(BotConnectionError) as error:
                await self.verify(response)
            self.assertNotIn(TOKEN, str(error.exception))

    async def test_wrong_bot_or_non_bot_is_rejected(self):
        for result in ({"id": 200003, "is_bot": True, "username": "PartnerBot"},
                       {"id": 200002, "is_bot": False, "username": "PartnerBot"}):
            with self.assertRaises(BotConnectionError):
                await self.verify(Response([json.dumps({"ok": True, "result": result}).encode()]))
