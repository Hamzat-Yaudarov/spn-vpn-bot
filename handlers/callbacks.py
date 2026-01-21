import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import SUPPORT_URL
from states import UserStates
import database as db
from handlers.start import show_main_menu
from services.cryptobot import create_cryptobot_invoice, get_invoice_status, process_paid_invoice
from services.yookassa import create_yookassa_payment, get_payment_status, process_paid_yookassa_payment


router = Router()

# Mapping of button values to amounts
TOPUP_AMOUNTS = {
    "topup_100": 100,
    "topup_500": 500,
    "topup_1000": 1000,
    "topup_5000": 5000
}


@router.callback_query(F.data == "accept_terms")
async def process_accept_terms(callback: CallbackQuery, state: FSMContext):
    """Обработчик принятия условий использования"""
    tg_id = callback.from_user.id
    username = callback.from_user.username
    logging.info(f"User {tg_id}(@{username}) accepted terms")

    await db.accept_terms(tg_id)

    await callback.message.delete()
    await state.clear()

    await callback.bot.send_message(
        callback.message.chat.id,
        "Соглашение принято! Добро пожаловать!"
    )

    await show_main_menu(callback.message)


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} returned to main menu")

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔐 Моя подписка", callback_data="my_subscription")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data="show_balance")],
        [InlineKeyboardButton(text="📲 Как подключиться", callback_data="how_to_connect")],
        [InlineKeyboardButton(text="🎁 Получить подарок", callback_data="get_gift")],
        [InlineKeyboardButton(text="👥 Бонус за друга", callback_data="referral")],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)]
    ])

    text = (
        "<b>SPN — стабильное и быстрое интернет-соединение</b>\n\n"
        "<b>Что вы получаете:</b>\n"
        "<blockquote>"
        "• Улучшенную работу сайтов, мессенджеров и онлайн-сервисов\n"
        "• Более стабильное соединение даже при перегрузках сети\n"
        "• Поддержку Android, iOS, Windows, macOS и Linux\n"
        "• Простое подключение за 1–2 минуты\n"
        "• Защиту и оптимизацию интернет-трафика"
        "</blockquote>\n\n"
        "<b>После активации:</b>\n"
        "<blockquote>"
        "🔐 Персональный доступ SPN на выбранный срок\n"
        "📥 Пошаговую инструкцию по подключению\n"
        "🛟 Поддержку в Telegram\n"
        "🌍 Свободную и стабильную работу в интернете"
        "</blockquote>\n\n"
        "<b>Реферальная программа:</b>\n"
        "<blockquote>"
        "👥 За каждого приглашённого пользователя,\n"
        "активировавшего доступ, получаете 25% от суммы"
        "</blockquote>"
    )

    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "show_balance")
async def process_show_balance(callback: CallbackQuery):
    """Показать баланс пользователя"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking balance")

    balance = await db.get_balance(tg_id)
    referral_balance = await db.get_referral_balance(tg_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="top_up_balance")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>💰 Мой баланс</b>\n\n"
        f"<b>Основной баланс:</b> <code>{balance:.2f} ₽</code>\n"
        f"<b>Реферальный баланс:</b> <code>{referral_balance:.2f} ₽</code>\n\n"
        "Баланс используется для оплаты подписок. "
        "Пополните баланс и оплачивайте подписки прямо из приложения!"
    )

    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "top_up_balance")
async def process_top_up_balance(callback: CallbackQuery, state: FSMContext):
    """Начать процесс пополнения баланса"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started balance top-up")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="100 ₽", callback_data="topup_100")],
        [InlineKeyboardButton(text="500 ₽", callback_data="topup_500")],
        [InlineKeyboardButton(text="1000 ₽", callback_data="topup_1000")],
        [InlineKeyboardButton(text="5000 ₽", callback_data="topup_5000")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="show_balance")]
    ])

    text = "<b>Выберите сумму для пополнения:</b>"
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_topup_amount)


@router.callback_query(F.data.startswith("topup_"))
async def process_topup_amount(callback: CallbackQuery, state: FSMContext):
    """Выбрать способ оплаты для пополнения баланса"""
    tg_id = callback.from_user.id
    topup_amount = TOPUP_AMOUNTS.get(callback.data)

    if topup_amount is None:
        await callback.answer("❌ Неверная сумма", show_alert=True)
        return

    await state.update_data(topup_amount=topup_amount)
    logging.info(f"User {tg_id} selected topup amount: {topup_amount}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="topup_pay_cryptobot")],
        [InlineKeyboardButton(text="💳 Yookassa", callback_data="topup_pay_yookassa")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="show_balance")]
    ])

    text = (
        f"<b>Пополнение баланса на {topup_amount} ₽</b>\n\n"
        "Выберите способ оплаты:"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_topup_payment)


