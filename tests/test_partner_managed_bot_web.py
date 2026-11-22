"""Managed creation HTTP boundaries; signed offline requests, no Telegram or DB."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from partner_web import create_router
from services.partner_managed_bots import ManagedBotConflict, ManagedBotUnavailable
from services.partner_security import PartnerSettings, TokenVault
from services.partner_store import ProjectNotFound
from test_partner_web import MANAGER, TENANT, authorization, project


PID = project()["id"]
FOREIGN = "ad2c8d31-d4d2-4056-9bb2-9eab24f6d68e"
OWNER = 123
RESULT = {"creation_url": "https://t.me/newbot/ManagerBot?name=My%20VPN",
          "status": "pending", "expires_at": datetime(2026, 10, 22, 15, tzinfo=timezone.utc)}


class ManagedBotHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = project()
        self.store, self.verifier, self.entry, self.checkout = (AsyncMock() for _ in range(4))

        async def owned(pid, actor):
            if str(pid) != PID or actor != OWNER:
                raise ProjectNotFound()
            return dict(self.saved)

        self.store.get_project.side_effect = owned
        self.service = SimpleNamespace(enabled=True, available=True, request_creation=AsyncMock(return_value={
            **RESULT, "token": "never-render-private-token", "encrypted_token": "private-cipher",
            "owner_tg_id": OWNER, "project_id": FOREIGN,
        }))
        key = Fernet.generate_key().decode()
        self.settings = PartnerSettings(True, "partners.example.test", MANAGER, (key,))
        active = project(status="active", bot={"id": 200002, "username": "TenantBot"})
        self.store.get_workspace_auth_project.return_value = {
            **active, "encrypted_token": TokenVault((key,)).encrypt(PID, 200002, TENANT),
        }
        self.client = await self.client_for()

    async def client_for(self, surface="enrollment", *, service=True, allowlist=None):
        base = "/partners/workspace/{workspace_project_id}" if surface == "workspace" else "/partners"
        app = FastAPI()
        app.include_router(create_router(
            self.store, self.settings, verifier=self.verifier, surface=surface, route_base=base,
            managed_service=self.service if service else None,
            enrollment_store=self.entry, entry_checkout=self.checkout,
            addon_loader=AsyncMock(return_value={}), manager_allowed_user_ids=allowlist,
        ))
        return await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://offline.test",
            headers={"authorization": authorization(OWNER)},
        ))

    @property
    def endpoint(self): return f"/partners/api/projects/{PID}/managed-bot"

    def assert_no_financial_or_token_work(self):
        self.assertEqual(self.entry.mock_calls, [])
        self.assertEqual(self.checkout.mock_calls, [])
        self.verifier.verify.assert_not_awaited()
        self.store.save_bot_connection.assert_not_awaited()

    async def test_signed_central_owner_only_returns_public_creation_fields_no_invoice(self):
        for status in ("draft", "ready"):
            self.saved["status"] = status
            response = await self.client.post(self.endpoint, json={"expected_version": 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {**RESULT, "expires_at": RESULT["expires_at"].isoformat()})
            self.service.request_creation.assert_awaited_with(PID, OWNER, 1)
            self.assertIn("no-store", response.headers["cache-control"])
            for private in ("never-render-private-token", "private-cipher", FOREIGN, "owner_tg_id"):
                self.assertNotIn(private, response.text)
        self.assert_no_financial_or_token_work()

    async def test_authentication_precedes_body_store_and_service(self):
        for proof in ("tma invalid", authorization(OWNER, token=TENANT), ""):
            response = await self.client.post(self.endpoint, content="not-json", headers={"authorization": proof})
            self.assertEqual(response.status_code, 401)
        self.store.get_project.assert_not_awaited()
        self.service.request_creation.assert_not_awaited()
        self.assert_no_financial_or_token_work()

    async def test_allowlist_denies_signed_unapproved_actor_before_registry(self):
        client = await self.client_for(allowlist=[OWNER + 1])
        self.assertEqual((await client.post(self.endpoint, json={"expected_version": 1})).status_code, 403)
        self.store.get_project.assert_not_awaited()
        self.service.request_creation.assert_not_awaited()

    async def test_foreign_draft_and_actor_never_reach_managed_service(self):
        for endpoint, actor in ((self.endpoint.replace(PID, FOREIGN), OWNER), (self.endpoint, OWNER + 1)):
            response = await self.client.post(endpoint, json={"expected_version": 1},
                                              headers={"authorization": authorization(actor)})
            self.assertEqual(response.status_code, 404, response.text)
        self.service.request_creation.assert_not_awaited()
        self.assert_no_financial_or_token_work()

    async def test_live_project_and_stale_version_cannot_create_or_replace(self):
        for status in ("active", "suspended", "archived"):
            self.saved["status"] = status
            self.assertEqual((await self.client.post(self.endpoint, json={"expected_version": 1})).status_code, 403)
        self.saved["status"] = "draft"
        self.saved["version"] = 2
        self.assertEqual((await self.client.post(self.endpoint, json={"expected_version": 1})).status_code, 409)
        self.service.request_creation.assert_not_awaited()
        self.assert_no_financial_or_token_work()

    async def test_version_and_body_scope_are_strict_before_registry(self):
        for body in ({}, {"expected_version": True}, {"expected_version": "1"},
                     {"expected_version": 0}, {"expected_version": 1.0},
                     *({"expected_version": 1, field: "forged"} for field in
                       ("owner_tg_id", "project_id", "token", "request_key", "price", "entry_paid"))):
            response = await self.client.post(self.endpoint, json=body)
            self.assertEqual(response.status_code, 400, body)
        self.store.get_project.assert_not_awaited()
        self.service.request_creation.assert_not_awaited()

    async def test_central_token_entry_is_rejected_before_body_and_verification(self):
        response = await self.client.post(self.endpoint.removesuffix("managed-bot") + "bot", content="not-json")
        self.assertEqual(response.status_code, 403)
        self.assertIn("токен вводить не нужно", response.text)
        self.store.get_project.assert_not_awaited()
        self.assert_no_financial_or_token_work()

    async def test_disabled_and_legacy_surfaces_never_call_service(self):
        for surface, service in (("enrollment", False), ("legacy", True)):
            client = await self.client_for(surface, service=service)
            self.assertEqual((await client.post(self.endpoint, json={"expected_version": 1})).status_code, 404)
        self.service.enabled = False
        self.assertEqual((await self.client.post(self.endpoint, json={"expected_version": 1})).status_code, 404)
        self.service.request_creation.assert_not_awaited()

    async def test_workspace_has_no_managed_endpoint_even_with_tenant_owner_proof(self):
        client = await self.client_for("workspace")
        response = await client.post(f"/partners/workspace/{PID}/api/projects/{PID}/managed-bot",
                                     json={"expected_version": 1}, headers={"authorization": authorization(OWNER, token=TENANT)})
        self.assertEqual(response.status_code, 404)
        response = await client.get(f"/partners/workspace/{PID}/api/settings",
                                    headers={"authorization": authorization(OWNER, token=TENANT)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["managed_bot_creation_enabled"])
        self.assertFalse(response.json()["managed_bot_creation_available"])
        self.service.request_creation.assert_not_awaited()

    async def test_settings_managed_flags_do_not_claim_availability(self):
        for enabled, available in ((True, True), (True, False), (False, False)):
            self.service.enabled, self.service.available = enabled, available
            response = await self.client.get("/partners/api/settings")
            self.assertEqual(response.status_code, 200)
            self.assertEqual((response.json()["managed_bot_creation_enabled"],
                              response.json()["managed_bot_creation_available"]), (enabled, available))
        self.service.request_creation.assert_not_awaited()

    async def test_managed_errors_are_bounded_and_redacted(self):
        for error, code in ((ManagedBotConflict("private-secret"), 409),
                            (ProjectNotFound("private-secret"), 403),
                            (ManagedBotUnavailable("private-secret"), 503),
                            (RuntimeError("private-secret"), 503)):
            self.service.request_creation.side_effect = error
            response = await self.client.post(self.endpoint, json={"expected_version": 1})
            self.assertEqual(response.status_code, code, response.text)
            self.assertNotIn("private-secret", response.text)
        self.assert_no_financial_or_token_work()


if __name__ == "__main__":
    unittest.main()
