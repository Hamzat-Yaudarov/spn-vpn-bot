import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import DEFAULT_SQUAD_UUID
from states import UserStates
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url
)
from handlers.start import show_main_menu


router = Router()


@router.callback_query(F.data == "enter_promo")
async def process_enter_promo(callback: CallbackQuery, state: FSMContext):
    """Предложить ввести промокод"""
    await callback.message.edit_text("Введи промокод:")
    await state.set_state(UserStates.waiting_for_promo)


@router.message(UserStates.waiting_for_promo)
async def process_promo_input(message: Message, state: FSMContext):
    """Обработать введённый промокод"""
    code = message.text.strip().upper()
    tg_id = message.from_user.id

    if not db.acquire_user_lock(tg_id):
        await message.answer("Подожди пару секунд ⏳")
        return

    try:
        # Проверяем промокод в БД
        promo = db.get_promo_code(code)

        if not promo or not promo[3] or promo[2] >= promo[1]:  # active и used_count < max_uses
            await message.answer("❌ Неверный или исчерпанный промокод")
            await state.clear()
            await show_main_menu(message)
            return

        days = promo[0]

        # Создаём или получаем пользователя в Remnawave
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days=days, extend_if_exists=True
            )

            if not uuid:
                await message.answer("❌ Ошибка при применении промокода")
                await state.clear()
                await show_main_menu(message)
                return

            # Добавляем в сквад
            await remnawave_add_to_squad(session, uuid)
            
            # Получаем ссылку подписки
            sub_url = await remnawave_get_subscription_url(session, uuid)

            if not sub_url:
                await message.answer("❌ Ошибка при получении ссылки подписки")
                await state.clear()
                await show_main_menu(message)
                return

        # Обновляем промокод (увеличиваем счётчик использования)
        db.increment_promo_usage(code)

        # Обновляем подписку пользователя в БД
        new_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

        # Отправляем успешное сообщение
        await message.answer(
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"Добавлено {days} дней подписки\n\n"
            f"<b>Ссылка подписки:</b>\n<code>{sub_url}</code>"
        )
        
        logging.info(f"Promo code {code} applied by user {tg_id}")

    except Exception as e:
        logging.error(f"Promo error: {e}")
        await message.answer("❌ Ошибка при применении промокода")
    
    finally:
        db.release_user_lock(tg_id)

    await state.clear()
    await show_main_menu(message)
