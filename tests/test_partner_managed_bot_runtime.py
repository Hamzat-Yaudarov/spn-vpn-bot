"""Managed-bot runtime wiring is opt-in, no Telegram or remote database."""
from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services.partner_runtime import (
    MANAGED_BOT_REQUIRED_COLUMNS, PartnerRuntimeSettings,
    RuntimeConfigurationError, build_runtime,
)
from services.partner_runtime_catalog import StagingAddonCatalog
from test_partner_runtime import FakeConnection, FakePool, staging
from test_partner_owner_pilot_runtime import owner_pilot
from test_partner_runtime_postgres import ReadOnlyBridge


ROOT = Path(__file__).resolve().parents[1]


def managed_settings(**changes):
    values = dict(
        enable_managed_bots=True, enable_http=True, enable_manager_webhook=True,
        manager_bot_username="ManagerBot", webhook_origin="https://manager.example.com",
        webhook_key_hex="b" * 64, allow_staging_network=True, allow_staging_writes=True,
        manager_allowed_user_ids=(42,),
    )
    values.update(changes)
    return staging(**values)


class ManagedBotSettingsTests(unittest.TestCase):
    def test_flag_is_strict_false_by_default_including_examples(self):
        self.assertIs(PartnerRuntimeSettings().enable_managed_bots, False)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(RuntimeConfigurationError):
                staging(enable_managed_bots=value)
        for name in ("runtime.example.json", "runtime.supabase.example.json"):
            self.assertIs(json.loads((ROOT / "ops/partner-platform" / name).read_text())["runtime"]["enable_managed_bots"], False)

    def test_requires_explicit_http_manager_vault_and_io_consent_but_not_billing(self):
        valid = managed_settings()
        self.assertTrue(valid.preflight()["configured"], valid.preflight())
        self.assertFalse(valid.enable_billing)
        for name, absent in (("enable_http", False), ("enable_manager_webhook", False),
                             ("manager_bot_username", ""), ("manager_bot_token", ""),
                             ("encryption_keys", ())):
            with self.subTest(name=name):
                report = replace(valid, **{name: absent}).preflight()
                self.assertIn("managed_bots_require_http_manager_vault", report["blockers"])
        for name, code in (("allow_staging_network", "staging_network_not_authorized"),
                           ("allow_staging_writes", "staging_writes_not_authorized")):
            self.assertIn(code, replace(valid, **{name: False}).preflight()["blockers"])
        self.assertTrue(owner_pilot(enable_managed_bots=True).preflight()["configured"])

    def test_composition_no_io_independent_telegram_and_disabled_legacy_username(self):
        pool = Mock(side_effect=AssertionError("no pool on construction"))
        managed_telegram = Mock()
        with patch("socket.create_connection", side_effect=AssertionError("no network")):
            runtime = build_runtime(managed_settings(), pool, addon_loader=StagingAddonCatalog().quote,
                                    managed_bot_telegram=managed_telegram)
            legacy = build_runtime(staging(), pool, addon_loader=StagingAddonCatalog().quote)
        self.assertTrue(runtime.managed_bots.available)
        self.assertTrue(runtime.manager_bootstrap.managed_bots_enabled)
        self.assertFalse(legacy.managed_bots.available)
        self.assertFalse(legacy.manager_bootstrap.managed_bots_enabled)
        pool.assert_not_called()
        self.assertEqual(managed_telegram.mock_calls, [])


class ManagedConnection(FakeConnection):
    missing_column = None

    async def fetch(self, query, *args):
        self.requested_tables = args[0]
        return await super().fetch(query, *args) + [
            {"table_name": "wl_managed_bot_requests", "column_name": column}
            for column in MANAGED_BOT_REQUIRED_COLUMNS if column != self.missing_column
        ]


class ManagedBotRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_table_not_required_or_requested_with_feature_off(self):
        connection = ManagedConnection()
        runtime = build_runtime(staging(), lambda: FakePool(connection), addon_loader=StagingAddonCatalog().quote)
        self.assertTrue((await runtime.probe())["database_ready"])
        self.assertNotIn("wl_managed_bot_requests", connection.requested_tables)

    async def test_missing_table_column_blocks_before_queue_scan_in_readonly_probe(self):
        for column in sorted(MANAGED_BOT_REQUIRED_COLUMNS):
            with self.subTest(column=column):
                connection = ManagedConnection()
                connection.missing_column = column
                runtime = build_runtime(managed_settings(), lambda: FakePool(connection), addon_loader=StagingAddonCatalog().quote)
                report = await runtime.probe()
                self.assertFalse(report["database_ready"])
                self.assertEqual(report["blockers"], ["explicit_migrations_required"])
                self.assertEqual(connection.readonly, [True])
                self.assertEqual(len(connection.calls), 1)
        connection = ManagedConnection()
        runtime = build_runtime(managed_settings(), lambda: FakePool(connection), addon_loader=StagingAddonCatalog().quote)
        self.assertTrue((await runtime.probe())["database_ready"])

    async def test_http_routes_and_manager_handler_share_same_server_owned_service_without_bootstrap(self):
        from partner_web import create_router
        from services.partner_manager import PartnerManagerHandler

        runtime = build_runtime(managed_settings(), lambda: None, addon_loader=StagingAddonCatalog().quote)
        runtime.manager_bootstrap.install = AsyncMock(side_effect=AssertionError("no install on app construction"))
        with (
            patch("partner_web.create_router", wraps=create_router) as core,
            patch("services.partner_manager.PartnerManagerHandler", wraps=PartnerManagerHandler) as manager,
        ):
            runtime.app()
        self.assertEqual(core.call_count, 2)
        for call in core.call_args_list:
            self.assertIs(call.kwargs["managed_service"], runtime.managed_bots)
        self.assertIs(manager.call_args.kwargs["managed_service"], runtime.managed_bots)
        runtime.manager_bootstrap.install.assert_not_awaited()


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class ManagedBotRuntimeSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_migration_is_required_but_probe_does_not_create_or_enqueue(self):
        sql = await ReadOnlyBridge().start()
        self.addAsyncCleanup(sql.close)
        folder = ROOT / "ops/partner-platform"
        for name in ("commerce.sql", "enrollment.sql", "entitlements.sql", "traffic.sql",
                     "notifications.sql", "subscriptions.sql", "operations.sql"):
            await sql.query((folder / name).read_text(), exec=True)
        runtime = build_runtime(managed_settings(), lambda: sql, addon_loader=StagingAddonCatalog().quote)
        self.assertEqual((await runtime.probe())["blockers"], ["explicit_migrations_required"])
        self.assertIsNone(await sql.fetchval("SELECT to_regclass('public.wl_managed_bot_requests')"))
        await sql.query((folder / "managed_bots.sql").read_text(), exec=True)
        self.assertTrue((await runtime.probe())["database_ready"])
        self.assertEqual(await sql.fetchval("SELECT count(*) FROM wl_managed_bot_requests"), 0)
