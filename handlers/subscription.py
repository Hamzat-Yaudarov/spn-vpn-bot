import logging
import html
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db
from config import (
    ADMIN_ID,
    BYPASS_BASE_TRAFFIC_GB,
    BYPASS_HWID_DEVICE_LIMIT,
    BYPASS_SQUAD_UUID,
    BYPASS_TRAFFIC_PACKAGES,
    BYPASS_TARIFFS,
    GB_BYTES,
    REGULAR_HWID_DEVICE_LIMIT,
    REGULAR_SQUAD_UUID,
    REGULAR_TARIFFS,
    TARIFFS,
)
from services.cryptobot import create_cryptobot_invoice, get_invoice_status, process_paid_invoice
from services.image_handler import edit_text_with_photo, send_text_with_photo
from services.remnawave import (
    remnawave_delete_all_hwid_devices,
    remnawave_delete_hwid_device,
    remnawave_get_hwid_devices,
    remnawave_get_or_create_user,
    remnawave_get_subscription_url,
    remnawave_get_user_info,
    remnawave_set_subscription_expiry,
)
from services.yookassa import create_yookassa_payment, get_payment_status, process_paid_yookassa_payment
from services.subscription_sync import refresh_subscription_expiry
from services.discounts import calculate_discounted_price, current_price
from states import UserStates


logger = logging.getLogger(__name__)

router = Router()
REFUND_WINDOW = timedelta(days=3)


def _subscription_name(subscription) -> str:
    plan_kind = subscription.get('plan_kind') if subscription.get('plan_kind') in {'regular', 'bypass'} else 'bypass'
    type_index = subscription.get('type_index') or subscription.get('slot_number')
    label = "Обычная" if plan_kind == "regular" else "С антиглушилкой"
    return f"{label} #{type_index}"


def _subscription_short_status(subscription) -> str:
    if subscription.get('generation') == 'v2' and not subscription.get('is_visible'):
        return "скрытая"
    until = subscription.get('subscription_until')
    if not until:
        return "без срока"
    if until > datetime.utcnow():
        return "активна"
    return "истекла"


def _is_bot_viewable_subscription(subscription) -> bool:
    if not subscription:
        return False
    if subscription.get('generation') == 'v2' and subscription.get('is_visible'):
        return True
    until = subscription.get('subscription_until')
    return bool(
        subscription.get('legacy_readonly')
        and until
        and until > datetime.utcnow()
    )


def _format_traffic_gb(bytes_value: int | None) -> str:
    if not bytes_value:
        return "0 ГБ"
    return f"{bytes_value / GB_BYTES:.1f} ГБ"


def _format_date(dt) -> str:
    return dt.strftime('%d.%m.%Y') if dt else 'неизвестно'


def _format_datetime(dt) -> str:
    return dt.strftime('%d.%m.%Y %H:%M') if dt else 'неизвестно'


def _payment_action_text(payment) -> str:
    return "Продление" if (payment.get("payment_target") or "new") == "renew" else "Покупка"


def _payment_subscription_name(payment) -> str:
    plan_kind = payment.get("plan_kind") or (TARIFFS.get(payment.get("tariff_code")) or {}).get("kind") or "regular"
    type_index = payment.get("type_index") or payment.get("target_slot_number") or payment.get("slot_number")
    label = "Обычная" if plan_kind == "regular" else "С антиглушилкой"
    return f"{label} #{type_index or '—'}"


def _payment_paid_at(payment) -> datetime | None:
    return payment.get("updated_at") or payment.get("created_at")


def _refund_deadline(payment) -> datetime | None:
    paid_at = _payment_paid_at(payment)
    return paid_at + REFUND_WINDOW if paid_at else None


def _refund_is_available(payment) -> bool:
    deadline = _refund_deadline(payment)
    return bool(deadline and datetime.utcnow() <= deadline and not payment.get("refund_requested_at"))


def _refund_payment_button_text(payment) -> str:
    tariff = TARIFFS.get(payment.get("tariff_code")) or {}
    days = tariff.get("days")
    date_text = _format_date(_payment_paid_at(payment))
    status = "⏳ заявка отправлена" if payment.get("refund_requested_at") else ("✅ доступен" if _refund_is_available(payment) else "❌ поздно")
    days_text = f" · {days} дн." if days else ""
    return f"{_payment_action_text(payment)} · {_payment_subscription_name(payment)}{days_text} · {date_text} · {status}"


def _refund_payment_details(payment) -> str:
    tariff = TARIFFS.get(payment.get("tariff_code")) or {}
    deadline = _refund_deadline(payment)
    return (
        f"{_payment_action_text(payment)}: <b>{_html(_payment_subscription_name(payment))}</b>\n"
        f"Тариф: <b>{_html(tariff.get('title') or payment.get('tariff_code'))}</b>\n"
        f"Сумма: <b>{float(payment.get('amount') or 0):.2f} ₽</b>\n"
        f"Оплачено: <b>{_format_datetime(_payment_paid_at(payment))}</b>\n"
        f"Возврат доступен до: <b>{_format_datetime(deadline)}</b>"
    )


def _format_device_date(value: str | None) -> str:
    if not value:
        return "неизвестно"
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%d.%m.%Y')
    except Exception:
        return value[:10]


def _device_title(device: dict) -> str:
    platform = device.get('platform') or 'Устройство'
    model = device.get('deviceModel')
    return f"{platform} • {model}" if model else platform


def _html(value: str | None) -> str:
    return html.escape(str(value or ""), quote=False)


def _device_limit_text(subscription) -> str:
    limit = subscription.get('hwid_device_limit')
    if not limit:
        limit = BYPASS_HWID_DEVICE_LIMIT if subscription.get('plan_kind') == 'bypass' else REGULAR_HWID_DEVICE_LIMIT
    return f"{limit} устройства" if limit in (2, 3, 4) else f"{limit} устройств"


