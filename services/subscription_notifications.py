import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from config import GB_BYTES
from services.remnawave import remnawave_get_user_info


logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
TELEGRAM_RATE_LIMIT = 0.1

EXPIRED_NOTIFICATION_TYPE = "expired_or_no_subscription"
LOW_TRAFFIC_NOTIFICATION_TYPE = "low_bypass_traffic"

EXPIRING_ONCE_COOLDOWN_HOURS = 24 * 365 * 20
EXPIRED_COOLDOWN_HOURS = 86
LOW_TRAFFIC_COOLDOWN_HOURS = 36

EXPIRING_STAGES = [
    {
        "type": "expires_today",
        "threshold": timedelta(hours=12),
        "title": "Подписка заканчивается сегодня",
        "body": "Лучше продлить сейчас, чтобы ключ не отключился.",
    },
    {
        "type": "expires_1d",
        "threshold": timedelta(days=1),
        "title": "Остался 1 день подписки",
        "body": "Продлите заранее: оставшиеся дни сохранятся и добавятся к новому сроку.",
    },
    {
        "type": "expires_3d",
        "threshold": timedelta(days=3),
        "title": "До окончания осталось 3 дня",
        "body": "Можно продлить в пару кликов, чтобы доступ продолжил работать без пауз.",
    },
    {
        "type": "expires_7d",
        "threshold": timedelta(days=7),
        "title": "До окончания осталось 7 дней",
        "body": "Если продлите заранее, текущие дни не сгорят.",
    },
]


def ensure_utc_aware(dt):
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_time_left(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return f"{days} дн. {hours} ч."
    return f"{hours} ч. {minutes} мин."


def _subscription_name(subscription) -> str:
    plan_kind = subscription.get("plan_kind") or "regular"
    type_index = subscription.get("type_index") or subscription.get("slot_number")
    title = "С антиглушилкой" if plan_kind == "bypass" else "Обычная"
    return f"{title} #{type_index}"


def _buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить / Продлить", callback_data="buy_subscription", style="success")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu", style="danger")],
    ])


async def _has_multiple_active_visible_subscriptions(tg_id: int) -> bool:
    subscriptions = await db.get_visible_subscriptions(tg_id)
    now = datetime.utcnow()
    active_count = sum(1 for subscription in subscriptions or [] if subscription.get("subscription_until") and subscription["subscription_until"] > now)
    return active_count > 1


async def check_and_send_notifications(bot):
    """
    Фоновая задача уведомлений:
    - подписка истекает: ежедневно в 19:00 МСК на этапах 7/3/1 день и сегодня;
    - мало ГБ: раз в 36 часов, если осталось <10 ГБ и до обновления >8 дней;
    - нет активной подписки/закончилась: раз в 86 часов.
    """
    logger.info("✅ Scheduled notification service started")
    last_expiring_run_date = None
    last_periodic_check_at = None

    try:
        while True:
            now_msk = datetime.now(MSK)

            if now_msk.hour == 19 and now_msk.minute == 0 and last_expiring_run_date != now_msk.date():
                last_expiring_run_date = now_msk.date()
                logger.info("⏰ Scheduled check: 19:00 MSK - expiring subscriptions")
                await _safe_run(_send_notifications_for_expiring, bot)
                await asyncio.sleep(60)

            now_utc = datetime.utcnow()
            if not last_periodic_check_at or now_utc - last_periodic_check_at >= timedelta(hours=1):
                last_periodic_check_at = now_utc
                await _safe_run(_send_notifications_for_low_traffic, bot)
                await _safe_run(_send_notifications_for_expired, bot)

            await asyncio.sleep(30)
    except asyncio.CancelledError:
        logger.info("Scheduled notification service shut down gracefully")
        raise


async def _safe_run(func, bot):
    try:
        await func(bot)
    except Exception as e:
        logger.error("Notification task %s failed: %s", func.__name__, e, exc_info=True)


