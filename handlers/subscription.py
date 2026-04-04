import logging
import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import TARIFFS, DEFAULT_SQUAD_UUID
from states import UserStates
import database as db
from services.remnawave import (
    remnawave_get_subscription_url,
    remnawave_get_user_info,
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
)
from services.cryptobot import create_cryptobot_invoice, get_invoice_status, process_paid_invoice
from services.yookassa import create_yookassa_payment, get_payment_status, process_paid_yookassa_payment
from services.image_handler import edit_text_with_photo


logger = logging.getLogger(__name__)

router = Router()


async def _activate_subscription_with_referral_balance(tg_id: int, tariff_code: str):
    """Активировать подписку за счёт реферального баланса."""
    tariff = TARIFFS[tariff_code]
    days = tariff["days"]
    amount = tariff["price"]

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        uuid, username = await remnawave_get_or_create_user(
            session, tg_id, days, extend_if_exists=True
        )

        if not uuid:
            logging.error(f"Failed to create/get Remnawave user for {tg_id}")
            return None

        squad_added = await remnawave_add_to_squad(session, uuid)
        if not squad_added:
            logging.warning(f"Failed to add user {uuid} to squad")

        sub_url = await remnawave_get_subscription_url(session, uuid)
        if not sub_url:
            logging.warning(f"Failed to get subscription URL for {uuid}")

        user = await db.get_user(tg_id)
        existing_subscription = user.get('subscription_until') if user else None
        now = datetime.utcnow()

        if existing_subscription and existing_subscription > now:
            new_until = existing_subscription + timedelta(days=days)
            logging.info(
                f"User {tg_id} has active subscription, extending from {existing_subscription} "
                f"by {days} days to {new_until}"
            )
        else:
            new_until = now + timedelta(days=days)
            logging.info(f"User {tg_id} has no active subscription, creating new one with {days} days until {new_until}")

        await db.update_subscription(tg_id, uuid, username, new_until, None)

        try:
            referrer = await db.get_referrer(tg_id)
            if referrer and referrer[0]:
                referrer_id = referrer[0]

                is_first_purchase = await db.check_first_referral_purchase(tg_id, referrer_id)
                await db.add_referral_earning(
                    referrer_id,
                    tg_id,
                    tariff_code,
                    amount,
                    is_first_purchase=is_first_purchase,
                )
                await db.mark_first_payment(tg_id)
        except Exception as e:
            logging.error(f"Error processing referral for user {tg_id}: {e}")

        try:
            partner_result = await db.db_execute(
                """
                SELECT DISTINCT partner_id FROM partner_referrals
                WHERE referred_user_id = $1
                LIMIT 1
                """,
                (tg_id,),
                fetch_one=True
            )

            if partner_result:
                partner_id = partner_result['partner_id']
                partnership = await db.get_partnership(partner_id)
                if partnership:
                    await db.add_partner_earning(
                        partner_id,
                        tg_id,
                        tariff_code,
                        amount,
                        partnership['percentage']
                    )
        except Exception as e:
            logging.error(f"Error processing partner earnings for user {tg_id}: {e}", exc_info=True)

        try:
            await db.create_referral_balance_spend(tg_id, amount, tariff_code)
        except Exception as e:
            logging.error(f"Error recording referral balance spend for user {tg_id}: {e}")

        internal_payment_id = f"referral_balance_{tg_id}_{tariff_code}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        try:
            await db.create_payment(tg_id, tariff_code, amount, "referral_balance", internal_payment_id)
            await db.update_payment_status_by_invoice(internal_payment_id, 'paid')
        except Exception as e:
            logging.error(f"Error recording referral balance payment for user {tg_id}: {e}")

        return {
            "sub_url": sub_url,
            "days": days,
            "amount": amount,
            "payment_id": internal_payment_id,
        }


