"""Explicit factual launch delivery capability; no live adapters or database."""
from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services.partner_runtime import (
    ENTRY_LAUNCH_NOTIFICATION_COLUMNS,
    ENTRY_LAUNCH_NOTIFICATIONS_SCHEMA_CHECK_SQL,
    OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL,
    PartnerRuntimeSettings, PartnerRuntimeWorker, RuntimeConfigurationError, build_runtime,
)
from services.partner_runtime_catalog import StagingAddonCatalog
from services.partner_entry_notifications import TelegramEntryLaunchNotificationAdapter
from test_partner_runtime import FakeConnection, FakePool, staging
from test_partner_owner_pilot_runtime import owner_pilot
from test_partner_runtime_postgres import ReadOnlyBridge


ROOT = Path(__file__).resolve().parents[1]


def ordinary_delivery(**changes):
    values = dict(
        enable_entry_launch_notifications=True, enable_http=True, enable_billing=True,
        enable_manager_webhook=True, manager_bot_username="ManagerBot",
        enable_launches=True, enable_customer_webhooks=True, enable_worker=True,
        allow_staging_network=True, allow_staging_writes=True,
        webhook_origin="https://manager.example.com", webhook_key_hex="b" * 64,
        invoice_intents_enabled=True, enable_provisioning=True,
        staging_no_addons_confirmed=True, yookassa_shop_id="123", yookassa_secret_key="test-secret-key",
        remnawave_api_url="https://panel.example.com/api", remnawave_api_token="panel-test-token",
        remnawave_squad_ids=("00000000-0000-4000-8000-000000000001",),
        subscription_source_origin="https://source.example.com",
    )
    values.update(changes)
    return staging(**values)


class LaunchNotificationSettingsTests(unittest.TestCase):
    def test_explicit_strict_false_default_and_disabled_examples(self):
        self.assertFalse(PartnerRuntimeSettings().enable_entry_launch_notifications)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(RuntimeConfigurationError):
                staging(enable_entry_launch_notifications=value)
        for name in ("runtime.example.json", "runtime.supabase.example.json"):
            config = json.loads((ROOT / "ops/partner-platform" / name).read_text())["runtime"]
            self.assertIs(config["enable_entry_launch_notifications"], False)

    def test_delivery_requires_all_dependencies_and_separate_write_network_consent(self):
        valid = ordinary_delivery()
        self.assertTrue(valid.preflight()["configured"], valid.preflight())
        self.assertFalse(valid.enable_owner_entry_pilot)
        for name in ("enable_billing", "enable_launches", "enable_worker", "enable_manager_webhook",
                     "manager_bot_token", "manager_bot_username"):
            with self.subTest(name=name):
                invalid = replace(valid, **{name: False if name.startswith("enable_") else ""})
                self.assertIn("entry_launch_notifications_require_billing_launch_worker_manager", invalid.preflight()["blockers"])
        for name, code in (("allow_staging_network", "staging_network_not_authorized"),
                           ("allow_staging_writes", "staging_writes_not_authorized")):
            self.assertIn(code, replace(valid, **{name: False}).preflight()["blockers"])

    def test_pilot_can_opt_in_without_opening_any_customer_delivery(self):
        settings = owner_pilot(enable_entry_launch_notifications=True)
        self.assertTrue(settings.preflight()["configured"], settings.preflight())
        for name in ("enable_notifications", "enable_marketing_delivery", "enable_support_delivery",
                     "enable_finance_notifications", "enable_provisioning", "enable_public_checkout"):
            self.assertFalse(getattr(settings, name))

    def test_composition_is_no_io_and_sender_is_dedicated_manager_adapter(self):
        pool = Mock(side_effect=AssertionError("construction must not open database"))
        with patch("socket.create_connection", side_effect=AssertionError("no network")):
            runtime = build_runtime(owner_pilot(enable_entry_launch_notifications=True), pool,
                                    addon_loader=StagingAddonCatalog().quote)
        pool.assert_not_called()
        self.assertTrue(runtime.enrollment.entry_launch_notifications_enabled)
        self.assertTrue(runtime.entry_launch_notifications.ready)
        self.assertIsInstance(runtime.entry_launch_notifications.sender, TelegramEntryLaunchNotificationAdapter)
        self.assertFalse(runtime.notifications.ready)
        self.assertIsNone(runtime.checkout.provider)
        default = build_runtime(staging(), pool, addon_loader=StagingAddonCatalog().quote)
        self.assertFalse(default.enrollment.entry_launch_notifications_enabled)
        self.assertFalse(default.entry_launch_notifications.ready)
        self.assertIsNone(default.entry_launch_notifications.sender)


class NoticeConnection(FakeConnection):
    guard = {"kind_constraint_safe": True}
    missing_notice_column = None

    async def fetch(self, query, *args):
        rows = await super().fetch(query, *args)
        rows += [{"table_name": table, "column_name": column}
                 for table, columns in ENTRY_LAUNCH_NOTIFICATION_COLUMNS.items() for column in columns]
        return [row for row in rows if (row["table_name"], row["column_name"]) != self.missing_notice_column]

    async def fetchrow(self, query):
        if query == ENTRY_LAUNCH_NOTIFICATIONS_SCHEMA_CHECK_SQL:
            self.calls.append(query)
            return self.guard
        if query == OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL:
            self.calls.append(query)
            return {key: True for key in ("price_constraint_safe", "owner_pilot_unique_safe", "financial_snapshot_guard_safe")}
        return await super().fetchrow(query)


class LaunchNotificationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_guard_is_not_queried_when_feature_is_off(self):
        connection = FakeConnection()
        runtime = build_runtime(staging(), lambda: FakePool(connection), addon_loader=StagingAddonCatalog().quote)
        self.assertTrue((await runtime.probe())["database_ready"])
        self.assertNotIn(ENTRY_LAUNCH_NOTIFICATIONS_SCHEMA_CHECK_SQL, connection.calls)

    async def test_probe_requires_exact_true_evidence_before_any_business_queue_scan(self):
        for guard in (None, {}, {"kind_constraint_safe": False}, {"kind_constraint_safe": 1},
                      {"kind_constraint_safe": "true"}, {"kind_constraint_safe": True, "unexpected": True}):
            with self.subTest(guard=guard):
                connection = NoticeConnection()
                connection.guard = guard
                runtime = build_runtime(ordinary_delivery(), lambda: FakePool(connection), addon_loader=StagingAddonCatalog().quote)
                result = await runtime.probe()
                self.assertFalse(result["database_ready"])
                self.assertEqual(result["blockers"], ["entry_launch_notifications_migration_required"])
                self.assertEqual(connection.readonly, [True])
                self.assertEqual(len(connection.calls), 2)
        connection = NoticeConnection()
        runtime = build_runtime(owner_pilot(enable_entry_launch_notifications=True), lambda: FakePool(connection), addon_loader=StagingAddonCatalog().quote)
        self.assertTrue((await runtime.probe())["database_ready"])

    async def test_every_delivery_column_is_required_without_schema_mutation(self):
        for column in ENTRY_LAUNCH_NOTIFICATION_COLUMNS["wl_partner_outbox"]:
            with self.subTest(column=column):
                connection = NoticeConnection()
                connection.missing_notice_column = ("wl_partner_outbox", column)
                runtime = build_runtime(ordinary_delivery(), lambda: FakePool(connection), addon_loader=StagingAddonCatalog().quote)
                report = await runtime.probe()
                self.assertFalse(report["database_ready"])
                self.assertEqual(report["blockers"], ["explicit_migrations_required"])
                self.assertNotIn(ENTRY_LAUNCH_NOTIFICATIONS_SCHEMA_CHECK_SQL, connection.calls)

    async def test_pilot_worker_delivers_after_entries_without_generic_commerce_or_hooks(self):
        runtime = build_runtime(owner_pilot(enable_entry_launch_notifications=True), AsyncMock(), addon_loader=StagingAddonCatalog().quote)
        runtime.probe = AsyncMock(return_value={"database_ready": True})
        order = []
        async def entries(**kwargs):
            order.append("entries")
            return []
        async def notices(**kwargs):
            order.append("notices")
            return {"sent": 1}
        runtime.enrollment.list_recoverable_entry_orders = AsyncMock(side_effect=entries)
        runtime.entry_launch_notifications.recover_once = AsyncMock(side_effect=notices)
        runtime.recovery.recover_once = AsyncMock()
        unrelated = AsyncMock()
        runtime.register_worker_hook("unrelated_delivery", unrelated, enabled=True)
        result = await PartnerRuntimeWorker(runtime).run_once()
        self.assertEqual(order, ["entries", "notices"])
        self.assertEqual(result["entry_launch_notifications"], {"sent": 1})
        self.assertEqual(result["phase_errors"], 0)
        runtime.entry_launch_notifications.recover_once.assert_awaited_once_with(limit=runtime.settings.pass_limit)
        runtime.recovery.recover_once.assert_not_awaited()
        unrelated.assert_not_awaited()
        runtime.probe.return_value = {"database_ready": False}
        await PartnerRuntimeWorker(runtime).run_once()
        self.assertEqual(runtime.entry_launch_notifications.recover_once.await_count, 1)


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class LaunchNotificationSchemaPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_exact_guard_rejects_missing_and_permissive_constraint(self):
        sql = await ReadOnlyBridge().start()
        self.addAsyncCleanup(sql.close)
        folder = ROOT / "ops/partner-platform"
        for name in ("commerce.sql", "enrollment.sql", "entitlements.sql", "traffic.sql",
                     "notifications.sql", "subscriptions.sql", "operations.sql"):
            await sql.query((folder / name).read_text(), exec=True)
        runtime = build_runtime(ordinary_delivery(), lambda: sql, addon_loader=StagingAddonCatalog().quote)
        before = await sql.fetchval("SELECT count(*) FROM wl_partner_outbox")
        missing = await runtime.probe()
        self.assertEqual(missing["blockers"], ["entry_launch_notifications_migration_required"])
        migration = (folder / "entry_launch_notifications.sql").read_text()
        await sql.query(migration, exec=True)
        self.assertTrue((await runtime.probe())["database_ready"])
        for expression in ("kind IN ('entry_paid','entry_launched','arbitrary')", "true"):
            await sql.query("ALTER TABLE wl_partner_outbox DROP CONSTRAINT wl_partner_outbox_kind_check; "
                            "ALTER TABLE wl_partner_outbox ADD CONSTRAINT wl_partner_outbox_kind_check CHECK (" + expression + ")", exec=True)
            rejected = await runtime.probe()
            self.assertEqual(rejected["blockers"], ["entry_launch_notifications_migration_required"])
        await sql.query("ALTER TABLE wl_partner_outbox DROP CONSTRAINT wl_partner_outbox_kind_check; "
                        "ALTER TABLE wl_partner_outbox ADD CONSTRAINT wl_partner_outbox_kind_check "
                        "CHECK(kind='entry_paid' OR kind='entry_launched') NOT VALID", exec=True)
        self.assertEqual((await runtime.probe())["blockers"], ["entry_launch_notifications_migration_required"])
        self.assertEqual(await sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), before)
