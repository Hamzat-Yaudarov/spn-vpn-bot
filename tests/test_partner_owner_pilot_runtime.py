"""Bounded real-money entry policy; fake transports only, no credentials or I/O."""

from copy import deepcopy
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from services.partner_checkout import CheckoutError
from services.partner_entry_pricing import ENTRY_PILOT_OWNER_ID, ENTRY_PILOT_PRICE_KOPECKS
from services.partner_runtime import (
    OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL, PartnerRuntimeWorker,
    RuntimeConfigurationError, build_runtime,
)
from services.partner_runtime_catalog import StagingAddonCatalog
from services.partner_yookassa import PartnerYooKassaProvider, PartnerYooKassaSettings
from test_partner_runtime import FakeConnection, FakePool, staging
from test_partner_runtime_postgres import ReadOnlyBridge
from test_partner_yookassa import Factory, Response, SHOP, SECRET, ORDER, PROJECT, PAYMENT, payment_body


def owner_pilot(**changes):
    values = dict(
        enable_owner_entry_pilot=True,
        enable_billing=True,
        yookassa_test_mode=False,
        manager_allowed_user_ids=(ENTRY_PILOT_OWNER_ID,),
        operator_ids=(ENTRY_PILOT_OWNER_ID,),
        admin_recipient_id=ENTRY_PILOT_OWNER_ID,
        allow_staging_network=True,
        allow_staging_writes=True,
        enable_http=True,
        enable_manager_webhook=True,
        manager_bot_username="ManagerBot",
        enable_customer_webhooks=True,
        enable_launches=True,
        enable_worker=True,
        webhook_origin="https://manager.example.com",
        webhook_key_hex="b" * 64,
        yookassa_shop_id=SHOP,
        yookassa_secret_key=SECRET,
    )
    values.update(changes)
    return staging(**values)


class OwnerPilotRuntimeTests(unittest.TestCase):
    def test_complete_owner_pilot_is_not_general_commerce_or_production(self):
        settings = owner_pilot()
        report = settings.preflight()
        self.assertTrue(report["configured"], report)
        self.assertFalse(report["production_ready"])
        self.assertFalse(report["external_checks_performed"])
        self.assertFalse(settings.invoice_intents_enabled)
        self.assertFalse(settings.enable_provisioning)
        self.assertFalse(settings.enable_public_gateway)
        self.assertFalse(settings.enable_public_checkout)

    def test_live_pilot_rejects_missing_boundaries_or_extra_capabilities(self):
        changes = (
            {"mode": "disabled"},
            {"enable_owner_entry_pilot": 1},
            {"enable_billing": False},
            {"yookassa_test_mode": True},
            {"manager_allowed_user_ids": None},
            {"manager_allowed_user_ids": ()},
            {"manager_allowed_user_ids": (ENTRY_PILOT_OWNER_ID, 42)},
            {"manager_allowed_user_ids": (42,)},
            {"operator_ids": ()},
            {"operator_ids": (ENTRY_PILOT_OWNER_ID, 42)},
            {"admin_recipient_id": 42},
            {"staging_isolation_confirmed": False},
            {"database_isolation_confirmed": False},
            {"allow_staging_network": False},
            {"allow_staging_writes": False},
            {"enable_http": False},
            {"enable_manager_webhook": False},
            {"enable_customer_webhooks": False},
            {"enable_launches": False},
            {"enable_worker": False},
            {"enable_provisioning": True},
            {"invoice_intents_enabled": True},
            {"enable_public_gateway": True},
            {"enable_public_checkout": True},
            {"enable_notifications": True},
            {"enable_marketing_delivery": True},
            {"enable_support_delivery": True},
            {"enable_finance_notifications": True},
        )
        for value in changes:
            with self.subTest(value=value), self.assertRaises(RuntimeConfigurationError):
                owner_pilot(**value)

    def test_pilot_still_needs_explicit_merchant_and_webhook_configuration(self):
        for value in ({"yookassa_secret_key": ""}, {"webhook_key_hex": ""},
                      {"webhook_origin": ""}, {"manager_bot_username": ""}):
            with self.subTest(value=value):
                self.assertFalse(owner_pilot(**value).preflight()["configured"])

    def test_injected_provider_does_not_enable_live_customer_checkout(self):
        pool, provider = AsyncMock(), AsyncMock()
        catalogue = StagingAddonCatalog()
        runtime = build_runtime(owner_pilot(), pool, addon_loader=catalogue.quote, provider=provider)
        self.assertIs(runtime.entry.provider, provider)
        self.assertIsNone(runtime.checkout.provider)
        self.assertIsNone(runtime.checkout.provisioner)
        self.assertTrue(runtime.launcher.ready)
        self.assertEqual(runtime.enrollment.entry_pricing.price_for(ENTRY_PILOT_OWNER_ID), 1000)
        self.assertEqual(runtime.enrollment.entry_pricing.price_for(42), 999000)
        self.assertIs(runtime.entry.entry_pricing, runtime.enrollment.entry_pricing)
        provider.assert_not_called()
        provider.create_invoice.assert_not_awaited()
        pool.assert_not_called()

    def test_composed_live_provider_is_entry_only_and_inert(self):
        pool = AsyncMock()
        runtime = build_runtime(owner_pilot(), pool, addon_loader=StagingAddonCatalog().quote)
        self.assertFalse(runtime.entry.provider.settings.test_mode)
        self.assertTrue(runtime.entry.provider.settings.owner_entry_pilot)
        self.assertIsNone(runtime.checkout.provider)
        self.assertFalse(runtime.commerce.entitlements_enabled)
        pool.assert_not_called()


