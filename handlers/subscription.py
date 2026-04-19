import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from config import DEFAULT_SQUAD_UUID, TARIFFS
from services.cryptobot import create_cryptobot_invoice, get_invoice_status, process_paid_invoice
from services.image_handler import edit_text_with_photo
from services.remnawave import (
    remnawave_add_to_squad,
    remnawave_get_or_create_user,
    remnawave_get_subscription_url,
    remnawave_get_user_info,
)
from services.yookassa import create_yookassa_payment, get_payment_status, process_paid_yookassa_payment
from states import UserStates


logger = logging.getLogger(__name__)

router = Router()


def _subscription_name(subscription) -> str:
    return f"Подписка #{subscription['slot_number']}"


def _subscription_short_status(subscription) -> str:
    until = subscription.get('subscription_until')
    if not until:
        return "без срока"
    if until > datetime.utcnow():
        return "активна"
    return "истекла"


async def _get_subscription_access_data(subscription) -> tuple[str | None, str]:
    """Получить ссылку подписки и остаток времени."""
    remaining_str = "неизвестно"
    sub_url = None

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            if subscription.get('remnawave_uuid'):
                sub_url = await remnawave_get_subscription_url(session, subscription['remnawave_uuid'])
                user_info = await remnawave_get_user_info(session, subscription['remnawave_uuid'])

                if user_info and "expireAt" in user_info:
                    remaining_str = _format_remaining(user_info["expireAt"])
                elif subscription.get('subscription_until'):
                    remaining_str = _format_remaining(subscription['subscription_until'].replace(tzinfo=timezone.utc).isoformat())
            elif subscription.get('subscription_until'):
                remaining_str = _format_remaining(subscription['subscription_until'].replace(tzinfo=timezone.utc).isoformat())
    except Exception as e:
        logging.error(f"Error fetching subscription info from Remnawave: {e}")
        remaining_str = "ошибка загрузки"

    return sub_url, remaining_str


def _build_instruction_text(sub_url: str) -> str:
    return (
        "📲 <b>Инструкция по подключению</b>\n\n"
        "1. Скачай <b>Happ Plus</b> из Google Play или App Store\n"
        f"2. Скопируй ключ:\n<code>{sub_url}</code>\n"
        "3. Открой Happ Plus\n"
        "4. Нажми <b>+</b> в правом верхнем углу\n"
        "5. Выбери <b>Вставить из буфера обмена</b>\n\n"
        "По всем вопросам: @wayspn_support"
    )


def _build_remnawave_username(tg_id: int, subscription_id: int) -> str:
    return f"tg_{tg_id}_{subscription_id}"


def _format_remaining(expire_at_str: str | None) -> str:
    if not expire_at_str:
        return "неизвестно"

    expire_at = datetime.fromisoformat(expire_at_str.replace('Z', '+00:00'))
    remaining = expire_at - datetime.now(timezone.utc)

    if remaining.total_seconds() <= 0:
        return "истекла"

    days = remaining.days
    hours = remaining.seconds // 3600
    minutes = (remaining.seconds % 3600) // 60
    return f"{days}д {hours}ч {minutes}м"


async def _show_tariff_selection(callback: CallbackQuery, state: FSMContext, title: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц — 200₽", callback_data="tariff_1m")],
        [InlineKeyboardButton(text="3 месяца — 500₽", callback_data="tariff_3m")],
        [InlineKeyboardButton(text="6 месяцев — 900₽", callback_data="tariff_6m")],
        [InlineKeyboardButton(text="12 месяцев — 1550₽", callback_data="tariff_12m")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
    ])

    await edit_text_with_photo(callback, title, kb, "Выбери срок подписки")
    await state.set_state(UserStates.choosing_tariff)


