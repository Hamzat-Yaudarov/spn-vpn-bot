import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import TARIFFS, TARIFFS_REGULAR, TARIFFS_ANTI_JAMMING, DEFAULT_SQUAD_UUID
from states import UserStates
import database as db
from services.remnawave import remnawave_get_subscription_url, remnawave_get_user_info
from services.cryptobot import create_cryptobot_invoice, get_invoice_status, process_paid_invoice
from services.yookassa import create_yookassa_payment, get_payment_status, process_paid_yookassa_payment


router = Router()


@router.callback_query(F.data == "buy_subscription")
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext):
    """Показать выбор типа подписки"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: buy_subscription")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔐 Обычная подписка",
            callback_data="subscription_type_regular"
        )],
        [InlineKeyboardButton(
            text="🛡️ Обычная подписка + Обход глушилок",
            callback_data="subscription_type_anti_jamming"
        )],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Выберите тип подписки:</b>\n\n"
        "<b>🔐 Обычная подписка</b>\n"
        "• Быстрый интернет\n"
        "• VPN серверы\n\n"
        "<b>🛡️ Обычная подписка + Обход глушилок</b>\n"
        "• Все возможности обычной подписки\n"
        "• Дополнительный способ подключения\n"
        "• Защита от помех\n"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_subscription_type)


@router.callback_query(UserStates.choosing_subscription_type, F.data.startswith("subscription_type_"))
async def process_subscription_type_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор типа подписки"""
    tg_id = callback.from_user.id
    # Парсим корректно: "subscription_type_regular" или "subscription_type_anti_jamming"
    sub_type = callback.data.replace("subscription_type_", "")
    logging.info(f"User {tg_id} selected subscription type: {sub_type}")

    # Сохраняем тип подписки в state
    await state.update_data(subscription_type=sub_type)

    # Устанавливаем тип подписки в БД
    await db.set_subscription_type(tg_id, sub_type)

    # Показываем тарифы для выбранного типа
    await show_tariffs_for_type(callback, state, sub_type)


async def show_tariffs_for_type(callback: CallbackQuery, state: FSMContext, sub_type: str):
    """Показать тарифы для выбранного типа подписки"""
    from config import TARIFFS_REGULAR, TARIFFS_ANTI_JAMMING

    tariffs = TARIFFS_ANTI_JAMMING if sub_type == "anti_jamming" else TARIFFS_REGULAR

    kb_buttons = []
    for code, tariff in tariffs.items():
        days = tariff['days']
        price = tariff['price']
        kb_buttons.append([InlineKeyboardButton(
            text=f"{code.upper()} — {price}₽",
            callback_data=f"tariff_{code}_{sub_type}"
        )])

    kb_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    type_name = "Обычная подписка" if sub_type == "regular" else "Обычная подписка + Обход глушилок"

    await callback.message.edit_text(f"<b>{type_name}</b>\n\nВыбери срок подписки:", reply_markup=kb)
    await state.set_state(UserStates.choosing_tariff)


