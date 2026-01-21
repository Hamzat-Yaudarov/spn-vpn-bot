import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import SUPPORT_URL
from states import UserStates
import database as db
from handlers.start import show_main_menu
from services.cryptobot import create_cryptobot_invoice, get_invoice_status
from services.yookassa import create_yookassa_payment, get_payment_status


router = Router()


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
        [InlineKeyboardButton(text="💰 Баланс", callback_data="check_balance")],
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
        "активировавшего доступ, вы получаете +7 дней"
        "</blockquote>"
    )

    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "check_balance")
async def process_check_balance(callback: CallbackQuery):
    """Показать баланс пользователя"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking balance")

    balance = await db.get_balance(tg_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="topup_balance")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>💰 Мой баланс</b>\n\n"
        f"<blockquote>"
        f"Доступные средства: <b>{balance:.2f} ₽</b>\n"
        "</blockquote>\n\n"
        "Баланс пополняется через реферальную программу.\n"
        "За каждую покупку реферала вы получаете <b>25%</b> с суммы покупки.\n\n"
        "Рекомендация: используйте баланс для покупки подписки и экономьте еще больше!"
    )

    await callback.message.edit_text(text, reply_markup=kb)


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


@router.callback_query(F.data == "topup_balance")
async def process_topup_balance(callback: CallbackQuery, state: FSMContext):
    """Показать варианты пополнения баланса"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: topup_balance")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="100 ₽", callback_data="topup_amount_100")],
        [InlineKeyboardButton(text="250 ₽", callback_data="topup_amount_250")],
        [InlineKeyboardButton(text="500 ₽", callback_data="topup_amount_500")],
        [InlineKeyboardButton(text="1000 ₽", callback_data="topup_amount_1000")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="check_balance")]
    ])

    text = (
        "<b>➕ Пополнить баланс</b>\n\n"
        "Выбери сумму пополнения:\n\n"
        "💡 Совет: пополните баланс и экономьте на подписках!"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_topup_amount)


@router.callback_query(F.data.startswith("topup_amount_"))
async def process_topup_amount(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор суммы пополнения"""
    tg_id = callback.from_user.id
    amount = int(callback.data.split("_")[2])
    logging.info(f"User {tg_id} selected topup amount: {amount}")

    await state.update_data(topup_amount=amount)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="topup_cryptobot")],
        [InlineKeyboardButton(text="💳 Yookassa", callback_data="topup_yookassa")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="topup_balance")]
    ])

    text = (
        f"<b>Пополнение баланса</b>\n\n"
        f"Сумма: <b>{amount} ₽</b>\n\n"
        "Выбери способ оплаты:"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_topup_method)


@router.callback_query(F.data == "topup_cryptobot")
async def process_topup_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать счёт для пополнения баланса через CryptoBot"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    amount = data.get("topup_amount")
    logging.info(f"User {tg_id} selected topup via CryptoBot (amount: {amount})")

    if not amount:
        await callback.message.edit_text("Ошибка: сумма не выбрана")
        await state.clear()
        return

    # Создаём счёт для пополнения баланса
    invoice = await create_cryptobot_invoice(callback.bot, amount, f"topup_{amount}", tg_id)

    if not invoice:
        await callback.message.edit_text("Ошибка создания счёта в CryptoBot. Попробуй позже.")
        await state.clear()
        return

    invoice_id = invoice["invoice_id"]
    pay_url = invoice["bot_invoice_url"]

    # Записываем платеж в БД с типом "topup"
    await db.create_payment(
        tg_id,
        f"topup_{amount}",
        amount,
        "cryptobot",
        invoice_id,
        "topup"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_topup_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="check_balance")]
    ])

    text = (
        f"<b>Пополнение баланса</b>\n\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати через CryptoBot. После оплаты баланс пополнится автоматически.\n"
        "Если не пополнилось — нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "topup_yookassa")
async def process_topup_yookassa(callback: CallbackQuery, state: FSMContext):
    """Создать платёж для пополнения баланса через Yookassa"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    amount = data.get("topup_amount")
    logging.info(f"User {tg_id} selected topup via Yookassa (amount: {amount})")

    if not amount:
        await callback.message.edit_text("Ошибка: сумма не выбрана")
        await state.clear()
        return

    # Создаём платёж для пополнения баланса
    payment = await create_yookassa_payment(callback.bot, amount, f"topup_{amount}", tg_id)

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

    # Записываем платеж в БД с типом "topup"
    await db.create_payment(
        tg_id,
        f"topup_{amount}",
        amount,
        "yookassa",
        payment_id,
        "topup"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_topup_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="check_balance")]
    ])

    text = (
        f"<b>Пополнение баланса</b>\n\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати картой, СБП или другим способом через Yookassa.\n"
        "После оплаты баланс пополнится автоматически.\n"
        "Если не пополнилось — нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "check_topup_payment")
async def process_check_topup_payment(callback: CallbackQuery):
    """Проверить статус платежа пополнения баланса"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking topup payment status")

    # Проверка anti-spam: не более одной проверки в 1 секунду
    can_check, error_msg = await db.can_check_payment(tg_id)
    if not can_check:
        await callback.answer(error_msg, show_alert=True)
        return

    # Обновляем время последней проверки
    await db.update_last_payment_check(tg_id)

    # Получаем последний ожидающий платеж пополнения с информацией о провайдере
    result = await db.db_execute(
        """
        SELECT invoice_id, tariff_code, provider, subscription_type
        FROM payments
        WHERE tg_id = $1 AND status = 'pending' AND subscription_type = 'topup'
        ORDER BY id DESC
        LIMIT 1
        """,
        (tg_id,),
        fetch_one=True
    )

    if not result:
        await callback.answer("Нет ожидающих пополнений", show_alert=True)
        return

    invoice_id = result['invoice_id']
    tariff_code = result['tariff_code']
    provider = result['provider']
    amount = int(tariff_code.split("_")[1])

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        if provider == "yookassa":
            # Проверяем платёж в Yookassa
            payment = await get_payment_status(invoice_id)

            if payment and payment.get("status") == "succeeded":
                # Пополняем баланс
                await db.add_balance(tg_id, amount)
                await db.update_payment_status_by_invoice(invoice_id, 'paid')

                await callback.message.edit_text(
                    f"✅ <b>Пополнение прошло успешно!</b>\n\n"
                    f"Сумма: {amount} ₽\n"
                    f"Баланс пополнен!"
                )
                logging.info(f"User {tg_id} balance topped up with {amount}₽ via Yookassa")
            else:
                await callback.answer("Оплата ещё не прошла", show_alert=True)

        elif provider == "cryptobot":
            # Проверяем платёж в CryptoBot
            invoice = await get_invoice_status(invoice_id)

            if invoice and invoice.get("status") == "paid":
                # Пополняем баланс
                await db.add_balance(tg_id, amount)
                await db.update_payment_status_by_invoice(invoice_id, 'paid')

                await callback.message.edit_text(
                    f"✅ <b>Пополнение прошло успешно!</b>\n\n"
                    f"Сумма: {amount} ₽\n"
                    f"Баланс пополнен!"
                )
                logging.info(f"User {tg_id} balance topped up with {amount}₽ via CryptoBot")
            else:
                await callback.answer("Оплата ещё не прошла", show_alert=True)

    except Exception as e:
        logging.error(f"Check topup payment error: {e}")
        await callback.answer("Ошибка при проверке платежа", show_alert=True)

    finally:
        await db.release_user_lock(tg_id)