async def _get_subscription_access_data(subscription) -> tuple[str | None, str, datetime | None]:
    """Получить ссылку подписки и остаток времени."""
    effective_until = subscription.get('subscription_until')
    sub_url = None

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        if subscription.get('remnawave_uuid'):
            try:
                sub_url = await remnawave_get_subscription_url(session, subscription['remnawave_uuid'])
            except Exception as e:
                logging.error(f"Error fetching subscription URL from Remnawave: {e}")
            try:
                effective_until = await refresh_subscription_expiry(subscription, session)
            except Exception as e:
                logging.error(f"Error fetching subscription expiry from Remnawave: {e}")

    remaining_str = _format_remaining(effective_until.replace(tzinfo=timezone.utc).isoformat()) if effective_until else "неизвестно"
    return sub_url, remaining_str, effective_until


def _build_instruction_text(sub_url: str) -> str:
    return (
        "📲 <b>Инструкция по подключению</b>\n\n"
        "1. Скачай <b>Happ Plus</b> из Google Play или App Store\n"
        f"2. Скопируй ключ:\n<code>{sub_url}</code>\n"
        "3. Открой Happ Plus\n"
        "4. Нажми <b>+</b> в правом верхнем углу\n"
        "5. Выбери <b>Вставить из буфера обмена</b>\n\n"
        "По всем вопросам: @wayspn_support"
    )


def _build_remnawave_username(tg_id: int, subscription_id: int) -> str:
    return f"tg_{tg_id}_{subscription_id}"


def _build_new_remnawave_username(tg_id: int, plan_kind: str, type_index: int) -> str:
    return f"tg_{tg_id}_{plan_kind}_{type_index}"


def _format_remaining(expire_at_str: str | None) -> str:
    if not expire_at_str:
        return "неизвестно"

    expire_at = datetime.fromisoformat(expire_at_str.replace('Z', '+00:00'))
    remaining = expire_at - datetime.now(timezone.utc)

    if remaining.total_seconds() <= 0:
        return "истекла"

    days = remaining.days
    hours = remaining.seconds // 3600
    minutes = (remaining.seconds % 3600) // 60
    return f"{days}д {hours}ч {minutes}м"


async def _show_tariff_selection(callback: CallbackQuery, state: FSMContext, title: str):
    data = await state.get_data()
    plan_kind = data.get("plan_kind", "regular")
    purchase_mode = data.get("purchase_mode", "new")
    tariffs = REGULAR_TARIFFS if plan_kind == "regular" else BYPASS_TARIFFS
    discounts = await db.get_active_discounts()

    keyboard = []
    for tariff_code, tariff in tariffs.items():
        pricing = calculate_discounted_price(tariff["price"], discounts, product_type="subscription", code=tariff_code, plan_kind=plan_kind)
        price_label = f"{pricing['original_price']:g}₽ → {pricing['price']:g}₽" if pricing["discount"] else f"{pricing['price']:g}₽"
        if purchase_mode == "renew":
            period = "1 месяц" if tariff["days"] == 30 else "3 месяца" if tariff["days"] == 90 else tariff["title"]
            label = f"{period} — {price_label}"
        else:
            devices_count = REGULAR_HWID_DEVICE_LIMIT if plan_kind == "regular" else BYPASS_HWID_DEVICE_LIMIT
            devices = f"{devices_count} устройства" if devices_count in (2, 3, 4) else f"{devices_count} устройств"
            label = f"{tariff['title']} — {price_label} ({devices})"
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"tariff_{tariff_code}", style="primary")])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")])
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    image_key = "Покупка обычной подписки" if plan_kind == "regular" else "Покупка подписки с антиглушилкой"
    await edit_text_with_photo(callback, title, kb, image_key)
    await state.set_state(UserStates.choosing_tariff)


async def _show_subscriptions_hub(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscriptions = await db.get_visible_subscriptions(tg_id)
    renewable_subscriptions = await db.get_renewable_subscriptions(tg_id)

    keyboard = []

    if renewable_subscriptions:
        keyboard.append([InlineKeyboardButton(text="Продлить имеющуюся подписку", callback_data="renew_existing_subscription", style="success")])

    buy_text = "Купить первую подписку" if not subscriptions else "Купить новую подписку"
    keyboard.append([InlineKeyboardButton(text=buy_text, callback_data="buy_new_subscription", style="success")])

    if not subscriptions:
        text = (
            "💳 <b>Купить / Продлить подписку</b>\n\n"
            "У тебя пока нет подписок.\n"
            "Купи первую подписку: обычную или с антиглушилкой."
        )
        image_key = "Нет подписок"
    else:
        text = (
            "💳 <b>Купить / Продлить подписку</b>\n\n"
            f"Активных новых подписок: <b>{len(subscriptions)}</b>.\n"
            "Можно продлить существующую или купить новую."
        )
        image_key = "Выбор покупки или продления"

    keyboard.append([InlineKeyboardButton(text="Закрыть", callback_data="back_to_menu", style="danger")])
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await state.clear()
    await edit_text_with_photo(callback, text, kb, image_key)


async def _show_my_subscriptions_type_choice(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscriptions = await db.get_bot_visible_subscriptions(tg_id)

    if not subscriptions:
        await callback.answer("У тебя пока нет подписок", show_alert=True)
        return

    has_regular = any((subscription.get('plan_kind') or 'regular') == 'regular' for subscription in subscriptions)
    has_bypass = any(subscription.get('plan_kind') == 'bypass' for subscription in subscriptions)

    keyboard = []
    if has_regular:
        keyboard.append([InlineKeyboardButton(text="Обычные подписки", callback_data="my_subscriptions_regular", style="primary")])
    if has_bypass:
        keyboard.append([InlineKeyboardButton(text="Подписки с антиглушилкой", callback_data="my_subscriptions_bypass", style="primary")])
    keyboard.append([InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo", style="primary")])
    keyboard.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu", style="danger")])

    await state.clear()
    await edit_text_with_photo(
        callback,
        "🔐 <b>Мои подписки</b>\n\nВыбери тип подписок:",
        InlineKeyboardMarkup(inline_keyboard=keyboard),
        "Мои подписки",
    )


async def _show_my_subscriptions_by_kind(callback: CallbackQuery, plan_kind: str):
    tg_id = callback.from_user.id
    subscriptions = [
        subscription
        for subscription in await db.get_bot_visible_subscriptions(tg_id)
        if (subscription.get('plan_kind') or 'regular') == plan_kind
    ]

    if not subscriptions:
        await callback.answer("Подписок этого типа нет", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton(text=f"Подписка #{subscription.get('type_index') or subscription['slot_number']}", callback_data=f"my_subscription_view_{subscription['id']}", style="primary")]
        for subscription in subscriptions
    ]
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="my_subscriptions", style="danger")])

    title = "Обычные подписки" if plan_kind == "regular" else "Подписки с антиглушилкой"
    await edit_text_with_photo(
        callback,
        f"🔐 <b>{title}</b>\n\nВыбери подписку:",
        InlineKeyboardMarkup(inline_keyboard=keyboard),
        "Мои подписки",
    )


