from datetime import datetime, timedelta

import database as db
from services.remnawave import remnawave_set_subscription_expiry
from services.subscription_sync import refresh_subscription_expiry


class SubscriptionAdjustmentError(RuntimeError):
    pass


async def adjust_subscription_days(subscription, days: int) -> datetime:
    """Изменить срок подписки и синхронизировать Remnawave с БД."""
    if days == 0:
        raise SubscriptionAdjustmentError("Количество дней не может быть нулём")

    current_until = await refresh_subscription_expiry(subscription)
    now = datetime.utcnow()
    if days > 0:
        base = current_until if current_until and current_until > now else now
    else:
        base = current_until or now
    new_until = base + timedelta(days=days)

    if subscription.get("remnawave_uuid"):
        updated = await remnawave_set_subscription_expiry(
            None,
            subscription["remnawave_uuid"],
            new_until,
        )
        if not updated:
            raise SubscriptionAdjustmentError("Remnawave не принял новую дату")

    await db.sync_subscription_expiry(subscription["id"], new_until)
    return new_until
