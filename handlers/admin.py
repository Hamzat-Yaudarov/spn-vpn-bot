import logging
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


def validate_tg_id(tg_id: int) -> bool:
    """Валидировать Telegram ID"""
    return isinstance(tg_id, int) and 0 < tg_id < 10**15


def validate_days(days: int) -> bool:
    """Валидировать количество дней"""
    return isinstance(days, int) and 0 < days <= 3650  # макс 10 лет


def validate_promo_code(code: str) -> bool:
    """Валидировать промокод"""
    if not isinstance(code, str):
        return False
    code = code.strip()
    return 3 <= len(code) <= 50 and code.isalnum()


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

        # Валидация
        if not validate_promo_code(code):
            raise ValueError("Invalid promo code format (3-50 alphanumeric characters)")
        
        if not validate_days(days):
            raise ValueError(f"Invalid days: {days} (must be 1-3650)")
        
        if limit <= 0 or limit > 100000:
            raise ValueError(f"Invalid limit: {limit} (must be 1-100000)")

    except (ValueError, IndexError) as e:
        await message.answer(
            "❌ Неверный формат команды\n\n"
            "<b>Формат:</b> /new_code КОД ДНЕЙ ЛИМИТ\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100\n\n"
            f"<b>Ошибка:</b> {str(e)}"
        )
        logger.error(f"Admin {admin_id} /new_code parsing error: {e}")
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

        logger.info(f"[ADMIN:{admin_id}] Created promo code: {code.upper()} (days={days}, limit={limit})")

    except Exception as e:
        await message.answer(f"❌ Ошибка при создании промокода: {str(e)}")
        logger.error(f"Admin {admin_id} /new_code error: {e}", exc_info=True)


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

        # Валидация
        if not validate_tg_id(tg_id):
            raise ValueError(f"Invalid tg_id: {tg_id}")
        
        if not validate_days(days):
            raise ValueError(f"Invalid days: {days} (must be 1-3650)")

    except (ValueError, IndexError) as e:
        await message.answer(
            "❌ Неверный формат команды\n\n"
            "<b>Формат:</b> /give_sub ТГ_ИД ДНЕЙ\n\n"
            "<b>Пример:</b> /give_sub 123456789 30\n\n"
            f"<b>Ошибка:</b> {str(e)}"
        )
        logger.error(f"Admin {admin_id} /give_sub parsing error: {e}")
        return

    async with db.UserLockContext(tg_id) as acquired:
        if not acquired:
            await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
            return

        try:
            # Убедимся что пользователь существует в БД
            if not await db.user_exists(tg_id):
                await db.create_user(tg_id, f"user_{tg_id}")
                logger.info(f"[ADMIN:{admin_id}] Created new user {tg_id} in database")

            from main import get_global_session
            
            session = get_global_session()
            
            # Создаём или получаем пользователя в Remnawave
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days=days, extend_if_exists=True
            )

            if not uuid:
                await message.answer(f"❌ Ошибка при работе с Remnawave API для пользователя {tg_id}")
                logger.error(f"[ADMIN:{admin_id}] Failed to get/create Remnawave user for TG {tg_id}")
                return

            # Добавляем в сквад
            squad_added = await remnawave_add_to_squad(session, uuid)
            if not squad_added:
                logger.warning(f"[ADMIN:{admin_id}] Failed to add user {uuid} to squad, continuing anyway")

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
                logger.info(f"[ADMIN:{admin_id}] User {tg_id} notified about subscription")
            except Exception as e:
                logger.warning(f"[ADMIN:{admin_id}] Failed to notify user {tg_id}: {e}")

            logger.info(f"[ADMIN:{admin_id}] Gave subscription to user {tg_id} for {days} days")

        except Exception as e:
            logger.error(f"[ADMIN:{admin_id}] Give subscription error: {e}", exc_info=True)
            await message.answer(f"❌ Ошибка при выдаче подписки: {str(e)}")


@router.message(Command("stats"))
async def admin_stats(message: Message):
    """Админ команда: получить статистику"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"User {admin_id} tried to use /stats without admin permissions")
        return

    try:
        # Получаем статистику из БД
        stats = await db.get_overall_stats()

        if not stats:
            await message.answer("❌ Ошибка при получении статистики")
            return

        text = (
            "📊 <b>СТАТИСТИКА БОТА</b>\n\n"
            f"<b>👥 Пользователи:</b>\n"
            f"  Всего: <code>{stats['total_users']}</code>\n"
            f"  С активной подпиской: <code>{stats['active_subscriptions']}</code>\n"
            f"  Приняли условия: <code>{stats['accepted_terms']}</code>\n\n"
            f"<b>💰 Платежи:</b>\n"
            f"  Всего успешных: <code>{stats['paid_payments']}</code>\n"
            f"  Ожидающих: <code>{stats['pending_payments']}</code>\n"
            f"  Общая сумма: <code>{stats['total_revenue']} ₽</code>\n\n"
            f"<b>🎁 Подарки:</b>\n"
            f"  Выданных: <code>{stats['gifts_given']}</code>\n\n"
            f"👥 <b>Рефералы:</b>\n"
            f"  Всего рефералов: <code>{stats['total_referrals']}</code>\n"
            f"  Активных рефералов: <code>{stats['active_referrals']}</code>\n\n"
            f"🎟 <b>Промокоды:</b>\n"
            f"  Использовано: <code>{stats['promos_used']}</code>\n"
        )

        await message.answer(text)
        logger.info(f"[ADMIN:{admin_id}] Requested stats")

    except Exception as e:
        logger.error(f"[ADMIN:{admin_id}] Stats error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при получении статистики: {str(e)}")
