import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import TARIFFS_REGULAR, TARIFFS_VIP, TARIFFS_BOTH, DEFAULT_SQUAD_UUID
from states import UserStates
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url,
    remnawave_get_user_info
)


router = Router()


@router.callback_query(F.data == "buy_subscription")
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext):
    """Показать выбор типа подписки"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: buy_subscription")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Обычная подписка", callback_data="subtype_regular")],
        [InlineKeyboardButton(text="🔒 Обход глушилок", callback_data="subtype_vip")],
        [InlineKeyboardButton(text="⭐ Обычная + Обход", callback_data="subtype_both")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Выберите тип подписки:</b>\n\n"
        "<b>🌐 Обычная подписка</b>\n"
        "Стандартный VPN доступ\n\n"
        "<b>🔒 Обход глушилок</b>\n"
        "Специальная подписка для обхода блокировок\n\n"
        "<b>⭐ Обычная + Обход</b>\n"
        "Оба типа подписки вместе по выгодной цене"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_subscription_type)


@router.callback_query(F.data.startswith("subtype_"), UserStates.choosing_subscription_type)
async def process_subscription_type_choice(callback: CallbackQuery, state: FSMContext):
    """Обработить выбор типа подписки"""
    tg_id = callback.from_user.id
    sub_type = callback.data.split("_")[1]  # regular, vip, both
    logging.info(f"User {tg_id} selected subscription type: {sub_type}")

    await state.update_data(subscription_type=sub_type)

    # Выбираем нужный набор тарифов
    if sub_type == "regular":
        tariffs = TARIFFS_REGULAR
        title = "Обычная подписка"
    elif sub_type == "vip":
        tariffs = TARIFFS_VIP
        title = "Обход глушилок"
    else:  # both
        tariffs = TARIFFS_BOTH
        title = "Обычная + Обход глушилок"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1 месяц — {tariffs['1m']['price']}₽", callback_data="tariff_1m")],
        [InlineKeyboardButton(text=f"3 месяца — {tariffs['3m']['price']}₽", callback_data="tariff_3m")],
        [InlineKeyboardButton(text=f"6 месяцев — {tariffs['6m']['price']}₽", callback_data="tariff_6m")],
        [InlineKeyboardButton(text=f"12 месяцев — {tariffs['12m']['price']}₽", callback_data="tariff_12m")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    await callback.message.edit_text(f"<b>{title}</b>\n\nВыбери срок подписки:", reply_markup=kb)
    await state.set_state(UserStates.choosing_tariff)


@router.callback_query(F.data.startswith("tariff_"), UserStates.choosing_tariff)
async def process_tariff_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор тарифа"""
    tg_id = callback.from_user.id
    tariff_code = callback.data.split("_")[1]
    data = await state.get_data()
    sub_type = data.get("subscription_type", "regular")
    logging.info(f"User {tg_id} selected tariff: {tariff_code} for {sub_type}")

    # Выбираем правильный набор тарифов
    if sub_type == "regular":
        tariffs = TARIFFS_REGULAR
    elif sub_type == "vip":
        tariffs = TARIFFS_VIP
    else:  # both
        tariffs = TARIFFS_BOTH

    if tariff_code not in tariffs:
        await callback.message.edit_text("❌ Неверный тариф")
        await state.clear()
        return

    tariff = tariffs[tariff_code]
    amount = tariff["price"]

    await state.update_data(tariff_code=tariff_code, amount=amount)

    # Проверяем баланс пользователя
    balance = await db.get_balance(tg_id)

    if balance >= amount:
        # Баланс достаточен - предлагаем оплатить с баланса или пополнить
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оплатить с баланса", callback_data="pay_from_balance")],
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="top_up_balance_and_pay")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
        ])
        text = (
            f"<b>💰 Ваш баланс: {balance:.2f} ₽</b>\n\n"
            f"Сумма: {amount} ₽\n\n"
            "У вас достаточно средств. Как вы хотите оплатить?"
        )
    else:
        # Баланса недостаточно - предлагаем пополнить
        needed = amount - balance
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="top_up_balance_and_pay")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
        ])
        text = (
            f"<b>💰 Ваш баланс: {balance:.2f} ₽</b>\n\n"
            f"Сумма подписки: {amount} ₽\n"
            f"<b>Не хватает: {needed:.2f} ₽</b>\n\n"
            "Пополните баланс чтобы завершить покупку"
        )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_payment)


