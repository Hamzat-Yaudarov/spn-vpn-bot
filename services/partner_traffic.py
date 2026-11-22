"""Paid-period quota arithmetic and an explicit, inert-by-default worker.

The remote counter is NEVER reset. Only an absolute cap is raised, once per
confirmed 30-day purchase. Unused base GB do not stack; unconsumed paid GB do.
If usage/cadence cannot be verified, stop rather than invent a zero or erase GB.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BASE_BYTES = 300 * 1024**3
MAX_BYTES = 2**53 - 1
MAX_OBSERVATION_AGE = timedelta(minutes=1)
MAX_CYCLE_LAG = timedelta(minutes=30)


class TrafficStateError(ValueError):
    pass


def utc(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TrafficStateError("Не удалось подтвердить время учёта трафика.")
    return value.astimezone(timezone.utc)


def bytes_value(value):
    if type(value) is not int or not 0 <= value <= MAX_BYTES:
        raise TrafficStateError("Не удалось подтвердить счётчик трафика.")
    return value


@dataclass(frozen=True)
class PartnerTrafficUsage:
    project_id: str
    subscription_id: str
    remote_user_id: str
    used_bytes: int
    limit_bytes: int
    observed_at: datetime


def next_traffic_cycle(*, baseline, paid_credit, previous_limit, last_used, used):
    baseline, paid_credit, previous_limit, last_used, used = map(
        bytes_value, (baseline, paid_credit, previous_limit, last_used, used)
    )
    if (
        used < baseline
        or used < last_used
        or used > previous_limit
        or previous_limit != baseline + BASE_BYTES + paid_credit
    ):
        raise TrafficStateError(
            "Счётчик трафика изменился; нужна сверка без сброса ГБ."
        )
    paid_consumed = max(0, used - baseline - BASE_BYTES)
    remaining_paid = max(0, paid_credit - paid_consumed)
    limit = bytes_value(used + BASE_BYTES + remaining_paid)
    if limit < previous_limit:
        raise TrafficStateError("Уменьшение абсолютного лимита не разрешено.")
    return {
        "baseline": used,
        "remaining_paid_bytes": remaining_paid,
        "new_limit_bytes": limit,
        "paid_consumed_bytes": paid_consumed,
    }


class PartnerTrafficService:
    def __init__(self, store, provisioner=None, *, clock=None):
        self.store, self.provisioner = store, provisioner
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def ready(self):
        return bool(
            getattr(self.store, "traffic_cycles_enabled", False)
            and callable(getattr(self.provisioner, "read_usage", None))
        )

    async def recover_once(self, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Некорректный размер обработки.")
        counts = {
            "enabled": self.ready,
            "checked": 0,
            "prepared": 0,
            "held": 0,
            "errors": 0,
        }
        if not self.ready:
            return counts
        for row in await self.store.list_due_traffic_cycles(
            now=utc(self.clock()), limit=limit
        ):
            counts["checked"] += 1
            try:
                async with self.store.acquire_activation_lock(
                    row["project_id"], row["subscription_id"]
                ) as lease:
                    active = lease or self.store
                    context = await active.get_traffic_context(
                        row["project_id"], row["subscription_id"], now=utc(self.clock())
                    )
                    if context is None:
                        continue
                    observation = await self.provisioner.read_usage(context)
                    result = await active.prepare_traffic_cycle(
                        context, observation, now=utc(self.clock())
                    )
                    counts["held" if result.get("hold") else "prepared"] += 1
            except (Exception, asyncio.CancelledError) as error:
                defer = getattr(self.store, "defer_traffic_cycle", None)
                if callable(defer):
                    await defer(
                        row["project_id"], row["subscription_id"], now=utc(self.clock())
                    )
                if isinstance(error, asyncio.CancelledError):
                    raise
                counts["errors"] += 1
        # Prepared jobs are intentionally applied by the same entitlement
        # worker/FIFO as bonuses and paid orders, not by a parallel reset path.
        return counts
