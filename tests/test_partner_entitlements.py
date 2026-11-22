"""Recovery uses only injected local ports; no network or live database."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from test_partner_checkout import FakeProvider, FakeProvisioner, FakeStore, NOW
from services.partner_checkout import (
    CheckoutError,
    PartnerCheckoutService,
    ProvisionedSubscription,
    public_order,
    validate_provisioned_subscription,
)
from services.partner_entitlements import PartnerEntitlementService


class EntitlementWorkerStore:
    entitlements_enabled = True

    def __init__(self):
        self.pid, self.sid, self.jid = map(str, (uuid4(), uuid4(), uuid4()))
        self.job = {
            "project_id": self.pid,
            "subscription_id": self.sid,
            "job_id": self.jid,
            "status": "pending",
            "remote_username": "wl_" + self.sid.replace("-", ""),
            "remote_user_id": "remote-1",
            "kind": "referral_bonus",
            "expires_at": NOW + timedelta(days=37),
            "traffic_limit_bytes": 300 * 1024**3,
            "device_limit": 3,
        }
        self.list_bonus_customers = AsyncMock(
            return_value=[{"project_id": self.pid, "tg_id": 42}]
        )
        self.queue_referral_bonus_activation = AsyncMock(return_value=dict(self.job))
        self.queue_expired_device_activations = AsyncMock(return_value=[])
        self.list_entitlement_activations = AsyncMock(return_value=[self.job])
        self.mark_entitlement_activation_retry = AsyncMock()
        self.in_lock = False
        self.fail_complete = False
        self.completed = 0

    @asynccontextmanager
    async def acquire_activation_lock(self, pid, sid):
        assert (pid, sid) == (self.pid, self.sid)
        self.in_lock = True
        try:
            yield self
        finally:
            self.in_lock = False

    async def get_entitlement_activation(self, pid, jid):
        assert self.in_lock and (pid, jid) == (self.pid, self.jid)
        return dict(self.job)

    async def complete_entitlement_activation(self, pid, jid, **kwargs):
        assert self.in_lock and kwargs["remote_user_id"] == "remote-1"
        if self.fail_complete:
            self.fail_complete = False
            raise RuntimeError("simulated lost local commit")
        self.job["status"] = "complete"
        self.completed += 1


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_remote_apply_preserves_job_and_releases_lease(self):
        store, provider = EntitlementWorkerStore(), FakeProvisioner()
        started = asyncio.Event()

        async def slow(activation):
            started.set()
            await asyncio.sleep(10)

        provider.ensure_subscription = slow
        service = PartnerEntitlementService(store, provider, clock=lambda: NOW)
        task = asyncio.create_task(service.activate(store.pid, store.sid, store.jid))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        store.mark_entitlement_activation_retry.assert_awaited_once_with(
            store.pid, store.jid
        )
        self.assertFalse(store.in_lock)
        self.assertEqual(store.job["status"], "pending")

    async def test_default_disabled_does_not_queue_or_call_remote(self):
        store = EntitlementWorkerStore()
        provider = FakeProvisioner()
        service = PartnerEntitlementService(store)
        self.assertFalse((await service.recover_once())["enabled"])
        store.list_bonus_customers.assert_not_awaited()
        store.entitlements_enabled = False
        self.assertEqual(
            await PartnerEntitlementService(store, provider).activate(
                store.pid, store.sid, store.jid
            ),
            "disabled",
        )
        self.assertEqual(provider.applied, [])

    async def test_one_pass_uses_shared_lease_and_no_repeat_complete(self):
        store, provider = EntitlementWorkerStore(), FakeProvisioner()
        service = PartnerEntitlementService(store, provider, clock=lambda: NOW)
        result = await service.recover_once()
        self.assertEqual(
            (result["queued"], result["complete"], result["errors"]), (1, 1, 0)
        )
        await service.activate(store.pid, store.sid, store.jid)
        self.assertEqual(store.completed, 1)
        self.assertEqual(len(provider.applied), 1)
        self.assertFalse(store.in_lock)

    async def test_lost_commit_retries_identical_snapshot_no_second_bonus(self):
        store, provider = EntitlementWorkerStore(), FakeProvisioner()
        store.fail_complete = True
        service = PartnerEntitlementService(store, provider, clock=lambda: NOW)
        self.assertEqual(
            await service.activate(store.pid, store.sid, store.jid), "pending"
        )
        store.mark_entitlement_activation_retry.assert_awaited_once_with(
            store.pid, store.jid
        )
        self.assertEqual(
            await service.activate(store.pid, store.sid, store.jid), "complete"
        )
        self.assertEqual(provider.applied[0], provider.applied[1])

    async def test_failed_provisioner_keeps_job_and_redacts_internal_errors(self):
        store, provider = EntitlementWorkerStore(), FakeProvisioner()
        provider.fail_once = True
        result = await PartnerEntitlementService(
            store, provider, clock=lambda: NOW
        ).recover_once()
        self.assertEqual((result["pending"], result["complete"]), (1, 0))
        self.assertNotIn("private", str(result))
        self.assertEqual(store.job["status"], "pending")
        self.assertFalse(store.in_lock)

    async def test_invalid_limit_never_starts_work(self):
        store = EntitlementWorkerStore()
        service = PartnerEntitlementService(store, FakeProvisioner())
        for limit in (True, 0, 101, "50"):
            with self.assertRaises(ValueError):
                await service.recover_once(limit=limit)
        store.list_bonus_customers.assert_not_awaited()


class CheckoutAddonTests(unittest.IsolatedAsyncioTestCase):
    async def test_addon_wrappers_use_trusted_store_quote_and_clock(self):
        store = FakeStore()
        store.get_addon_catalog = AsyncMock(
            return_value={"devices": {"addon_status": "available"}}
        )
        store.create_addon_order = AsyncMock(
            return_value={**store.row, "purpose": "devices"}
        )
        service = PartnerCheckoutService(store, clock=lambda: NOW)
        await service.quote_addons(store.project, 42, store.sub)
        store.get_addon_catalog.assert_awaited_once_with(
            store.project, 42, store.sub, now=NOW
        )
        key = str(uuid4())
        result = await service.prepare_addon_order(
            store.project,
            42,
            key,
            1000,
            subscription_id=store.sub,
            purpose="devices",
            package_id="1",
        )
        self.assertEqual(result["purpose"], "devices")
        store.create_addon_order.assert_awaited_once_with(
            store.project,
            42,
            key,
            1000,
            subscription_id=store.sub,
            purpose="devices",
            package_id="1",
            now=NOW,
        )

    async def test_paused_project_preflight_prevents_new_invoice_but_not_existing_lookup(
        self,
    ):
        store = FakeStore()
        provider = FakeProvider(store)
        store.get_checkout_order = AsyncMock(side_effect=LookupError("paused"))
        service = PartnerCheckoutService(store, provider, clock=lambda: NOW)
        with self.assertRaises(LookupError):
            await service.create_checkout(store.project, 42, store.order_id)
        self.assertEqual(provider.created, [])
        store.row["provider_payment_id"] = provider.payment.id
        await service.reconcile(store.project, 42, store.order_id)
        self.assertEqual(provider.checked, [provider.payment.id])

    async def test_unmatched_remote_identity_cannot_complete(self):
        activation = {"remote_username": "wl_123", "remote_user_id": "remote-1"}
        with self.assertRaises(CheckoutError):
            validate_provisioned_subscription(
                ProvisionedSubscription(
                    "remote-2", "wl_123", "https://sub.example/own"
                ),
                activation,
            )

    async def test_manual_addon_issue_not_retried_or_offered_for_payment(self):
        store = FakeStore()
        store.row.update(
            status="paid_pending_activation",
            activation_issue="addon_expired",
            purpose="devices",
        )
        provider = FakeProvisioner()
        result = await PartnerCheckoutService(store, provisioner=provider).reconcile(
            store.project, 42, store.order_id
        )
        self.assertEqual(result["activation_issue"], "addon_expired")
        self.assertIn("поддержкой", result["message"])
        self.assertNotIn("confirmation_url", result)
        self.assertEqual(provider.applied, [])
        self.assertNotIn(
            "activation_issue",
            public_order({**store.row, "activation_issue": "private-url-or-token"}),
        )