@router.callback_query(F.data == "pay_from_balance", UserStates.choosing_payment)
async def process_pay_from_balance(callback: CallbackQuery, state: FSMContext):
    """Оплатить подписку с баланса"""
    tg_id = callback.from_user.id
    data = await state.get_data()
    amount = data.get("amount")
    subscription_type = data.get("subscription_type", "regular")
    tariff_code = data.get("tariff_code")

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        # Списываем со счета
        deducted = await db.deduct_balance(tg_id, amount)
        if not deducted:
            await callback.answer("❌ Недостаточно средств", show_alert=True)
            return

        # Выбираем нужный набор тарифов для получения количества дней
        if subscription_type == "regular":
            tariffs = TARIFFS_REGULAR
        elif subscription_type == "vip":
            tariffs = TARIFFS_VIP
        else:  # both
            tariffs = TARIFFS_BOTH

        tariff = tariffs[tariff_code]
        days = tariff["days"]

        # Активируем подписку в Remnawave
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            if subscription_type == "both":
                # Создаём обе подписки
                uuid_regular, username_regular = await remnawave_get_or_create_user(
                    session, tg_id, days, extend_if_exists=True, sub_type="regular"
                )
                uuid_vip, username_vip = await remnawave_get_or_create_user(
                    session, tg_id, days, extend_if_exists=True, sub_type="vip"
                )

                if not uuid_regular or not uuid_vip:
                    await db.add_balance(tg_id, amount)  # Возвращаем деньги
                    await callback.answer("❌ Ошибка активации подписки", show_alert=True)
                    return

                # Добавляем обе подписки в сквады
                await remnawave_add_to_squad(session, uuid_regular)
                await remnawave_add_to_squad(session, uuid_vip)

                # Получаем ссылки подписок
                sub_url_regular = await remnawave_get_subscription_url(session, uuid_regular)
                sub_url_vip = await remnawave_get_subscription_url(session, uuid_vip)

                # Обновляем обе подписки в БД
                new_until = datetime.utcnow() + timedelta(days=days)
                await db.update_both_subscriptions(
                    tg_id,
                    uuid_regular, username_regular, new_until, DEFAULT_SQUAD_UUID,
                    uuid_vip, username_vip, new_until, DEFAULT_SQUAD_UUID
                )

                text = (
                    "✅ <b>Подписка активирована!</b>\n\n"
                    f"Срок: {days} дней\n\n"
                    f"<b>🌐 Обычная подписка:</b>\n<code>{sub_url_regular}</code>\n\n"
                    f"<b>🔒 Обход глушилок:</b>\n<code>{sub_url_vip}</code>"
                )
            else:
                # Создаём только один тип подписки
                uuid, username = await remnawave_get_or_create_user(
                    session, tg_id, days, extend_if_exists=True, sub_type=subscription_type
                )

                if not uuid:
                    await db.add_balance(tg_id, amount)  # Возвращаем деньги
                    await callback.answer("❌ Ошибка активации подписки", show_alert=True)
                    return

                # Добавляем в сквад
                await remnawave_add_to_squad(session, uuid)
                sub_url = await remnawave_get_subscription_url(session, uuid)

                # Обновляем подписку в БД
                new_until = datetime.utcnow() + timedelta(days=days)
                if subscription_type == "regular":
                    await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)
                else:  # vip
                    await db.update_subscription_vip(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

                text = (
                    "✅ <b>Подписка активирована!</b>\n\n"
                    f"Срок: {days} дней\n"
                    f"<b>Ссылка подписки:</b>\n<code>{sub_url}</code>"
                )

            # Обрабатываем реферальную программу (25% от суммы)
            try:
                referrer = await db.get_referrer(tg_id)
                if referrer and referrer[0] and not referrer[1]:  # есть рефералит и это первый платеж
                    referral_bonus = amount * 0.25
                    await db.add_referral_balance(referrer[0], referral_bonus)
                    await db.mark_first_payment(tg_id)
                    logging.info(f"Referral bonus {referral_bonus} given to {referrer[0]}")
            except Exception as e:
                logging.error(f"Error processing referral for user {tg_id}: {e}")

        await callback.message.edit_text(text)
        await state.clear()

    except Exception as e:
        logging.error(f"Pay from balance error: {e}")
        await db.add_balance(tg_id, amount)  # Возвращаем деньги при ошибке
        await callback.answer(f"❌ Ошибка при оплате: {str(e)[:50]}", show_alert=True)

    finally:
        await db.release_user_lock(tg_id)


@router.callback_query(F.data == "top_up_balance_and_pay")
async def process_top_up_balance_and_pay(callback: CallbackQuery, state: FSMContext):
    """Переходим на пополнение баланса, а потом назад к покупке"""
    await state.update_data(return_to_payment=True)
    await callback.message.edit_text("Выберите сумму для пополнения:", 
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="100 ₽", callback_data="topup_100")],
        [InlineKeyboardButton(text="500 ₽", callback_data="topup_500")],
        [InlineKeyboardButton(text="1000 ₽", callback_data="topup_1000")],
        [InlineKeyboardButton(text="5000 ₽", callback_data="topup_5000")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ]))