async def _send_refund_payment_choice(message: Message):
    tg_id = message.from_user.id
    payments = await db.list_subscription_payments_for_refund(tg_id)

    if not payments:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_subscription", style="success")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu", style="danger")],
        ])
        await send_text_with_photo(
            message,
            "↩️ <b>Оформить возврат</b>\n\n"
            "У тебя пока нет оплаченных покупок или продлений подписки, по которым можно выбрать возврат.",
            kb,
            "Возврат",
        )
        return

    keyboard = [
        [InlineKeyboardButton(text=_refund_payment_button_text(payment), callback_data=f"refund_payment_{payment['id']}", style="primary")]
        for payment in payments
    ]
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu", style="danger")])

    await send_text_with_photo(
        message,
        "↩️ <b>Оформить возврат</b>\n\n"
        "Выбери покупку или продление подписки, по которому хочешь оформить возврат.\n\n"
        "Возврат можно оформить только в течение <b>3 суток</b> после покупки или последнего продления.",
        InlineKeyboardMarkup(inline_keyboard=keyboard),
        "Возврат",
    )


async def _show_subscription_card(callback: CallbackQuery, subscription_id: int, *, back_callback: str = "buy_subscription"):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not _is_bot_viewable_subscription(subscription):
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    sub_url, remaining_str, effective_until = await _get_subscription_access_data(subscription)
    plan_title = 'С антиглушилкой' if subscription.get('plan_kind') == 'bypass' else 'Обычная'
    display_subscription = {**subscription, 'subscription_until': effective_until}
    status_text = _subscription_short_status(display_subscription)
    until_text = _format_date(effective_until)
    limit_text = _device_limit_text(subscription)
    traffic_text = ""

    if subscription.get('plan_kind') == 'bypass' and not subscription.get('legacy_readonly'):
        used_bytes = subscription.get('last_known_used_traffic_bytes') or 0
        try:
            if subscription.get('remnawave_uuid'):
                connector = aiohttp.TCPConnector(ssl=False)
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    user_info = await remnawave_get_user_info(session, subscription['remnawave_uuid'])
                    used_bytes = (user_info.get('userTraffic') or {}).get('usedTrafficBytes') or used_bytes if user_info else used_bytes
        except Exception as e:
            logging.warning(f"Failed to fetch traffic for subscription {subscription_id}: {e}")

        limit_bytes = subscription.get('current_period_limit_bytes') or subscription.get('base_traffic_bytes') or 0
        reset_at = subscription.get('traffic_reset_at')
        reset_text = _format_date(reset_at)
        traffic_text = (
            f"\n📦 Трафик антиглушилки: <b>{_format_traffic_gb(used_bytes)} / {_format_traffic_gb(limit_bytes)}</b>\n"
            f"🔄 Сброс трафика: <b>{reset_text}</b>"
        )

    keyboard = [
        [InlineKeyboardButton(text="📲 Инструкция", callback_data=f"subscription_instruction_{subscription_id}", style="primary")],
        [InlineKeyboardButton(text="📱 Устройства", callback_data=f"subscription_devices_{subscription_id}", style="primary")],
    ]
    if subscription.get('generation') == 'v2' and subscription.get('is_renewable'):
        keyboard.append([InlineKeyboardButton(text="🔄 Продлить эту подписку", callback_data=f"renew_subscription_{subscription_id}", style="success")])
    if (
        subscription.get('generation') == 'v2'
        and subscription.get('is_renewable')
        and subscription.get('plan_kind') == 'bypass'
        and status_text == 'активна'
    ):
        keyboard.append([InlineKeyboardButton(text="📦 Купить ГБ", callback_data=f"gb_sub_{subscription_id}", style="success")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback, style="danger")])
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    text = (
        f"🔐 <b>{_subscription_name(subscription)}</b>\n\n"
        "<blockquote>"
        f"📍 Статус: <b>{status_text}</b>\n"
        f"📆 Срок: <b>до {until_text}</b>, осталось <b>{remaining_str}</b>\n"
        f"🌐 Тип: <b>{plan_title}</b>\n"
        f"🧩 Лимит: <b>{limit_text}</b>"
        f"{traffic_text}"
        "</blockquote>\n\n"
        "<b>Ваш ключ:</b>\n"
        f"{sub_url or '<i>Ошибка получения ссылки</i>'}"
    )

    await edit_text_with_photo(callback, text, kb, "Моя подписка")


