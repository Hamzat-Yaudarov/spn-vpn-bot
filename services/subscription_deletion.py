import logging
from datetime import datetime, timezone

import database as db
from services.remnawave import remnawave_delete_user


logger = logging.getLogger(__name__)


class SubscriptionDeletionError(RuntimeError):
    """Базовая ошибка удаления подписки."""


class SubscriptionNotFoundError(SubscriptionDeletionError):
    """Подписка не найдена или уже удалена."""


class SubscriptionBusyError(SubscriptionDeletionError):
    """Пользователь уже занят другой операцией."""


class RemnawaveDeletionError(SubscriptionDeletionError):
    """Не удалось удалить пользователя в Remnawave."""


class SubscriptionActiveError(SubscriptionDeletionError):
    """Пользователь может удалить только подписку с истёкшим сроком."""


def subscription_has_expired(subscription, *, now: datetime | None = None) -> bool:
    until = subscription.get('subscription_until')
    if until is None:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return until <= now


async def delete_subscription_everywhere(
    subscription_id: int,
    *,
    tg_id: int | None = None,
    actor: str = "user",
    require_expired: bool = False,
) -> dict:
    """Удалить подписку в Remnawave и локальной базе."""
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)
    if not subscription:
        raise SubscriptionNotFoundError("Подписка не найдена")

    owner_id = int(subscription["tg_id"])
    if not await db.acquire_user_lock(owner_id):
        raise SubscriptionBusyError("Пользователь сейчас занят другой операцией")

    try:
        subscription = await db.get_subscription_by_id(subscription_id, tg_id)
        if not subscription:
            raise SubscriptionNotFoundError("Подписка уже удалена")

        # Recheck under the same user lock as renewal: an old confirmation
        # button must not delete a subscription renewed in the meantime.
        if require_expired and not subscription_has_expired(subscription):
            raise SubscriptionActiveError("Удалить можно только истёкшую подписку.")

        remnawave_uuid = subscription.get("remnawave_uuid")
        remnawave_deleted = True
        if remnawave_uuid:
            remnawave_deleted = await remnawave_delete_user(None, str(remnawave_uuid))
            if not remnawave_deleted:
                raise RemnawaveDeletionError("Не удалось удалить подписку в Remnawave")

        if not await db.delete_subscription_record(subscription_id):
            raise SubscriptionNotFoundError("Подписка уже удалена")

        logger.info(
            "%s deleted subscription %s for user %s (remnawave_deleted=%s)",
            actor,
            subscription_id,
            owner_id,
            remnawave_deleted,
        )
        return {"subscription": subscription, "remnawave_deleted": remnawave_deleted}
    finally:
        await db.release_user_lock(owner_id)