@router.callback_query(F.data == "buy_subscription")
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext):
    """Показать выбор тарифов"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: buy_subscription")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц — 200₽", callback_data="tariff_1m")],
        [InlineKeyboardButton(text="3 месяца — 500₽", callback_data="tariff_3m")],
        [InlineKeyboardButton(text="6 месяцев — 900₽", callback_data="tariff_6m")],
        [InlineKeyboardButton(text="12 месяцев — 1550₽", callback_data="tariff_12m")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
    ])

    text = "Выбери срок подписки:"
    await edit_text_with_photo(callback, text, kb, "Выбери срок подписки")
    await state.set_state(UserStates.choosing_tariff)


@router.callback_query(F.data.startswith("tariff_"))
async def process_tariff_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор тарифа"""
    tg_id = callback.from_user.id
    tariff_code = callback.data.split("_")[1]
    logging.info(f"User {tg_id} selected tariff: {tariff_code}")

    await state.update_data(tariff_code=tariff_code)

    tariff = TARIFFS[tariff_code]

    referral_stats = await db.get_referral_stats(tg_id)
    referral_balance = float(referral_stats['current_balance'] or 0)

    payment_buttons = []
    if referral_balance >= tariff['price']:
        payment_buttons.append([
            InlineKeyboardButton(text="💰 Реферальный баланс", callback_data="pay_referral_balance")
        ])
    payment_buttons.extend([
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_cryptobot")],
        [InlineKeyboardButton(text="💳 Оплатить картой", callback_data="pay_yookassa")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=payment_buttons)

    text = (
        f"<b>Оплата тарифа {tariff_code}</b>\n"
        f"Сумма: {tariff['price']} ₽\n"
        f"Доступно на реферальном балансе: {referral_balance:.2f} ₽\n\n"
        "Выбери способ оплаты:"
    )

    await edit_text_with_photo(callback, text, kb, "Выбери способ оплаты")
    await state.set_state(UserStates.choosing_payment)


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий счёт в CryptoBot"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    logging.info(f"User {tg_id} selected payment method: cryptobot (tariff: {tariff_code})")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    amount = tariff["price"]
    tg_id = callback.from_user.id

    # Проверяем, есть ли уже активный счёт для этого пользователя и тарифа
    existing_invoice_id = await db.get_active_payment_for_user_and_tariff(tg_id, tariff_code, "cryptobot")

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
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")]
                ])

                text = (
                    f"<b>Счёт на оплату (существующий)</b>\n\n"
                    f"Тариф: {tariff_code}\n"
                    f"Сумма: {amount} ₽\n\n"
                    "Оплати через CryptoBot. После оплаты бот автоматически активирует подписку.\n"
                    "Если не активировалось — нажми «Проверить оплату»"
                )

                await edit_text_with_photo(callback, text, kb, "Оплати")
                await state.clear()
                logging.info(f"Returned existing CryptoBot invoice {existing_invoice_id} for user {tg_id}")
                return

    # Счёта нет или он истёк - создаём новый
    invoice = await create_cryptobot_invoice(callback.bot, amount, tariff_code, tg_id)

    if not invoice:
        await callback.answer("Ошибка создания счёта в CryptoBot. Попробуй позже.", show_alert=True)
        await state.clear()
        return

    invoice_id = invoice["invoice_id"]
    pay_url = invoice["bot_invoice_url"]

    # Записываем платеж в БД
    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        "cryptobot",
        invoice_id
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")]
    ])

    text = (
        f"<b>Счёт на оплату</b>\n\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати через CryptoBot. После оплаты бот автоматически активирует подписку.\n"
        "Если не активировалось — нажми «Проверить оплату»"
    )

    await edit_text_with_photo(callback, text, kb, "Оплати")
    await state.clear()


@router.callback_query(F.data == "pay_yookassa")
async def process_pay_yookassa(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий платёж через Yookassa"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    logging.info(f"User {tg_id} selected payment method: yookassa (tariff: {tariff_code})")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    amount = tariff["price"]
    tg_id = callback.from_user.id

    # Проверяем, есть ли уже активный платёж для этого пользователя и тарифа
    existing_payment_id = await db.get_active_payment_for_user_and_tariff(tg_id, tariff_code, "yookassa")

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
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")]
                ])

                text = (
                    f"<b>💳 Yookassa (существующий платёж)</b>\n\n"
                    f"Тариф: {tariff_code}\n"
                    f"Сумма: {amount} ₽\n\n"
                    "Оплати картой, СБП или другим способом через Yookassa.\n"
                    "После оплаты бот автоматически активирует подписку.\n"
                    "Если не активировалось — нажми «Проверить оплату»"
                )

                await edit_text_with_photo(callback, text, kb, "Оплати")
                await state.clear()
                logging.info(f"Returned existing Yookassa payment {existing_payment_id} for user {tg_id}")
                return

    # Платежа нет или он истёк - создаём новый
    payment = await create_yookassa_payment(callback.bot, amount, tariff_code, tg_id)

    if not payment:
        await callback.answer("Ошибка создания платежа в Yookassa. Попробуй позже.", show_alert=True)
        await state.clear()
        return

    payment_id = payment["id"]
    confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")

    if not confirmation_url:
        await callback.answer("Ошибка: не получена ссылка для оплаты", show_alert=True)
        await state.clear()
        return

    # Записываем платеж в БД
    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        "yookassa",
        payment_id
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url)],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")]
    ])

    text = (
        f"<b>💳 Yookassa</b>\n\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати картой, СБП или другим способом через Yookassa.\n"
        "После оплаты бот автоматически активирует подписку.\n"
        "Если не активировалось — нажми «Проверить оплату»"
    )

    await edit_text_with_photo(callback, text, kb, "Оплати")
    await state.clear()


