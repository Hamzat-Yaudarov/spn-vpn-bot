"""Independent 300 GB migration safety checks; mocks/ephemeral SQL only.

The SQL is extracted from the actual runtime source, not a copied migration.
No .env, provider API, production database or application startup is invoked.
"""
import ast
from datetime import datetime, timedelta
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

with patch('dotenv.load_dotenv', return_value=False), patch.dict(os.environ, {'DATABASE_URL': '', 'ADMIN_ID': '999'}):
    import database as db
    from config import GB_BYTES
    from services import traffic_periods, traffic_resets


NEW_BASE = 300 * GB_BYTES
UUID = 'f30a18ee-68d3-4919-8b78-8615391bc94b'


def runtime_upgrade_sql():
    """Find the deployed base-upgrade statement without running all migrations."""
    source = ast.parse(Path(db.__file__).read_text())
    statements = [node.value for node in ast.walk(source)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and 'UPDATE subscriptions' in node.value
                  and 'base_traffic_bytes = $1' in node.value
                  and 'GREATEST' in node.value and 'current_period_limit_bytes' in node.value]
    if len(statements) != 1:
        raise AssertionError(f'Expected one actual base migration, found {len(statements)}')
    return statements[0]


class Traffic300PureSafetyTests(unittest.TestCase):
    def test_base_upgrade_preserves_carried_paid_usage_and_reset_deadline(self):
        now = datetime(2026, 10, 12, 12)
        reset_at = now + timedelta(days=8)
        subscription = {'subscription_until': now + timedelta(days=18),
                        'base_traffic_bytes': 200 * GB_BYTES,
                        'carried_traffic_bytes': 15 * GB_BYTES,
                        'current_paid_traffic_bytes': 40 * GB_BYTES,
                        'current_period_limit_bytes': 255 * GB_BYTES,
                        'traffic_reset_at': reset_at,
                        'last_known_used_traffic_bytes': 240 * GB_BYTES}
        with patch.object(traffic_periods, 'BYPASS_BASE_TRAFFIC_GB', 300):
            state = traffic_periods.build_traffic_period_state(subscription, 'bypass', now)
        self.assertEqual((state.base_bytes, state.carried_bytes, state.paid_bytes, state.limit_bytes),
                         (NEW_BASE, 15 * GB_BYTES, 40 * GB_BYTES, 355 * GB_BYTES))
        self.assertEqual(state.last_known_used_bytes, 240 * GB_BYTES)
        self.assertEqual(state.reset_at, reset_at)

    def test_observed_extra_stays_in_total_and_rebase_is_idempotent(self):
        total = traffic_periods.rebase_traffic_limit_bytes(current_base_bytes=200 * GB_BYTES,
            current_limit_bytes=250 * GB_BYTES, carried_bytes=0, paid_bytes=0, new_base_bytes=NEW_BASE)
        self.assertEqual(total, 350 * GB_BYTES)
        repeated = traffic_periods.rebase_traffic_limit_bytes(current_base_bytes=NEW_BASE,
            current_limit_bytes=total, carried_bytes=0, paid_bytes=0, new_base_bytes=NEW_BASE)
        self.assertEqual(repeated, total)

    def test_runtime_upgrade_has_no_usage_or_reset_or_term_write(self):
        sql = runtime_upgrade_sql()
        for field in ('last_known_used_traffic_bytes', 'traffic_reset_at', 'subscription_until',
                      'current_paid_traffic_bytes =', 'carried_traffic_bytes ='):
            self.assertNotIn(field, sql)
        self.assertIn('last_traffic_sync_at = NULL', sql)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Traffic300SyncSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_time_active_subscription_syncs_355gb_without_usage_reset(self):
        subscription = {'id': 55, 'tg_id': 456, 'plan_kind': 'bypass',
                        'remnawave_uuid': UUID, 'base_traffic_bytes': NEW_BASE,
                        'current_period_limit_bytes': 355 * GB_BYTES, 'is_active': False,
                        'last_known_used_traffic_bytes': 255 * GB_BYTES}
        with (patch.object(traffic_resets.aiohttp, 'TCPConnector'),
              patch.object(traffic_resets.aiohttp, 'ClientSession', return_value=FakeSession()),
              patch.object(db, 'get_bypass_subscriptions_for_limit_sync', new=AsyncMock(return_value=[subscription])),
              patch.object(db, 'get_active_device_addon_count', new=AsyncMock(return_value=0)),
              patch.object(db, 'mark_traffic_limit_synced', new=AsyncMock()) as synced,
              patch.object(traffic_resets, 'remnawave_update_user_profile', new=AsyncMock(return_value=True)) as update,
              patch.object(traffic_resets, 'remnawave_reset_user_traffic', new=AsyncMock()) as reset):
            await traffic_resets.process_pending_traffic_limit_sync()
        self.assertEqual(update.await_args.kwargs['traffic_limit_bytes'], 355 * GB_BYTES)
        self.assertEqual(update.await_args.kwargs['traffic_limit_strategy'], 'NO_RESET')
        reset.assert_not_awaited()
        synced.assert_awaited_once_with(55)


