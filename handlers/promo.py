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

        # Создаём или получаем пользователя в Remnawave (обычная подписка)
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
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

        # Обновляем подписку пользователя в БД
        new_until = datetime.utcnow() + timedelta(days=days)
        await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

        # Выдаём VIP подписку (промокод даёт обе подписки)
        vip_info = await db.get_vip_subscription_info(tg_id)
        vip_sub_url = None

        if vip_info and vip_info['xui_uuid']:
            # Продляем существующего клиента
            await xui.extend_vip_client(
                tg_id,
                vip_info['xui_email'],
                vip_info['xui_uuid'],
                vip_info['xui_subscription_id'],
                days
            )
        else:
            # Создаём нового VIP клиента
            result = await xui.create_or_extend_vip_client(tg_id, days, is_new=True)
            if result:
                email, client_uuid, subscription_id, vip_sub_url = result
                vip_until = datetime.utcnow() + timedelta(days=days)
                await db.update_vip_subscription(tg_id, email, client_uuid, subscription_id, vip_until)

        # Отправляем успешное сообщение
        text = (
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"Добавлено {days} дней обеим подпискам:\n"
            f"• 📱 Обычная подписка\n"
            f"• 🛡️ Обход глушилок (VIP)\n\n"
            f"<b>Ссылка обычной подписки:</b>\n<code>{sub_url}</code>\n\n"
            "VIP ссылка доступна в разделе «Моя подписка»"
        )
        await message.answer(text)

        logging.info(f"Promo code {code} applied by user {tg_id}")

    except Exception as e:
        logging.error(f"Promo error: {e}")
        await message.answer("❌ Ошибка при применении промокода")

    finally:
        await db.release_user_lock(tg_id)

    await state.clear()
    await show_main_menu(message)
