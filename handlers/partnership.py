import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from states import UserStates
import database as db
from services.image_handler import edit_text_with_photo
from config import ADMIN_ID

logger = logging.getLogger(__name__)

router = Router()

# Тексты соглашений по процентам
AGREEMENTS = {
    15: "Соглашение для партнёров с 15% доходом от всех транзакций приведённых ими пользователей",
    20: "Соглашение для партнёров с 20% доходом от всех транзакций приведённых ими пользователей",
    25: "Соглашение для партнёров с 25% доходом от всех транзакций приведённых ими пользователей",
    30: "Соглашение для партнёров с 30% доходом от всех транзакций приведённых ими пользователей",
}


@router.callback_query(F.data == "partnership")
async def process_partnership(callback: CallbackQuery, state: FSMContext):
    """Показать меню партнёрства"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} viewing partnership menu")

    partnership = await db.get_partnership(tg_id)
    
    if not partnership:
        await callback.answer("❌ Вы не включены в партнёрскую программу", show_alert=True)
        return

    # Проверяем принял ли пользователь соглашение
    if not partnership.get('agreement_accepted', False):
        # Первый клик - показываем соглашение
        percentage = partnership.get('percentage')
        agreement_text = AGREEMENTS.get(percentage, "Соглашение партнёра")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять соглашение", callback_data="accept_partnership_agreement")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])

        text = (
            f"<b>Внимание! У нас {len(AGREEMENTS)} вида соглашения:</b>\n\n"
            f"<b>Ваше соглашение:</b>\n"
            f"{agreement_text}\n\n"
            f"<blockquote>"
            f"Прочитав соглашение, нажмите <b>«Принять соглашение»</b> для продолжения."
            f"</blockquote>"
        )

        await edit_text_with_photo(callback, text, kb, "Соглашение партнёра")
        await state.set_state(UserStates.waiting_partnership_agreement_response)
    else:
        # Уже принял соглашение - показываем личный кабинет
        await show_partnership_cabinet(callback, state)


@router.callback_query(F.data == "accept_partnership_agreement", UserStates.waiting_partnership_agreement_response)
async def process_accept_partnership_agreement(callback: CallbackQuery, state: FSMContext):
    """Обработчик принятия соглашения партнёра"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} accepted partnership agreement")

    await db.accept_partnership_agreement(tg_id)
    await callback.answer("✅ Соглашение принято!", show_alert=False)
    
    # Показываем личный кабинет
    await show_partnership_cabinet(callback, state)