@unittest.skipUnless(os.environ.get('PGLITE_MODULE'), 'Requires isolated local PostgreSQL/WASM module')
class Traffic300SQLSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_payment_recovery_postgres import Bridge
        self.sql = await Bridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.enterContext(patch.object(db, '_pool', self.sql))
        self.migration = runtime_upgrade_sql()
        await self.sql.execute('INSERT INTO users(tg_id) VALUES(456)')

    async def seed(self, *, base=200, carried=15, paid=40, total=255, used=210,
                   generation='v2', plan='bypass', visible=True, active=True, expired=False):
        return await self.sql.fetchrow('''INSERT INTO subscriptions(tg_id,slot_number,type_index,plan_kind,
            generation,is_visible,is_renewable,is_active,traffic_enabled,remnawave_uuid,subscription_until,
            base_traffic_bytes,carried_traffic_bytes,current_paid_traffic_bytes,current_period_limit_bytes,
            last_known_used_traffic_bytes,traffic_reset_at,last_traffic_sync_at)
            VALUES(456,(SELECT COALESCE(MAX(slot_number),0)+1 FROM subscriptions),1,$1,$2,$3,TRUE,$4,TRUE,$5,
                now()+$6*interval '1 day',$7,$8,$9,$10,$11,now()+interval '8 days',now()-interval '1 day')
            RETURNING *''', plan, generation, visible, active, UUID, -1 if expired else 20,
            base * GB_BYTES, carried * GB_BYTES, paid * GB_BYTES, total * GB_BYTES, used * GB_BYTES)

    async def test_actual_sql_preserves_all_buckets_and_unknown_extra_and_is_idempotent(self):
        rows = [await self.seed(), await self.seed(carried=5, paid=10, total=245),
                await self.seed(base=150, carried=5, paid=10, total=165),
                await self.seed(active=False, used=255), await self.seed(expired=True),
                await self.seed(base=300, total=355), await self.seed(base=350, total=405),
                await self.seed(generation='legacy'), await self.seed(plan='regular'),
                await self.seed(visible=False)]
        await self.sql.execute(self.migration, NEW_BASE)
        snapshots = []
        for before, total in zip(rows, (355, 345, 315, 355, 355, 355, 405, 255, 255, 255)):
            after = await self.sql.fetchrow('SELECT * FROM subscriptions WHERE id=$1', before['id'])
            eligible = (before['generation'] == 'v2' and before['plan_kind'] == 'bypass'
                        and before['is_visible'] and before['base_traffic_bytes'] < NEW_BASE)
            self.assertEqual(after['base_traffic_bytes'], NEW_BASE if eligible else before['base_traffic_bytes'])
            self.assertEqual(after['current_period_limit_bytes'], total * GB_BYTES)
            for field in ('carried_traffic_bytes', 'current_paid_traffic_bytes', 'last_known_used_traffic_bytes',
                          'traffic_reset_at', 'subscription_until', 'is_active'):
                self.assertEqual(after[field], before[field], field)
            if eligible:
                self.assertIsNone(after['last_traffic_sync_at'])
            snapshots.append(after)
        await self.sql.execute(self.migration, NEW_BASE)
        self.assertEqual(await self.sql.fetch('SELECT * FROM subscriptions ORDER BY id'), snapshots)

    async def test_additive_purchase_before_or_after_migration_has_same_paid_limit(self):
        before = await self.seed(carried=10, paid=20, total=230)
        after = await self.seed(carried=10, paid=20, total=230)
        await db.add_traffic_to_subscription(before['id'], 50 * GB_BYTES)
        await self.sql.execute(self.migration, NEW_BASE)
        await db.add_traffic_to_subscription(after['id'], 50 * GB_BYTES)
        for row in (before, after):
            current = await self.sql.fetchrow('SELECT * FROM subscriptions WHERE id=$1', row['id'])
            self.assertEqual(current['base_traffic_bytes'], NEW_BASE)
            self.assertEqual(current['carried_traffic_bytes'], 10 * GB_BYTES)
            self.assertEqual(current['current_paid_traffic_bytes'], 70 * GB_BYTES)
            self.assertEqual(current['current_period_limit_bytes'], 380 * GB_BYTES)

    async def test_sync_query_includes_exhausted_but_not_time_expired_rows(self):
        exhausted = await self.seed(active=False)
        expired = await self.seed(active=False, expired=True)
        await self.sql.execute(self.migration, NEW_BASE)
        sync_ids = {row['id'] for row in await db.get_bypass_subscriptions_for_limit_sync()}
        self.assertIn(exhausted['id'], sync_ids)
        self.assertNotIn(expired['id'], sync_ids)


if __name__ == '__main__':
    unittest.main()
