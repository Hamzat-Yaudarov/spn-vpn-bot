import logging
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from config import (
    PARTNERSHIP_AGREEMENT_15, PARTNERSHIP_AGREEMENT_20, PARTNERSHIP_AGREEMENT_25, PARTNERSHIP_AGREEMENT_30,
    ADMIN_ID
)
import database as db
from states import UserStates


logger = logging.getLogger(__name__)
router = Router()


class PartnershipStates(StatesGroup):
    """Состояния для партнёрства"""
    awaiting_withdrawal_amount = State()
    awaiting_bank_name = State()
    awaiting_usdt_address = State()


def get_agreement_url(percent: int) -> str:
    """Получить URL соглашения по проценту"""
    if percent == 15:
        return PARTNERSHIP_AGREEMENT_15
    elif percent == 20:
        return PARTNERSHIP_AGREEMENT_20
    elif percent == 25:
        return PARTNERSHIP_AGREEMENT_25
    elif percent == 30:
        return PARTNERSHIP_AGREEMENT_30
    return ""


def get_percent_label(percent: int) -> str:
    """Получить описание для процента"""
    return {
        15: "Соглашение для партнёров с 15% доходом",
        20: "Соглашение для партнёров с 20% доходом",
        25: "Соглашение для партнёров с 25% доходом",
        30: "Соглашение для партнёров с 30% доходом",
    }.get(percent, "Неизвестный процент")


@router.callback_query(F.data == "partnership")
async def show_partnership_menu(callback: CallbackQuery, state: FSMContext):
    """Показать меню партнёрства или соглашение если это первый раз"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} clicked partnership button")

    partner_info = await db.get_partner_info(tg_id)

    if not partner_info:
        await callback.answer("❌ Вы не являетесь партнёром", show_alert=True)
        return

    # Если соглашение ещё не принято, показываем его
    if not partner_info['partnership_accepted']:
        percent = partner_info['partnership_percent']
        agreement_url = get_agreement_url(percent)
        percent_label = get_percent_label(percent)

        text = (
            f"📋 <b>Партнёрское соглашение</b>\n\n"
            f"{percent_label}\n\n"
            f"Внимание! Перед началом работы необходимо ознакомиться и принять соглашение.\n\n"
            f"💰 <b>Ваша доля:</b> {percent}% от каждого платежа\n"
            f"📅 <b>Срок партнёрства:</b> до {partner_info['partnership_until'].strftime('%d.%m.%Y')}\n\n"
            f"<i>Нажмите кнопку ниже, чтобы прочитать и принять соглашение</i>"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📄 Прочитать соглашение", url=agreement_url)],
            [InlineKeyboardButton(text="✅ Я принимаю соглашение", callback_data="accept_partnership")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])

        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception as e:
            logger.warning(f"Could not edit message: {e}, sending new message instead")
            await callback.message.answer(text, reply_markup=kb)
        logger.info(f"User {tg_id} shown partnership agreement")
    else:
        # Показываем личный кабинет
        await show_partner_cabinet(callback, tg_id, state)


@router.callback_query(F.data == "accept_partnership")
async def accept_partnership(callback: CallbackQuery):
    """Принять партнёрское соглашение"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} accepted partnership agreement")

    await db.accept_partnership_agreement(tg_id)

    # Генерируем партнёрскую ссылку
    partner_link = f"https://t.me/WaySPN_robot?start=partner_{tg_id}"

    text = (
        "✅ <b>Соглашение принято!</b>\n\n"
        "🎉 Добро пожаловать в программу партнёрства SPN VPN!\n\n"
        f"<b>Ваша партнёрская ссылка:</b>\n"
        f"<code>{partner_link}</code>\n\n"
        "<i>Делитесь этой ссылкой, и получайте комиссию от каждого платежа приведённых вами пользователей!</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤝 Открыть личный кабинет", callback_data="show_partner_cabinet")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        logger.warning(f"Could not edit message: {e}, sending new message instead")
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "show_partner_cabinet")
async def show_partner_cabinet_callback(callback: CallbackQuery, state: FSMContext):
    """Callback для открытия личного кабинета"""
    tg_id = callback.from_user.id
    await show_partner_cabinet(callback, tg_id, state)