async def _show_subscriptions_hub(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscriptions = await db.get_user_subscriptions(tg_id)

    keyboard = []
    for subscription in subscriptions:
        keyboard.append([
            InlineKeyboardButton(
                text=f"{_subscription_name(subscription)} • {_subscription_short_status(subscription)}",
                callback_data=f"subscription_view_{subscription['id']}",
            )
        ])

    if not subscriptions:
        keyboard.append([
            InlineKeyboardButton(text="Купить первую подписку", callback_data="buy_new_subscription", style="success")
        ])
        text = (
            "🔐 <b>Мои подписки</b>\n\n"
            "У тебя пока нет подписок.\n"
            "Купи первую подписку и при необходимости потом добавь ещё для близких."
        )
    else:
        if len(subscriptions) < db.MAX_SUBSCRIPTIONS_PER_USER:
            keyboard.append([
                InlineKeyboardButton(text="Купить ещё подписку", callback_data="buy_new_subscription", style="success")
            ])

        text = (
            "🔐 <b>Мои подписки</b>\n\n"
            f"У тебя оформлено подписок: <b>{len(subscriptions)}</b> из <b>{db.MAX_SUBSCRIPTIONS_PER_USER}</b>.\n"
            "Открой нужную подписку или оформи новую."
        )

    keyboard.append([InlineKeyboardButton(text="Закрыть", callback_data="back_to_menu", style="danger")])
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await state.clear()
    await edit_text_with_photo(callback, text, kb, "Мои подписки")


async def _show_subscription_card(callback: CallbackQuery, subscription_id: int):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not subscription:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    sub_url, remaining_str = await _get_subscription_access_data(subscription)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Инструкция", callback_data=f"subscription_instruction_{subscription_id}")],
        [InlineKeyboardButton(text="🔄 Продлить эту подписку", callback_data=f"renew_subscription_{subscription_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
    ])

    text = (
        f"🔐 <b>{_subscription_name(subscription)}</b>\n\n"
        "<blockquote>"
        f"📍 Статус: <b>{_subscription_short_status(subscription)}</b>\n"
        f"📆 Осталось времени: <b>{remaining_str}</b>\n"
        f"🌐 Группа подключения: <b>SPN-Squad</b>"
        "</blockquote>\n\n"
        "<b>Ваш ключ:</b>\n"
        f"{sub_url or '<i>Ошибка получения ссылки</i>'}"
    )

    await edit_text_with_photo(callback, text, kb, "Моя подписка")


async def _show_subscription_instruction(callback: CallbackQuery, subscription_id: int):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not subscription:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    sub_url, _ = await _get_subscription_access_data(subscription)
    if not sub_url:
        await callback.answer("Не удалось получить ключ подписки", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Открыть эту подписку", callback_data=f"subscription_view_{subscription_id}")],
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu", style="danger")],
    ])

    await edit_text_with_photo(
        callback,
        _build_instruction_text(sub_url),
        kb,
        "Как подключиться",
    )


async def _get_or_create_target_subscription_for_direct_flow(tg_id: int, state_data: dict):
    purchase_mode = state_data.get("purchase_mode", "new")
    target_subscription_id = state_data.get("target_subscription_id")
    target_slot_number = state_data.get("target_slot_number")

    if purchase_mode == "renew":
        subscription = await db.get_subscription_by_id(target_subscription_id, tg_id)
        return subscription, purchase_mode

    if target_slot_number is None:
        target_slot_number = await db.get_next_subscription_slot(tg_id)
        state_data["target_slot_number"] = target_slot_number

    if target_slot_number is None:
        return None, purchase_mode

    subscription = await db.get_subscription_by_slot(tg_id, target_slot_number)
    if subscription is None:
        subscription = await db.create_subscription_record(tg_id, target_slot_number)

    return subscription, purchase_mode