@router.callback_query(F.data == "pay_referral_balance")
async def process_pay_referral_balance(callback: CallbackQuery, state: FSMContext):
    """Оплатить подписку за счёт реферального баланса"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    logging.info(f"User {tg_id} selected payment method: referral_balance (tariff: {tariff_code})")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    amount = tariff["price"]

    referral_stats = await db.get_referral_stats(tg_id)
    referral_balance = float(referral_stats['current_balance'] or 0)

    if referral_balance < amount:
        await callback.answer("❌ Недостаточно средств на реферальном балансе", show_alert=True)
        await state.clear()
        return

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        referral_stats = await db.get_referral_stats(tg_id)
        referral_balance = float(referral_stats['current_balance'] or 0)
        if referral_balance < amount:
            await callback.answer("❌ Недостаточно средств на реферальном балансе", show_alert=True)
            return

        result = await _activate_subscription_with_referral_balance(tg_id, tariff_code)
        if not result:
            await callback.answer("Ошибка при активации подписки", show_alert=True)
            return

        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Тариф: {tariff_code} ({result['days']} дней)\n"
            f"Списано с реферального баланса: {result['amount']} ₽\n"
            f"<b>Ваш ключ:</b>\n{result['sub_url'] or 'Ошибка получения ссылки'}"
        )
        await callback.bot.send_message(tg_id, text)

        await callback.answer(
            "✅ Оплата подтверждена!\n\nВаш ключ отправлен в сообщении от бота.",
            show_alert=True
        )

    except Exception as e:
        logging.error(f"Referral balance payment error: {e}")
        await callback.answer("Ошибка при оплате реферальным балансом", show_alert=True)
    finally:
        await db.release_user_lock(tg_id)
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
        WHERE tg_id = $1 AND status = 'pending'
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
                success = await process_paid_yookassa_payment(callback.bot, tg_id, invoice_id, tariff_code)

                if success:
                    await callback.answer(
                        "✅ Оплата подтверждена!\n\n"
                        f"Тариф: {tariff_code}\n"
                        "Ваш ключ отправлен в сообщении от бота.",
                        show_alert=True
                    )
                else:
                    await callback.answer("Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)

        elif provider == "cryptobot":
            # Проверяем платёж в CryptoBot
            invoice = await get_invoice_status(invoice_id)

            if invoice and invoice.get("status") == "paid":
                success = await process_paid_invoice(callback.bot, tg_id, invoice_id, tariff_code)

                if success:
                    await callback.answer(
                        "✅ Оплата подтверждена!\n\n"
                        f"Тариф: {tariff_code}\n"
                        "Ваш ключ отправлен в сообщении выше.",
                        show_alert=True
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


@router.callback_query(F.data == "my_subscription")
async def process_my_subscription(callback: CallbackQuery):
    """Показать информацию о подписке пользователя"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking subscription status")

    user = await db.get_user(tg_id)

    if not user or not user['remnawave_uuid']:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оформить подписку", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
        ])
        text = "У тебя пока нет активной подписки.\nОформи её сейчас!"
        await edit_text_with_photo(callback, text, kb, "My-not_subscription")
        return

    # Получаем актуальную информацию о подписке из Remnawave
    remaining_str = "неизвестно"
    sub_url = "ошибка получения ссылки"

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Получаем ссылку подписки
            sub_url = await remnawave_get_subscription_url(session, user['remnawave_uuid'])

            # Получаем информацию о пользователе (включая expireAt)
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

    except Exception as e:
        logging.error(f"Error fetching subscription info from Remnawave: {e}")
        remaining_str = "ошибка загрузки"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")]
    ])

    text = (
            "🔐 <b>Мой доступ</b>\n\n"
            "<blockquote>"
            f"📆 Осталось времени: <b>{remaining_str}</b>\n"
        "🌐 Группа подключения: <b>SPN-Squad</b>\n"
        "</blockquote>\n\n"
        "<b>Ваш ключ:</b>\n"
        f"{sub_url or '<i>Ошибка получения ссылки</i>'}\n\n"
    )

    await edit_text_with_photo(callback, text, kb, "Моя подписка")
