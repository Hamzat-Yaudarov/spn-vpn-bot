"""Offline central-manager access policy; tenant customer access stays separate."""

from dataclasses import FrozenInstanceError, asdict
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi import FastAPI, HTTPException, Request

from partner_client_web import webhook_secret
from partner_web import create_router
from services.partner_manager import PartnerManagerHandler, manager_webhook_secret
from services.partner_manager_access import normalize_manager_allowed_user_ids
from services.partner_runtime import (
    PartnerRuntimeSettings,
    RuntimeConfigurationError,
    build_runtime,
)
from services.partner_runtime_catalog import StagingAddonCatalog
from services.partner_security import PartnerSettings
from services.partner_web_auth import create_authenticate
from test_partner_runtime import TOKEN, staging
from test_partner_runtime_manager import KEY, ORIGIN, update
from test_partner_web import MANAGER, TENANT, authorization, project


BOT_ID = int(TOKEN.split(":", 1)[0])
PROJECT_ID = project()["id"]


class AllowlistConfigurationTests(unittest.TestCase):
    def test_none_is_legacy_access_and_explicit_empty_is_deny_all(self):
        self.assertIsNone(normalize_manager_allowed_user_ids(None))
        self.assertEqual(normalize_manager_allowed_user_ids([]), ())
        self.assertEqual(normalize_manager_allowed_user_ids(()), ())
        self.assertIsNone(PartnerRuntimeSettings().manager_allowed_user_ids)
        self.assertEqual(
            PartnerRuntimeSettings(manager_allowed_user_ids=[]).manager_allowed_user_ids,
            (),
        )

    def test_ids_are_strict_bounded_unique_and_collection_is_bounded(self):
        invalid = (
            True, 42, "42", {42}, {"42": True},
            [True], [False], [0], [-1], [2**63], [1.0], ["42"], [None],
            [42, 42], list(range(1, 102)),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_manager_allowed_user_ids(value)
                with self.assertRaises(RuntimeConfigurationError):
                    PartnerRuntimeSettings(manager_allowed_user_ids=value)
        self.assertEqual(
            normalize_manager_allowed_user_ids([1, 2**63 - 1]), (1, 2**63 - 1)
        )
        self.assertEqual(
            normalize_manager_allowed_user_ids(list(range(1, 101))),
            tuple(range(1, 101)),
        )

    def test_json_input_is_copied_to_immutable_tuple_and_roundtrips(self):
        values = json.loads('{"manager_allowed_user_ids": [42, 123]}')
        settings = PartnerRuntimeSettings.from_mapping(values)
        values["manager_allowed_user_ids"].append(999)
        self.assertEqual(settings.manager_allowed_user_ids, (42, 123))
        with self.assertRaises(FrozenInstanceError):
            settings.manager_allowed_user_ids = None
        roundtrip = PartnerRuntimeSettings.from_mapping(
            json.loads(json.dumps(asdict(settings)))
        )
        self.assertEqual(roundtrip, settings)
        for source, expected in ((None, None), ([], ())):
            self.assertEqual(
                PartnerRuntimeSettings.from_mapping(
                    json.loads(json.dumps({"manager_allowed_user_ids": source}))
                ).manager_allowed_user_ids,
                expected,
            )

    def test_allowlist_does_not_expose_existing_credentials_in_repr_or_errors(self):
        settings = staging(manager_allowed_user_ids=[42])
        self.assertIn("manager_allowed_user_ids=(42,)", repr(settings))
        self.assertNotIn(TOKEN, repr(settings))
        self.assertNotIn(settings.database_dsn, repr(settings))
        with self.assertRaises(RuntimeConfigurationError) as raised:
            PartnerRuntimeSettings.from_mapping(
                {"manager_allowed_user_ids": ["private-invalid-value"]}
            )
        self.assertNotIn("private-invalid-value", str(raised.exception))

    def test_direct_entry_points_reject_bad_policies_at_construction(self):
        for value in ("42", [True], [42, 42]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PartnerManagerHandler(
                        BOT_ID, ORIGIN, manager_allowed_user_ids=value
                    )
                with self.assertRaises(ValueError):
                    create_authenticate(
                        PartnerSettings(), manager_allowed_user_ids=value
                    )
                with self.assertRaises(ValueError):
                    create_router(
                        AsyncMock(), PartnerSettings(),
                        manager_allowed_user_ids=value,
                    )


class ManagerAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_none_keep_existing_manager_start(self):
        for kwargs in ({}, {"manager_allowed_user_ids": None}):
            handler = PartnerManagerHandler(BOT_ID, ORIGIN, **kwargs)
            result = await handler.handle(update())
            self.assertEqual(result["chat_id"], 42)
            self.assertEqual(
                result["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"],
                ORIGIN + "/partners",
            )

    async def test_allowlist_matches_verified_sender_not_start_argument(self):
        values = [42]
        handler = PartnerManagerHandler(
            BOT_ID, ORIGIN, manager_allowed_user_ids=values
        )
        values.append(123)
        self.assertIsNotNone(await handler.handle(update()))
        denied = update()
        denied["message"]["chat"]["id"] = 123
        denied["message"]["from"]["id"] = 123
        denied["message"]["text"] = "/start owner_42"
        self.assertIsNone(await handler.handle(denied))
        self.assertIsNone(await PartnerManagerHandler(
            BOT_ID, ORIGIN, manager_allowed_user_ids=[]
        ).handle(update()))

    async def test_authentication_still_precedes_allowlist_membership(self):
        settings = PartnerSettings(True, "partners.example.com", MANAGER)
        authenticate = create_authenticate(settings, manager_allowed_user_ids=[123])
        for header in (
            authorization(123, token=TENANT), authorization(True),
            authorization("123"), authorization(0), "tma invalid",
        ):
            request = Request({
                "type": "http", "headers": [(b"authorization", header.encode())]
            })
            with self.subTest(header=header), self.assertRaises(HTTPException) as raised:
                await authenticate(request)
            self.assertEqual(raised.exception.status_code, 401)


class OwnerAPIAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = AsyncMock()
        self.store.list_projects.return_value = []
        self.store.get_project.return_value = project()
        self.management = AsyncMock()
        self.management.visible_projects.return_value = {"items": []}
        self.finance = AsyncMock()
        self.finance.summary.return_value = {"balance_kopecks": 0}
        self.finance.list_operator_withdrawals.return_value = {"items": []}
        self.finance.require_operator = Mock()
        self.brand = AsyncMock()
        self.brand.get_logo.return_value = {"logo": None}
        self.verifier = AsyncMock()
        self.addons = AsyncMock(return_value={})

    async def client(self, **kwargs):
        app = FastAPI()
        app.include_router(create_router(
            self.store, PartnerSettings(True, "partners.example.com", MANAGER),
            verifier=self.verifier, addon_loader=self.addons,
            management_store=self.management, finance_store=self.finance,
            brand_asset_store=self.brand, **kwargs,
        ))
        return await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
            headers={"authorization": authorization()},
        ))

    async def test_omitted_and_none_defaults_allow_existing_signed_owner(self):
        for kwargs in ({}, {"manager_allowed_user_ids": None}):
            client = await self.client(**kwargs)
            response = await client.get("/partners/api/projects")
            self.assertEqual(response.status_code, 200)
            self.store.list_projects.assert_awaited_with(123)

    async def test_listed_owner_reaches_private_injected_extension_routes(self):
        client = await self.client(manager_allowed_user_ids=[123])
        for path in (
            "/partners/api/projects", "/partners/api/management/projects",
            f"/partners/api/projects/{PROJECT_ID}/finance/summary",
            "/partners/api/operator/withdrawals",
            f"/partners/api/projects/{PROJECT_ID}/logo",
        ):
            with self.subTest(path=path):
                self.assertEqual((await client.get(path)).status_code, 200)
        self.management.record_manager_identity.assert_awaited_once_with(123)
        self.finance.require_operator.assert_called_once_with(123)

    async def test_unlisted_owner_and_deny_all_stop_before_body_stores_and_verifier(self):
        for policy in ([42], []):
            client = await self.client(manager_allowed_user_ids=policy)
            paths = (
                "/partners/api/settings", "/partners/api/projects",
                "/partners/api/catalog", "/partners/api/management/projects",
                f"/partners/api/projects/{PROJECT_ID}/finance/summary",
                "/partners/api/operator/withdrawals",
                f"/partners/api/projects/{PROJECT_ID}/logo",
            )
            for path in paths:
                with self.subTest(policy=policy, path=path):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 403)
                    self.assertIn("no-store", response.headers["cache-control"])
                    self.assertEqual(response.json(), {"detail": "Раздел недоступен."})
            for method, path in (
                ("POST", "/partners/api/projects"),
                ("PATCH", f"/partners/api/projects/{PROJECT_ID}"),
                ("POST", f"/partners/api/projects/{PROJECT_ID}/bot"),
            ):
                response = await client.request(method, path, content="invalid-json")
                self.assertEqual(response.status_code, 403)
        for dependency in (
            self.store, self.management, self.finance, self.brand,
            self.verifier, self.addons,
        ):
            # Defaults evaluate verifier/addon truthiness at construction;
            # neither that nor policy setup is a store/external operation.
            self.assertEqual(
                [call for call in dependency.mock_calls if call[0] != "__bool__"], []
            )

    async def test_public_shell_is_not_confused_with_authorized_data(self):
        client = await self.client(manager_allowed_user_ids=[])
        self.assertEqual((await client.get("/partners")).status_code, 200)
        self.assertEqual((await client.get("/partners/api/projects")).status_code, 403)
        self.store.list_projects.assert_not_awaited()

    async def test_auth_policy_copies_input_and_cannot_be_changed_by_caller(self):
        policy = [42]
        client = await self.client(manager_allowed_user_ids=policy)
        policy.append(123)
        self.assertEqual((await client.get("/partners/api/projects")).status_code, 403)
        self.store.list_projects.assert_not_awaited()


class RuntimeAllowlistTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, policy=None, *, public=False):
        loader = StagingAddonCatalog()
        values = dict(
            enable_http=True, allow_staging_network=True, allow_staging_writes=True,
            enable_manager_webhook=True, manager_bot_username="ManagerBot",
            webhook_origin=ORIGIN, webhook_key_hex=KEY.hex(),
            manager_allowed_user_ids=policy,
        )
        if public:
            values.update(
                enable_customer_webhooks=True, enable_public_gateway=True,
                enable_provisioning=True, enable_worker=True,
                subscription_key_hex="c" * 64,
                remnawave_api_url="https://panel.example.com/api",
                remnawave_api_token="offline-panel-value",
                remnawave_squad_ids=("00000000-0000-4000-8000-000000000001",),
                subscription_source_origin="https://source.example.com",
            )
        pool = Mock(side_effect=AssertionError("no database access"))
        runtime = build_runtime(staging(**values), pool, addon_loader=loader.quote)
        runtime.registry = AsyncMock()
        runtime.registry.list_projects.return_value = []
        runtime.manager_bootstrap.install = AsyncMock(
            side_effect=AssertionError("no Telegram calls")
        )
        return runtime, pool

    async def test_runtime_wires_same_policy_to_webhook_and_signed_owner_api(self):
        for policy, allowed in ((None, True), ([42], True), ([123], False), ([], False)):
            with self.subTest(policy=policy):
                runtime, pool = self.runtime(policy)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=runtime.app()), base_url=ORIGIN,
                ) as client:
                    response = await client.post(
                        "/partner-manager/webhook", json=update(), headers={
                            "x-telegram-bot-api-secret-token": manager_webhook_secret(KEY, BOT_ID)
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    if allowed:
                        self.assertIn("reply_markup", response.json())
                    else:
                        self.assertEqual(response.json(), {"ok": True})
                    response = await client.get("/partners/api/projects", headers={
                        "authorization": authorization(42, token=TOKEN)
                    })
                    self.assertEqual(response.status_code, 200 if allowed else 403)
                if allowed:
                    runtime.registry.list_projects.assert_awaited_once_with(42)
                else:
                    runtime.registry.list_projects.assert_not_awaited()
                runtime.manager_bootstrap.install.assert_not_awaited()
                pool.assert_not_called()

    async def test_runtime_extensions_share_gate_without_operator_bypass(self):
        runtime, pool = self.runtime([42])
        tenant = project(
            owner_tg_id=42, status="active", bot={"id": 200002, "username": "TenantBot"}
        )
        runtime.registry.get_workspace_auth_project.return_value = {
            **tenant, "encrypted_token": runtime.vault.encrypt(PROJECT_ID, 200002, TENANT),
        }
        management = AsyncMock()
        management.visible_projects.return_value = {"items": []}
        finance = AsyncMock()
        finance.require_operator = Mock()
        brand = AsyncMock()
        runtime.extra_router_builder = lambda _: {"core_kwargs": {
            "management_store": management, "finance_store": finance,
            "brand_asset_store": brand, "operator_ids": (123,),
        }}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runtime.app()), base_url=ORIGIN,
        ) as client:
            for path in (
                "/partners/api/management/projects",
                f"/partners/api/projects/{PROJECT_ID}/finance/summary",
                "/partners/api/operator/withdrawals",
                f"/partners/api/projects/{PROJECT_ID}/logo",
            ):
                response = await client.get(path, headers={
                    "authorization": authorization(123, token=TOKEN)
                })
                # Central enrollment does not mount ongoing management APIs,
                # including for configured finance operator IDs.
                self.assertEqual(response.status_code, 404)
            workspace = f"/partners/workspace/{PROJECT_ID}/api"
            for suffix in (
                "/management/projects",
                f"/projects/{PROJECT_ID}/finance/summary",
                f"/projects/{PROJECT_ID}/logo",
            ):
                response = await client.get(workspace + suffix, headers={
                    "authorization": authorization(123, token=TENANT)
                })
                self.assertEqual(response.status_code, 403)
            # A listed central identity cannot sign the tenant workspace, and
            # no tenant token can reach any global finance/operator endpoint.
            response = await client.get(workspace + "/management/projects", headers={
                "authorization": authorization(42, token=TOKEN)
            })
            self.assertEqual(response.status_code, 401)
            response = await client.get(workspace + "/operator/withdrawals", headers={
                "authorization": authorization(42, token=TENANT)
            })
            self.assertEqual(response.status_code, 404)
            for store in (management, finance, brand):
                self.assertEqual(store.mock_calls, [])
            response = await client.get("/partners/api/management/projects", headers={
                "authorization": authorization(42, token=TOKEN)
            })
            self.assertEqual(response.status_code, 404)
            response = await client.get(workspace + "/management/projects", headers={
                "authorization": authorization(42, token=TENANT)
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual([row["id"] for row in response.json()["items"]], [PROJECT_ID])
            management.record_manager_identity.assert_not_awaited()
            management.visible_projects.assert_not_awaited()
            finance.require_operator.assert_not_called()
        pool.assert_not_called()

    async def test_deny_all_does_not_apply_to_tenant_customer_or_public_subscription(self):
        runtime, pool = self.runtime([], public=True)
        tenant = project(status="active", bot={"id": 200002, "username": "TenantBot"})
        runtime.registry.get_runtime_project.return_value = tenant
        runtime.registry.get_bot_update_context.return_value = tenant
        runtime.commerce = AsyncMock()
        runtime.subscriptions.config = AsyncMock(return_value=SimpleNamespace(
            body=b"offline-vpn-config", status=200, content_type="text/plain", headers={}
        ))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runtime.app()), base_url=ORIGIN,
        ) as client:
            response = await client.post(
                f"/partner-bots/{PROJECT_ID}/webhook", json=update(), headers={
                    "x-telegram-bot-api-secret-token": webhook_secret(KEY, PROJECT_ID, 200002)
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["method"], "sendMessage")
            self.assertEqual(response.json()["chat_id"], 42)
            runtime.registry.get_bot_update_context.assert_awaited_once_with(PROJECT_ID, 42)
            runtime.commerce.register_customer.assert_awaited_once_with(
                PROJECT_ID, 42, referrer_tg_id=None
            )
            response = await client.get("/s/" + "a" * 64 + "/config", headers={
                "host": "my-vpn.partners.example.com"
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"offline-vpn-config")
            runtime.subscriptions.config.assert_awaited_once()
            self.assertEqual((await client.get("/healthz")).status_code, 200)
        pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
