import asyncio
import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from config import ADMIN_ID, DEFAULT_SQUAD_UUID
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_set_subscription_expiry,
    remnawave_get_user_info
)

logger = logging.getLogger(__name__)

router = Router()


def is_admin(user_id: int) -> bool:
    """Проверить является ли пользователь администратором"""
    return user_id == ADMIN_ID


@router.message(Command("new_code"))
async def admin_new_code(message: Message):
    """Админ команда: создать новый промокод"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /new_code attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 4:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /new_code КОД ДНЕЙ ЛИМИТ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>КОД</code> - код промокода (только буквы и цифры)\n"
            "• <code>ДНЕЙ</code> - количество дней (число > 0)\n"
            "• <code>ЛИМИТ</code> - максимум использований (число > 0)\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100"
        )
        logger.warning(f"Admin {admin_id} /new_code - wrong number of arguments: {len(parts)-1}")
        return

    try:
        code = parts[1].strip()
        days = int(parts[2])
        limit = int(parts[3])

        # Валидация значений
        if not code or not code.isalnum():
            await message.answer("❌ Код промокода должен содержать только буквы и цифры")
            return

        if len(code) < 3:
            await message.answer("❌ Код промокода должен быть не менее 3 символов")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if limit <= 0:
            await message.answer("❌ Лимит использований должен быть больше 0")
            return

        # Создаём промокод
        await db.create_promo_code(code.upper(), days, limit)

        await message.answer(
            f"✅ <b>Промокод создан успешно!</b>\n\n"
            f"<b>Код:</b> <code>{code.upper()}</code>\n"
            f"<b>Дней подписки:</b> {days}\n"
            f"<b>Лимит использований:</b> {limit}\n"
            f"<b>Статус:</b> активен"
        )

        logger.info(f"Admin {admin_id} created promo code: {code.upper()} (days={days}, limit={limit})")

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ДНЕЙ и ЛИМИТ - целые числа\n"
            "• Оба числа больше 0\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100"
        )
        logger.warning(f"Admin {admin_id} /new_code - parsing error for arguments: {parts[1:]}")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы данных: {str(e)[:100]}")
        logger.error(f"Admin {admin_id} /new_code database error: {e}")


@router.message(Command("give_sub"))
async def admin_give_sub(message: Message):
    """Админ команда: выдать/продлить подписку пользователю по ИД"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /give_sub attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /give_sub ТГ_ИД ДНЕЙ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>ДНЕЙ</code> - количество дней (число > 0)\n\n"
            "<b>Пример:</b> /give_sub 123456789 30"
        )
        logger.warning(f"Admin {admin_id} /give_sub - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        days = int(parts[2])

        # Валидация значений
        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя выдать подписку самому себе")
            logger.warning(f"Admin {admin_id} tried to give subscription to themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД и ДНЕЙ - целые числа\n"
            "• Оба числа больше 0\n\n"
            "<b>Пример:</b> /give_sub 123456789 30"
        )
        logger.warning(f"Admin {admin_id} /give_sub - parsing error for arguments: {parts[1:]}")
        return

    if not await db.acquire_user_lock(tg_id):
        await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
        logger.info(f"Admin {admin_id} /give_sub - could not acquire lock for user {tg_id}")
        return

    try:
        # Получаем существующего пользователя или создаём нового
        user = await db.get_user(tg_id)
        new_until = None

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Проверяем есть ли у пользователя UUID в Remnawave
            remnawave_uuid = user.get('remnawave_uuid') if user else None

            if remnawave_uuid:
                # Пользователь существует в Remnawave - получаем актуальную дату окончания
                user_info = await remnawave_get_user_info(session, remnawave_uuid)
                if user_info and 'expireAt' in user_info:
                    # Парсим дату окончания подписки
                    expire_at_str = user_info['expireAt']
                    current_until = datetime.fromisoformat(expire_at_str.replace('Z', '+00:00'))
                    # Конвертируем в naive UTC
                    current_until = current_until.replace(tzinfo=None)
                    # Добавляем дни к существующей подписке
                    new_until = current_until + timedelta(days=days)
                    logger.info(f"User {tg_id} has existing subscription in Remnawave until {current_until}, extending by {days} days to {new_until}")
                else:
                    # Ошибка получения информации из Remnawave, используем БД или создаём новую
                    logger.warning(f"Failed to get Remnawave info for {tg_id}, using default calculation")
                    new_until = datetime.utcnow() + timedelta(days=days)
            else:
                # Пользователя нет в Remnawave - создаём новую подписку
                new_until = datetime.utcnow() + timedelta(days=days)
                logger.info(f"User {tg_id} has no Remnawave account, setting new subscription to {new_until}")

            # Убедимся что пользователь существует в БД
            if not user:
                await db.create_user(tg_id, f"user_{tg_id}")
                logger.info(f"Created new user {tg_id} in database for admin {admin_id}")

            # Создаём или получаем пользователя в Remnawave (с минимальными днями для создания)
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days=30, extend_if_exists=False
            )

            if not uuid:
                await message.answer(
                    f"❌ <b>Ошибка Remnawave API</b>\n\n"
                    f"Не удалось создать/обновить аккаунт для пользователя {tg_id}\n\n"
                    "Попробуй позже"
                )
                logger.error(f"Failed to get/create Remnawave user for TG {tg_id} by admin {admin_id}")
                return

            # Добавляем в сквад
            squad_added = await remnawave_add_to_squad(session, uuid)
            if not squad_added:
                logger.warning(f"Failed to add user {uuid} to squad by admin {admin_id}, continuing")

            # Устанавливаем точную дату окончания подписки в Remnawave
            success = await remnawave_set_subscription_expiry(session, uuid, new_until)
            if not success:
                logger.warning(f"Failed to set subscription expiry in Remnawave for {tg_id}, but continuing")

            # Обновляем подписку в БД с рассчитанной датой
            await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

        await message.answer(
            f"✅ <b>Подписка выдана успешно!</b>\n\n"
            f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
            f"📅 <b>Дней:</b> {days}\n"
            f"🔑 <b>UUID:</b> <code>{uuid}</code>"
        )

        # Уведомляем пользователя
        try:
            await message.bot.send_message(
                tg_id,
                f"🎉 <b>Поздравляем!</b>\n\n"
                f"Вам выдана подписка SPN VPN на <b>{days} дней</b>\n\n"
                f"Спасибо за использование нашего сервиса! 🚀"
            )
            logger.info(f"User {tg_id} notified about subscription by admin {admin_id}")
        except Exception as e:
            logger.warning(f"Failed to notify user {tg_id}: {e}")
            await message.answer(
                f"⚠️ Подписка выдана, но не удалось отправить уведомление пользователю\n"
                f"(Ошибка: {str(e)[:50]})"
            )

        logger.info(f"Admin {admin_id} gave {days} days subscription to user {tg_id}")

    except Exception as e:
        logger.error(f"Give subscription error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

    finally:
        await db.release_user_lock(tg_id)


@router.message(Command("take_sub"))
async def admin_take_sub(message: Message):
    """Админ команда: забрать подписку пользователю (уменьшить на N дней)"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /take_sub attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /take_sub ТГ_ИД ДНЕЙ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>ДНЕЙ</code> - количество дней для удаления (число > 0)\n\n"
            "<b>Пример:</b> /take_sub 123456789 10\n\n"
            "<i>Если дней больше чем осталось, подписка будет аннулирована</i>"
        )
        logger.warning(f"Admin {admin_id} /take_sub - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        days = int(parts[2])

        # Валидация значений
        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя отобрать подписку самому себе")
            logger.warning(f"Admin {admin_id} tried to take subscription from themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД и ДНЕЙ - целые числа\n"
            "• Оба числа больше 0\n\n"
            "<b>Пример:</b> /take_sub 123456789 10"
        )
        logger.warning(f"Admin {admin_id} /take_sub - parsing error for arguments: {parts[1:]}")
        return

    if not await db.acquire_user_lock(tg_id):
        await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
        logger.info(f"Admin {admin_id} /take_sub - could not acquire lock for user {tg_id}")
        return

    try:
        # Получаем информацию о пользователе
        user = await db.get_user(tg_id)

        if not user:
            await message.answer(f"❌ Пользователь {tg_id} не найден в БД")
            logger.warning(f"Admin {admin_id} tried to take subscription from non-existent user {tg_id}")
            return

        remnawave_uuid = user.get('remnawave_uuid')

        if not remnawave_uuid:
            await message.answer(f"❌ Пользователь {tg_id} не имеет активной подписки")
            logger.info(f"Admin {admin_id} /take_sub - user {tg_id} has no Remnawave UUID")
            return

        # Получаем актуальную информацию о подписке из Remnawave
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            user_info = await remnawave_get_user_info(session, remnawave_uuid)

        if not user_info or 'expireAt' not in user_info:
            await message.answer(f"❌ Не удалось получить информацию о подписке из Remnawave")
            logger.warning(f"Admin {admin_id} /take_sub - failed to get user info from Remnawave for {tg_id}")
            return

        # Парсим дату окончания подписки из Remnawave (она в ISO формате)
        expire_at_str = user_info['expireAt']
        current_subscription_until = datetime.fromisoformat(expire_at_str.replace('Z', '+00:00'))
        # Конвертируем в naive UTC для сравнения
        current_subscription_until = current_subscription_until.replace(tzinfo=None)

        # Рассчитываем новое время окончания подписки
        new_subscription_until = current_subscription_until - timedelta(days=days)
        now = datetime.utcnow()

        # Логируем информацию для отладки
        logger.info(f"Admin {admin_id} /take_sub user {tg_id}: current_until={current_subscription_until}, removing {days} days, new_until={new_subscription_until}, now={now}")

        # Если новое время в прошлом, аннулируем подписку
        if new_subscription_until <= now:
            # Сначала обновляем Remnawave если есть UUID
            if remnawave_uuid:
                connector = aiohttp.TCPConnector(ssl=False)
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    # Устанавливаем дату окончания в прошлое чтобы деактивировать
                    success = await remnawave_set_subscription_expiry(
                        session,
                        remnawave_uuid,
                        now - timedelta(seconds=1)
                    )
                    if not success:
                        logger.warning(f"Failed to update Remnawave for user {tg_id}, continuing")

            # Обновляем БД - аннулируем подписку
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE users
                        SET remnawave_uuid = NULL,
                            remnawave_username = NULL,
                            subscription_until = NULL,
                            squad_uuid = NULL,
                            next_notification_time = NULL,
                            notification_type = NULL
                        WHERE tg_id = $1
                        """,
                        tg_id
                    )

            await message.answer(
                f"✅ <b>Подписка аннулирована</b>\n\n"
                f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
                f"📅 <b>Удалено дней:</b> {days}\n"
                f"❌ <b>Статус:</b> подписка истекла"
            )

            # Уведомляем пользователя
            try:
                await message.bot.send_message(
                    tg_id,
                    f"❌ <b>Ваша подписка была аннулирована</b>\n\n"
                    f"Администратор удалил подписку на {days} дней.\n\n"
                    f"Ваш доступ закрыт. Пожалуйста, свяжитесь с поддержкой."
                )
                logger.info(f"User {tg_id} notified about subscription cancellation by admin {admin_id}")
            except Exception as e:
                logger.warning(f"Failed to notify user {tg_id}: {e}")

            logger.info(f"Admin {admin_id} cancelled subscription for user {tg_id} (removed {days} days)")

        else:
            # Сначала обновляем Remnawave если есть UUID
            if remnawave_uuid:
                connector = aiohttp.TCPConnector(ssl=False)
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    success = await remnawave_set_subscription_expiry(
                        session,
                        remnawave_uuid,
                        new_subscription_until
                    )
                    if not success:
                        logger.warning(f"Failed to update Remnawave for user {tg_id}, but continuing with DB update")

            # Обновляем подписку в БД
            # Пересчитываем следующее уведомление на основе нового времени подписки
            next_notification = new_subscription_until - timedelta(days=1.5)
            notification_type = "1day_left" if next_notification > now else None

            pool = await db.get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE users
                        SET subscription_until = $1,
                            next_notification_time = $2,
                            notification_type = $3
                        WHERE tg_id = $4
                        """,
                        new_subscription_until,
                        next_notification if next_notification > now else None,
                        notification_type,
                        tg_id
                    )

            remaining_days = (new_subscription_until - now).days
            remaining_hours = ((new_subscription_until - now).seconds // 3600) % 24

            await message.answer(
                f"✅ <b>Подписка обновлена</b>\n\n"
                f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
                f"📅 <b>Удалено дней:</b> {days}\n"
                f"⏰ <b>Осталось:</b> {remaining_days}д {remaining_hours}ч\n"
                f"🟢 <b>Статус:</b> активна"
            )

            # Уведомляем пользователя
            try:
                await message.bot.send_message(
                    tg_id,
                    f"⚠️ <b>Ваша подписка была сокращена</b>\n\n"
                    f"Администратор удалил {days} дней из вашей подписки.\n\n"
                    f"⏰ <b>Осталось:</b> <b>{remaining_days}д {remaining_hours}ч</b>"
                )
                logger.info(f"User {tg_id} notified about subscription reduction by admin {admin_id}")
            except Exception as e:
                logger.warning(f"Failed to notify user {tg_id}: {e}")

            logger.info(f"Admin {admin_id} took {days} days subscription from user {tg_id}, remaining: {remaining_days}д")

    except Exception as e:
        logger.error(f"Take subscription error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

    finally:
        await db.release_user_lock(tg_id)


@router.message(Command("enable_collab"))
async def admin_enable_collab(message: Message):
    """Админ команда: активировать партнёрство"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /enable_collab attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /enable_collab ТГ_ИД %\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>%</code> - процент дохода (15, 20, 25 или 30)\n\n"
            "<b>Пример:</b> /enable_collab 123456789 20"
        )
        logger.warning(f"Admin {admin_id} /enable_collab - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        percentage = int(parts[2])

        # Валидация значений
        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if percentage not in [15, 20, 25, 30]:
            await message.answer("❌ Процент должен быть одним из: 15, 20, 25, 30")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя активировать партнёрство самому себе")
            logger.warning(f"Admin {admin_id} tried to enable collab for themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД и % - целые числа\n"
            "• % - одно из: 15, 20, 25, 30\n\n"
            "<b>Пример:</b> /enable_collab 123456789 20"
        )
        logger.warning(f"Admin {admin_id} /enable_collab - parsing error for arguments: {parts[1:]}")
        return

    # Убедимся что пользователь существует в БД
    if not await db.user_exists(tg_id):
        await db.create_user(tg_id, f"user_{tg_id}")
        logger.info(f"Created new user {tg_id} in database for admin {admin_id}")

    # Создаём партнёрство
    await db.create_partnership(tg_id, percentage)

    await message.answer(
        f"✅ <b>Партнёрство активировано!</b>\n\n"
        f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
        f"💯 <b>Процент дохода:</b> {percentage}%\n"
        f"<b>Статус:</b> активно"
    )

    # Уведомляем пользователя
    try:
        await message.bot.send_message(
            tg_id,
            f"🎉 <b>Поздравляем!</b>\n\n"
            f"Вы активированы как партнёр нашего сервиса!\n\n"
            f"💯 <b>Ваш процент дохода:</b> <b>{percentage}%</b>\n\n"
            f"В главном меню появилась новая кнопка 'Партнёрство' — нажмите на неё, чтобы начать зарабатывать! 💰"
        )
        logger.info(f"User {tg_id} notified about partnership activation by admin {admin_id}")
    except Exception as e:
        logger.warning(f"Failed to notify user {tg_id}: {e}")
        await message.answer(
            f"⚠️ Партнёрство активировано, но не удалось отправить уведомление пользователю\n"
            f"(Ошибка: {str(e)[:50]})"
        )

    logger.info(f"Admin {admin_id} enabled collab for user {tg_id} with percentage {percentage}")


@router.message(Command("stats"))
async def admin_stats(message: Message):
    """Админ команда: получить статистику"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /stats attempt from user {admin_id}")
        return

    try:
        # TODO: Реализовать получение полной статистики из БД
        await message.answer(
            "📊 <b>Статистика бота</b>\n\n"
            "Функция в разработке...\n\n"
            "<i>Будут доступны:</i>\n"
            "• 👥 Количество активных пользователей\n"
            "• 💳 Статистика платежей\n"
            "• 🎁 Активированные подарки\n"
            "• 🎟 Использованные промокоды\n"
            "• 👥 Статистика рефералов"
        )
        logger.info(f"Admin {admin_id} requested /stats")
    except Exception as e:
        await message.answer(f"❌ Ошибка при получении статистики: {str(e)[:100]}")
        logger.error(f"Error getting stats for admin {admin_id}: {e}")


@router.message(Command("all_sms"))
async def admin_broadcast_all(message: Message):
    """Админ команда: отправить сообщение всем пользователям"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /all_sms attempt from user {admin_id}")
        return

    # Извлекаем текст после команды
    text_parts = message.text.split(maxsplit=1)
    if len(text_parts) < 2 or not text_parts[1].strip():
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /all_sms <i>[Сообщение]</i>\n\n"
            "<b>Пример:</b> /all_sms 🎉 Специальное предложение!"
        )
        logger.warning(f"Admin {admin_id} /all_sms - no message provided")
        return

    broadcast_text = text_parts[1]

    try:
        # Получаем всех пользователей из БД
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            users = await conn.fetch("SELECT tg_id FROM users ORDER BY tg_id")

        if not users:
            await message.answer("❌ В БД нет пользователей")
            logger.warning(f"Admin {admin_id} /all_sms - no users found")
            return

        total_users = len(users)
        sent_count = 0
        error_count = 0
        blocked_count = 0
        rate_limited_delay = 0.3  # Начальная задержка между сообщениями (3-4 сообщений в секунду)

        await message.answer(
            f"📤 <b>Начинаю рассылку всем {total_users} пользователям...</b>\n\n"
            f"<i>Это может занять некоторое время...</i>"
        )

        # Отправляем сообщение каждому пользователю с rate limiting
        for user_record in users:
            user_id = user_record['tg_id']
            try:
                await message.bot.send_message(user_id, broadcast_text)
                sent_count += 1
                logger.info(f"Broadcast message sent to user {user_id}")
                # Сбрасываем задержку при успешной отправке
                rate_limited_delay = 0.3
            except Exception as e:
                error_msg = str(e)
                # Проверяем если это ошибка блокировки
                if "blocked user" in error_msg.lower() or "user is deactivated" in error_msg.lower():
                    blocked_count += 1
                    logger.warning(f"User {user_id} has blocked bot or deactivated account")
                # Проверяем если это 429 (Too Many Requests)
                elif "429" in error_msg or "too many requests" in error_msg.lower():
                    error_count += 1
                    # Экспоненциально увеличиваем задержку при 429 ошибке
                    rate_limited_delay = min(rate_limited_delay * 1.5, 3.0)  # Максимум 3 секунды
                    logger.warning(f"Rate limited (429) for user {user_id}. New delay: {rate_limited_delay}s")
                    # Ждём перед следующей попыткой
                    await asyncio.sleep(rate_limited_delay)
                    continue
                else:
                    error_count += 1
                    logger.warning(f"Failed to send broadcast to user {user_id}: {error_msg[:100]}")

            # Rate limiting: задержка между сообщениями
            await asyncio.sleep(rate_limited_delay)

        await message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• ✅ Отправлено: {sent_count}/{total_users}\n"
            f"• 🚫 Заблокировано: {blocked_count}\n"
            f"• ❌ Ошибок: {error_count}\n\n"
            f"<i>Сообщение: {broadcast_text[:50]}...</i>"
        )

        logger.info(f"Admin {admin_id} completed /all_sms broadcast: sent={sent_count}, blocked={blocked_count}, errors={error_count}")

    except Exception as e:
        logger.error(f"Broadcast all error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при рассылке: {str(e)[:100]}")


