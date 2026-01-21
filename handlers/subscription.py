import logging
import aiohttp
from datetime import datetime, timedelta, timezone, UTC
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import TARIFFS, COMBO_TARIFFS, DEFAULT_SQUAD_UUID
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
        [InlineKeyboardButton(text="📱 Обычная подписка", callback_data="subscription_type_normal")],
        [InlineKeyboardButton(text="📱 + Обход глушилок", callback_data="subscription_type_vip")],
        [InlineKeyboardButton(text="📱 Обычная + Обход глушилок", callback_data="subscription_type_combo")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Выбери тип подписки:</b>\n\n"
        "<b>📱 Обычная подписка</b>\n"
        "Стабильное соединение, оптимизация интернета\n\n"
        "<b>📱 + Обход глушилок</b>\n"
        "Обычная подписка + улучшенный VIP доступ\n"
        "с дополнительными серверами для преодоления блокировок\n\n"
        "<b>📱 Обычная + Обход глушилок</b>\n"
        "Оба типа подписок по выгодной цене"
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_subscription_type)


@router.callback_query(F.data.startswith("subscription_type_"))
async def process_subscription_type(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор типа подписки"""
    tg_id = callback.from_user.id
    sub_type = callback.data.split("_")[2]  # "normal", "vip" или "combo"
    logging.info(f"User {tg_id} selected subscription type: {sub_type}")

    await state.update_data(subscription_type=sub_type)

    # Выбираем правильные тарифы в зависимости от типа подписки
    tariff_dict = COMBO_TARIFFS if sub_type == "combo" else TARIFFS

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1 месяц — {tariff_dict['1m']['price']}₽", callback_data="tariff_1m")],
        [InlineKeyboardButton(text=f"3 месяца — {tariff_dict['3m']['price']}₽", callback_data="tariff_3m")],
        [InlineKeyboardButton(text=f"6 месяцев — {tariff_dict['6m']['price']}₽", callback_data="tariff_6m")],
        [InlineKeyboardButton(text=f"12 месяцев — {tariff_dict['12m']['price']}₽", callback_data="tariff_12m")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
    ])

    if sub_type == "combo":
        sub_type_label = "Обычная + Обход глушилок"
    elif sub_type == "vip":
        sub_type_label = "Обход глушилок (VIP)"
    else:
        sub_type_label = "Обычная подписка"

    text = f"<b>Выбери срок подписки</b> ({sub_type_label}):"

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(UserStates.choosing_tariff)


@router.callback_query(F.data.startswith("tariff_"))
async def process_tariff_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор тарифа - оплата ТОЛЬКО с баланса"""
    tg_id = callback.from_user.id
    tariff_code = callback.data.split("_")[1]
    logging.info(f"User {tg_id} selected tariff: {tariff_code}")

    await state.update_data(tariff_code=tariff_code)

    # Получаем тип подписки из состояния
    data = await state.get_data()
    subscription_type = data.get("subscription_type", "normal")

    # Выбираем правильные тарифы в зависимости от типа подписки
    tariff_dict = COMBO_TARIFFS if subscription_type == "combo" else TARIFFS
    tariff = tariff_dict[tariff_code]
    price = tariff["price"]
    days = tariff["days"]

    # Получаем баланс пользователя
    balance = await db.get_balance(tg_id)

    if balance >= price:
        # Достаточно средств - вычитаем со счёта и активируем подписку
        if not await db.acquire_user_lock(tg_id):
            await callback.answer("Подожди пару секунд ⏳", show_alert=True)
            return

        try:
            success = await db.subtract_balance(tg_id, price)

            if not success:
                await callback.answer("Ошибка при списании со счёта", show_alert=True)
                return

            # Активируем подписку
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # Создаём или получаем пользователя в Remnawave для обычной подписки
                uuid, username = await remnawave_get_or_create_user(
                    session, tg_id, days, extend_if_exists=True
                )

                if not uuid:
                    logging.error(f"Failed to create/get Remnawave user for {tg_id}")
                    # Откат: возвращаем деньги
                    await db.add_balance(tg_id, price)
                    await callback.answer("Ошибка создания подписки", show_alert=True)
                    return

                # Добавляем в сквад
                squad_added = await remnawave_add_to_squad(session, uuid)
                if not squad_added:
                    logging.warning(f"Failed to add user {uuid} to squad")

                # Получаем ссылку подписки
                sub_url = await remnawave_get_subscription_url(session, uuid)
                if not sub_url:
                    logging.warning(f"Failed to get subscription URL for {uuid}")

                # Обновляем обычную подписку пользователя в БД
                new_until = datetime.utcnow() + timedelta(days=days)
                await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

            # Если выбрана VIP подписка или комбо, создаём её через XUI
            if subscription_type in ("vip", "combo"):
                from services.xui_panel import get_xui_session, xui_create_or_extend_client
                xui_session = await get_xui_session()
                if xui_session:
                    try:
                        vip_uuid, vip_email = await xui_create_or_extend_client(xui_session, tg_id, days)
                        if vip_uuid and vip_email:
                            new_vip_until = datetime.utcnow() + timedelta(days=days)
                            await db.update_vip_subscription(tg_id, vip_uuid, vip_email, new_vip_until)
                    except Exception as e:
                        logging.warning(f"Failed to create/extend VIP subscription: {e}")
                    finally:
                        await xui_session.close()

            # Отправляем сообщение пользователю
            if subscription_type == "combo":
                sub_type_text = "Обычная подписка + Обход глушилок"
            elif subscription_type == "vip":
                sub_type_text = "Обход глушилок (VIP)"
            else:
                sub_type_text = "Обычная подписка"

            text = (
                "✅ <b>Подписка активирована!</b>\n\n"
                f"Тариф: {tariff_code} ({days} дней)\n"
                f"Тип: {sub_type_text}\n"
                f"Списано со счёта: {price} ₽\n\n"
                f"<b>Ссылка подписки:</b>\n<code>{sub_url or 'Ошибка получения ссылки'}</code>"
            )

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
            ])

            await callback.message.edit_text(text, reply_markup=kb)
            await state.clear()

        except Exception as e:
            logging.error(f"Error processing subscription payment: {e}")
            # Откат: возвращаем деньги
            await db.add_balance(tg_id, price)
            await callback.answer("Ошибка при активации подписки", show_alert=True)
        finally:
            await db.release_user_lock(tg_id)

    else:
        # Недостаточно средств
        needed = price - balance
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="topup_balance")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription")]
        ])

        text = (
            f"<b>Недостаточно средств</b>\n\n"
            f"Стоимость тарифа: {price} ₽\n"
            f"Ваш баланс: {balance:.2f} ₽\n"
            f"Не хватает: {needed:.2f} ₽\n\n"
            "Пополните баланс, чтобы активировать подписку"
        )

        await callback.message.edit_text(text, reply_markup=kb)
        await state.clear()