async def _send_notifications_for_expiring(bot):
    now = datetime.now(timezone.utc)
    subscriptions = await db.db_execute(
        """
        SELECT id, tg_id, slot_number, type_index, plan_kind, subscription_until
        FROM subscriptions
        WHERE remnawave_uuid IS NOT NULL
          AND subscription_until IS NOT NULL
          AND subscription_until > $1
          AND subscription_until <= $2
        ORDER BY tg_id ASC, subscription_until ASC
        """,
        (now.replace(tzinfo=None), (now + timedelta(days=7)).replace(tzinfo=None)),
        fetch_all=True,
    )

    sent = 0
    for i, subscription in enumerate(subscriptions or []):
        tg_id = subscription["tg_id"]
        subscription_id = subscription["id"]
        expire_at = ensure_utc_aware(subscription["subscription_until"])
        time_left = expire_at - now
        stage = _pick_expiring_stage(time_left)
        if not stage:
            continue
        if not await db.can_send_notification(tg_id, stage["type"], EXPIRING_ONCE_COOLDOWN_HOURS, subscription_id):
            continue

        text = (
            f"⏰ <b>{stage['title']}</b>\n\n"
            f"Подписка: <b>{_subscription_name(subscription)}</b>\n"
            f"Осталось: <b>{_format_time_left(time_left)}</b>\n\n"
            f"{stage['body']}"
        )
        keyboard = [[InlineKeyboardButton(text="🔄 Продлить эту подписку", callback_data=f"renew_subscription_{subscription_id}", style="success")]]
        if await _has_multiple_active_visible_subscriptions(tg_id):
            keyboard.append([InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")])
        keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu", style="danger")])
        kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
        if await _send_message(bot, tg_id, text, kb):
            await db.mark_notification_state_sent(tg_id, stage["type"], subscription_id)
            sent += 1
        if i < len(subscriptions) - 1:
            await asyncio.sleep(TELEGRAM_RATE_LIMIT)

    logger.info("✅ Expiring notification batch complete: %s sent", sent)


def _pick_expiring_stage(time_left: timedelta) -> dict | None:
    if time_left.total_seconds() <= 0:
        return None
    for stage in EXPIRING_STAGES:
        if time_left <= stage["threshold"]:
            return stage
    return None


async def _send_notifications_for_expired(bot):
    users = await db.db_execute("SELECT tg_id FROM users ORDER BY tg_id ASC", fetch_all=True)
    now = datetime.utcnow()
    sent = 0

    for i, user in enumerate(users or []):
        tg_id = user["tg_id"]
        subscriptions = await db.get_user_subscriptions(tg_id)
        has_active = any(
            sub.get("subscription_until") and sub["subscription_until"] > now
            for sub in subscriptions
        )
        if has_active:
            continue
        if not await db.can_send_notification(tg_id, EXPIRED_NOTIFICATION_TYPE, EXPIRED_COOLDOWN_HOURS):
            continue

        expired_dates = [
            sub["subscription_until"] for sub in subscriptions
            if sub.get("subscription_until") and sub["subscription_until"] <= now
        ]
        if expired_dates:
            last_expired_at = max(expired_dates)
            days_expired = max(0, (now - last_expired_at).days)
            text = (
                "❌ <b>Подписка закончилась</b>\n\n"
                f"Закончилась: <b>{days_expired} дн. назад</b>\n\n"
                "Что сделать: нажмите «Купить / Продлить», чтобы вернуть доступ."
            )
        else:
            text = (
                "❌ <b>Активной подписки нет</b>\n\n"
                "Что сделать: нажмите «Купить / Продлить», чтобы получить доступ к VPN."
            )

        if await _send_message(bot, tg_id, text, _buy_keyboard()):
            await db.mark_notification_state_sent(tg_id, EXPIRED_NOTIFICATION_TYPE)
            sent += 1
        if i < len(users) - 1:
            await asyncio.sleep(TELEGRAM_RATE_LIMIT)

    logger.info("✅ Expired/no-sub notification batch complete: %s sent", sent)


async def _send_notifications_for_low_traffic(bot):
    now = datetime.utcnow()
    subscriptions = await db.db_execute(
        """
        SELECT id, tg_id, slot_number, type_index, plan_kind, remnawave_uuid,
               subscription_until, current_period_limit_bytes, traffic_reset_at
        FROM subscriptions
        WHERE plan_kind = 'bypass'
          AND remnawave_uuid IS NOT NULL
          AND subscription_until IS NOT NULL
          AND subscription_until > $1
          AND current_period_limit_bytes > 0
          AND traffic_reset_at IS NOT NULL
          AND traffic_reset_at > $2
        ORDER BY tg_id ASC, id ASC
        """,
        (now, now + timedelta(days=8)),
        fetch_all=True,
    )

    if not subscriptions:
        return

    sent = 0
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for i, subscription in enumerate(subscriptions):
            try:
                tg_id = subscription["tg_id"]
                subscription_id = subscription["id"]
                if not await db.can_send_notification(tg_id, LOW_TRAFFIC_NOTIFICATION_TYPE, LOW_TRAFFIC_COOLDOWN_HOURS, subscription_id):
                    continue

                user_info = await remnawave_get_user_info(session, subscription["remnawave_uuid"])
                used_bytes = ((user_info or {}).get("userTraffic") or {}).get("usedTrafficBytes") or 0
                limit_bytes = subscription.get("current_period_limit_bytes") or 0
                remaining_bytes = max(0, limit_bytes - used_bytes)
                if remaining_bytes >= 10 * GB_BYTES:
                    continue

                reset_at = subscription["traffic_reset_at"]
                days_to_reset = max(0, (reset_at - now).days)
                text = (
                    f"📦 <b>Мало ГБ антиглушилки</b>\n\n"
                    f"Подписка: <b>{_subscription_name(subscription)}</b>\n"
                    f"Осталось: <b>{remaining_bytes / GB_BYTES:.1f} ГБ</b>\n"
                    f"До обновления: <b>{days_to_reset} дн.</b>\n\n"
                    "Что сделать: нажмите «Купить ГБ», если хотите сохранить работу без пауз."
                )
                keyboard = [[InlineKeyboardButton(text="📦 Купить ГБ", callback_data="buy_gb", style="success")]]
                if await _has_multiple_active_visible_subscriptions(tg_id):
                    keyboard.append([InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")])
                keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu", style="danger")])
                kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
                if await _send_message(bot, tg_id, text, kb):
                    await db.mark_notification_state_sent(tg_id, LOW_TRAFFIC_NOTIFICATION_TYPE, subscription_id)
                    sent += 1
            except Exception as e:
                logger.warning("Low traffic check failed for subscription %s: %s", subscription.get("id"), e)

            if i < len(subscriptions) - 1:
                await asyncio.sleep(TELEGRAM_RATE_LIMIT)

    logger.info("✅ Low traffic notification batch complete: %s sent", sent)


async def _send_message(bot, tg_id: int, text: str, reply_markup: InlineKeyboardMarkup) -> bool:
    try:
        await bot.send_message(tg_id, text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as e:
        if "429" in str(e) or "Too Many Requests" in str(e):
            logger.warning("🚫 Rate limited while sending notification")
            await asyncio.sleep(5)
        elif "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
            logger.debug("User %s blocked bot or deactivated account", tg_id)
        else:
            logger.error("Failed to send notification to user %s: %s", tg_id, e)
    except Exception as e:
        logger.error("Unexpected error sending notification to user %s: %s", tg_id, e)
    return False
