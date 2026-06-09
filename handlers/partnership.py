import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import PARTNERSHIP_AGREEMENTS
from states import UserStates
import database as db
from services.image_handler import edit_text_with_photo, send_text_with_photo


logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == "partnership")
async def process_partnership_button(callback: CallbackQuery, state: FSMContext):
    """Обработчик нажатия на кнопку 'Партнёрство'"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: partnership")

    partnership = await db.get_partnership(tg_id)
    if not partnership:
        await callback.answer("❌ Партнёрство не активировано", show_alert=True)
        return

    # Проверяем принял ли партнёр соглашение
    if not partnership['agreement_accepted']:
        # Показываем соглашение
        await show_partnership_agreement(callback, state, tg_id, partnership['percentage'])
    else:
        # Показываем личный кабинет
        await show_partnership_cabinet(callback, tg_id)


async def show_partnership_agreement(callback: CallbackQuery, state: FSMContext, tg_id: int, percentage: int):
    """Показать соглашение партнёрства"""
    agreement_url = PARTNERSHIP_AGREEMENTS.get(percentage)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Прочитать соглашение", url=agreement_url, style="primary")],
        [InlineKeyboardButton(text="✅ Я принимаю соглашение", callback_data="accept_partnership_agreement", style="success")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
    ])

    text = (
        "<b>Партнёрское соглашение</b>\n\n"
        "⚠️ <b>Внимание!</b> Перед началом работы необходимо ознакомиться и принять соглашение:\n\n"
    )

    if percentage == 15:
        text += "📋 <b>Соглашение для партнёров с 15% доходом</b>\n"
        text += "от всех транзакций приведённых ими пользователей\n\n"
    elif percentage == 20:
        text += "📋 <b>Соглашение для партнёров с 20% доходом</b>\n"
        text += "от всех транзакций приведённых ими пользователей\n\n"
    elif percentage == 25:
        text += "📋 <b>Соглашение для партнёров с 25% доходом</b>\n"
        text += "от всех транзакций приведённых ими пользователей\n\n"
    elif percentage == 30:
        text += "📋 <b>Соглашение для партнёров с 30% доходом</b>\n"
        text += "от всех транзакций приведённых ими пользователей\n\n"

    text += "Нажми кнопку выше, чтобы прочитать полный текст соглашения."

    await send_text_with_photo(callback.message, text, kb, "Партнёрское соглашение")
    await state.set_state(UserStates.partnership_viewing_agreement)


@router.callback_query(F.data == "accept_partnership_agreement")
async def process_accept_partnership_agreement(callback: CallbackQuery, state: FSMContext):
    """Обработчик принятия соглашения партнёрства"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} accepted partnership agreement")

    # Отмечаем что соглашение принято
    await db.accept_partnership_agreement(tg_id)

    # Показываем личный кабинет
    await show_partnership_cabinet(callback, tg_id)


async def show_partnership_cabinet(callback: CallbackQuery, tg_id: int):
    """Показать личный кабинет партнёра"""
    partnership = await db.get_partnership(tg_id)
    if not partnership:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    # Получаем статистику
    stats = await db.get_partner_stats(tg_id)

    # Получаем партнёрскую ссылку
    bot_username = (await callback.bot.get_me()).username
    partner_link = f"https://t.me/{bot_username}?start=partner_{tg_id}"

    # Подсчитываем покупки по тарифам
    tariff_counts = {
        '1m': 0,
        '3m': 0,
        '6m': 0,
        '12m': 0
    }

    if stats['earnings_by_tariff']:
        for earning in stats['earnings_by_tariff']:
            tariff_code = earning['tariff_code']
            count = earning['purchase_count']
            if tariff_code in tariff_counts:
                tariff_counts[tariff_code] = count

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Скопировать ссылку", url=partner_link, style="primary")],
        [InlineKeyboardButton(text="🏦 Вывод на карту по СБП", callback_data="partnership_withdraw_sbp", style="success")],
        [InlineKeyboardButton(text="💎 Вывод в USDT", callback_data="partnership_withdraw_usdt", style="success")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
    ])

    text = (
        "<b>👤 Личный кабинет партнёра</b>\n\n"
        "<b>Ваша партнёрская ссылка:</b>\n"
        f"<code>{partner_link}</code>\n\n"
        f"<b>Процент дохода:</b> <b>{stats['percentage']}%</b>\n"
        f"<b>Всего пользователей по ссылке:</b> <b>{stats['total_referrals']}</b>\n\n"
        "<b>📊 Покупки по тарифам:</b>\n"
        f"• 1 месяц: <b>{tariff_counts['1m']}</b>\n"
        f"• 3 месяца: <b>{tariff_counts['3m']}</b>\n"
        f"• 6 месяцев: <b>{tariff_counts['6m']}</b>\n"
        f"• 12 месяцев: <b>{tariff_counts['12m']}</b>\n\n"
        f"<b>💰 Всего заработано:</b> <b>{stats['total_earned']:.2f} ₽</b>\n"
        f"<b>💸 Всего выведено:</b> <b>{stats['total_withdrawn']:.2f} ₽</b>\n"
        f"<b>🪙 Текущий баланс:</b> <b>{stats['current_balance']:.2f} ₽</b>\n\n"
        "<i>Минимальная сумма вывода: 5000 ₽</i>"
    )

    # Если баланс меньше минимума, отключаем кнопки вывода
    if stats['current_balance'] < 5000:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Скопировать ссылку", url=partner_link, style="primary")],
            [InlineKeyboardButton(text="🏦 Вывод на карту по СБП", callback_data="partnership_withdraw_sbp", style="success")],
            [InlineKeyboardButton(text="💎 Вывод в USDT", callback_data="partnership_withdraw_usdt", style="success")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
        ])
        text += "\n\n⚠️ <i>Баланс меньше минимальной суммы вывода (5000 ₽)</i>"

    await send_text_with_photo(callback.message, text, kb, "Личный кабинет партнёра")


