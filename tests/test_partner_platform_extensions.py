"""Local composition, capability branding and private error contracts. No network."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from fastapi import APIRouter, FastAPI
from cryptography.fernet import Fernet

from partner_platform_web import build_platform_extensions
from partner_subscription_web import (
    create_subscription_router,
    render_subscription_page,
)
from services.partner_store import InvalidProject, _brand
from services.partner_web_auth import PrivateAPIRoute
from services.partner_security import PartnerSettings
from partner_web import create_router
from test_partner_web import authorization, project, MANAGER, TENANT
from test_partner_subscriptions import (
    context,
    PartnerSubscriptionLinks,
    PartnerLinkSettings,
)


class ExtensionCompositionTests(unittest.TestCase):
    def runtime(self, **changes):
        values = dict(
            mode="staging",
            allow_staging_network=False,
            allow_staging_writes=False,
            management_enabled=True,
            finance_enabled=True,
            branding_enabled=True,
            enable_marketing_delivery=False,
            enable_support_delivery=False,
            enable_finance_notifications=False,
            encryption_keys=(Fernet.generate_key().decode(),),
            operator_ids=(123,),
            admin_recipient_id=123,
            manager_bot_token="123456:" + "a" * 35,
            pass_limit=5,
        )
        values.update(changes)
        return SimpleNamespace(
            settings=SimpleNamespace(**values),
            pool_getter=AsyncMock(),
            notifications=None,
            vault=object(),
            subscriptions=object(),
        )

    def test_composition_inert_and_delivery_independently_off(self):
        runtime = self.runtime()
        result = build_platform_extensions(runtime)
        self.assertEqual(
            set(result["core_kwargs"]),
            {"management_store", "finance_store", "brand_asset_store", "operator_ids"},
        )
        self.assertTrue(all(not row[2] for row in result["worker_hooks"]))
        runtime.pool_getter.assert_not_called()
        self.assertFalse(result["core_kwargs"]["management_store"].delivery_enabled)

    def test_delivery_flags_alone_do_not_override_network_or_write_permission(self):
        runtime = self.runtime(
            enable_marketing_delivery=True,
            enable_support_delivery=True,
            enable_finance_notifications=True,
        )
        self.assertTrue(
            all(
                not row[2] for row in build_platform_extensions(runtime)["worker_hooks"]
            )
        )
        runtime.pool_getter.assert_not_called()

    def test_disabled_mode_constructs_nothing(self):
        runtime = self.runtime(mode="disabled")
        self.assertEqual(build_platform_extensions(runtime), {})
        runtime.pool_getter.assert_not_called()

    def test_logo_id_is_not_an_arbitrary_url_or_html(self):
        self.assertEqual(_brand({"logo_asset_id": "a" * 64})["logo_asset_id"], "a" * 64)
        for item in [
            "../secret",
            "https://external.test/logo.png",
            '" onerror="alert(1)',
            "A" * 64,
        ]:
            with self.subTest(item=item), self.assertRaises(InvalidProject):
                _brand({"logo_asset_id": item})


class PrivateValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mounted_extensions_share_central_auth_and_trusted_actor(self):
        registry, finance, management, brand = [AsyncMock() for _ in range(4)]
        management.visible_projects.return_value = {"items": [], "truncated": False}
        registry.list_projects.return_value = [project()]
        finance.summary.return_value = {"available_kopecks": 100}
        brand.get_logo.return_value = {
            "logo_id": None,
            "png_base64": None,
            "project": project(),
        }
        settings = PartnerSettings(
            True, "partners.example.test", MANAGER, (Fernet.generate_key().decode(),)
        )
        app = FastAPI()
        app.include_router(
            create_router(
                registry,
                settings,
                management_store=management,
                finance_store=finance,
                brand_asset_store=brand,
                operator_ids=(123,),
            )
        )
        base = "/partners/api/projects/" + project()["id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://offline.test"
        ) as client:
            for endpoint in [
                base + "/finance/summary",
                base + "/logo",
                base + "/management/access",
            ]:
                response = await client.get(
                    endpoint, headers={"Authorization": authorization(token=TENANT)}
                )
                self.assertEqual(response.status_code, 401)
                self.assertIn("no-store", response.headers["cache-control"])
            brand.get_logo.assert_not_awaited()
            result = await client.get(
                base + "/logo", headers={"Authorization": authorization(user_id=456)}
            )
            self.assertEqual(result.status_code, 200)
            brand.get_logo.assert_awaited_once_with(project()["id"], 456)
            result = await client.get(
                "/partners/api/settings", headers={"Authorization": authorization()}
            )
            self.assertTrue(result.json()["is_operator"])
            management.record_manager_identity.assert_awaited_once_with(123)

    async def test_422_does_not_echo_private_input_or_allow_cache(self):
        router = APIRouter(route_class=PrivateAPIRoute)

        @router.get("/private")
        async def get_private(limit: int):
            return {"limit": limit}

        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://offline.test"
        ) as client:
            response = await client.get("/private?limit=PRIVATE-TOKEN")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("PRIVATE-TOKEN", response.text)
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_public_logo_bound_to_tenant_host_and_no_query(self):
        service = SimpleNamespace(
            enabled=True,
            links=PartnerSubscriptionLinks(
                PartnerLinkSettings("partners.example.test", b"z" * 32)
            ),
        )
        assets = SimpleNamespace(public_logo=AsyncMock(return_value=b"normalized-png"))
        app = FastAPI()
        app.include_router(
            create_subscription_router(service, brand_asset_store=assets)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://my-vpn.partners.example.test",
        ) as client:
            response = await client.get(
                "/partner-subscription-assets/logo/" + "a" * 64 + ".png"
            )
            self.assertEqual(response.status_code, 200)
            assets.public_logo.assert_awaited_once_with("my-vpn", "a" * 64)
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertEqual(
                (
                    await client.get(
                        "/partner-subscription-assets/logo/"
                        + "a" * 64
                        + ".png?project=other"
                    )
                ).status_code,
                404,
            )
            self.assertEqual(
                (
                    await client.get(
                        "/partner-subscription-assets/logo/" + "a" * 64 + ".png",
                        headers={"Host": "foreign.example.test"},
                    )
                ).status_code,
                404,
            )

    async def test_rendered_logo_uses_same_origin_asset_only(self):
        row = context(brand={"logo_asset_id": "a" * 64})
        links = PartnerSubscriptionLinks(
            PartnerLinkSettings("partners.example.test", b"z" * 32)
        )
        response = render_subscription_page(
            row,
            links,
            "ready",
            {"remaining_bytes": 100, "used_bytes": 1, "total_bytes": 101},
        )
        self.assertIn(
            ("/partner-subscription-assets/logo/" + "a" * 64 + ".png").encode(),
            response.body,
        )
        self.assertNotIn(row["subscription_url"].encode(), response.body)
        self.assertIn("img-src 'self'", response.headers["content-security-policy"])