@router.callback_query(F.data.in_({"buy_subscription", "my_subscription"}))
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext):
    """Показать список подписок пользователя."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened subscriptions hub")
    await _show_subscriptions_hub(callback, state)


@router.callback_query(F.data == "buy_new_subscription")
async def process_buy_new_subscription(callback: CallbackQuery, state: FSMContext):
    """Начать покупку новой подписки."""
    tg_id = callback.from_user.id
    slot_number = await db.get_next_subscription_slot(tg_id)

    if slot_number is None:
        await callback.answer("У тебя уже максимум 3 подписки", show_alert=True)
        return

    await state.update_data(
        purchase_mode="new",
        target_subscription_id=None,
        target_slot_number=slot_number,
    )
    await _show_tariff_selection(callback, state, f"Выбери срок для новой подписки #{slot_number}:")


@router.callback_query(F.data.startswith("subscription_view_"))
async def process_subscription_view(callback: CallbackQuery, state: FSMContext):
    subscription_id = int(callback.data.split("_")[-1])
    await _show_subscription_card(callback, subscription_id)


@router.callback_query(F.data.startswith("subscription_instruction_"))
async def process_subscription_instruction(callback: CallbackQuery, state: FSMContext):
    subscription_id = int(callback.data.split("_")[-1])
    await _show_subscription_instruction(callback, subscription_id)


@router.callback_query(F.data.startswith("renew_subscription_"))
async def process_subscription_renew(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscription_id = int(callback.data.split("_")[-1])
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not subscription:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    await state.update_data(
        purchase_mode="renew",
        target_subscription_id=subscription_id,
        target_slot_number=subscription['slot_number'],
    )
    await _show_tariff_selection(callback, state, f"Выбери срок для продления подписки #{subscription['slot_number']}:")


@router.callback_query(F.data.startswith("tariff_"))
async def process_tariff_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор тарифа."""
    tg_id = callback.from_user.id
    tariff_code = callback.data.split("_")[1]
    logging.info(f"User {tg_id} selected tariff: {tariff_code}")

    data = await state.get_data()
    purchase_mode = data.get("purchase_mode", "new")
    target_slot_number = data.get("target_slot_number")
    target_subscription_id = data.get("target_subscription_id")

    await state.update_data(tariff_code=tariff_code)

    tariff = TARIFFS[tariff_code]
    stats = await db.get_referral_stats(tg_id)
    referral_balance = stats['current_balance']

    if purchase_mode == "renew" and target_slot_number:
        purchase_text = f"Продление подписки #{target_slot_number}"
    elif target_slot_number:
        purchase_text = f"Новая подписка #{target_slot_number}"
    else:
        purchase_text = "Новая подписка"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_cryptobot")],
        [InlineKeyboardButton(text="💳 Оплатить картой", callback_data="pay_yookassa")],
        [InlineKeyboardButton(text="💰 Оплатить с баланса от рефералов", callback_data="pay_referral_balance")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=(f"subscription_view_{target_subscription_id}" if purchase_mode == "renew" and target_subscription_id else "buy_subscription"), style="danger")],
    ])

    text = (
        f"<b>{purchase_text}</b>\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {tariff['price']} ₽\n"
        f"Баланс от рефералов: {referral_balance:.2f} ₽\n\n"
        "Выбери способ оплаты:"
    )

    await edit_text_with_photo(callback, text, kb, "Выбери способ оплаты")
    await state.set_state(UserStates.choosing_payment)


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий счёт в CryptoBot."""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    purchase_mode = data.get("purchase_mode", "new")
    target_subscription_id = data.get("target_subscription_id")
    target_slot_number = data.get("target_slot_number")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    if purchase_mode == "new" and target_slot_number is None:
        target_slot_number = await db.get_next_subscription_slot(tg_id)
        if target_slot_number is None:
            await callback.answer("У тебя уже максимум 3 подписки", show_alert=True)
            return
        await state.update_data(target_slot_number=target_slot_number)

    tariff = TARIFFS[tariff_code]
    amount = tariff["price"]

    existing_invoice_id = await db.get_active_payment_for_user_and_tariff(
        tg_id,
        tariff_code,
        "cryptobot",
        subscription_id=target_subscription_id,
        payment_target=purchase_mode,
        target_slot_number=target_slot_number,
    )

    if existing_invoice_id:
        invoice = await get_invoice_status(existing_invoice_id)
        if invoice and invoice.get("status") == "active":
            pay_url = invoice.get("bot_invoice_url", "")
            if pay_url:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
                ])
                await edit_text_with_photo(callback, "<b>Счёт на оплату (существующий)</b>", kb, "Оплати")
                await state.clear()
                return

    invoice = await create_cryptobot_invoice(callback.bot, amount, tariff_code, tg_id)
    if not invoice:
        await callback.answer("Ошибка создания счёта в CryptoBot. Попробуй позже.", show_alert=True)
        await state.clear()
        return

    invoice_id = invoice["invoice_id"]
    pay_url = invoice["bot_invoice_url"]

    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        "cryptobot",
        invoice_id,
        subscription_id=target_subscription_id,
        payment_target=purchase_mode,
        target_slot_number=target_slot_number,
    )

    target_text = f"подписки #{target_slot_number}" if purchase_mode == "new" else f"подписки #{data.get('target_slot_number')}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
    ])
    text = (
        f"<b>Счёт на оплату {target_text}</b>\n\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати через CryptoBot. После оплаты бот автоматически активирует подписку.\n"
        "Если не активировалось, нажми «Проверить оплату»."
    )
    await edit_text_with_photo(callback, text, kb, "Оплати")
    await state.clear()


@router.callback_query(F.data == "pay_yookassa")
async def process_pay_yookassa(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий платёж через Yookassa."""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    purchase_mode = data.get("purchase_mode", "new")
    target_subscription_id = data.get("target_subscription_id")
    target_slot_number = data.get("target_slot_number")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    if purchase_mode == "new" and target_slot_number is None:
        target_slot_number = await db.get_next_subscription_slot(tg_id)
        if target_slot_number is None:
            await callback.answer("У тебя уже максимум 3 подписки", show_alert=True)
            return
        await state.update_data(target_slot_number=target_slot_number)

    tariff = TARIFFS[tariff_code]
    amount = tariff["price"]

    existing_payment_id = await db.get_active_payment_for_user_and_tariff(
        tg_id,
        tariff_code,
        "yookassa",
        subscription_id=target_subscription_id,
        payment_target=purchase_mode,
        target_slot_number=target_slot_number,
    )

    if existing_payment_id:
        payment = await get_payment_status(existing_payment_id)
        if payment and payment.get("status") == "pending":
            confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")
            if confirmation_url:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
                ])
                await edit_text_with_photo(callback, "<b>💳 Yookassa (существующий платёж)</b>", kb, "Оплати")
                await state.clear()
                return

    payment = await create_yookassa_payment(callback.bot, amount, tariff_code, tg_id)
    if not payment:
        await callback.answer("Ошибка создания платежа в Yookassa. Попробуй позже.", show_alert=True)
        await state.clear()
        return

    payment_id = payment["id"]
    confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")
    if not confirmation_url:
        await callback.answer("Ошибка: не получена ссылка для оплаты", show_alert=True)
        await state.clear()
        return

    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        "yookassa",
        payment_id,
        subscription_id=target_subscription_id,
        payment_target=purchase_mode,
        target_slot_number=target_slot_number,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
    ])
    await edit_text_with_photo(callback, "<b>💳 Yookassa</b>", kb, "Оплати")
    await state.clear()


