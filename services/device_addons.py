from __future__ import annotations

import math
from datetime import datetime

from config import (
    BYPASS_HWID_DEVICE_LIMIT,
    DEVICE_ADDON_DISCOUNT_STEP_PERCENT,
    DEVICE_ADDON_MAX_DISCOUNT_PERCENT,
    DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT,
    DEVICE_ADDON_MIN_PRICE,
    DEVICE_ADDON_PACKAGES,
    DEVICE_ADDON_PRICE_PER_30_DAYS,
    REGULAR_HWID_DEVICE_LIMIT,
)


SECONDS_IN_DAY = 86_400


def base_device_limit(plan_kind: str | None) -> int:
    return BYPASS_HWID_DEVICE_LIMIT if plan_kind == "bypass" else REGULAR_HWID_DEVICE_LIMIT


def current_device_limit(subscription) -> int:
    value = subscription.get("hwid_device_limit") if subscription else None
    return int(value or base_device_limit(subscription.get("plan_kind") if subscription else "regular"))


def device_count_text(count: int) -> str:
    return f"{count} устройство" if count == 1 else f"{count} устройства" if count in (2, 3, 4) else f"{count} устройств"


def device_addon_discount_percent(device_count: int) -> int:
    return min(max(device_count - 1, 0) * DEVICE_ADDON_DISCOUNT_STEP_PERCENT, DEVICE_ADDON_MAX_DISCOUNT_PERCENT)


def remaining_billable_days(until: datetime, *, now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    seconds = max(0, (until - now).total_seconds())
    return max(1, math.ceil(seconds / SECONDS_IN_DAY))


def calculate_device_addon_price(
    plan_kind: str,
    device_count: int,
    until: datetime,
    *,
    now: datetime | None = None,
) -> dict:
    days = remaining_billable_days(until, now=now)
    base_price = DEVICE_ADDON_PRICE_PER_30_DAYS.get(plan_kind, DEVICE_ADDON_PRICE_PER_30_DAYS["regular"])
    original_price = base_price * device_count * days / 30
    discount_percent = device_addon_discount_percent(device_count)
    discounted = original_price * (100 - discount_percent) / 100
    price = max(DEVICE_ADDON_MIN_PRICE, math.ceil(discounted))
    return {
        "price": price,
        "original_price": math.ceil(original_price),
        "discount_percent": discount_percent,
        "days": days,
    }


def available_device_addon_packages(subscription, *, now: datetime | None = None) -> list[dict]:
    if not subscription:
        return []

    until = subscription.get("subscription_until")
    if not until or until <= (now or datetime.utcnow()):
        return []

    plan_kind = subscription.get("plan_kind") if subscription.get("plan_kind") in {"regular", "bypass"} else "regular"
    current_limit = current_device_limit(subscription)
    free_limit = max(0, DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT - current_limit)
    packages = []
    for count in DEVICE_ADDON_PACKAGES:
        if count > free_limit:
            continue
        pricing = calculate_device_addon_price(plan_kind, count, until, now=now)
        packages.append({
            "count": count,
            **pricing,
        })
    return packages


def effective_device_limit(plan_kind: str | None, active_addon_devices: int) -> int:
    return min(DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT, base_device_limit(plan_kind) + max(0, int(active_addon_devices or 0)))
