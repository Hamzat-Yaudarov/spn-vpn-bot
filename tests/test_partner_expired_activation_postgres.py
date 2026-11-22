"""Expired immutable grants stop for review without another grant or charge."""

import os
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import test_partner_traffic_postgres as fixtures
from services.partner_checkout import PartnerCheckoutService
from services.partner_commerce_store import CommerceConflict, SalesUnavailable

NOW = fixtures.NOW


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class ExpiredActivationTests(unittest.IsolatedAsyncioTestCase):
    subscription = fixtures.TrafficPostgresTests.subscription
    pay = fixtures.TrafficPostgresTests.pay
    complete = fixtures.TrafficPostgresTests.complete

    async def asyncSetUp(self):
        await fixtures.TrafficPostgresTests.asyncSetUp(self)
        await self.sql.query(
            (fixtures.ROOT / "ops/partner-platform/operations.sql").read_text(),
            exec=True,
        )

    async def test_expired_paid_order_flag_and_fifo_stop_without_new_invoice_or_remote_write(
        self,
    ):
        await self.store.register_customer(self.pid, 100)
        order = await self.store.create_order(self.pid, 100, str(uuid4()), 19900)
        await self.pay(order)
        remote = AsyncMock()
        checkout = PartnerCheckoutService(
            self.store, provisioner=remote, clock=lambda: NOW + timedelta(days=31)
        )
        result = await checkout.reconcile(self.pid, 100, order["id"])
        self.assertEqual(result["activation_issue"], "activation_expired")
        self.assertEqual(result["status"], "paid_pending_activation")
        remote.ensure_subscription.assert_not_awaited()
        self.assertEqual(
            await self.store.list_recoverable_orders(now=NOW + timedelta(days=31)), []
        )
        with self.assertRaises(CommerceConflict):
            await self.store.get_activation(self.pid, order["id"])
        with self.assertRaises(SalesUnavailable):
            await self.store.create_order(
                self.pid,
                100,
                str(uuid4()),
                19900,
                subscription_id=order["subscription_id"],
            )
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 1)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1
        )
        await self.sql.query(
            (fixtures.ROOT / "ops/partner-platform/entitlements.sql").read_text(),
            exec=True,
        )
        self.assertEqual(
            (await self.store.get_order(self.pid, 100, order["id"]))[
                "activation_issue"
            ],
            "activation_expired",
        )

    async def test_unexpired_job_cannot_be_marked_complete_or_expired_by_wrong_uuid(
        self,
    ):
        await self.store.register_customer(self.pid, 100)
        order = await self.store.create_order(self.pid, 100, str(uuid4()), 19900)
        await self.pay(order)
        activation = await self.store.get_activation(self.pid, order["id"])
        with self.assertRaises(CommerceConflict):
            await self.store.mark_activation_expired(
                self.pid, activation["job_id"], now=NOW
            )
        self.assertIsNone(
            (await self.store.get_order(self.pid, 100, order["id"]))["activation_issue"]
        )