@router.callback_query(F.data == "pay_referral_balance")
async def process_pay_referral_balance(callback: CallbackQuery, state: FSMContext):
    """Оплатить подписку с баланса рефералов."""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    amount = tariff["price"]

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        stats = await db.get_referral_stats(tg_id)
        referral_balance = stats["current_balance"]

        if referral_balance < amount:
            missing = amount - referral_balance
            await callback.answer(f"Не хватает {missing:.2f} ₽ на балансе рефералов", show_alert=True)
            return

        subscription, purchase_mode = await _get_or_create_target_subscription_for_direct_flow(tg_id, data)
        if not subscription:
            await callback.answer("Не удалось определить целевую подписку", show_alert=True)
            return

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            remna_username = subscription.get("remnawave_username") or _build_remnawave_username(tg_id, subscription['id'])
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                tariff["days"],
                extend_if_exists=purchase_mode == "renew" and bool(subscription.get("remnawave_uuid")),
                remna_username=remna_username,
            )

            if not uuid:
                await callback.answer("Ошибка получения доступа в VPN. Попробуй позже.", show_alert=True)
                return

            squad_added = await remnawave_add_to_squad(session, uuid, subscription.get("squad_uuid") or DEFAULT_SQUAD_UUID)
            if not squad_added:
                logging.warning(f"Failed to add user {uuid} to squad")

            sub_url = await remnawave_get_subscription_url(session, uuid)
            if not sub_url:
                logging.warning(f"Failed to get subscription URL for {uuid}")

        existing_subscription = subscription.get("subscription_until")
        now = datetime.utcnow()
        if existing_subscription and existing_subscription > now:
            new_until = existing_subscription + timedelta(days=tariff["days"])
        else:
            new_until = now + timedelta(days=tariff["days"])

        await db.update_subscription_record(
            subscription['id'],
            uuid,
            username,
            new_until,
            subscription.get("squad_uuid") or DEFAULT_SQUAD_UUID,
        )
        await db.spend_referral_balance_for_subscription(tg_id, amount, tariff_code)

        remaining_balance = referral_balance - amount
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")],
        ])

        text = (
            f"✅ <b>{_subscription_name(subscription)} оплачена с баланса рефералов!</b>\n\n"
            f"Тариф: {tariff_code} ({tariff['days']} дней)\n"
            f"Списано: {amount} ₽\n"
            f"Остаток баланса: {remaining_balance:.2f} ₽\n\n"
            f"<b>Ваш ключ:</b>\n{sub_url or 'Ошибка получения ссылки'}"
        )

        await edit_text_with_photo(callback, text, kb, "Оплати")
        await state.clear()

    except Exception as e:
        logging.error(f"Referral balance payment error: {e}", exc_info=True)
        await callback.answer("Ошибка при оплате с баланса рефералов", show_alert=True)
    finally:
        await db.release_user_lock(tg_id)


