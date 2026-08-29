import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

import database as db
from config import (
    BYPASS_HWID_DEVICE_LIMIT,
    BYPASS_SQUAD_UUID,
    GB_BYTES,
    REACTIVATION_NEW_USER_DAYS,
    REACTIVATION_NEW_USER_TRAFFIC_GB,
    REACTIVATION_WINBACK_DAYS,
    REACTIVATION_WINBACK_TRAFFIC_GB,
)
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_get_subscription_url,
    remnawave_reset_user_traffic,
    remnawave_set_subscription_expiry,
    remnawave_update_user_profile,
)


logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
CAMPAIGN_CHECK_INTERVAL_SECONDS = 60
CLEANUP_RETRY_INTERVAL_SECONDS = 10 * 60
TELEGRAM_RATE_LIMIT_SECONDS = 0.1
# Одноразовое отключение кампаний по решению владельца сервиса.
CAMPAIGN_RETIRE_AT_MSK = datetime(2026, 8, 29, 18, 0, tzinfo=MSK)

OFFER_CONFIG = {
    "winback_7d": {
        "days": REACTIVATION_WINBACK_DAYS,
        "traffic_gb": REACTIVATION_WINBACK_TRAFFIC_GB,
    },
    "new_user_1d": {
        "days": REACTIVATION_NEW_USER_DAYS,
        "traffic_gb": REACTIVATION_NEW_USER_TRAFFIC_GB,
    },
}


def offer_config(offer_type: str) -> dict | None:
    return OFFER_CONFIG.get(offer_type)


async def _delete_offer_message(bot, tg_id: int, message_id: int | None) -> bool:
    if not message_id:
        return True
    try:
        await bot.delete_message(tg_id, message_id)
        return True
    except Exception as exc:
        logger.debug(
            "Could not delete reactivation message %s for user %s: %s",
            message_id,
            tg_id,
            exc,
        )
        return False


async def retire_reactivation_campaigns(bot) -> tuple[int, int]:
    """Закрыть обе бесплатные кампании и удалить их последние сообщения."""
    offers = await db.retire_reactivation_offers()
    deleted_count = 0

    for index, offer in enumerate(offers or []):
        offer_id = int(offer["id"])
        tg_id = int(offer["tg_id"])
        message_id = offer.get("last_message_id")
        if await _delete_offer_message(bot, tg_id, message_id):
            await db.clear_reactivation_offer_message(offer_id, message_id)
            deleted_count += 1
        if index < len(offers) - 1:
            await asyncio.sleep(TELEGRAM_RATE_LIMIT_SECONDS)

    pending_count = len(offers or []) - deleted_count
    logger.info(
        "Reactivation campaigns retired: %s message(s) deleted, %s pending; no new offers will be sent",
        deleted_count,
        pending_count,
    )
    return deleted_count, pending_count


async def cancel_reactivation_offers_after_purchase(bot, tg_id: int) -> None:
    rows = await db.cancel_open_reactivation_offers(tg_id)
    if bot is not None:
        for row in rows or []:
            await _delete_offer_message(bot, tg_id, row.get("last_message_id"))
    if rows:
        logger.info("Cancelled %s reactivation offer(s) after purchase by user %s", len(rows), tg_id)


