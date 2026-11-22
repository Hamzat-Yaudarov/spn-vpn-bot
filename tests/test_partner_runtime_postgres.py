"""Actual isolated PostgreSQL/WASM schema probe, never DATABASE_URL or network."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import unittest

from services.partner_runtime import build_runtime
from services.partner_runtime_catalog import StagingAddonCatalog
from test_partner_runtime import staging
from test_partner_store_postgres import RegistryBridge


class ReadOnlyBridge(RegistryBridge):
    @asynccontextmanager
    async def transaction(self, *, readonly=False):
        await self.execute("BEGIN READ ONLY" if readonly else "BEGIN")
        try:
            yield
        except BaseException:
            await self.execute("ROLLBACK")
            raise
        else:
            await self.execute("COMMIT")


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"),
    "Optional isolated PostgreSQL/WASM is not configured",
)
class RuntimePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await ReadOnlyBridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.catalogue = StagingAddonCatalog()

    async def test_unmigrated_database_is_reported_without_creating_tables(self):
        runtime = build_runtime(
            staging(), lambda: self.sql, addon_loader=self.catalogue.quote
        )
        before = await self.sql.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        )
        result = await runtime.probe()
        self.assertFalse(result["database_ready"])
        self.assertIn("explicit_migrations_required", result["blockers"])
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            ),
            before,
        )

    async def test_full_explicit_schema_and_extension_readonly_probe(self):
        root = Path(__file__).resolve().parents[1] / "ops" / "partner-platform"
        for name in (
            "commerce.sql",
            "enrollment.sql",
            "entitlements.sql",
            "traffic.sql",
            "notifications.sql",
            "subscriptions.sql",
            "finance.sql",
            "management.sql",
            "operations.sql",
        ):
            path = root / name
            if not path.exists() and name == "operations.sql":
                continue
            await self.sql.query(path.read_text(), exec=True)
        settings = staging(
            finance_enabled=True,
            management_enabled=True,
            allow_staging_writes=True,
            operator_ids=(90001,),
            admin_recipient_id=90001,
        )
        runtime = build_runtime(
            settings, lambda: self.sql, addon_loader=self.catalogue.quote
        )
        result = await runtime.probe()
        self.assertTrue(result["database_ready"], result)
        self.assertFalse(result["production_ready"])
        self.assertEqual(result["manual_review"]["activation_expired"], 0)
        self.assertEqual(result["queue_health"]["activation_pending"], 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 0)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_withdrawals"), 0
        )

    async def test_public_checkout_flag_requires_its_separate_explicit_migration(self):
        root = Path(__file__).resolve().parents[1] / "ops" / "partner-platform"
        for name in (
            "commerce.sql",
            "enrollment.sql",
            "entitlements.sql",
            "traffic.sql",
            "notifications.sql",
            "subscriptions.sql",
            "operations.sql",
        ):
            await self.sql.query((root / name).read_text(), exec=True)
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
        runtime = build_runtime(
            settings, lambda: self.sql, addon_loader=self.catalogue.quote
        )
        first = await runtime.probe()
        self.assertFalse(first["database_ready"])
        self.assertIn("explicit_migrations_required", first["blockers"])
        await self.sql.query((root / "public_checkout.sql").read_text(), exec=True)
        second = await runtime.probe()
        self.assertTrue(second["database_ready"], second)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_public_renewal_requests"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
