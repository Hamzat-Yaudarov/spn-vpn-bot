from dataclasses import dataclass
from datetime import datetime, timedelta

from config import BYPASS_BASE_TRAFFIC_GB, GB_BYTES


@dataclass(slots=True)
class TrafficPeriodState:
    enabled: bool
    was_active: bool
    base_bytes: int
    carried_bytes: int
    paid_bytes: int
    limit_bytes: int
    reset_at: datetime | None
    last_known_used_bytes: int


def build_traffic_period_state(subscription, plan_kind: str, now: datetime | None = None) -> TrafficPeriodState:
    """Посчитать состояние traffic-cycle при покупке/продлении подписки."""
    now = now or datetime.utcnow()

    if plan_kind != "bypass":
        return TrafficPeriodState(
            enabled=False,
            was_active=False,
            base_bytes=0,
            carried_bytes=0,
            paid_bytes=0,
            limit_bytes=0,
            reset_at=None,
            last_known_used_bytes=0,
        )

    current_until = subscription.get("subscription_until")
    was_active = bool(current_until and current_until > now)
    base_bytes = BYPASS_BASE_TRAFFIC_GB * GB_BYTES

    if not was_active:
        return TrafficPeriodState(
            enabled=True,
            was_active=False,
            base_bytes=base_bytes,
            carried_bytes=0,
            paid_bytes=0,
            limit_bytes=base_bytes,
            reset_at=now + timedelta(days=30),
            last_known_used_bytes=0,
        )

    carried_bytes = int(subscription.get("carried_traffic_bytes") or 0)
    paid_bytes = int(subscription.get("current_paid_traffic_bytes") or 0)
    limit_bytes = max(
        int(subscription.get("current_period_limit_bytes") or 0),
        base_bytes + carried_bytes + paid_bytes,
    )

    return TrafficPeriodState(
        enabled=True,
        was_active=True,
        base_bytes=base_bytes,
        carried_bytes=carried_bytes,
        paid_bytes=paid_bytes,
        limit_bytes=limit_bytes,
        reset_at=subscription.get("traffic_reset_at") or now + timedelta(days=30),
        last_known_used_bytes=int(subscription.get("last_known_used_traffic_bytes") or 0),
    )