@router.callback_query(UserStates.choosing_tariff, F.data.startswith("tariff_"))
async def process_tariff_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор тарифа"""
    from config import TARIFFS_REGULAR, TARIFFS_ANTI_JAMMING

    tg_id = callback.from_user.id
    data_parts = callback.data.split("_")
    tariff_code = data_parts[1]  # 1m, 3m, 6m, 12m
    sub_type = data_parts[2]      # regular или anti_jamming

    logging.info(f"User {tg_id} selected tariff: {tariff_code} for type: {sub_type}")

    # Получаем тарифы для выбранного типа подписки
    tariffs = TARIFFS_ANTI_JAMMING if sub_type == "anti_jamming" else TARIFFS_REGULAR

    if tariff_code not in tariffs:
        await callback.answer("Неверный тариф")
        return

    tariff = tariffs[tariff_code]

    await state.update_data(tariff_code=tariff_code, subscription_type=sub_type)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_cryptobot")],
        [InlineKeyboardButton(text="💳 Yookassa", callback_data="pay_yookassa")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    type_name = "Обычная подписка" if sub_type == "regular" else "Обычная подписка + Обход глушилок"

    text = (
        f"<b>{type_name}</b>\n"
        f"Тариф: <b>{tariff_code.upper()}</b>\n"
        f"Срок: <b>{tariff['days']} дней</b>\n"
        f"Сумма: <b>{tariff['price']} ₽</b>\n\n"
        "Выбери способ оплаты:"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_payment)


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий счёт в CryptoBot"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    sub_type = data.get("subscription_type", "regular")
    logging.info(f"User {tg_id} selected payment method: cryptobot (tariff: {tariff_code}, type: {sub_type})")

    if not tariff_code:
        await callback.message.edit_text("Ошибка: тариф не выбран")
        await state.clear()
        return

    # Выбираем правильный словарь тарифов
    tariffs = TARIFFS_ANTI_JAMMING if sub_type == "anti_jamming" else TARIFFS_REGULAR
    tariff = tariffs[tariff_code]
    amount = tariff["price"]

    # Проверяем, есть ли уже активный счёт для этого пользователя и тарифа
    existing_invoice_id = await db.get_active_payment_for_user_and_tariff(tg_id, tariff_code, "cryptobot")

    if existing_invoice_id:
        # Счёт уже есть - получаем его статус
        invoice = await get_invoice_status(existing_invoice_id)

        if invoice and invoice.get("status") == "active":
            pay_url = invoice.get("bot_invoice_url", "")

            if pay_url:
                # Возвращаем существующий счёт
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
                ])

                text = (
                    f"<b>Счёт на оплату (существующий)</b>\n\n"
                    f"Тариф: {tariff_code}\n"
                    f"Сумма: {amount} ₽\n\n"
                    "Оплати через CryptoBot. После оплаты бот автоматически активирует подписку.\n"
                    "Если не активировалось — нажми «Проверить оплату»"
                )

                await callback.message.edit_text(text, reply_markup=kb)
                await state.clear()
                logging.info(f"Returned existing CryptoBot invoice {existing_invoice_id} for user {tg_id}")
                return

    # Счёта нет или он истёк - создаём новый
    invoice = await create_cryptobot_invoice(callback.bot, amount, tariff_code, tg_id)

    if not invoice:
        await callback.message.edit_text("Ошибка создания счёта в CryptoBot. Попробуй позже.")
        await state.clear()
        return

    invoice_id = invoice["invoice_id"]
    pay_url = invoice["bot_invoice_url"]

    # Записываем платеж в БД
    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        "cryptobot",
        invoice_id
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    text = (
        f"<b>Счёт на оплату</b>\n\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати через CryptoBot. После оплаты бот автоматически активирует подписку.\n"
        "Если не активировалось — нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "pay_yookassa")
async def process_pay_yookassa(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий платёж через Yookassa"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    sub_type = data.get("subscription_type", "regular")
    logging.info(f"User {tg_id} selected payment method: yookassa (tariff: {tariff_code}, type: {sub_type})")

    if not tariff_code:
        await callback.message.edit_text("Ошибка: тариф не выбран")
        await state.clear()
        return

    # Выбираем правильный словарь тарифов
    tariffs = TARIFFS_ANTI_JAMMING if sub_type == "anti_jamming" else TARIFFS_REGULAR
    tariff = tariffs[tariff_code]
    amount = tariff["price"]

    # Проверяем, есть ли уже активный платёж для этого пользователя и тарифа
    existing_payment_id = await db.get_active_payment_for_user_and_tariff(tg_id, tariff_code, "yookassa")

    if existing_payment_id:
        # Платёж уже есть - получаем его статус
        payment = await get_payment_status(existing_payment_id)

        if payment and payment.get("status") == "pending":
            confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")

            if confirmation_url:
                # Возвращаем существующий платёж
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
                ])

                text = (
                    f"<b>💳 Yookassa (существующий платёж)</b>\n\n"
                    f"Тариф: {tariff_code}\n"
                    f"Сумма: {amount} ₽\n\n"
                    "Оплати картой, СБП или другим способом через Yookassa.\n"
                    "После оплаты бот автоматически активирует подписку.\n"
                    "Если не активировалось — нажми «Проверить оплату»"
                )

                await callback.message.edit_text(text, reply_markup=kb)
                await state.clear()
                logging.info(f"Returned existing Yookassa payment {existing_payment_id} for user {tg_id}")
                return

    # Платежа нет или он истёк - создаём новый
    payment = await create_yookassa_payment(callback.bot, amount, tariff_code, tg_id)

    if not payment:
        await callback.message.edit_text("Ошибка создания платежа в Yookassa. Попробуй позже.")
        await state.clear()
        return

    payment_id = payment["id"]
    confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")

    if not confirmation_url:
        await callback.message.edit_text("Ошибка: не получена ссылка для оплаты")
        await state.clear()
        return

    # Записываем платеж в БД
    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        "yookassa",
        payment_id
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    text = (
        f"<b>💳 Yookassa</b>\n\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати картой, СБП или другим способом через Yookassa.\n"
        "После оплаты бот автоматически активирует подписку.\n"
        "Если не активировалось — нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "check_payment")
async def process_check_payment(callback: CallbackQuery):
    """Проверить статус платежа"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking payment status")

    # Проверка anti-spam: не более одной проверки в 1 секунду
    can_check, error_msg = await db.can_check_payment(tg_id)
    if not can_check:
        await callback.answer(error_msg, show_alert=True)
        return

    # Обновляем время последней проверки
    await db.update_last_payment_check(tg_id)

    # Получаем последний ожидающий платеж с информацией о провайдере
    result = await db.db_execute(
        """
        SELECT invoice_id, tariff_code, provider
        FROM payments
        WHERE tg_id = $1 AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (tg_id,),
        fetch_one=True
    )

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
        # Получаем тип подписки пользователя
        sub_type = await db.get_subscription_type(tg_id)

        if provider == "yookassa":
            # Проверяем платёж в Yookassa
            payment = await get_payment_status(invoice_id)

            if payment and payment.get("status") == "succeeded":
                success = await process_paid_yookassa_payment(callback.bot, tg_id, invoice_id, tariff_code, sub_type)

                if success:
                    await callback.message.edit_text(
                        "✅ <b>Оплата подтверждена!</b>\n\n"
                        f"Тариф: {tariff_code}\n"
                        "Подписка активирована"
                    )
                else:
                    await callback.answer("Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)

        elif provider == "cryptobot":
            # Проверяем платёж в CryptoBot
            invoice = await get_invoice_status(invoice_id)

            if invoice and invoice.get("status") == "paid":
                success = await process_paid_invoice(callback.bot, tg_id, invoice_id, tariff_code, sub_type)

                if success:
                    await callback.message.edit_text(
                        "✅ <b>Оплата подтверждена!</b>\n\n"
                        f"Тариф: {tariff_code}\n"
                        "Подписка активирована"
                    )
                else:
                    await callback.answer("Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)

    except Exception as e:
        logging.error(f"Check payment error: {e}")
        await callback.answer("Ошибка при проверке платежа", show_alert=True)

    finally:
        await db.release_user_lock(tg_id)


@router.callback_query(F.data == "my_subscription")
async def process_my_subscription(callback: CallbackQuery):
    """Показать информацию о подписке пользователя"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking subscription status")

    user = await db.get_user(tg_id)

    if not user or not user['remnawave_uuid']:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оформить подписку", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])
        await callback.message.edit_text(
            "У тебя пока нет активной подписки.\nОформи её сейчас!",
            reply_markup=kb
        )
        return

    # Получаем тип подписки пользователя
    sub_type = await db.get_subscription_type(tg_id)

    # Получаем актуальную информацию о подписке из Remnawave
    remaining_str = "неизвестно"
    sub_url = "ошибка получения ссылки"

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Получаем ссылку подписки
            sub_url = await remnawave_get_subscription_url(session, user['remnawave_uuid'])

            # Получаем информацию о пользователе (включая expireAt)
            user_info = await remnawave_get_user_info(session, user['remnawave_uuid'])

            if user_info and "expireAt" in user_info:
                expire_at = user_info["expireAt"]
                exp_date = datetime.fromisoformat(expire_at.replace('Z', '+00:00'))
                remaining = exp_date - datetime.now(timezone.utc)

                if remaining.total_seconds() <= 0:
                    remaining_str = "истекла"
                else:
                    days = remaining.days
                    hours = remaining.seconds // 3600
                    minutes = (remaining.seconds % 3600) // 60
                    remaining_str = f"{days}д {hours}ч {minutes}м"

    except Exception as e:
        logging.error(f"Error fetching subscription info from Remnawave: {e}")
        remaining_str = "ошибка загрузки"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    # Для anti_jamming показываем обе ссылки
    if sub_type == "anti_jamming":
        xui_sub = await db.get_xui_subscription(tg_id)
        xui_link = "ошибка получения ссылки"

        # Получаем 3X-UI ссылку подписки
        if xui_sub and xui_sub['xui_username']:
            try:
                from services.xui import get_xui_client_traffic
                traffic_info = await get_xui_client_traffic(xui_sub['xui_username'])
                if traffic_info:
                    xui_link = traffic_info.get('link', xui_link)
            except Exception as e:
                logging.error(f"Error getting 3X-UI link: {e}")

        text = (
            "🔐 <b>Мой доступ (Обычная подписка + Обход глушилок)</b>\n\n"
            "<blockquote>"
            f"📆 Осталось времени: <b>{remaining_str}</b>\n"
            "🌐 Статус: <b>Активен</b>\n"
            "</blockquote>\n\n"
            "<b>📌 Ссылка для обычного подключения (Remnawave):</b>\n"
            f"<code>{sub_url or 'Ошибка получения ссылки'}</code>\n\n"
            "<b>📌 Ссылка для обхода глушилок (3X-UI):</b>\n"
            f"<code>{xui_link}</code>\n\n"
            "🟢 <i>Оба способа активны</i>"
        )
    else:
        text = (
            "🔐 <b>Мой доступ (Обычная подписка)</b>\n\n"
            "<blockquote>"
            f"📆 Осталось времени: <b>{remaining_str}</b>\n"
            "🌐 Группа подключения: <b>SPN-Squad</b>\n"
            "</blockquote>\n\n"
            "<b>Персональная ссылка доступа:</b>\n"
            f"<code>{sub_url or 'Ошибка получения ссылки'}</code>\n\n"
            "🟢 <i>Статус: активен</i>"
        )

    await callback.message.edit_text(text, reply_markup=kb)
