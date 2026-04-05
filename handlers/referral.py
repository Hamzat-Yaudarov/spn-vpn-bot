import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
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

    # Получаем полную статистику рефералов
    stats = await db.get_referral_stats(tg_id)

    # Формируем информацию по тарифам
    tariffs_info = ""
    if stats['earnings_by_tariff']:
        for earning in stats['earnings_by_tariff']:
            tariff_code = earning['tariff_code']
            purchase_count = earning['purchase_count'] or 0
            tariffs_info += f"• {tariff_code}: {purchase_count}\n"
    else:
        tariffs_info = "• 1 месяц: \n• 3 месяца: \n• 6 месяцев: \n• 12 месяцев: \n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Запросить вывод", callback_data="referral_withdraw")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
    ])

    text = (
        "<b>💰 Реферальная программа</b>\n"
        "Приглашайте друзей и зарабатывайте на их покупках:\n\n"
        "<blockquote>"
        "• <b>35%</b> от первой покупки реферала\n"
        "• <b>15%</b> от повторных покупок\n"
        "</blockquote>\n\n"
        f"👥 <b>Всего активных друзей:</b> {stats['active_referrals']}\n\n"
        "<b>📊 Покупки по тарифам:</b>\n"
        f"{tariffs_info}\n"
        f"<b>💰 Всего заработано:</b> {stats['total_earned']:.2f} ₽\n"
        f"<b>💸 Всего выведено/потрачено:</b> {stats['total_withdrawn']:.2f} ₽\n"
        f"<b>🪙 Текущий баланс:</b> {stats['current_balance']:.2f} ₽\n\n"
        "<blockquote>"
        "ℹ️ <b>Минимальная сумма вывода:</b> 5000 ₽\n"
        "</blockquote>\n\n"
        "<b>🔗 Ваша реферальная ссылка:</b>\n"
        f"<code>{referral_link}</code>\n\n"
        "💡 <i>Нажми на ссылку чтобы скопировать её</i>"
    )

    await edit_text_with_photo(callback, text, kb, "Реферальная программа")


# ────────────────────────────────────────────────
#            WITHDRAWAL FLOWS: SBP
# ────────────────────────────────────────────────

@router.callback_query(F.data == "referral_withdraw")
async def process_referral_withdraw_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода денег для реферала"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started referral withdrawal")

    # Получаем статистику
    stats = await db.get_referral_stats(tg_id)

    if stats['current_balance'] < 5000:
        await callback.answer("❌ Баланс меньше минимальной суммы вывода (5000 ₽)", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏦 Вывод на карту по СБП", callback_data="referral_withdraw_sbp")],
        [InlineKeyboardButton(text="💎 Вывод в USDT", callback_data="referral_withdraw_usdt")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral", style="danger")]
    ])

    text = (
        "<b>💰 Выбор способа вывода</b>\n\n"
        "Выберите удобный для вас способ получения денег:"
    )

    await send_text_with_photo(callback.message, text, kb, "Выбор способа вывода")


@router.callback_query(F.data == "referral_withdraw_sbp")
async def process_referral_withdraw_sbp_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода на карту по СБП"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started SBP withdrawal")

    # Проверяем баланс
    stats = await db.get_referral_stats(tg_id)

    if stats['current_balance'] < 5000:
        await callback.answer("❌ Баланс меньше минимальной суммы вывода (5000 ₽)", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
    ])

    text = "💳 <b>Вывод на карту по СБП</b>\n\n✅ Введите сумму вывода (минимум 5000 ₽):"

    await send_text_with_photo(callback.message, text, kb, "Введите сумму вывода")
    await state.set_state(UserStates.referral_waiting_sbp_amount)


