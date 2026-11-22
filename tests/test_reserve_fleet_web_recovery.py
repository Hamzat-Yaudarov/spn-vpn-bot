"""Recovery/retirement API behavior without a live node or controller."""
import copy
from pathlib import Path
import tempfile
import unittest

import httpx

from spn_reserve_fleet.auth import Auth
from spn_reserve_fleet.store import Store
from spn_reserve_fleet.web import create_app, public_server


SERVER = "legacy.example.com"
CURRENT = "current.example.com"
PASSWORD = "offline-recovery-password-123"


class RuntimeFixture:
    def __init__(self, root):
        self.store = Store(root)
        self.auth = Auth(root / "password.json")
        self.calls = []
        for domain in (SERVER, CURRENT):
            self.store.register({"domain": domain, "name": domain, "current_ip": "8.8.8.8",
                "state_dir": "/private/installation-credential-" + domain,
                "installation_id": "private-deployment-" + domain,
                "ssh_port": 22, "transport": "raw-tls-vision-v1"})
        self.store.add_reserve(SERVER, "1.1.1.1")
        self.store.add_reserve(SERVER, "9.9.9.9")

    def check(self, server_id):
        self.calls.append(("check", server_id))

    def switch(self, server_id, ip):
        self.calls.append(("switch", server_id, ip))


class RecoveryAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = RuntimeFixture(Path(self.temporary.name).resolve() / "data")
        self.runtime.auth.set_password(PASSWORD)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime=self.runtime)),
                                        base_url="http://127.0.0.1")
        guest = (await self.client.get("/api/session")).json()
        logged_in = await self.client.post("/api/login", json={"password": PASSWORD},
                                           headers={"X-CSRF-Token": guest["csrf"]})
        self.assertEqual(logged_in.status_code, 200)
        self.headers = {"X-CSRF-Token": logged_in.json()["csrf"]}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temporary.cleanup()

    async def test_pending_target_and_safe_diagnostic_fields_are_public_without_private_paths(self):
        self.runtime.store.update(SERVER, pending_ip="1.1.1.1", last_switch_target="1.1.1.1",
            last_error="Настройки установки отличаются от журнала", last_error_code="topology_changed",
            last_error_log_id="c" * 32, executor_log_path="/private/Bearer-secret.log",
            controller_key="do-not-export-private-key")
        response = await self.client.get("/api/overview")
        self.assertEqual(response.status_code, 200)
        row = next(row for row in response.json()["servers"] if row["id"] == SERVER)
        self.assertEqual(row["pending_ip"], "1.1.1.1")
        self.assertEqual(row["last_switch_target"], "1.1.1.1")
        self.assertEqual(row["last_error_code"], "topology_changed")
        self.assertEqual(row["last_error_log_id"], "c" * 32)
        for private in ("state_dir", "installation_id", "Bearer-secret", "controller_key", "private-key"):
            self.assertNotIn(private, response.text)

    async def test_archived_pending_installation_has_readonly_history_and_keeps_state(self):
        self.runtime.store.update(SERVER, archived=True, archive_reason="Заменена новой установкой",
                                  replacement_id=CURRENT, pending_ip="1.1.1.1", auto_enabled=False)
        before = copy.deepcopy(self.runtime.store.get_server(SERVER))
        response = await self.client.get("/api/overview")
        self.assertEqual(response.status_code, 200)
        state = response.json()
        self.assertEqual([row["id"] for row in state["servers"]], [CURRENT])
        self.assertEqual([row["id"] for row in state["archived_servers"]], [SERVER])
        archived = state["archived_servers"][0]
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["replacement_id"], CURRENT)
        self.assertEqual(archived["pending_ip"], "1.1.1.1")
        self.assertEqual([row["ip"] for row in archived["reserves"]], ["1.1.1.1", "9.9.9.9"])
        self.assertTrue(any(event["server_id"] == SERVER for event in state["events"]))
        self.assertEqual(self.runtime.store.get_server(SERVER), before)
        self.assertEqual(self.runtime.calls, [])

    async def test_archived_mutations_are_rejected_before_runtime_or_store_writes(self):
        self.runtime.store.update(SERVER, archived=True, archive_reason="Прежняя установка", auto_enabled=False)
        before = copy.deepcopy(self.runtime.store.get_server(SERVER))
        prefix = "/api/servers/" + SERVER
        requests = [("POST", "/check", {}), ("POST", "/switch", {"ip": "1.1.1.1"}),
                    ("PUT", "/auto", {"enabled": True}), ("POST", "/reserves", {"ip": "1.0.0.1"}),
                    ("DELETE", "/reserves/1.1.1.1", {})]
        for method, suffix, payload in requests:
            with self.subTest(endpoint=suffix):
                response = await self.client.request(method, prefix + suffix, json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("архиве", response.text)
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.runtime.store.get_server(SERVER), before)

    async def test_pending_allows_only_same_target_and_blocks_automatic_enable(self):
        self.runtime.store.update(SERVER, pending_ip="1.1.1.1", auto_enabled=False)
        prefix = "/api/servers/" + SERVER
        response = await self.client.post(prefix + "/switch", json={"ip": "9.9.9.9"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        response = await self.client.put(prefix + "/auto", json={"enabled": True}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.runtime.calls, [])
        response = await self.client.post(prefix + "/switch", json={"ip": "1.1.1.1"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.runtime.calls, [("switch", SERVER, "1.1.1.1")])


class PublicLogReference(unittest.TestCase):
    def test_log_reference_cannot_expose_a_path_or_unbounded_log_contents(self):
        for value in ("/private/secret.log", "Bearer secret", "a" * 33, "../" + "a" * 32):
            with self.subTest(value=value):
                result = public_server({"id": SERVER, "last_error_log_id": value})
                self.assertNotIn("last_error_log_id", result)
                self.assertNotIn(value, str(result))


if __name__ == "__main__":
    unittest.main()
