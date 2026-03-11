import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import database as db
from services.image_handler import edit_text_with_photo


logger = logging.getLogger(__name__)


class ReferralWithdrawalStates(StatesGroup):
    """Состояния для вывода средств рефералом"""
    choosing_method = State()
    entering_sbp_amount = State()
    entering_sbp_bank = State()
    entering_sbp_phone = State()
    entering_usdt_amount = State()
    entering_usdt_address = State()


router = Router()


@router.callback_query(F.data == "referral")
async def process_referral(callback: CallbackQuery):
    """Показать информацию о реферальной программе и балансе"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} viewing referral program")

    # Получаем реферальную ссылку
    bot_username = (await callback.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{tg_id}"

    # Получаем статистику баланса рефералов
    balance_stats = await db.get_referral_balance_stats(tg_id)

    # Форматируем заработок по тарифам
    tariff_names = {
        "1m": "1 месяц",
        "3m": "3 месяца",
        "6m": "6 месяцев",
        "12m": "12 месяцев"
    }

    earnings_text = ""
    if balance_stats['earnings_by_tariff']:
        earnings_text = "\n📊 <b>Покупки по тарифам:</b>\n"
        for earning in balance_stats['earnings_by_tariff']:
            tariff_name = tariff_names.get(earning['tariff_code'], earning['tariff_code'])
            earnings_text += f"• {tariff_name}: {earning['purchase_count']}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Вывести средства", callback_data="referral_withdraw_start")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>💰 Реферальная программа</b>\n\n"
        "<blockquote>"
        "Приглашайте друзей и зарабатывайте на их покупках:\n"
        "• 35% от первой покупки реферала\n"
        "• 15% от повторных покупок\n"
        "</blockquote>\n\n"
        f"👥 <b>Всего приглашено:</b> {balance_stats['referral_count']}\n"
        f"{earnings_text}"
        f"\n💰 <b>Всего заработано:</b> {balance_stats['total_earned']:.2f} ₽\n"
        f"💸 <b>Всего выведено:</b> {balance_stats['total_withdrawn']:.2f} ₽\n"
        f"🪙 <b>Текущий баланс:</b> {balance_stats['current_balance']:.2f} ₽\n\n"
        f"<blockquote>\n"
        f"ℹ️ <i>Минимальная сумма вывода: 5000 ₽</i>\n"
        f"</blockquote>\n\n"
        "🔗 <b>Ваша реферальная ссылка:</b>\n"
        f"<code>{referral_link}</code>"
    )

    await edit_text_with_photo(callback, text, kb, "Реферальная программа")


@router.callback_query(F.data == "referral_withdraw_start")
async def referral_withdraw_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода средств"""
    tg_id = callback.from_user.id

    # Проверяем баланс
    balance_stats = await db.get_referral_balance_stats(tg_id)
    balance = balance_stats['current_balance']

    if balance < 5000:
        await callback.answer(f"❌ Недостаточно средств! Ваш баланс: {balance:.2f} ₽\nМинимум для вывода: 5000 ₽", show_alert=True)
        return

    await state.set_state(ReferralWithdrawalStates.choosing_method)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 На карту (СБП)", callback_data="referral_withdraw_sbp")],
        [InlineKeyboardButton(text="💎 USDT (крипто)", callback_data="referral_withdraw_usdt")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="referral")]
    ])

    text = (
        "<b>💸 Способ вывода средств</b>\n\n"
        "Выбери способ, которым хочешь вывести заработанные деньги:"
    )

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "referral_withdraw_sbp")
async def referral_withdraw_sbp(callback: CallbackQuery, state: FSMContext):
    """Вывод на карту через СБП"""
    tg_id = callback.from_user.id
    balance_stats = await db.get_referral_balance_stats(tg_id)
    balance = balance_stats['current_balance']

    await state.set_state(ReferralWithdrawalStates.entering_sbp_amount)

    text = (
        "<b>💳 Вывод на карту (СБП)</b>\n\n"
        f"Ваш баланс: <b>{balance:.2f} ₽</b>\n\n"
        "Укажи сумму для вывода (от 5000 ₽):"
    )

    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.message(ReferralWithdrawalStates.entering_sbp_amount)
