import logging
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import database as db
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError


logger = logging.getLogger(__name__)

# Часовой пояс MSK (UTC+3)
MSK = ZoneInfo("Europe/Moscow")

# Лимиты Telegram бота
TELEGRAM_RATE_LIMIT = 0.1  # Одно сообщение в 100ms (10 сообщений в секунду)
BATCH_SIZE = 50  # Обрабатываем по 50 пользователей за раз


def ensure_utc_aware(dt):
    """
    Убедиться что datetime имеет timezone UTC.
    Если это naive datetime, добавляем UTC.
    Если это datetime с другим timezone, конвертируем в UTC.
    """
    if dt is None:
        return None

    if not isinstance(dt, datetime):
        return None

    if dt.tzinfo is None:
        # Наивный datetime, предполагаем что это UTC
        return dt.replace(tzinfo=timezone.utc)
    elif dt.tzinfo != timezone.utc:
        # Конвертируем в UTC
        return dt.astimezone(timezone.utc)

    return dt


async def check_and_send_notifications(bot):
    """
    Главная фоновая задача для отправки уведомлений по расписанию:
    - 10:00 MSK: пользователи с <24h до конца подписки
    - 16:00 MSK: пользователи у которых подписка уже закончилась
    - 20:00 MSK: пользователи с <24h до конца подписки
    """
    logger.info("✅ Scheduled notification service started")
    
    try:
        while True:
            now_msk = datetime.now(MSK)
            hour = now_msk.hour
            minute = now_msk.minute
            
            # Проверяем каждое из трёх времён
            if hour == 10 and minute == 0:
                logger.info("⏰ Scheduled check: 10:00 MSK - Users with <24h left")
                try:
                    await _send_notifications_for_expiring(bot)
                except Exception as e:
                    logger.error(f"Error in 10:00 check: {e}", exc_info=True)
                # Ждём минуту чтобы не повторить
                await asyncio.sleep(60)
                
            elif hour == 16 and minute == 0:
                logger.info("⏰ Scheduled check: 16:00 MSK - Users with expired subscriptions")
                try:
                    await _send_notifications_for_expired(bot)
                except Exception as e:
                    logger.error(f"Error in 16:00 check: {e}", exc_info=True)
                # Ждём минуту чтобы не повторить
                await asyncio.sleep(60)
                
            elif hour == 20 and minute == 0:
                logger.info("⏰ Scheduled check: 20:00 MSK - Users with <24h left")
                try:
                    await _send_notifications_for_expiring(bot)
                except Exception as e:
                    logger.error(f"Error in 20:00 check: {e}", exc_info=True)
                # Ждём минуту чтобы не повторить
                await asyncio.sleep(60)
            
            # Проверяем каждые 30 секунд (не будем крутиться вечно в цикле)
            await asyncio.sleep(30)
            
    except asyncio.CancelledError:
        logger.info("Scheduled notification service shut down gracefully")
        raise


async def _send_notifications_for_expiring(bot):
    """
    Найти и отправить уведомления пользователям у которых до конца подписки <24h
    Соблюдает лимиты Telegram API
    """
    try:
        logger.info("🔍 Searching for users with <24h left until subscription expires...")
        
        # Находим пользователей с активной подпиской, заканчивающейся в ближайшие 24 часа
        users = await db.db_execute(
            """
            SELECT tg_id, remnawave_uuid, subscription_until
            FROM users
            WHERE subscription_until IS NOT NULL
            AND subscription_until > now() AT TIME ZONE 'UTC'
            AND subscription_until <= (now() AT TIME ZONE 'UTC') + INTERVAL '24 hours'
            ORDER BY subscription_until ASC
            """,
            fetch_all=True
        )
        
        if not users:
            logger.info("No users found with <24h left")
            return
        
        logger.info(f"📤 Found {len(users)} users with <24h left, sending notifications with rate limiting...")
        
        # Обрабатываем пользователей батчами с соблюдением rate limits
        success_count = 0
        error_count = 0
        
        for i, user in enumerate(users):
            try:
                tg_id = user['tg_id']
                subscription_until = ensure_utc_aware(user['subscription_until'])

                # Если не удалось получить время подписки, пропускаем
                if subscription_until is None:
                    continue

                now = datetime.now(timezone.utc)
                time_left = subscription_until - now
                
                days_left = time_left.days
                hours_left = (time_left.seconds // 3600)
                minutes_left = (time_left.seconds % 3600) // 60
                
                # Формируем сообщение
                if days_left > 0:
                    time_str = f"{days_left} дн. {hours_left} ч."
                else:
                    time_str = f"{hours_left} ч. {minutes_left} мин."
                
                text = (
                    "⏰ <b>Ваша подписка скоро закончится!</b>\n\n"
                    f"Осталось: <b>{time_str}</b>\n\n"
                    "Продлите подписку, чтобы не потерять доступ к быстрой и безопасной сети!"
                )
                
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="buy_subscription")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
                ])
                
                # Отправляем сообщение
                await bot.send_message(tg_id, text, reply_markup=kb)
                success_count += 1
                logger.debug(f"✅ Notification sent to user {tg_id} ({days_left}d {hours_left}h left)")
                
            except TelegramAPIError as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    # Если получили 429, ждём перед тем как продолжить
                    logger.warning(f"🚫 Rate limited! Waiting before continuing...")
                    await asyncio.sleep(5)
                    error_count += 1
                elif "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    # Бот был заблокирован или аккаунт деактивирован - не логируем как ошибку
                    logger.debug(f"User {user.get('tg_id')} blocked the bot or deactivated account")
                else:
                    logger.error(f"Failed to send notification to user {user.get('tg_id')}: {e}")
                    error_count += 1
            except Exception as e:
                logger.error(f"Unexpected error sending notification to user {user.get('tg_id')}: {e}")
                error_count += 1
            
            # Соблюдаем rate limit между сообщениями
            if i < len(users) - 1:  # Не ждём после последнего сообщения
                await asyncio.sleep(TELEGRAM_RATE_LIMIT)
        
        logger.info(f"✅ Notification batch complete: {success_count} sent, {error_count} errors")
        
    except Exception as e:
        logger.error(f"Error in _send_notifications_for_expiring: {e}", exc_info=True)


