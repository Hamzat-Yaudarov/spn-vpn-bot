import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import TARIFFS, ADMIN_ID
from states import UserStates
import database as db
from services.image_handler import edit_text_with_photo, send_text_with_photo


logger = logging.getLogger(__name__)


router = Router()


@router.callback_query(F.data == "referral")
async def process_referral(callback: CallbackQuery):
    """Показать информацию о реферальной программе"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} viewing referral program")

    # Получаем реферальную ссылку
    bot_username = (await callback.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{tg_id}"

    # Получаем статистику реферальных заработков
    stats = await db.get_referral_earnings_stats(tg_id)
    
    if not stats:
        # У пользователя ещё нет рефералов
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
        ])

        text = (
            "<b>💰 Реферальная программа</b>\n\n"
            "<blockquote>"
            "Приглашайте друзей и получайте комиссию за каждую их покупку!\n\n"
            "📊 <b>Как это работает:</b>\n"
            "• За <b>первую покупку</b> реферала: <b>35%</b> от суммы\n"
            "• За <b>последующие покупки</b>: <b>15%</b> от суммы\n"
            "</blockquote>\n\n"
            "🔗 <b>Ваша персональная ссылка:</b>\n"
            f"<code>{referral_link}</code>\n\n"
            "💡 <i>Нажми на ссылку чтобы скопировать её</i>\n\n"
            "ℹ️ <i>Минимальная сумма вывода: 5000 ₽</i>"
        )

        await edit_text_with_photo(callback, text, kb, "Реферальная программа")
        return

    # У пользователя есть рефералы - показываем полную статистику
    earnings_text = ""
    if stats['earnings_by_tariff']:
        earnings_text += "\n📊 <b>Покупки по тарифам:</b>\n"
        for earning in stats['earnings_by_tariff']:
            tariff_code = earning['tariff_code']
            count = earning['referral_count']
            days = TARIFFS[tariff_code]["days"]
            earnings_text += f"• {days} дней: {count}\n"
    else:
        earnings_text += "\n<i>Нет покупок у рефералов</i>\n"

    total_earned = stats['total_earned']
    total_withdrawn = stats['total_withdrawn']
    current_balance = stats['current_balance']

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Вывести средства", callback_data="referral_withdraw")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
    ])

    text = (
        "<b>💰 Реферальная программа</b>\n\n"
        "<blockquote>"
        "Вы приглашаете друзей и получаете комиссию за каждую их покупку!\n"
        "</blockquote>\n"
        f"{earnings_text}"
        f"\n💸 <b>Всего заработано:</b> {total_earned:.2f} ₽\n"
        f"📤 <b>Всего выведено:</b> {total_withdrawn:.2f} ₽\n"
        f"🪙 <b>Текущий баланс:</b> {current_balance:.2f} ₽\n\n"
        f"ℹ️ <b>Минимальная сумма вывода:</b> 5000 ₽\n\n"
        f"🔗 <b>Ваша персональная ссылка:</b>\n"
        f"<code>{referral_link}</code>\n\n"
        "💡 <i>Нажми на ссылку чтобы скопировать её</i>"
    )

    await edit_text_with_photo(callback, text, kb, "Реферальная программа")


@router.callback_query(F.data == "referral_withdraw")
async def process_referral_withdraw(callback: CallbackQuery):
    """Показать меню вывода реферальных средств"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} requesting referral withdrawal")

    # Получаем текущий баланс
    balance = await db.get_referral_withdrawal_balance(tg_id)

    if balance < 5000:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="referral", style="danger")]
        ])

        needed = 5000 - balance
        text = (
            "<b>❌ Недостаточно средств</b>\n\n"
            f"Ваш баланс: {balance:.2f} ₽\n"
            f"Минимальная сумма вывода: 5000 ₽\n\n"
            f"Вам нужно ещё: {needed:.2f} ₽"
        )

        await edit_text_with_photo(callback, text, kb, "Вывод средств")
        return

    # У пользователя достаточно средств
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏦 На банковский счёт", callback_data="referral_withdraw_bank")],
        [InlineKeyboardButton(text="💳 На карту по СБП", callback_data="referral_withdraw_card")],
        [InlineKeyboardButton(text="₿ На USDT", callback_data="referral_withdraw_usdt")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral", style="danger")]
    ])

    text = (
        "<b>💰 Вывод реферальных средств</b>\n\n"
        f"Ваш баланс: {balance:.2f} ₽\n"
        f"Минимальная сумма вывода: 5000 ₽\n\n"
        "Выберите способ вывода:"
    )

    await edit_text_with_photo(callback, text, kb, "Вывод средств")


