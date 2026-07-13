from datetime import datetime, timedelta

import database as db
from config import BYPASS_SQUAD_UUID
from services.device_addons import effective_device_limit
from services.remnawave import (
    remnawave_reset_user_traffic,
    remnawave_set_subscription_expiry,
    remnawave_update_user_profile,
)
from services.subscription_sync import refresh_subscription_expiry
from services.traffic_periods import build_traffic_period_state


class SubscriptionAdjustmentError(RuntimeError):
    pass


async def adjust_subscription_days(subscription, days: int) -> datetime:
    """Изменить срок подписки и синхронизировать Remnawave с БД."""
    if days == 0:
        raise SubscriptionAdjustmentError("Количество дней не может быть нулём")

    current_until = await refresh_subscription_expiry(subscription)
    now = datetime.utcnow()
    subscription_snapshot = dict(subscription)
    subscription_snapshot["subscription_until"] = current_until
    plan_kind = subscription_snapshot.get("plan_kind") or "regular"
    traffic_state = build_traffic_period_state(subscription_snapshot, plan_kind, now)

    if days > 0:
        base = current_until if current_until and current_until > now else now
    else:
        base = current_until or now
    new_until = base + timedelta(days=days)

    if subscription.get("remnawave_uuid"):
        if days > 0 and traffic_state.enabled:
            active_device_addons = await db.get_active_device_addon_count(subscription["id"])
            device_limit = effective_device_limit(plan_kind, active_device_addons)
            profile_updated = await remnawave_update_user_profile(
                None,
                subscription["remnawave_uuid"],
                traffic_limit_bytes=traffic_state.limit_bytes,
                traffic_limit_strategy="NO_RESET",
                active_internal_squads=[BYPASS_SQUAD_UUID],
                hwid_device_limit=device_limit,
                telegram_id=subscription["tg_id"],
            )
            if not profile_updated:
                raise SubscriptionAdjustmentError("Remnawave не принял новый лимит трафика")

            if not traffic_state.was_active:
                reset_ok = await remnawave_reset_user_traffic(None, subscription["remnawave_uuid"])
                if not reset_ok:
                    traffic_state.reset_at = now
                    traffic_state.last_known_used_bytes = int(subscription.get("last_known_used_traffic_bytes") or 0)

        updated = await remnawave_set_subscription_expiry(
            None,
            subscription["remnawave_uuid"],
            new_until,
        )
        if not updated:
            raise SubscriptionAdjustmentError("Remnawave не принял новую дату")

    await db.sync_subscription_expiry(subscription["id"], new_until)
    if days > 0 and traffic_state.enabled:
        await db.update_subscription_traffic_period(
            subscription["id"],
            traffic_enabled=traffic_state.enabled,
            base_traffic_bytes=traffic_state.base_bytes,
            carried_traffic_bytes=traffic_state.carried_bytes,
            current_paid_traffic_bytes=traffic_state.paid_bytes,
            current_period_limit_bytes=traffic_state.limit_bytes,
            traffic_reset_at=traffic_state.reset_at,
            last_known_used_traffic_bytes=traffic_state.last_known_used_bytes,
        )
    return new_until
