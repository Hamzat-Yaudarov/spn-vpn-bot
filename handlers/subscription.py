import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import TARIFFS, VIP_TARIFFS, COMBO_TARIFFS, DEFAULT_SQUAD_UUID, REFERRAL_COMMISSION_PERCENT
from states import UserStates
import database as db
from services.remnawave import remnawave_get_subscription_url, remnawave_get_user_info, remnawave_get_or_create_user, remnawave_add_to_squad
from services.cryptobot import create_cryptobot_invoice, get_invoice_status, process_paid_invoice
from services.yookassa import create_yookassa_payment, get_payment_status, process_paid_yookassa_payment
from services import xui


router = Router()


@router.callback_query(F.data == "buy_subscription")
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext):
    """Показать выбор типа подписки"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: buy_subscription")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Обычная подписка", callback_data="sub_type_regular")],
        [InlineKeyboardButton(text="🛡️ Обход глушилок (VIP)", callback_data="sub_type_vip")],
        [InlineKeyboardButton(text="⭐ Обычная + VIP Комбо", callback_data="sub_type_combo")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Выбери тип подписки:</b>\n\n"
        "<b>📱 Обычная подписка</b>\n"
        "Ускорение интернета и стабильное соединение\n\n"
        "<b>🛡️ Обход глушилок (VIP)</b>\n"
        "Продвинутая защита и обход блокировок\n\n"
        "<b>⭐ Комбо</b>\n"
        "Обе подписки сразу со скидкой"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_subscription_type)


@router.callback_query(F.data.startswith("sub_type_"))
async def process_subscription_type(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор типа подписки"""
    tg_id = callback.from_user.id
    sub_type = callback.data.split("_")[2]
    logging.info(f"User {tg_id} selected subscription type: {sub_type}")

    await state.update_data(subscription_type=sub_type)

    # Выбираем тарифы в зависимости от типа
    if sub_type == "regular":
        tariffs = TARIFFS
        title = "📱 Обычная подписка"
    elif sub_type == "vip":
        tariffs = VIP_TARIFFS
        title = "🛡️ Обход глушилок (VIP)"
    else:  # combo
        tariffs = COMBO_TARIFFS
        title = "⭐ Обычная + VIP Комбо"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1 месяц — {tariffs['1m']['price']}₽", callback_data="tariff_1m")],
        [InlineKeyboardButton(text=f"3 месяца — {tariffs['3m']['price']}₽", callback_data="tariff_3m")],
        [InlineKeyboardButton(text=f"6 месяцев — {tariffs['6m']['price']}₽", callback_data="tariff_6m")],
        [InlineKeyboardButton(text=f"12 месяцев — {tariffs['12m']['price']}₽", callback_data="tariff_12m")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    await callback.message.edit_text(f"<b>{title}</b>\n\nВыбери срок подписки:", reply_markup=kb)
    await state.set_state(UserStates.choosing_tariff)


@router.callback_query(F.data.startswith("tariff_"))
async def process_tariff_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор тарифа"""
    tg_id = callback.from_user.id
    tariff_code = callback.data.split("_")[1]
    data = await state.get_data()
    sub_type = data.get("subscription_type", "regular")
    
    logging.info(f"User {tg_id} selected tariff: {tariff_code} (type: {sub_type})")

    # Выбираем тарифы в зависимости от типа
    if sub_type == "regular":
        tariffs = TARIFFS
        prefix = "regular"
    elif sub_type == "vip":
        tariffs = VIP_TARIFFS
        prefix = "vip"
    else:  # combo
        tariffs = COMBO_TARIFFS
        prefix = "combo"

    tariff = tariffs[tariff_code]
    amount = tariff["price"]
    
    await state.update_data(tariff_code=tariff_code, amount=amount)
    
    balance = await db.get_balance(tg_id)
    
    # Если хватает баланса, предлагаем оплату с баланса
    payment_buttons = []
    if balance >= amount:
        payment_buttons.append([InlineKeyboardButton(text="💰 Оплатить с баланса", callback_data="pay_balance")])
    
    payment_buttons.extend([
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_cryptobot")],
        [InlineKeyboardButton(text="💳 Yookassa", callback_data="pay_yookassa")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=payment_buttons)

    text = f"<b>Оплата тарифа {tariff_code}</b>\nСумма: {amount} ₽\nВаш баланс: {balance:.2f} ₽\n\nВыбери способ оплаты:"

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_payment)


@router.callback_query(F.data == "pay_balance")
async def process_pay_balance(callback: CallbackQuery, state: FSMContext):
    """Оплатить подписку с баланса"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    
    tariff_code = data.get("tariff_code")
    amount = data.get("amount", 0)
    sub_type = data.get("subscription_type", "regular")
    
    logging.info(f"User {tg_id} paying with balance: {amount} (type: {sub_type}, tariff: {tariff_code})")
    
    if amount <= 0:
        await callback.message.edit_text("Ошибка: сумма не определена")
        await state.clear()
        return
    
    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return
    
    try:
        # Проверяем баланс и вычитаем
        if not await db.subtract_balance(tg_id, amount):
            await callback.answer("Недостаточно средств на балансе", show_alert=True)
            return
        
        # Обрабатываем каждый тип подписки
        if sub_type == "regular":
            await _activate_regular_subscription(callback.bot, tg_id, tariff_code)
        elif sub_type == "vip":
            await _activate_vip_subscription(callback.bot, tg_id, tariff_code)
        else:  # combo
            await _activate_regular_subscription(callback.bot, tg_id, tariff_code)
            await _activate_vip_subscription(callback.bot, tg_id, tariff_code)
        
        # Обрабатываем реферальные комиссии
        await _process_referral_commission(tg_id, amount)
        
        await callback.message.edit_text(
            f"✅ <b>Подписка активирована!</b>\n\n"
            f"Тип: {sub_type}\n"
            f"Тариф: {tariff_code}\n"
            f"Списано с баланса: {amount} ₽"
        )
        
        logging.info(f"User {tg_id} successfully activated {sub_type} subscription with balance")
        
    except Exception as e:
        logging.error(f"Balance payment error for user {tg_id}: {e}")
        await callback.answer(f"Ошибка при активации подписки: {str(e)[:100]}", show_alert=True)
    
    finally:
        await db.release_user_lock(tg_id)
        await state.clear()


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий счёт в CryptoBot"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    amount = data.get("amount", 0)
    sub_type = data.get("subscription_type", "regular")
    
    logging.info(f"User {tg_id} selected payment method: cryptobot (tariff: {tariff_code})")

    if not tariff_code or amount <= 0:
        await callback.message.edit_text("Ошибка: тариф не выбран")
        await state.clear()
        return

    # Проверяем, есть ли уже активный счёт для этого пользователя и тарифа
    invoice_key = f"{sub_type}_{tariff_code}"
    existing_invoice_id = await db.get_active_payment_for_user_and_tariff(tg_id, invoice_key, "cryptobot")

    if existing_invoice_id:
        # Счёт уже есть - получаем его статус
        invoice = await get_invoice_status(existing_invoice_id)

        if invoice and invoice.get("status") == "active":
            pay_url = invoice.get("bot_invoice_url", "")

            if pay_url:
                # Возвращаем существующий счёт
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
                ])

                text = (
                    f"<b>Счёт на оплату (существующий)</b>\n\n"
                    f"Тариф: {invoice_key}\n"
                    f"Сумма: {amount} ₽\n\n"
                    "Оплати через CryptoBot. После оплаты бот автоматически активирует подписку.\n"
                    "Если не активировалось — нажми «Проверить оплату»"
                )

                await callback.message.edit_text(text, reply_markup=kb)
                await state.clear()
                logging.info(f"Returned existing CryptoBot invoice {existing_invoice_id} for user {tg_id}")
                return

    # Счёта нет или он истёк - создаём новый
    invoice = await create_cryptobot_invoice(callback.bot, amount, invoice_key, tg_id)

    if not invoice:
        await callback.message.edit_text("Ошибка создания счёта в CryptoBot. Попробуй позже.")
        await state.clear()
        return

    invoice_id = invoice["invoice_id"]
    pay_url = invoice["bot_invoice_url"]

    # Записываем платеж в БД
    await db.create_payment(
        tg_id,
        invoice_key,
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
        f"Тариф: {invoice_key}\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати через CryptoBot. После оплаты бот автоматически активирует подписку.\n"
        "Если не активировалось — нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "pay_yookassa")
async def process_pay_yookassa(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий платёж через Yookassa"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    amount = data.get("amount", 0)
    sub_type = data.get("subscription_type", "regular")
    
    logging.info(f"User {tg_id} selected payment method: yookassa (tariff: {tariff_code})")

    if not tariff_code or amount <= 0:
        await callback.message.edit_text("Ошибка: тариф не выбран")
        await state.clear()
        return

    # Проверяем, есть ли уже активный платёж для этого пользователя и тарифа
    invoice_key = f"{sub_type}_{tariff_code}"
    existing_payment_id = await db.get_active_payment_for_user_and_tariff(tg_id, invoice_key, "yookassa")

    if existing_payment_id:
        # Платёж уже есть - получаем его статус
        payment = await get_payment_status(existing_payment_id)

        if payment and payment.get("status") == "pending":
            confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")

            if confirmation_url:
                # Возвращаем существующий платёж
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
                ])

                text = (
                    f"<b>💳 Yookassa (существующий платёж)</b>\n\n"
                    f"Тариф: {invoice_key}\n"
                    f"Сумма: {amount} ₽\n\n"
                    "Оплати картой, СБП или другим способом через Yookassa.\n"
                    "После оплаты бот автоматически активирует подписку.\n"
                    "Если не активировалось — нажми «Проверить оплату»"
                )

                await callback.message.edit_text(text, reply_markup=kb)
                await state.clear()
                logging.info(f"Returned existing Yookassa payment {existing_payment_id} for user {tg_id}")
                return

    # Платежа нет или он истёк - создаём новый
    payment = await create_yookassa_payment(callback.bot, amount, invoice_key, tg_id)

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

    # Записываем платеж в БД
    await db.create_payment(
        tg_id,
        invoice_key,
        amount,
        "yookassa",
        payment_id
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    text = (
        f"<b>💳 Yookassa</b>\n\n"
        f"Тариф: {invoice_key}\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати картой, СБП или другим способом через Yookassa.\n"
        "После оплаты бот автоматически активирует подписку.\n"
        "Если не активировалось — нажми «Проверить оплату»"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data == "check_payment")
async def process_check_payment(callback: CallbackQuery):
    """Проверить статус платежа"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking payment status")

    # Проверка anti-spam: не более одной проверки в 1 секунду
    can_check, error_msg = await db.can_check_payment(tg_id)
    if not can_check:
        await callback.answer(error_msg, show_alert=True)
        return

    # Обновляем время последней проверки
    await db.update_last_payment_check(tg_id)

    # Получаем последний ожидающий платеж с информацией о провайдере
    result = await db.db_execute(
        """
        SELECT invoice_id, tariff_code, provider
        FROM payments
        WHERE tg_id = $1 AND status = 'pending' AND tariff_code NOT LIKE 'topup_%'
        ORDER BY id DESC
        LIMIT 1
        """,
        (tg_id,),
        fetch_one=True
    )

    if not result:
        await callback.answer("Нет ожидающих оплаты счетов", show_alert=True)
        return

    invoice_id = result['invoice_id']
    tariff_code = result['tariff_code']
    provider = result['provider']

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        if provider == "yookassa":
            # Проверяем платёж в Yookassa
            payment = await get_payment_status(invoice_id)

            if payment and payment.get("status") == "succeeded":
                success = await process_paid_subscription(callback.bot, tg_id, invoice_id, tariff_code)

                if success:
                    await callback.message.edit_text(
                        "✅ <b>Оплата подтверждена!</b>\n\n"
                        f"Тариф: {tariff_code}\n"
                        "Подписка активирована."
                    )
                else:
                    await callback.answer("Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)

        elif provider == "cryptobot":
            # Проверяем платёж в CryptoBot
            invoice = await get_invoice_status(invoice_id)

            if invoice and invoice.get("status") == "paid":
                success = await process_paid_subscription(callback.bot, tg_id, invoice_id, tariff_code)

                if success:
                    await callback.message.edit_text(
                        "✅ <b>Оплата подтверждена!</b>\n\n"
                        f"Тариф: {tariff_code}\n"
                        "Подписка активирована."
                    )
                else:
                    await callback.answer("Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)

    except Exception as e:
        logging.error(f"Check payment error: {e}")
        await callback.answer("Ошибка при проверке платежа", show_alert=True)

    finally:
        await db.release_user_lock(tg_id)


async def process_paid_subscription(bot, tg_id: int, invoice_id: str, tariff_code: str) -> bool:
    """Обработать оплаченный платёж подписки"""
    # Парсим tariff_code вида "regular_1m", "vip_3m", "combo_6m"
    parts = tariff_code.split("_")
    if len(parts) < 2:
        logging.error(f"Invalid tariff_code: {tariff_code}")
        return False
    
    sub_type = parts[0]
    code = parts[1]
    
    try:
        # Активируем каждый тип подписки
        if sub_type == "regular":
            await _activate_regular_subscription(bot, tg_id, code)
        elif sub_type == "vip":
            await _activate_vip_subscription(bot, tg_id, code)
        elif sub_type == "combo":
            await _activate_regular_subscription(bot, tg_id, code)
            await _activate_vip_subscription(bot, tg_id, code)
        
        # Обрабатываем реферальные комиссии
        # Получаем сумму платежа из БД
        payment = await db.db_execute(
            "SELECT amount FROM payments WHERE invoice_id = $1 LIMIT 1",
            (invoice_id,),
            fetch_one=True
        )
        if payment:
            await _process_referral_commission(tg_id, payment['amount'])
        
        # Отмечаем платёж как обработанный
        await db.update_payment_status_by_invoice(invoice_id, "succeeded")
        
        return True
        
    except Exception as e:
        logging.error(f"Failed to activate subscription: {e}")
        return False


async def _activate_regular_subscription(bot, tg_id: int, tariff_code: str):
    """Активировать обычную подписку"""
    days = TARIFFS[tariff_code]["days"]
    
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        uuid, username = await remnawave_get_or_create_user(
            session, tg_id, days=days, extend_if_exists=True
        )

        if not uuid:
            raise Exception(f"Failed to get/create Remnawave user")

        await remnawave_add_to_squad(session, uuid)
        
        new_until = datetime.utcnow() + timedelta(days=days)
        await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)


async def _activate_vip_subscription(bot, tg_id: int, tariff_code: str):
    """Активировать VIP подписку"""
    days = VIP_TARIFFS[tariff_code]["days"]
    
    # Получаем информацию о существующем VIP клиенте если есть
    vip_info = await db.get_vip_subscription_info(tg_id)
    
    if vip_info and vip_info['xui_uuid']:
        # Продляем существующего клиента
        success = await xui.extend_vip_client(
            tg_id,
            vip_info['xui_email'],
            vip_info['xui_uuid'],
            vip_info['xui_subscription_id'],
            days
        )
        if not success:
            raise Exception("Failed to extend VIP client")
    else:
        # Создаём нового VIP клиента
        result = await xui.create_or_extend_vip_client(tg_id, days, is_new=True)
        if not result:
            raise Exception("Failed to create VIP client")
        
        email, client_uuid, subscription_id, sub_url = result
        
        vip_until = datetime.utcnow() + timedelta(days=days)
        await db.update_vip_subscription(tg_id, email, client_uuid, subscription_id, vip_until)


async def _process_referral_commission(tg_id: int, amount: float):
    """Обработать реферальные комиссии"""
    # Получаем информацию о рефералите
    referrer_id, first_payment = await db.get_referrer(tg_id)
    
    if referrer_id:
        # Добавляем комиссию рефералу (25% от суммы)
        commission = amount * (REFERRAL_COMMISSION_PERCENT / 100)
        await db.add_referral_commission(referrer_id, commission)
        
        logging.info(f"Referral commission: {commission}₽ added to user {referrer_id} (from {tg_id})")


@router.callback_query(F.data == "my_subscription")
async def process_my_subscription(callback: CallbackQuery):
    """Показать информацию о подписке пользователя"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking subscription status")

    user = await db.get_user(tg_id)

    if not user or (not user['remnawave_uuid'] and not user['xui_uuid']):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оформить подписку", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])
        await callback.message.edit_text(
            "У тебя пока нет активной подписки.\nОформи её сейчас!",
            reply_markup=kb
        )
        return

    subscription_info = []
    
    # Получаем информацию об обычной подписке
    if user['remnawave_uuid']:
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                sub_url = await remnawave_get_subscription_url(session, user['remnawave_uuid'])
                user_info = await remnawave_get_user_info(session, user['remnawave_uuid'])

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

                    subscription_info.append((
                        "📱 <b>Обычная подписка</b>",
                        f"Осталось: {remaining_str}\nСсылка: <code>{sub_url}</code>"
                    ))
        except Exception as e:
            logging.error(f"Error fetching subscription info: {e}")
            subscription_info.append(("📱 <b>Обычная подписка</b>", "Ошибка загрузки"))
    
    # Получаем информацию о VIP подписке
    if user['xui_uuid']:
        vip_until = user.get('vip_subscription_until')
        if vip_until:
            if isinstance(vip_until, str):
                vip_dt = datetime.fromisoformat(vip_until.replace('Z', '+00:00'))
            else:
                vip_dt = vip_until.replace(tzinfo=timezone.utc)
            
            remaining = vip_dt - datetime.now(timezone.utc)
            
            if remaining.total_seconds() <= 0:
                remaining_str = "истекла"
            else:
                days = remaining.days
                hours = remaining.seconds // 3600
                remaining_str = f"{days}д {hours}ч"
            
            sub_url = f"https://{user['xui_subscription_id']}"
            subscription_info.append((
                "🛡️ <b>Обход глушилок (VIP)</b>",
                f"Осталось: {remaining_str}\nСсылка: <code>https://spn.sub.idlebat.online:2096/{user['xui_subscription_id']}</code>"
            ))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = "<b>🔐 Мой доступ</b>\n\n"
    for title, info in subscription_info:
        text += f"{title}\n{info}\n\n"

    await callback.message.edit_text(text, reply_markup=kb)
