import logging
import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import database as db
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError
from services.remnawave import remnawave_get_user_info


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
    Информация берётся прямо из Remnawave API для точности
    Соблюдает лимиты Telegram API
    """
    try:
        logger.info("🔍 Searching for users with <24h left (checking Remnawave)...")

        # Находим всех пользователей с remnawave_uuid
        users = await db.db_execute(
            """
            SELECT tg_id, remnawave_uuid
            FROM users
            WHERE remnawave_uuid IS NOT NULL
            ORDER BY tg_id ASC
            """,
            fetch_all=True
        )

        if not users:
            logger.info("No users found with Remnawave UUID")
            return

        logger.info(f"📤 Found {len(users)} users with Remnawave UUID, checking their subscription status...")

        # Обрабатываем пользователей батчами с соблюдением rate limits
        success_count = 0
        error_count = 0
        users_to_notify = []

        # Сначала получаем информацию из Remnawave для всех пользователей
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            now = datetime.now(timezone.utc)

            for user in users:
                try:
                    tg_id = user['tg_id']
                    remnawave_uuid = user['remnawave_uuid']

                    # Получаем информацию из Remnawave
                    user_info = await remnawave_get_user_info(session, remnawave_uuid)

                    if not user_info or 'expireAt' not in user_info:
                        logger.debug(f"Could not get Remnawave info for user {tg_id}")
                        continue

                    # Парсим дату окончания подписки из Remnawave
                    expire_at_str = user_info['expireAt']
                    expire_at = datetime.fromisoformat(expire_at_str.replace('Z', '+00:00'))
                    expire_at = ensure_utc_aware(expire_at)

                    # Проверяем есть ли <24h до конца подписки
                    time_left = expire_at - now

                    # Если подписка активна И закончится в ближайшие 24 часа
                    if time_left.total_seconds() > 0 and time_left.total_seconds() <= 86400:  # 86400 = 24 hours
                        users_to_notify.append({
                            'tg_id': tg_id,
                            'expire_at': expire_at,
                            'time_left': time_left
                        })

                except Exception as e:
                    logger.warning(f"Error checking Remnawave info for user {user.get('tg_id')}: {e}")
                    error_count += 1

        if not users_to_notify:
            logger.info("No users found with <24h left in Remnawave")
            return

        logger.info(f"📤 Found {len(users_to_notify)} users with <24h left, sending notifications...")

        # Отправляем уведомления
        for i, user_data in enumerate(users_to_notify):
            try:
                tg_id = user_data['tg_id']
                time_left = user_data['time_left']

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
                logger.debug(f"✅ Notification sent to user {tg_id} ({days_left}d {hours_left}h left from Remnawave)")

            except TelegramAPIError as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    # Если получили 429, ждём перед тем как продолжить
                    logger.warning(f"🚫 Rate limited! Waiting before continuing...")
                    await asyncio.sleep(5)
                    error_count += 1
                elif "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    # Бот был заблокирован или аккаунт деактивирован - не логируем как ошибку
                    logger.debug(f"User {tg_id} blocked the bot or deactivated account")
                else:
                    logger.error(f"Failed to send notification to user {tg_id}: {e}")
                    error_count += 1
            except Exception as e:
                logger.error(f"Unexpected error sending notification to user {user_data.get('tg_id')}: {e}")
                error_count += 1

            # Соблюдаем rate limit между сообщениями
            if i < len(users_to_notify) - 1:  # Не ждём после последнего сообщения
                await asyncio.sleep(TELEGRAM_RATE_LIMIT)

        logger.info(f"✅ Expiry notification batch complete: {success_count} sent, {error_count} errors")

    except Exception as e:
        logger.error(f"Error in _send_notifications_for_expiring: {e}", exc_info=True)


async def _send_notifications_for_expired(bot):
    """
    Найти и отправить уведомления пользователям у которых подписка уже закончилась
    Информация берётся прямо из Remnawave API для точности
    Соблюдает лимиты Telegram API
    """
    try:
        logger.info("🔍 Searching for users with expired subscriptions (checking Remnawave)...")

        # Находим всех пользователей в БД
        all_users = await db.db_execute(
            """
            SELECT tg_id, remnawave_uuid
            FROM users
            ORDER BY tg_id ASC
            """,
            fetch_all=True
        )

        if not all_users:
            logger.info("No users found in database")
            return

        logger.info(f"📤 Found {len(all_users)} users in database, checking their subscription status in Remnawave...")

        # Обрабатываем пользователей батчами с соблюдением rate limits
        success_count = 0
        error_count = 0
        users_to_notify = []

        # Сначала получаем информацию из Remnawave для пользователей с uuid
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            now = datetime.now(timezone.utc)

            for user in all_users:
                try:
                    tg_id = user['tg_id']
                    remnawave_uuid = user['remnawave_uuid']

                    has_active_subscription = False
                    message_type = None
                    days_expired = None

                    if remnawave_uuid:
                        # Получаем информацию из Remnawave
                        user_info = await remnawave_get_user_info(session, remnawave_uuid)

                        if user_info and 'expireAt' in user_info:
                            # Парсим дату окончания подписки из Remnawave
                            expire_at_str = user_info['expireAt']
                            expire_at = datetime.fromisoformat(expire_at_str.replace('Z', '+00:00'))
                            expire_at = ensure_utc_aware(expire_at)

                            # Проверяем активна ли подписка
                            if expire_at > now:
                                has_active_subscription = True
                            else:
                                # Подписка истекла
                                days_expired = (now - expire_at).days
                                message_type = "expired"
                        else:
                            # Нет информации - считаем что подписки нет
                            message_type = "no_subscription"
                    else:
                        # Нет UUID - пользователь никогда не платил или информация потеряна
                        message_type = "no_subscription"

                    # Если подписка неактивна, добавляем в список для уведомления
                    if not has_active_subscription:
                        users_to_notify.append({
                            'tg_id': tg_id,
                            'message_type': message_type,
                            'days_expired': days_expired
                        })

                except Exception as e:
                    logger.warning(f"Error checking Remnawave info for user {user.get('tg_id')}: {e}")
                    error_count += 1

        if not users_to_notify:
            logger.info("No users with expired/no subscriptions found in Remnawave")
            return

        logger.info(f"📤 Found {len(users_to_notify)} users with expired/no subscriptions, sending notifications...")

        # Отправляем уведомления
        for i, user_data in enumerate(users_to_notify):
            try:
                tg_id = user_data['tg_id']
                message_type = user_data['message_type']
                days_expired = user_data['days_expired']

                # Формируем сообщение в зависимости от типа
                if message_type == "no_subscription":
                    # Пользователь никогда не имел подписку
                    text = (
                        "❌ <b>У вас нет активной подписки!</b>\n\n"
                        "Приобретите подписку, чтобы получить доступ к быстрой и безопасной сети!"
                    )
                    log_msg = "no subscription"
                else:
                    # Пользователь имел подписку, но она закончилась
                    text = (
                        "❌ <b>Ваша подписка закончилась!</b>\n\n"
                        f"Закончилась: <b>{days_expired} дн. назад</b>\n\n"
                        "Продлите подписку, чтобы вернуть доступ к быстрой и безопасной сети!"
                    )
                    log_msg = f"expired {days_expired}d ago"

                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="buy_subscription")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
                ])

                # Отправляем сообщение
                await bot.send_message(tg_id, text, reply_markup=kb)
                success_count += 1
                logger.debug(f"✅ Notification sent to user {tg_id} ({log_msg}) from Remnawave")

            except TelegramAPIError as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    # Если получили 429, ждём перед тем как продолжить
                    logger.warning(f"🚫 Rate limited! Waiting before continuing...")
                    await asyncio.sleep(5)
                    error_count += 1
                elif "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    # Бот был заблокирован или аккаунт деактивирован - не логируем как ошибку
                    logger.debug(f"User {user_data.get('tg_id')} blocked the bot or deactivated account")
                else:
                    logger.error(f"Failed to send notification to user {user_data.get('tg_id')}: {e}")
                    error_count += 1
            except Exception as e:
                logger.error(f"Unexpected error sending notification to user {user_data.get('tg_id')}: {e}")
                error_count += 1

            # Соблюдаем rate limit между сообщениями
            if i < len(users_to_notify) - 1:  # Не ждём после последнего сообщения
                await asyncio.sleep(TELEGRAM_RATE_LIMIT)

        logger.info(f"✅ Expired notification batch complete: {success_count} sent, {error_count} errors")

    except Exception as e:
        logger.error(f"Error in _send_notifications_for_expired: {e}", exc_info=True)
