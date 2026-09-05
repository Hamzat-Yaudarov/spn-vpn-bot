"""Optional real PostgreSQL/WASM tests of the actual Python SQL paths.

PGLITE_MODULE=/absolute/path/to/@electric-sql/pglite/dist/index.js
python3 -m unittest discover -s tests -p test_payment_recovery_postgres.py

Only an ephemeral in-memory database is used. Never accepts DATABASE_URL.
"""
import asyncio
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import database as db
from config import GB_BYTES
from services import payment_activation as activation, payment_processing as processing

LOCAL = "f30a18ee-68d3-4919-8b78-8615391bc94b"


class Bridge:
    fail_paid = False

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "node", str(Path(__file__).with_name("pglite_payment_bridge.mjs")), os.environ["PGLITE_MODULE"],
            env={**os.environ, "TZ": "UTC"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ready = await asyncio.wait_for(self.proc.stdout.readline(), 30)
        if not ready:
            raise RuntimeError((await self.proc.stderr.read()).decode())
        assert json.loads(ready)["ready"]
        return self

    async def close(self):
        self.proc.stdin.close()
        await asyncio.wait_for(self.proc.wait(), 10)

    async def query(self, sql, *params):
        if self.fail_paid and "UPDATE payments SET status = 'paid'" in sql:
            raise RuntimeError("injected failure before paid commit")
        data = json.dumps({"sql": sql, "params": params}, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v))
        self.proc.stdin.write((data + "\n").encode())
        await self.proc.stdin.drain()
        result = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 15))
        if result.get("error"):
            raise RuntimeError(result["error"])
        for row in result.get("rows", []):
            for field in result.get("fields", []):
                key, oid = field["name"], field["dataTypeID"]
                if row[key] is None:
                    continue
                if oid in (20, 21, 23):
                    row[key] = int(row[key])
                elif oid == 1700:
                    row[key] = Decimal(row[key])
                elif oid in (1114, 1184):
                    row[key] = datetime.fromisoformat(row[key].replace("Z", "+00:00")).replace(tzinfo=None)
        return result.get("rows", [])

    async def fetch(self, sql, *params):
        return await self.query(sql, *params)

    async def fetchrow(self, sql, *params):
        rows = await self.query(sql, *params)
        return rows[0] if rows else None

    async def fetchval(self, sql, *params):
        row = await self.fetchrow(sql, *params)
        return next(iter(row.values())) if row else None

    async def execute(self, sql, *params):
        await self.query(sql, *params)

    def transaction(self):
        bridge = self

        class Transaction:
            async def __aenter__(self):
                await bridge.execute("BEGIN")

            async def __aexit__(self, exc_type, *args):
                await bridge.execute("ROLLBACK" if exc_type else "COMMIT")
                return False
        return Transaction()

    def acquire(self):
        bridge = self

        class Acquire:
            async def __aenter__(self):
                return bridge

            async def __aexit__(self, *args):
                return False

            def __await__(self):
                return self.__aenter__().__await__()
        return Acquire()

    async def release(self, conn):
        pass


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional in-memory PostgreSQL integration environment")
class PostgreSQLRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await Bridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.enterContext(patch.object(db, "_pool", self.sql))
        self.enterContext(patch.object(processing, "_cancel_reactivation_safely", AsyncMock()))
        self.remote = self.enterContext(patch.object(processing, "remnawave_get_or_create_user", AsyncMock(return_value=(LOCAL, "tg_123_bypass_1"))))
        self.expiry = self.enterContext(patch.object(processing, "remnawave_set_subscription_expiry", AsyncMock(return_value=True)))
        self.enterContext(patch.object(processing, "remnawave_get_subscription_url", AsyncMock(return_value="https://example.test/key")))
        self.reset = self.enterContext(patch.object(processing, "remnawave_reset_user_traffic", AsyncMock(return_value=True)))
        self.bot = AsyncMock()
        await self.sql.execute("INSERT INTO users (tg_id) VALUES (123)")
        await self.sql.execute("""
            INSERT INTO payments (tg_id, tariff_code, amount, provider, invoice_id, target_slot_number)
            VALUES (123, 'bypass_1m', 300, 'yookassa', 'invoice-1', 1)
        """)

    async def issue(self):
        return await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m")

    async def test_duplicate_slot_incident_then_database_failure_then_success(self):
        await self.sql.execute("""
            INSERT INTO subscriptions (tg_id, slot_number, type_index, plan_kind, generation, is_visible, is_renewable)
            SELECT 123, n, 1, 'bypass', 'v2', TRUE, TRUE FROM generate_series(1, 10) n
        """)
        self.remote.return_value = (None, None)
        self.assertFalse(await self.issue())
        reserved = await self.sql.fetchrow("SELECT * FROM payment_subscription_activations")
        self.assertIsNotNone(reserved)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM subscriptions"), 10)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM subscriptions WHERE is_visible"), 1)
        self.remote.return_value = (LOCAL, "tg_123_bypass_1")
        self.sql.fail_paid = True
        self.assertFalse(await self.issue())
        self.assertEqual(await self.sql.fetchval("SELECT status FROM payments"), "pending")
        self.assertIsNone(await self.sql.fetchval("SELECT subscription_until FROM subscriptions WHERE is_visible"))
        self.sql.fail_paid = False
        self.assertTrue(await self.issue())
        self.assertEqual(await self.sql.fetchval("SELECT subscription_until FROM subscriptions WHERE is_visible"), reserved["expires_at"])
        self.assertEqual(await self.sql.fetchval("SELECT status FROM payments"), "paid")
        self.assertEqual(await self.sql.fetchval("SELECT current_period_limit_bytes FROM subscriptions WHERE is_visible"), 200 * GB_BYTES)
        self.assertEqual(await self.sql.fetchval("SELECT remnawave_uuid::text FROM users WHERE tg_id = 123"), LOCAL)
        self.assertTrue(await self.issue())
        self.bot.send_message.assert_awaited_once()

    async def test_active_renewal_keeps_paid_traffic_and_only_adds_days_once(self):
        old = await self.sql.fetchrow("""
            INSERT INTO subscriptions (tg_id, slot_number, type_index, plan_kind, generation, is_visible, is_renewable,
                remnawave_uuid, remnawave_username, subscription_until, base_traffic_bytes,
                current_paid_traffic_bytes, current_period_limit_bytes)
            VALUES (123, 1, 1, 'bypass', 'v2', TRUE, TRUE, $1, 'tg_123_bypass_1',
                date_trunc('second', now()) + interval '5 days', $2, $3, $4) RETURNING *
        """, LOCAL, 200 * GB_BYTES, 10 * GB_BYTES, 210 * GB_BYTES)
        await self.sql.execute("UPDATE payments SET payment_target = 'renew', subscription_id = $1", old["id"])
        self.assertTrue(await self.issue())
        first = await self.sql.fetchval("SELECT subscription_until FROM subscriptions")
        self.assertEqual((first - old["subscription_until"]).days, 30)
        self.assertEqual(await self.sql.fetchval("SELECT current_paid_traffic_bytes FROM subscriptions"), 10 * GB_BYTES)
        self.assertEqual(await self.sql.fetchval("SELECT current_period_limit_bytes FROM subscriptions"), 210 * GB_BYTES)
        self.assertTrue(await self.issue())
        self.assertEqual(await self.sql.fetchval("SELECT subscription_until FROM subscriptions"), first)
        self.reset.assert_not_awaited()

    async def test_placeholder_linked_to_another_payment_is_not_reused(self):
        sid = await self.sql.fetchval("""
            INSERT INTO subscriptions (tg_id, slot_number, type_index, plan_kind, generation, is_visible, is_renewable)
            VALUES (123, 1, 1, 'bypass', 'v2', TRUE, TRUE) RETURNING id
        """)
        await self.sql.execute("""
            INSERT INTO payments (tg_id, tariff_code, amount, provider, invoice_id, subscription_id)
            VALUES (123, 'bypass_1m', 300, 'yookassa', 'other', $1)
        """, sid)
        self.assertFalse(await self.issue())
        self.remote.assert_not_awaited()
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM payment_subscription_activations"), 0)
