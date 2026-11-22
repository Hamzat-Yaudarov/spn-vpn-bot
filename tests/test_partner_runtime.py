"""No real database, process daemon, Telegram, merchant or VPN writes."""

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services.partner_runtime import (
    PartnerRuntime,
    PartnerRuntimeSettings,
    REQUIRED_COLUMNS,
    RuntimeConfigurationError,
    build_runtime,
)
from services.partner_runtime_catalog import StagingAddonCatalog


KEY = base64.urlsafe_b64encode(b"v" * 32).decode()
TOKEN = "123456789:" + "a" * 30


def staging(**changes):
    values = dict(
        mode="staging",
        staging_isolation_confirmed=True,
        database_isolation_confirmed=True,
        database_dsn="postgresql://test:private-db-password@127.0.0.1/partner_test",
        database_tls=False,
        base_domain="partners.example.com",
        manager_origin="https://manager.example.com",
        manager_bot_token=TOKEN,
        encryption_keys=(KEY,),
    )
    values.update(changes)
    return PartnerRuntimeSettings(**values)


class SettingsTests(unittest.TestCase):
    def test_default_check_does_not_discover_config_or_environment(self):
        with patch.dict(
            os.environ, {"PARTNER_PLATFORM_ENABLED": "true", "DATABASE_URL": "secret"}
        ):
            settings = PartnerRuntimeSettings()
            self.assertEqual(settings.preflight()["blockers"], ["runtime_disabled"])
            self.assertFalse(settings.preflight()["production_ready"])
            runtime = build_runtime(settings)
            self.assertIsNone(runtime.commerce)

    def test_production_mode_and_implicit_truthy_flags_rejected(self):
        for values in (
            {"mode": "live"},
            {"mode": "production"},
            {"enable_worker": 1},
            {"enable_billing": "true"},
            {"interval_seconds": True},
        ):
            with self.subTest(values=values), self.assertRaises(
                RuntimeConfigurationError
            ):
                PartnerRuntimeSettings.from_mapping(values)

    def test_unknown_configuration_rejected_without_echoing_secrets(self):
        with self.assertRaises(RuntimeConfigurationError) as raised:
            PartnerRuntimeSettings.from_mapping({"token_secret_value": "never echo me"})
        self.assertNotIn("never echo me", str(raised.exception))

    def test_secrets_hidden_in_repr(self):
        config = staging(
            webhook_key_hex="b" * 64,
            subscription_key_hex="c" * 64,
            yookassa_shop_id="123",
            yookassa_secret_key="merchant-secret-value",
            remnawave_api_token="panel-secret-value",
        )
        text = repr(config)
        for secret in (
            KEY,
            TOKEN,
            config.database_dsn,
            "merchant-secret-value",
            "panel-secret-value",
            "b" * 64,
        ):
            self.assertNotIn(secret, text)

    def test_independent_keys_and_tls_required_for_remote_database(self):
        for changes in (
            {"webhook_key_hex": "b" * 64, "subscription_key_hex": "b" * 64},
            {"webhook_key_hex": (b"v" * 32).hex()},
            {"database_dsn": "postgres://user:password@db.example.com/staging"},
            {
                "database_dsn": "postgres://user:password@localhost/staging?options=unsafe"
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(
                RuntimeConfigurationError
            ):
                staging(**changes)

    def test_no_real_money_or_unsupported_scheme(self):
        for changes in (
            {"enable_billing": True, "yookassa_test_mode": False},
            {"manager_origin": "http://manager.example.com"},
            {"manager_origin": "https://secret@example.com"},
            {"base_domain": "*.example.com"},
            {"remnawave_api_url": "https://secret@panel.example.com/api"},
            {"remnawave_squad_ids": ("not-a-real-id",)},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                RuntimeConfigurationError
            ):
                staging(**changes)

    def test_preflight_reports_codes_without_network_checks_or_claims(self):
        report = staging(enable_billing=True).preflight()
        self.assertIn("staging_network_not_authorized", report["blockers"])
        self.assertIn("billing_requires_provisioning_worker", report["blockers"])
        self.assertIn("main_addon_prices_not_reviewed", report["blockers"])
        self.assertFalse(report["external_checks_performed"])
        self.assertFalse(report["production_ready"])

    def test_finance_requires_explicit_operator_recipient(self):
        self.assertIn(
            "finance_operator_missing",
            staging(finance_enabled=True).preflight()["blockers"],
        )
        with self.assertRaises(RuntimeConfigurationError):
            staging(operator_ids=(True,))
        self.assertIn(
            "finance_operator_missing",
            staging(
                finance_enabled=True, operator_ids=(1,), admin_recipient_id=2
            ).preflight()["blockers"],
        )

    def test_direct_write_capability_cannot_skip_separate_write_consent(self):
        report = staging(
            enable_provisioning=True, allow_staging_network=True
        ).preflight()
        self.assertIn("staging_writes_not_authorized", report["blockers"])

    def test_gateway_flag_requires_the_http_host_as_well_as_worker(self):
        report = staging(
            enable_public_gateway=True,
            enable_worker=True,
            enable_provisioning=True,
            allow_staging_writes=True,
            allow_staging_network=True,
            subscription_key_hex="c" * 64,
        ).preflight()
        self.assertIn("gateway_configuration_missing", report["blockers"])

    def test_separate_delivery_flags_default_off_and_require_their_stores(self):
        settings = PartnerRuntimeSettings()
        self.assertFalse(settings.enable_marketing_delivery)
        self.assertFalse(settings.enable_support_delivery)
        self.assertFalse(settings.enable_finance_notifications)
        report = staging(
            enable_marketing_delivery=True,
            enable_support_delivery=True,
            enable_finance_notifications=True,
        ).preflight()
        for code in (
            "staging_network_not_authorized",
            "staging_writes_not_authorized",
            "management_delivery_requires_store_worker",
            "finance_delivery_requires_store_worker",
        ):
            self.assertIn(code, report["blockers"])

    def test_build_is_inert_and_ignores_injected_disabled_ports(self):
        loader = StagingAddonCatalog()
        pool = AsyncMock()
        runtime = build_runtime(staging(), pool, addon_loader=loader.quote)
        self.assertIsNone(runtime.checkout.provider)
        self.assertIsNone(runtime.checkout.provisioner)
        self.assertFalse(runtime.launcher.ready)
        pool.assert_not_called()

    def test_public_checkout_default_off_and_requires_existing_gateway_payment_guards(
        self,
    ):
        self.assertFalse(PartnerRuntimeSettings().enable_public_checkout)
        self.assertIn(
            "public_checkout_requires_gateway_billing_intents",
            staging(enable_public_checkout=True).preflight()["blockers"],
        )

    def test_full_staging_composition_is_inert(self):
        loader = StagingAddonCatalog()
        pool = AsyncMock()
        settings = staging(
            allow_staging_network=True,
            allow_staging_writes=True,
            enable_http=True,
            enable_worker=True,
            enable_provisioning=True,
            enable_billing=True,
            invoice_intents_enabled=True,
            enable_manager_webhook=True,
            manager_bot_username="ManagerBot",
            enable_customer_webhooks=True,
            enable_launches=True,
            enable_public_gateway=True,
            enable_notifications=True,
            staging_no_addons_confirmed=True,
            webhook_origin="https://manager.example.com",
            webhook_key_hex="b" * 64,
            subscription_key_hex="c" * 64,
            yookassa_shop_id="123",
            yookassa_secret_key="test-secret-key",
            remnawave_api_url="https://panel.example.com/api",
            remnawave_api_token="panel-secret-value",
            remnawave_squad_ids=("00000000-0000-4000-8000-000000000001",),
            subscription_source_origin="https://source.example.com",
        )
        runtime = build_runtime(
            settings, pool, addon_loader=loader.quote, catalog_loader=loader.summary
        )
        self.assertTrue(runtime.launcher.ready)
        self.assertTrue(runtime.recovery.ready)
        self.assertTrue(runtime.subscriptions.enabled)
        self.assertTrue(runtime.notifications.ready)
        pool.assert_not_called()
        self.assertNotIn("panel-secret-value", repr(runtime))


class FakeConnection:
    def __init__(self, *, missing=False):
        self.readonly = []
        self.missing = missing
        self.calls = []

    @asynccontextmanager
    async def transaction(self, *, readonly=False):
        self.readonly.append(readonly)
        yield

    async def fetch(self, query, *args):
        self.calls.append(query)
        if self.missing:
            return []
        return [
            {"table_name": table, "column_name": column}
            for table, columns in REQUIRED_COLUMNS.items()
            for column in columns
        ]

    async def fetchrow(self, query):
        self.calls.append(query)
        if "activation_expired" in query:
            return {
                "activation_expired": 2,
                "traffic_holds": 1,
                "webhook_consent_required": 1,
            }
        return {"activation_pending": 2, "customer_delivery_uncertain": 1}


class FakePool:
    def __init__(self, connection):
        self.conn = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_is_explicit_readonly_and_reports_holds_not_heals_them(self):
        conn = FakeConnection()
        loader = StagingAddonCatalog()
        runtime = build_runtime(
            staging(), lambda: FakePool(conn), addon_loader=loader.quote
        )
        result = await runtime.probe()
        self.assertTrue(result["database_ready"])
        self.assertFalse(result["production_ready"])
        self.assertEqual(result["manual_review"]["activation_expired"], 2)
        self.assertEqual(conn.readonly, [True])
        self.assertTrue(all("SELECT" in query for query in conn.calls))

    async def test_missing_schema_fails_closed_without_migrations(self):
        conn = FakeConnection(missing=True)
        loader = StagingAddonCatalog()
        runtime = build_runtime(
            staging(), lambda: FakePool(conn), addon_loader=loader.quote
        )
        result = await runtime.probe()
        self.assertFalse(result["database_ready"])
        self.assertEqual(result["blockers"], ["explicit_migrations_required"])
        self.assertEqual(len(conn.calls), 1)

    async def test_probe_failure_redacted(self):
        runtime = PartnerRuntime(
            staging(),
            pool_getter=AsyncMock(side_effect=RuntimeError("password-secret")),
        )
        result = await runtime.probe()
        self.assertNotIn("password-secret", str(result))
        self.assertIn("staging_database_unavailable", result["blockers"])

    async def test_disabled_http_has_no_private_routes_or_database_access(self):
        pool = AsyncMock()
        runtime = build_runtime(PartnerRuntimeSettings(), pool)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runtime.app()),
            base_url="https://example.com",
        ) as client:
            response = await client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["production_ready"])
            for route in (
                "/partners",
                "/partners/api/projects",
                "/openapi.json",
                "/s/secret/config",
            ):
                self.assertEqual((await client.get(route)).status_code, 404)
        pool.assert_not_called()

    async def test_worker_defaults_inert_and_stop_before_first_pass(self):
        runtime = build_runtime(PartnerRuntimeSettings())
        stop = asyncio.Event()
        result = await runtime.worker().run(stop)
        self.assertEqual(result, {"enabled": False, "passes": 0})
        runtime = PartnerRuntime(staging(enable_worker=True, allow_staging_writes=True))
        runtime.probe = AsyncMock()
        stop.set()
        self.assertEqual((await runtime.worker().run(stop))["passes"], 0)
        runtime.probe.assert_not_awaited()

    async def test_worker_missing_database_does_not_recover_or_send(self):
        runtime = PartnerRuntime(staging(enable_worker=True, allow_staging_writes=True))
        runtime.probe = AsyncMock(return_value={"database_ready": False})
        runtime.recovery = type("Recovery", (), {"recover_once": AsyncMock()})()
        result = await runtime.worker().run_once()
        self.assertEqual(result["phase_errors"], 1)
        runtime.recovery.recover_once.assert_not_awaited()

    async def test_bounded_worker_phases_redact_errors_and_private_adapter_fields(self):
        runtime = PartnerRuntime(
            staging(
                enable_worker=True, allow_staging_writes=True, phase_timeout_seconds=1
            )
        )
        runtime.probe = AsyncMock(
            return_value={
                "database_ready": True,
                "manual_review": {"activation_expired": 1},
            }
        )
        runtime.recovery = type(
            "Recovery",
            (),
            {
                "recover_once": AsyncMock(
                    return_value={
                        "phase_errors": 1,
                        "tg_id": 12345,
                        "private_secret": "never echo",
                        "checked": 2,
                    }
                )
            },
        )()

        async def slow():
            await asyncio.sleep(10)

        runtime.register_worker_hook("slow", slow, enabled=True)
        result = await runtime.worker().run_once()
        self.assertEqual(result["phase_errors"], 2)
        self.assertEqual(result["commerce"], {"phase_errors": 1, "checked": 2})
        self.assertNotIn("tg_id", str(result))
        self.assertNotIn("never echo", str(result))
        self.assertEqual(result["slow"], {"errors": 1})

    async def test_entry_recovery_uses_existing_scoped_recovery_not_force_replace(self):
        runtime = PartnerRuntime(staging(enable_worker=True, allow_staging_writes=True))
        runtime.probe = AsyncMock(return_value={"database_ready": True})
        runtime.entry, runtime.enrollment = object(), object()
        with patch(
            "services.partner_entry_checkout.recover_partner_entries_once",
            new=AsyncMock(return_value={"checked": 1, "launched": 1}),
        ) as recover:
            result = await runtime.worker().run_once()
        recover.assert_awaited_once_with(runtime.enrollment, runtime.entry, limit=25)
        self.assertEqual(result["entries"], {"checked": 1, "launched": 1})

    async def test_cancellation_is_not_swallowed_or_reported_as_success(self):
        runtime = PartnerRuntime(staging(enable_worker=True, allow_staging_writes=True))
        runtime.probe = AsyncMock(return_value={"database_ready": True})

        async def cancel():
            raise asyncio.CancelledError()

        runtime.register_worker_hook("cancel", cancel, enabled=True)
        with self.assertRaises(asyncio.CancelledError):
            await runtime.worker().run_once()

    async def test_failure_backoff_and_signal_stop(self):
        runtime = PartnerRuntime(staging(enable_worker=True, allow_staging_writes=True))
        worker = runtime.worker()
        worker.failures = 1
        self.assertEqual(worker.delay_seconds(), 60)
        worker.failures = 7
        self.assertEqual(worker.delay_seconds(), 300)
        worker.run_once = AsyncMock(return_value={"enabled": True, "phase_errors": 1})
        stop = asyncio.Event()
        reports = []

        def report(value):
            reports.append(value)
            stop.set()

        result = await worker.run(stop, report=report)
        self.assertEqual(result["passes"], 1)
        self.assertEqual(len(reports), 1)

    async def test_extensions_constructed_once_for_http_and_worker_and_defaults_not_called(
        self,
    ):
        disabled = build_runtime(
            PartnerRuntimeSettings(), extra_router_builder=lambda _: self.fail()
        )
        disabled.app()
        disabled.worker()
        settings = staging(
            enable_http=True, allow_staging_network=True, allow_staging_writes=True
        )
        loader = StagingAddonCatalog()
        seen = []

        def extensions(runtime):
            seen.append(runtime)
            return {
                "worker_hooks": [
                    ("audit", AsyncMock(return_value={"checked": 1}), True)
                ]
            }

        runtime = build_runtime(
            settings,
            lambda: None,
            addon_loader=loader.quote,
            extra_router_builder=extensions,
        )
        runtime.app()
        runtime.worker()
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(runtime._hooks), 1)

    async def test_management_injected_into_customer_handler_and_operator_core_scope(
        self,
    ):
        from fastapi import APIRouter

        settings = staging(
            enable_http=True,
            enable_customer_webhooks=True,
            allow_staging_network=True,
            allow_staging_writes=True,
            management_enabled=True,
            manager_bot_username="ManagerBot",
            webhook_origin="https://manager.example.com",
            webhook_key_hex="b" * 64,
        )
        loader = StagingAddonCatalog()
        management = object()
        runtime = build_runtime(
            settings,
            lambda: None,
            addon_loader=loader.quote,
            extra_router_builder=lambda runtime: {
                "core_kwargs": {"management_store": management, "operator_ids": (1234,)}
            },
        )
        with patch("services.partner_bot.PartnerBotHandler") as handler, patch(
            "partner_client_web.create_client_router", return_value=APIRouter()
        ):
            runtime.app()
        self.assertIs(handler.call_args.kwargs["management"], management)
        self.assertEqual(handler.call_args.args[2], "https://t.me/ManagerBot")

    async def test_brand_store_injected_only_into_optional_gateway(self):
        from fastapi import APIRouter

        settings = staging(
            enable_http=True,
            enable_worker=True,
            enable_provisioning=True,
            allow_staging_network=True,
            allow_staging_writes=True,
            enable_public_gateway=True,
            branding_enabled=True,
            subscription_key_hex="c" * 64,
            remnawave_api_url="https://panel.example.com/api",
            remnawave_api_token="panel-secret-value",
            remnawave_squad_ids=("00000000-0000-4000-8000-000000000001",),
            subscription_source_origin="https://source.example.com",
        )
        loader = StagingAddonCatalog()
        brand = object()
        runtime = build_runtime(
            settings,
            lambda: None,
            addon_loader=loader.quote,
            extra_router_builder=lambda runtime: {
                "core_kwargs": {"brand_asset_store": brand}
            },
        )
        with patch(
            "partner_subscription_web.create_subscription_router",
            return_value=APIRouter(),
        ) as gateway:
            runtime.app()
        gateway.assert_called_once_with(
            runtime.subscriptions, brand_asset_store=brand, public_checkout=None
        )

    async def test_public_checkout_extension_is_wired_but_never_creates_invoice_on_start(
        self,
    ):
        from fastapi import APIRouter

        settings = staging(
            enable_http=True,
            enable_worker=True,
            enable_provisioning=True,
            enable_billing=True,
            invoice_intents_enabled=True,
            enable_manager_webhook=True,
            manager_bot_username="ManagerBot",
            webhook_origin="https://manager.example.com",
            webhook_key_hex="b" * 64,
            allow_staging_network=True,
            allow_staging_writes=True,
            enable_public_gateway=True,
            enable_public_checkout=True,
            staging_no_addons_confirmed=True,
            subscription_key_hex="c" * 64,
            yookassa_shop_id="123",
            yookassa_secret_key="test-secret-key",
            remnawave_api_url="https://panel.example.com/api",
            remnawave_api_token="panel-secret-value",
            remnawave_squad_ids=("00000000-0000-4000-8000-000000000001",),
            subscription_source_origin="https://source.example.com",
        )
        public = type("Public", (), {"enabled": True, "create": AsyncMock()})()
        loader = StagingAddonCatalog()
        runtime = build_runtime(
            settings,
            lambda: None,
            addon_loader=loader.quote,
            extra_router_builder=lambda runtime: {"public_checkout": public},
        )
        with patch(
            "partner_subscription_web.create_subscription_router",
            return_value=APIRouter(),
        ) as gateway:
            runtime.app()
        gateway.assert_called_once_with(runtime.subscriptions, public_checkout=public)
        public.create.assert_not_awaited()
        self.assertIs(runtime.public_checkout, public)

    async def test_actual_extension_helper_all_flags_composes_routes_and_worker_without_io(
        self,
    ):
        settings = staging(
            enable_http=True,
            enable_worker=True,
            enable_provisioning=True,
            enable_billing=True,
            invoice_intents_enabled=True,
            enable_manager_webhook=True,
            enable_customer_webhooks=True,
            enable_launches=True,
            enable_notifications=True,
            enable_marketing_delivery=True,
            enable_support_delivery=True,
            enable_finance_notifications=True,
            management_enabled=True,
            finance_enabled=True,
            branding_enabled=True,
            operator_ids=(90001,),
            admin_recipient_id=90001,
            manager_bot_username="ManagerBot",
            webhook_origin="https://manager.example.com",
            webhook_key_hex="b" * 64,
            allow_staging_network=True,
            allow_staging_writes=True,
            enable_public_gateway=True,
            enable_public_checkout=True,
            staging_no_addons_confirmed=True,
            subscription_key_hex="c" * 64,
            yookassa_shop_id="123",
            yookassa_secret_key="test-secret-key",
            remnawave_api_url="https://panel.example.com/api",
            remnawave_api_token="panel-secret-value",
            remnawave_squad_ids=("00000000-0000-4000-8000-000000000001",),
            subscription_source_origin="https://source.example.com",
        )
        pool = AsyncMock(side_effect=AssertionError("construction must not acquire DB"))
        provider = type(
            "Provider", (), {"create_invoice": AsyncMock(), "get_payment": AsyncMock()}
        )()
        provisioner = type(
            "Provisioner",
            (),
            {"ensure_subscription": AsyncMock(), "read_usage": AsyncMock()},
        )()
        telegram = type(
            "Telegram",
            (),
            {
                "ready": True,
                "get_me": AsyncMock(),
                "get_webhook_info": AsyncMock(),
                "set_webhook": AsyncMock(),
            },
        )()
        sender = type("Sender", (), {"verify": AsyncMock(), "send": AsyncMock()})()
        catalogue = StagingAddonCatalog()
        with patch(
            "asyncpg.create_pool", side_effect=AssertionError("no actual DB")
        ), patch(
            "aiohttp.ClientSession", side_effect=AssertionError("no actual network")
        ):
            runtime = build_runtime(
                settings,
                pool,
                addon_loader=catalogue.quote,
                catalog_loader=catalogue.summary,
                provider=provider,
                provisioner=provisioner,
                telegram=telegram,
                sender=sender,
            )
            app = runtime.app()
            first = runtime.extensions()
            runtime.worker()
            runtime.worker()
            self.assertIs(runtime.extensions(), first)
            def all_paths(routes):
                for route in routes:
                    if type(route).__name__ == "_IncludedRouter":
                        # New FastAPI keeps inclusion prefixes on lazy wrappers;
                        # inspect effective routes, not unprefixed originals.
                        yield from all_paths(route.effective_candidates())
                        yield from all_paths(route.effective_low_priority_routes())
                    else:
                        concrete = getattr(route, "starlette_route", None) or route
                        # Unknown objects fail here rather than hiding a route
                        # from the expected/forbidden security assertions.
                        self.assertIsInstance(concrete.path, str)
                        yield concrete.path

            paths = set(all_paths(app.routes))
            workspace = "/partners/workspace/{workspace_project_id}/api"
            expected = {
                "/partner-manager/webhook",
                "/partner-bots/{project_id}/webhook",
                "/s/{token}",
                "/s/{token}/config",
                "/s/{token}/purchase-quote",
                "/s/{token}/purchases",
                "/partners/api/projects/{project_id}/entry",
                workspace + "/projects/{project_id}/logo",
                workspace + "/projects/{project_id}/finance/summary",
                workspace + "/projects/{project_id}/management/team",
            }
            self.assertTrue(expected <= paths, expected - paths)
            self.assertTrue({
                "/partners/api/projects/{project_id}/logo",
                "/partners/api/projects/{project_id}/finance/summary",
                "/partners/api/projects/{project_id}/management/team",
                "/partners/api/operator/withdrawals",
                workspace + "/operator/withdrawals",
                workspace + "/projects/{project_id}/entry",
            }.isdisjoint(paths))
            self.assertEqual(
                {name for name, _ in runtime._hooks},
                {"marketing_delivery", "support_delivery", "finance_delivery"},
            )
            self.assertEqual(len(runtime._hooks), 3)
            self.assertTrue(runtime.public_checkout.enabled)
        pool.assert_not_called()
        provider.create_invoice.assert_not_awaited()
        provider.get_payment.assert_not_awaited()
        provisioner.ensure_subscription.assert_not_awaited()
        provisioner.read_usage.assert_not_awaited()
        telegram.get_me.assert_not_awaited()
        telegram.set_webhook.assert_not_awaited()
        sender.verify.assert_not_awaited()
        sender.send.assert_not_awaited()


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts" / "partner_runtime.py"
        spec = importlib.util.spec_from_file_location("partner_runtime_cli_test", path)
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    def private_config(self, directory, data):
        path = Path(directory) / "staging.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def test_default_cli_check_no_network_or_file_discovery(self):
        with patch.object(
            self.cli, "open_pool", side_effect=AssertionError("network")
        ), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(self.cli.main([]), 0)
            self.assertEqual(json.loads(output.getvalue())["mode"], "disabled")

    def test_explicit_default_config_is_pure_and_readable_only_by_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.private_config(directory, {"runtime": {"mode": "disabled"}})
            config, source = self.cli.load_config(path)
            self.assertEqual(config.mode, "disabled")
            path.chmod(0o644)
            with self.assertRaises(RuntimeConfigurationError):
                self.cli.load_config(path)

    def test_symlink_dotenv_unknown_fields_and_duplicate_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.private_config(directory, {"runtime": {"mode": "disabled"}})
            link = Path(directory) / "symlink.json"
            link.symlink_to(path)
            with self.assertRaises(RuntimeConfigurationError):
                self.cli.load_config(link)
            env = Path(directory) / ".env"
            env.write_text("secrets must not be read")
            with patch.object(
                self.cli.os, "open", side_effect=AssertionError("must not read")
            ):
                with self.assertRaises(RuntimeConfigurationError):
                    self.cli.load_config(env)
            for value in (
                '{"runtime":{},"runtime":{}}',
                '{"runtime":{},"extra_secret":"private"}',
            ):
                path.write_text(value)
                with self.assertRaises(RuntimeConfigurationError) as raised:
                    self.cli.load_config(path)
                self.assertNotIn("private", str(raised.exception))

    def test_probe_or_live_action_without_config_is_rejected_without_connecting(self):
        for action in ("--probe", "--serve", "--worker", "--bootstrap-manager"):
            with self.subTest(action=action), patch.object(
                self.cli, "operate", side_effect=AssertionError("network")
            ), patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(self.cli.main([action]), 2)
                self.assertEqual(
                    json.loads(output.getvalue())["error"],
                    "explicit_staging_configuration_required",
                )


if __name__ == "__main__":
    unittest.main()