async def activate_reactivation_offer(offer_id: int, tg_id: int) -> dict:
    """Выдать бесплатный bypass-доступ с фиксированным сроком и лимитом."""
    offer = await db.db_execute(
        "SELECT offer_type FROM reactivation_offers WHERE id = $1 AND tg_id = $2 LIMIT 1",
        (offer_id, tg_id),
        fetch_one=True,
    )
    if not offer:
        return {"error": "not_found"}

    config = OFFER_CONFIG.get(offer["offer_type"])
    if not config:
        return {"error": "not_available"}

    prepared = await db.prepare_reactivation_offer_claim(offer_id, tg_id, config["days"])
    if prepared.get("error"):
        return prepared

    prepared_offer = prepared["offer"]
    subscription = prepared["subscription"]
    subscription_id = int(subscription["id"])
    type_index = int(subscription.get("type_index") or subscription_id)
    remna_username = subscription.get("remnawave_username") or f"tg_{tg_id}_bypass_{type_index}"
    traffic_limit_bytes = int(config["traffic_gb"] * GB_BYTES)

    connector = aiohttp.TCPConnector()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        uuid, username = await remnawave_get_or_create_user(
            session,
            tg_id,
            days=config["days"],
            extend_if_exists=False,
            remna_username=remna_username,
            traffic_limit_bytes=traffic_limit_bytes,
            traffic_limit_strategy="NO_RESET",
            active_internal_squads=[BYPASS_SQUAD_UUID],
            hwid_device_limit=BYPASS_HWID_DEVICE_LIMIT,
            telegram_id=tg_id,
        )
        if not uuid:
            return {"error": "remnawave_unavailable"}

        profile_updated = await remnawave_update_user_profile(
            session,
            uuid,
            traffic_limit_bytes=traffic_limit_bytes,
            traffic_limit_strategy="NO_RESET",
            active_internal_squads=[BYPASS_SQUAD_UUID],
            hwid_device_limit=BYPASS_HWID_DEVICE_LIMIT,
            telegram_id=tg_id,
        )
        if not profile_updated:
            logger.error("Could not sync profile for reactivation offer %s", offer_id)
            return {"error": "remnawave_unavailable"}

        if not await remnawave_reset_user_traffic(session, uuid):
            logger.error("Could not reset traffic for reactivation offer %s", offer_id)
            return {"error": "remnawave_unavailable"}

        if not await remnawave_set_subscription_expiry(session, uuid, prepared_offer["trial_expires_at"]):
            logger.error("Could not set expiry for reactivation offer %s", offer_id)
            return {"error": "remnawave_unavailable"}

        subscription_url = await remnawave_get_subscription_url(session, uuid)
        if not subscription_url:
            logger.error("Could not obtain URL for reactivation offer %s", offer_id)
            return {"error": "remnawave_unavailable"}

    finalized = await db.finalize_reactivation_offer_claim(
        offer_id,
        tg_id,
        subscription_id,
        uuid,
        username or remna_username,
        BYPASS_SQUAD_UUID,
        traffic_limit_bytes,
        BYPASS_HWID_DEVICE_LIMIT,
        config["days"],
    )
    if not finalized:
        return {"error": "not_available"}

    logger.info(
        "Reactivation offer %s claimed by user %s: subscription=%s days=%s traffic_gb=%s",
        offer_id,
        tg_id,
        subscription_id,
        config["days"],
        config["traffic_gb"],
    )
    return {
        "offer_type": offer["offer_type"],
        "subscription_id": subscription_id,
        "subscription_url": subscription_url,
        "expires_at": prepared_offer["trial_expires_at"],
        "days": config["days"],
        "traffic_gb": config["traffic_gb"],
        "last_message_id": prepared_offer.get("last_message_id"),
    }


async def run_reactivation_cleanup_loop(bot) -> None:
    logger.info("Retired reactivation campaign cleanup service started")

    while True:
        retry_delay = CAMPAIGN_CHECK_INTERVAL_SECONDS
        try:
            now_msk = datetime.now(MSK)
            if now_msk >= CAMPAIGN_RETIRE_AT_MSK:
                _deleted_count, pending_count = await retire_reactivation_campaigns(bot)
                if pending_count == 0:
                    logger.info("All retired reactivation campaign messages are cleaned up")
                    return
                retry_delay = CLEANUP_RETRY_INTERVAL_SECONDS
        except asyncio.CancelledError:
            logger.info("Retired reactivation campaign cleanup service stopped")
            raise
        except Exception as exc:
            logger.error("Reactivation campaign cleanup failed: %s", exc, exc_info=True)

        await asyncio.sleep(retry_delay)
