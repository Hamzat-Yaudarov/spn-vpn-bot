"""Offline central enrollment / tenant workspace separation and scope guards."""

import base64
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import APIRouter, FastAPI

from partner_web import create_router
from services.partner_security import PartnerSettings, TokenVault
from services.partner_management import ManagementDenied
from services.partner_runtime import build_runtime
from services.partner_runtime_catalog import StagingAddonCatalog
from test_partner_runtime import staging
from test_partner_web import MANAGER, TENANT, authorization, project


PID = project()["id"]
FOREIGN = "ad2c8d31-d4d2-4056-9bb2-9eab24f6d68e"
KEY = base64.urlsafe_b64encode(b"s" * 32).decode()
BASE = f"/partners/workspace/{PID}"


class WorkspaceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = project(status="active", bot={"id": 200002, "username": "TenantBot"})
        self.cipher = TokenVault((KEY,)).encrypt(PID, 200002, TENANT)
        self.store = AsyncMock()
        self.store.list_projects.return_value = [self.saved]
        self.store.get_project.return_value = self.saved
        self.store.get_workspace_auth_project.return_value = {**self.saved, "encrypted_token": self.cipher}
        self.management = AsyncMock()
        self.management.authorize.return_value = {
            "project_id": PID, "role": "viewer", "permissions": ["analytics", "customers", "orders", "referrals"]
        }
        self.management.overview.return_value = {"project_id": PID, "customers": 0}
        self.brand, self.finance, self.commerce = AsyncMock(), AsyncMock(), AsyncMock()
        self.finance.require_operator = Mock()
        self.commerce.overview.return_value = {"customers": 0}
        self.settings = PartnerSettings(True, "partners.example.com", MANAGER, (KEY,))
        app = FastAPI()
        kwargs = dict(
            management_store=self.management, brand_asset_store=self.brand,
            finance_store=self.finance, commerce_store=self.commerce,
            addon_loader=AsyncMock(return_value={}),
        )
        app.include_router(create_router(self.store, self.settings, surface="enrollment", **kwargs))
        app.include_router(create_router(
            self.store, self.settings, surface="workspace",
            route_base="/partners/workspace/{workspace_project_id}", **kwargs,
        ))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://manager.example.com"
        ))

    async def get(self, path, *, token=TENANT, actor=123):
        return await self.client.get(path, headers={"authorization": authorization(actor, token=token)})

    async def test_central_has_only_minimal_enrollment_data(self):
        response = await self.get("/partners/api/settings", token=MANAGER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["surface"], "enrollment")
        self.assertEqual(response.json()["features"], {"management": False, "finance": False, "branding": False})
        response = await self.get("/partners/api/projects", token=MANAGER)
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertTrue(item["workspace_available"])
        for field in ("id", "owner_tg_id", "title", "slug", "price_kopecks", "version", "status", "bot"):
            self.assertEqual(item[field], self.saved[field])
        self.assertNotIn("brand", item)
        self.assertNotIn("support_url", item)
        self.assertNotIn("permissions", item)
        self.management.visible_projects.assert_not_awaited()

    async def test_central_workspace_endpoints_are_not_mounted(self):
        for path in (
            f"/partners/api/projects/{PID}/overview",
            "/partners/api/management/projects",
            f"/partners/api/projects/{PID}/management/overview",
            f"/partners/api/projects/{PID}/logo",
            f"/partners/api/projects/{PID}/finance/summary",
            "/partners/api/operator/withdrawals",
        ):
            self.assertEqual((await self.get(path, token=MANAGER)).status_code, 404, path)
        response = await self.client.patch(f"/partners/api/projects/{PID}", json={}, headers={"authorization": authorization(token=MANAGER)})
        self.assertEqual(response.status_code, 405)
        self.store.update_project.assert_not_awaited()
        self.finance.require_operator.assert_not_called()

    async def test_central_cannot_reconfigure_an_already_active_bot(self):
        response = await self.client.post(f"/partners/api/projects/{PID}/bot", json={
            "expected_version": 1, "token": TENANT,
        }, headers={"authorization": authorization(token=MANAGER)})
        self.assertEqual(response.status_code, 403)
        self.store.save_bot_connection.assert_not_awaited()

    async def test_central_and_tenant_signatures_are_not_interchangeable(self):
        self.assertEqual((await self.get(BASE + "/api/settings", token=MANAGER)).status_code, 401)
        self.assertEqual((await self.get("/partners/api/settings", token=TENANT)).status_code, 401)

    async def test_tenant_forged_owner_identity_cannot_mutate_central_enrollment(self):
        headers = {"authorization": authorization(123, token=TENANT)}
        for path in (
            "/partners/api/projects",
            f"/partners/api/projects/{PID}/bot",
            f"/partners/api/projects/{PID}/entry",
        ):
            response = await self.client.post(path, json={}, headers=headers)
            self.assertEqual(response.status_code, 401, path)
        self.store.create_project.assert_not_awaited()
        self.store.get_project.assert_not_awaited()
        self.store.save_bot_connection.assert_not_awaited()
        self.management.record_manager_identity.assert_not_awaited()

    async def test_workspace_lists_only_authenticated_project_without_global_identity(self):
        for suffix in ("/api/settings", "/api/projects", "/api/management/projects"):
            response = await self.get(BASE + suffix)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(TENANT, response.text)
            self.assertNotIn(self.cipher, response.text)
        self.store.list_projects.assert_not_awaited()
        self.management.visible_projects.assert_not_awaited()
        self.management.record_manager_identity.assert_not_awaited()
        response = await self.get(BASE + "/api/projects")
        self.assertEqual([row["id"] for row in response.json()["items"]], [PID])

    async def test_project_path_and_query_cannot_escape_tenant_token_scope(self):
        for suffix in (
            f"/api/projects/{FOREIGN}", f"/api/projects/{FOREIGN}/management/overview",
            f"/api/projects/{FOREIGN}/logo", f"/api/projects/{FOREIGN}/finance/summary",
            f"/api/catalog?project_id={FOREIGN}",
        ):
            self.assertEqual((await self.get(BASE + suffix)).status_code, 403, suffix)
        self.store.get_project.assert_not_awaited()
        self.management.overview.assert_not_awaited()
        self.finance.summary.assert_not_awaited()
        self.brand.get_logo.assert_not_awaited()

    async def test_tenant_cannot_call_global_operator_or_central_enrollment(self):
        for suffix in ("/api/operator/withdrawals", f"/api/projects/{PID}/entry"):
            self.assertEqual((await self.get(BASE + suffix)).status_code, 404)
        response = await self.client.post(BASE + "/api/projects", json={}, headers={"authorization": authorization(token=TENANT)})
        self.assertEqual(response.status_code, 405)
        self.store.create_project.assert_not_awaited()
        self.finance.require_operator.assert_not_called()

    async def test_member_remains_member_and_cannot_mutate_owner_settings(self):
        response = await self.get(BASE + "/api/projects", actor=456)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["role"], "viewer")
        response = await self.get(BASE + f"/api/projects/{PID}/management/overview", actor=456)
        self.assertEqual(response.status_code, 200)
        self.management.overview.assert_awaited_once_with(PID, 456)
        response = await self.client.patch(BASE + f"/api/projects/{PID}", json={"expected_version": 1, "title": "forged"}, headers={"authorization": authorization(456, token=TENANT)})
        self.assertEqual(response.status_code, 403)
        self.store.update_project.assert_not_awaited()
        self.management.record_manager_identity.assert_not_awaited()
        self.management.authorize.side_effect = ManagementDenied("revoked")
        self.assertEqual((await self.get(BASE + "/api/settings", actor=456)).status_code, 403)

    async def test_tenant_members_have_no_owner_finance_or_global_operator_authority(self):
        # Even if a tenant-token holder forges an operator's numeric actor ID,
        # only this project's current member role is used by the owner gate.
        for role in ("viewer", "support", "marketer"):
            self.management.authorize.return_value = {"project_id": PID, "role": role}
            for suffix in (
                f"/api/projects/{PID}/finance/summary",
                f"/api/projects/{PID}/finance/withdrawals",
                f"/api/projects/{PID}/logo",
            ):
                response = await self.get(BASE + suffix, actor=90001)
                self.assertEqual(response.status_code, 403, (role, suffix))
        response = await self.get(BASE + "/api/operator/withdrawals", actor=90001)
        self.assertEqual(response.status_code, 404)
        self.finance.summary.assert_not_awaited()
        self.finance.list_withdrawals.assert_not_awaited()
        self.finance.require_operator.assert_not_called()
        self.brand.get_logo.assert_not_awaited()
        self.management.record_manager_identity.assert_not_awaited()


class RuntimeWorkspaceSeparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_mounts_strict_surfaces_not_legacy(self):
        loader = StagingAddonCatalog()
        runtime = build_runtime(staging(
            enable_http=True, allow_staging_network=True, allow_staging_writes=True,
            manager_allowed_user_ids=[123],
        ), lambda: None, addon_loader=loader.quote)
        runtime.registry = AsyncMock()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app()), base_url="https://manager.example.com") as client:
            # Central manager signatures cannot reach ongoing administration.
            response = await client.get(f"/partners/api/projects/{PID}/overview")
            self.assertEqual(response.status_code, 404)
            response = await client.patch(f"/partners/api/projects/{PID}", json={})
            self.assertEqual(response.status_code, 405)
            # A separately mounted tenant auth boundary exists and requires proof.
            response = await client.get(BASE + "/api/settings")
            self.assertEqual(response.status_code, 401)
        runtime.registry.get_workspace_auth_project.assert_not_awaited()


class LazyRouterCompositionTests(unittest.TestCase):
    class IncludedRouter:
        """Emulate newer FastAPI: deliberately has neither path nor methods."""
        def __init__(self, router):
            self.router = router

    def router(self, surface):
        settings = PartnerSettings(True, "partners.example.com", MANAGER, (KEY,))
        return create_router(
            AsyncMock(), settings, surface=surface,
            route_base="/partners/workspace/{workspace_project_id}" if surface == "workspace" else "/partners",
            management_store=AsyncMock(), finance_store=AsyncMock(), brand_asset_store=AsyncMock(),
        )

    def test_workspace_filters_direct_routes_before_opaque_lazy_extensions(self):
        def lazy_include(router, child, *args, **kwargs):
            router.routes.append(self.IncludedRouter(child))
        with patch.object(APIRouter, "include_router", lazy_include):
            router = self.router("workspace")
        wrappers = [route for route in router.routes if isinstance(route, self.IncludedRouter)]
        self.assertEqual(len(wrappers), 3)
        self.assertTrue(all(not hasattr(route, "path") and not hasattr(route, "methods") for route in wrappers))
        direct = [route for route in router.routes if not isinstance(route, self.IncludedRouter)]
        prefix = "/partners/workspace/{workspace_project_id}/api"
        self.assertFalse(any("/entry" in route.path for route in direct))
        self.assertFalse(any(route.path == prefix + "/projects" and "POST" in route.methods for route in direct))
        nested = [route for wrapper in wrappers for route in wrapper.router.routes]
        self.assertTrue(nested)
        self.assertTrue(all(route.path.startswith(prefix + "/") for route in nested))
        self.assertFalse(any("/operator/" in route.path for route in nested))
        self.assertTrue(any("/management/" in route.path for route in nested))
        self.assertTrue(any("/finance/" in route.path for route in nested))

    def test_enrollment_never_includes_ongoing_extension_routers(self):
        with patch.object(APIRouter, "include_router", side_effect=AssertionError("central extension forbidden")):
            router = self.router("enrollment")
        paths = {route.path for route in router.routes}
        self.assertIn("/partners/api/projects/{project_id}/entry", paths)
        self.assertTrue(paths.isdisjoint({
            "/partners/api/projects/{project_id}/overview",
            "/partners/api/projects/{project_id}/finance/summary",
            "/partners/api/projects/{project_id}/logo",
            "/partners/api/management/projects",
            "/partners/api/operator/withdrawals",
        }))


if __name__ == "__main__":
    unittest.main()
