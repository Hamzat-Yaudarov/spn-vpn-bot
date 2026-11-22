"""Historical blocked add-on jobs must not disable an already paid base.

Actual isolated PostgreSQL/WASM plus mocked read-only panel/source ports. No
network, provider mutation, quota reset, cancellation or financial recomputation.
"""

from datetime import timedelta
import os
import unittest
from unittest.mock import AsyncMock

import test_partner_entitlements_postgres as fixtures
from services.partner_commerce_store import PartnerCommerceStore
from services.partner_subscriptions import (
    PartnerLinkSettings,
    PartnerSubscriptionLinks,
    PartnerSubscriptionService,
    SubscriptionPayload,
    SubscriptionUnavailable,
)
from services.partner_traffic import PartnerTrafficUsage


NOW, ROOT, GB = fixtures.NOW, fixtures.ROOT, 1024**3


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class BlockedAddonGatewayPostgresTests(unittest.IsolatedAsyncioTestCase):
    subscription = fixtures.EntitlementPostgresTests.subscription
    pay = fixtures.EntitlementPostgresTests.pay
    complete = fixtures.EntitlementPostgresTests.complete
    addon = fixtures.EntitlementPostgresTests.addon

    async def asyncSetUp(self):
        await fixtures.EntitlementPostgresTests.asyncSetUp(self)
        for name in ("traffic.sql", "subscriptions.sql"):
            await self.sql.query(
                (ROOT / "ops/partner-platform" / name).read_text(), exec=True
            )
        self.store = PartnerCommerceStore(
            lambda: self.sql,
            fixtures.quotes,
            entitlements_enabled=True,
            traffic_cycles_enabled=True,
        )

    async def gateway_with_historical_blocker(self, reason):
        sub = await self.subscription()
        addon = await self.addon(sub, purpose="devices", package="1", price=10000)
        await self.subscription(now=NOW + timedelta(days=1))
        when = NOW + timedelta(days=30, seconds=1)

        # Apply the next legitimately paid base period first. The stale device
        # quote still expires at day 30, while the base subscription lasts 60.
        context = await self.store.get_traffic_context(self.pid, sub["id"], now=when)
        cycle = await self.store.prepare_traffic_cycle(
            context,
            PartnerTrafficUsage(self.pid, sub["id"], "remote-1", 0, 300 * GB, when),
            now=when,
        )
        await self.store.get_entitlement_activation(self.pid, cycle["job_id"])
        await self.store.complete_entitlement_activation(
            self.pid,
            cycle["job_id"],
            remote_user_id="remote-1",
            subscription_url="https://sub.example.test/credential",
        )
        paid = await self.pay(addon, now=when)
        self.assertEqual(paid["activation_issue"], "addon_expired")
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_device_grants"), 0
        )

        # Older platform versions may retain an explicitly blocked activation
        # row for this paid/manual-review add-on. Preserve the row and ledger;
        # both supported add-on block reasons are non-granting historical facts.
        await self.sql.execute(
            "UPDATE wl_orders SET activation_issue=$2 WHERE id=$1", addon["id"], reason
        )
        await self.sql.execute(
            """INSERT INTO wl_activation_jobs
            (project_id,order_id,subscription_id,expires_at,traffic_limit_bytes,
             device_limit,status,blocked_reason)
            VALUES($1,$2,$3,$4,$5,3,'retry',$6)""",
            self.pid,
            addon["id"],
            sub["id"],
            NOW + timedelta(days=60),
            300 * GB,
            reason,
        )
        links = PartnerSubscriptionLinks(
            PartnerLinkSettings(
                "sub.example.test", b"isolated-blocked-addon-sign-key-32"
            )
        )
        context = await self.store.get_subscription_link_context(
            self.pid, 100, sub["id"]
        )
        self.assertFalse(context["activation_pending"])
        source = AsyncMock()
        source.fetch.return_value = SubscriptionPayload(
            b'{"outbounds":[{"tag":"unchanged-config"}]}',
            "application/json",
            {
                "subscription-userinfo": f"upload=0; download=123; total={300*GB}; expire={int(context['expires_at'].timestamp())}"
            },
        )
        provisioner = AsyncMock()
        provisioner.read_public_usage.return_value = PartnerTrafficUsage(
            self.pid, sub["id"], "remote-1", 123, 300 * GB, when
        )
        service = PartnerSubscriptionService(
            self.store,
            links,
            provisioner=provisioner,
            source=source,
            enabled=True,
            clock=lambda: when,
        )
        return sub, addon, links, context, service, source

    async def assert_base_config_survives(self, reason):
        sub, addon, links, context, service, source = (
            await self.gateway_with_historical_blocker(reason)
        )
        client = {"x-hwid": "device-abc", "user-agent": "Happ/1.0"}
        payload = await service.config("entitlement-vpn", links.token(context), client)
        self.assertEqual(payload.body, source.fetch.return_value.body)
        self.assertEqual(payload.status, 200)
        source.fetch.assert_awaited_once_with(context["subscription_url"], client)
        _, state, quota = await service.page("entitlement-vpn", links.token(context))
        self.assertEqual(state, "ready")
        self.assertEqual(quota["used_bytes"], 123)
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT traffic_last_used_bytes FROM wl_subscriptions WHERE id=$1",
                sub["id"],
            ),
            123,
        )
        job = await self.sql.fetchrow(
            "SELECT status,blocked_reason FROM wl_activation_jobs WHERE order_id=$1",
            addon["id"],
        )
        self.assertEqual((job["status"], job["blocked_reason"]), ("retry", reason))
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_revenue_ledger WHERE order_id=$1", addon["id"]
            ),
            1,
        )
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_device_grants"), 0
        )

    async def test_addon_expired_blocker_preserves_paid_base_page_and_config(self):
        await self.assert_base_config_survives("addon_expired")

    async def test_device_limit_blocker_preserves_paid_base_page_and_config(self):
        await self.assert_base_config_survives("device_limit")

    async def test_activation_expired_still_blocks_base_gateway(self):
        _, addon, links, context, service, source = (
            await self.gateway_with_historical_blocker("addon_expired")
        )
        await self.sql.execute(
            "UPDATE wl_activation_jobs SET blocked_reason='activation_expired' WHERE order_id=$1",
            addon["id"],
        )
        with self.assertRaises(SubscriptionUnavailable):
            await self.store.remember_public_usage(
                context,
                service.provisioner.read_public_usage.return_value,
                now=service.clock(),
            )
        with self.assertRaises(SubscriptionUnavailable):
            await service.config("entitlement-vpn", links.token(context), {})
        source.fetch.assert_not_awaited()
