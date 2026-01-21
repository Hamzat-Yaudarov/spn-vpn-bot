import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import TARIFFS, VIP_TARIFFS, COMBO_TARIFFS
from states import UserStates
import database as db
from services.cryptobot import create_cryptobot_invoice
from services.yookassa import create_yookassa_payment


router = Router()

# Предустановленные суммы для пополнения баланса
TOPUP_AMOUNTS = {
    "100": 100,
    "500": 500,
    "1000": 1000,
    "other": 0  # Произвольная сумма
}


@router.message(UserStates.topup_choose_amount)
async def process_custom_topup_amount(message: Message, state: FSMContext):
    """Обработать введённую сумму пополнения"""
    try:
        amount = int(message.text)
        if amount <= 0:
            await message.answer("Сумма должна быть больше 0")
            return

        logging.info(f"User {message.from_user.id} entered custom top-up amount: {amount}")
        await state.update_data(topup_amount=amount)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 CryptoBot", callback_data="topup_pay_cryptobot")],
            [InlineKeyboardButton(text="💳 Yookassa", callback_data="topup_pay_yookassa")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="topup_balance")]
        ])

        text = f"<b>Способ оплаты</b>\n\nСумма: <b>{amount} ₽</b>\n\nВыбери способ:"
        await message.answer(text, reply_markup=kb)
        await state.set_state(UserStates.topup_choose_payment)
    except ValueError:
        await message.answer("Введи целое число (например: 100)")
    except Exception as e:
        logging.error(f"Custom top-up error: {e}")
        await message.answer("Ошибка при обработке суммы")


@router.callback_query(F.data == "balance")
async def process_balance(callback: CallbackQuery):
    """Показать баланс и опции"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} viewing balance")

    balance = await db.get_balance(tg_id)
    referral_commission = await db.get_referral_commission(tg_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Пополнить баланс", callback_data="topup_balance")],
        [InlineKeyboardButton(text="📊 Снять комиссию", callback_data="withdraw_commission")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>💳 Мой баланс</b>\n\n"
        f"<b>Основной баланс:</b> {balance:.2f} ₽\n"
        f"<b>Реферальная комиссия:</b> {referral_commission:.2f} ₽\n\n"
        "Используй баланс для покупки или продления подписки"
    )

    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "topup_balance")
async def process_topup_balance(callback: CallbackQuery, state: FSMContext):
    """Выбрать сумму для пополнения"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} initiated balance top-up")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="100 ₽", callback_data="topup_amount_100")],
        [InlineKeyboardButton(text="500 ₽", callback_data="topup_amount_500")],
        [InlineKeyboardButton(text="1000 ₽", callback_data="topup_amount_1000")],
        [InlineKeyboardButton(text="📝 Другая сумма", callback_data="topup_amount_other")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="balance")]
    ])

    text = "<b>Выбери сумму пополнения:</b>"
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("topup_amount_"))
async def process_topup_amount(callback: CallbackQuery, state: FSMContext):
    """Обработать выбранную сумму"""
    tg_id = callback.from_user.id
    amount_key = callback.data.split("_")[2]
    
    if amount_key == "other":
        await callback.message.edit_text("Введи сумму (целое число в рублях):")
        await state.set_state(UserStates.topup_choose_amount)
        return
    
    amount = TOPUP_AMOUNTS.get(amount_key, 0)
    if amount <= 0:
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    
    logging.info(f"User {tg_id} selected top-up amount: {amount}")
    await state.update_data(topup_amount=amount)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="topup_pay_cryptobot")],
        [InlineKeyboardButton(text="💳 Yookassa", callback_data="topup_pay_yookassa")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="topup_balance")]
    ])

    text = f"<b>Способ оплаты</b>\n\nСумма: <b>{amount} ₽</b>\n\nВыбери способ:"
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.topup_choose_payment)


