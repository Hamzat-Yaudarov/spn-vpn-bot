"""Post-release audit plan and signed read-only identity checks, entirely local."""
import contextlib
from contextlib import asynccontextmanager
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from services.telegram_auth import validate_telegram_init_data
from test_partner_runtime import TOKEN
from test_partner_runtime import FakePool


PATH = Path(__file__).resolve().parents[1] / "ops/partner-platform/audit_payment_first.py"
spec = importlib.util.spec_from_file_location("_payment_first_audit", PATH)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PaymentFirstAuditTests(unittest.TestCase):
    def test_default_plan_never_loads_private_config_or_runs_audit(self):
        output = io.StringIO()
        with patch.object(audit, "audit") as check, patch.object(Path, "read_bytes") as read, \
             patch.object(audit.asyncio, "run") as run, contextlib.redirect_stdout(output):
            self.assertEqual(audit.main([]), 0)
        check.assert_not_called(); read.assert_not_called(); run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {
            "plan_only": True, "network_used": False, "invoices_created": 0,
        })

    def test_proof_is_valid_only_for_the_explicit_actor_without_echoing_token(self):
        for actor in (audit.OWNER, audit.FICTIONAL):
            signed = audit.proof(TOKEN, actor)
            self.assertTrue(signed.startswith("tma "))
            user = validate_telegram_init_data(signed[4:], TOKEN, max_age_seconds=3600)
            self.assertEqual(user["id"], actor)
            self.assertNotIn(TOKEN, signed)

    def test_wrong_host_or_prefix_is_rejected_before_private_config_or_imports(self):
        output = io.StringIO()
        with patch.object(audit.sys, "platform", "darwin"), \
             patch.object(Path, "read_bytes") as read, \
             contextlib.redirect_stderr(output):
            self.assertEqual(audit.main(["--release", "/private/tmp/secret-release"]), 2)
        read.assert_not_called()
        self.assertNotIn("secret-release", output.getvalue())

    def test_invalid_options_are_redacted_without_argparse_echo(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(audit.main(["--unknown", "private-argument-never-echo"]), 2)
        self.assertNotIn("private-argument-never-echo", output.getvalue())
        self.assertNotIn("usage:", output.getvalue())

    def test_entry_snapshot_keeps_old_money_bot_and_receipt_but_allows_later_status(self):
        prior = {"id": "00000000-0000-4000-8000-000000000001",
                 "project_id": "00000000-0000-4000-8000-000000000002",
                 "owner_tg_id": 42, "request_key": "00000000-0000-4000-8000-000000000003",
                 "purpose": "entry", "price_kopecks": 1000, "currency": "RUB",
                 "bot_id": 777, "provider_payment_id": "saved-receipt", "status": "created"}
        current = {**prior, "id": UUID(prior["id"]), "status": "launched"}
        audit.check_entry_snapshot(prior, current)
        for key, value in (("price_kopecks", 999000), ("owner_tg_id", 43),
                           ("project_id", "00000000-0000-4000-8000-000000000004"),
                           ("currency", "USD"), ("purpose", "subscription"),
                           ("bot_id", 888), ("bot_id", None),
                           ("provider_payment_id", "other"), ("provider_payment_id", None)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                audit.check_entry_snapshot(prior, {**current, key: value})
        with self.assertRaises(ValueError): audit.check_entry_snapshot(prior, None)
        unbound = {**prior, "bot_id": None, "provider_payment_id": None}
        audit.check_entry_snapshot(unbound, current)
        self.assertEqual(prior["status"], "created")


class PaymentFirstAuditIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_current_operator_stops_before_http_timestamp_refresh(self):
        from scripts import partner_runtime
        import aiohttp

        release = Path("/opt/spn-franchise-staging/releases/20261026-0123456789ab")
        settings = SimpleNamespace(enable_public_entry_sales=True, enable_payment_first_entry=True,
            enable_public_entry_trial=False, enable_owner_entry_pilot=False,
            manager_allowed_user_ids=None, operator_ids=(audit.OWNER,),
            database_project_ref=audit.PROJECT, enable_provisioning=False, enable_public_checkout=False)
        tables = {"wl_" + str(i): [] for i in range(31)}
        tables["wl_manager_identities"] = [{"tg_id": audit.OWNER}]
        snapshot = {"project_ref": audit.PROJECT, "tables": tables}

        class Connection:
            readonly = []
            fetchval = AsyncMock(return_value=False)

            @asynccontextmanager
            async def transaction(self, *, readonly=False):
                self.readonly.append(readonly)
                yield

        conn = Connection()

        @asynccontextmanager
        async def pool(config):
            self.assertIs(config, settings)
            yield FakePool(conn)

        with patch.object(audit.sys, "platform", "linux"), \
             patch.object(audit.sys, "prefix", str(release / ".venv")), \
             patch.object(audit.os, "geteuid", return_value=0), \
             patch.object(audit.sys, "path", list(audit.sys.path)), \
             patch.object(partner_runtime, "load_config", return_value=(settings, None)), \
             patch.object(partner_runtime, "open_pool", pool), \
             patch.object(Path, "read_bytes", return_value=json.dumps(snapshot).encode()), \
             patch.object(aiohttp, "ClientSession") as http:
            with self.assertRaises(ValueError): await audit.audit(release)
        http.assert_not_called()
        self.assertEqual(conn.readonly, [True])
        conn.fetchval.assert_awaited_once_with(
            "SELECT EXISTS(SELECT 1 FROM wl_manager_identities WHERE tg_id=$1)", audit.OWNER)


if __name__ == "__main__": unittest.main()
