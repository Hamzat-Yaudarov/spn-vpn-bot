"""Offline backup helper contracts: no private configuration or live database."""
from contextlib import asynccontextmanager, redirect_stdout
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("private_pilot_backup", Path(__file__).with_name("backup_private_pilot.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Rows:
    def __init__(self, rows): self.rows = rows
    def __aiter__(self):
        async def iterate():
            for row in self.rows: yield {"row": row}
        return iterate()


class Connection:
    def __init__(self): self.transactions, self.queries = [], []
    @asynccontextmanager
    async def transaction(self, **kwargs):
        self.transactions.append(kwargs)
        yield
    async def fetch(self, sql, *args):
        self.queries.append(sql)
        if "information_schema" in sql: return []
        return [{"tablename": "wl_table_" + chr(97 + index // 26) + chr(97 + index % 26)} for index in range(31)]
    def cursor(self, sql, **kwargs):
        self.queries.append(sql)
        return Rows(['{"nullable":null,"json":{"x":1}}', {"nullable": None, "json": {"x": 2}}])


class Pool:
    def __init__(self, conn): self.conn = conn
    @asynccontextmanager
    async def acquire(self): yield self.conn


class PrivateBackupTests(unittest.IsolatedAsyncioTestCase):
    def test_default_plan_has_no_file_database_import_or_network(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(module, "backup", side_effect=AssertionError("no I/O")), patch("builtins.open", side_effect=AssertionError("no file read")):
            self.assertEqual(module.main([]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["plan_only"])
        self.assertFalse(result["database_read"])
        self.assertFalse(result["file_written"])

    def test_explicit_confirmation_and_fixed_target_required_before_io(self):
        for args in (["--backup-db"], ["--backup-db", "--confirm-target", "127.0.0.1"], ["--unexpected"]):
            output = io.StringIO()
            with self.subTest(args=args), redirect_stdout(output), patch.object(module, "backup", side_effect=AssertionError("no I/O")):
                self.assertEqual(module.main(args), 2)
                self.assertEqual(json.loads(output.getvalue())["error"], "private_backup_failed")

    async def test_every_table_every_row_json_preserved_repeatable_read_readonly(self):
        conn = Connection()
        blob, counts = await module.snapshot(Pool(conn))
        data = json.loads(blob)
        self.assertEqual(len(data["tables"]), 31)
        self.assertEqual(set(counts.values()), {2})
        self.assertEqual(conn.transactions, [{"isolation": "repeatable_read", "readonly": True}])
        for rows in data["tables"].values():
            self.assertEqual(rows, [{"nullable": None, "json": {"x": 1}}, {"nullable": None, "json": {"x": 2}}])
        self.assertTrue(all(query.lstrip().startswith("SELECT") and "LIMIT" not in query for query in conn.queries))

    async def test_bound_and_main_spn_schema_fail_closed(self):
        with self.assertRaises(module.BackupError):
            await module.snapshot(Pool(Connection()), maximum=100)
        conn = Connection()
        original = conn.fetch
        async def fetch(sql, *args):
            if "information_schema" in sql:
                return [{"table_name": "payments", "column_name": name} for name in module.MAIN_SIGNATURES["payments"]]
            return await original(sql, *args)
        conn.fetch = fetch
        with self.assertRaises(module.BackupError):
            await module.snapshot(Pool(conn))
        self.assertEqual(conn.queries, [])

    async def test_original_database_json_decimal_text_is_not_rounded(self):
        conn = Connection()
        raw = '{"json":{"precise":1.2345678901234567890123456789},"nullable":null}'
        conn.cursor = lambda *args, **kwargs: Rows([raw])
        blob, counts = await module.snapshot(Pool(conn))
        self.assertEqual(set(counts.values()), {1})
        self.assertIn(raw.encode(), blob)
        decoded = json.loads(blob, parse_float=Decimal)
        for rows in decoded["tables"].values():
            self.assertEqual(rows[0]["json"]["precise"], Decimal("1.2345678901234567890123456789"))

    async def test_non_object_or_non_json_database_text_is_rejected(self):
        for raw in ('[]', '"encoded-string"', '{"invalid":NaN}', '{broken'):
            conn = Connection()
            conn.cursor = lambda *args, **kwargs: Rows([raw])
            with self.subTest(raw=raw), self.assertRaises((module.BackupError, ValueError)):
                await module.snapshot(Pool(conn))


if __name__ == "__main__":
    unittest.main()
