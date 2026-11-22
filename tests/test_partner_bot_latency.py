"""Read-only timing diagnostics and retained authorization mutation locks."""
from contextlib import asynccontextmanager
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from services.partner_management import PartnerManagementStore


SOURCE = Path(__file__).resolve().parents[1] / "ops/partner-platform/diagnose_bot_latency.py"
spec = importlib.util.spec_from_file_location("partner_bot_latency_diagnostic", SOURCE)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class LatencyDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_measurement_only_runs_fixed_selects_and_returns_aggregates(self):
        calls, events = [], []

        class Connection:
            async def fetchval(self, sql, *params):
                calls.append((sql, params))
                return 1

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                events.append("acquire")
                yield Connection()
                events.append("release")

        @asynccontextmanager
        async def factory(settings):
            self.assertIs(settings, marker)
            events.append("pool_open")
            yield Pool()
            events.append("pool_close")

        marker = object()
        result = await diagnostic.measure_selects(marker, factory)
        self.assertEqual(calls, [("SELECT 1", ()), ("SELECT $1::integer", (1,))] * 5)
        self.assertEqual(events.count("pool_open"), 1)
        self.assertEqual(events.count("pool_close"), 1)
        self.assertEqual(events.count("release"), 5)
        self.assertEqual(result["select_calls"], 10)
        self.assertEqual(result["bot_updates_replayed"], 0)
        self.assertTrue(result["read_only"])
        for key in ("pool_acquire", "pool_release", "plain_select", "parameterized_select"):
            self.assertEqual(set(result[key]), {"min_ms", "median_ms", "max_ms"})
            self.assertGreaterEqual(result[key]["min_ms"], 0)

    async def test_sample_boundaries_are_not_coerced_and_do_not_open_pool(self):
        factory = AsyncMock()
        for samples in (True, 0, 11, 1.0, "5"):
            with self.subTest(samples=samples), self.assertRaises(diagnostic.DiagnosisError):
                await diagnostic.measure_selects(None, factory, samples=samples)
        factory.assert_not_called()

    async def test_mutation_authorization_keeps_project_lock_by_default(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"id": "project"}, {"role": "owner"}]
        await PartnerManagementStore._authorize(conn, UUID(int=1), 42, "analytics")
        self.assertEqual(conn.fetchrow.await_count, 2)
        self.assertIn("FOR UPDATE", conn.fetchrow.await_args_list[0].args[0])

    def test_default_plan_never_loads_private_runtime_or_opens_files(self):
        output = io.StringIO()
        with patch.object(diagnostic, "trusted_runtime") as loader, \
                patch.object(diagnostic.os, "open", side_effect=AssertionError("No file I/O")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main([]), 0)
        loader.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["network_used"])

    def test_cli_errors_and_failed_connection_cannot_echo_secrets(self):
        secret = "artificial-private-error-token"
        output = io.StringIO()
        with patch.object(diagnostic, "trusted_runtime", side_effect=RuntimeError(secret)), \
                contextlib.redirect_stderr(output):
            self.assertEqual(diagnostic.main(["--measure"]), 2)
            self.assertEqual(diagnostic.main(["--token", secret]), 2)
        self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
