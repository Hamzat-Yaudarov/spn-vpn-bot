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
    remnawave_add_to_squad
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
        logger.warning(f"User {admin_id} tried to use /new_code without admin permissions")
        return

    parts = message.text.split()

    # ⚠️ УЛУЧШЕННАЯ ВАЛИДАЦИЯ
    if len(parts) < 4:
        await message.answer(
            "❌ Не хватает параметров\n\n"
            "<b>Формат:</b> /new_code КОД ДНЕЙ ЛИМИТ\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100"
        )
        logger.warning(f"Admin {admin_id} /new_code: missing arguments")
        return

    code = parts[1]
    days_str = parts[2]
    limit_str = parts[3]

    # Валидация КОД
    if not code or len(code) < 3 or len(code) > 50:
        await message.answer(
            "❌ <b>Ошибка в КОД</b>\n\n"
            "Промокод должен быть от 3 до 50 символов"
        )
        logger.warning(f"Admin {admin_id} /new_code: invalid code format")
        return

    # Валидация ДНЕЙ
    try:
        days = int(days_str)
        if days <= 0 or days > 3650:  # максимум 10 лет
            raise ValueError("out of range")
    except ValueError:
        await message.answer(
            "❌ <b>Ошибка в ДНЕЙ</b>\n\n"
            "Количество дней должно быть числом от 1 до 3650"
        )
        logger.warning(f"Admin {admin_id} /new_code: invalid days value '{days_str}'")
        return

    # Валидация ЛИМИТ
    try:
        limit = int(limit_str)
        if limit <= 0 or limit > 100000:  # максимум 100k использований
            raise ValueError("out of range")
    except ValueError:
        await message.answer(
            "❌ <b>Ошибка в ЛИМИТ</b>\n\n"
            "Лимит должен быть числом от 1 до 100000"
        )
        logger.warning(f"Admin {admin_id} /new_code: invalid limit value '{limit_str}'")
        return

    try:
        # Создаём промокод
        await db.create_promo_code(code.upper(), days, limit)

        await message.answer(
            f"✅ <b>Промокод создан успешно</b>\n\n"
            f"<b>Код:</b> <code>{code.upper()}</code>\n"
            f"<b>Дней подписки:</b> {days}\n"
            f"<b>Лимит использований:</b> {limit}"
        )

        logger.info(f"Admin {admin_id} created promo code: {code.upper()} (days={days}, limit={limit})")

    except Exception as e:
        await message.answer(f"❌ Ошибка при создании промокода: {str(e)}")
        logger.error(f"Admin {admin_id} /new_code error: {e}")


@router.message(Command("give_sub"))
async def admin_give_sub(message: Message):
    """Админ команда: выдать/продлить подписку пользователю по ИД"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"User {admin_id} tried to use /give_sub without admin permissions")
        return

    parts = message.text.split()

    # ⚠️ УЛУЧШЕННАЯ ВАЛИДАЦИЯ
    if len(parts) < 3:
        await message.answer(
            "❌ Не хватает параметров\n\n"
            "<b>Формат:</b> /give_sub ТГ_ИД ДНЕЙ\n\n"
            "<b>Пример:</b> /give_sub 123456789 30"
        )
        logger.warning(f"Admin {admin_id} /give_sub: missing arguments")
        return

    tg_id_str = parts[1]
    days_str = parts[2]

    # Валидация ТГ_ИД
    try:
        tg_id = int(tg_id_str)
        if tg_id <= 0 or tg_id > 9999999999:  # Telegram ID не может быть отрицательным
            raise ValueError("out of range")
    except ValueError:
        await message.answer(
            "❌ <b>Ошибка в ТГ_ИД</b>\n\n"
            "ID должен быть числом без пробелов и спецсимволов\n\n"
            "<b>Пример:</b> 123456789"
        )
        logger.warning(f"Admin {admin_id} /give_sub: invalid tg_id '{tg_id_str}'")
        return

    # Валидация ДНЕЙ
    try:
        days = int(days_str)
        if days <= 0 or days > 3650:  # максимум 10 лет
            raise ValueError("out of range")
    except ValueError:
        await message.answer(
            "❌ <b>Ошибка в ДНЕЙ</b>\n\n"
            "Количество дней должно быть числом от 1 до 3650"
        )
        logger.warning(f"Admin {admin_id} /give_sub: invalid days value '{days_str}'")
        return

    if not await db.acquire_user_lock(tg_id):
        await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
        return

    try:
        # Убедимся что пользователь существует в БД
        if not await db.user_exists(tg_id):
            await db.create_user(tg_id, f"user_{tg_id}")
            logger.info(f"Created new user {tg_id} in database")

        # ⚠️ Добавляем таймаут для сессии (максимум 15 сек)
        timeout = aiohttp.ClientTimeout(total=15, connect=10)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Создаём или получаем пользователя в Remnawave
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days=days, extend_if_exists=True
            )

            if not uuid:
                await message.answer(f"❌ Ошибка при работе с Remnawave API для пользователя {tg_id}")
                logger.error(f"Failed to get/create Remnawave user for TG {tg_id}")
                return

            # Добавляем в сквад
            squad_added = await remnawave_add_to_squad(session, uuid)
            if not squad_added:
                logger.warning(f"Failed to add user {uuid} to squad, continuing anyway")

            # Обновляем подписку в БД
            new_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

        await message.answer(
            f"✅ <b>Подписка выдана успешно</b>\n\n"
            f"<b>Пользователь:</b> {tg_id}\n"
            f"<b>Дней:</b> {days}\n"
            f"<b>Remnawave UUID:</b> <code>{uuid}</code>"
        )

        # Уведомляем пользователя
        try:
            await message.bot.send_message(
                tg_id,
                f"🎉 <b>Поздравляем!</b>\n\n"
                f"Вам выдана подписка SPN VPN на <b>{days} дней</b>\n\n"
                f"Спасибо за использование нашего сервиса! 🚀"
            )
            logger.info(f"User {tg_id} notified about subscription")
        except Exception as e:
            logger.warning(f"Failed to notify user {tg_id}: {e}")

        logger.info(f"Admin {admin_id} gave subscription to user {tg_id} for {days} days")

    except Exception as e:
        logger.error(f"Give subscription error: {e}")
        await message.answer(f"❌ Ошибка при выдаче подписки: {str(e)}")

    finally:
        await db.release_user_lock(tg_id)


@router.message(Command("stats"))
async def admin_stats(message: Message):
    """Админ команда: получить статистику"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"User {admin_id} tried to use /stats without admin permissions")
        return

    # TODO: Реализовать получение статистики из БД
    await message.answer("📊 Статистика ещё не реализована\n\nВ разработке...")
