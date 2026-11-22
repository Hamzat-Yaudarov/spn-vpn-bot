"""Optional actual PostgreSQL/WASM execution of registry SQL, never a live DB.

PGLITE_MODULE=/absolute/test-only/path/@electric-sql/pglite/dist/index.js
PGLITE_NODE=/absolute/path/to/node python3 -m unittest discover -s tests -p test_partner_store_postgres.py

No config import, .env read, network, DATABASE_URL or existing SPN schema. The
single-process bridge does NOT simulate concurrent independent PG connections;
production locking races must additionally be tested on isolated native PG.
"""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import unittest

import asyncpg

from services.partner_store import InvalidProject, PartnerStore, ProjectConflict, ProjectNotFound


class RegistryBridge:
    def __init__(self):
        self.fail_audit = False

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            os.environ.get("PGLITE_NODE", "node"), str(Path(__file__).with_name("pglite_partner_bridge.mjs")),
            os.environ["PGLITE_MODULE"], stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ready = await asyncio.wait_for(self.proc.stdout.readline(), 40)
        if not ready or not json.loads(ready).get("ready"):
            raise RuntimeError((await self.proc.stderr.read()).decode())
        return self

    async def close(self):
        if self.proc.returncode is None:
            self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), 15)

    async def query(self, sql, *params, exec=False):
        if self.fail_audit and "INSERT INTO wl_project_audit" in sql:
            raise RuntimeError("injected audit failure")
        message = json.dumps({"sql": sql, "params": params, "exec": exec}, default=str)
        self.proc.stdin.write((message + "\n").encode())
        await self.proc.stdin.drain()
        response = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 20))
        if isinstance(response, dict) and response.get("error"):
            exception = {
                "23505": asyncpg.UniqueViolationError,
                "23514": asyncpg.CheckViolationError,
                "23503": asyncpg.ForeignKeyViolationError,
            }.get(response.get("code"), RuntimeError)
            error = exception(response["error"])
            if response.get("constraint"):
                error.constraint_name = response["constraint"]
            raise error
        if exec:
            return response
        for row in response.get("rows", []):
            for field in response.get("fields", []):
                key, oid = field["name"], field["dataTypeID"]
                if row[key] is None:
                    continue
                if oid in (20, 21, 23):
                    row[key] = int(row[key])
                elif oid in (1114, 1184):
                    row[key] = datetime.fromisoformat(row[key].replace("Z", "+00:00"))
        return response.get("rows", [])

    async def execute(self, sql, *params):
        await self.query(sql, *params)

    async def fetch(self, sql, *params):
        return await self.query(sql, *params)

    async def fetchrow(self, sql, *params):
        rows = await self.query(sql, *params)
        return rows[0] if rows else None

    async def fetchval(self, sql, *params):
        row = await self.fetchrow(sql, *params)
        return next(iter(row.values())) if row else None

    def acquire(self):
        bridge = self

        class Acquire:
            async def __aenter__(self):
                return bridge

            async def __aexit__(self, *args):
                return False
        return Acquire()

    def transaction(self):
        bridge = self

        class Transaction:
            async def __aenter__(self):
                await bridge.execute("BEGIN")

            async def __aexit__(self, exc_type, *args):
                await bridge.execute("ROLLBACK" if exc_type else "COMMIT")
                return False
        return Transaction()


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM module is not configured")
class PartnerStorePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.store = PartnerStore(lambda: self.sql)

    async def create(self, *, owner=42, key="request-123", slug="my-vpn", title="Мой VPN", price=19900):
        return await self.store.create_project(owner, key, slug, title, price)

    async def test_schema_reapplication_and_registry_only(self):
        schema = (Path(__file__).resolve().parents[1] / "ops/partner-platform/schema.sql").read_text()
        await self.sql.query(schema, exec=True)
        names = await self.sql.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        self.assertEqual([r["tablename"] for r in names], ["wl_project_audit", "wl_project_create_requests", "wl_projects"])

    async def test_create_restart_retry_and_changed_payload(self):
        first = await self.create(slug="My-VPN")
        self.assertEqual(first["slug"], "my-vpn")
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["status"], "draft")
        # A new service instance reuses the persisted key, no process-local state.
        self.store = PartnerStore(lambda: self.sql)
        second = await self.create()
        self.assertEqual(first, second)
        with self.assertRaises(ProjectConflict):
            await self.create(price=24900)
        with self.assertRaises(ProjectConflict):
            await self.create(title="Другой бренд")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_create_requests"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_audit"), 1)

    async def test_owner_isolation_and_multiple_projects(self):
        first = await self.create()
        await self.create(key="request-456", slug="my-second-vpn")
        foreign = await self.create(owner=99, slug="their-vpn")
        self.assertEqual(len(await self.store.list_projects(42)), 2)
        self.assertEqual([r["id"] for r in await self.store.list_projects(99)], [foreign["id"]])
        self.assertEqual(await self.store.list_projects(111), [])
        for method, args in (
            (self.store.get_project, (first["id"], 99)),
            (self.store.update_project, (first["id"], 99, 1, {"title": "stolen"})),
            (self.store.save_bot_connection, (first["id"], 99, 1, 111, "TheftBot", "x" * 100)),
        ):
            with self.assertRaises(ProjectNotFound):
                await method(*args)
        self.assertEqual((await self.store.get_project(first["id"], 42))["version"], 1)

    async def test_global_slug_and_bot_uniqueness_with_atomic_rollback(self):
        first = await self.create()
        with self.assertRaises(ProjectConflict):
            await self.create(owner=99)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_create_requests"), 1)
        second = await self.create(owner=99, slug="other-vpn")
        first = await self.store.save_bot_connection(first["id"], 42, 1, 555, "MyProjectBot", "cipher:" + "x" * 100)
        with self.assertRaises(ProjectConflict):
            await self.store.save_bot_connection(second["id"], 99, 1, 555, "MyProjectBot", "cipher:" + "y" * 100)
        self.assertEqual((await self.store.get_project(second["id"], 99))["version"], 1)
        self.assertEqual((await self.store.get_project(second["id"], 99))["bot"]["id"], None)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_audit WHERE action='bot_connected'"), 1)

    async def test_settings_update_stale_version_and_private_fields(self):
        first = await self.create()
        changed = await self.store.update_project(first["id"], 42, 1, {
            "title": "Новый VPN", "price_kopecks": 24999, "support_url": "https://t.me/my_support",
            "news_url": "https://t.me/my_news", "brand": {"accent_color": "#FFCC00", "welcome_text": "Привет!"},
        })
        self.assertEqual(changed["version"], 2)
        self.assertEqual(changed["price_kopecks"], 24999)
        self.assertEqual(changed["brand"]["accent_color"], "#ffcc00")
        with self.assertRaises(ProjectConflict):
            await self.store.update_project(first["id"], 42, 1, {"title": "stale"})
        with self.assertRaises(ProjectConflict):
            await self.store.save_bot_connection(first["id"], 42, 1, 555, "MyProjectBot", "x" * 100)
        for changes in ({"status": "active"}, {"slug": "hijacked"}, {"entry_paid": True}, {"price_kopecks": 19899}):
            with self.assertRaises(InvalidProject):
                await self.store.update_project(first["id"], 42, 2, changes)
        connected = await self.store.save_bot_connection(first["id"], 42, 2, 555, "MyProjectBot", "cipher:" + "x" * 100)
        self.assertEqual(connected["version"], 3)
        self.assertEqual(connected["status"], "draft")
        self.assertNotIn("encrypted_token", connected)
        self.assertNotIn("cipher:", str(await self.store.list_projects(42)))
        self.assertNotIn("cipher:", str(await self.sql.fetch("SELECT details FROM wl_project_audit")))
        self.assertEqual((await self.create())["version"], 3)

    async def test_audit_failure_rolls_back_create_and_update(self):
        self.sql.fail_audit = True
        with self.assertRaises(RuntimeError):
            await self.create()
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_create_requests"), 0)
        self.sql.fail_audit = False
        first = await self.create()
        self.sql.fail_audit = True
        with self.assertRaises(RuntimeError):
            await self.store.update_project(first["id"], 42, 1, {"price_kopecks": 29900})
        persisted = await self.store.get_project(first["id"], 42)
        self.assertEqual(persisted["price_kopecks"], 19900)
        self.assertEqual(persisted["version"], 1)

    async def test_database_constraints_min_price_and_owner_consistency(self):
        first = await self.create()
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("UPDATE wl_projects SET price_kopecks=1 WHERE id=$1", first["id"])
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.sql.execute("""INSERT INTO wl_project_create_requests
                (owner_tg_id,request_key,fingerprint,project_id) VALUES(99,'foreign-key',$1,$2)""", "0" * 64, first["id"])
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("""UPDATE wl_projects SET bot_id=555, encrypted_token=$2,
                token_updated_at=now() WHERE id=$1""", first["id"], "x" * 100)


if __name__ == "__main__":
    unittest.main()