@router.callback_query(F.data == "my_subscription")
async def process_my_subscription(callback: CallbackQuery):
    """Показать информацию о подписках пользователя"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking subscriptions")

    user = await db.get_user(tg_id)

    if not user or (not user['remnawave_uuid'] and not user['remnawave_uuid_vip']):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оформить подписку", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])
        await callback.message.edit_text(
            "У тебя пока нет активной подписки.\nОформи её сейчас!",
            reply_markup=kb
        )
        return

    # Получаем информацию о подписках
    sub_info_regular = "Не активирована"
    sub_info_vip = "Не активирована"

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Обычная подписка
            if user['remnawave_uuid']:
                try:
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

                        sub_url = await remnawave_get_subscription_url(session, user['remnawave_uuid'])
                        sub_info_regular = f"Осталось: <b>{remaining_str}</b>\n<code>{sub_url or 'Ошибка'}</code>"
                except Exception as e:
                    logging.error(f"Error fetching regular subscription info: {e}")
                    sub_info_regular = "Ошибка загрузки"

            # VIP подписка
            if user['remnawave_uuid_vip']:
                try:
                    user_info = await remnawave_get_user_info(session, user['remnawave_uuid_vip'])
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

                        sub_url = await remnawave_get_subscription_url(session, user['remnawave_uuid_vip'])
                        sub_info_vip = f"Осталось: <b>{remaining_str}</b>\n<code>{sub_url or 'Ошибка'}</code>"
                except Exception as e:
                    logging.error(f"Error fetching VIP subscription info: {e}")
                    sub_info_vip = "Ошибка загрузки"

    except Exception as e:
        logging.error(f"Error fetching subscription info from Remnawave: {e}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "🔐 <b>Мои подписки</b>\n\n"
        "<blockquote>"
        f"<b>🌐 Обычная подписка</b>\n{sub_info_regular}\n\n"
        f"<b>🔒 Обход глушилок</b>\n{sub_info_vip}\n"
        "</blockquote>\n\n"
        "🟢 <i>Статус: активны</i>"
    )

    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "check_payment")
async def process_check_payment(callback: CallbackQuery):
    """Проверить статус платежа (не используется для оплаты с баланса, но оставляем для совместимости)"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking payment status")

    await callback.answer("Эта функция используется для платежей через внешние системы", show_alert=True)
