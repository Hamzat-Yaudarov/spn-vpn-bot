"""Pure quota policy and mocked worker, never working Remnawave/DB."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from services.partner_traffic import (
    BASE_BYTES,
    MAX_BYTES,
    PartnerTrafficService,
    PartnerTrafficUsage,
    TrafficStateError,
    next_traffic_cycle,
)

GB = 1024**3
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


class QuotaPolicyTests(unittest.TestCase):
    def calculate(self, used, *, baseline=0, paid=100 * GB, last=0):
        return next_traffic_cycle(
            baseline=baseline,
            paid_credit=paid,
            previous_limit=baseline + BASE_BYTES + paid,
            last_used=last,
            used=used,
        )

    def test_base_used_first_paid_remainder_preserved_exactly(self):
        for used, remaining in ((0, 100), (100, 100), (300, 100), (350, 50), (400, 0)):
            with self.subTest(used=used):
                result = self.calculate(used * GB)
                self.assertEqual(result["remaining_paid_bytes"], remaining * GB)
                self.assertEqual(
                    result["new_limit_bytes"] - used * GB, (300 + remaining) * GB
                )

    def test_unused_base_does_not_stack_and_nonzero_baseline_accounted(self):
        self.assertEqual(self.calculate(0, paid=0)["new_limit_bytes"], BASE_BYTES)
        result = self.calculate(
            950 * GB, baseline=600 * GB, paid=100 * GB, last=900 * GB
        )
        self.assertEqual(
            (result["remaining_paid_bytes"], result["new_limit_bytes"]),
            (50 * GB, 1300 * GB),
        )

    def test_counter_rollback_missing_bool_overshoot_and_inconsistent_cap_fail(self):
        for used in (None, True, "0", -1, MAX_BYTES + 1, 401 * GB):
            with self.subTest(used=used), self.assertRaises(TrafficStateError):
                self.calculate(used)
        with self.assertRaises(TrafficStateError):
            self.calculate(5, baseline=10)
        with self.assertRaises(TrafficStateError):
            self.calculate(5, last=10)
        with self.assertRaises(TrafficStateError):
            next_traffic_cycle(
                baseline=0,
                paid_credit=0,
                previous_limit=BASE_BYTES + 1,
                last_used=0,
                used=1,
            )


class TrafficWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_has_no_reads_or_mutations(self):
        store = type(
            "Store",
            (),
            {"traffic_cycles_enabled": False, "list_due_traffic_cycles": AsyncMock()},
        )()
        result = await PartnerTrafficService(store).recover_once()
        self.assertFalse(result["enabled"])
        store.list_due_traffic_cycles.assert_not_awaited()

    async def test_shared_connection_lease_covers_usage_read_and_persisted_target(self):
        pid, sid = str(uuid4()), str(uuid4())
        leased = type("Lease", (), {})()
        leased.get_traffic_context = AsyncMock(
            return_value={"project_id": pid, "subscription_id": sid}
        )
        leased.prepare_traffic_cycle = AsyncMock(return_value={"job_id": str(uuid4())})
        store = type("Store", (), {"traffic_cycles_enabled": True})()
        store.list_due_traffic_cycles = AsyncMock(
            return_value=[{"project_id": pid, "subscription_id": sid}]
        )
        state = {"locked": False}

        @asynccontextmanager
        async def lock(*args):
            state["locked"] = True
            try:
                yield leased
            finally:
                state["locked"] = False

        async def read(context):
            self.assertTrue(state["locked"])
            return PartnerTrafficUsage(pid, sid, "remote-1", 0, BASE_BYTES, NOW)

        store.acquire_activation_lock = lock
        remote = type("Remote", (), {})()
        remote.read_usage = AsyncMock(side_effect=read)
        result = await PartnerTrafficService(
            store, remote, clock=lambda: NOW
        ).recover_once()
        self.assertEqual(
            (result["checked"], result["prepared"], result["errors"]), (1, 1, 0)
        )
        self.assertFalse(state["locked"])
        leased.prepare_traffic_cycle.assert_awaited_once()

    async def test_read_failure_does_not_prepare_an_invented_zero(self):
        store = type("Store", (), {"traffic_cycles_enabled": True})()
        store.list_due_traffic_cycles = AsyncMock(
            return_value=[{"project_id": "p", "subscription_id": "s"}]
        )
        store.get_traffic_context = AsyncMock(return_value={})
        store.prepare_traffic_cycle = AsyncMock()

        @asynccontextmanager
        async def lock(*args):
            yield store

        store.acquire_activation_lock = lock
        remote = type(
            "Remote",
            (),
            {"read_usage": AsyncMock(side_effect=TimeoutError("private-token"))},
        )()
        result = await PartnerTrafficService(
            store, remote, clock=lambda: NOW
        ).recover_once()
        self.assertEqual(result["errors"], 1)
        store.prepare_traffic_cycle.assert_not_awaited()
        self.assertNotIn("private-token", str(result))
