"""Offline HTTP/security tests. Never load dotenv, send Telegram or connect DB."""
import hashlib
import hmac
import json
import time
from datetime import datetime
from urllib.parse import urlencode
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from partner_web import create_router
from services.partner_security import PartnerSettings, TokenVault, PartnerConfigurationError
from services.partner_store import ProjectConflict, ProjectNotFound


MANAGER = "100001:" + "A" * 35
TENANT = "200002:" + "B" * 35


def authorization(user_id=123, token=MANAGER, **fields):
    data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id}), **fields}
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(data)


def project(**changes):
    return {"id": "7df2b77c-fefd-4ce4-a49d-228eec3a2607", "owner_tg_id": 123,
            "title": "Мой VPN", "slug": "my-vpn", "price_kopecks": 19900,
            "version": 1, "status": "draft", "brand": {}, "support_url": "", "news_url": "",
            "bot": {"id": None, "username": None}, "created_at": datetime(2026, 10, 18), **changes}


class PartnerAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = project()
        self.store = AsyncMock()
        self.store.list_projects.return_value = [self.saved]
        self.store.get_project.return_value = self.saved
        self.store.create_project.return_value = self.saved
        self.store.update_project.return_value = project(version=2)
        self.store.save_bot_connection.return_value = project(version=2, bot={"id": 200002, "username": "owned_bot"})
        self.verifier = AsyncMock()
        self.verifier.verify.return_value = {"id": 200002, "username": "owned_bot"}
        self.key = Fernet.generate_key().decode()
        self.settings = PartnerSettings(True, "partners.example.com", MANAGER, (self.key,))
        self.addons = AsyncMock(return_value={})
        self.client = await self.make_client(self.settings)

    async def make_client(self, settings):
        app = FastAPI()
        app.include_router(create_router(self.store, settings, verifier=self.verifier, addon_loader=self.addons))
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="https://offline.test", headers={"Authorization": authorization()}))

    @property
    def path(self):
        return f"/partners/api/projects/{self.saved['id']}"

    async def test_disabled_feature_never_reads_registry(self):
        client = await self.make_client(PartnerSettings())
        for path in ("/partners", "/partners/api/settings", "/partners/api/projects", "/partners/api/catalog"):
            self.assertEqual((await client.get(path)).status_code, 404)
        self.store.list_projects.assert_not_awaited()
        self.addons.assert_not_awaited()

    async def test_bad_configuration_fails_closed(self):
        client = await self.make_client(PartnerSettings(True, "https://wrong.example", MANAGER))
        response = await client.get("/partners/api/settings")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(MANAGER, response.text)

    async def test_setup_page_is_not_cached_and_has_restrictive_security_headers(self):
        response = await self.client.get("/partners")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("object-src 'none'", response.headers["content-security-policy"])
        self.assertNotIn("unsafe-eval", response.headers["content-security-policy"])
        self.assertNotIn(MANAGER, response.text)

    async def test_auth_precedes_body_parsing_and_database(self):
        self.client.headers["Authorization"] = "tma invalid"
        for method, path in (("GET", "/partners/api/projects"), ("POST", "/partners/api/projects"),
                             ("PATCH", self.path), ("POST", self.path + "/bot"), ("GET", "/partners/api/catalog")):
            response = await self.client.request(method, path, content="invalid-json")
            self.assertEqual(response.status_code, 401)
        self.store.create_project.assert_not_awaited()
        self.verifier.verify.assert_not_awaited()

    async def test_partner_token_cannot_forge_platform_owner_auth(self):
        self.client.headers["Authorization"] = authorization(token=TENANT)
        self.assertEqual((await self.client.get("/partners/api/projects")).status_code, 401)
        self.store.list_projects.assert_not_awaited()

    async def test_auth_rejects_non_integer_owner_and_duplicate_fields(self):
        for uid in (True, -1, "123", 2**63):
            self.client.headers["Authorization"] = authorization(user_id=uid)
            self.assertEqual((await self.client.get("/partners/api/projects")).status_code, 401)
        self.client.headers["Authorization"] = authorization() + "&auth_date=1"
        self.assertEqual((await self.client.get("/partners/api/projects")).status_code, 401)

    async def test_owner_passed_only_from_verified_management_bot(self):
        response = await self.client.get("/partners/api/projects")
        self.assertEqual(response.status_code, 200)
        self.store.list_projects.assert_awaited_once_with(123)
        self.assertEqual(response.json()["items"][0]["hostname"], "my-vpn.partners.example.com")
        self.assertFalse(response.json()["items"][0]["sales_enabled"])
        self.assertEqual(response.json()["items"][0]["created_at"], "2026-10-18T00:00:00")

    async def test_create_minimum_and_higher_price_preserve_request_key(self):
        key = str(uuid4())
        for price, cents in (("199", 19900), ("349.50", 34950)):
            response = await self.client.post("/partners/api/projects", json={
                "request_key": key, "slug": "my-vpn", "title": "Мой VPN", "price": price})
            self.assertEqual(response.status_code, 201)
            self.store.create_project.assert_awaited_with(123, key, "my-vpn", "Мой VPN", cents)

    async def test_create_rejects_price_and_untrusted_extra_fields(self):
        base = {"request_key": str(uuid4()), "slug": "my-vpn", "title": "Мой VPN"}
        for price in ("198.99", 199, True, "NaN", "199.001", "1e4"):
            response = await self.client.post("/partners/api/projects", json={**base, "price": price})
            self.assertEqual(response.status_code, 400)
        for field in ("owner_id", "tenant_id", "entry_paid", "status", "base_traffic_gb", "device_limit"):
            response = await self.client.post("/partners/api/projects", json={**base, field: 1000})
            self.assertEqual(response.status_code, 400)
        self.store.create_project.assert_not_awaited()

    async def test_patch_accepts_only_versioned_brand_and_price_fields(self):
        response = await self.client.patch(self.path, json={"expected_version": 1,
            "price": "299.90", "brand": {"accent_color": "#bba060"}})
        self.assertEqual(response.status_code, 200)
        self.store.update_project.assert_awaited_once_with(self.saved["id"], 123, 1,
            {"price_kopecks": 29990, "brand": {"accent_color": "#bba060"}})
        for field in ("status", "slug", "entry_paid", "balance", "days", "device_limit", "encrypted_token"):
            response = await self.client.patch(self.path, json={"expected_version": 1, field: "secret"})
            self.assertEqual(response.status_code, 400)

    async def test_foreign_project_is_404_for_read_update_connect_and_catalog(self):
        self.store.get_project.side_effect = ProjectNotFound()
        self.store.update_project.side_effect = ProjectNotFound()
        responses = [await self.client.get(self.path),
                     await self.client.patch(self.path, json={"expected_version": 1, "title": "Other"}),
                     await self.client.post(self.path + "/bot", json={"expected_version": 1, "token": TENANT}),
                     await self.client.get("/partners/api/catalog", params={"project_id": self.saved["id"]})]
        self.assertTrue(all(response.status_code == 404 for response in responses))
        self.verifier.verify.assert_not_awaited()
        self.addons.assert_not_awaited()

    async def test_version_conflict_and_database_error_do_not_report_success(self):
        self.store.update_project.side_effect = ProjectConflict()
        response = await self.client.patch(self.path, json={"expected_version": 1, "title": "Brand"})
        self.assertEqual(response.status_code, 409)
        self.store.list_projects.side_effect = RuntimeError("SQL DATABASE_URL token-private")
        response = await self.client.get("/partners/api/projects")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("token-private", response.text)
        self.assertNotIn("items", response.json())

    async def test_token_verified_then_context_encrypted_never_echoed(self):
        response = await self.client.post(self.path + "/bot", json={"expected_version": 1, "token": TENANT})
        self.assertEqual(response.status_code, 200)
        args = self.store.save_bot_connection.await_args.args
        self.assertEqual(args[:5], (self.saved["id"], 123, 1, 200002, "owned_bot"))
        self.assertNotEqual(args[5], TENANT)
        self.assertEqual(TokenVault((self.key,)).decrypt(self.saved["id"], 200002, args[5]), TENANT)
        self.assertNotIn(TENANT, response.text)
        self.assertNotIn(args[5], response.text)

    async def test_token_rejections_do_not_leak_or_call_telegram(self):
        for token in (MANAGER, "url:https://private", "200002:short", None):
            response = await self.client.post(self.path + "/bot", json={"expected_version": 1, "token": token})
            self.assertEqual(response.status_code, 400)
            if token:
                self.assertNotIn(token, response.text)
        self.verifier.verify.assert_not_awaited()
        self.store.save_bot_connection.assert_not_awaited()

    async def test_missing_key_and_stale_version_precede_external_verification(self):
        client = await self.make_client(PartnerSettings(True, "partners.example.com", MANAGER))
        response = await client.post(self.path + "/bot", json={"expected_version": 1, "token": TENANT})
        self.assertEqual(response.status_code, 503)
        response = await self.client.post(self.path + "/bot", json={"expected_version": 2, "token": TENANT})
        self.assertEqual(response.status_code, 409)
        self.verifier.verify.assert_not_awaited()

    async def test_network_error_never_exposes_token(self):
        self.verifier.verify.side_effect = RuntimeError(f"GET /bot{TENANT}/getMe failed")
        response = await self.client.post(self.path + "/bot", json={"expected_version": 1, "token": TENANT})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(TENANT, response.text)
        self.store.save_bot_connection.assert_not_awaited()

    async def test_catalog_uses_project_price_no_payments_or_fake_devices(self):
        self.store.get_project.return_value = project(price_kopecks=34900)
        response = await self.client.get("/partners/api/catalog", params={"project_id": self.saved["id"]})
        self.assertEqual(response.status_code, 200)
        product = response.json()["products"][0]
        self.assertEqual((product["price_kopecks"], product["base_traffic_gb"], product["base_device_limit"]), (34900, 300, 3))
        self.assertEqual(response.json()["addons"]["devices"]["addon_status"], "not_loaded")
        for path in ("/partners/api/payments", self.path + "/activate", self.path + "/give-subscription"):
            self.assertEqual((await self.client.post(path, json={})).status_code, 404)

    async def test_invalid_large_body_never_reaches_registry(self):
        response = await self.client.post("/partners/api/projects", content="x" * 17000,
                                          headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.store.create_project.assert_not_awaited()


class PartnerVaultTests(unittest.TestCase):
    def test_project_binding_tampering_and_key_rotation(self):
        first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        project_id, other = str(uuid4()), str(uuid4())
        cipher = TokenVault((first,)).encrypt(project_id, 200002, TENANT)
        rotated = TokenVault((second, first))
        self.assertEqual(rotated.decrypt(project_id, 200002, cipher), TENANT)
        for identity, bot_id, value in ((other, 200002, cipher), (project_id, 300003, cipher),
                                         (project_id, 200002, cipher[:-6] + "broken")):
            with self.assertRaises(PartnerConfigurationError):
                rotated.decrypt(identity, bot_id, value)

    def test_settings_repr_does_not_reveal_credentials(self):
        key = Fernet.generate_key().decode()
        value = repr(PartnerSettings(True, "example.com", MANAGER, (key,)))
        self.assertNotIn(MANAGER, value)
        self.assertNotIn(key, value)

    def test_vault_fails_closed_without_key(self):
        for keys in ((), ("bad-key",)):
            with self.assertRaises(PartnerConfigurationError):
                TokenVault(keys)
