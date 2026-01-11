import logging
import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import TARIFFS, DEFAULT_SQUAD_UUID
from states import UserStates
import database as db
from services.remnawave import remnawave_get_subscription_url, remnawave_get_user_info
from services.cryptobot import create_cryptobot_invoice, get_invoice_status, process_paid_invoice


router = Router()


@router.callback_query(F.data == "buy_subscription")
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext):
    """Показать выбор тарифов"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц — 100₽", callback_data="tariff_1m")],
        [InlineKeyboardButton(text="3 месяца — 249₽", callback_data="tariff_3m")],
        [InlineKeyboardButton(text="6 месяцев — 449₽", callback_data="tariff_6m")],
        [InlineKeyboardButton(text="12 месяцев — 990₽", callback_data="tariff_12m")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    await callback.message.edit_text("Выбери срок подписки:", reply_markup=kb)
    await state.set_state(UserStates.choosing_tariff)


@router.callback_query(F.data.startswith("tariff_"))
async def process_tariff_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор тарифа"""
    tariff_code = callback.data.split("_")[1]
    await state.update_data(tariff_code=tariff_code)

    tariff = TARIFFS[tariff_code]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_cryptobot")],
        [InlineKeyboardButton(text="💳 Yookassa", callback_data="pay_yookassa")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    text = f"<b>Оплата тарифа {tariff_code}</b>\nСумма: {tariff['price']} ₽\n\nВыбери способ оплаты:"

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_payment)


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать счёт в CryptoBot"""
    data = await state.get_data()
    tariff_code = data.get("tariff_code")

    if not tariff_code:
        await callback.message.edit_text("Ошибка: тариф не выбран")
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    amount = tariff["price"]

    # Создаём счёт в CryptoBot
    invoice = await create_cryptobot_invoice(callback.bot, amount, tariff_code, callback.from_user.id)

    if not invoice:
        await callback.message.edit_text("Ошибка создания счёта в CryptoBot. Попробуй позже.")
        await state.clear()
        return

    invoice_id = invoice["invoice_id"]
    pay_url = invoice["bot_invoice_url"]

    # Записываем платеж в БД
    db.create_payment(
        callback.from_user.id,
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
    """Заглушка для оплаты через Yookassa"""
    data = await state.get_data()
    tariff_code = data.get("tariff_code")

    if not tariff_code:
        await callback.message.edit_text("Ошибка: тариф не выбран")
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    amount = tariff["price"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    text = (
        f"<b>💳 Yookassa</b>\n\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {amount} ₽\n\n"
        "⚠️ Способ оплаты Yookassa ещё находится в разработке.\n\n"
        "Используй CryptoBot для оплаты или обратись в поддержку."
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "check_payment")
async def process_check_payment(callback: CallbackQuery):
    """Проверить статус платежа"""
    tg_id = callback.from_user.id
    pending = db.get_last_pending_payment(tg_id)

    if not pending:
        await callback.answer("Нет ожидающих оплаты счетов", show_alert=True)
        return

    if not db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        invoice_id, tariff_code = pending

        # Проверяем статус счёта
        invoice = await get_invoice_status(invoice_id)

        if invoice and invoice.get("status") == "paid":
            # Обрабатываем оплату
            success = await process_paid_invoice(callback.bot, tg_id, invoice_id, tariff_code)
            
            if success:
                await callback.message.edit_text(
                    "✅ <b>Оплата подтверждена!</b>\n\n"
                    f"Тариф: {tariff_code}\n"
                    "Ссылка подписки отправлена в сообщении выше."
                )
            else:
                await callback.answer("Ошибка при активации подписки", show_alert=True)
        else:
            await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)

    except Exception as e:
        logging.error(f"Check payment error: {e}")
        await callback.answer("Ошибка при проверке платежа", show_alert=True)
    
    finally:
        db.release_user_lock(tg_id)


@router.callback_query(F.data == "my_subscription")
async def process_my_subscription(callback: CallbackQuery):
    """Показать информацию о подписке пользователя"""
    tg_id = callback.from_user.id
    user = db.get_user(tg_id)

    if not user or not user[3]:  # remnawave_uuid
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оформить подписку", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])
        await callback.message.edit_text(
            "У тебя пока нет активной подписки.\nОформи её сейчас!",
            reply_markup=kb
        )
        return

    # Получаем актуальную информацию о подписке из Remnawave
    remaining_str = "неизвестно"
    sub_url = "ошибка получения ссылки"

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Получаем ссылку подписки
            sub_url = await remnawave_get_subscription_url(session, user[3])

            # Получаем информацию о пользователе (включая expireAt)
            user_info = await remnawave_get_user_info(session, user[3])

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

    text = (
        "🔐 <b>Моя подписка</b>\n\n"
        f"📆 Осталось ещё: {remaining_str}\n"
        f"Сквад: SPN-Squad\n\n"
        f"<b>Ссылка (кликабельно):</b>\n{sub_url or 'ошибка получения ссылки'}\n\n"
        "Статус: активна"
    )

    await callback.message.edit_text(text, reply_markup=kb)