@router.callback_query(F.data == "my_subscription")
async def process_my_subscription(callback: CallbackQuery):
    """Показать информацию о подписке пользователя"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking subscription status")

    user = await db.get_user(tg_id)

    if not user or not user['remnawave_uuid']:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оформить подписку", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])
        await callback.message.edit_text(
            "У тебя пока нет активной подписки.\nОформи её сейчас!",
            reply_markup=kb
        )
        return

    # Получаем актуальную информацию о подписке из Remnawave
    remaining_str = "неизвестно"
    sub_url = "ошибка получения ссылки"
    vip_remaining_str = None

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

    # Проверяем VIP подписку
    vip_status = "❌ Нет"
    if user['vip_subscription_until']:
        vip_until = user['vip_subscription_until']
        if vip_until > datetime.utcnow():
            remaining = vip_until - datetime.utcnow()
            days = remaining.days
            hours = remaining.seconds // 3600
            vip_remaining_str = f"{days}д {hours}ч"
            vip_status = f"✅ Активна ({vip_remaining_str})"
        else:
            vip_status = "❌ Истекла"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
            "🔐 <b>Мой доступ</b>\n\n"
            "<b>Обычная подписка:</b>\n"
            "<blockquote>"
            f"📆 Осталось времени: <b>{remaining_str}</b>\n"
        "🌐 Группа подключения: <b>SPN-Squad</b>\n"
        "</blockquote>\n\n"
        "<b>Обход глушилок (VIP):</b>\n"
        f"<blockquote>{vip_status}</blockquote>\n\n"
        "<b>Персональная ссылка доступа:</b>\n"
        f"{sub_url or '<i>Ошибка получения ссылки</i>'}\n\n"
        "🟢 <i>Статус: активен</i>"
    )

    await callback.message.edit_text(text, reply_markup=kb)