@router.message(UserStates.referral_waiting_sbp_amount)
async def process_referral_sbp_amount(message: Message, state: FSMContext):
    """Обработчик ввода суммы для вывода"""
    tg_id = message.from_user.id

    try:
        amount = float(message.text)
        if amount < 5000:
            await message.answer("❌ Сумма должна быть не менее 5000 ₽")
            return

        # Проверяем баланс
        stats = await db.get_referral_stats(tg_id)
        if amount > stats['current_balance']:
            await message.answer(f"❌ Невозможно вывести больше чем есть на балансе ({stats['current_balance']:.2f} ₽)")
            return

        await state.update_data(withdrawal_amount=amount)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
        ])

        text = f"🏦 <b>Укажите банк</b>\n\n✅ Вы хотите вывести: <b>{amount:.2f} ₽</b>\n\nВведите название вашего банка:"

        await send_text_with_photo(message, text, kb, "Укажите банк")
        await state.set_state(UserStates.referral_waiting_sbp_bank)

    except ValueError:
        await message.answer("❌ Введите корректную сумму")


@router.message(UserStates.referral_waiting_sbp_bank)
async def process_referral_sbp_bank(message: Message, state: FSMContext):
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
    await state.set_state(UserStates.referral_waiting_sbp_phone)


@router.message(UserStates.referral_waiting_sbp_phone)
async def process_referral_sbp_phone(message: Message, state: FSMContext):
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

    # Создаём запрос на вывод
    await db.create_referral_withdrawal_request(
        tg_id, amount, 'sbp',
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
        f"💳 <b>Новый запрос на вывод средств от реферала (СБП)</b>\n\n"
        f"👤 <b>Пользователь:</b> @{message.from_user.username or 'unknown'}\n"
        f"🆔 <b>ID:</b> <code>{tg_id}</code>\n"
        f"💰 <b>Сумма:</b> {amount:.2f} ₽\n"
        f"🏦 <b>Банк:</b> {bank_name}\n"
        f"📱 <b>Телефон:</b> {phone}"
    )

    try:
        from config import ADMIN_ID
        await message.bot.send_message(ADMIN_ID, admin_text)
    except Exception as e:
        logging.error(f"Failed to send withdrawal notification to admin: {e}")

    await state.clear()


# ────────────────────────────────────────────────
#            WITHDRAWAL FLOWS: USDT
# ────────────────────────────────────────────────

@router.callback_query(F.data == "referral_withdraw_usdt")
async def process_referral_withdraw_usdt_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода в USDT"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started USDT withdrawal")

    # Проверяем баланс
    stats = await db.get_referral_stats(tg_id)

    if stats['current_balance'] < 5000:
        await callback.answer("❌ Баланс меньше минимальной суммы вывода (5000 ₽)", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="referral_withdraw", style="danger")]
    ])

    text = "💎 <b>Вывод в USDT</b>\n\n✅ Введите сумму вывода (минимум 5000 ₽):"

    await send_text_with_photo(callback.message, text, kb, "Введите сумму вывода")
    await state.set_state(UserStates.referral_waiting_usdt_amount)


@router.message(UserStates.referral_waiting_usdt_amount)
async def process_referral_usdt_amount(message: Message, state: FSMContext):
    """Обработчик ввода суммы для вывода в USDT"""
    tg_id = message.from_user.id

    try:
        amount = float(message.text)
        if amount < 5000:
            await message.answer("❌ Сумма должна быть не менее 5000 ₽")
            return

        # Проверяем баланс
        stats = await db.get_referral_stats(tg_id)
        if amount > stats['current_balance']:
            await message.answer(f"❌ Невозможно вывести больше чем есть на балансе ({stats['current_balance']:.2f} ₽)")
            return

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
        f"💎 <b>Новый запрос на вывод средств от реферала (USDT)</b>\n\n"
        f"👤 <b>Пользователь:</b> @{message.from_user.username or 'unknown'}\n"
        f"🆔 <b>ID:</b> <code>{tg_id}</code>\n"
        f"💰 <b>Сумма:</b> {amount:.2f} ₽\n"
        f"💎 <b>Адрес:</b> <code>{address}</code>"
    )

    try:
        from config import ADMIN_ID
        await message.bot.send_message(ADMIN_ID, admin_text)
    except Exception as e:
        logging.error(f"Failed to send withdrawal notification to admin: {e}")

    await state.clear()
