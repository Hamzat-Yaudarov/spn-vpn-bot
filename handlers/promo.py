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
from services import xui
from handlers.start import show_main_menu


router = Router()


@router.callback_query(F.data == "enter_promo")
async def process_enter_promo(callback: CallbackQuery, state: FSMContext):
    """Предложить ввести промокод"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} initiated promo code entry")

    await callback.message.edit_text("Введи промокод:")
    await state.set_state(UserStates.waiting_for_promo)


@router.message(UserStates.waiting_for_promo)
async def process_promo_input(message: Message, state: FSMContext):
    """Обработать введённый промокод"""
    code = message.text.strip().upper()
    tg_id = message.from_user.id
    logging.info(f"User {tg_id} entered promo code: {code}")

    # Проверка anti-spam: не более одной попытки в 1.5 секунды
    can_request, error_msg = await db.can_request_promo(tg_id)
    if not can_request:
        await message.answer(error_msg)
        await state.clear()
        await show_main_menu(message)
        return

    # Обновляем время последней попытки
    await db.update_last_promo_attempt(tg_id)

    if not await db.acquire_user_lock(tg_id):
        await message.answer("Подожди пару секунд ⏳")
        await state.clear()
        await show_main_menu(message)
        return

    try:
        # Атомарно проверяем и увеличиваем счётчик использования промокода
        success, error_msg = await db.increment_promo_usage_atomic(code)

        if not success:
            await message.answer(f"❌ {error_msg}")
            await state.clear()
            await show_main_menu(message)
            return

        # Получаем информацию о промокоде (дни)
        promo = await db.get_promo_code(code)
        if not promo:
            await message.answer("❌ Ошибка при получении информации о промокоде")
            await state.clear()
            await show_main_menu(message)
            return

        days = promo[0]

        # Промокоды всегда дают обе подписки
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Создаём/продлеваем обычную подписку через Remnawave
            uuid_regular, username_regular = await remnawave_get_or_create_user(
                session, tg_id, days=days, extend_if_exists=True, sub_type="regular"
            )

            # Создаём/продлеваем VIP подписку через 3X-UI
            email_vip, _ = await xui.xui_get_or_create_client(tg_id, days=days, extend_if_exists=True)

            if not uuid_regular or not email_vip:
                await message.answer("❌ Ошибка при применении промокода")
                await state.clear()
                await show_main_menu(message)
                return

            # Добавляем обычную подписку в сквад
            await remnawave_add_to_squad(session, uuid_regular)

            # Получаем ссылки подписок
            sub_url_regular = await remnawave_get_subscription_url(session, uuid_regular)
            sub_url_vip = await xui.xui_get_subscription_url(tg_id, email_vip)

            if not sub_url_regular or not sub_url_vip:
                await message.answer("❌ Ошибка при получении ссылок подписок")
                await state.clear()
                await show_main_menu(message)
                return

        # Обновляем обе подписки пользователя в БД
        new_until = datetime.utcnow() + timedelta(days=days)
        await db.update_both_subscriptions(
            tg_id,
            uuid_regular, username_regular, new_until, DEFAULT_SQUAD_UUID,
            email_vip, email_vip, new_until, DEFAULT_SQUAD_UUID
        )

        # Отправляем успешное сообщение
        await message.answer(
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"Добавлено {days} дней к обеим подпискам\n\n"
            f"<b>🌐 Обычная подписка:</b>\n<code>{sub_url_regular}</code>\n\n"
            f"<b>🔒 Обход глушилок (3X-UI):</b>\n<code>{sub_url_vip}</code>"
        )

        logging.info(f"Promo code {code} applied by user {tg_id}")

    except Exception as e:
        logging.error(f"Promo error: {e}")
        await message.answer("❌ Ошибка при применении промокода")

    finally:
        await db.release_user_lock(tg_id)

    await state.clear()
    await show_main_menu(message)
