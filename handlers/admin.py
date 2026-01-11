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

    try:
        parts = message.text.split()
        if len(parts) < 4:
            raise ValueError("Not enough arguments")

        code = parts[1]
        days = int(parts[2])
        limit = int(parts[3])

        if days <= 0 or limit <= 0:
            raise ValueError("Days and limit must be positive numbers")

    except (ValueError, IndexError) as e:
        await message.answer(
            "❌ Неверный формат команды\n\n"
            "<b>Формат:</b> /new_code КОД ДНЕЙ ЛИМИТ\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100"
        )
        logger.error(f"Admin {admin_id} /new_code parsing error: {e}")
        return

    try:
        # Создаём промокод
        db.create_promo_code(code.upper(), days, limit)

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

    try:
        parts = message.text.split()
        if len(parts) < 3:
            raise ValueError("Not enough arguments")

        tg_id_str = parts[1]
        days_str = parts[2]

        tg_id = int(tg_id_str)
        days = int(days_str)

        if days <= 0:
            raise ValueError("Days must be a positive number")

    except (ValueError, IndexError) as e:
        await message.answer(
            "❌ Неверный формат команды\n\n"
            "<b>Формат:</b> /give_sub ТГ_ИД ДНЕЙ\n\n"
            "<b>Пример:</b> /give_sub 123456789 30"
        )
        logger.error(f"Admin {admin_id} /give_sub parsing error: {e}")
        return

    if not db.acquire_user_lock(tg_id):
        await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
        return

    try:
        # Убедимся что пользователь существует в БД
        if not db.user_exists(tg_id):
            db.create_user(tg_id, f"user_{tg_id}")
            logger.info(f"Created new user {tg_id} in database")

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
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
            db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

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
        db.release_user_lock(tg_id)


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
