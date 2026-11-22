"""Offline admin HTTP checks. No real tokens, orders, probes or SSH."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
import uuid

import httpx

from voxa_fleet.auth import Auth
from voxa_fleet.credentials import Credentials, CredentialError
from voxa_fleet.runtime import Runtime
from voxa_fleet.web import create_app


PASSWORD = "offline-owner-password-123"
PROJECT = "fleet-test"


class FakeProvider:
    calls = []

    def __init__(self, token, project_id=None):
        assert token == "offline-provider-token"
        self.project_id = project_id

    def identity(self):
        self.calls.append("identity")
        return {"key": {"scopes": ["read"], "project": {"id": "project-id", "slug": PROJECT}},
                "project": {"id": "project-id", "bound": True}}

    def balance(self):
        self.calls.append("balance")
        return {"balance": "20.00", "currency": "EUR"}

    def products(self):
        self.calls.append("products")
        return [{"id": "11111111-1111-4111-8111-111111111111", "name": "Test tariff", "hourly_price": "0.02"}]


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "private"
        self.runtime = Runtime(self.root, provider_factory=FakeProvider)
        self.runtime.auth.set_password(PASSWORD)
        self.app = create_app(runtime=self.runtime)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")
        FakeProvider.calls = []

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        session = (await self.client.get("/api/session")).json()
        response = await self.client.post("/api/login", json={"password": PASSWORD},
                                          headers={"X-CSRF-Token": session["csrf"]})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": response.json()["csrf"]}

    async def test_anonymous_cannot_read_state_and_assets_have_security_headers(self):
        self.assertEqual((await self.client.get("/api/overview")).status_code, 401)
        page = await self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("script-src 'self'", page.headers["content-security-policy"])
        self.assertIn("/static/app.js", page.text)
        self.assertEqual((await self.client.get("/static/app.js")).status_code, 200)

    async def test_login_rotates_session_and_csrf_and_logout_invalidates(self):
        guest = (await self.client.get("/api/session")).json()
        old_cookie = self.client.cookies.get("spn_fleet_session")
        headers = await self.login()
        self.assertNotEqual(self.client.cookies.get("spn_fleet_session"), old_cookie)
        self.assertNotEqual(headers["X-CSRF-Token"], guest["csrf"])
        self.assertEqual((await self.client.get("/api/overview")).status_code, 200)
        self.assertEqual((await self.client.post("/api/logout", json={}, headers=headers)).status_code, 200)
        self.assertEqual((await self.client.get("/api/overview")).status_code, 401)

    async def test_csrf_and_cross_origin_mutations_rejected(self):
        headers = await self.login()
        self.assertEqual((await self.client.put("/api/settings", json={"daily_budget": "100"})).status_code, 403)
        response = await self.client.put("/api/settings", json={"daily_budget": "100"},
                                         headers={**headers, "Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.runtime.store.get_settings()["daily_budget"], "0")

    async def test_money_off_by_default_and_cannot_enable_without_limits_terms(self):
        headers = await self.login()
        response = await self.client.put("/api/settings", json={"financial_enabled": True}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.runtime.store.get_settings()["financial_enabled"])
        response = await self.client.put("/api/settings", json={"daily_budget": "10", "max_hourly": "1",
            "terms_accepted": True, "financial_enabled": True, "auto_enabled": True}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["auto_enabled"])
        response = await self.client.put("/api/settings", json={"financial_enabled": False}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["auto_enabled"])

    async def test_secret_fields_are_write_only_and_blank_does_not_erase(self):
        headers = await self.login()
        secrets = {"provider_token": "offline-provider-token", "cloudflare_token": "offline-cloudflare-token",
                   "remnawave_token": "offline-panel-token", "project_id": PROJECT,
                   "remnawave_url": "https://panel.example.com/api"}
        response = await self.client.post("/api/credentials", json=secrets, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["provider_token"], True)
        await self.client.post("/api/credentials", json={"provider_token": ""}, headers=headers)
        self.assertEqual(self.runtime.credentials.read()["provider_token"], secrets["provider_token"])
        for path in ("/api/overview", "/api/settings", "/api/events"):
            body = (await self.client.get(path)).text
            for key in ("provider_token", "cloudflare_token", "remnawave_token"):
                self.assertNotIn(secrets[key], body)
        self.assertEqual(self.runtime.credentials.path.stat().st_mode & 0o777, 0o600)

    async def test_refresh_is_read_only_and_overview_poll_does_not_hit_provider(self):
        headers = await self.login()
        self.runtime.credentials.update({"provider_token": "offline-provider-token", "project_id": PROJECT})
        preflight = await self.client.post("/api/preflight", json={}, headers=headers)
        self.assertEqual(preflight.status_code, 200, preflight.text)
        self.assertFalse(preflight.json()["financial_ready"])
        catalog = await self.client.post("/api/catalog/refresh", json={}, headers=headers)
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()["products"][0]["hourly_price"], "0.02")
        count = len(FakeProvider.calls)
        for _ in range(3):
            overview = await self.client.get("/api/overview")
            self.assertEqual(overview.status_code, 200)
        self.assertEqual(len(FakeProvider.calls), count)

    async def test_create_retry_keeps_one_logical_server_and_queued_operation(self):
        headers = await self.login()
        headers["X-Request-ID"] = str(uuid.uuid4())
        body = {"name": "Нидерланды", "domain": "nl-test.example.com", "country": "NL",
                "product_id": "11111111-1111-4111-8111-111111111111", "location": "amsterdam",
                "mode": "observe", "squads": ["22222222-2222-4222-8222-222222222222"]}
        first = await self.client.post("/api/servers", json=body, headers=headers)
        again = await self.client.post("/api/servers", json=body, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(first.json()["operation"]["id"], again.json()["operation"]["id"])
        self.assertEqual(len(self.runtime.store.list_servers()), 1)
        self.assertEqual(len(self.runtime.store.list_operations()), 1)
        self.assertEqual(FakeProvider.calls, [])

    async def test_untrusted_operation_data_and_event_secrets_not_returned(self):
        headers = await self.login()
        server = self.runtime.store.create_server({"name": "Test", "domain": "test.example.com"})
        op = self.runtime.store.enqueue_operation("check", server["id"], {"bootstrap_token": "must-not-leak"})
        self.runtime.store.add_event("test", "Public message", data={"token": "must-not-leak"}, operation_id=op["id"])
        for path in ("/api/overview", "/api/operations", "/api/events"):
            self.assertNotIn("must-not-leak", (await self.client.get(path)).text)

    async def test_pause_does_not_delete_or_cancel_jobs(self):
        headers = await self.login()
        server = self.runtime.store.create_server({"name": "Test", "domain": "test.example.com"})
        response = await self.client.post(f'/api/servers/{server["id"]}/actions/pause', json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.runtime.store.get_server(server["id"])["enabled"])
        self.assertEqual(FakeProvider.calls, [])


class PrivateAuthTests(unittest.TestCase):
    def test_credentials_symlink_and_password_strength_and_rate_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve()
            private = path / "private"
            credentials = Credentials(private)
            auth = Auth(private / "auth.json", clock=lambda: 1000)
            with self.assertRaises(ValueError):
                auth.set_password("short")
            auth.set_password(PASSWORD)
            with self.assertRaises(ValueError):
                auth.set_password(PASSWORD)
            token, _ = auth.session()
            for _ in range(5):
                with self.assertRaises(ValueError):
                    auth.login(token, "wrong", "attacker")
            with self.assertRaisesRegex(ValueError, "10 минут"):
                auth.login(token, PASSWORD, "attacker")
            target = path / "symlink"
            target.symlink_to(private, target_is_directory=True)
            with self.assertRaises(CredentialError):
                Credentials(target)


if __name__ == "__main__":
    unittest.main()
