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
from services.image_handler import send_text_with_photo


logger = logging.getLogger(__name__)

router = Router()


def _build_remnawave_username(tg_id: int, subscription_id: int) -> str:
    return f"tg_{tg_id}_{subscription_id}"


def _subscription_name(subscription) -> str:
    return f"Подписка #{subscription['slot_number']}"


async def _prompt_promo_input(message_or_callback, state: FSMContext, text: str = "Введи промокод:"):
    if hasattr(message_or_callback, "message"):
        await message_or_callback.message.answer(text)
    else:
        await message_or_callback.answer(text)
    await state.set_state(UserStates.waiting_for_promo)


@router.callback_query(F.data == "enter_promo")
async def process_enter_promo(callback: CallbackQuery, state: FSMContext):
    """Предложить ввести промокод"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} initiated promo code entry")

    subscriptions = await db.get_user_subscriptions(tg_id)
    next_slot = await db.get_next_subscription_slot(tg_id)

    if not subscriptions:
        await state.update_data(promo_target_mode="new", promo_target_slot=1)
        await _prompt_promo_input(callback, state, "Введи промокод для новой подписки #1:")
        return

    if len(subscriptions) == 1 and next_slot is None:
        await state.update_data(
            promo_target_mode="existing",
            promo_target_subscription_id=subscriptions[0]["id"],
        )
        await _prompt_promo_input(callback, state, f"Введи промокод для {_subscription_name(subscriptions[0]).lower()}:")
        return

    if len(subscriptions) == 1 and next_slot is not None:
        await state.update_data(
            promo_target_mode="existing",
            promo_target_subscription_id=subscriptions[0]["id"],
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Применить к {_subscription_name(subscriptions[0])}", callback_data=f"promo_target_existing_{subscriptions[0]['id']}")],
            [InlineKeyboardButton(text=f"Активировать новую подписку #{next_slot}", callback_data=f"promo_target_new_{next_slot}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")],
        ])
        await callback.message.answer("Куда применить промокод?", reply_markup=kb)
        await state.set_state(UserStates.waiting_for_promo_target)
        return

    keyboard = [
        [InlineKeyboardButton(text=f"Применить к {_subscription_name(subscription)}", callback_data=f"promo_target_existing_{subscription['id']}")]
        for subscription in subscriptions
    ]
    if next_slot is not None:
        keyboard.append([InlineKeyboardButton(text=f"Активировать новую подписку #{next_slot}", callback_data=f"promo_target_new_{next_slot}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")])

    await callback.message.answer(
        "Выбери, куда применить промокод:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await state.set_state(UserStates.waiting_for_promo_target)


@router.callback_query(F.data.startswith("promo_target_existing_"))
async def process_promo_target_existing(callback: CallbackQuery, state: FSMContext):
    subscription_id = int(callback.data.split("_")[-1])
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not subscription:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    await state.update_data(
        promo_target_mode="existing",
        promo_target_subscription_id=subscription_id,
        promo_target_slot=subscription["slot_number"],
    )
    await _prompt_promo_input(callback, state, f"Введи промокод для {_subscription_name(subscription).lower()}:")


@router.callback_query(F.data.startswith("promo_target_new_"))
async def process_promo_target_new(callback: CallbackQuery, state: FSMContext):
    slot_number = int(callback.data.split("_")[-1])
    await state.update_data(
        promo_target_mode="new",
        promo_target_subscription_id=None,
        promo_target_slot=slot_number,
    )
    await _prompt_promo_input(callback, state, f"Введи промокод для новой подписки #{slot_number}:")


@router.message(UserStates.waiting_for_promo)
async def process_promo_input(message: Message, state: FSMContext):
    """Обработать введённый промокод"""
    code = message.text.strip().upper()
    tg_id = message.from_user.id
    state_data = await state.get_data()
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
        success, error_msg = await db.increment_promo_usage_atomic(code, tg_id)

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

        promo_target_mode = state_data.get("promo_target_mode", "existing")
        target_subscription_id = state_data.get("promo_target_subscription_id")
        target_slot = state_data.get("promo_target_slot")

        if promo_target_mode == "existing":
            subscription = await db.get_subscription_by_id(target_subscription_id, tg_id)
            if not subscription:
                await message.answer("❌ Выбранная подписка не найдена")
                await state.clear()
                await show_main_menu(message)
                return
        else:
            if target_slot is None:
                target_slot = await db.get_next_subscription_slot(tg_id)

            if target_slot is None:
                await message.answer("❌ У тебя уже максимум подписок")
                await state.clear()
                await show_main_menu(message)
                return

            subscription = await db.get_subscription_by_slot(tg_id, target_slot)
            if subscription is None:
                subscription = await db.create_subscription_record(tg_id, target_slot)

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            remna_username = subscription.get("remnawave_username") or _build_remnawave_username(tg_id, subscription["id"])
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days=days,
                extend_if_exists=promo_target_mode == "existing" and bool(subscription.get("remnawave_uuid")),
                remna_username=remna_username,
            )

            if not uuid:
                await message.answer("❌ Ошибка при применении промокода")
                await state.clear()
                await show_main_menu(message)
                return

            await remnawave_add_to_squad(session, uuid, subscription.get("squad_uuid") or DEFAULT_SQUAD_UUID)

            # Получаем ссылку подписки
            sub_url = await remnawave_get_subscription_url(session, uuid)

            if not sub_url:
                await message.answer("❌ Ошибка при получении ссылки подписки")
                await state.clear()
                await show_main_menu(message)
                return

        existing_subscription = subscription.get('subscription_until')
        now = datetime.utcnow()

        if existing_subscription and existing_subscription > now:
            # Активная подписка есть - добавляем дни к ней
            new_until = existing_subscription + timedelta(days=days)
            logger.info(f"User {tg_id} has active subscription, extending from {existing_subscription} by {days} days to {new_until}")
        else:
            # Подписки нет или она истекла - создаём новую
            new_until = now + timedelta(days=days)
            logger.info(f"User {tg_id} has no active subscription, creating new one with {days} days until {new_until}")

        await db.update_subscription_record(
            subscription['id'],
            uuid,
            username,
            new_until,
            subscription.get('squad_uuid') or DEFAULT_SQUAD_UUID,
        )

        # Отправляем успешное сообщение
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
        ])

        text = (
            f"✅ <b>Промокод активирован!</b>\n\n"
            f"Подписка: <b>{_subscription_name(subscription)}</b>\n"
            f"Добавлено {days} дней подписки\n\n"
            f"<b>Ваш ключ:</b>\n"f"{sub_url}"
        )

        await send_text_with_photo(message, text, kb, "Add_a_subscription")

        logging.info(f"Promo code {code} applied by user {tg_id}")

    except Exception as e:
        logging.error(f"Promo error: {e}")
        await message.answer("❌ Ошибка при применении промокода")

    finally:
        await db.release_user_lock(tg_id)

    await state.clear()
    await show_main_menu(message)
