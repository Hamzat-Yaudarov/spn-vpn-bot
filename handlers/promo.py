import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import BYPASS_HWID_DEVICE_LIMIT, BYPASS_SQUAD_UUID, REGULAR_HWID_DEVICE_LIMIT, REGULAR_SQUAD_UUID
from states import UserStates
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_get_subscription_url,
    remnawave_set_subscription_expiry,
)
from handlers.start import show_main_menu
from services.image_handler import send_text_with_photo


logger = logging.getLogger(__name__)

router = Router()


def _build_v2_remnawave_username(tg_id: int, plan_kind: str, type_index: int) -> str:
    return f"tg_{tg_id}_{plan_kind}_{type_index}"


def _subscription_name(subscription) -> str:
    plan_kind = subscription.get("plan_kind") or "regular"
    title = "Обычная" if plan_kind == "regular" else "С антиглушилкой"
    return f"{title} #{subscription.get('type_index') or subscription['slot_number']}"


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

    subscriptions = await db.get_renewable_subscriptions(tg_id)
    next_type_index = await db.get_next_type_index(tg_id, "regular")

    if not subscriptions:
        await state.update_data(promo_target_mode="new", promo_target_type_index=next_type_index or 1)
        await _prompt_promo_input(callback, state, "Введи промокод для новой обычной подписки #1:")
        return

    if len(subscriptions) == 1 and next_type_index is None:
        await state.update_data(
            promo_target_mode="existing",
            promo_target_subscription_id=subscriptions[0]["id"],
        )
        await _prompt_promo_input(callback, state, f"Введи промокод для {_subscription_name(subscriptions[0]).lower()}:")
        return

    if len(subscriptions) == 1 and next_type_index is not None:
        await state.update_data(
            promo_target_mode="existing",
            promo_target_subscription_id=subscriptions[0]["id"],
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Применить к {_subscription_name(subscriptions[0])}", callback_data=f"promo_target_existing_{subscriptions[0]['id']}", style="success")],
            [InlineKeyboardButton(text=f"Активировать новую обычную #{next_type_index}", callback_data=f"promo_target_new_{next_type_index}", style="success")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")],
        ])
        await callback.message.answer("Куда применить промокод?", reply_markup=kb)
        await state.set_state(UserStates.waiting_for_promo_target)
        return

    keyboard = [
        [InlineKeyboardButton(text=f"Применить к {_subscription_name(subscription)}", callback_data=f"promo_target_existing_{subscription['id']}", style="success")]
        for subscription in subscriptions
    ]
    if next_type_index is not None:
        keyboard.append([InlineKeyboardButton(text=f"Активировать новую обычную #{next_type_index}", callback_data=f"promo_target_new_{next_type_index}", style="success")])
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

    if not subscription or subscription.get("generation") != "v2" or not subscription.get("is_visible") or not subscription.get("is_renewable"):
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    await state.update_data(
        promo_target_mode="existing",
        promo_target_subscription_id=subscription_id,
        promo_target_type_index=subscription.get("type_index"),
    )
    await _prompt_promo_input(callback, state, f"Введи промокод для {_subscription_name(subscription).lower()}:")


@router.callback_query(F.data.startswith("promo_target_new_"))
async def process_promo_target_new(callback: CallbackQuery, state: FSMContext):
    type_index = int(callback.data.split("_")[-1])
    await state.update_data(
        promo_target_mode="new",
        promo_target_subscription_id=None,
        promo_target_type_index=type_index,
    )
    await _prompt_promo_input(callback, state, f"Введи промокод для новой обычной подписки #{type_index}:")


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
        target_type_index = state_data.get("promo_target_type_index")

        if promo_target_mode == "existing":
            subscription = await db.get_subscription_by_id(target_subscription_id, tg_id)
            if not subscription or subscription.get("generation") != "v2" or not subscription.get("is_visible") or not subscription.get("is_renewable"):
                await message.answer("❌ Выбранная подписка не найдена")
                await state.clear()
                await show_main_menu(message)
                return
        else:
            if target_type_index is None:
                target_type_index = await db.get_next_type_index(tg_id, "regular")

            target_slot = await db.get_next_subscription_slot(tg_id)
            if target_type_index is None or target_slot is None:
                await message.answer("❌ У тебя уже максимум обычных подписок")
                await state.clear()
                await show_main_menu(message)
                return

            subscription = await db.create_subscription_record(
                tg_id,
                target_slot,
                plan_kind="regular",
                type_index=target_type_index,
                generation="v2",
                is_visible=True,
                is_renewable=True,
                purchase_days=days,
            )

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            plan_kind = subscription.get("plan_kind") or "regular"
            squad_uuid = REGULAR_SQUAD_UUID if plan_kind == "regular" else BYPASS_SQUAD_UUID
            device_limit = REGULAR_HWID_DEVICE_LIMIT if plan_kind == "regular" else BYPASS_HWID_DEVICE_LIMIT
            remna_username = subscription.get("remnawave_username") or _build_v2_remnawave_username(
                tg_id,
                plan_kind,
                subscription.get("type_index") or subscription["id"],
            )
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days=days,
                extend_if_exists=promo_target_mode == "existing" and bool(subscription.get("remnawave_uuid")),
                remna_username=remna_username,
                active_internal_squads=[squad_uuid],
                hwid_device_limit=device_limit,
                telegram_id=tg_id,
            )

            if not uuid:
                await message.answer("❌ Ошибка при применении промокода")
                await state.clear()
                await show_main_menu(message)
                return

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

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            if not await remnawave_set_subscription_expiry(session, uuid, new_until):
                logger.warning("Failed to sync Remnawave expiry for promo subscription %s", subscription['id'])

        await db.update_subscription_record(
            subscription['id'],
            uuid,
            username,
            new_until,
            squad_uuid,
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
