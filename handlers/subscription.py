import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from config import (
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
from services.image_handler import edit_text_with_photo
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_get_subscription_url,
    remnawave_get_user_info,
)
from services.yookassa import create_yookassa_payment, get_payment_status, process_paid_yookassa_payment
from states import UserStates


logger = logging.getLogger(__name__)

router = Router()


def _subscription_name(subscription) -> str:
    plan_kind = subscription.get('plan_kind') or 'regular'
    type_index = subscription.get('type_index') or subscription.get('slot_number')
    label = "Обычная" if plan_kind == "regular" else "С антиглушилкой"
    return f"{label} #{type_index}"


def _subscription_short_status(subscription) -> str:
    if subscription.get('generation') != 'v2':
        return "архивная"
    if not subscription.get('is_visible'):
        return "скрытая"
    until = subscription.get('subscription_until')
    if not until:
        return "без срока"
    if until > datetime.utcnow():
        return "активна"
    return "истекла"


def _format_traffic_gb(bytes_value: int | None) -> str:
    if not bytes_value:
        return "0 ГБ"
    return f"{bytes_value / GB_BYTES:.1f} ГБ"


def _format_date(dt) -> str:
    return dt.strftime('%d.%m.%Y') if dt else 'неизвестно'


def _device_limit_text(subscription) -> str:
    limit = subscription.get('hwid_device_limit')
    if not limit:
        limit = BYPASS_HWID_DEVICE_LIMIT if subscription.get('plan_kind') == 'bypass' else REGULAR_HWID_DEVICE_LIMIT
    return f"{limit} устройства" if limit in (2, 3, 4) else f"{limit} устройств"


async def _get_subscription_access_data(subscription) -> tuple[str | None, str]:
    """Получить ссылку подписки и остаток времени."""
    remaining_str = "неизвестно"
    sub_url = None

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            if subscription.get('remnawave_uuid'):
                sub_url = await remnawave_get_subscription_url(session, subscription['remnawave_uuid'])
                user_info = await remnawave_get_user_info(session, subscription['remnawave_uuid'])

                if user_info and "expireAt" in user_info:
                    remaining_str = _format_remaining(user_info["expireAt"])
                elif subscription.get('subscription_until'):
                    remaining_str = _format_remaining(subscription['subscription_until'].replace(tzinfo=timezone.utc).isoformat())
            elif subscription.get('subscription_until'):
                remaining_str = _format_remaining(subscription['subscription_until'].replace(tzinfo=timezone.utc).isoformat())
    except Exception as e:
        logging.error(f"Error fetching subscription info from Remnawave: {e}")
        remaining_str = "ошибка загрузки"

    return sub_url, remaining_str


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

    keyboard = []
    for tariff_code, tariff in tariffs.items():
        if purchase_mode == "renew":
            period = "1 месяц" if tariff["days"] == 30 else "3 месяца" if tariff["days"] == 90 else tariff["title"]
            label = f"{period} — {tariff['price']}₽"
        else:
            devices_count = REGULAR_HWID_DEVICE_LIMIT if plan_kind == "regular" else BYPASS_HWID_DEVICE_LIMIT
            devices = f"{devices_count} устройства" if devices_count in (2, 3, 4) else f"{devices_count} устройств"
            label = f"{tariff['title']} — {tariff['price']}₽ ({devices})"
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
    subscriptions = await db.get_visible_subscriptions(tg_id)

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
        for subscription in await db.get_visible_subscriptions(tg_id)
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


async def _show_subscription_card(callback: CallbackQuery, subscription_id: int, *, back_callback: str = "buy_subscription"):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not subscription or subscription.get('generation') != 'v2' or not subscription.get('is_visible'):
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    sub_url, remaining_str = await _get_subscription_access_data(subscription)
    plan_title = 'С антиглушилкой' if subscription.get('plan_kind') == 'bypass' else 'Обычная'
    status_text = _subscription_short_status(subscription)
    until_text = _format_date(subscription.get('subscription_until'))
    limit_text = _device_limit_text(subscription)
    traffic_text = ""

    if subscription.get('plan_kind') == 'bypass':
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
    ]
    if subscription.get('is_renewable'):
        keyboard.append([InlineKeyboardButton(text="🔄 Продлить эту подписку", callback_data=f"renew_subscription_{subscription_id}", style="success")])
    if subscription.get('plan_kind') == 'bypass' and status_text == 'активна':
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


async def _show_subscription_instruction(callback: CallbackQuery, subscription_id: int):
    tg_id = callback.from_user.id
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not subscription:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    sub_url, _ = await _get_subscription_access_data(subscription)
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


@router.callback_query(F.data.startswith("renew_subscription_"))
async def process_subscription_renew(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    subscription_id = int(callback.data.split("_")[-1])
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)

    if not subscription:
        await callback.answer("Подписка не найдена", show_alert=True)
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

    if not subscription or subscription.get('plan_kind') != 'bypass':
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    keyboard = []
    for package_code, package in BYPASS_TRAFFIC_PACKAGES.items():
        keyboard.append([
            InlineKeyboardButton(
                text=f"{package['gb']} ГБ — {package['price']}₽",
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

    await state.update_data(gb_package_code=package_code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="pay_gb_cryptobot", style="success")],
        [InlineKeyboardButton(text="💳 Оплатить картой", callback_data="pay_gb_yookassa", style="success")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_gb", style="danger")],
    ])
    await edit_text_with_photo(
        callback,
        f"📦 <b>Пакет {package['gb']} ГБ</b>\n\nСумма: <b>{package['price']}₽</b>\n\nВыбери способ оплаты:",
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
    if not subscription or subscription.get('plan_kind') != 'bypass':
        await callback.answer("Подписка не найдена", show_alert=True)
        await state.clear()
        return

    amount = package['price']

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
        f"Сумма: {tariff['price']} ₽\n"
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
    amount = tariff["price"]

    existing_invoice_id = await db.get_active_payment_for_user_and_tariff(
        tg_id,
        tariff_code,
        "cryptobot",
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
    amount = tariff["price"]

    existing_payment_id = await db.get_active_payment_for_user_and_tariff(
        tg_id,
        tariff_code,
        "yookassa",
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
    amount = tariff["price"]

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
            traffic_limit_bytes = subscription.get("current_period_limit_bytes") or base_traffic_bytes if plan_kind == "bypass" else 0
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
                traffic_reset_at = COALESCE(traffic_reset_at, $5),
                hwid_device_limit = $6,
                purchase_days = $7
            WHERE id = $8
            """,
            (
                plan_kind,
                plan_kind == "bypass",
                base_traffic_bytes,
                traffic_limit_bytes,
                now + timedelta(days=30) if plan_kind == "bypass" else None,
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
