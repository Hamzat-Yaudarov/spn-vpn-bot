"""Explicit one-pass partner bonus/device-expiry recovery, disabled by default.

No loop starts on import. The host must opt into the additive entitlement
migration and inject a provisioner. No payment is synthesized by this worker.
All jobs share the subscription's durable PostgreSQL lock and sequence with
paid activation, so an older bonus/add-on cannot overwrite a newer purchase.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from services.partner_checkout import validate_provisioned_subscription


class PartnerEntitlementService:
    def __init__(self, store, provisioner=None, *, clock=None):
        self.store = store
        self.provisioner = provisioner
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def ready(self):
        return bool(
            getattr(self.store, "entitlements_enabled", False)
            and self.provisioner is not None
        )

    async def activate(self, project_id, subscription_id, job_id):
        if not self.ready:
            return "disabled"
        try:
            async with self.store.acquire_activation_lock(
                project_id, subscription_id
            ) as lease:
                active_store = lease or self.store
                job = await active_store.get_entitlement_activation(project_id, job_id)
                if job["subscription_id"] != str(subscription_id):
                    raise ValueError("Entitlement identity mismatch")
                if job.get("status") == "complete":
                    return "complete"
                if (
                    getattr(active_store, "traffic_cycles_enabled", False)
                    and job["expires_at"] <= self.clock()
                ):
                    await active_store.mark_activation_expired(
                        project_id, job_id, now=self.clock()
                    )
                    return "pending"
                remote = await self.provisioner.ensure_subscription(job)
                validate_provisioned_subscription(remote, job)
                await active_store.complete_entitlement_activation(
                    project_id,
                    job_id,
                    remote_user_id=remote.remote_user_id,
                    subscription_url=remote.subscription_url,
                )
            return "complete"
        except (Exception, asyncio.CancelledError) as error:
            # Always leave the immutable target for recovery. Do not replace a
            # failed bonus with a new job or recalculate another +7 days.
            await self.store.mark_entitlement_activation_retry(
                project_id,
                job_id,
                **(
                    {"now": self.clock()}
                    if getattr(self.store, "traffic_cycles_enabled", False)
                    else {}
                ),
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            return "pending"

    async def recover_once(self, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Некорректный размер обработки.")
        counts = {
            "enabled": self.ready,
            "queued": 0,
            "checked": 0,
            "complete": 0,
            "pending": 0,
            "errors": 0,
        }
        if not self.ready:
            return counts
        now = self.clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("Время обработки должно содержать часовой пояс.")
        now = now.astimezone(timezone.utc)
        # Each enqueue operation is independently transactional/idempotent.
        # A crash between phases loses no earned bank or paid entitlement.
        rows = await self.store.list_bonus_customers(limit=limit)
        for row in rows:
            try:
                job = await self.store.queue_referral_bonus_activation(
                    row["project_id"],
                    row["tg_id"],
                    now=now,
                )
                counts["queued"] += bool(job)
            except Exception:
                counts["errors"] += 1
        expired = await self.store.queue_expired_device_activations(
            now=now, limit=limit
        )
        counts["queued"] += len(expired)
        jobs = await self.store.list_entitlement_activations(
            limit=limit,
            **(
                {"now": now}
                if getattr(self.store, "traffic_cycles_enabled", False)
                else {}
            ),
        )
        for job in jobs:
            counts["checked"] += 1
            try:
                result = await self.activate(
                    job["project_id"], job["subscription_id"], job["job_id"]
                )
                counts["complete" if result == "complete" else "pending"] += 1
            except Exception:
                counts["errors"] += 1
        return counts