async def _send_notifications_for_expired(bot):
    """
    Найти и отправить уведомления пользователям у которых подписка уже закончилась
    Соблюдает лимиты Telegram API
    """
    try:
        logger.info("🔍 Searching for users with expired subscriptions...")
        
        # Находим пользователей подписка которых уже закончилась
        users = await db.db_execute(
            """
            SELECT tg_id, remnawave_uuid, subscription_until
            FROM users
            WHERE subscription_until IS NOT NULL
            AND subscription_until <= now() AT TIME ZONE 'UTC'
            AND remnawave_uuid IS NOT NULL
            ORDER BY subscription_until DESC
            """,
            fetch_all=True
        )
        
        if not users:
            logger.info("No users found with expired subscriptions")
            return
        
        logger.info(f"📤 Found {len(users)} users with expired subscriptions, sending notifications with rate limiting...")
        
        # Обрабатываем пользователей батчами с соблюдением rate limits
        success_count = 0
        error_count = 0
        
        for i, user in enumerate(users):
            try:
                tg_id = user['tg_id']
                subscription_until = ensure_utc_aware(user['subscription_until'])

                # Если не удалось получить время подписки, пропускаем
                if subscription_until is None:
                    continue

                now = datetime.now(timezone.utc)
                days_expired = (now - subscription_until).days
                
                text = (
                    "❌ <b>Ваша подписка закончилась!</b>\n\n"
                    f"Закончилась: <b>{days_expired} дн. назад</b>\n\n"
                    "Продлите подписку, чтобы вернуть доступ к быстрой и безопасной сети!"
                )
                
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="buy_subscription")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
                ])
                
                # Отправляем сообщение
                await bot.send_message(tg_id, text, reply_markup=kb)
                success_count += 1
                logger.debug(f"✅ Expiry notification sent to user {tg_id} (expired {days_expired}d ago)")
                
            except TelegramAPIError as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    # Если получили 429, ждём перед тем как продолжить
                    logger.warning(f"🚫 Rate limited! Waiting before continuing...")
                    await asyncio.sleep(5)
                    error_count += 1
                elif "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    # Бот был заблокирован или аккаунт деактивирован - не логируем как ошибку
                    logger.debug(f"User {user.get('tg_id')} blocked the bot or deactivated account")
                else:
                    logger.error(f"Failed to send notification to user {user.get('tg_id')}: {e}")
                    error_count += 1
            except Exception as e:
                logger.error(f"Unexpected error sending notification to user {user.get('tg_id')}: {e}")
                error_count += 1
            
            # Соблюдаем rate limit между сообщениями
            if i < len(users) - 1:  # Не ждём после последнего сообщения
                await asyncio.sleep(TELEGRAM_RATE_LIMIT)
        
        logger.info(f"✅ Expiry notification batch complete: {success_count} sent, {error_count} errors")
        
    except Exception as e:
        logger.error(f"Error in _send_notifications_for_expired: {e}", exc_info=True)