# ────────────────────────────────────────────────
#        WITHDRAWAL FLOWS: BANK ACCOUNT
# ────────────────────────────────────────────────

@router.callback_query(F.data == "referral_withdraw_bank")
async def process_referral_withdraw_bank_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода на банковский счёт"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started bank withdrawal")

    # Проверяем баланс
    balance = await db.get_referral_withdrawal_balance(tg_id)

    if balance < 5000:
        await callback.answer("❌ Баланс меньше минимальной суммы вывода (5000 ₽)", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
    ])

    text = "🏦 <b>Вывод на банковский счёт</b>\n\n✅ Введите сумму вывода (минимум 5000 ₽):"

    await send_text_with_photo(callback.message, text, kb, "Введите сумму вывода")
    await state.set_state(UserStates.referral_waiting_withdraw_amount)


@router.message(UserStates.referral_waiting_withdraw_amount)
async def process_referral_amount(message: Message, state: FSMContext):
    """Обработчик ввода суммы для вывода"""
    tg_id = message.from_user.id

    try:
        amount = float(message.text)
        if amount < 5000:
            await message.answer("❌ Сумма должна быть не менее 5000 ₽")
            return

        # Проверяем баланс
        balance = await db.get_referral_withdrawal_balance(tg_id)
        if amount > balance:
            await message.answer(f"❌ Невозможно вывести больше чем есть на балансе ({balance:.2f} ₽)")
            return

        await state.update_data(withdrawal_amount=amount)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
        ])

        text = f"🏦 <b>Укажите банк</b>\n\n✅ Вы хотите вывести: <b>{amount:.2f} ₽</b>\n\nВведите название вашего банка:"

        await send_text_with_photo(message, text, kb, "Укажите банк")
        await state.set_state(UserStates.referral_waiting_bank_name)

    except ValueError:
        await message.answer("❌ Введите корректную сумму")


@router.message(UserStates.referral_waiting_bank_name)
async def process_referral_bank(message: Message, state: FSMContext):
    """Обработчик ввода банка"""
    tg_id = message.from_user.id
    bank_name = message.text.strip()

    if len(bank_name) < 2:
        await message.answer("❌ Введите корректное название банка")
        return

    await state.update_data(bank_name=bank_name)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
    ])

    text = f"📱 <b>Укажите номер телефона</b>\n\n✅ Введите номер телефона, к которому привязана карта (с кодом страны, например +7XXXXXXXXXX):"

    await send_text_with_photo(message, text, kb, "Укажите номер телефона")
    await state.set_state(UserStates.referral_waiting_phone_number)


@router.message(UserStates.referral_waiting_phone_number)
async def process_referral_phone(message: Message, state: FSMContext):
    """Обработчик ввода номера телефона"""
    tg_id = message.from_user.id
    phone = message.text.strip()

    # Базовая валидация номера телефона
    if not phone.startswith('+') or len(phone) < 10:
        await message.answer("❌ Введите корректный номер телефона")
        return

    data = await state.get_data()
    amount = data['withdrawal_amount']
    bank_name = data['bank_name']

    # Создаём запрос на вывод (используем функцию из database)
    await db.create_referral_withdrawal_request(
        tg_id, amount, 'bank',
        bank_name=bank_name,
        phone_number=phone
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral", style="danger")]
    ])

    text = (
        f"✅ <b>Запрос на вывод принят!</b>\n\n"
        f"💰 <b>Сумма:</b> {amount:.2f} ₽\n"
        f"🏦 <b>Банк:</b> {bank_name}\n"
        f"📱 <b>Телефон:</b> {phone}\n\n"
        f"Администратор скоро свяжется с вами для подтверждения вывода."
    )

    await send_text_with_photo(message, text, kb, "Запрос на вывод")

    # Отправляем уведомление администратору
    admin_text = (
        f"🏦 <b>Новый запрос на вывод реферальных средств (Банк)</b>\n\n"
        f"👤 <b>Пользователь:</b> @{message.from_user.username or 'unknown'}\n"
        f"🆔 <b>ID:</b> <code>{tg_id}</code>\n"
        f"💰 <b>Сумма:</b> {amount:.2f} ₽\n"
        f"🏦 <b>Банк:</b> {bank_name}\n"
        f"📱 <b>Телефон:</b> {phone}"
    )

    try:
        await message.bot.send_message(ADMIN_ID, admin_text)
    except Exception as e:
        logging.error(f"Failed to send withdrawal notification to admin: {e}")

    await state.clear()


