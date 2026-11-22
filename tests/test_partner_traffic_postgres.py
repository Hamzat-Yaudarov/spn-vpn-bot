"""Paid-period SQL tests on optional isolated PostgreSQL/WASM, no live data."""

from datetime import timedelta
import os
import unittest
from uuid import uuid4

import test_partner_entitlements_postgres as fixtures
from services.partner_commerce_store import PartnerCommerceStore, CommerceConflict
from services.partner_traffic import PartnerTrafficUsage, TrafficStateError
from services.partner_traffic import PartnerTrafficService
from services.partner_checkout import PartnerCheckoutService, ProvisionedSubscription
from services.partner_entitlements import PartnerEntitlementService
from services.partner_recovery import PartnerRecoveryService

GB = 1024**3
ROOT, NOW, quotes = fixtures.ROOT, fixtures.NOW, fixtures.quotes


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class TrafficPostgresTests(unittest.IsolatedAsyncioTestCase):
    subscription = fixtures.EntitlementPostgresTests.subscription
    pay = fixtures.EntitlementPostgresTests.pay
    complete = fixtures.EntitlementPostgresTests.complete
    addon = fixtures.EntitlementPostgresTests.addon

    async def asyncSetUp(self):
        await fixtures.EntitlementPostgresTests.asyncSetUp(self)
        await self.sql.query(
            (ROOT / "ops/partner-platform/traffic.sql").read_text(), exec=True
        )
        self.store = PartnerCommerceStore(
            lambda: self.sql,
            quotes,
            entitlements_enabled=True,
            traffic_cycles_enabled=True,
        )

    async def due(self, *, used=305 * GB, when=None):
        when = when or NOW + timedelta(days=30, seconds=1)
        context = await self.store.get_traffic_context(
            self.pid, self.sub["id"], now=when
        )
        observation = PartnerTrafficUsage(
            self.pid,
            self.sub["id"],
            "remote-1",
            used,
            context["traffic_limit_bytes"],
            when,
        )
        return context, observation, when

    async def prepare_two_periods(self, *, addon=True):
        self.sub = await self.subscription()
        if addon:
            order = await self.addon(self.sub)
            await self.pay(order)
            await self.complete(order)
        await self.subscription(now=NOW + timedelta(days=1))

    async def test_first_paid_period_and_prepaid_renewal_no_early_base_or_bonus_quota(
        self,
    ):
        await self.prepare_two_periods(addon=False)
        rows = await self.sql.fetch(
            "SELECT * FROM wl_traffic_credits ORDER BY available_at"
        )
        self.assertEqual([r["state"] for r in rows], ["applied", "waiting"])
        self.assertEqual(rows[1]["available_at"], NOW + timedelta(days=30))
        self.assertEqual(
            await self.store.list_due_traffic_cycles(now=NOW + timedelta(days=29)), []
        )
        await self.store.register_customer(self.pid, 200, 100)
        await self.subscription(200)
        bonus = await self.store.queue_referral_bonus_activation(self.pid, 100, now=NOW)
        await self.store.complete_entitlement_activation(
            self.pid,
            bonus["job_id"],
            remote_user_id="remote-1",
            subscription_url="https://sub.example.test/credential",
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_traffic_credits WHERE subscription_id=$1",
                self.sub["id"],
            ),
            2,
        )
        self.assertEqual(
            (await self.store.list_subscriptions(self.pid, 100))[0][
                "traffic_limit_bytes"
            ],
            300 * GB,
        )

    async def test_300gb_plus_remaining_paid_committed_once_before_remote_and_survives_restart(
        self,
    ):
        await self.prepare_two_periods()
        context, observation, when = await self.due()
        async with self.store.acquire_activation_lock(
            self.pid, self.sub["id"]
        ) as lease:
            self.assertTrue(lease.traffic_cycles_enabled)
            result = await lease.prepare_traffic_cycle(context, observation, now=when)
        self.assertEqual(
            (result["remaining_paid_bytes"], result["new_limit_bytes"]),
            (5 * GB, 610 * GB),
        )
        duplicate = await self.store.prepare_traffic_cycle(
            context, observation, now=when
        )
        self.assertEqual(duplicate["job_id"], result["job_id"])
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_activation_jobs WHERE kind='traffic_cycle'"
            ),
            1,
        )
        job = await self.store.get_entitlement_activation(self.pid, result["job_id"])
        self.assertEqual(
            (job["traffic_limit_bytes"], job["usage_floor_bytes"]), (610 * GB, 305 * GB)
        )
        await self.store.mark_entitlement_activation_retry(self.pid, result["job_id"])
        restarted = PartnerCommerceStore(
            lambda: self.sql,
            quotes,
            entitlements_enabled=True,
            traffic_cycles_enabled=True,
        )
        self.assertEqual(
            (await restarted.get_entitlement_activation(self.pid, result["job_id"]))[
                "traffic_limit_bytes"
            ],
            610 * GB,
        )
        await restarted.complete_entitlement_activation(
            self.pid,
            result["job_id"],
            remote_user_id="remote-1",
            subscription_url="https://sub.example.test/credential",
        )
        self.assertEqual(await restarted.list_due_traffic_cycles(now=when), [])
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 3
        )  # No new sale from a reset.

    async def test_no_usage_read_until_all_earlier_paid_jobs_complete(self):
        self.sub = await self.subscription()
        order = await self.store.create_order(
            self.pid, 100, str(uuid4()), 19900, subscription_id=self.sub["id"]
        )
        await self.pay(order, now=NOW + timedelta(days=1))
        self.assertIsNone(
            await self.store.get_traffic_context(
                self.pid, self.sub["id"], now=NOW + timedelta(days=30)
            )
        )
        self.assertEqual(
            await self.store.list_due_traffic_cycles(now=NOW + timedelta(days=30)), []
        )

    async def test_concurrent_paid_gb_changes_context_and_never_loses_purchase(self):
        await self.prepare_two_periods()
        context, observation, when = await self.due()
        extra = await self.addon(self.sub, now=when)
        await self.pay(extra, now=when)
        with self.assertRaises(CommerceConflict):
            await self.store.prepare_traffic_cycle(context, observation, now=when)
        state = (await self.store.list_subscriptions(self.pid, 100))[0]
        self.assertEqual(
            (state["traffic_paid_credit_bytes"], state["traffic_limit_bytes"]),
            (20 * GB, 320 * GB),
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_activation_jobs WHERE kind='traffic_cycle'"
            ),
            0,
        )

    async def test_external_counter_reset_and_long_missed_boundary_hold_without_mutation(
        self,
    ):
        await self.prepare_two_periods()
        context, observation, when = await self.due()
        await self.sql.execute(
            "UPDATE wl_subscriptions SET traffic_last_used_bytes=$2 WHERE id=$1",
            self.sub["id"],
            100 * GB,
        )
        context = await self.store.get_traffic_context(
            self.pid, self.sub["id"], now=when
        )
        bad = PartnerTrafficUsage(
            self.pid, self.sub["id"], "remote-1", 0, 310 * GB, when
        )
        result = await self.store.prepare_traffic_cycle(context, bad, now=when)
        self.assertEqual(result, {"hold": "counter_changed"})
        self.assertEqual(
            (await self.store.list_subscriptions(self.pid, 100))[0][
                "traffic_limit_bytes"
            ],
            310 * GB,
        )
        # Explicit isolated fixture repair only, not a public operator API.
        await self.sql.execute(
            "UPDATE wl_subscriptions SET traffic_cycle_hold=NULL,traffic_last_used_bytes=0 WHERE id=$1",
            self.sub["id"],
        )
        late = NOW + timedelta(days=31)
        context, observation, _ = await self.due(when=late)
        result = await self.store.prepare_traffic_cycle(context, observation, now=late)
        self.assertEqual(result, {"hold": "late_observation"})
        self.assertEqual(
            (await self.store.list_subscriptions(self.pid, 100))[0][
                "traffic_paid_credit_bytes"
            ],
            10 * GB,
        )

    async def test_wrong_identity_and_stale_observation_cannot_prepare(self):
        await self.prepare_two_periods()
        context, observation, when = await self.due()
        for changes in (
            {"project_id": str(uuid4())},
            {"remote_user_id": "foreign"},
            {"observed_at": when - timedelta(minutes=2)},
        ):
            values = dict(observation.__dict__)
            values.update(changes)
            with self.assertRaises(TrafficStateError):
                await self.store.prepare_traffic_cycle(
                    context, PartnerTrafficUsage(**values), now=when
                )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_activation_jobs WHERE kind='traffic_cycle'"
            ),
            0,
        )
        await self.store.defer_traffic_cycle(self.pid, self.sub["id"], now=when)
        self.assertEqual(await self.store.list_due_traffic_cycles(now=when), [])
        self.assertEqual(
            len(
                await self.store.list_due_traffic_cycles(
                    now=when + timedelta(seconds=31)
                )
            ),
            1,
        )

    async def test_cycle_snapshot_and_credit_rollback_together_if_commit_path_fails(
        self,
    ):
        await self.prepare_two_periods()
        context, observation, when = await self.due()
        execute = self.sql.execute

        async def fail_credit(sql, *args):
            if "UPDATE wl_traffic_credits SET state='prepared'" in sql:
                raise RuntimeError("local injected failure")
            return await execute(sql, *args)

        self.sql.execute = fail_credit
        with self.assertRaises(RuntimeError):
            await self.store.prepare_traffic_cycle(context, observation, now=when)
        self.sql.execute = execute
        sub = (await self.store.list_subscriptions(self.pid, 100))[0]
        self.assertEqual(
            (
                sub["traffic_cycle_baseline_bytes"],
                sub["traffic_paid_credit_bytes"],
                sub["traffic_limit_bytes"],
            ),
            (0, 10 * GB, 310 * GB),
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_activation_jobs WHERE kind='traffic_cycle'"
            ),
            0,
        )

    async def test_migration_reapply_keeps_existing_tracked_quota_and_marks_old_untracked(
        self,
    ):
        self.sub = await self.subscription()
        await self.sql.query(
            (ROOT / "ops/partner-platform/traffic.sql").read_text(), exec=True
        )
        tracked = (await self.store.list_subscriptions(self.pid, 100))[0]
        self.assertIsNone(tracked["traffic_cycle_hold"])
        await self.sql.execute(
            "UPDATE wl_subscriptions SET traffic_cycle_baseline_bytes=NULL,traffic_cycle_started_at=NULL WHERE id=$1",
            self.sub["id"],
        )
        await self.sql.query(
            (ROOT / "ops/partner-platform/traffic.sql").read_text(), exec=True
        )
        legacy = (await self.store.list_subscriptions(self.pid, 100))[0]
        self.assertEqual(
            (legacy["traffic_cycle_hold"], legacy["traffic_limit_bytes"]),
            ("legacy_review", 300 * GB),
        )

    async def test_actual_recovery_pass_reads_prepares_applies_and_retries_saved_quota_once(
        self,
    ):
        await self.prepare_two_periods()
        when = NOW + timedelta(days=30, seconds=1)

        class Remote:
            def __init__(self):
                self.reads = 0
                self.applied = []
                self.fail = True

            async def read_usage(self, context):
                self.reads += 1
                return PartnerTrafficUsage(
                    context["project_id"],
                    context["subscription_id"],
                    "remote-1",
                    305 * GB,
                    310 * GB,
                    when,
                )

            async def ensure_subscription(self, activation):
                self.applied.append(dict(activation))
                if self.fail:
                    self.fail = False
                    raise TimeoutError("test ambiguous remote response")
                return ProvisionedSubscription(
                    "remote-1",
                    activation["remote_username"],
                    "https://sub.example.test/credential",
                )

        remote = Remote()
        checkout = PartnerCheckoutService(
            self.store, provisioner=remote, clock=lambda: when
        )
        entitlements = PartnerEntitlementService(self.store, remote, clock=lambda: when)
        traffic = PartnerTrafficService(self.store, remote, clock=lambda: when)
        worker = PartnerRecoveryService(
            self.store, checkout, entitlements, traffic, enabled=True
        )
        result = await worker.recover_once(limit=5)
        self.assertEqual(
            (result["due_traffic"]["prepared"], result["entitlements"]["pending"]),
            (1, 1),
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_traffic_credits WHERE state='prepared'"
            ),
            1,
        )
        pending = await self.sql.fetchrow(
            "SELECT * FROM wl_activation_jobs WHERE kind='traffic_cycle'"
        )
        self.assertGreater(pending["next_attempt_at"], when)
        await worker.recover_once(limit=5)
        self.assertEqual(len(remote.applied), 1)
        when += timedelta(seconds=31)
        await worker.recover_once(limit=5)
        self.assertEqual(remote.reads, 1)
        target_keys = (
            "project_id",
            "subscription_id",
            "remote_user_id",
            "remote_username",
            "expires_at",
            "traffic_limit_bytes",
            "device_limit",
            "usage_floor_bytes",
        )
        self.assertEqual(
            {k: remote.applied[0][k] for k in target_keys},
            {k: remote.applied[1][k] for k in target_keys},
        )
        self.assertEqual(remote.applied[-1]["traffic_limit_bytes"], 610 * GB)
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_traffic_credits WHERE state='prepared'"
            ),
            0,
        )
        self.assertEqual((await self.store.traffic_health())["prepared_cycles"], 0)
        await self.sql.query(
            (ROOT / "ops/partner-platform/entitlements.sql").read_text(), exec=True
        )
        await self.sql.query(
            (ROOT / "ops/partner-platform/traffic.sql").read_text(), exec=True
        )
