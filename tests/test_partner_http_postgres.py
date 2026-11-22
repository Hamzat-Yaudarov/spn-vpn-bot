"""Real HTTP -> registry -> isolated PostgreSQL tests; no production/network.

The only remote adapter is a fake read-only Telegram getMe. This is not an
end-to-end payment, bot-webhook or DNS deployment test.
"""
import os
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from partner_web import create_router
from services.partner_security import PartnerSettings, TokenVault
from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge
from test_partner_web import MANAGER, TENANT, authorization


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM module is not configured")
class PartnerHTTPPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.key = Fernet.generate_key().decode()
        self.settings = PartnerSettings(True, "partners.example.com", MANAGER, (self.key,))
        self.verifier = AsyncMock()
        self.verifier.verify.return_value = {"id": 200002, "username": "MyPartnerBot"}
        self.addons = AsyncMock(return_value={"traffic_packages": {"gb_10": {"gb": 10, "price": "19.20"}}})
        self.client = await self.client_for()

    async def client_for(self):
        app = FastAPI()
        app.include_router(create_router(PartnerStore(lambda: self.sql), self.settings,
                                         verifier=self.verifier, addon_loader=self.addons))
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="https://offline.test", headers={"Authorization": authorization()}))

    async def create(self, **changes):
        body = {"request_key": str(uuid4()), "slug": "my-brand", "title": "Мой бренд", "price": "199.00", **changes}
        response = await self.client.post("/partners/api/projects", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return body, response.json()

    async def test_saved_project_survives_http_service_restart_and_remains_owner_scoped(self):
        body, project = await self.create()
        path = f"/partners/api/projects/{project['id']}"
        changed = await self.client.patch(path, json={"expected_version": 1, "price": "299.90",
            "brand": {"accent_color": "#BBA060", "welcome_text": "Привет!"}})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["price_kopecks"], 29990)
        restarted = await self.client_for()
        retry = await restarted.post("/partners/api/projects", json=body)
        self.assertEqual(retry.json()["id"], project["id"])
        self.assertEqual(retry.json()["version"], 2)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 1)
        self.assertEqual((await restarted.patch(path, json={"expected_version": 1, "title": "Old"})).status_code, 409)
        catalog = await restarted.get("/partners/api/catalog", params={"project_id": project["id"]})
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(catalog.json()["products"][0]["price_kopecks"], 29990)
        self.assertEqual(catalog.json()["addons"]["traffic_packages"][0]["price_kopecks"], 1920)
        restarted.headers["Authorization"] = authorization(user_id=456)
        self.assertEqual((await restarted.get(path)).status_code, 404)
        self.assertEqual((await restarted.get("/partners/api/projects")).json(), {"items": []})
        self.assertEqual((await restarted.patch(path, json={"expected_version": 2, "title": "Theft"})).status_code, 404)

    async def test_bot_secret_is_durable_encrypted_and_absent_from_every_public_view(self):
        _, project = await self.create()
        path = f"/partners/api/projects/{project['id']}"
        response = await self.client.post(path + "/bot", json={"expected_version": 1, "token": TENANT})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["bot"], {"id": 200002, "username": "MyPartnerBot"})
        cipher = await self.sql.fetchval("SELECT encrypted_token FROM wl_projects WHERE id=$1", project["id"])
        self.assertNotEqual(cipher, TENANT)
        self.assertEqual(TokenVault((self.key,)).decrypt(project["id"], 200002, cipher), TENANT)
        for response in (response, await self.client.get(path), await self.client.get("/partners/api/projects")):
            self.assertNotIn(TENANT, response.text)
            self.assertNotIn(cipher, response.text)
            self.assertNotIn("encrypted_token", response.text)
        audit = str(await self.sql.fetch("SELECT details FROM wl_project_audit"))
        self.assertNotIn(TENANT, audit)
        self.assertNotIn(cipher, audit)
        self.assertEqual(await self.sql.fetchval("SELECT status FROM wl_projects WHERE id=$1", project["id"]), "draft")

    async def test_audit_failure_returns_recoverable_error_without_half_created_project(self):
        body = {"request_key": str(uuid4()), "slug": "my-brand", "title": "Brand", "price": "199"}
        self.sql.fail_audit = True
        response = await self.client.post("/partners/api/projects", json=body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 0)
        self.sql.fail_audit = False
        response = await self.client.post("/partners/api/projects", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_create_requests"), 1)