# ────────────────────────────────────────────────
#         WITHDRAWAL FLOWS: SBP CARD
# ────────────────────────────────────────────────

@router.callback_query(F.data == "referral_withdraw_card")
async def process_referral_withdraw_card_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода на карту по СБП"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started card withdrawal")

    # Проверяем баланс
    balance = await db.get_referral_withdrawal_balance(tg_id)

    if balance < 5000:
        await callback.answer("❌ Баланс меньше минимальной суммы вывода (5000 ₽)", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
    ])

    text = "💳 <b>Вывод на карту по СБП</b>\n\n✅ Введите сумму вывода (минимум 5000 ₽):"

    await send_text_with_photo(callback.message, text, kb, "Введите сумму вывода")
    await state.set_state(UserStates.referral_waiting_withdraw_amount)


# ────────────────────────────────────────────────
#         WITHDRAWAL FLOWS: USDT
# ────────────────────────────────────────────────

@router.callback_query(F.data == "referral_withdraw_usdt")
async def process_referral_withdraw_usdt_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода в USDT"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started USDT withdrawal")

    # Проверяем баланс
    balance = await db.get_referral_withdrawal_balance(tg_id)

    if balance < 5000:
        await callback.answer("❌ Баланс меньше минимальной суммы вывода (5000 ₽)", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
    ])

    text = "💎 <b>Вывод в USDT</b>\n\n✅ Введите сумму вывода (минимум 5000 ₽):"

    await send_text_with_photo(callback.message, text, kb, "Введите сумму вывода")
    await state.set_state(UserStates.referral_waiting_withdraw_amount)


@router.message(UserStates.referral_waiting_withdraw_amount)
async def process_referral_usdt_amount(message: Message, state: FSMContext):
    """Обработчик ввода суммы для вывода в USDT"""
    tg_id = message.from_user.id

    try:
        amount = float(message.text)
        if amount < 5000:
            await message.answer("❌ Сумма должна быть не менее 5000 ₽")
            return

        # Проверяем баланс
        balance = await db.get_referral_withdrawal_balance(tg_id)
        if amount > balance:
            await message.answer(f"❌ Невозможно вывести больше чем есть на балансе ({balance:.2f} ₽)")
            return

        # Проверяем в каком состоянии сейчас пользователь (для USDT или для карты)
        current_state = await state.get_state()

        if current_state == UserStates.referral_waiting_withdraw_amount.state:
            await state.update_data(withdrawal_amount=amount)

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
            ])

            text = f"💎 <b>Введите адрес USDT кошелька</b>\n\n✅ Вы хотите вывести: <b>{amount:.2f} ₽</b>\n\nВведите адрес вашего USDT кошелька (TRC-20 или ERC-20):"

            await send_text_with_photo(message, text, kb, "Введите адрес кошелька")
            await state.set_state(UserStates.referral_waiting_usdt_address)

    except ValueError:
        await message.answer("❌ Введите корректную сумму")


@router.message(UserStates.referral_waiting_usdt_address)
async def process_referral_usdt_address(message: Message, state: FSMContext):
    """Обработчик ввода адреса USDT"""
    tg_id = message.from_user.id
    address = message.text.strip()

    # Базовая валидация адреса
    if len(address) < 20:
        await message.answer("❌ Введите корректный адрес кошелька")
        return

    data = await state.get_data()
    amount = data['withdrawal_amount']

    # Создаём запрос на вывод
    await db.create_referral_withdrawal_request(
        tg_id, amount, 'usdt',
        usdt_address=address
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral", style="danger")]
    ])

    text = (
        f"✅ <b>Запрос на вывод принят!</b>\n\n"
        f"💰 <b>Сумма:</b> {amount:.2f} ₽\n"
        f"💎 <b>Адрес:</b> <code>{address}</code>\n\n"
        f"Администратор скоро свяжется с вами для подтверждения вывода."
    )

    await send_text_with_photo(message, text, kb, "Запрос на вывод")

    # Отправляем уведомление администратору
    admin_text = (
        f"💎 <b>Новый запрос на вывод реферальных средств (USDT)</b>\n\n"
        f"👤 <b>Пользователь:</b> @{message.from_user.username or 'unknown'}\n"
        f"🆔 <b>ID:</b> <code>{tg_id}</code>\n"
        f"💰 <b>Сумма:</b> {amount:.2f} ₽\n"
        f"💎 <b>Адрес:</b> <code>{address}</code>"
    )

    try:
        await message.bot.send_message(ADMIN_ID, admin_text)
    except Exception as e:
        logging.error(f"Failed to send withdrawal notification to admin: {e}")

    await state.clear()
