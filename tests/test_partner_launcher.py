"""Paid launch state machine and Telegram transport; no external calls."""
import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from partner_client_web import webhook_secret
from services.partner_launcher import (
    LaunchError, PartnerLaunchService, TelegramLaunchAdapter, TelegramLaunchError,
)
from services.partner_security import TokenVault


PROJECT = "5c970cf0-347b-4c05-9d35-2243bb7589d4"
OTHER = "5c970cf0-347b-4c05-9d35-2243bb7589d5"
ORDER = "309fc91e-11a9-4c9c-844c-0d4e14b6740b"
OWNER = 101
BOT = 200002
TOKEN = f"{BOT}:" + "B" * 35
KEY = b"webhook-key-for-test-only-32bytes!!"
TARGET = f"https://bots.example.test/partner-bots/{PROJECT}/webhook"


class Store:
    def __init__(self, token):
        self.order = {"id": ORDER, "project_id": PROJECT, "owner_tg_id": OWNER,
                      "status": "paid_pending_launch", "price_kopecks": 999000}
        self.job = {"project_id": PROJECT, "order_id": ORDER, "owner_tg_id": OWNER,
                    "bot_id": BOT, "bot_username": "PartnerBot", "encrypted_token": token,
                    "project_version": 4, "status": "pending"}
        self.lock = asyncio.Lock()
        self.in_lease = False
        self.calls = []
        self.status = "draft"
        self.failure = None

    async def get_entry_order(self, project_id, owner_id, order_id):
        self.calls.append(("entry", self.in_lease))
        if self.failure == "read":
            raise RuntimeError("private SQL details")
        if (project_id, owner_id, order_id) != (PROJECT, OWNER, ORDER):
            raise LookupError("not found")
        return copy.deepcopy(self.order)

    @asynccontextmanager
    async def acquire_launch_lock(self, project_id):
        self.calls.append(("lock", project_id))
        async with self.lock:
            self.in_lease = True
            try:
                yield self
            finally:
                self.in_lease = False

    async def get_launch(self, project_id, order_id):
        self.calls.append(("job", self.in_lease))
        assert self.in_lease
        if self.status in {"suspended", "closed"}:
            raise LaunchError("Проект недоступен.")
        return copy.deepcopy(self.job)

    async def mark_launch_retry(self, project_id, order_id, *, needs_webhook_replace=False):
        self.calls.append(("retry", needs_webhook_replace, self.in_lease))
        assert self.in_lease
        if self.failure == "retry":
            raise RuntimeError("private DB failure")
        self.job["needs_webhook_replace"] = needs_webhook_replace
        return copy.deepcopy(self.order)

    async def complete_launch(self, project_id, order_id, **kwargs):
        self.calls.append(("complete", kwargs, self.in_lease))
        assert self.in_lease
        if self.failure == "complete":
            raise RuntimeError("private DB failure")
        self.order["status"] = "launched"
        self.status = "active"
        return copy.deepcopy(self.order)


class Telegram:
    def __init__(self, url=""):
        self.url, self.allowed = url, []
        self.calls = []
        self.failure = None
        self.wrong_identity = False
        self.read_count = 0

    async def get_me(self, token):
        self.calls.append(("getMe", token))
        await asyncio.sleep(0)
        if self.failure == "identity":
            raise RuntimeError(f"https://api.telegram.org/bot{TOKEN}/private")
        return {"id": BOT + 1 if self.wrong_identity else BOT,
                "is_bot": True, "username": "RenamedBot"}

    async def get_webhook_info(self, token):
        self.calls.append(("getWebhookInfo", token))
        self.read_count += 1
        if self.failure == "readback" and self.read_count % 2 == 0:
            return {"url": self.url, "allowed_updates": ["message"]}
        return {"url": self.url, "allowed_updates": list(self.allowed)}

    async def set_webhook(self, token, *, url, secret_token):
        self.calls.append(("setWebhook", token, url, secret_token))
        self.url, self.allowed = url, ["message", "callback_query"]
        if self.failure == "ambiguous_set":
            self.failure = None
            raise TimeoutError(f"{TOKEN} {secret_token}")


class LaunchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        self.vault = TokenVault((Fernet.generate_key().decode(),))
        self.store = Store(self.vault.encrypt(PROJECT, BOT, TOKEN))
        self.telegram = Telegram()
        self.service = self.make()

    def make(self, **kwargs):
        return PartnerLaunchService(self.store, self.vault, self.telegram,
                                    webhook_base_url=kwargs.pop("webhook_base_url", "https://bots.example.test"),
                                    webhook_secret_key=kwargs.pop("webhook_secret_key", KEY),
                                    clients_enabled=kwargs.pop("clients_enabled", True), **kwargs)

    async def launch(self, **kwargs):
        return await self.service.launch(PROJECT, OWNER, ORDER, **kwargs)

    def test_ready_requires_explicit_switch_and_all_safe_config(self):
        self.assertTrue(self.service.ready)
        for value in (False, "true", 1, None):
            self.assertFalse(self.make(clients_enabled=value).ready)
        for url in ("", "http://bots.example.test", "https://user:pass@example.test",
                    "https://example.test/path", "https://example.test?a=1", "https://example.test?",
                    "https://example.test#fragment", "https://example.test#", "https://example.test:8443",
                    "https://127.0.0.1", "https://*.example.test", "https://example.test\\@evil.test",
                    "https://example.test:bad", "https://example.test:", "https://localhost"):
            self.assertFalse(self.make(webhook_base_url=url).ready, url)
        for key in (b"short", "not bytes", None, b"x" * 513):
            self.assertFalse(self.make(webhook_secret_key=key).ready)
        self.telegram.ready = False
        self.assertFalse(self.service.ready)

    async def test_default_construction_is_disabled_and_does_no_io(self):
        service = PartnerLaunchService(self.store, self.vault,
                                       webhook_base_url="https://bots.example.test", webhook_secret_key=KEY)
        self.assertFalse(service.ready)
        self.assertEqual((await service.launch(PROJECT, OWNER, ORDER))["launch_state"], "disabled")
        self.assertFalse(any(row[0] == "lock" for row in self.store.calls))

    async def test_paid_owned_project_launches_after_readback_on_same_lease(self):
        result = await self.launch()
        self.assertEqual(result, {"launch_state": "launched"})
        self.assertEqual(self.store.status, "active")
        self.assertEqual([call[0] for call in self.telegram.calls],
                         ["getMe", "getWebhookInfo", "setWebhook", "getWebhookInfo"])
        complete = next(call for call in self.store.calls if call[0] == "complete")
        secret = webhook_secret(KEY, PROJECT, BOT)
        self.assertEqual(complete[1], {"bot_id": BOT, "bot_username": "RenamedBot", "webhook_url": TARGET,
                          "secret_fingerprint": hashlib.sha256(secret.encode()).hexdigest()})
        self.assertTrue(complete[2])
        self.assertNotIn(TOKEN, repr(result))
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, repr(complete))
        self.assertEqual([c[1] for c in self.store.calls if c[0] == "job"], [True])

    async def test_foreign_owner_and_unpaid_never_call_telegram(self):
        with self.assertRaises(LookupError):
            await self.service.launch(PROJECT, OWNER + 1, ORDER)
        for status in ("created", "canceled", "refunded"):
            self.store.order["status"] = status
            with self.assertRaises(LaunchError):
                await self.launch()
        self.assertEqual(self.telegram.calls, [])

    async def test_suspended_closed_or_wrong_private_job_never_call_telegram(self):
        for state in ("suspended", "closed"):
            self.store.status = state
            with self.assertRaises(LaunchError):
                await self.launch()
        self.store.status = "draft"
        for key, bad in (("project_id", OTHER), ("order_id", OTHER), ("owner_tg_id", 102),
                         ("bot_id", True), ("bot_username", "bad"), ("encrypted_token", None)):
            original = self.store.job[key]
            self.store.job[key] = bad
            with self.assertRaises(LaunchError):
                await self.launch()
            self.store.job[key] = original
        self.assertEqual(self.telegram.calls, [])

    async def test_already_launched_is_idempotent_even_if_service_disabled(self):
        self.store.order["status"] = "launched"
        self.service = self.make(clients_enabled=False)
        self.assertEqual(await self.launch(), {"launch_state": "launched"})
        self.assertEqual(self.telegram.calls, [])

    async def test_foreign_webhook_needs_owner_confirmation_no_write_or_url_exposure(self):
        self.telegram.url = "https://old-private-service.test/contains-private-secret"
        result = await self.launch()
        self.assertEqual(result["launch_state"], "needs_webhook_replace")
        self.assertNotIn("old-private-service", repr(result))
        self.assertEqual([call[0] for call in self.telegram.calls], ["getMe", "getWebhookInfo"])
        self.assertEqual(self.store.order["status"], "paid_pending_launch")
        self.assertTrue(self.store.job["needs_webhook_replace"])
        result = await self.launch(replace_existing=True)
        self.assertEqual(result["launch_state"], "launched")

    async def test_same_webhook_reapplies_secret_not_skipped_based_on_url(self):
        self.telegram.url = TARGET
        self.telegram.allowed = ["message", "callback_query"]
        await self.launch()
        writes = [call for call in self.telegram.calls if call[0] == "setWebhook"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][3], webhook_secret(KEY, PROJECT, BOT))

    async def test_ambiguous_set_retry_reuses_same_url_secret_no_second_fee(self):
        self.telegram.failure = "ambiguous_set"
        result = await self.launch()
        self.assertEqual(result["launch_state"], "retry")
        self.assertEqual(self.store.order["status"], "paid_pending_launch")
        self.assertEqual(self.store.status, "draft")
        result = await self.launch()
        self.assertEqual(result["launch_state"], "launched")
        writes = [call for call in self.telegram.calls if call[0] == "setWebhook"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0], writes[1])
        self.assertEqual(self.store.order["price_kopecks"], 999000)

    async def test_readback_mismatch_never_activates(self):
        self.telegram.failure = "readback"
        result = await self.launch()
        self.assertEqual(result["launch_state"], "retry")
        self.assertEqual(self.store.status, "draft")
        self.assertFalse(any(call[0] == "complete" for call in self.store.calls))

    async def test_wrong_identity_or_invalid_vault_binding_never_write_webhook(self):
        self.telegram.wrong_identity = True
        self.assertEqual((await self.launch())["launch_state"], "retry")
        self.assertEqual([c[0] for c in self.telegram.calls], ["getMe"])
        self.telegram.calls.clear()
        self.store.job["encrypted_token"] = self.vault.encrypt(OTHER, BOT, TOKEN)
        self.assertEqual((await self.launch())["launch_state"], "retry")
        self.assertEqual(self.telegram.calls, [])

    async def test_errors_are_generic_and_never_echo_credentials(self):
        self.telegram.failure = "identity"
        result = await self.launch()
        self.assertEqual(result["launch_state"], "retry")
        self.assertNotIn(TOKEN, str(result))
        self.assertNotIn(TOKEN, repr(self.service))
        self.assertNotIn(KEY.decode(), repr(self.service))

    async def test_db_failures_not_acknowledged_as_success_or_network_retry(self):
        self.store.failure = "read"
        with self.assertRaises(RuntimeError):
            await self.launch()
        self.assertEqual(self.telegram.calls, [])
        self.store.failure = "complete"
        with self.assertRaises(RuntimeError):
            await self.launch()
        self.assertEqual(self.store.order["status"], "paid_pending_launch")
        self.assertFalse(any(call[0] == "retry" for call in self.store.calls))
        self.store.failure = "retry"
        self.telegram.url = "https://other-service.test"
        with self.assertRaises(RuntimeError):
            await self.launch()

    async def test_concurrent_launches_only_perform_one_write(self):
        results = await asyncio.gather(self.launch(), self.launch(), self.launch())
        self.assertTrue(all(row["launch_state"] == "launched" for row in results))
        self.assertEqual(len([call for call in self.telegram.calls if call[0] == "setWebhook"]), 1)
        self.assertEqual(len([call for call in self.store.calls if call[0] == "complete"]), 1)

    async def test_changed_replace_flag_and_invalid_ids_rejected_before_io(self):
        for value in (1, "yes", None):
            with self.assertRaises(LaunchError):
                await self.launch(replace_existing=value)
        for project, owner, order in (("bad", OWNER, ORDER), (PROJECT, True, ORDER),
                                      (PROJECT, 0, ORDER), (PROJECT, OWNER, "bad")):
            with self.assertRaises(LaunchError):
                await self.service.launch(project, owner, order)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.telegram.calls, [])


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class Response:
    def __init__(self, result=None, *, chunks=None, status=200):
        self.status = status
        self.content = Content(chunks if chunks is not None else
                               [json.dumps({"ok": True, "result": result}).encode()])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return next(self.responses)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return next(self.responses)


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_host_bounded_chunked_response_and_transport_security(self):
        raw = json.dumps({"ok": True, "result": {"id": BOT, "is_bot": True,
                                                    "username": "PartnerBot"}}).encode()
        session = Session([Response(chunks=[raw[:5], raw[5:40], raw[40:]])])
        with patch("aiohttp.ClientSession", return_value=session) as factory:
            result = await TelegramLaunchAdapter().get_me(TOKEN)
        self.assertEqual(result["id"], BOT)
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 20)
        self.assertEqual(factory.call_args.kwargs["timeout"].connect, 5)
        self.assertEqual(session.calls, [("get", f"https://api.telegram.org/bot{TOKEN}/getMe",
                                         {"allow_redirects": False})])

    async def test_webhook_write_explicitly_preserves_updates_and_sends_no_authorization(self):
        session = Session([Response({"url": "", "pending_update_count": 7}), Response(True)])
        secret = webhook_secret(KEY, PROJECT, BOT)
        with patch("aiohttp.ClientSession", return_value=session):
            adapter = TelegramLaunchAdapter()
            info = await adapter.get_webhook_info(TOKEN)
            await adapter.set_webhook(TOKEN, url=TARGET, secret_token=secret)
        self.assertEqual(info["pending_update_count"], 7)
        self.assertEqual(session.calls[1], ("post", f"https://api.telegram.org/bot{TOKEN}/setWebhook", {
            "json": {"url": TARGET, "secret_token": secret,
                     "allowed_updates": ["message", "callback_query"], "drop_pending_updates": False},
            "allow_redirects": False,
        }))
        self.assertNotIn("Authorization", repr(session.calls))

    async def test_response_failures_are_redacted_and_not_chained(self):
        responses = (Response(chunks=[b"x" * 8192] * 9), Response(chunks=[b'{"ok":']),
                     Response(chunks=[b"{}"]), Response(status=302),
                     Response(chunks=[json.dumps({"ok": False, "description": TOKEN}).encode()]),
                     Response(chunks=[b"[]"]), Response(chunks=[b"null"]))
        for response in responses:
            with patch("aiohttp.ClientSession", return_value=Session([response])):
                with self.assertRaises(TelegramLaunchError) as error:
                    await TelegramLaunchAdapter().get_me(TOKEN)
                self.assertNotIn(TOKEN, str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)

    async def test_network_exception_and_invalid_token_never_leak_credentials(self):
        session = Session([])
        def network_failure(*args, **kwargs):
            raise RuntimeError(f"https://api.telegram.org/bot{TOKEN}/getMe")
        session.get = network_failure
        with patch("aiohttp.ClientSession", return_value=session):
            with self.assertRaises(TelegramLaunchError) as error:
                await TelegramLaunchAdapter().get_me(TOKEN)
        self.assertNotIn(TOKEN, repr(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        for token in (None, "invalid", TOKEN + "/getUpdates", TOKEN + "\n"):
            with patch("aiohttp.ClientSession") as factory:
                with self.assertRaises(TelegramLaunchError):
                    await TelegramLaunchAdapter().get_me(token)
                factory.assert_not_called()

    async def test_private_api_methods_cannot_be_used_as_arbitrary_transport(self):
        with patch("aiohttp.ClientSession") as factory:
            with self.assertRaises(TelegramLaunchError):
                await TelegramLaunchAdapter()._call(TOKEN, "deleteWebhook")
            factory.assert_not_called()

    async def test_wrong_identity_and_invalid_webhook_shapes_rejected(self):
        for identity in ({"id": BOT + 1, "is_bot": True, "username": "PartnerBot"},
                         {"id": BOT, "is_bot": False, "username": "PartnerBot"},
                         {"id": BOT, "is_bot": True, "username": "bad"}):
            with patch("aiohttp.ClientSession", return_value=Session([Response(identity)])):
                with self.assertRaises(TelegramLaunchError):
                    await TelegramLaunchAdapter().get_me(TOKEN)
        for info in ({}, {"url": 0}, {"url": "x" * 2049}, {"url": "", "allowed_updates": "message"}):
            with patch("aiohttp.ClientSession", return_value=Session([Response(info)])):
                with self.assertRaises(TelegramLaunchError):
                    await TelegramLaunchAdapter().get_webhook_info(TOKEN)

    async def test_invalid_write_and_false_result_fail_closed(self):
        for url, secret in (("http://bots.example.test/x", "a" * 64),
                            (TARGET + "?x=1", "a" * 64), (TARGET, "not safe"),
                            (TARGET.replace(PROJECT, "invalid"), "a" * 64)):
            with patch("aiohttp.ClientSession") as factory:
                with self.assertRaises(TelegramLaunchError):
                    await TelegramLaunchAdapter().set_webhook(TOKEN, url=url, secret_token=secret)
                factory.assert_not_called()
        with patch("aiohttp.ClientSession", return_value=Session([Response(False)])):
            with self.assertRaises(TelegramLaunchError):
                await TelegramLaunchAdapter().set_webhook(TOKEN, url=TARGET,
                                                           secret_token=webhook_secret(KEY, PROJECT, BOT))
