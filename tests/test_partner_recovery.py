"""No background loop is started, and all ports must share the same store/API."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from services.partner_recovery import PartnerRecoveryService


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setup_ports(self):
        store, remote = object(), object()
        checkout = type("Checkout", (), {"store": store, "provisioner": remote})()
        entitlement = type(
            "Entitlement", (), {"store": store, "provisioner": remote, "ready": True}
        )()
        traffic = type(
            "Traffic", (), {"store": store, "provisioner": remote, "ready": True}
        )()
        entitlement.recover_once = AsyncMock(return_value={"complete": 1})
        traffic.recover_once = AsyncMock(return_value={"prepared": 1})
        return store, checkout, entitlement, traffic

    async def test_default_is_inert(self):
        store, checkout, entitlements, traffic = self.setup_ports()
        result = await PartnerRecoveryService(
            store, checkout, entitlements, traffic
        ).recover_once()
        self.assertFalse(result["enabled"])
        entitlements.recover_once.assert_not_awaited()
        traffic.recover_once.assert_not_awaited()

    async def test_mismatched_store_or_panel_is_rejected_before_any_work(self):
        store, checkout, entitlements, traffic = self.setup_ports()
        traffic.provisioner = object()
        with self.assertRaises(ValueError):
            PartnerRecoveryService(store, checkout, entitlements, traffic, enabled=True)

    async def test_ordered_phases_bounded_and_errors_redacted(self):
        store, checkout, entitlements, traffic = self.setup_ports()
        sequence = []

        async def paid(*args, **kwargs):
            sequence.append("paid")
            return {"active": 1}

        async def apply(**kwargs):
            sequence.append("apply")
            return {"complete": 1}

        async def quota(**kwargs):
            sequence.append("quota")
            raise RuntimeError("private token")

        entitlements.recover_once.side_effect = apply
        traffic.recover_once.side_effect = quota
        with patch(
            "services.partner_recovery.recover_partner_orders_once", side_effect=paid
        ):
            result = await PartnerRecoveryService(
                store, checkout, entitlements, traffic, enabled=True
            ).recover_once(limit=2)
        self.assertEqual(sequence, ["quota", "paid", "apply", "quota", "apply", "paid"])
        self.assertEqual(result["phase_errors"], 2)
        self.assertNotIn("private token", str(result))

    async def test_slow_external_phase_is_cancelled_without_starving_other_phases(self):
        store, checkout, entitlements, traffic = self.setup_ports()
        calls = 0

        async def read(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(10)
            return {"prepared": 0}

        traffic.recover_once.side_effect = read
        with patch(
            "services.partner_recovery.recover_partner_orders_once",
            new=AsyncMock(return_value={"active": 0}),
        ):
            result = await PartnerRecoveryService(
                store,
                checkout,
                entitlements,
                traffic,
                enabled=True,
                phase_timeout_seconds=1,
            ).recover_once(limit=1)
        self.assertEqual(result["phase_errors"], 1)
        self.assertEqual(result["traffic"]["prepared"], 0)
        self.assertEqual(entitlements.recover_once.await_count, 2)