async def _show_subscription_devices(callback: CallbackQuery, subscription_id: int):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not _is_bot_viewable_subscription(subscription):
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    if not subscription.get('remnawave_uuid'):
        await callback.answer("У ключа пока нет UUID Remnawave", show_alert=True)
        return

    devices = []
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            devices = await remnawave_get_hwid_devices(session, subscription['remnawave_uuid']) or []
    except Exception as e:
        logging.error(f"Failed to get devices for subscription {subscription_id}: {e}")
        await callback.answer("Не удалось загрузить устройства", show_alert=True)
        return

    keyboard = []
    text_lines = [
        f"📱 <b>Устройства {_subscription_name(subscription)}</b>",
        "",
    ]

    if devices:
        for index, device in enumerate(devices, start=1):
            hwid = device.get('hwid') or ''
            title = _device_title(device)
            created_at = _format_device_date(device.get('createdAt'))
            text_lines.append(f"{index}. <b>{_html(title)}</b>")
            text_lines.append(f"   Подключено: {_html(created_at)}")
            if hwid:
                text_lines.append(f"   HWID: <code>{_html(hwid)}</code>")
                keyboard.append([InlineKeyboardButton(text=f"Удалить {index}. {title}", callback_data=f"device_delete_{subscription_id}_{index - 1}", style="danger")])
            text_lines.append("")
        keyboard.append([InlineKeyboardButton(text="🧹 Удалить все устройства", callback_data=f"device_delete_all_{subscription_id}", style="danger")])
    else:
        text_lines.append("Подключённых устройств пока нет.")

    keyboard.append([InlineKeyboardButton(text="🔙 Назад к подписке", callback_data=f"subscription_view_{subscription_id}", style="primary")])

    await edit_text_with_photo(
        callback,
        "\n".join(text_lines),
        InlineKeyboardMarkup(inline_keyboard=keyboard),
        "Моя подписка",
    )


async def _delete_subscription_device(callback: CallbackQuery, subscription_id: int, device_index: int):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not _is_bot_viewable_subscription(subscription) or not subscription.get('remnawave_uuid'):
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        devices = await remnawave_get_hwid_devices(session, subscription['remnawave_uuid']) or []
        if device_index < 0 or device_index >= len(devices) or not devices[device_index].get('hwid'):
            await callback.answer("Устройство не найдено", show_alert=True)
            return

        deleted = await remnawave_delete_hwid_device(session, subscription['remnawave_uuid'], devices[device_index]['hwid'])

    if not deleted:
        await callback.answer("Не удалось удалить устройство", show_alert=True)
        return

    await callback.answer("Устройство удалено")
    await _show_subscription_devices(callback, subscription_id)


async def _delete_all_subscription_devices(callback: CallbackQuery, subscription_id: int):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not _is_bot_viewable_subscription(subscription) or not subscription.get('remnawave_uuid'):
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        deleted = await remnawave_delete_all_hwid_devices(session, subscription['remnawave_uuid'])

    if not deleted:
        await callback.answer("Не удалось удалить устройства", show_alert=True)
        return

    await callback.answer("Все устройства удалены")
    await _show_subscription_devices(callback, subscription_id)


async def _show_subscription_instruction(callback: CallbackQuery, subscription_id: int):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not _is_bot_viewable_subscription(subscription):
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    sub_url, _, _ = await _get_subscription_access_data(subscription)
    if not sub_url:
        await callback.answer("Не удалось получить ключ подписки", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Открыть эту подписку", callback_data=f"subscription_view_{subscription_id}", style="primary")],
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu", style="danger")],
    ])

    await edit_text_with_photo(
        callback,
        _build_instruction_text(sub_url),
        kb,
        "Как подключиться",
    )


async def _get_or_create_target_subscription_for_direct_flow(tg_id: int, state_data: dict):
    purchase_mode = state_data.get("purchase_mode", "new")
    target_subscription_id = state_data.get("target_subscription_id")
    target_slot_number = state_data.get("target_slot_number")
    plan_kind = state_data.get("plan_kind", "regular")
    type_index = state_data.get("type_index")
    tariff_code = state_data.get("tariff_code")
    tariff = TARIFFS.get(tariff_code, {}) if tariff_code else {}

    if purchase_mode == "renew":
        subscription = await db.get_subscription_by_id(target_subscription_id, tg_id)
        if (
            not subscription
            or subscription.get("generation") != "v2"
            or not subscription.get("is_visible")
            or not subscription.get("is_renewable")
        ):
            return None, purchase_mode
        return subscription, purchase_mode

    if type_index is None:
        type_index = await db.get_next_type_index(tg_id, plan_kind)

    if type_index is None:
        return None, purchase_mode

    storage_slot = await db.get_next_subscription_slot(tg_id)
    if storage_slot is None:
        return None, purchase_mode

    subscription = await db.create_subscription_record(
        tg_id,
        storage_slot,
        plan_kind=plan_kind,
        type_index=type_index,
        generation="v2",
        is_visible=True,
        is_renewable=True,
        purchase_days=tariff.get("days"),
    )

    return subscription, purchase_mode


@router.callback_query(F.data.in_({"buy_subscription", "my_subscription"}))
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext):
    """Показать список подписок пользователя."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened subscriptions hub")
    await _show_subscriptions_hub(callback, state)


@router.callback_query(F.data == "my_subscriptions")
async def process_my_subscriptions(callback: CallbackQuery, state: FSMContext):
    """Показать выбор типа подписок пользователя."""
    await _show_my_subscriptions_type_choice(callback, state)


@router.message(Command("refund"))
async def process_refund_command(message: Message, state: FSMContext):
    """Показать покупки, по которым пользователь может запросить возврат."""
    tg_id = message.from_user.id
    logging.info(f"User {tg_id} opened refund flow")
    await state.clear()
    await _send_refund_payment_choice(message)


@router.callback_query(F.data.startswith("refund_payment_"))
async def process_refund_payment(callback: CallbackQuery):
    """Оформить заявку на возврат по выбранному платежу."""
    tg_id = callback.from_user.id
    payment_id = int(callback.data.split("_")[-1])
    payment = await db.get_subscription_payment_for_refund(payment_id, tg_id)

    if not payment:
        await callback.answer("Покупка не найдена", show_alert=True)
        return

    if payment.get("refund_requested_at"):
        await callback.answer("Заявка на возврат по этой покупке уже отправлена.", show_alert=True)
        return

    if not _refund_is_available(payment):
        await callback.answer(
            "Возврат за эту подписку сделать нельзя, так как после покупки / продления прошло 3 суток.",
            show_alert=True,
        )
        return

    if not await db.request_payment_refund(payment_id, tg_id):
        await callback.answer("Не удалось оформить заявку. Попробуй ещё раз.", show_alert=True)
        return

    details = _refund_payment_details(payment)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu", style="danger")],
    ])
    await edit_text_with_photo(
        callback,
        "✅ <b>Заявка на возврат отправлена</b>\n\n"
        f"{details}\n\n"
        "Мы проверим покупку и свяжемся с тобой по результату.",
        kb,
        "Возврат",
    )

    if ADMIN_ID:
        try:
            await callback.bot.send_message(
                ADMIN_ID,
                "↩️ <b>Новая заявка на возврат</b>\n\n"
                f"Пользователь: <code>{tg_id}</code>\n"
                f"{details}\n"
                f"Платёж: <code>{_html(payment.get('invoice_id'))}</code>",
            )
        except Exception as exc:
            logger.warning("Failed to notify admin about refund request %s: %s", payment_id, exc)


@router.callback_query(F.data.in_({"my_subscriptions_regular", "my_subscriptions_bypass"}))
async def process_my_subscriptions_kind(callback: CallbackQuery):
    """Показать подписки выбранного типа."""
    plan_kind = callback.data.removeprefix("my_subscriptions_")
    await _show_my_subscriptions_by_kind(callback, plan_kind)


@router.callback_query(F.data == "buy_new_subscription")
async def process_buy_new_subscription(callback: CallbackQuery, state: FSMContext):
    """Начать покупку новой подписки."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обычная (Без антиглушилки)", callback_data="plan_regular", style="primary")],
        [InlineKeyboardButton(text="С антиглушилкой", callback_data="plan_bypass", style="success")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
    ])
    text = "Выбери тип новой подписки:"
    await state.update_data(purchase_mode="new", target_subscription_id=None, target_slot_number=None)
    await edit_text_with_photo(callback, text, kb, "Выбор типа подписки")