@router.message(Command("not_sub_sms"))
async def admin_broadcast_no_subscription(message: Message):
    """Админ команда: отправить сообщение только пользователям без активной подписки"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /not_sub_sms attempt from user {admin_id}")
        return

    # Извлекаем текст после команды
    text_parts = message.text.split(maxsplit=1)
    if len(text_parts) < 2 or not text_parts[1].strip():
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /not_sub_sms <i>[Сообщение]</i>\n\n"
            "<b>Пример:</b> /not_sub_sms 💳 Получи подписку со скидкой!"
        )
        logger.warning(f"Admin {admin_id} /not_sub_sms - no message provided")
        return

    broadcast_text = text_parts[1]

    try:
        # Получаем пользователей БЕЗ активной подписки
        # Активная подписка = subscription_until IS NOT NULL AND subscription_until > now()
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            users = await conn.fetch(
                """
                SELECT tg_id FROM users
                WHERE subscription_until IS NULL
                   OR subscription_until <= now() AT TIME ZONE 'UTC'
                ORDER BY tg_id
                """
            )

        if not users:
            await message.answer("❌ Не найдено пользователей без подписки")
            logger.info(f"Admin {admin_id} /not_sub_sms - no users without subscription found")
            return

        total_users = len(users)
        sent_count = 0
        error_count = 0
        blocked_count = 0
        rate_limited_delay = 0.3  # Начальная задержка между сообщениями (3-4 сообщений в секунду)

        await message.answer(
            f"📤 <b>Начинаю рассылку {total_users} пользователям без подписки...</b>\n\n"
            f"<i>Это может занять некоторое время...</i>"
        )

        # Отправляем сообщение каждому пользователю с rate limiting
        for user_record in users:
            user_id = user_record['tg_id']
            try:
                await message.bot.send_message(user_id, broadcast_text)
                sent_count += 1
                logger.info(f"Broadcast message sent to user {user_id} (no subscription)")
                # Сбрасываем задержку при успешной отправке
                rate_limited_delay = 0.3
            except Exception as e:
                error_msg = str(e)
                # Проверяем если это ошибка блокировки
                if "blocked user" in error_msg.lower() or "user is deactivated" in error_msg.lower():
                    blocked_count += 1
                    logger.warning(f"User {user_id} has blocked bot or deactivated account")
                # Проверяем если это 429 (Too Many Requests)
                elif "429" in error_msg or "too many requests" in error_msg.lower():
                    error_count += 1
                    # Экспоненциально увеличиваем задержку при 429 ошибке
                    rate_limited_delay = min(rate_limited_delay * 1.5, 3.0)  # Максимум 3 секунды
                    logger.warning(f"Rate limited (429) for user {user_id}. New delay: {rate_limited_delay}s")
                    # Ждём перед следующей попыткой
                    await asyncio.sleep(rate_limited_delay)
                    continue
                else:
                    error_count += 1
                    logger.warning(f"Failed to send broadcast to user {user_id}: {error_msg[:100]}")

            # Rate limiting: задержка между сообщениями
            await asyncio.sleep(rate_limited_delay)

        await message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• ✅ Отправлено: {sent_count}/{total_users}\n"
            f"• 🚫 Заблокировано: {blocked_count}\n"
            f"• ❌ Ошибок: {error_count}\n\n"
            f"<i>Сообщение: {broadcast_text[:50]}...</i>"
        )

        logger.info(f"Admin {admin_id} completed /not_sub_sms broadcast: sent={sent_count}, blocked={blocked_count}, errors={error_count}")

    except Exception as e:
        logger.error(f"Broadcast no subscription error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при рассылке: {str(e)[:100]}")
