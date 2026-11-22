"""Actual isolated PostgreSQL/WASM identity cutover; no real remote calls."""

import json
from datetime import timedelta
import os
from pathlib import Path
import unittest
from uuid import uuid4

import asyncpg

from services.partner_commerce_store import PartnerCommerceStore
from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge
from test_partner_remnawave import NOW
import test_partner_entitlements_postgres as fixtures
from services.partner_subscriptions import (
    PartnerSubscriptionLinks,
    PartnerLinkSettings,
    profile_headers,
)
from services.partner_traffic import PartnerTrafficUsage
from partner_subscription_web import render_subscription_page


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PG/WASM not configured"
)
class PanelIdentityPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        root = Path(__file__).resolve().parents[1] / "ops/partner-platform"
        await self.sql.query((root / "commerce.sql").read_text(), exec=True)
        self.migration = (root / "panel_identity.sql").read_text()
        self.registry = PartnerStore(lambda: self.sql)
        self.project = await self.registry.create_project(
            42, "panel-identity-key", "sever-vpn", "Север VPN"
        )
        self.pid = self.project["id"]
        await self.registry.save_bot_connection(
            self.pid, 42, 1, 123456, "SeverBot", "cipher:" + "x" * 100
        )
        await self.sql.execute(
            "UPDATE wl_projects SET status='active' WHERE id=$1", self.pid
        )

    async def order(self, store, tg=100):
        await store.register_customer(self.pid, tg)
        return await store.create_order(self.pid, tg, str(uuid4()), 19900)

    async def paid(self, store, order):
        payment = "p-" + order["id"]
        await store.save_invoice(
            self.pid,
            order["tg_id"],
            order["id"],
            payment,
            "https://pay.example.org/" + payment,
        )
        await store.record_verified_payment(
            self.pid, order["id"], payment, 19900, "RUB", now=NOW
        )

    async def test_cutover_preserves_legacy_and_unknown_remote_create(self):
        old = PartnerCommerceStore(lambda: self.sql)
        order = await self.order(old)
        await self.paid(old, order)
        initial = await old.get_activation(self.pid, order["id"])
        await self.sql.query(self.migration, exec=True)
        await self.sql.query(self.migration, exec=True)
        cutover = PartnerCommerceStore(lambda: self.sql, panel_identity_enabled=True)
        snapshot = await cutover.get_activation(self.pid, order["id"])
        self.assertEqual(snapshot["remote_username"], initial["remote_username"])
        self.assertEqual(snapshot["remote_identity"]["version"], 1)
        self.assertEqual(
            snapshot["remote_identity"]["description"],
            f"SPN-WL:{self.pid}:{order['subscription_id']}",
        )

    async def test_new_identity_saved_before_network_and_immutable_after_brand_rename(
        self,
    ):
        await self.sql.query(self.migration, exec=True)
        store = PartnerCommerceStore(lambda: self.sql, panel_identity_enabled=True)
        order = await self.order(store)
        raw = await self.sql.fetchval(
            "SELECT remote_identity FROM wl_subscriptions WHERE id=$1",
            order["subscription_id"],
        )
        identity = json.loads(raw) if isinstance(raw, str) else raw
        self.assertEqual(identity["version"], 2)
        self.assertEqual(identity["telegram_id"], 100)
        self.assertEqual(identity["title"], "Север VPN")
        await self.paid(store, order)
        initial = await store.get_activation(self.pid, order["id"])
        await self.sql.execute(
            "UPDATE wl_projects SET title='Новое название',bot_username='AnotherBot' WHERE id=$1",
            self.pid,
        )
        after = await store.get_activation(self.pid, order["id"])
        self.assertEqual(initial, after)
        # Turning creation flag off cannot create a second legacy remote user.
        restarted = PartnerCommerceStore(lambda: self.sql)
        self.assertEqual(await restarted.get_activation(self.pid, order["id"]), initial)
        for query in (
            "UPDATE wl_subscriptions SET remote_identity='{}'::jsonb WHERE id=$1",
            "UPDATE wl_subscriptions SET tg_id=101 WHERE id=$1",
        ):
            with self.assertRaises(asyncpg.CheckViolationError):
                await self.sql.execute(query, order["subscription_id"])
        public = await store.list_subscriptions(self.pid, 100)
        self.assertNotIn("remote_identity", public[0])

    async def test_old_writer_after_migration_keeps_legacy_and_unique_names(self):
        await self.sql.query(self.migration, exec=True)
        old = PartnerCommerceStore(lambda: self.sql)
        order = await self.order(old)
        raw = await self.sql.fetchval(
            "SELECT remote_identity FROM wl_subscriptions WHERE id=$1",
            order["subscription_id"],
        )
        identity = json.loads(raw) if isinstance(raw, str) else raw
        self.assertEqual(identity["version"], 1)
        store = PartnerCommerceStore(lambda: self.sql, panel_identity_enabled=True)
        second = await self.order(store, tg=200)
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(DISTINCT remote_identity->>'username') FROM wl_subscriptions"
            ),
            2,
        )
        self.assertNotEqual(order["subscription_id"], second["subscription_id"])


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PG/WASM not configured"
)
class PanelIdentityIntegrationPostgresTests(unittest.IsolatedAsyncioTestCase):
    subscription = fixtures.EntitlementPostgresTests.subscription
    pay = fixtures.EntitlementPostgresTests.pay
    complete = fixtures.EntitlementPostgresTests.complete

    async def asyncSetUp(self):
        await fixtures.EntitlementPostgresTests.asyncSetUp(self)
        for name in ("traffic.sql", "subscriptions.sql", "panel_identity.sql"):
            await self.sql.query(
                (fixtures.ROOT / "ops/partner-platform" / name).read_text(), exec=True
            )
        self.store = PartnerCommerceStore(
            lambda: self.sql,
            fixtures.quotes,
            entitlements_enabled=True,
            traffic_cycles_enabled=True,
            panel_identity_enabled=True,
        )

    async def test_public_page_and_client_headers_never_expose_private_panel_snapshot(
        self,
    ):
        sub = await self.subscription(100000123456)
        context = await self.store.get_subscription_link_context(
            self.pid, sub["tg_id"], sub["id"]
        )
        self.assertEqual(context["remote_identity"]["telegram_id"], sub["tg_id"])
        self.assertEqual(
            context["remote_username"], context["remote_identity"]["username"]
        )
        links = PartnerSubscriptionLinks(
            PartnerLinkSettings(
                "sub.example.test", b"isolated-identity-test-signing-32b"
            )
        )
        quota = {
            "used_bytes": 0,
            "total_bytes": 300 * 1024**3,
            "remaining_bytes": 300 * 1024**3,
        }
        page = render_subscription_page(context, links, "ready", quota)
        client_headers = profile_headers(context, links, quota)
        for output in (page.body.decode(), json.dumps(client_headers)):
            for private in (
                str(sub["tg_id"]),
                context["remote_username"],
                context["subscription_url"],
                context["remote_identity"]["tag"],
                "SPN-WL:",
                "remote_identity",
            ):
                self.assertNotIn(private, output)

    async def test_paid_renewal_traffic_cycle_and_activation_lease_keep_same_identity(
        self,
    ):
        sub = await self.subscription()
        first = await self.store.get_subscription_link_context(self.pid, 100, sub["id"])
        await self.subscription(now=NOW + timedelta(days=1))
        await self.sql.execute(
            "UPDATE wl_projects SET title='Renamed VPN',bot_username='RenamedBot' WHERE id=$1",
            self.pid,
        )
        when = NOW + timedelta(days=30, seconds=1)
        context = await self.store.get_traffic_context(self.pid, sub["id"], now=when)
        self.assertEqual(context["remote_identity"], first["remote_identity"])
        observation = PartnerTrafficUsage(
            self.pid,
            sub["id"],
            "remote-1",
            200 * 1024**3,
            context["traffic_limit_bytes"],
            when,
        )
        async with self.store.acquire_activation_lock(self.pid, sub["id"]) as lease:
            self.assertTrue(lease.panel_identity_enabled)
            job = await lease.prepare_traffic_cycle(context, observation, now=when)
            activation = await lease.get_entitlement_activation(self.pid, job["job_id"])
        self.assertEqual(activation["remote_identity"], first["remote_identity"])
        self.assertEqual(activation["remote_username"], first["remote_username"])