@router.callback_query(F.data.startswith("plan_"))
async def process_plan_choice(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    plan_kind = callback.data.split("_", 1)[1]

    if plan_kind not in {"regular", "bypass"}:
        await callback.answer("Неизвестный тип подписки", show_alert=True)
        return

    type_index = await db.get_next_type_index(tg_id, plan_kind)
    if type_index is None:
        plan_name = "обычных" if plan_kind == "regular" else "подписок с антиглушилкой"
        await callback.answer(f"У тебя уже максимум 3 {plan_name}", show_alert=True)
        return

    await state.update_data(
        purchase_mode="new",
        plan_kind=plan_kind,
        type_index=type_index,
        target_subscription_id=None,
        target_slot_number=type_index,
    )
    plan_title = "обычной подписки" if plan_kind == "regular" else "подписки с антиглушилкой"
    await _show_tariff_selection(callback, state, f"Выбери срок для {plan_title} #{type_index}:")


@router.callback_query(F.data == "renew_existing_subscription")
async def process_renew_existing_subscription(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscriptions = await db.get_renewable_subscriptions(tg_id)

    if not subscriptions:
        await callback.answer("Нет подписок для продления", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton(text=f"{_subscription_name(subscription)} • {_subscription_short_status(subscription)}", callback_data=f"renew_subscription_{subscription['id']}", style="success")]
        for subscription in subscriptions
    ]
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")])

    await edit_text_with_photo(
        callback,
        "Выбери подписку, которую хочешь продлить:",
        InlineKeyboardMarkup(inline_keyboard=keyboard),
        "Мои подписки",
    )


@router.callback_query(F.data.startswith("subscription_view_"))
async def process_subscription_view(callback: CallbackQuery, state: FSMContext):
    subscription_id = int(callback.data.split("_")[-1])
    await _show_subscription_card(callback, subscription_id)


@router.callback_query(F.data.startswith("my_subscription_view_"))
async def process_my_subscription_view(callback: CallbackQuery, state: FSMContext):
    subscription_id = int(callback.data.split("_")[-1])
    subscription = await db.get_subscription_by_id(subscription_id, callback.from_user.id)
    plan_kind = (subscription.get('plan_kind') or 'regular') if subscription else 'regular'
    await _show_subscription_card(callback, subscription_id, back_callback=f"my_subscriptions_{plan_kind}")


@router.callback_query(F.data.startswith("subscription_instruction_"))
async def process_subscription_instruction(callback: CallbackQuery, state: FSMContext):
    subscription_id = int(callback.data.split("_")[-1])
    await _show_subscription_instruction(callback, subscription_id)


@router.callback_query(F.data.startswith("subscription_devices_"))
async def process_subscription_devices(callback: CallbackQuery, state: FSMContext):
    subscription_id = int(callback.data.split("_")[-1])
    await _show_subscription_devices(callback, subscription_id)


@router.callback_query(F.data.startswith("device_delete_all_"))
async def process_device_delete_all(callback: CallbackQuery, state: FSMContext):
    subscription_id = int(callback.data.split("_")[-1])
    await _delete_all_subscription_devices(callback, subscription_id)


@router.callback_query(F.data.startswith("device_delete_"))
async def process_device_delete(callback: CallbackQuery, state: FSMContext):
    if callback.data.startswith("device_delete_all_"):
        return
    parts = callback.data.split("_")
    subscription_id = int(parts[-2])
    device_index = int(parts[-1])
    await _delete_subscription_device(callback, subscription_id, device_index)


@router.callback_query(F.data.startswith("renew_subscription_"))
async def process_subscription_renew(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscription_id = int(callback.data.split("_")[-1])
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if (
        not subscription
        or subscription.get('generation') != 'v2'
        or not subscription.get('is_visible')
        or not subscription.get('is_renewable')
    ):
        await callback.answer("Эту подписку нельзя продлить", show_alert=True)
        return

    await state.update_data(
        purchase_mode="renew",
        plan_kind=subscription.get('plan_kind') or 'regular',
        target_subscription_id=subscription_id,
        target_slot_number=subscription['slot_number'],
        type_index=subscription.get('type_index'),
    )
    await _show_tariff_selection(callback, state, f"Выбери срок для продления {_subscription_name(subscription).lower()}:")


@router.callback_query(F.data == "buy_gb")
async def process_buy_gb(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscriptions = await db.get_active_bypass_subscriptions(tg_id)

    if not subscriptions:
        await callback.answer("Нет активных подписок с антиглушилкой", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton(text=_subscription_name(subscription), callback_data=f"gb_sub_{subscription['id']}", style="primary")]
        for subscription in subscriptions
    ]
    keyboard.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu", style="danger")])

    await edit_text_with_photo(
        callback,
        "📦 <b>Купить ГБ</b>\n\nВыбери подписку с антиглушилкой:",
        InlineKeyboardMarkup(inline_keyboard=keyboard),
        "Мои подписки",
    )


@router.callback_query(F.data.startswith("gb_sub_"))
async def process_gb_subscription_choice(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscription_id = int(callback.data.split("_")[-1])
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if (
        not subscription
        or subscription.get('generation') != 'v2'
        or not subscription.get('is_visible')
        or not subscription.get('is_renewable')
        or subscription.get('plan_kind') != 'bypass'
    ):
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    discounts = await db.get_active_discounts()
    keyboard = []
    for package_code, package in BYPASS_TRAFFIC_PACKAGES.items():
        pricing = calculate_discounted_price(package["price"], discounts, product_type="traffic", code=package_code, plan_kind="bypass")
        price_label = f"{pricing['original_price']:g}₽ → {pricing['price']:g}₽" if pricing["discount"] else f"{pricing['price']:g}₽"
        keyboard.append([
            InlineKeyboardButton(
                text=f"{package['gb']} ГБ — {price_label}",
                callback_data=f"gb_package_{package_code}",
                style="success",
            )
        ])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="buy_gb", style="danger")])

    await state.update_data(gb_subscription_id=subscription_id)
    await edit_text_with_photo(
        callback,
        f"📦 <b>{_subscription_name(subscription)}</b>\n\nВыбери пакет трафика:",
        InlineKeyboardMarkup(inline_keyboard=keyboard),
        "Мои подписки",
    )


