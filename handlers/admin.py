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
        # Убедимся что пользователь существует в БД
        if not await db.user_exists(tg_id):
            await db.create_user(tg_id, f"user_{tg_id}")
            logger.info(f"Created new user {tg_id} in database for admin {admin_id}")

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Создаём или получаем пользователя в Remnawave
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days=days, extend_if_exists=True
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

            # Обновляем подписку в БД
            new_until = datetime.utcnow() + timedelta(days=days)
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
