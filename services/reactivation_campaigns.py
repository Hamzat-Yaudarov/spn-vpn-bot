import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from config import (
    BYPASS_HWID_DEVICE_LIMIT,
    BYPASS_SQUAD_UUID,
    GB_BYTES,
    REACTIVATION_MAX_SENDS,
    REACTIVATION_NEW_USER_DAYS,
    REACTIVATION_NEW_USER_TRAFFIC_GB,
    REACTIVATION_NEW_USER_WAIT_DAYS,
    REACTIVATION_SEND_HOUR_MSK,
    REACTIVATION_WINBACK_DAYS,
    REACTIVATION_WINBACK_INACTIVE_DAYS,
    REACTIVATION_WINBACK_TRAFFIC_GB,
)
from services.notification_delivery import mark_telegram_delivery_blocked
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
CANDIDATE_REFRESH_INTERVAL = timedelta(hours=1)
TELEGRAM_RATE_LIMIT_SECONDS = 0.1

OFFER_CONFIG = {
    "winback_7d": {
        "days": REACTIVATION_WINBACK_DAYS,
        "traffic_gb": REACTIVATION_WINBACK_TRAFFIC_GB,
        "button": "🎁 Активировать 7 дней бесплатно",
        "text": (
            "Ассаламу алайкум!\n\n"
            "<b>Мы хотим вернуть ваше доверие не словами, а делом.</b>\n\n"
            "Для вас доступны <b>7 дней Way SPN с антиглушилкой бесплатно</b> "
            "и <b>50 ГБ трафика</b>. Ничего оплачивать и писать в поддержку не нужно.\n\n"
            "Нажмите кнопку ниже — бот сразу выдаст новый доступ и поможет подключиться.\n\n"
            "Предложение доступно один раз."
        ),
    },
    "new_user_1d": {
        "days": REACTIVATION_NEW_USER_DAYS,
        "traffic_gb": REACTIVATION_NEW_USER_TRAFFIC_GB,
        "button": "⚡️ Попробовать бесплатно",
        "text": (
            "Ассаламу алайкум!\n\n"
            "Хотите проверить, работает ли Way SPN именно на вашем устройстве и в вашей сети?\n\n"
            "Получите <b>1 день бесплатного доступа с антиглушилкой</b> и "
            "<b>10 ГБ трафика</b>. Без оплаты и банковской карты.\n\n"
            "Нажмите кнопку — доступ появится сразу.\n\n"
            "Предложение доступно один раз."
        ),
    },
}


def offer_config(offer_type: str) -> dict | None:
    return OFFER_CONFIG.get(offer_type)


def _offer_keyboard(offer_id: int, offer_type: str) -> InlineKeyboardMarkup:
    config = OFFER_CONFIG[offer_type]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=config["button"],
            callback_data=f"reactivation_claim:{offer_id}",
            style="success",
        )
    ]])


def _day_start_utc(now_msk: datetime) -> datetime:
    local_midnight = datetime.combine(now_msk.date(), time.min, tzinfo=MSK)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


async def refresh_reactivation_candidates() -> None:
    now = datetime.utcnow()
    await db.ensure_reactivation_candidates(
        now - timedelta(days=REACTIVATION_WINBACK_INACTIVE_DAYS),
        now - timedelta(days=REACTIVATION_NEW_USER_WAIT_DAYS),
    )


async def _delete_offer_message(bot, tg_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(tg_id, message_id)
    except Exception as exc:
        logger.debug(
            "Could not delete reactivation message %s for user %s: %s",
            message_id,
            tg_id,
            exc,
        )


async def send_due_reactivation_offers(bot, now_msk: datetime | None = None) -> int:
    now_msk = now_msk or datetime.now(MSK)
    day_start_utc = _day_start_utc(now_msk)
    offers = await db.get_reactivation_offers_due(day_start_utc, REACTIVATION_MAX_SENDS)
    sent_count = 0

    for index, offer in enumerate(offers or []):
        tg_id = int(offer["tg_id"])
        config = OFFER_CONFIG.get(offer["offer_type"])
        if not config:
            logger.error("Unknown reactivation offer type %s", offer["offer_type"])
            continue

        await _delete_offer_message(bot, tg_id, offer.get("last_message_id"))
        try:
            message = await bot.send_message(
                tg_id,
                config["text"],
                reply_markup=_offer_keyboard(int(offer["id"]), offer["offer_type"]),
            )
        except TelegramAPIError as exc:
            error_text = str(exc).lower()
            if any(marker in error_text for marker in ("chat not found", "bot was blocked", "user is deactivated")):
                try:
                    await mark_telegram_delivery_blocked(tg_id)
                except Exception as state_error:
                    logger.error("Failed to mark reactivation recipient %s blocked: %s", tg_id, state_error)
            elif "429" in error_text or "too many requests" in error_text:
                logger.warning("Telegram rate limit during reactivation campaign: %s", exc)
                await asyncio.sleep(5)
            else:
                logger.warning("Could not send reactivation offer to %s: %s", tg_id, exc)
            continue
        except Exception as exc:
            logger.warning("Unexpected reactivation delivery error for %s: %s", tg_id, exc)
            continue

        recorded = await db.mark_reactivation_offer_sent(
            int(offer["id"]),
            int(message.message_id),
            day_start_utc,
            REACTIVATION_MAX_SENDS,
        )
        if not recorded:
            await _delete_offer_message(bot, tg_id, int(message.message_id))
            continue

        sent_count += 1
        if index < len(offers) - 1:
            await asyncio.sleep(TELEGRAM_RATE_LIMIT_SECONDS)

    logger.info("Reactivation campaign delivery complete: %s sent", sent_count)
    return sent_count


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


async def run_reactivation_campaign_loop(bot) -> None:
    logger.info("Reactivation campaign service started")
    last_refresh_at = None
    last_delivery_date = None

    while True:
        try:
            now_utc = datetime.utcnow()
            if last_refresh_at is None or now_utc - last_refresh_at >= CANDIDATE_REFRESH_INTERVAL:
                await refresh_reactivation_candidates()
                last_refresh_at = now_utc

            now_msk = datetime.now(MSK)
            if now_msk.hour >= REACTIVATION_SEND_HOUR_MSK and last_delivery_date != now_msk.date():
                await send_due_reactivation_offers(bot, now_msk)
                last_delivery_date = now_msk.date()
        except asyncio.CancelledError:
            logger.info("Reactivation campaign service stopped")
            raise
        except Exception as exc:
            logger.error("Reactivation campaign loop failed: %s", exc, exc_info=True)

        await asyncio.sleep(CAMPAIGN_CHECK_INTERVAL_SECONDS)