@router.callback_query(F.data == "topup_pay_cryptobot")
async def process_topup_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать счёт для пополнения через CryptoBot"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    amount = data.get("topup_amount", 0)
    
    if amount <= 0:
        await callback.message.edit_text("Ошибка: сумма не выбрана")
        await state.clear()
        return
    
    logging.info(f"User {tg_id} selected CryptoBot for top-up: {amount}")
    
    invoice = await create_cryptobot_invoice(callback.bot, amount, f"topup_{amount}", tg_id)
    
    if not invoice:
        await callback.message.edit_text("Ошибка создания счёта в CryptoBot. Попробуй позже.")
        await state.clear()
        return
    
    invoice_id = invoice["invoice_id"]
    pay_url = invoice["bot_invoice_url"]
    
    # Записываем платеж в БД как top-up
    await db.create_payment(
        tg_id,
        f"topup_{amount}",
        amount,
        "cryptobot",
        invoice_id
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_topup_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="topup_balance")]
    ])

    text = (
        f"<b>Счёт на пополнение</b>\n\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати через CryptoBot. После оплаты баланс автоматически пополнится.\n"
        "Если не пополнилось — нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "topup_pay_yookassa")
async def process_topup_pay_yookassa(callback: CallbackQuery, state: FSMContext):
    """Создать платёж для пополнения через Yookassa"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    amount = data.get("topup_amount", 0)
    
    if amount <= 0:
        await callback.message.edit_text("Ошибка: сумма не выбрана")
        await state.clear()
        return
    
    logging.info(f"User {tg_id} selected Yookassa for top-up: {amount}")
    
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
    
    # Записываем платеж в БД как top-up
    await db.create_payment(
        tg_id,
        f"topup_{amount}",
        amount,
        "yookassa",
        payment_id
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_topup_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="topup_balance")]
    ])

    text = (
        f"<b>💳 Yookassa</b>\n\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати картой, СБП или другим способом через Yookassa.\n"
        "После оплаты баланс автоматически пополнится.\n"
        "Если не пополнилось — нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "check_topup_payment")
async def process_check_topup_payment(callback: CallbackQuery):
    """Проверить статус платежа для пополнения"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking top-up payment")
    
    # Получаем последний ожидающий платеж с информацией о провайдере
    result = await db.db_execute(
        """
        SELECT invoice_id, tariff_code, provider, amount
        FROM payments
        WHERE tg_id = $1 AND status = 'pending' AND tariff_code LIKE 'topup_%'
        ORDER BY id DESC
        LIMIT 1
        """,
        (tg_id,),
        fetch_one=True
    )

    if not result:
        await callback.answer("Нет ожидающих платежей пополнения", show_alert=True)
        return

    invoice_id = result['invoice_id']
    amount = result['amount']
    provider = result['provider']
    
    # Используем тот же механизм, что и для платежей подписки
    # Но с добавлением денег на баланс вместо активации подписки
    if provider == "yookassa":
        from services.yookassa import get_payment_status
        payment = await get_payment_status(invoice_id)

        if payment and payment.get("status") == "succeeded":
            # Пополняем баланс
            await db.add_balance(tg_id, amount)
            await db.update_payment_status_by_invoice(invoice_id, "succeeded")
            
            await callback.message.edit_text(
                f"✅ <b>Баланс пополнен!</b>\n\n"
                f"Добавлено: {amount} ₽"
            )
        else:
            await callback.answer("Оплата ещё не прошла", show_alert=True)

    elif provider == "cryptobot":
        from services.cryptobot import get_invoice_status
        invoice = await get_invoice_status(invoice_id)

        if invoice and invoice.get("status") == "paid":
            # Пополняем баланс
            await db.add_balance(tg_id, amount)
            await db.update_payment_status_by_invoice(invoice_id, "paid")
            
            await callback.message.edit_text(
                f"✅ <b>Баланс пополнен!</b>\n\n"
                f"Добавлено: {amount} ₽"
            )
        else:
            await callback.answer("Оплата ещё не прошла", show_alert=True)


@router.callback_query(F.data == "withdraw_commission")
async def process_withdraw_commission(callback: CallbackQuery):
    """Снять реферальную комиссию на основной баланс"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} withdrawing referral commission")

    commission = await db.get_referral_commission(tg_id)

    if commission <= 0:
        await callback.answer("У тебя нет накопленной комиссии", show_alert=True)
        return

    new_balance = await db.withdraw_referral_commission(tg_id)

    await callback.answer(
        f"✅ Комиссия переведена на баланс!\n"
        f"Сумма: {commission:.2f} ₽\n"
        f"Новый баланс: {new_balance:.2f} ₽",
        show_alert=True
    )

    # Обновляем отображение баланса
    await process_balance(callback)
