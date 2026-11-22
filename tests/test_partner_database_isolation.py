"""Explicit partner database boundary, no main dotenv or real connection."""

import ast
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services.partner_runtime import (
    MAIN_SPN_SIGNATURES,
    PartnerRuntime,
    PartnerRuntimeSettings,
    RuntimeConfigurationError,
    build_runtime,
)
from test_partner_runtime import FakeConnection, FakePool, staging


ROOT = Path(__file__).resolve().parents[1]
REF = "abcdefghijklmnopqrst"


def supabase(**changes):
    values = dict(
        database_dsn=f"postgresql://spn_partner_runtime:private@db.{REF}.supabase.co:5432/postgres",
        database_tls=True,
        database_project_ref=REF,
        database_api_disabled_confirmed=True,
    )
    values.update(changes)
    return staging(**values)


class DatabaseSettingsTests(unittest.TestCase):
    def test_separate_database_requires_new_explicit_confirmation(self):
        config = replace(staging(), database_isolation_confirmed=False)
        self.assertIn(
            "separate_partner_database_not_confirmed", config.preflight()["blockers"]
        )
        with self.assertRaises(RuntimeConfigurationError):
            build_runtime(config, lambda: self.fail("no shared DB fallback"))
        self.assertFalse(PartnerRuntimeSettings().database_isolation_confirmed)
        self.assertIn(
            "supabase_data_api_disabled_not_confirmed",
            supabase(database_api_disabled_confirmed=False).preflight()["blockers"],
        )

    def test_supabase_project_binds_to_direct_host_or_session_username(self):
        self.assertTrue(supabase().preflight()["configured"])
        self.assertTrue(
            supabase(
                database_connection_mode="session",
                database_dsn=f"postgresql://spn_partner_runtime.{REF}:private@aws-0-region.pooler.supabase.com:5432/postgres",
            ).preflight()["configured"]
        )
        for changes in (
            {"database_project_ref": ""},
            {"database_project_ref": "b" * 20},
            {"database_connection_mode": "transaction"},
            {"database_connection_mode": "session"},
            {
                "database_dsn": f"postgres://spn_partner_runtime:private@db.{REF}.supabase.co:6543/postgres"
            },
            {
                "database_dsn": f"postgres://spn_partner_runtime.{REF}:private@aws-0-region.pooler.supabase.com:6543/postgres",
                "database_connection_mode": "session",
            },
            {
                "database_dsn": f"postgres://spn_partner_runtime.{REF}:private@aws-0-region.pooler.supabase.com:5432/postgres"
            },
            {
                "database_dsn": "postgres://spn_partner_runtime.foreign:private@aws-0-region.pooler.supabase.com:5432/postgres",
                "database_connection_mode": "session",
            },
            {
                "database_dsn": "postgres://spn_partner_runtime:private@different.example.com/postgres"
            },
            {"database_tls": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                RuntimeConfigurationError
            ) as raised:
                supabase(**changes)
            self.assertNotIn("private", str(raised.exception))

    def test_ca_path_explicit_and_must_not_point_at_dotenv(self):
        for path in (
            "relative.pem",
            "/etc/spn/.env",
            "/etc/spn/.env.backup",
            "/etc/spn/../key.pem",
            "/etc/spn/key\n.pem",
        ):
            with self.subTest(path=path), self.assertRaises(RuntimeConfigurationError):
                supabase(database_ca_file=path)
        self.assertEqual(
            supabase(database_ca_file="/etc/spn-partner/root.crt").database_ca_file,
            "/etc/spn-partner/root.crt",
        )

    def test_main_webhooks_no_longer_mount_any_partner_pool_or_settings(self):
        tree = ast.parse((ROOT / "webhooks.py").read_text())
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(
            any(
                (node.module or "").startswith(("partner_", "services.partner_"))
                for node in imports
            )
        )
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertTrue(
            {
                "admin_router",
                "customer_router",
                "renewal_router",
                "mobile_router",
                "mobile_public_router",
            }
            <= names
        )
        self.assertTrue(
            {
                "create_miniapp_referral_router",
                "process_paid_payment",
                "reconcile_payment",
            }
            <= names
        )
        self.assertFalse(
            {
                "PartnerStore",
                "PartnerCommerceStore",
                "PartnerEnrollmentStore",
                "PartnerSettings",
            }
            & names
        )


class DatabaseProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_spn_signature_rejected_before_counts_or_mutations(self):
        for table, columns in MAIN_SPN_SIGNATURES.items():
            conn = FakeConnection()
            original = conn.fetch

            async def with_main(query, *args):
                return await original(query, *args) + [
                    {"table_name": table, "column_name": column} for column in columns
                ]

            conn.fetch = with_main
            result = await PartnerRuntime(staging(), lambda: FakePool(conn)).probe()
            self.assertFalse(result["database_ready"])
            self.assertEqual(result["blockers"], ["main_spn_database_detected"])
            self.assertEqual(len(conn.calls), 1)
            self.assertEqual(conn.readonly, [True])

    async def test_supabase_probe_requires_server_role_and_private_data_api(self):
        conn = FakeConnection()
        original = conn.fetchrow

        async def insecure(query):
            if "AS role_safe" in query:
                conn.calls.append(query)
                return {"role_safe": True, "api_objects_revoked": False}
            return await original(query)

        conn.fetchrow = insecure
        result = await PartnerRuntime(supabase(), lambda: FakePool(conn)).probe()
        self.assertFalse(result["database_ready"])
        self.assertEqual(result["blockers"], ["supabase_access_policy_required"])
        self.assertEqual(conn.readonly, [True])
        self.assertTrue(all("SELECT" in query for query in conn.calls))

    async def test_pool_uses_verified_tls_and_zero_prepared_cache_for_session(self):
        spec = importlib.util.spec_from_file_location(
            "isolated_runtime_cli", ROOT / "scripts/partner_runtime.py"
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for mode, cache in (("direct", 100), ("session", 0)):
            pool = SimpleNamespace(close=AsyncMock(), terminate=Mock())
            context = object()
            config = staging(
                database_connection_mode=mode,
                database_tls=True,
                database_ca_file="/etc/spn-partner/root.crt",
            )
            with patch(
                "asyncpg.create_pool", new=AsyncMock(return_value=pool)
            ) as create, patch.object(
                cli.ssl, "create_default_context", return_value=context
            ) as tls:
                async with cli.open_pool(config) as actual:
                    self.assertIs(actual, pool)
                self.assertEqual(create.call_args.kwargs["dsn"], config.database_dsn)
                self.assertIs(create.call_args.kwargs["ssl"], context)
                self.assertEqual(create.call_args.kwargs["statement_cache_size"], cache)
                self.assertEqual(
                    create.call_args.kwargs["server_settings"]["search_path"], "public"
                )
                tls.assert_called_once_with(cafile="/etc/spn-partner/root.crt")
                pool.close.assert_awaited_once()
                pool.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
