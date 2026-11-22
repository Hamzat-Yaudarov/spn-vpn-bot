"""Explicit stage-6 SQL on isolated PostgreSQL/WASM, never working SPN data."""

from datetime import timedelta
import os
import unittest
from uuid import uuid4

import asyncpg

import test_partner_entitlements_postgres as fixtures
from services.partner_commerce_store import (
    PartnerCommerceStore,
    CommerceConflict,
    CommerceNotFound,
    SalesUnavailable,
)
from services.partner_subscriptions import (
    PartnerSubscriptionLinks,
    PartnerLinkSettings,
    SubscriptionNotFound,
    SubscriptionUnavailable,
)
from services.partner_traffic import PartnerTrafficUsage

ROOT, NOW, quotes = fixtures.ROOT, fixtures.NOW, fixtures.quotes


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class PublicSubscriptionsPostgresTests(unittest.IsolatedAsyncioTestCase):
    subscription = fixtures.EntitlementPostgresTests.subscription
    pay = fixtures.EntitlementPostgresTests.pay
    complete = fixtures.EntitlementPostgresTests.complete

    async def asyncSetUp(self):
        await fixtures.EntitlementPostgresTests.asyncSetUp(self)
        for name in ("traffic.sql", "subscriptions.sql"):
            await self.sql.query(
                (ROOT / "ops/partner-platform" / name).read_text(), exec=True
            )
        self.store = PartnerCommerceStore(
            lambda: self.sql,
            quotes,
            entitlements_enabled=True,
            traffic_cycles_enabled=True,
        )
        self.sub = await self.subscription()
        self.links = PartnerSubscriptionLinks(
            PartnerLinkSettings(
                "partners.example.test", b"isolated-sql-test-signing-key-32b"
            )
        )

    async def test_explicit_idempotent_migration_does_not_touch_money_or_limits(self):
        before = await self.sql.fetchrow(
            "SELECT * FROM wl_subscriptions WHERE id=$1", self.sub["id"]
        )
        await self.sql.query(
            (ROOT / "ops/partner-platform/subscriptions.sql").read_text(), exec=True
        )
        after = await self.sql.fetchrow(
            "SELECT * FROM wl_subscriptions WHERE id=$1", self.sub["id"]
        )
        self.assertEqual(before, after)
        self.assertEqual(after["public_link_generation"], 0)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1
        )
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute(
                "UPDATE wl_subscriptions SET public_link_generation=-1 WHERE id=$1",
                self.sub["id"],
            )

    async def test_resolve_private_snapshot_customer_scope_and_no_owner_or_ciphertext(
        self,
    ):
        context = await self.store.get_subscription_link_context(
            self.pid, 100, self.sub["id"]
        )
        self.assertEqual(context["subscription_id"], self.sub["id"])
        self.assertEqual(context["traffic_cycle_baseline_bytes"], 0)
        self.assertFalse(context["activation_pending"])
        self.assertFalse(
            {"encrypted_token", "tg_id", "owner_tg_id", "price_kopecks"}
            & context.keys()
        )
        for project, tg, sub in (
            (self.pid, 999, self.sub["id"]),
            (str(uuid4()), 100, self.sub["id"]),
            (self.pid, 100, str(uuid4())),
        ):
            with self.assertRaises(CommerceNotFound):
                await self.store.get_subscription_link_context(project, tg, sub)
        with self.assertRaises(CommerceNotFound):
            await self.store.resolve_public_subscription("other-vpn", self.sub["id"])

    async def test_rotation_revokes_old_link_no_double_rotation_after_lost_response(
        self,
    ):
        before = await self.store.get_subscription_link_context(
            self.pid, 100, self.sub["id"]
        )
        old = self.links.token(before)
        first = await self.store.rotate_subscription_link(
            self.pid, 100, self.sub["id"], 0
        )
        retry = await self.store.rotate_subscription_link(
            self.pid, 100, self.sub["id"], 0
        )
        self.assertEqual(self.links.token(first), self.links.token(retry))
        self.assertEqual(first["public_link_generation"], 1)
        with self.assertRaises(SubscriptionNotFound):
            self.links.verify(old, first)
        self.links.verify(self.links.token(first), retry)
        await self.store.rotate_subscription_link(self.pid, 100, self.sub["id"], 1)
        with self.assertRaises(CommerceConflict):
            await self.store.rotate_subscription_link(self.pid, 100, self.sub["id"], 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 1)

    async def test_foreign_rotation_cannot_invalidate_another_customers_link(self):
        with self.assertRaises(CommerceNotFound):
            await self.store.rotate_subscription_link(self.pid, 999, self.sub["id"], 0)
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT public_link_generation FROM wl_subscriptions WHERE id=$1",
                self.sub["id"],
            ),
            0,
        )

    async def test_verified_usage_survives_restart_and_rollback_hold_is_committed_not_reset(
        self,
    ):
        context = await self.store.get_subscription_link_context(
            self.pid, 100, self.sub["id"]
        )

        def usage(used):
            return PartnerTrafficUsage(
                self.pid, self.sub["id"], "remote-1", used, 300 * 1024**3, NOW
            )

        async with self.store.acquire_activation_lock(
            self.pid, self.sub["id"]
        ) as lease:
            await lease.remember_public_usage(context, usage(100 * 1024**3), now=NOW)
        restarted = PartnerCommerceStore(
            lambda: self.sql,
            quotes,
            entitlements_enabled=True,
            traffic_cycles_enabled=True,
        )
        row = await restarted.get_subscription_link_context(
            self.pid, 100, self.sub["id"]
        )
        self.assertEqual(row["traffic_last_used_bytes"], 100 * 1024**3)
        with self.assertRaises(SubscriptionUnavailable):
            await restarted.remember_public_usage(row, usage(90 * 1024**3), now=NOW)
        row = await restarted.get_subscription_link_context(
            self.pid, 100, self.sub["id"]
        )
        self.assertEqual(row["traffic_cycle_hold"], "counter_changed")
        self.assertEqual(row["traffic_last_used_bytes"], 100 * 1024**3)
        self.assertEqual(row["traffic_limit_bytes"], 300 * 1024**3)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1
        )

    async def test_observation_cannot_commit_over_rotated_link_pending_grant_or_foreign_identity(
        self,
    ):
        row = await self.store.get_subscription_link_context(
            self.pid, 100, self.sub["id"]
        )
        observation = PartnerTrafficUsage(
            self.pid, self.sub["id"], "remote-1", 123, 300 * 1024**3, NOW
        )
        await self.store.rotate_subscription_link(self.pid, 100, self.sub["id"], 0)
        with self.assertRaises(SubscriptionUnavailable):
            await self.store.remember_public_usage(row, observation, now=NOW)
        row = await self.store.get_subscription_link_context(
            self.pid, 100, self.sub["id"]
        )
        order = await self.store.create_order(
            self.pid, 100, str(uuid4()), 19900, subscription_id=self.sub["id"]
        )
        await self.pay(order, now=NOW + timedelta(days=1))
        with self.assertRaises(SubscriptionUnavailable):
            await self.store.remember_public_usage(row, observation, now=NOW)
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT traffic_last_used_bytes FROM wl_subscriptions WHERE id=$1",
                self.sub["id"],
            ),
            0,
        )

    async def test_paid_pending_snapshot_flag_and_same_connection_lease(self):
        order = await self.store.create_order(
            self.pid, 100, str(uuid4()), 19900, subscription_id=self.sub["id"]
        )
        await self.pay(order, now=NOW + timedelta(days=1))
        async with self.store.acquire_activation_lock(
            self.pid, self.sub["id"]
        ) as lease:
            row = await lease.resolve_public_subscription(
                "entitlement-vpn", self.sub["id"]
            )
            self.assertTrue(row["activation_pending"])
            self.assertEqual(row["expires_at"], NOW + timedelta(days=30))
            self.assertEqual(row["desired_expires_at"], NOW + timedelta(days=60))
        await self.complete(order)
        self.assertFalse(
            (
                await self.store.resolve_public_subscription(
                    "entitlement-vpn", self.sub["id"]
                )
            )["activation_pending"]
        )

    async def test_unpaid_unissued_inactive_unknown_projects_and_default_capability_rejected(
        self,
    ):
        await self.store.register_customer(self.pid, 200)
        order = await self.store.create_order(self.pid, 200, str(uuid4()), 19900)
        with self.assertRaises(CommerceNotFound):
            await self.store.get_subscription_link_context(
                self.pid, 200, order["subscription_id"]
            )
        await self.sql.execute(
            "UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid
        )
        with self.assertRaises(CommerceNotFound):
            await self.store.resolve_public_subscription(
                "entitlement-vpn", self.sub["id"]
            )
        disabled = PartnerCommerceStore(lambda: self.sql, quotes)
        with self.assertRaises(SalesUnavailable):
            await disabled.get_subscription_link_context(self.pid, 100, self.sub["id"])
