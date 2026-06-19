import logging
from datetime import datetime, timezone

import database as db
from services.remnawave import remnawave_get_user_info


logger = logging.getLogger(__name__)


def _as_utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def remnawave_expiry(user_info: dict | None) -> datetime | None:
    value = (user_info or {}).get("expireAt")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _as_utc_naive(parsed)
    except (TypeError, ValueError):
        logger.warning("Invalid Remnawave expireAt value: %r", value)
        return None


async def reconcile_subscription_expiry(subscription, user_info: dict | None) -> datetime | None:
    """Вернуть фактический срок Remnawave и обновить устаревшую дату в БД."""
    local_expiry = _as_utc_naive(subscription.get("subscription_until"))
    remote_expiry = remnawave_expiry(user_info)
    if remote_expiry is None:
        return local_expiry

    if local_expiry is None or abs((remote_expiry - local_expiry).total_seconds()) >= 1:
        await db.sync_subscription_expiry(subscription["id"], remote_expiry)
        logger.info(
            "Synchronized subscription %s expiry from %s to %s",
            subscription["id"],
            local_expiry,
            remote_expiry,
        )

    return remote_expiry


async def refresh_subscription_expiry(subscription, session=None) -> datetime | None:
    if not subscription.get("remnawave_uuid"):
        return _as_utc_naive(subscription.get("subscription_until"))

    user_info = await remnawave_get_user_info(session, subscription["remnawave_uuid"])
    return await reconcile_subscription_expiry(subscription, user_info)