async def show_partner_cabinet(callback_or_message, tg_id: int, state: FSMContext):
    """Показать личный кабинет партнёра"""
    partner_info = await db.get_partner_info(tg_id)

    if not partner_info:
        await callback_or_message.answer("❌ Вы не являетесь партнёром", show_alert=True)
        return

    # Получаем статистику
    stats = await db.get_partnership_stats(tg_id)

    # Рассчитываем оставшееся время
    now = datetime.utcnow()
    partnership_until = partner_info['partnership_until']
    time_left = partnership_until - now

    if time_left.total_seconds() > 0:
        days_left = time_left.days
        hours_left = time_left.seconds // 3600
        time_str = f"{days_left} дн. {hours_left} ч."
    else:
        time_str = "⚠️ Истекло"

    # Генерируем партнёрскую ссылку
    partner_link = f"https://t.me/WaySPN_robot?start=partner_{tg_id}"

    text = (
        f"🤝 <b>Личный кабинет партнёра</b>\n\n"
        f"<b>📊 Статистика:</b>\n"
        f"👥 Всего привлечено пользователей: <b>{stats['total_users']}</b>\n"
        f"💰 % от каждого платежа: <b>{partner_info['partnership_percent']}%</b>\n\n"
        f"<b>📈 Покупки приведённых пользователей:</b>\n"
        f"• 1 месяц: <b>{stats['purchases_1m']}</b> покупок\n"
        f"• 3 месяца: <b>{stats['purchases_3m']}</b> покупок\n"
        f"• 6 месяцев: <b>{stats['purchases_6m']}</b> покупок\n"
        f"• 1 год: <b>{stats['purchases_12m']}</b> покупок\n\n"
        f"<b>💵 Финансы:</b>\n"
        f"💸 Всего заработано: <b>{float(partner_info['partner_earned_total']):.2f} ₽</b>\n"
        f"📤 Всего выведено: <b>{float(partner_info['partner_withdrawn_total']):.2f} ₽</b>\n"
        f"💳 Текущий баланс: <b>{float(partner_info['partner_balance']):.2f} ₽</b>\n\n"
        f"<b>📅 Статус партнёрства:</b>\n"
        f"⏰ Осталось: <b>{time_str}</b>\n\n"
        f"<b>🔗 Ваша партнёрская ссылка:</b>\n"
        f"<code>{partner_link}</code>"
    )

    # Проверяем может ли партнёр выводить (минимум 5000 рублей)
    can_withdraw = partner_info['partner_balance'] >= 5000

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💳 Вывод на карту по СБП",
            callback_data="withdraw_sbp" if can_withdraw else "withdraw_disabled"
        )],
        [InlineKeyboardButton(
            text="💰 Вывод в USDT",
            callback_data="withdraw_usdt" if can_withdraw else "withdraw_disabled"
        )],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    try:
        if hasattr(callback_or_message, 'message'):
            await callback_or_message.message.edit_text(text, reply_markup=kb)
        else:
            await callback_or_message.edit_text(text, reply_markup=kb)
    except Exception as e:
        logger.warning(f"Could not edit message: {e}, sending new message instead")
        if hasattr(callback_or_message, 'message'):
            # It's a CallbackQuery
            await callback_or_message.message.answer(text, reply_markup=kb)
        else:
            # Shouldn't happen, but fallback
            await callback_or_message.answer(text, reply_markup=kb)

    await state.clear()
    logger.info(f"User {tg_id} opened partner cabinet")


