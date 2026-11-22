"""One explicit bounded recovery pass; no scheduler, settings or startup I/O."""

import asyncio

from services.partner_checkout import recover_partner_orders_once


class PartnerRecoveryService:
    def __init__(
        self,
        store,
        checkout,
        entitlements,
        traffic,
        notifications=None,
        *,
        enabled=False,
        phase_timeout_seconds=30,
    ):
        if type(enabled) is not bool:
            raise ValueError("Нужен явный флаг восстановления.")
        if (
            type(phase_timeout_seconds) is not int
            or not 1 <= phase_timeout_seconds <= 120
        ):
            raise ValueError("Некорректный таймаут обработки.")
        self.phase_timeout_seconds = phase_timeout_seconds
        if (
            checkout.store is not store
            or entitlements.store is not store
            or traffic.store is not store
            or checkout.provisioner is not entitlements.provisioner
            or checkout.provisioner is not traffic.provisioner
        ):
            raise ValueError(
                "Выдача, трафик и восстановление должны использовать один контур."
            )
        self.store, self.checkout, self.entitlements, self.traffic = (
            store,
            checkout,
            entitlements,
            traffic,
        )
        self.notifications, self.enabled = notifications, enabled

    @property
    def ready(self):
        return bool(self.enabled and self.entitlements.ready and self.traffic.ready)

    async def recover_once(self, *, limit=25):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Некорректный размер обработки.")
        results = {"enabled": self.ready, "phase_errors": 0}
        if not self.ready:
            return results
        # The second application pass handles newly prepared traffic snapshots;
        # the second paid pass unblocks orders behind older internal jobs.
        phases = [
            ("due_traffic", lambda: self.traffic.recover_once(limit=limit)),
            (
                "payments",
                lambda: recover_partner_orders_once(
                    self.store, self.checkout, limit=limit
                ),
            ),
            ("entitlements", lambda: self.entitlements.recover_once(limit=limit)),
            ("traffic", lambda: self.traffic.recover_once(limit=limit)),
            ("apply_traffic", lambda: self.entitlements.recover_once(limit=limit)),
            (
                "finish_payments",
                lambda: recover_partner_orders_once(
                    self.store, self.checkout, limit=limit
                ),
            ),
        ]
        if self.notifications is not None and self.notifications.ready:
            phases.append(
                ("notifications", lambda: self.notifications.recover_once(limit=limit))
            )
        for name, run in phases:
            try:
                results[name] = await asyncio.wait_for(
                    run(), timeout=self.phase_timeout_seconds
                )
            except Exception:
                results[name] = {"error": True}
                results["phase_errors"] += 1
        if callable(getattr(self.store, "traffic_health", None)):
            try:
                results["traffic_health"] = await asyncio.wait_for(
                    self.store.traffic_health(), timeout=self.phase_timeout_seconds
                )
            except Exception:
                results["phase_errors"] += 1
        return results