@router.callback_query(F.data.startswith("gb_package_"))
async def process_gb_package_choice(callback: CallbackQuery, state: FSMContext):
    package_code = callback.data.removeprefix("gb_package_")
    package = BYPASS_TRAFFIC_PACKAGES.get(package_code)

    if not package:
        await callback.answer("Пакет не найден", show_alert=True)
        return

    pricing = await current_price(package["price"], product_type="traffic", code=package_code, plan_kind="bypass")
    await state.update_data(gb_package_code=package_code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_gb_cryptobot", style="success")],
        [InlineKeyboardButton(text="💳 Оплатить картой", callback_data="pay_gb_yookassa", style="success")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_gb", style="danger")],
    ])
    await edit_text_with_photo(
        callback,
        f"📦 <b>Пакет {package['gb']} ГБ</b>\n\nСумма: <b>{pricing['price']:g}₽</b>\n\nВыбери способ оплаты:",
        kb,
        "Выбери способ оплаты",
    )


async def _create_gb_payment(callback: CallbackQuery, state: FSMContext, provider: str):
    tg_id = callback.from_user.id
    data = await state.get_data()
    subscription_id = data.get("gb_subscription_id")
    package_code = data.get("gb_package_code")
    package = BYPASS_TRAFFIC_PACKAGES.get(package_code)

    if not subscription_id or not package:
        await callback.answer("Не выбран пакет трафика", show_alert=True)
        await state.clear()
        return

    subscription = await db.get_subscription_by_id(subscription_id, tg_id)
    if (
        not subscription
        or subscription.get('generation') != 'v2'
        or not subscription.get('is_visible')
        or not subscription.get('is_renewable')
        or subscription.get('plan_kind') != 'bypass'
    ):
        await callback.answer("Подписка не найдена", show_alert=True)
        await state.clear()
        return

    amount = (await current_price(package['price'], product_type="traffic", code=package_code, plan_kind="bypass"))["price"]

    if provider == "cryptobot":
        invoice = await create_cryptobot_invoice(callback.bot, amount, package_code, tg_id)
        if not invoice:
            await callback.answer("Ошибка создания счёта", show_alert=True)
            return
        invoice_id = invoice["invoice_id"]
        pay_url = invoice["bot_invoice_url"]
    else:
        payment = await create_yookassa_payment(callback.bot, amount, package_code, tg_id)
        if not payment:
            await callback.answer("Ошибка создания платежа", show_alert=True)
            return
        invoice_id = payment["id"]
        pay_url = payment.get("confirmation", {}).get("confirmation_url", "")

    await db.create_payment(
        tg_id,
        package_code,
        amount,
        provider,
        invoice_id,
        subscription_id=subscription_id,
        payment_target="traffic",
        payment_kind="traffic_package",
        traffic_package_code=package_code,
    )
    await db.create_traffic_purchase(
        subscription_id,
        package_code,
        package['gb'] * GB_BYTES,
        amount,
        provider,
        invoice_id,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url, style="success")],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment", style="primary")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_gb", style="danger")],
    ])
    await edit_text_with_photo(
        callback,
        f"📦 <b>Счёт на {package['gb']} ГБ</b>\n\nСумма: {amount}₽",
        kb,
        "Оплати",
    )
    await state.clear()


@router.callback_query(F.data == "pay_gb_cryptobot")
async def process_pay_gb_cryptobot(callback: CallbackQuery, state: FSMContext):
    await _create_gb_payment(callback, state, "cryptobot")


@router.callback_query(F.data == "pay_gb_yookassa")
async def process_pay_gb_yookassa(callback: CallbackQuery, state: FSMContext):
    await _create_gb_payment(callback, state, "yookassa")