@router.callback_query(F.data == "withdraw_disabled")
async def withdraw_disabled(callback: CallbackQuery):
    """Вывод отключен (недостаточно средств)"""
    balance = await db.get_user(callback.from_user.id)
    await callback.answer(
        f"❌ Минимальная сумма для вывода: 5000 ₽\n"
        f"Ваш баланс: {float(balance['partner_balance']):.2f} ₽",
        show_alert=True
    )


@router.callback_query(F.data == "withdraw_sbp")
async def withdraw_sbp_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода на СБП"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} started SBP withdrawal")

    user = await db.get_user(tg_id)

    text = (
        f"💳 <b>Вывод на карту по СБП</b>\n\n"
        f"💰 Доступный баланс: <b>{float(user['partner_balance']):.2f} ₽</b>\n\n"
        f"<i>Введите сумму вывода (минимум 5000 ₽):</i>"
    )

    try:
        await callback.message.edit_text(text)
    except Exception as e:
        logger.warning(f"Could not edit message: {e}, sending new message instead")
        await callback.message.answer(text)

    await state.set_state(PartnershipStates.awaiting_withdrawal_amount)
    state_data = await state.get_data()
    state_data['withdrawal_type'] = 'sbp'
    await state.update_data(state_data)


@router.callback_query(F.data == "withdraw_usdt")
async def withdraw_usdt_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода в USDT"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} started USDT withdrawal")

    user = await db.get_user(tg_id)

    text = (
        f"💰 <b>Вывод в USDT</b>\n\n"
        f"💵 Доступный баланс: <b>{float(user['partner_balance']):.2f} ₽</b>\n\n"
        f"<i>Введите сумму вывода (минимум 5000 ₽):</i>"
    )

    try:
        await callback.message.edit_text(text)
    except Exception as e:
        logger.warning(f"Could not edit message: {e}, sending new message instead")
        await callback.message.answer(text)

    await state.set_state(PartnershipStates.awaiting_withdrawal_amount)
    state_data = await state.get_data()
    state_data['withdrawal_type'] = 'usdt'
    await state.update_data(state_data)


@router.message(PartnershipStates.awaiting_withdrawal_amount)
async def process_withdrawal_amount(message: Message, state: FSMContext):
    """Обработать ввод суммы вывода"""
    tg_id = message.from_user.id
    
    try:
        amount = float(message.text)
        
        if amount < 5000:
            await message.answer("❌ Минимальная сумма вывода: 5000 ₽")
            return
        
        user = await db.get_user(tg_id)
        if amount > float(user['partner_balance']):
            await message.answer(f"❌ Недостаточно средств. Баланс: {float(user['partner_balance']):.2f} ₽")
            return
        
        state_data = await state.get_data()
        withdrawal_type = state_data.get('withdrawal_type')
        
        if withdrawal_type == 'sbp':
            text = (
                f"💳 <b>Вывод на карту по СБП</b>\n\n"
                f"Сумма вывода: <b>{amount:.2f} ₽</b>\n\n"
                f"<i>Укажите название вашего банка (например: Сбербанк, ВТБ, Альфа-Банк):</i>"
            )
            await state.set_state(PartnershipStates.awaiting_bank_name)
        else:  # usdt
            text = (
                f"💰 <b>Вывод в USDT</b>\n\n"
                f"Сумма вывода: <b>{amount:.2f} ₽</b>\n\n"
                f"<i>Введите адрес вашего USDT кошелька:</i>"
            )
            await state.set_state(PartnershipStates.awaiting_usdt_address)
        
        await message.answer(text)
        state_data['amount'] = amount
        await state.update_data(state_data)
        logger.info(f"User {tg_id} entered withdrawal amount: {amount}")
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректную сумму")


