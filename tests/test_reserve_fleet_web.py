"""Offline HTTP and auth regressions: no deployed server is contacted."""
import asyncio
from pathlib import Path
import tempfile
from threading import Event, get_ident
import unittest

import httpx

from spn_reserve_fleet.auth import Auth
from spn_reserve_fleet.store import Store
from spn_reserve_fleet.web import COOKIE, create_app


PASSWORD = "offline-owner-password-123"
SERVER = "de11-1-1.example.com"


class FakeRuntime:
    def __init__(self, root):
        self.store = Store(root)
        self.auth = Auth(root / "password.json")
        self.calls = []
        self.store.register({"domain": SERVER, "name": "🇩🇪 Германия", "current_ip": "8.8.8.8",
            "state_dir": "/private/test-installation-secret", "installation_id": "offline-id",
            "ssh_port": 22, "transport": "raw-tls-vision-v1"})

    def check(self, server_id):
        self.calls.append(("check", server_id, get_ident()))
        return self.store.update(server_id, status="online", failures=0, last_checked=1234567890)

    def switch(self, server_id, ip):
        self.calls.append(("switch", server_id, ip, get_ident()))
        row = self.store.get_server(server_id)
        return self.store.update(server_id, current_ip=ip,
                                 reserves=[item for item in row["reserves"] if item["ip"] != ip])


class ReservePanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "private"
        self.runtime = FakeRuntime(self.root)
        self.runtime.auth.set_password(PASSWORD)
        self.app = create_app(runtime=self.runtime)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        guest = (await self.client.get("/api/session")).json()
        response = await self.client.post("/api/login", json={"password": PASSWORD},
                                          headers={"X-CSRF-Token": guest["csrf"]})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": response.json()["csrf"]}

    async def test_anonymous_denied_and_static_assets_have_csp(self):
        self.assertEqual((await self.client.get("/api/overview")).status_code, 401)
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertIn("/static/app.js", response.text)
        self.assertEqual((await self.client.get("/static/app.js")).status_code, 200)
        self.assertEqual((await self.client.get("/static/app.css")).status_code, 200)

    async def test_login_rotates_cookie_csrf_and_logout_invalidates(self):
        guest = (await self.client.get("/api/session")).json()
        old_cookie = self.client.cookies.get(COOKIE)
        headers = await self.login()
        self.assertNotEqual(old_cookie, self.client.cookies.get(COOKIE))
        self.assertNotEqual(guest["csrf"], headers["X-CSRF-Token"])
        self.assertEqual((await self.client.get("/api/overview")).status_code, 200)
        self.assertEqual((await self.client.post("/api/logout", json={}, headers=headers)).status_code, 200)
        self.assertEqual((await self.client.get("/api/overview")).status_code, 401)

    async def test_csrf_and_foreign_origin_block_mutations(self):
        headers = await self.login()
        endpoint = "/api/servers/" + SERVER + "/reserves"
        self.assertEqual((await self.client.post(endpoint, json={"ip": "1.1.1.1"})).status_code, 403)
        self.assertEqual((await self.client.post(endpoint, json={"ip": "1.1.1.1"},
            headers={**headers, "Origin": "https://attacker.example"})).status_code, 403)
        self.assertEqual(self.runtime.store.get_server(SERVER)["reserves"], [])

    async def test_overview_read_does_not_probe_and_omits_private_paths(self):
        await self.login()
        for _ in range(3):
            response = await self.client.get("/api/overview")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("test-installation-secret", response.text)
            self.assertNotIn("installation_id", response.text)
            self.assertNotIn("state_dir", response.text)
        self.assertEqual(self.runtime.calls, [])
        self.assertFalse(response.json()["servers"][0]["auto_enabled"])
        self.assertIn("time", response.json()["events"][0])

    async def test_reserve_validation_add_delete_and_automatic_toggle(self):
        headers = await self.login()
        prefix = "/api/servers/" + SERVER
        for invalid in ("127.0.0.1", "10.1.1.1", "::1", "8.8.8.8", "; shutdown now"):
            response = await self.client.post(prefix + "/reserves", json={"ip": invalid}, headers=headers)
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.client.put(prefix + "/auto", json={"enabled": True}, headers=headers)
        self.assertEqual(response.status_code, 400)
        response = await self.client.post(prefix + "/reserves", json={"ip": "1.1.1.1"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["reserves"][0]["ip"], "1.1.1.1")
        self.assertNotIn("state_dir", response.text)
        response = await self.client.put(prefix + "/auto", json={"enabled": True}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["auto_enabled"])
        response = await self.client.put(prefix + "/auto", json={"enabled": "false"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        response = await self.client.request("DELETE", prefix + "/reserves/1.1.1.1", json={}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["reserves"], [])

    async def test_switch_only_registered_reserve_and_check_calls_worker_thread(self):
        headers = await self.login()
        prefix = "/api/servers/" + SERVER
        response = await self.client.post(prefix + "/switch", json={"ip": "1.0.0.1"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.runtime.calls, [])
        response = await self.client.post(prefix + "/check", json={}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "online")
        self.assertNotEqual(self.runtime.calls[0][-1], get_ident())
        self.runtime.store.add_reserve(SERVER, "1.1.1.1")
        response = await self.client.post(prefix + "/switch", json={"ip": "1.1.1.1"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["current_ip"], "1.1.1.1")
        self.assertNotEqual(self.runtime.calls[-1][-1], get_ident())

    async def test_slow_check_keeps_read_requests_responsive(self):
        headers = await self.login()
        entered, release = Event(), Event()
        def slow_check(server_id):
            entered.set()
            release.wait(timeout=3)
        self.runtime.check = slow_check
        task = asyncio.create_task(self.client.post("/api/servers/" + SERVER + "/check", json={}, headers=headers))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            overview = await asyncio.wait_for(self.client.get("/api/overview"), timeout=1)
            self.assertEqual(overview.status_code, 200)
            self.assertFalse(task.done())
        finally:
            release.set()
            await task

    async def test_missing_server_and_bad_request_are_safe(self):
        headers = await self.login()
        response = await self.client.post("/api/servers/absent/check", json={}, headers=headers)
        self.assertEqual(response.status_code, 404)
        response = await self.client.post("/api/servers/" + SERVER + "/check", json={"command": "bad"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/api/servers/" + SERVER + "/reserves", content="not-json", headers=headers)
        self.assertEqual(response.status_code, 400)

    async def test_controller_error_is_scrubbed(self):
        headers = await self.login()
        def broken_check(server_id):
            raise ValueError("Bearer super-secret: /private/key.pem")
        self.runtime.check = broken_check
        response = await self.client.post("/api/servers/" + SERVER + "/check", json={}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("super-secret", response.text)
        self.assertNotIn("key.pem", response.text)


class AuthTests(unittest.TestCase):
    def test_password_is_hashed_private_and_login_throttled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "private"
            auth = Auth(root / "password.json", clock=lambda: 1000)
            self.assertFalse(auth.configured())
            auth.set_password(PASSWORD)
            self.assertEqual(auth.path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(PASSWORD, auth.path.read_text())
            with self.assertRaises(ValueError):
                auth.set_password(PASSWORD)
            token, state = auth.session()
            for _ in range(5):
                with self.assertRaisesRegex(ValueError, "Неверный пароль"):
                    auth.login(token, "wrong", "test-source")
            with self.assertRaisesRegex(ValueError, "Слишком много"):
                auth.login(token, PASSWORD, "test-source")
            _, logged_in = auth.login(token, PASSWORD, "different-source")
            self.assertTrue(logged_in["authenticated"])


if __name__ == "__main__":
    unittest.main()
