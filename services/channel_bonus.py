import logging

import aiohttp

import database as db
from config import (
    BYPASS_BASE_TRAFFIC_GB,
    BYPASS_HWID_DEVICE_LIMIT,
    BYPASS_SQUAD_UUID,
    GB_BYTES,
)
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_set_subscription_expiry,
)


logger = logging.getLogger(__name__)


async def activate_news_channel_bonus(tg_id: int) -> bool:
    """Активировать новому пользователю антиглушилку на один день.

    Повтор после сетевого сбоя использует ту же скрытую запись, но заново даёт
    полные 24 часа. Видимой она становится только при атомарном завершении.
    Вызывающая сторона должна держать пользовательскую блокировку.
    """
    subscription = await db.prepare_news_channel_bonus_subscription(tg_id)
    if subscription is None:
        return not await db.needs_news_channel_onboarding(tg_id)

    subscription_id = int(subscription['id'])
    type_index = int(subscription.get('type_index') or subscription_id)
    remna_username = (
        subscription.get('remnawave_username')
        or f"tg_{tg_id}_bypass_{type_index}"
    )
    subscription_until = subscription['subscription_until']

    connector = aiohttp.TCPConnector()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        uuid, username = await remnawave_get_or_create_user(
            session,
            tg_id,
            days=1,
            extend_if_exists=False,
            remna_username=remna_username,
            traffic_limit_bytes=BYPASS_BASE_TRAFFIC_GB * GB_BYTES,
            traffic_limit_strategy="NO_RESET",
            active_internal_squads=[BYPASS_SQUAD_UUID],
            hwid_device_limit=BYPASS_HWID_DEVICE_LIMIT,
            telegram_id=tg_id,
        )
        if not uuid:
            logger.error("Failed to create Remnawave welcome subscription for user %s", tg_id)
            return False

        if not await remnawave_set_subscription_expiry(session, uuid, subscription_until):
            logger.error("Failed to set welcome subscription expiry for user %s", tg_id)
            return False

    finalized = await db.finalize_news_channel_bonus(
        tg_id,
        subscription_id,
        uuid,
        username or remna_username,
        subscription_until,
        BYPASS_SQUAD_UUID,
    )
    if not finalized and await db.needs_news_channel_onboarding(tg_id):
        logger.error("Failed to finalize news-channel bonus for user %s", tg_id)
        return False

    logger.info(
        "News-channel bonus activated for user %s, subscription %s, until %s",
        tg_id,
        subscription_id,
        subscription_until,
    )
    return True