@router.message(PartnershipStates.awaiting_bank_name)
async def process_bank_name(message: Message, state: FSMContext):
    """Обработать ввод названия банка"""
    tg_id = message.from_user.id
    bank_name = message.text.strip()
    
    if len(bank_name) < 2:
        await message.answer("❌ Пожалуйста, введите корректное название банка")
        return
    
    state_data = await state.get_data()
    amount = state_data.get('amount')
    
    # Создаём запрос на вывод
    await db.create_withdrawal_request(tg_id, amount, 'sbp', bank_name=bank_name)
    
    text = (
        f"✅ <b>Запрос на вывод отправлен!</b>\n\n"
        f"💳 Способ: Карта по СБП\n"
        f"💰 Сумма: {amount:.2f} ₽\n"
        f"🏦 Банк: {bank_name}\n\n"
        f"Администратор обработает ваш запрос в течение 24 часов."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤝 Вернуться в кабинет", callback_data="show_partner_cabinet")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")]
    ])
    
    await message.answer(text, reply_markup=kb)
    
    # Уведомляем администратора
    user = await db.get_user(tg_id)
    admin_text = (
        f"💳 <b>Новый запрос на вывод СБП</b>\n\n"
        f"👤 Пользователь: <code>{tg_id}</code>\n"
        f"📝 Юзернейм: @{user.get('username', 'N/A')}\n"
        f"💰 Сумма: {amount:.2f} ₽\n"
        f"🏦 Банк: {bank_name}\n\n"
        f"⏱ Время запроса: {datetime.utcnow().strftime('%d.%m.%Y %H:%M:%S UTC')}"
    )
    
    try:
        await message.bot.send_message(ADMIN_ID, admin_text)
        logger.info(f"Admin notified about withdrawal request from user {tg_id}")
    except Exception as e:
        logger.error(f"Failed to notify admin: {e}")
    
    await state.clear()
    logger.info(f"User {tg_id} created SBP withdrawal request: {amount} ₽ to {bank_name}")


@router.message(PartnershipStates.awaiting_usdt_address)
async def process_usdt_address(message: Message, state: FSMContext):
    """Обработать ввод USDT адреса"""
    tg_id = message.from_user.id
    usdt_address = message.text.strip()
    
    # Базовая валидация USDT адреса (TRC-20 начинается с T)
    if not usdt_address.startswith('T') or len(usdt_address) != 34:
        await message.answer("❌ Пожалуйста, введите корректный USDT адрес")
        return
    
    state_data = await state.get_data()
    amount = state_data.get('amount')
    
    # Создаём запрос на вывод
    await db.create_withdrawal_request(tg_id, amount, 'usdt', usdt_address=usdt_address)
    
    text = (
        f"✅ <b>Запрос на вывод отправлен!</b>\n\n"
        f"💰 Способ: USDT\n"
        f"💵 Сумма: {amount:.2f} ₽\n"
        f"🔗 Адрес: <code>{usdt_address}</code>\n\n"
        f"Администратор обработает ваш запрос в течение 24 часов."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤝 Вернуться в кабинет", callback_data="show_partner_cabinet")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")]
    ])
    
    await message.answer(text, reply_markup=kb)
    
    # Уведомляем администратора
    user = await db.get_user(tg_id)
    admin_text = (
        f"💰 <b>Новый запрос на вывод USDT</b>\n\n"
        f"👤 Пользователь: <code>{tg_id}</code>\n"
        f"📝 Юзернейм: @{user.get('username', 'N/A')}\n"
        f"💵 Сумма: {amount:.2f} ₽\n"
        f"🔗 Адрес: <code>{usdt_address}</code>\n\n"
        f"⏱ Время запроса: {datetime.utcnow().strftime('%d.%m.%Y %H:%M:%S UTC')}"
    )
    
    try:
        await message.bot.send_message(ADMIN_ID, admin_text)
        logger.info(f"Admin notified about USDT withdrawal request from user {tg_id}")
    except Exception as e:
        logger.error(f"Failed to notify admin: {e}")
    
    await state.clear()
    logger.info(f"User {tg_id} created USDT withdrawal request: {amount} ₽ to {usdt_address}")