async def show_partnership_cabinet(callback: CallbackQuery, state: FSMContext):
    """Показать личный кабинет партнёра"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} viewing partnership cabinet")

    partnership = await db.get_partnership(tg_id)
    if not partnership:
        await callback.answer("❌ Партнёрство не найдено", show_alert=True)
        return

    percentage = partnership.get('percentage')
    partner_link_id = partnership.get('partner_link_id')

    # Получаем статистику
    stats = await db.get_partnership_stats(tg_id)
    balance = await db.get_partnership_balance(tg_id)

    total_users = stats['total_users'] if stats else 0
    one_month = stats['one_month_count'] if stats else 0
    three_month = stats['three_month_count'] if stats else 0
    six_month = stats['six_month_count'] if stats else 0
    one_year = stats['one_year_count'] if stats else 0
    total_earned = float(stats['total_earned']) if stats else 0.0
    total_withdrawn = float(stats['total_withdrawn']) if stats else 0.0

    # Получаем партнёрскую ссылку
    bot_username = (await callback.bot.get_me()).username
    partnership_link = f"https://t.me/{bot_username}?start=partner_{partner_link_id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Вывод на карту по СБП", callback_data="withdraw_sbp")],
        [InlineKeyboardButton(text="💰 Вывод в USDT", callback_data="withdraw_usdt")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>💼 Личный кабинет партнёра</b>\n\n"
        
        f"<b>📊 Ваш процент:</b> {percentage}%\n\n"
        
        f"<b>🔗 Партнёрская ссылка:</b>\n"
        f"<code>{partnership_link}</code>\n\n"
        
        f"<b>📈 Статистика:</b>\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"💳 Покупок на 1 месяц: <b>{one_month}</b>\n"
        f"💳 Покупок на 3 месяца: <b>{three_month}</b>\n"
        f"💳 Покупок на 6 месяцев: <b>{six_month}</b>\n"
        f"💳 Покупок на 1 год: <b>{one_year}</b>\n\n"
        
        f"<b>💵 Финансы:</b>\n"
        f"💰 Всего заработано: <b>{total_earned:.2f}₽</b>\n"
        f"💸 Всего выведено: <b>{total_withdrawn:.2f}₽</b>\n"
        f"📊 Текущий баланс: <b>{balance:.2f}₽</b>\n\n"
        
        f"<blockquote>"
        f"Минимальная сумма вывода: <b>5000₽</b>"
        f"</blockquote>"
    )

    await edit_text_with_photo(callback, text, kb, "Личный кабинет партнёра")
    await state.set_state(UserStates.viewing_partnership)


@router.callback_query(F.data == "withdraw_sbp", UserStates.viewing_partnership)
async def process_withdraw_sbp(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода по СБП"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} started SBP withdrawal")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership")]
    ])

    text = "💳 <b>Вывод на карту по СБП</b>\n\n" \
           "Введите сумму вывода (минимум 5000₽):\n\n" \
           "<i>Отправьте число в ответ на это сообщение</i>"

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.entering_withdrawal_amount)
    await state.update_data(withdrawal_method="sbp")


@router.callback_query(F.data == "withdraw_usdt", UserStates.viewing_partnership)
async def process_withdraw_usdt(callback: CallbackQuery, state: FSMContext):
    """Начать процесс вывода в USDT"""
    tg_id = callback.from_user.id
    logger.info(f"User {tg_id} started USDT withdrawal")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="partnership")]
    ])

    text = "💰 <b>Вывод в USDT</b>\n\n" \
           "Введите сумму вывода (минимум 5000₽):\n\n" \
           "<i>Отправьте число в ответ на это сообщение</i>"

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.entering_withdrawal_amount)
    await state.update_data(withdrawal_method="usdt")


@router.message(UserStates.entering_withdrawal_amount)
async def process_withdrawal_amount(message: Message, state: FSMContext):
    """Обработать введённую сумму вывода"""
    tg_id = message.from_user.id
    
    try:
        amount = float(message.text)
        
        if amount < 5000:
            await message.answer("❌ Минимальная сумма вывода: 5000₽")
            return
        
        # Проверяем баланс
        balance = await db.get_partnership_balance(tg_id)
        if balance < amount:
            await message.answer(f"❌ Недостаточно средств. Ваш баланс: {balance:.2f}₽")
            return
        
        # Сохраняем сумму в состояние
        await state.update_data(withdrawal_amount=amount)
        
        # Получаем метод вывода
        data = await state.get_data()
        method = data.get('withdrawal_method')
        
        if method == "sbp":
            # Следующий шаг - запрос банка
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Отменить", callback_data="partnership")]
            ])
            
            text = "🏦 <b>Укажите банк</b>\n\n" \
                   "Введите название вашего банка (например: Сбербанк, Яндекс.Касса, Альфа-Банк):\n\n" \
                   "<i>Отправьте название в ответ на это сообщение</i>"
            
            await message.answer(text, reply_markup=kb)
            await state.set_state(UserStates.entering_bank_name)
        else:
            # USDT - переходим к вводу адреса кошелька
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Отменить", callback_data="partnership")]
            ])
            
            text = "📬 <b>Укажите адрес USDT кошелька</b>\n\n" \
                   "Введите адрес вашего кошелька (TRC20, ERC20 или BEP20):\n\n" \
                   "<i>Отправьте адрес в ответ на это сообщение</i>"
            
            await message.answer(text, reply_markup=kb)
            await state.set_state(UserStates.entering_wallet_address)
    
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число")


@router.message(UserStates.entering_bank_name)
async def process_bank_name(message: Message, state: FSMContext):
    """Обработать введённое название банка"""
    tg_id = message.from_user.id
    bank_name = message.text.strip()
    
    if len(bank_name) < 2:
        await message.answer("❌ Пожалуйста, введите корректное название банка")
        return
    
    await state.update_data(bank_name=bank_name)
    
    # Следующий шаг - запрос номера телефона
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отменить", callback_data="partnership")]
    ])
    
    text = "📱 <b>Укажите номер телефона</b>\n\n" \
           "Введите номер телефона, к которому привязана карта (в формате +7XXXXXXXXXX):\n\n" \
           "<i>Отправьте номер в ответ на это сообщение</i>"
    
    await message.answer(text, reply_markup=kb)
    await state.set_state(UserStates.entering_phone_number)


@router.message(UserStates.entering_phone_number)
async def process_phone_number(message: Message, state: FSMContext):
    """Обработать введённый номер телефона"""
    tg_id = message.from_user.id
    phone = message.text.strip()
    
    # Базовая валидация номера
    if not phone.replace('+', '').replace(' ', '').replace('-', '').isdigit() or len(phone) < 11:
        await message.answer("❌ Пожалуйста, введите корректный номер телефона")
        return
    
    # Сохраняем и создаём запрос на вывод
    await state.update_data(phone_number=phone)
    data = await state.get_data()
    
    amount = data.get('withdrawal_amount')
    method = data.get('withdrawal_method')
    bank_name = data.get('bank_name')
    
    try:
        await db.create_withdrawal_request(
            tg_id,
            amount,
            method,
            bank_name=bank_name,
            phone_number=phone
        )
        
        # Показываем подтверждение
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В личный кабинет", callback_data="partnership")]
        ])
        
        text = (
            "✅ <b>Запрос на вывод отправлен успешно!</b>\n\n"
            f"<b>Сумма:</b> {amount:.2f}₽\n"
            f"<b>Метод:</b> Вывод на карту по СБП\n"
            f"<b>Банк:</b> {bank_name}\n"
            f"<b>Телефон:</b> {phone}\n\n"
            f"<blockquote>"
            f"Администратор обработает ваш запрос в ближайшее время."
            f"</blockquote>"
        )
        
        await message.answer(text, reply_markup=kb)
        
        # Отправляем уведомление админу
        await send_withdrawal_notification_to_admin(message.bot, tg_id, message.from_user.username, amount, method, bank_name=bank_name, phone_number=phone)
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Error creating withdrawal request for user {tg_id}: {e}")
        await message.answer(f"❌ Ошибка при создании запроса: {str(e)[:100]}")


@router.message(UserStates.entering_wallet_address)
async def process_wallet_address(message: Message, state: FSMContext):
    """Обработать введённый адрес кошелька"""
    tg_id = message.from_user.id
    wallet = message.text.strip()
    
    # Базовая валидация адреса (примерно 34-42 символа для USDT)
    if len(wallet) < 26 or len(wallet) > 66:
        await message.answer("❌ Пожалуйста, введите корректный адрес кошелька")
        return
    
    data = await state.get_data()
    amount = data.get('withdrawal_amount')
    method = data.get('withdrawal_method')
    
    try:
        await db.create_withdrawal_request(
            tg_id,
            amount,
            method,
            wallet_address=wallet
        )
        
        # Показываем подтверждение
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В личный кабинет", callback_data="partnership")]
        ])
        
        text = (
            "✅ <b>Запрос на вывод отправлен успешно!</b>\n\n"
            f"<b>Сумма:</b> {amount:.2f}₽\n"
            f"<b>Метод:</b> Вывод в USDT\n"
            f"<b>Адрес:</b> <code>{wallet}</code>\n\n"
            f"<blockquote>"
            f"Администратор обработает ваш запрос в ближайшее время."
            f"</blockquote>"
        )
        
        await message.answer(text, reply_markup=kb)
        
        # Отправляем уведомление админу
        await send_withdrawal_notification_to_admin(message.bot, tg_id, message.from_user.username, amount, method, wallet_address=wallet)
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Error creating withdrawal request for user {tg_id}: {e}")
        await message.answer(f"❌ Ошибка при создании запроса: {str(e)[:100]}")


async def send_withdrawal_notification_to_admin(bot, partner_tg_id: int, username: str, amount: float, method: str, **kwargs):
    """Отправить уведомление админу о новом запросе на вывод"""
    try:
        text = (
            f"<b>📤 Новый запрос на вывод средств</b>\n\n"
            f"👤 <b>Партнёр:</b> @{username or f'ID {partner_tg_id}'}\n"
            f"🆔 <b>ID:</b> {partner_tg_id}\n"
            f"💰 <b>Сумма:</b> {amount:.2f}₽\n"
            f"📊 <b>Метод:</b> {'Вывод на карту по СБП' if method == 'sbp' else 'Вывод в USDT'}\n"
        )
        
        if method == "sbp":
            text += (
                f"\n<b>Банк:</b> {kwargs.get('bank_name', 'N/A')}\n"
                f"<b>Телефон:</b> {kwargs.get('phone_number', 'N/A')}\n"
            )
        else:
            text += f"\n<b>Адрес кошелька:</b> <code>{kwargs.get('wallet_address', 'N/A')}</code>\n"
        
        await bot.send_message(ADMIN_ID, text)
        logger.info(f"Withdrawal notification sent to admin for user {partner_tg_id}")
    except Exception as e:
        logger.error(f"Failed to send withdrawal notification to admin: {e}")