# ────────────────────────────────────────────────
#            WITHDRAWAL FLOWS: SBP
# ────────────────────────────────────────────────

@router.callback_query(F.data == "partnership_withdraw_sbp")
async def process_withdraw_sbp_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода на карту по СБП"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started SBP withdrawal")

    # Проверяем баланс
    partnership = await db.get_partnership(tg_id)
    stats = await db.get_partner_stats(tg_id)

    if stats['current_balance'] < 5000:
        await callback.answer("❌ Баланс меньше минимальной суммы вывода (5000 ₽)", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership", style="danger")]
    ])

    text = "💳 <b>Вывод на карту по СБП</b>\n\n✅ Введите сумму вывода (минимум 5000 ₽):"

    await send_text_with_photo(callback.message, text, kb, "Введите сумму вывода")
    await state.set_state(UserStates.partnership_waiting_sbp_amount)


@router.message(UserStates.partnership_waiting_sbp_amount)
async def process_sbp_amount(message: Message, state: FSMContext):
    """Обработчик ввода суммы для вывода"""
    tg_id = message.from_user.id

    try:
        amount = float(message.text)
        if amount < 5000:
            await message.answer("❌ Сумма должна быть не менее 5000 ₽")
            return

        # Проверяем баланс
        stats = await db.get_partner_stats(tg_id)
        if amount > stats['current_balance']:
            await message.answer(f"❌ Невозможно вывести больше чем есть на балансе ({stats['current_balance']:.2f} ₽)")
            return

        await state.update_data(withdrawal_amount=amount)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership", style="danger")]
        ])

        text = f"🏦 <b>Укажите банк</b>\n\n✅ Вы хотите вывести: <b>{amount:.2f} ₽</b>\n\nВведите название вашего банка:"

        await send_text_with_photo(message, text, kb, "Укажите банк")
        await state.set_state(UserStates.partnership_waiting_sbp_bank)

    except ValueError:
        await message.answer("❌ Введите корректную сумму")


@router.message(UserStates.partnership_waiting_sbp_bank)
async def process_sbp_bank(message: Message, state: FSMContext):
    """Обработчик ввода банка"""
    tg_id = message.from_user.id
    bank_name = message.text.strip()

    if len(bank_name) < 2:
        await message.answer("❌ Введите корректное название банка")
        return

    await state.update_data(bank_name=bank_name)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership", style="danger")]
    ])

    text = f"📱 <b>Укажите номер телефона</b>\n\n✅ Введите номер телефона, к которому привязана карта (с кодом страны, например +7XXXXXXXXXX):"

    await send_text_with_photo(message, text, kb, "Укажите номер телефона")
    await state.set_state(UserStates.partnership_waiting_sbp_phone)


@router.message(UserStates.partnership_waiting_sbp_phone)
async def process_sbp_phone(message: Message, state: FSMContext):
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
    await db.create_withdrawal_request(
        tg_id, amount, 'sbp',
        bank_name=bank_name,
        phone_number=phone
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership", style="danger")]
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
        f"💳 <b>Новый запрос на вывод средств (СБП)</b>\n\n"
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

@router.callback_query(F.data == "partnership_withdraw_usdt")
async def process_withdraw_usdt_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода в USDT"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} started USDT withdrawal")

    # Проверяем баланс
    stats = await db.get_partner_stats(tg_id)

    if stats['current_balance'] < 5000:
        await callback.answer("❌ Баланс меньше минимальной суммы вывода (5000 ₽)", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership", style="danger")]
    ])

    text = "💎 <b>Вывод в USDT</b>\n\n✅ Введите сумму вывода (минимум 5000 ₽):"

    await send_text_with_photo(callback.message, text, kb, "Введите сумму вывода")
    await state.set_state(UserStates.partnership_waiting_usdt_amount)


@router.message(UserStates.partnership_waiting_usdt_amount)
async def process_usdt_amount(message: Message, state: FSMContext):
    """Обработчик ввода суммы для вывода в USDT"""
    tg_id = message.from_user.id

    try:
        amount = float(message.text)
        if amount < 5000:
            await message.answer("❌ Сумма должна быть не менее 5000 ₽")
            return

        # Проверяем баланс
        stats = await db.get_partner_stats(tg_id)
        if amount > stats['current_balance']:
            await message.answer(f"❌ Невозможно вывести больше чем есть на балансе ({stats['current_balance']:.2f} ₽)")
            return

        await state.update_data(withdrawal_amount=amount)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership", style="danger")]
        ])

        text = f"💎 <b>Введите адрес USDT кошелька</b>\n\n✅ Вы хотите вывести: <b>{amount:.2f} ₽</b>\n\nВведите адрес вашего USDT кошелька (TRC-20 или ERC-20):"

        await send_text_with_photo(message, text, kb, "Введите адрес кошелька")
        await state.set_state(UserStates.partnership_waiting_usdt_address)

    except ValueError:
        await message.answer("❌ Введите корректную сумму")


@router.message(UserStates.partnership_waiting_usdt_address)
async def process_usdt_address(message: Message, state: FSMContext):
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
    await db.create_withdrawal_request(
        tg_id, amount, 'usdt',
        usdt_address=address
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership", style="danger")]
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
        f"💎 <b>Новый запрос на вывод средств (USDT)</b>\n\n"
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