async def process_sbp_amount(message: Message, state: FSMContext):
    """Получить сумму вывода для СБП"""
    tg_id = message.from_user.id
    balance_stats = await db.get_referral_balance_stats(tg_id)
    balance = balance_stats['current_balance']

    try:
        amount = float(message.text)
        if amount < 5000:
            await message.answer("❌ Минимальная сумма для вывода: 5000 ₽")
            return
        if amount > balance:
            await message.answer(f"❌ Сумма превышает ваш баланс ({balance:.2f} ₽)")
            return
    except ValueError:
        await message.answer("❌ Укажи корректную сумму")
        return

    await state.update_data(amount=amount)
    await state.set_state(ReferralWithdrawalStates.entering_sbp_bank)

    text = (
        "<b>💳 Указание реквизитов</b>\n\n"
        "Укажи название банка (например: Сбербанк, Тинькофф, Альфа-Банк):"
    )

    await message.answer(text, parse_mode="HTML")


@router.message(ReferralWithdrawalStates.entering_sbp_bank)
async def process_sbp_bank(message: Message, state: FSMContext):
    """Получить название банка"""
    bank_name = message.text.strip()
    await state.update_data(bank_name=bank_name)
    await state.set_state(ReferralWithdrawalStates.entering_sbp_phone)

    text = (
        "<b>📱 Номер телефона</b>\n\n"
        "Укажи номер телефона для карты (например: +7 900 123-45-67):"
    )

    await message.answer(text, parse_mode="HTML")


@router.message(ReferralWithdrawalStates.entering_sbp_phone)
async def process_sbp_phone(message: Message, state: FSMContext):
    """Получить номер телефона"""
    tg_id = message.from_user.id
    phone_number = message.text.strip()
    data = await state.get_data()

    # Создаём запрос на вывод
    await db.create_referral_withdrawal_request(
        referrer_id=tg_id,
        amount=data['amount'],
        withdrawal_type='sbp',
        bank_name=data['bank_name'],
        phone_number=phone_number
    )

    await state.clear()

    text = (
        "<b>✅ Запрос создан</b>\n\n"
        f"💰 Сумма: {data['amount']:.2f} ₽\n"
        f"💳 Банк: {data['bank_name']}\n"
        f"📱 Номер: {phone_number}\n\n"
        "Администратор рассмотрит твой запрос в течение 24 часов."
    )

    await message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "referral_withdraw_usdt")
async def referral_withdraw_usdt(callback: CallbackQuery, state: FSMContext):
    """Вывод в USDT (крипто)"""
    tg_id = callback.from_user.id
    balance_stats = await db.get_referral_balance_stats(tg_id)
    balance = balance_stats['current_balance']

    await state.set_state(ReferralWithdrawalStates.entering_usdt_amount)

    text = (
        "<b>💎 Вывод в USDT</b>\n\n"
        f"Ваш баланс: <b>{balance:.2f} ₽</b>\n\n"
        "Укажи сумму для вывода (от 5000 ₽):"
    )

    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.message(ReferralWithdrawalStates.entering_usdt_amount)
async def process_usdt_amount(message: Message, state: FSMContext):
    """Получить сумму вывода для USDT"""
    tg_id = message.from_user.id
    balance_stats = await db.get_referral_balance_stats(tg_id)
    balance = balance_stats['current_balance']

    try:
        amount = float(message.text)
        if amount < 5000:
            await message.answer("❌ Минимальная сумма для вывода: 5000 ₽")
            return
        if amount > balance:
            await message.answer(f"❌ Сумма превышает ваш баланс ({balance:.2f} ₽)")
            return
    except ValueError:
        await message.answer("❌ Укажи корректную сумму")
        return

    await state.update_data(amount=amount)
    await state.set_state(ReferralWithdrawalStates.entering_usdt_address)

    text = (
        "<b>💎 USDT адрес</b>\n\n"
        "Укажи адрес кошелька для получения USDT (TRC20 сеть):"
    )

    await message.answer(text, parse_mode="HTML")


@router.message(ReferralWithdrawalStates.entering_usdt_address)
async def process_usdt_address(message: Message, state: FSMContext):
    """Получить адрес USDT кошелька"""
    tg_id = message.from_user.id
    usdt_address = message.text.strip()
    data = await state.get_data()

    # Создаём запрос на вывод
    await db.create_referral_withdrawal_request(
        referrer_id=tg_id,
        amount=data['amount'],
        withdrawal_type='usdt',
        usdt_address=usdt_address
    )

    await state.clear()

    text = (
        "<b>✅ Запрос создан</b>\n\n"
        f"💰 Сумма: {data['amount']:.2f} ₽\n"
        f"💎 Адрес: <code>{usdt_address}</code>\n\n"
        "Администратор рассмотрит твой запрос в течение 24 часов."
    )

    await message.answer(text, parse_mode="HTML")
