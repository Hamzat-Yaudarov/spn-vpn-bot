"""Payment-first capability and exact read-only database guard verification."""
from copy import deepcopy
import os
from pathlib import Path
import unittest

from services.partner_runtime import (
    OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL, PAYMENT_FIRST_ENTRY_SCHEMA_CHECK_SQL,
    PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL, build_runtime,
)
from services.partner_runtime_catalog import StagingAddonCatalog
from test_partner_managed_bot_runtime import ManagedConnection
from test_partner_owner_pilot_runtime import owner_pilot
from test_partner_public_entry_sales import public_sales
from test_partner_runtime import FakePool
from test_partner_runtime_postgres import ReadOnlyBridge


PAYMENT_FIRST_KEYS = {
    "bot_identity_nullable_safe", "launched_bot_required_safe",
    "financial_snapshot_guard_safe", "project_bot_identity_guard_safe",
}
LEGACY_KEYS = {
    "price_constraint_safe", "owner_pilot_unique_safe", "financial_snapshot_guard_safe",
}


class GuardConnection(ManagedConnection):
    def __init__(self, guard):
        super().__init__()
        self.guard = guard

    async def fetchrow(self, query):
        if query in {OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL, PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL}:
            self.calls.append(query)
            return {key: True for key in LEGACY_KEYS}
        if query == PAYMENT_FIRST_ENTRY_SCHEMA_CHECK_SQL:
            self.calls.append(query)
            return self.guard
        return await super().fetchrow(query)


class PaymentFirstRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_guard_is_required_and_unknown_or_truthy_proofs_fail_closed(self):
        valid = {key: True for key in PAYMENT_FIRST_KEYS}
        invalid = [None, {}, {**valid, "unreviewed_guard": True}]
        for key in sorted(PAYMENT_FIRST_KEYS):
            invalid.extend(({**valid, key: False}, {**valid, key: 1},
                            {name: value for name, value in valid.items() if name != key}))
        for guard in (valid, *invalid):
            with self.subTest(guard=guard):
                conn = GuardConnection(deepcopy(guard))
                runtime = build_runtime(
                    public_sales(enable_payment_first_entry=True, enable_managed_bots=True),
                    lambda: FakePool(conn), addon_loader=StagingAddonCatalog().quote,
                )
                result = await runtime.probe()
                self.assertEqual(result["database_ready"], guard is valid, result)
                self.assertEqual(conn.readonly, [True])
                self.assertIn(PAYMENT_FIRST_ENTRY_SCHEMA_CHECK_SQL, conn.calls)
                if guard is not valid:
                    self.assertEqual(result["blockers"], ["payment_first_entry_migration_required"])

    async def test_payment_first_disabled_does_not_query_optional_schema_guard(self):
        conn = GuardConnection(None)
        runtime = build_runtime(public_sales(enable_managed_bots=True), lambda: FakePool(conn),
                                addon_loader=StagingAddonCatalog().quote)
        self.assertTrue((await runtime.probe())["database_ready"])
        self.assertNotIn(PAYMENT_FIRST_ENTRY_SCHEMA_CHECK_SQL, conn.calls)


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class PaymentFirstRuntimePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await ReadOnlyBridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.root = Path(__file__).resolve().parents[1] / "ops/partner-platform"
        for name in ("commerce.sql", "enrollment.sql", "entitlements.sql", "traffic.sql",
                     "notifications.sql", "subscriptions.sql", "operations.sql",
                     "owner_entry_pilot.sql", "public_entry_trial.sql", "managed_bots.sql"):
            await self.sql.query((self.root / name).read_text(), exec=True)
        self.migration = (self.root / "payment_first_entry.sql").read_text()
        self.runtime = build_runtime(
            public_sales(enable_payment_first_entry=True, enable_managed_bots=True),
            lambda: self.sql, addon_loader=StagingAddonCatalog().quote,
        )

    async def test_exact_migration_is_required_and_both_old_and_new_guards_pass_after_it(self):
        before = await self.runtime.probe()
        self.assertFalse(before["database_ready"])
        self.assertEqual(before["blockers"], ["payment_first_entry_migration_required"])
        await self.sql.query(self.migration, exec=True)
        self.assertTrue((await self.runtime.probe())["database_ready"])
        for query in (PAYMENT_FIRST_ENTRY_SCHEMA_CHECK_SQL, PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL,
                      OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL):
            self.assertTrue(all(dict(await self.sql.fetchrow(query)).values()))
        # The closed owner-only fallback uses this new code with its old flags.
        # It must remain startable after the explicit one-way schema migration.
        fallback = build_runtime(owner_pilot(enable_managed_bots=True), lambda: self.sql,
                                 addon_loader=StagingAddonCatalog().quote)
        self.assertTrue((await fallback.probe())["database_ready"])
        for table in ("wl_projects", "wl_entry_orders", "wl_managed_bot_requests", "wl_launch_jobs"):
            self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM " + table), 0)

    async def test_disabled_weak_or_wrong_column_trigger_never_passes_exact_proof(self):
        await self.sql.query(self.migration, exec=True)
        checks = (
            ("ALTER TABLE wl_entry_orders DISABLE TRIGGER wl_entry_financial_snapshot_guard",
             "ALTER TABLE wl_entry_orders ENABLE TRIGGER wl_entry_financial_snapshot_guard"),
            ("ALTER TABLE wl_projects DISABLE TRIGGER wl_entry_bot_identity_guard",
             "ALTER TABLE wl_projects ENABLE TRIGGER wl_entry_bot_identity_guard"),
            ("CREATE OR REPLACE FUNCTION wl_guard_entry_financial_snapshot() RETURNS trigger "
             "LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ BEGIN RETURN NEW; END; $$",
             self.migration),
            ("CREATE OR REPLACE FUNCTION wl_guard_entry_bot_identity() RETURNS trigger "
             "LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ BEGIN RETURN NEW; END; $$",
             self.migration),
            ("DROP TRIGGER wl_entry_bot_identity_guard ON wl_projects; "
             "CREATE TRIGGER wl_entry_bot_identity_guard BEFORE UPDATE OF title ON wl_projects "
             "FOR EACH ROW EXECUTE FUNCTION wl_guard_entry_bot_identity()",
             "DROP TRIGGER wl_entry_bot_identity_guard ON wl_projects; "
             "CREATE TRIGGER wl_entry_bot_identity_guard BEFORE UPDATE OF bot_id ON wl_projects "
             "FOR EACH ROW EXECUTE FUNCTION wl_guard_entry_bot_identity()"),
            ("ALTER TABLE wl_entry_orders DROP CONSTRAINT wl_entry_order_launched_bot_required",
             self.migration),
        )
        for mutation, restore in checks:
            with self.subTest(mutation=mutation):
                await self.sql.query(mutation, exec=True)
                self.assertFalse((await self.runtime.probe())["database_ready"])
                await self.sql.query(restore, exec=True)
                self.assertTrue((await self.runtime.probe())["database_ready"])


if __name__ == "__main__": unittest.main()