@router.callback_query(F.data == "check_payment")
async def process_check_payment(callback: CallbackQuery):
    """Проверить статус последнего ожидающего платежа."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking payment status")

    can_check, error_msg = await db.can_check_payment(tg_id)
    if not can_check:
        await callback.answer(error_msg, show_alert=True)
        return

    await db.update_last_payment_check(tg_id)

    result = await db.get_last_pending_payment(tg_id)
    if not result:
        await callback.answer("Нет ожидающих оплаты счетов", show_alert=True)
        return

    invoice_id = result['invoice_id']
    tariff_code = result['tariff_code']
    provider = result['provider']

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        if provider == "yookassa":
            payment = await get_payment_status(invoice_id)
            if payment and payment.get("status") == "succeeded":
                success = await process_paid_yookassa_payment(callback.bot, tg_id, invoice_id, tariff_code)
                await callback.answer("✅ Оплата подтверждена!" if success else "Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)
        elif provider == "cryptobot":
            invoice = await get_invoice_status(invoice_id)
            if invoice and invoice.get("status") == "paid":
                success = await process_paid_invoice(callback.bot, tg_id, invoice_id, tariff_code)
                await callback.answer("✅ Оплата подтверждена!" if success else "Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)
    except Exception as e:
        logging.error(f"Check payment error: {e}", exc_info=True)
        await callback.answer("Ошибка при проверке платежа", show_alert=True)
    finally:
        await db.release_user_lock(tg_id)