def pilot_payment():
    body = payment_body()
    body["test"] = False
    body["amount"]["value"] = "10.00"
    body["metadata"].update(wl_kind="entry", wl_tg_id=str(ENTRY_PILOT_OWNER_ID))
    return body


class OwnerPilotWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_pilot_only_recovers_entry_orders_not_commerce_or_deliveries(self):
        runtime = build_runtime(owner_pilot(), AsyncMock(), addon_loader=StagingAddonCatalog().quote)
        runtime.probe = AsyncMock(return_value={"database_ready": True})
        runtime.enrollment.list_recoverable_entry_orders = AsyncMock(return_value=[])
        runtime.recovery.recover_once = AsyncMock()
        delivery = AsyncMock()
        runtime.register_worker_hook("unrelated_delivery", delivery, enabled=True)
        result = await PartnerRuntimeWorker(runtime).run_once()
        self.assertEqual(result["phase_errors"], 0)
        self.assertIn("entries", result)
        self.assertNotIn("commerce", result)
        self.assertNotIn("unrelated_delivery", result)
        runtime.enrollment.list_recoverable_entry_orders.assert_awaited_once()
        runtime.recovery.recover_once.assert_not_awaited()
        delivery.assert_not_awaited()


class OwnerPilotSchemaProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pilot_probe_requires_all_exact_boolean_evidence_not_a_trusted_fake_default(self):
        keys = {"price_constraint_safe", "owner_pilot_unique_safe", "financial_snapshot_guard_safe"}

        class Connection(FakeConnection):
            async def fetchrow(self, query):
                if query == OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL:
                    self.calls.append(query)
                    return self.guard
                return await super().fetchrow(query)

        for evidence in (None, {}, {k: True for k in keys - {"owner_pilot_unique_safe"}},
                         {k: k != "price_constraint_safe" for k in keys},
                         {k: k != "owner_pilot_unique_safe" for k in keys},
                         {k: k != "financial_snapshot_guard_safe" for k in keys},
                         {k: 1 for k in keys}, {k: "true" for k in keys}):
            with self.subTest(evidence=evidence):
                conn = Connection()
                conn.guard = evidence
                runtime = build_runtime(owner_pilot(), lambda: FakePool(conn), addon_loader=StagingAddonCatalog().quote)
                result = await runtime.probe()
                self.assertFalse(result["database_ready"])
                self.assertEqual(result["blockers"], ["owner_entry_pilot_migration_required"])
                self.assertEqual(conn.readonly, [True])
                self.assertEqual(len(conn.calls), 2)
        conn = Connection()
        conn.guard = {k: True for k in keys}
        runtime = build_runtime(owner_pilot(), lambda: FakePool(conn), addon_loader=StagingAddonCatalog().quote)
        self.assertTrue((await runtime.probe())["database_ready"])

    async def test_asyncpg_record_iterates_values_while_its_mapping_keys_are_valid(self):
        class RecordLike:
            def __init__(self, values):
                self.values = values

            def keys(self):
                return self.values.keys()

            def __getitem__(self, key):
                return self.values[key]

            def __iter__(self):
                # asyncpg.Record is tuple-like under direct iteration, not a dict.
                return iter(self.values.values())

            def __len__(self):
                return len(self.values)

        keys = {"price_constraint_safe", "owner_pilot_unique_safe", "financial_snapshot_guard_safe"}

        class Connection(FakeConnection):
            async def fetchrow(self, query):
                if query == OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL:
                    return RecordLike({k: k != self.failed for k in keys})
                return await super().fetchrow(query)

        for failed in (None, *sorted(keys)):
            with self.subTest(failed=failed):
                conn = Connection()
                conn.failed = failed
                runtime = build_runtime(owner_pilot(), lambda: FakePool(conn), addon_loader=StagingAddonCatalog().quote)
                result = await runtime.probe()
                self.assertEqual(result["database_ready"], failed is None, result)
                if failed:
                    self.assertIn("owner_entry_pilot_migration_required", result["blockers"])

    async def test_ordinary_runtime_does_not_demand_or_query_optional_pilot_migration(self):
        conn = FakeConnection()
        runtime = build_runtime(staging(), lambda: FakePool(conn), addon_loader=StagingAddonCatalog().quote)
        self.assertTrue((await runtime.probe())["database_ready"])
        self.assertNotIn(OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL, conn.calls)


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM is not configured")
class OwnerPilotSchemaPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_schema_guard_blocks_absent_and_weakened_migration_without_writes(self):
        sql = await ReadOnlyBridge().start()
        self.addAsyncCleanup(sql.close)
        root = Path(__file__).resolve().parents[1] / "ops" / "partner-platform"
        for name in ("commerce.sql", "enrollment.sql", "entitlements.sql", "traffic.sql",
                     "notifications.sql", "subscriptions.sql", "operations.sql"):
            await sql.query((root / name).read_text(), exec=True)
        runtime = build_runtime(owner_pilot(), lambda: sql, addon_loader=StagingAddonCatalog().quote)
        before = await sql.fetchval("SELECT count(*) FROM wl_entry_orders")
        absent = await runtime.probe()
        self.assertFalse(absent["database_ready"])
        self.assertEqual(absent["blockers"], ["owner_entry_pilot_migration_required"])
        migration = (root / "owner_entry_pilot.sql").read_text()
        await sql.query(migration, exec=True)
        self.assertTrue((await runtime.probe())["database_ready"])
        checks = (
            (
                "ALTER TABLE wl_entry_orders DISABLE TRIGGER wl_entry_financial_snapshot_guard",
                "ALTER TABLE wl_entry_orders ENABLE TRIGGER wl_entry_financial_snapshot_guard",
            ),
            (
                "CREATE OR REPLACE FUNCTION wl_guard_entry_financial_snapshot() RETURNS trigger LANGUAGE plpgsql "
                "SET search_path=pg_catalog,public AS $$ BEGIN RETURN NEW; END; $$",
                migration,
            ),
            (
                "DROP INDEX wl_entry_orders_one_open_owner_pilot; "
                "CREATE INDEX wl_entry_orders_one_open_owner_pilot ON wl_entry_orders(owner_tg_id) "
                "WHERE price_kopecks=1000 AND status<>'canceled'",
                "DROP INDEX wl_entry_orders_one_open_owner_pilot; "
                "CREATE UNIQUE INDEX wl_entry_orders_one_open_owner_pilot ON wl_entry_orders(owner_tg_id) "
                "WHERE price_kopecks=1000 AND status<>'canceled'",
            ),
            (
                "ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_orders_price_kopecks_check; "
                "ALTER TABLE wl_entry_orders ADD CONSTRAINT wl_entry_orders_price_kopecks_check "
                "CHECK(price_kopecks IN (1000,999000))",
                "ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_orders_price_kopecks_check; "
                "ALTER TABLE wl_entry_orders ADD CONSTRAINT wl_entry_orders_price_kopecks_check "
                "CHECK(price_kopecks=999000 OR (price_kopecks=1000 AND owner_tg_id=6910097562))",
            ),
            (
                "ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_orders_price_kopecks_check; "
                "ALTER TABLE wl_entry_orders ADD CONSTRAINT wl_entry_orders_price_kopecks_check "
                "CHECK(price_kopecks=999000 OR (price_kopecks=1000 AND owner_tg_id=6910097562)) NOT VALID",
                "ALTER TABLE wl_entry_orders VALIDATE CONSTRAINT wl_entry_orders_price_kopecks_check",
            ),
        )
        for mutation, restore in checks:
            with self.subTest(mutation=mutation):
                await sql.query(mutation, exec=True)
                result = await runtime.probe()
                self.assertFalse(result["database_ready"], result)
                self.assertEqual(result["blockers"], ["owner_entry_pilot_migration_required"])
                await sql.query(restore, exec=True)
                self.assertTrue((await runtime.probe())["database_ready"])
        self.assertEqual(await sql.fetchval("SELECT count(*) FROM wl_entry_orders"), before)


class OwnerPilotProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, body=None, **changes):
        factory = Factory(Response(pilot_payment() if body is None else body))
        settings = dict(test_mode=False, owner_entry_pilot=True)
        settings.update(changes)
        config = PartnerYooKassaSettings(SHOP, SECRET, "https://return.example.test/paid", **settings)
        return PartnerYooKassaProvider(config, session_factory=factory), factory

    def order(self, **changes):
        return {"id": ORDER, "project_id": PROJECT, "tg_id": ENTRY_PILOT_OWNER_ID,
                "price_kopecks": ENTRY_PILOT_PRICE_KOPECKS, "purpose": "entry", **changes}

    async def test_live_pilot_posts_only_exact_fee_owner_and_stable_key(self):
        provider, factory = self.provider()
        first = await provider.create_invoice(self.order(), idempotence_key=ORDER)
        second = await provider.create_invoice(self.order(), idempotence_key=ORDER)
        self.assertEqual(first, second)
        self.assertEqual((first.purpose, first.tg_id, first.amount_kopecks),
                         ("entry", ENTRY_PILOT_OWNER_ID, 1000))
        for method, _, options in factory.requests:
            self.assertEqual(method, "POST")
            self.assertEqual(options["headers"]["Idempotence-Key"], ORDER)
            self.assertEqual(options["json"]["amount"], {"value": "10.00", "currency": "RUB"})
            self.assertEqual(options["json"]["metadata"], {
                "wl_order_id": ORDER, "wl_project_id": PROJECT,
                "wl_tg_id": str(ENTRY_PILOT_OWNER_ID), "wl_kind": "entry",
            })

    async def test_pilot_cannot_charge_normal_entry_or_customers_or_addons(self):
        provider, factory = self.provider()
        for changes in ({"price_kopecks": 999000}, {"price_kopecks": 1001},
                        {"price_kopecks": 999}, {"price_kopecks": 1000.0},
                        {"price_kopecks": True}, {"tg_id": 42},
                        {"tg_id": str(ENTRY_PILOT_OWNER_ID)}, {"tg_id": True},
                        {"purpose": "subscription", "price_kopecks": 19900},
                        {"purpose": "devices"}, {"purpose": "traffic"}):
            with self.subTest(changes=changes), self.assertRaises(CheckoutError):
                await provider.create_invoice(self.order(**changes), idempotence_key=ORDER)
        self.assertEqual(factory.requests, [])

    async def test_pilot_canonical_get_requires_real_exact_owner_entry_and_merchant(self):
        provider, factory = self.provider()
        result = await provider.get_payment(PAYMENT)
        self.assertEqual(result.amount_kopecks, 1000)
        self.assertEqual(factory.requests[0][0], "GET")
        corruptions = (
            ("test", True), ("recipient", {"account_id": "other-shop"}),
            ("amount", {"value": "9990.00", "currency": "RUB"}),
            ("amount", {"value": "10.00", "currency": "USD"}),
            ("metadata", {"wl_order_id": ORDER, "wl_project_id": PROJECT, "wl_tg_id": "42", "wl_kind": "entry"}),
            ("metadata", {"wl_order_id": ORDER, "wl_project_id": PROJECT,
                          "wl_tg_id": str(ENTRY_PILOT_OWNER_ID), "wl_kind": "devices"}),
        )
        for key, value in corruptions:
            body = deepcopy(pilot_payment())
            body[key] = value
            with self.subTest(key=key, value=value):
                provider, _ = self.provider(body)
                with self.assertRaises(CheckoutError):
                    await provider.get_payment(PAYMENT)

    async def test_standard_test_provider_still_rejects_ten_ruble_entry(self):
        factory = Factory(Response())
        provider = PartnerYooKassaProvider(
            PartnerYooKassaSettings(SHOP, SECRET, "https://return.example.test/paid"),
            session_factory=factory,
        )
        with self.assertRaises(CheckoutError):
            await provider.create_invoice(self.order(), idempotence_key=ORDER)
        self.assertEqual(factory.requests, [])

    async def test_pilot_flag_is_explicit_boolean_and_never_testmode(self):
        for changes in ({"owner_entry_pilot": 1}, {"owner_entry_pilot": "true"},
                        {"owner_entry_pilot": True, "test_mode": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.provider(**changes)


if __name__ == "__main__":
    unittest.main()