@router.callback_query(F.data == "topup_pay_cryptobot", UserStates.choosing_topup_payment)
async def process_topup_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать платёж для пополнения баланса через CryptoBot"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    amount = data.get("topup_amount")
    logging.info(f"User {tg_id} selected topup via CryptoBot: {amount}")

    if not amount:
        await callback.answer("❌ Ошибка: сумма не найдена", show_alert=True)
        await state.clear()
        return

    # Проверяем есть ли уже активный платёж на пополнение баланса
    existing_invoice_id = await db.get_active_topup_payment(tg_id, amount, "cryptobot")

    invoice = None
    invoice_id = None
    pay_url = None

    if existing_invoice_id:
        # Счёт уже есть - получаем его статус
        invoice = await get_invoice_status(existing_invoice_id)
        if invoice and invoice.get("status") == "active":
            invoice_id = existing_invoice_id
            pay_url = invoice.get("bot_invoice_url", "")

    if not invoice_id:
        # Создаём новый счёт
        invoice = await create_cryptobot_invoice(callback.bot, amount, f"topup_{amount}", tg_id)
        if not invoice:
            await callback.answer("❌ Ошибка создания счёта. Попробуй позже.", show_alert=True)
            await state.clear()
            return

        invoice_id = invoice["invoice_id"]
        pay_url = invoice["bot_invoice_url"]

        # Записываем платёж на пополнение баланса в БД
        await db.create_balance_payment(tg_id, amount, "cryptobot", invoice_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data="check_topup_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="show_balance")]
    ])

    text = (
        f"<b>Пополнение баланса на {amount} ₽</b>\n\n"
        "💎 CryptoBot\n\n"
        "Оплати через CryptoBot. После оплаты нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "topup_pay_yookassa", UserStates.choosing_topup_payment)
async def process_topup_yookassa(callback: CallbackQuery, state: FSMContext):
    """Создать платёж для пополнения баланса через Yookassa"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    amount = data.get("topup_amount")
    logging.info(f"User {tg_id} selected topup via Yookassa: {amount}")

    if not amount:
        await callback.answer("❌ Ошибка: сумма не найдена", show_alert=True)
        await state.clear()
        return

    # Проверяем есть ли уже активный платёж на пополнение баланса
    existing_payment_id = await db.get_active_topup_payment(tg_id, amount, "yookassa")

    payment = None
    payment_id = None
    confirmation_url = None

    if existing_payment_id:
        # Платёж уже есть - получаем его статус
        payment = await get_payment_status(existing_payment_id)
        if payment and payment.get("status") == "pending":
            payment_id = existing_payment_id
            confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")

    if not payment_id:
        # Создаём новый платёж
        payment = await create_yookassa_payment(callback.bot, amount, f"topup_{amount}", tg_id)
        if not payment:
            await callback.answer("❌ Ошибка создания платежа. Попробуй позже.", show_alert=True)
            await state.clear()
            return

        payment_id = payment["id"]
        confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")

        if not confirmation_url:
            await callback.answer("❌ Ошибка: не получена ссылка для оплаты", show_alert=True)
            await state.clear()
            return

        # Записываем платёж на пополнение баланса в БД
        await db.create_balance_payment(tg_id, amount, "yookassa", payment_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data="check_topup_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="show_balance")]
    ])

    text = (
        f"<b>Пополнение баланса на {amount} ₽</b>\n\n"
        "💳 Yookassa\n\n"
        "Оплати картой, СБП или другим способом. После оплаты нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "check_topup_payment")
async def process_check_topup_payment(callback: CallbackQuery):
    """Проверить статус платежа пополнения баланса"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking topup payment status")

    # Получаем последний ожидающий платёж на пополнение баланса
    result = await db.db_execute(
        """
        SELECT invoice_id, provider
        FROM balance_payments
        WHERE tg_id = $1 AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (tg_id,),
        fetch_one=True
    )

    if not result:
        await callback.answer("Нет ожидающих платежей на пополнение баланса", show_alert=True)
        return

    invoice_id = result['invoice_id']
    provider = result['provider']

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        if provider == "yookassa":
            # Проверяем платёж в Yookassa
            payment = await get_payment_status(invoice_id)

            if payment and payment.get("status") == "succeeded":
                # Получаем сумму платежа
                amount = payment.get("amount", {}).get("value", 0)
                amount = float(amount)

                # Зачисляем на баланс
                await db.add_balance(tg_id, amount)
                await db.update_balance_payment_status(invoice_id, 'paid')

                await callback.message.edit_text(
                    f"✅ <b>Платёж подтвержден!</b>\n\n"
                    f"На баланс добавлено: {amount} ₽"
                )
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)

        elif provider == "cryptobot":
            # Проверяем платёж в CryptoBot
            invoice = await get_invoice_status(invoice_id)

            if invoice and invoice.get("status") == "paid":
                # Получаем сумму платежа
                amount = float(invoice.get("amount", 0))

                # Зачисляем на баланс
                await db.add_balance(tg_id, amount)
                await db.update_balance_payment_status(invoice_id, 'paid')

                await callback.message.edit_text(
                    f"✅ <b>Платёж подтвержден!</b>\n\n"
                    f"На баланс добавлено: {amount} ₽"
                )
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)

    except Exception as e:
        logging.error(f"Check topup payment error: {e}")
        await callback.answer("❌ Ошибка при проверке платежа", show_alert=True)

    finally:
        await db.release_user_lock(tg_id)


@router.callback_query(F.data == "how_to_connect")
async def process_how_to_connect(callback: CallbackQuery):
    """Показать инструкцию по подключению"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Как подключиться</b>\n\n"

        "<b>Способ 1 — v2RayTun</b>\n"
        "<blockquote>"
        "1️⃣ Установите приложение:\n"
        "• <a href=\"https://play.google.com/store/apps/details?id=com.v2raytun.android\">Android</a>\n"
        "• <a href=\"https://apps.apple.com/app/id6446114838\">iOS</a>\n\n"
        "2️⃣ Откройте приложение и добавьте новую конфигурацию\n"
        "3️⃣ Вставьте персональную ссылку доступа\n"
        "4️⃣ Активируйте соединение\n"
        "</blockquote>\n\n"

        "<b>Способ 2 — Happ</b>\n"
        "<blockquote>"
        "1️⃣ Установите приложение Happ из магазина приложений\n"
        "2️⃣ Откройте приложение и вставьте ссылку доступа\n"
        "3️⃣ Подтвердите подключение\n"
        "</blockquote>\n\n"

        "ℹ️ <i>Никаких ручных настроек и сложных параметров — всё работает автоматически.</i>"
    )

    await callback.message.edit_text(text, reply_markup=kb)