@router.callback_query(F.data.startswith("tariff_"))
async def process_tariff_choice(callback: CallbackQuery, state: FSMContext):
    """Обработать выбор тарифа."""
    tg_id = callback.from_user.id
    tariff_code = callback.data.removeprefix("tariff_")
    logging.info(f"User {tg_id} selected tariff: {tariff_code}")

    data = await state.get_data()
    purchase_mode = data.get("purchase_mode", "new")
    target_slot_number = data.get("target_slot_number")
    target_subscription_id = data.get("target_subscription_id")

    await state.update_data(tariff_code=tariff_code)

    tariff = TARIFFS[tariff_code]
    pricing = await current_price(tariff["price"], product_type="subscription", code=tariff_code, plan_kind=tariff["kind"])
    stats = await db.get_referral_stats(tg_id)
    referral_balance = stats['current_balance']

    if purchase_mode == "renew" and target_slot_number:
        purchase_text = f"Продление подписки #{target_slot_number}"
    elif target_slot_number:
        purchase_text = f"Новая подписка #{target_slot_number}"
    else:
        purchase_text = "Новая подписка"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_cryptobot", style="success")],
        [InlineKeyboardButton(text="💳 Оплатить картой", callback_data="pay_yookassa", style="success")],
        [InlineKeyboardButton(text="💰 Оплатить с баланса от рефералов", callback_data="pay_referral_balance", style="success")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=(f"subscription_view_{target_subscription_id}" if purchase_mode == "renew" and target_subscription_id else "buy_subscription"), style="danger")],
    ])

    text = (
        f"<b>{purchase_text}</b>\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {pricing['price']:g} ₽\n"
        f"Баланс от рефералов: {referral_balance:.2f} ₽\n\n"
        "Выбери способ оплаты:"
    )

    await edit_text_with_photo(callback, text, kb, "Выбери способ оплаты")
    await state.set_state(UserStates.choosing_payment)


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий счёт в CryptoBot."""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    purchase_mode = data.get("purchase_mode", "new")
    target_subscription_id = data.get("target_subscription_id")
    target_slot_number = data.get("target_slot_number")
    type_index = data.get("type_index")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    if purchase_mode == "new" and target_slot_number is None:
        target_slot_number = type_index
        await state.update_data(target_slot_number=target_slot_number)

    tariff = TARIFFS[tariff_code]
    amount = (await current_price(tariff["price"], product_type="subscription", code=tariff_code, plan_kind=tariff["kind"]))["price"]

    existing_invoice_id = await db.get_active_payment_for_user_and_tariff(
        tg_id,
        tariff_code,
        "cryptobot",
        amount=amount,
        subscription_id=target_subscription_id,
        payment_target=purchase_mode,
        target_slot_number=target_slot_number,
    )

    if existing_invoice_id:
        invoice = await get_invoice_status(existing_invoice_id)
        if invoice and invoice.get("status") == "active":
            pay_url = invoice.get("bot_invoice_url", "")
            if pay_url:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url, style="success")],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment", style="primary")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
                ])
                await edit_text_with_photo(callback, "<b>Счёт на оплату (существующий)</b>", kb, "Оплати")
                await state.clear()
                return

    invoice = await create_cryptobot_invoice(callback.bot, amount, tariff_code, tg_id)
    if not invoice:
        await callback.answer("Ошибка создания счёта в CryptoBot. Попробуй позже.", show_alert=True)
        await state.clear()
        return

    invoice_id = invoice["invoice_id"]
    pay_url = invoice["bot_invoice_url"]

    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        "cryptobot",
        invoice_id,
        subscription_id=target_subscription_id,
        payment_target=purchase_mode,
        target_slot_number=target_slot_number,
    )

    target_text = f"подписки #{target_slot_number}" if purchase_mode == "new" else f"подписки #{data.get('target_slot_number')}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=pay_url, style="success")],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment", style="primary")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
    ])
    text = (
        f"<b>Счёт на оплату {target_text}</b>\n\n"
        f"Тариф: {tariff_code}\n"
        f"Сумма: {amount} ₽\n\n"
        "Оплати через CryptoBot. После оплаты бот автоматически активирует подписку.\n"
        "Если не активировалось, нажми «Проверить оплату»."
    )
    await edit_text_with_photo(callback, text, kb, "Оплати")
    await state.clear()


@router.callback_query(F.data == "pay_yookassa")
async def process_pay_yookassa(callback: CallbackQuery, state: FSMContext):
    """Создать или вернуть существующий платёж через Yookassa."""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")
    purchase_mode = data.get("purchase_mode", "new")
    target_subscription_id = data.get("target_subscription_id")
    target_slot_number = data.get("target_slot_number")
    type_index = data.get("type_index")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    if purchase_mode == "new" and target_slot_number is None:
        target_slot_number = type_index
        await state.update_data(target_slot_number=target_slot_number)

    tariff = TARIFFS[tariff_code]
    amount = (await current_price(tariff["price"], product_type="subscription", code=tariff_code, plan_kind=tariff["kind"]))["price"]

    existing_payment_id = await db.get_active_payment_for_user_and_tariff(
        tg_id,
        tariff_code,
        "yookassa",
        amount=amount,
        subscription_id=target_subscription_id,
        payment_target=purchase_mode,
        target_slot_number=target_slot_number,
    )

    if existing_payment_id:
        payment = await get_payment_status(existing_payment_id)
        if payment and payment.get("status") == "pending":
            confirmation_url = payment.get("confirmation", {}).get("confirmation_url", "")
            if confirmation_url:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url, style="success")],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment", style="primary")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
                ])
                await edit_text_with_photo(callback, "<b>💳 Yookassa (существующий платёж)</b>", kb, "Оплати")
                await state.clear()
                return

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

    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        "yookassa",
        payment_id,
        subscription_id=target_subscription_id,
        payment_target=purchase_mode,
        target_slot_number=target_slot_number,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить сейчас", url=confirmation_url, style="success")],
        [InlineKeyboardButton(text="Проверить оплату", callback_data="check_payment", style="primary")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_subscription", style="danger")],
    ])
    await edit_text_with_photo(callback, "<b>💳 Yookassa</b>", kb, "Оплати")
    await state.clear()


@router.callback_query(F.data == "pay_referral_balance")
async def process_pay_referral_balance(callback: CallbackQuery, state: FSMContext):
    """Оплатить подписку с баланса рефералов."""
    tg_id = callback.from_user.id
    data = await state.get_data()
    tariff_code = data.get("tariff_code")

    if not tariff_code:
        await callback.answer("Ошибка: тариф не выбран", show_alert=True)
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    amount = (await current_price(tariff["price"], product_type="subscription", code=tariff_code, plan_kind=tariff["kind"]))["price"]

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        stats = await db.get_referral_stats(tg_id)
        referral_balance = stats["current_balance"]

        if referral_balance < amount:
            missing = amount - referral_balance
            await callback.answer(f"Не хватает {missing:.2f} ₽ на балансе рефералов", show_alert=True)
            return

        subscription, purchase_mode = await _get_or_create_target_subscription_for_direct_flow(tg_id, data)
        if not subscription:
            await callback.answer("Не удалось определить целевую подписку", show_alert=True)
            return

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            plan_kind = subscription.get("plan_kind") or tariff.get("kind", "regular")
            squad_uuid = REGULAR_SQUAD_UUID if plan_kind == "regular" else BYPASS_SQUAD_UUID
            device_limit = REGULAR_HWID_DEVICE_LIMIT if plan_kind == "regular" else BYPASS_HWID_DEVICE_LIMIT
            base_traffic_bytes = BYPASS_BASE_TRAFFIC_GB * GB_BYTES if plan_kind == "bypass" else 0
            traffic_limit_bytes = max(
                subscription.get("current_period_limit_bytes") or 0,
                base_traffic_bytes + (subscription.get("carried_traffic_bytes") or 0) + (subscription.get("current_paid_traffic_bytes") or 0),
            ) if plan_kind == "bypass" else 0
            remna_username = subscription.get("remnawave_username") or _build_new_remnawave_username(
                tg_id,
                plan_kind,
                subscription.get('type_index') or subscription['id'],
            )
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                tariff["days"],
                extend_if_exists=purchase_mode == "renew" and bool(subscription.get("remnawave_uuid")),
                remna_username=remna_username,
                traffic_limit_bytes=traffic_limit_bytes,
                traffic_limit_strategy="NO_RESET",
                active_internal_squads=[squad_uuid],
                hwid_device_limit=device_limit,
                telegram_id=tg_id,
            )

            if not uuid:
                await callback.answer("Ошибка получения доступа в VPN. Попробуй позже.", show_alert=True)
                return

            sub_url = await remnawave_get_subscription_url(session, uuid)
            if not sub_url:
                logging.warning(f"Failed to get subscription URL for {uuid}")

        existing_subscription = subscription.get("subscription_until")
        now = datetime.utcnow()
        if existing_subscription and existing_subscription > now:
            new_until = existing_subscription + timedelta(days=tariff["days"])
        else:
            new_until = now + timedelta(days=tariff["days"])

        traffic_reset_at = None
        if plan_kind == "bypass":
            traffic_reset_at = subscription.get("traffic_reset_at") if existing_subscription and existing_subscription > now else None
            traffic_reset_at = traffic_reset_at or now + timedelta(days=30)

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            if not await remnawave_set_subscription_expiry(session, uuid, new_until):
                logging.warning(f"Failed to sync Remnawave expiry for referral balance subscription {subscription['id']}")

        await db.update_subscription_record(
            subscription['id'],
            uuid,
            username,
            new_until,
            squad_uuid,
        )
        await db.db_execute(
            """
            UPDATE subscriptions
            SET plan_kind = $1,
                generation = 'v2',
                is_visible = TRUE,
                is_renewable = TRUE,
                traffic_enabled = $2,
                base_traffic_bytes = $3,
                current_period_limit_bytes = $4,
                traffic_reset_at = $5,
                hwid_device_limit = $6,
                last_traffic_sync_at = now(),
                purchase_days = $7
            WHERE id = $8
            """,
            (
                plan_kind,
                plan_kind == "bypass",
                base_traffic_bytes,
                traffic_limit_bytes,
                traffic_reset_at,
                device_limit,
                tariff["days"],
                subscription['id'],
            )
        )
        await db.spend_referral_balance_for_subscription(tg_id, amount, tariff_code)

        remaining_balance = referral_balance - amount
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu", style="danger")],
        ])

        text = (
            f"✅ <b>{_subscription_name(subscription)} оплачена с баланса рефералов!</b>\n\n"
            f"Тариф: {tariff_code} ({tariff['days']} дней)\n"
            f"Списано: {amount} ₽\n"
            f"Остаток баланса: {remaining_balance:.2f} ₽\n\n"
            f"<b>Ваш ключ:</b>\n{sub_url or 'Ошибка получения ссылки'}"
        )

        await edit_text_with_photo(callback, text, kb, "Оплати")
        await state.clear()

    except Exception as e:
        logging.error(f"Referral balance payment error: {e}", exc_info=True)
        await callback.answer("Ошибка при оплате с баланса рефералов", show_alert=True)
    finally:
        await db.release_user_lock(tg_id)


@router.callback_query(F.data == "check_payment")
async def process_check_payment(callback: CallbackQuery):
    """Проверить статус последнего ожидающего платежа."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} checking payment status")

    can_check, error_msg = await db.can_check_payment(tg_id)
    if not can_check:
        await callback.answer(error_msg, show_alert=True)
        return

    await db.update_last_payment_check(tg_id)

    result = await db.get_last_pending_payment(tg_id)
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
            payment = await get_payment_status(invoice_id)
            if payment and payment.get("status") == "succeeded":
                success = await process_paid_yookassa_payment(callback.bot, tg_id, invoice_id, tariff_code)
                await callback.answer("✅ Оплата подтверждена!" if success else "Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)
        elif provider == "cryptobot":
            invoice = await get_invoice_status(invoice_id)
            if invoice and invoice.get("status") == "paid":
                success = await process_paid_invoice(callback.bot, tg_id, invoice_id, tariff_code)
                await callback.answer("✅ Оплата подтверждена!" if success else "Ошибка при активации подписки", show_alert=True)
            else:
                await callback.answer("Оплата ещё не прошла или уже активирована", show_alert=True)
    except Exception as e:
        logging.error(f"Check payment error: {e}", exc_info=True)
        await callback.answer("Ошибка при проверке платежа", show_alert=True)
    finally:
        await db.release_user_lock(tg_id)
