import asyncio
import logging
import aiohttp
import asyncio
import html
import re
import uuid
from datetime import datetime, timedelta, timezone
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from config import (
    ADMIN_ID,
    BYPASS_BASE_TRAFFIC_GB,
    BYPASS_HWID_DEVICE_LIMIT,
    BYPASS_SQUAD_UUID,
    DEFAULT_SQUAD_UUID,
    GB_BYTES,
    MINIAPP_URL,
    PUBLIC_SITE_URL,
    REGULAR_HWID_DEVICE_LIMIT,
    REGULAR_SQUAD_UUID,
    SUBSCRIPTION_PUBLIC_BASE_URL,
    SUPPORT_URL,
)
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_set_subscription_expiry,
    remnawave_get_user_info,
    remnawave_get_subscription_url,
    remnawave_revoke_subscription,
)
from services.subscription_adjustment import SubscriptionAdjustmentError, adjust_subscription_days

logger = logging.getLogger(__name__)

router = Router()

ACTIVE_BROADCASTS: dict[str, dict] = {}


class BroadcastStates(StatesGroup):
    """Состояния для рассылок сообщений"""
    waiting_for_broadcast_all = State()
    waiting_for_broadcast_no_sub = State()
    reviewing_broadcast = State()
    choosing_broadcast_buttons = State()


def is_admin(user_id: int) -> bool:
    """Проверить является ли пользователь администратором"""
    return user_id == ADMIN_ID


def _build_remnawave_username(tg_id: int, subscription_id: int) -> str:
    return f"tg_{tg_id}_{subscription_id}"


def _build_v2_remnawave_username(tg_id: int, plan_kind: str, type_index: int) -> str:
    return f"tg_{tg_id}_{plan_kind}_{type_index}"


def _plan_title(plan_kind: str) -> str:
    return "Обычная" if plan_kind == "regular" else "С антиглушилкой"


def _parse_admin_subscription_command(parts: list[str]) -> tuple[int, int, int]:
    """Распарсить /give_sub и /take_sub в формат tg_id, slot, days."""
    if len(parts) == 3:
        return int(parts[1]), 1, int(parts[2])

    if len(parts) == 4:
        return int(parts[1]), int(parts[2]), int(parts[3])

    raise ValueError("Invalid arguments")


def _classify_broadcast_exception(exc: Exception) -> tuple[str, float | None]:
    """Классифицировать типовую ошибку Telegram при рассылке."""
    if isinstance(exc, TelegramRetryAfter):
        retry_after = float(getattr(exc, "retry_after", 1.0) or 1.0)
        return "rate_limited", retry_after

    if isinstance(exc, TelegramForbiddenError):
        error_msg = str(exc).lower()
        if "blocked" in error_msg or "deactivated" in error_msg:
            return "blocked", None
        return "unreachable", None

    if isinstance(exc, TelegramBadRequest):
        error_msg = str(exc).lower()
        if (
            "chat not found" in error_msg
            or "user not found" in error_msg
            or "private chat not found" in error_msg
            or "have no rights to send a message" in error_msg
            or "can't initiate conversation" in error_msg
            or "bot can't initiate conversation" in error_msg
            or "peers not found" in error_msg
        ):
            return "unreachable", None

    error_msg = str(exc).lower()
    if "429" in error_msg or "too many requests" in error_msg:
        return "rate_limited", 1.0
    if "blocked" in error_msg or "user is deactivated" in error_msg or "bot was blocked" in error_msg:
        return "blocked", None
    if "chat not found" in error_msg or "can't initiate conversation" in error_msg:
        return "unreachable", None

    return "error", None


BROADCAST_BUTTONS = {
    "buy_subscription": {"text": "💳 Купить / Продлить", "callback_data": "buy_subscription", "style": "success"},
    "my_subscriptions": {"text": "🔐 Мои подписки", "callback_data": "my_subscriptions", "style": "primary"},
    "buy_gb": {"text": "📦 Купить ГБ", "callback_data": "buy_gb", "style": "success"},
    "miniapp": {"text": "📱 Личный кабинет", "web_app": MINIAPP_URL, "style": "primary"},
    "how_to_connect": {"text": "📲 Инструкция", "callback_data": "how_to_connect", "style": "primary"},
    "support": {"text": "🆘 Поддержка", "url": SUPPORT_URL, "style": "primary"},
    "back_to_menu": {"text": "🏠 Главное меню", "callback_data": "back_to_menu", "style": "danger"},
}

BROADCAST_BUTTON_ORDER = (
    "buy_subscription",
    "my_subscriptions",
    "buy_gb",
    "miniapp",
    "how_to_connect",
    "support",
    "back_to_menu",
)


def _make_broadcast_button(key: str) -> InlineKeyboardButton:
    spec = BROADCAST_BUTTONS[key]
    kwargs = {"text": spec["text"], "style": spec["style"]}
    if "callback_data" in spec:
        kwargs["callback_data"] = spec["callback_data"]
    if "url" in spec:
        kwargs["url"] = spec["url"]
    if "web_app" in spec:
        kwargs["web_app"] = WebAppInfo(url=spec["web_app"])
    return InlineKeyboardButton(**kwargs)


def _build_broadcast_user_keyboard(selected_buttons: list[str] | None) -> InlineKeyboardMarkup | None:
    if not selected_buttons:
        return None
    rows = [[_make_broadcast_button(key)] for key in BROADCAST_BUTTON_ORDER if key in selected_buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _build_broadcast_admin_keyboard(selected_buttons: list[str] | None) -> InlineKeyboardMarkup:
    selected = set(selected_buttons or [])
    rows = []
    for key in BROADCAST_BUTTON_ORDER:
        spec = BROADCAST_BUTTONS[key]
        marker = "✅" if key in selected else "⬜"
        rows.append([
            InlineKeyboardButton(
                text=f"{marker} {spec['text']}",
                callback_data=f"broadcast_toggle:{key}",
                style="primary",
            )
        ])

    rows.extend([
        [InlineKeyboardButton(text="❌ Без кнопок", callback_data="broadcast_no_buttons", style="danger")],
        [InlineKeyboardButton(text="👁 Предпросмотр", callback_data="broadcast_preview", style="primary")],
        [InlineKeyboardButton(text="🚫 Отменить рассылку", callback_data="broadcast_cancel", style="danger")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _broadcast_button_selection_text(selected_buttons: list[str] | None) -> str:
    selected_labels = [BROADCAST_BUTTONS[key]["text"] for key in BROADCAST_BUTTON_ORDER if key in (selected_buttons or [])]
    selected_text = "\n".join(f"• {label}" for label in selected_labels) if selected_labels else "• без кнопок"
    return (
        "🔘 <b>Выбор кнопок для рассылки</b>\n\n"
        "Нажимай на кнопки ниже, чтобы добавить или убрать их из сообщения.\n\n"
        f"<b>Сейчас выбрано:</b>\n{selected_text}"
    )


def _broadcast_mode_text(mode: str) -> str:
    return "пользователям без подписки" if mode == "no_sub" else "всем пользователям"


def _broadcast_summary_text(mode: str, selected_buttons: list[str] | None = None) -> str:
    selected_labels = [BROADCAST_BUTTONS[key]["text"] for key in BROADCAST_BUTTON_ORDER if key in (selected_buttons or [])]
    selected_text = ", ".join(selected_labels) if selected_labels else "пока не выбраны"
    return (
        "📤 <b>Рассылка подготовлена</b>\n\n"
        f"Кому: <b>{_broadcast_mode_text(mode)}</b>\n"
        "Сообщение: <b>получено</b>\n"
        f"Кнопки: <b>{selected_text}</b>\n\n"
        "Следующий шаг: выбери кнопки или перейди к предпросмотру без кнопок."
    )


def _build_broadcast_summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔘 Выбрать кнопки", callback_data="broadcast_choose_buttons", style="primary")],
        [InlineKeyboardButton(text="👁 Предпросмотр без кнопок", callback_data="broadcast_preview", style="primary")],
        [InlineKeyboardButton(text="🚫 Отменить рассылку", callback_data="broadcast_cancel", style="danger")],
    ])


def _build_broadcast_ready_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Начать рассылку", callback_data="broadcast_start", style="success")],
        [InlineKeyboardButton(text="🔘 Изменить кнопки", callback_data="broadcast_choose_buttons", style="primary")],
        [InlineKeyboardButton(text="🚫 Отменить рассылку", callback_data="broadcast_cancel", style="danger")],
    ])


def _build_broadcast_stop_keyboard(broadcast_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Остановить рассылку", callback_data=f"broadcast_stop:{broadcast_id}", style="danger")],
    ])


async def _send_broadcast_preview(callback: CallbackQuery, state: FSMContext) -> bool:
    data = await state.get_data()
    source_chat_id = data.get("source_chat_id")
    source_message_id = data.get("source_message_id")
    selected_buttons = data.get("selected_buttons") or []
    if not source_chat_id or not source_message_id:
        await callback.answer("Исходное сообщение не найдено", show_alert=True)
        await state.clear()
        return False

    await callback.bot.copy_message(
        chat_id=callback.from_user.id,
        from_chat_id=source_chat_id,
        message_id=source_message_id,
        reply_markup=_build_broadcast_user_keyboard(selected_buttons),
    )
    await state.update_data(preview_sent=True)
    return True


async def _get_broadcast_users(mode: str):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if mode == "no_sub":
            return await conn.fetch(
                """
                SELECT tg_id FROM users
                WHERE NOT EXISTS (
                    SELECT 1 FROM subscriptions
                    WHERE subscriptions.tg_id = users.tg_id
                      AND subscriptions.subscription_until IS NOT NULL
                      AND subscriptions.subscription_until > now() AT TIME ZONE 'UTC'
                )
                ORDER BY tg_id
                """
            )

        return await conn.fetch("SELECT tg_id FROM users ORDER BY tg_id")


async def _run_broadcast_copy(
    bot,
    status_message: Message,
    users,
    source_chat_id: int,
    source_message_id: int,
    reply_markup: InlineKeyboardMarkup | None,
    mode: str,
    broadcast_id: str,
):
    total_users = len(users)
    sent_count = 0
    error_count = 0
    blocked_count = 0
    unreachable_count = 0
    rate_limited_delay = 0.3
    mode_text = "пользователям без подписки" if mode == "no_sub" else "всем пользователям"

    progress_message = await status_message.answer(
        f"📤 <b>Начинаю рассылку {mode_text}: {total_users} получателей...</b>\n\n"
        "Отправлено: <b>0</b>\n"
        "Заблокировано: <b>0</b>\n"
        "Недоступно: <b>0</b>\n"
        "Ошибок: <b>0</b>",
        reply_markup=_build_broadcast_stop_keyboard(broadcast_id),
    )

    stopped = False
    for index, user_record in enumerate(users, start=1):
        if ACTIVE_BROADCASTS.get(broadcast_id, {}).get("stop_requested"):
            stopped = True
            logger.info(f"Broadcast {broadcast_id} stopped by admin before next recipient")
            break

        user_id = user_record['tg_id']
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=reply_markup,
            )
            sent_count += 1
            logger.info(f"Broadcast message copied to user {user_id} ({mode})")
            rate_limited_delay = 0.3
        except Exception as e:
            status, retry_after = _classify_broadcast_exception(e)
            if status == "blocked":
                blocked_count += 1
                logger.warning(f"User {user_id} has blocked bot or deactivated account")
            elif status == "unreachable":
                unreachable_count += 1
                logger.info(f"User {user_id} is unreachable for broadcast: {str(e)[:100]}")
            elif status == "rate_limited":
                rate_limited_delay = min(max(retry_after or rate_limited_delay * 1.5, 0.5), 5.0)
                logger.warning(f"Rate limited during broadcast for user {user_id}. New delay: {rate_limited_delay}s")
                await asyncio.sleep(rate_limited_delay)
                continue
            else:
                error_count += 1
                logger.warning(f"Failed to send broadcast to user {user_id}: {str(e)[:100]}")

        if index == total_users or index % 25 == 0:
            try:
                await progress_message.edit_text(
                    f"📤 <b>Рассылка идет: {mode_text}</b>\n\n"
                    f"Прогресс: <b>{index}/{total_users}</b>\n"
                    f"Отправлено: <b>{sent_count}</b>\n"
                    f"Заблокировано: <b>{blocked_count}</b>\n"
                    f"Недоступно: <b>{unreachable_count}</b>\n"
                    f"Ошибок: <b>{error_count}</b>",
                    reply_markup=_build_broadcast_stop_keyboard(broadcast_id),
                )
            except Exception as e:
                logger.debug(f"Failed to update broadcast progress: {e}")

        await asyncio.sleep(rate_limited_delay)

    title = "⛔ <b>Рассылка остановлена!</b>" if stopped else "✅ <b>Рассылка завершена!</b>"
    await status_message.answer(
        f"{title}\n\n"
        "📊 <b>Статистика:</b>\n"
        f"• ✅ Отправлено: {sent_count}/{total_users}\n"
        f"• 🚫 Заблокировано: {blocked_count}\n"
        f"• 📴 Недоступно: {unreachable_count}\n"
        f"• ❌ Ошибок: {error_count}"
    )

    logger.info(
        f"Admin broadcast completed ({mode}, stopped={stopped}): sent={sent_count}, blocked={blocked_count}, "
        f"unreachable={unreachable_count}, errors={error_count}"
    )


def _normalize_tracking_code(value: str) -> str:
    return value.strip().lower()


def _is_valid_tracking_code(code: str) -> bool:
    if code.startswith("ref_") or code.startswith("partner_"):
        return False
    return bool(re.fullmatch(r"[a-z0-9_-]{3,64}", code))


def _format_tracking_tariffs(rows) -> str:
    if not rows:
        return "• покупок пока нет"

    lines = []
    for row in rows:
        payment_kind = row.get('payment_kind') or 'subscription'
        kind_title = "ГБ" if payment_kind == "traffic_package" else "Подписка"
        lines.append(
            f"• {kind_title} <code>{row['tariff_code']}</code>: "
            f"{row['purchase_count']} шт., {float(row['revenue'] or 0):.2f} ₽"
        )
    return "\n".join(lines)


@router.message(Command("new_link"))
async def admin_new_tracking_link(message: Message):
    """Админ команда: создать аккуратную tracking-ссылку."""
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /new_link КОД [Название]\n\n"
            "<b>Примеры:</b>\n"
            "<code>/new_link blogger1</code>\n"
            "<code>/new_link blogger1 Реклама у блогера</code>\n\n"
            "Код: 3-64 символа, латиница, цифры, <code>_</code> или <code>-</code>."
        )
        return

    code = _normalize_tracking_code(parts[1])
    title = parts[2].strip() if len(parts) > 2 else None
    if not _is_valid_tracking_code(code):
        await message.answer(
            "❌ Код ссылки некорректный.\n\n"
            "Можно использовать только латиницу, цифры, <code>_</code> и <code>-</code>, длина 3-64 символа.\n"
            "Коды <code>ref_...</code> и <code>partner_...</code> зарезервированы."
        )
        return

    await db.create_tracking_link(code, title, admin_id)
    bot_username = (await message.bot.get_me()).username
    bot_link = f"https://t.me/{bot_username}?start={code}"
    site_link = f"{PUBLIC_SITE_URL}/?t={code}"
    await message.answer(
        "✅ <b>Tracking-ссылка создана</b>\n\n"
        f"<b>Код:</b> <code>{code}</code>\n"
        f"<b>Название:</b> {html.escape(title) if title else 'не указано'}\n\n"
        f"<b>Telegram:</b>\n<code>{bot_link}</code>\n\n"
        f"<b>Сайт:</b>\n<code>{site_link}</code>\n\n"
        f"Статистика: <code>/link_stats {code}</code>"
    )


@router.message(Command("links"))
async def admin_list_tracking_links(message: Message):
    """Админ команда: показать tracking-ссылки."""
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        return

    links = await db.list_tracking_links()
    if not links:
        await message.answer("Tracking-ссылок пока нет. Создать: <code>/new_link blogger1</code>")
        return

    lines = []
    for link in links[:50]:
        status = "активна" if link['is_active'] else "выключена"
        title = f" — {html.escape(link['title'])}" if link.get('title') else ""
        lines.append(f"• <code>{link['code']}</code>{title} ({status})")

    await message.answer(
        "🔗 <b>Tracking-ссылки</b>\n\n"
        + "\n".join(lines)
        + "\n\nСтатистика: <code>/link_stats КОД</code>"
    )


@router.message(Command("link_stats"))
async def admin_tracking_link_stats(message: Message):
    """Админ команда: статистика tracking-ссылки."""
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("❌ Использование: <code>/link_stats КОД</code>")
        return

    code = _normalize_tracking_code(parts[1])
    stats = await db.get_tracking_link_stats(code)
    if not stats:
        await message.answer("❌ Tracking-ссылка не найдена")
        return

    link = stats['link']
    bot_username = (await message.bot.get_me()).username
    bot_url = f"https://t.me/{bot_username}?start={code}"
    site_url = f"{PUBLIC_SITE_URL}/?t={code}"
    await message.answer(
        f"📊 <b>Статистика ссылки <code>{code}</code></b>\n\n"
        f"<b>Название:</b> {html.escape(link['title']) if link.get('title') else 'не указано'}\n"
        f"<b>Статус:</b> {'активна' if link['is_active'] else 'выключена'}\n"
        f"<b>Telegram:</b> <code>{bot_url}</code>\n"
        f"<b>Сайт:</b> <code>{site_url}</code>\n\n"
        f"👁 <b>Переходов всего:</b> {stats['total_clicks']}\n"
        f"👤 <b>Уникальных пользователей:</b> {stats['unique_clicks']}\n"
        f"🆕 <b>Новых пользователей по ссылке:</b> {stats['attributed_users']}\n\n"
        f"💳 <b>Оплаченных платежей:</b> {stats['paid_payments']}\n"
        f"💰 <b>Выручка:</b> {stats['revenue']:.2f} ₽\n\n"
        "<b>Покупки по тарифам:</b>\n"
        f"{_format_tracking_tariffs(stats['payments_by_tariff'])}"
    )


@router.message(Command("disable_link"))
async def admin_disable_tracking_link(message: Message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("❌ Использование: <code>/disable_link КОД</code>")
        return
    code = _normalize_tracking_code(parts[1])
    if await db.set_tracking_link_active(code, False):
        await message.answer(f"✅ Ссылка <code>{code}</code> выключена")
    else:
        await message.answer("❌ Tracking-ссылка не найдена")


@router.message(Command("enable_link"))
async def admin_enable_tracking_link(message: Message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("❌ Использование: <code>/enable_link КОД</code>")
        return
    code = _normalize_tracking_code(parts[1])
    if await db.set_tracking_link_active(code, True):
        await message.answer(f"✅ Ссылка <code>{code}</code> включена")
    else:
        await message.answer("❌ Tracking-ссылка не найдена")


@router.message(Command("reissue_sub_links"))
async def admin_reissue_subscription_links(message: Message):
    """Массово перевыпустить подписочные ссылки Remnawave."""
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        return

    parts = (message.text or "").split(maxsplit=1)
    mode = parts[1].strip().lower() if len(parts) > 1 else "all"
    if mode not in {"all", "active"}:
        await message.answer(
            "❌ <b>Неверный формат</b>\n\n"
            "Использование:\n"
            "<code>/reissue_sub_links</code> — все подписки с Remnawave UUID\n"
            "<code>/reissue_sub_links active</code> — только активные подписки"
        )
        return

    active_only = mode == "active"
    subscriptions = await db.get_subscriptions_with_remnawave_uuid(active_only=active_only)
    if not subscriptions:
        await message.answer("Подписок для перевыпуска не найдено")
        return

    total = len(subscriptions)
    status_message = await message.answer(
        f"🔁 <b>Перевыпуск подписочных ссылок запущен</b>\n\n"
        f"Режим: <b>{'только активные' if active_only else 'все'}</b>\n"
        f"Найдено: <b>{total}</b>\n"
        "Готово: <b>0</b>"
    )

    success_count = 0
    error_count = 0
    failed_ids = []

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for index, subscription in enumerate(subscriptions, start=1):
            try:
                ok = await remnawave_revoke_subscription(session, subscription["remnawave_uuid"])
                if ok:
                    success_count += 1
                else:
                    error_count += 1
                    failed_ids.append(subscription["id"])
            except Exception as e:
                error_count += 1
                failed_ids.append(subscription["id"])
                logger.warning("Failed to reissue subscription link for subscription %s: %s", subscription["id"], e)

            if index == total or index % 10 == 0:
                try:
                    await status_message.edit_text(
                        f"🔁 <b>Перевыпуск подписочных ссылок идёт</b>\n\n"
                        f"Прогресс: <b>{index}/{total}</b>\n"
                        f"Успешно: <b>{success_count}</b>\n"
                        f"Ошибок: <b>{error_count}</b>"
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.2)

    failed_text = ""
    if failed_ids:
        failed_preview = ", ".join(str(item) for item in failed_ids[:20])
        suffix = "..." if len(failed_ids) > 20 else ""
        failed_text = f"\n\nНе удалось для subscription id: <code>{failed_preview}{suffix}</code>"

    await status_message.edit_text(
        "✅ <b>Перевыпуск подписочных ссылок завершён</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"Успешно: <b>{success_count}</b>\n"
        f"Ошибок: <b>{error_count}</b>"
        f"{failed_text}\n\n"
        f"После этого бот и MiniApp будут показывать ссылки на <code>{SUBSCRIPTION_PUBLIC_BASE_URL}</code>."
    )


@router.message(Command("new_code"))
async def admin_new_code(message: Message):
    """Админ команда: создать новый промокод"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /new_code attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 4:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /new_code КОД ДНЕЙ ЛИМИТ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>КОД</code> - код промокода (только буквы и цифры)\n"
            "• <code>ДНЕЙ</code> - количество дней (число > 0)\n"
            "• <code>ЛИМИТ</code> - максимум использований (число > 0)\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100"
        )
        logger.warning(f"Admin {admin_id} /new_code - wrong number of arguments: {len(parts)-1}")
        return

    try:
        code = parts[1].strip()
        days = int(parts[2])
        limit = int(parts[3])

        # Валидация значений
        if not code or not code.isalnum():
            await message.answer("❌ Код промокода должен содержать только буквы и цифры")
            return

        if len(code) < 2:
            await message.answer("❌ Код промокода должен быть не менее 3 символов")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if limit <= 0:
            await message.answer("❌ Лимит использований должен быть больше 0")
            return

        # Создаём промокод
        await db.create_promo_code(code.upper(), days, limit)

        await message.answer(
            f"✅ <b>Промокод создан успешно!</b>\n\n"
            f"<b>Код:</b> <code>{code.upper()}</code>\n"
            f"<b>Дней подписки:</b> {days}\n"
            f"<b>Лимит использований:</b> {limit}\n"
            f"<b>Статус:</b> активен"
        )

        logger.info(f"Admin {admin_id} created promo code: {code.upper()} (days={days}, limit={limit})")

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ДНЕЙ и ЛИМИТ - целые числа\n"
            "• Оба числа больше 0\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100"
        )
        logger.warning(f"Admin {admin_id} /new_code - parsing error for arguments: {parts[1:]}")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы данных: {str(e)[:100]}")
        logger.error(f"Admin {admin_id} /new_code database error: {e}")


@router.message(Command("give_sub"))
async def admin_give_sub(message: Message):
    """Админ команда: выдать/продлить v2-подписку regular/bypass."""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /give_sub attempt from user {admin_id}")
        return

    parts = message.text.split()

    if len(parts) not in (4, 5):
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /give_sub ТГ_ИД ТИП [НОМЕР] ДНЕЙ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>ТИП</code> - <code>regular</code> или <code>bypass</code>\n"
            "• <code>НОМЕР</code> - номер подписки внутри типа 1..3 (необязательно)\n"
            "• <code>ДНЕЙ</code> - количество дней (число > 0)\n\n"
            "<b>Примеры:</b>\n"
            "<code>/give_sub 123456789 regular 30</code>\n"
            "<code>/give_sub 123456789 bypass 30</code>\n"
            "<code>/give_sub 123456789 regular 2 90</code>"
        )
        logger.warning(f"Admin {admin_id} /give_sub - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        plan_kind = parts[2].lower()
        if plan_kind not in {"regular", "bypass"}:
            await message.answer("❌ Тип подписки должен быть <code>regular</code> или <code>bypass</code>")
            return

        if len(parts) == 4:
            type_index = None
            days = int(parts[3])
        else:
            type_index = int(parts[3])
            days = int(parts[4])

        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if type_index is not None and (type_index < 1 or type_index > db.MAX_SUBSCRIPTIONS_PER_USER):
            await message.answer(f"❌ Номер подписки должен быть от 1 до {db.MAX_SUBSCRIPTIONS_PER_USER}")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя выдать подписку самому себе")
            logger.warning(f"Admin {admin_id} tried to give subscription to themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД, НОМЕР и ДНЕЙ - целые числа\n"
            "• ТИП - regular или bypass\n"
            "• НОМЕР от 1 до 3\n"
            "• ДНЕЙ больше 0\n\n"
            "<b>Примеры:</b>\n"
            "<code>/give_sub 123456789 regular 30</code>\n"
            "<code>/give_sub 123456789 bypass 2 90</code>"
        )
        logger.warning(f"Admin {admin_id} /give_sub - parsing error for arguments: {parts[1:]}")
        return

    if not await db.acquire_user_lock(tg_id):
        await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
        logger.info(f"Admin {admin_id} /give_sub - could not acquire lock for user {tg_id}")
        return

    try:
        user = await db.get_user(tg_id)

        if not user:
            await db.create_user(tg_id, f"user_{tg_id}")
            user = await db.get_user(tg_id)
            logger.info(f"Created new user {tg_id} in database for admin {admin_id}")

        if type_index is None:
            type_index = await db.get_next_type_index(tg_id, plan_kind)
            if type_index is None:
                await message.answer(f"❌ У пользователя уже максимум 3 подписки типа <code>{plan_kind}</code>")
                return

        subscription = await db.get_subscription_by_type_index(tg_id, plan_kind, type_index)
        if subscription is None:
            storage_slot = await db.get_next_subscription_slot(tg_id)
            if storage_slot is None:
                await message.answer("❌ Нет свободного внутреннего слота подписки")
                return
            subscription = await db.create_subscription_record(
                tg_id,
                storage_slot,
                plan_kind=plan_kind,
                type_index=type_index,
                generation="v2",
                is_visible=True,
                is_renewable=True,
                purchase_days=days,
            )

        squad_uuid = REGULAR_SQUAD_UUID if plan_kind == "regular" else BYPASS_SQUAD_UUID
        device_limit = REGULAR_HWID_DEVICE_LIMIT if plan_kind == "regular" else BYPASS_HWID_DEVICE_LIMIT
        base_traffic_bytes = BYPASS_BASE_TRAFFIC_GB * GB_BYTES if plan_kind == "bypass" else 0
        traffic_limit_bytes = max(
            subscription.get("current_period_limit_bytes") or 0,
            base_traffic_bytes + (subscription.get("carried_traffic_bytes") or 0) + (subscription.get("current_paid_traffic_bytes") or 0),
        ) if plan_kind == "bypass" else 0
        now = datetime.utcnow()
        current_until = subscription.get('subscription_until')
        if current_until and current_until > now:
            new_until = current_until + timedelta(days=days)
        else:
            new_until = now + timedelta(days=days)

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days=days,
                extend_if_exists=False,
                remna_username=subscription.get('remnawave_username') or _build_v2_remnawave_username(tg_id, plan_kind, type_index),
                traffic_limit_bytes=traffic_limit_bytes if plan_kind == "bypass" else 0,
                traffic_limit_strategy="NO_RESET",
                active_internal_squads=[squad_uuid],
                hwid_device_limit=device_limit,
                telegram_id=tg_id,
            )

            if not uuid:
                await message.answer(
                    f"❌ <b>Ошибка Remnawave API</b>\n\n"
                    f"Не удалось создать/обновить аккаунт для пользователя {tg_id}\n\n"
                    "Попробуй позже"
                )
                logger.error(f"Failed to get/create Remnawave user for TG {tg_id} by admin {admin_id}")
                return

            success = await remnawave_set_subscription_expiry(session, uuid, new_until)
            if not success:
                logger.warning(f"Failed to set subscription expiry in Remnawave for {tg_id} {plan_kind} #{type_index}, but continuing")

            sub_url = await remnawave_get_subscription_url(session, uuid)

            await db.update_subscription_record(subscription['id'], uuid, username, new_until, squad_uuid)
            await db.db_execute(
                """
                UPDATE subscriptions
                SET plan_kind = $1,
                    generation = 'v2',
                    is_visible = TRUE,
                    is_renewable = TRUE,
                    type_index = $2,
                    purchase_days = $3,
                    traffic_enabled = $4,
                    base_traffic_bytes = $5,
                    current_period_limit_bytes = $6,
                    traffic_reset_at = $7,
                    hwid_device_limit = $8,
                    last_traffic_sync_at = now(),
                    updated_at = now()
                WHERE id = $9
                """,
                (
                    plan_kind,
                    type_index,
                    days,
                    plan_kind == "bypass",
                    base_traffic_bytes,
                    traffic_limit_bytes,
                    (subscription.get("traffic_reset_at") if plan_kind == "bypass" and current_until and current_until > now else None) or (now + timedelta(days=30) if plan_kind == "bypass" else None),
                    device_limit,
                    subscription['id'],
                )
            )

        await message.answer(
            f"✅ <b>Подписка выдана успешно!</b>\n\n"
            f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
            f"🌐 <b>Тип:</b> {_plan_title(plan_kind)}\n"
            f"🔢 <b>Номер:</b> #{type_index}\n"
            f"📅 <b>Дней:</b> {days}\n"
            f"⏳ <b>До:</b> {new_until.strftime('%d.%m.%Y %H:%M')}\n"
            f"🔑 <b>UUID:</b> <code>{uuid}</code>"
        )

        # Уведомляем пользователя
        try:
            await message.bot.send_message(
                tg_id,
                f"🎉 <b>Поздравляем!</b>\n\n"
                f"Вам выдана/продлена подписка <b>{_plan_title(plan_kind)} #{type_index}</b> на <b>{days} дней</b>\n\n"
                f"<b>Ваш ключ:</b>\n{sub_url or 'Ошибка получения ссылки'}"
            )
            logger.info(f"User {tg_id} notified about subscription by admin {admin_id}")
        except Exception as e:
            logger.warning(f"Failed to notify user {tg_id}: {e}")
            await message.answer(
                f"⚠️ Подписка выдана, но не удалось отправить уведомление пользователю\n"
                f"(Ошибка: {str(e)[:50]})"
            )

        logger.info(f"Admin {admin_id} gave {days} days {plan_kind} subscription #{type_index} to user {tg_id}")

    except Exception as e:
        logger.error(f"Give subscription error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

    finally:
        await db.release_user_lock(tg_id)


@router.message(Command("sub_days", "days"))
async def admin_adjust_subscription_days(message: Message):
    """Изменить срок конкретной подписки на произвольное число дней."""
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning("Unauthorized /sub_days attempt from user %s", admin_id)
        return

    parts = message.text.split()
    if len(parts) != 4:
        await message.answer(
            "❌ <b>Неверный формат</b>\n\n"
            "<b>Использование:</b>\n"
            "<code>/sub_days ID_ПОЛЬЗОВАТЕЛЯ ID_ПОДПИСКИ +/-ДНИ</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/sub_days 123456789 42 -15</code> — убрать 15 дней\n"
            "<code>/sub_days 123456789 42 30</code> — добавить 30 дней\n\n"
            "ID подписки отображается в веб-админке рядом с датой."
        )
        return

    try:
        tg_id = int(parts[1])
        subscription_id = int(parts[2])
        days = int(parts[3])
        if tg_id == 0 or subscription_id <= 0 or days == 0:
            raise ValueError
        if abs(days) > 3650:
            await message.answer("❌ За один раз можно изменить срок максимум на 3650 дней")
            return
    except ValueError:
        await message.answer(
            "❌ ID пользователя, ID подписки и дни должны быть целыми числами.\n\n"
            "Пример: <code>/sub_days 123456789 42 -15</code>"
        )
        return

    subscription = await db.get_subscription_by_id(subscription_id, tg_id)
    if not subscription:
        await message.answer(
            f"❌ Подписка <code>{subscription_id}</code> пользователя <code>{tg_id}</code> не найдена"
        )
        return

    if not await db.acquire_user_lock(tg_id):
        await message.answer("❌ Пользователь занят другой операцией. Попробуйте позже")
        return

    try:
        try:
            new_until = await adjust_subscription_days(subscription, days)
        except SubscriptionAdjustmentError as exc:
            await message.answer(f"❌ {html.escape(str(exc))}")
            return

        action = "Добавлено" if days > 0 else "Убрано"
        await message.answer(
            "✅ <b>Срок подписки изменён</b>\n\n"
            f"👤 Пользователь: <code>{tg_id}</code>\n"
            f"🔐 ID подписки: <code>{subscription_id}</code>\n"
            f"📅 {action}: <b>{abs(days)} дней</b>\n"
            f"⏳ Новая дата: <b>{new_until.strftime('%d.%m.%Y %H:%M')}</b>"
        )
        logger.info(
            "Admin %s adjusted subscription %s for user %s by %s days",
            admin_id,
            subscription_id,
            tg_id,
            days,
        )
    except Exception as exc:
        logger.error("Admin /sub_days failed: %s", exc, exc_info=True)
        await message.answer(f"❌ Ошибка: {html.escape(str(exc)[:100])}")
    finally:
        await db.release_user_lock(tg_id)


@router.message(Command("take_sub"))
async def admin_take_sub(message: Message):
    """Админ команда: забрать подписку пользователю (уменьшить на N дней)"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /take_sub attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) not in (3, 4):
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /take_sub ТГ_ИД [СЛОТ] ДНЕЙ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>СЛОТ</code> - номер подписки 1..3 (необязательно, по умолчанию 1)\n"
            "• <code>ДНЕЙ</code> - количество дней для удаления (число > 0)\n\n"
            "<b>Примеры:</b>\n"
            "<code>/take_sub 123456789 10</code>\n"
            "<code>/take_sub 123456789 2 10</code>\n\n"
            "<i>Если дней больше чем осталось, подписка будет аннулирована</i>"
        )
        logger.warning(f"Admin {admin_id} /take_sub - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id, slot_number, days = _parse_admin_subscription_command(parts)

        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if slot_number < 1 or slot_number > db.MAX_SUBSCRIPTIONS_PER_USER:
            await message.answer(f"❌ Слот должен быть от 1 до {db.MAX_SUBSCRIPTIONS_PER_USER}")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя отобрать подписку самому себе")
            logger.warning(f"Admin {admin_id} tried to take subscription from themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД, СЛОТ и ДНЕЙ - целые числа\n"
            "• СЛОТ от 1 до 3\n"
            "• ДНЕЙ больше 0\n\n"
            "<b>Примеры:</b>\n"
            "<code>/take_sub 123456789 10</code>\n"
            "<code>/take_sub 123456789 2 10</code>"
        )
        logger.warning(f"Admin {admin_id} /take_sub - parsing error for arguments: {parts[1:]}")
        return

    if not await db.acquire_user_lock(tg_id):
        await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
        logger.info(f"Admin {admin_id} /take_sub - could not acquire lock for user {tg_id}")
        return

    try:
        user = await db.get_user(tg_id)

        if not user:
            await message.answer(f"❌ Пользователь {tg_id} не найден в БД")
            logger.warning(f"Admin {admin_id} tried to take subscription from non-existent user {tg_id}")
            return

        subscription = await db.get_subscription_by_slot(tg_id, slot_number)
        if not subscription:
            await message.answer(f"❌ У пользователя {tg_id} нет подписки в слоте #{slot_number}")
            return

        remnawave_uuid = subscription.get('remnawave_uuid')

        if not remnawave_uuid:
            await message.answer(f"❌ В слоте #{slot_number} нет активной подписки")
            logger.info(f"Admin {admin_id} /take_sub - user {tg_id} slot {slot_number} has no Remnawave UUID")
            return

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            user_info = await remnawave_get_user_info(session, remnawave_uuid)

        if not user_info or 'expireAt' not in user_info:
            await message.answer(f"❌ Не удалось получить информацию о подписке из Remnawave")
            logger.warning(f"Admin {admin_id} /take_sub - failed to get user info from Remnawave for {tg_id}")
            return

        expire_at_str = user_info['expireAt']
        current_subscription_until = datetime.fromisoformat(expire_at_str.replace('Z', '+00:00'))
        current_subscription_until = current_subscription_until.replace(tzinfo=None)

        new_subscription_until = current_subscription_until - timedelta(days=days)
        now = datetime.utcnow()

        logger.info(f"Admin {admin_id} /take_sub user {tg_id} slot {slot_number}: current_until={current_subscription_until}, removing {days} days, new_until={new_subscription_until}, now={now}")

        if new_subscription_until <= now:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                success = await remnawave_set_subscription_expiry(
                    session,
                    remnawave_uuid,
                    now - timedelta(seconds=1)
                )
                if not success:
                    logger.warning(f"Failed to update Remnawave for user {tg_id}, continuing")

            await db.delete_subscription_record(subscription['id'])

            await message.answer(
                f"✅ <b>Подписка аннулирована</b>\n\n"
                f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
                f"🔢 <b>Слот:</b> #{slot_number}\n"
                f"📅 <b>Удалено дней:</b> {days}\n"
                f"❌ <b>Статус:</b> подписка истекла"
            )

            try:
                await message.bot.send_message(
                    tg_id,
                    f"❌ <b>Ваша подписка была аннулирована</b>\n\n"
                    f"Администратор удалил подписку <b>#{slot_number}</b> на {days} дней.\n\n"
                    f"Ваш доступ закрыт. Пожалуйста, свяжитесь с поддержкой."
                )
                logger.info(f"User {tg_id} notified about subscription cancellation by admin {admin_id}")
            except Exception as e:
                logger.warning(f"Failed to notify user {tg_id}: {e}")

            logger.info(f"Admin {admin_id} cancelled subscription for user {tg_id} slot {slot_number} (removed {days} days)")

        else:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                success = await remnawave_set_subscription_expiry(
                    session,
                    remnawave_uuid,
                    new_subscription_until
                )
                if not success:
                    logger.warning(f"Failed to update Remnawave for user {tg_id}, but continuing with DB update")

            await db.update_subscription_record(
                subscription['id'],
                subscription['remnawave_uuid'],
                subscription['remnawave_username'],
                new_subscription_until,
                subscription['squad_uuid'] or DEFAULT_SQUAD_UUID,
            )

            remaining_days = (new_subscription_until - now).days
            remaining_hours = ((new_subscription_until - now).seconds // 3600) % 24

            await message.answer(
                f"✅ <b>Подписка обновлена</b>\n\n"
                f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
                f"🔢 <b>Слот:</b> #{slot_number}\n"
                f"📅 <b>Удалено дней:</b> {days}\n"
                f"⏰ <b>Осталось:</b> {remaining_days}д {remaining_hours}ч\n"
                f"🟢 <b>Статус:</b> активна"
            )

            try:
                await message.bot.send_message(
                    tg_id,
                    f"⚠️ <b>Ваша подписка была сокращена</b>\n\n"
                    f"Администратор удалил {days} дней из подписки <b>#{slot_number}</b>.\n\n"
                    f"⏰ <b>Осталось:</b> <b>{remaining_days}д {remaining_hours}ч</b>"
                )
                logger.info(f"User {tg_id} notified about subscription reduction by admin {admin_id}")
            except Exception as e:
                logger.warning(f"Failed to notify user {tg_id}: {e}")

            logger.info(f"Admin {admin_id} took {days} days from user {tg_id} slot {slot_number}, remaining: {remaining_days}д")

    except Exception as e:
        logger.error(f"Take subscription error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

    finally:
        await db.release_user_lock(tg_id)


@router.message(Command("enable_collab"))
async def admin_enable_collab(message: Message):
    """Админ команда: активировать партнёрство"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /enable_collab attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /enable_collab ТГ_ИД %\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>%</code> - процент дохода (15, 20, 25 или 30)\n\n"
            "<b>Пример:</b> /enable_collab 123456789 20"
        )
        logger.warning(f"Admin {admin_id} /enable_collab - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        percentage = int(parts[2])

        # Валидация значений
        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if percentage not in [15, 20, 25, 30]:
            await message.answer("❌ Процент должен быть одним из: 15, 20, 25, 30")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя активировать партнёрство самому себе")
            logger.warning(f"Admin {admin_id} tried to enable collab for themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД и % - целые числа\n"
            "• % - одно из: 15, 20, 25, 30\n\n"
            "<b>Пример:</b> /enable_collab 123456789 20"
        )
        logger.warning(f"Admin {admin_id} /enable_collab - parsing error for arguments: {parts[1:]}")
        return

    # Убедимся что пользователь существует в БД
    if not await db.user_exists(tg_id):
        await db.create_user(tg_id, f"user_{tg_id}")
        logger.info(f"Created new user {tg_id} in database for admin {admin_id}")

    # Создаём партнёрство
    await db.create_partnership(tg_id, percentage)

    await message.answer(
        f"✅ <b>Партнёрство активировано!</b>\n\n"
        f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
        f"💯 <b>Процент дохода:</b> {percentage}%\n"
        f"<b>Статус:</b> активно"
    )

    # Уведомляем пользователя
    try:
        await message.bot.send_message(
            tg_id,
            f"🎉 <b>Поздравляем!</b>\n\n"
            f"Вы активированы как партнёр нашего сервиса!\n\n"
            f"💯 <b>Ваш процент дохода:</b> <b>{percentage}%</b>\n\n"
            f"В главном меню появилась новая кнопка 'Партнёрство' — нажмите на неё, чтобы начать зарабатывать! 💰"
        )
        logger.info(f"User {tg_id} notified about partnership activation by admin {admin_id}")
    except Exception as e:
        logger.warning(f"Failed to notify user {tg_id}: {e}")
        await message.answer(
            f"⚠️ Партнёрство активировано, но не удалось отправить уведомление пользователю\n"
            f"(Ошибка: {str(e)[:50]})"
        )

    logger.info(f"Admin {admin_id} enabled collab for user {tg_id} with percentage {percentage}")


@router.message(Command("stats"))
async def admin_stats(message: Message):
    """Админ команда: получить статистику"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /stats attempt from user {admin_id}")
        return

    try:
        # TODO: Реализовать получение полной статистики из БД
        await message.answer(
            "📊 <b>Статистика бота</b>\n\n"
            "Функция в разработке...\n\n"
            "<i>Будут доступны:</i>\n"
            "• 👥 Количество активных пользователей\n"
            "• 💳 Статистика платежей\n"
            "• 🎁 Активированные подарки\n"
            "• 🎟 Использованные промокоды\n"
            "• 👥 Статистика рефералов"
        )
        logger.info(f"Admin {admin_id} requested /stats")
    except Exception as e:
        await message.answer(f"❌ Ошибка при получении статистики: {str(e)[:100]}")
        logger.error(f"Error getting stats for admin {admin_id}: {e}")


@router.message(Command("all_sms"))
async def admin_broadcast_all(message: Message, state: FSMContext):
    """Админ команда: начать рассылку всем пользователям"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /all_sms attempt from user {admin_id}")
        return

    await state.set_state(BroadcastStates.waiting_for_broadcast_all)
    await message.answer(
        "📤 <b>Режим рассылки всем пользователям</b>\n\n"
        "Отправь сообщение, которое нужно разослать:\n"
        "• Только текст\n"
        "• Текст + фото\n"
        "• Текст + видео\n"
        "• Любая комбинация\n\n"
        "<i>После этого я предложу выбрать кнопки для сообщения</i>"
    )
    logger.info(f"Admin {admin_id} started /all_sms broadcast mode")


@router.message(BroadcastStates.waiting_for_broadcast_all)
async def handle_broadcast_all_message(message: Message, state: FSMContext):
    """Обработчик сообщения в режиме рассылки всем пользователям"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ У вас нет доступа")
        await state.clear()
        return

    await state.update_data(
        broadcast_mode="all",
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        selected_buttons=[],
        preview_sent=False,
    )
    await state.set_state(BroadcastStates.reviewing_broadcast)
    await message.answer(
        _broadcast_summary_text("all", []),
        reply_markup=_build_broadcast_summary_keyboard(),
    )


@router.message(Command("not_sub_sms"))
async def admin_broadcast_no_subscription(message: Message, state: FSMContext):
    """Админ команда: начать рассылку пользователям без активной подписки"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /not_sub_sms attempt from user {admin_id}")
        return

    await state.set_state(BroadcastStates.waiting_for_broadcast_no_sub)
    await message.answer(
        "📤 <b>Режим рассылки пользователям без подписки</b>\n\n"
        "Отправь сообщение, которое нужно разослать:\n"
        "• Только текст\n"
        "• Текст + фото\n"
        "• Текст + видео\n"
        "• Любая комбинация\n\n"
        "<i>После этого я предложу выбрать кнопки для сообщения</i>"
    )
    logger.info(f"Admin {admin_id} started /not_sub_sms broadcast mode")


@router.message(BroadcastStates.waiting_for_broadcast_no_sub)
async def handle_broadcast_no_sub_message(message: Message, state: FSMContext):
    """Обработчик сообщения в режиме рассылки пользователям без подписки"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ У вас нет доступа")
        await state.clear()
        return

    await state.update_data(
        broadcast_mode="no_sub",
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        selected_buttons=[],
        preview_sent=False,
    )
    await state.set_state(BroadcastStates.reviewing_broadcast)
    await message.answer(
        _broadcast_summary_text("no_sub", []),
        reply_markup=_build_broadcast_summary_keyboard(),
    )


@router.callback_query(BroadcastStates.reviewing_broadcast, F.data == "broadcast_choose_buttons")
@router.callback_query(BroadcastStates.choosing_broadcast_buttons, F.data == "broadcast_choose_buttons")
async def choose_broadcast_buttons(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        await state.clear()
        return

    data = await state.get_data()
    selected_buttons = data.get("selected_buttons") or []
    await state.set_state(BroadcastStates.choosing_broadcast_buttons)
    await callback.message.edit_text(
        _broadcast_button_selection_text(selected_buttons),
        reply_markup=_build_broadcast_admin_keyboard(selected_buttons),
    )
    await callback.answer()


@router.callback_query(BroadcastStates.choosing_broadcast_buttons, F.data.startswith("broadcast_toggle:"))
async def toggle_broadcast_button(callback: CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        await state.clear()
        return

    key = callback.data.split(":", 1)[1]
    if key not in BROADCAST_BUTTONS:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return

    data = await state.get_data()
    selected_buttons = list(data.get("selected_buttons") or [])
    if key in selected_buttons:
        selected_buttons.remove(key)
    else:
        selected_buttons.append(key)

    selected_buttons = [button_key for button_key in BROADCAST_BUTTON_ORDER if button_key in selected_buttons]
    await state.update_data(selected_buttons=selected_buttons, preview_sent=False)
    await callback.message.edit_text(
        _broadcast_button_selection_text(selected_buttons),
        reply_markup=_build_broadcast_admin_keyboard(selected_buttons),
    )
    await callback.answer()


@router.callback_query(BroadcastStates.choosing_broadcast_buttons, F.data == "broadcast_no_buttons")
async def clear_broadcast_buttons(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        await state.clear()
        return

    data = await state.get_data()
    if not data.get("selected_buttons"):
        await callback.answer("Кнопок уже нет")
        return

    await state.update_data(selected_buttons=[], preview_sent=False)
    await callback.message.edit_text(
        _broadcast_button_selection_text([]),
        reply_markup=_build_broadcast_admin_keyboard([]),
    )
    await callback.answer("Кнопки убраны")


@router.callback_query(BroadcastStates.reviewing_broadcast, F.data == "broadcast_preview")
@router.callback_query(BroadcastStates.choosing_broadcast_buttons, F.data == "broadcast_preview")
async def preview_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        await state.clear()
        return

    try:
        if not await _send_broadcast_preview(callback, state):
            return
        data = await state.get_data()
        await callback.message.edit_text(
            _broadcast_summary_text(data.get("broadcast_mode"), data.get("selected_buttons") or [])
            + "\n\n✅ <b>Предпросмотр отправлен.</b> Если всё выглядит правильно, запускай рассылку.",
            reply_markup=_build_broadcast_ready_keyboard(),
        )
        await callback.answer("Предпросмотр отправлен")
    except Exception as e:
        logger.error(f"Broadcast preview error: {e}", exc_info=True)
        await callback.answer(f"Ошибка предпросмотра: {str(e)[:80]}", show_alert=True)


@router.callback_query(BroadcastStates.reviewing_broadcast, F.data == "broadcast_cancel")
@router.callback_query(BroadcastStates.choosing_broadcast_buttons, F.data == "broadcast_cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        await state.clear()
        return

    await state.clear()
    await callback.message.edit_text("🚫 <b>Рассылка отменена</b>")
    await callback.answer()


@router.callback_query(F.data.startswith("broadcast_stop:"))
async def stop_active_broadcast(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        return

    broadcast_id = callback.data.split(":", 1)[1]
    broadcast = ACTIVE_BROADCASTS.get(broadcast_id)
    if not broadcast:
        await callback.answer("Эта рассылка уже завершена", show_alert=True)
        return

    if broadcast.get("stop_requested"):
        await callback.answer("Остановка уже запрошена")
        return

    broadcast["stop_requested"] = True
    await callback.message.edit_text(
        "⛔ <b>Остановка запрошена</b>\n\n"
        "Рассылка остановится перед следующим получателем."
    )
    await callback.answer("Останавливаю рассылку")


@router.callback_query(BroadcastStates.reviewing_broadcast, F.data == "broadcast_start")
@router.callback_query(BroadcastStates.choosing_broadcast_buttons, F.data == "broadcast_start")
async def start_selected_broadcast(callback: CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        await state.clear()
        return

    data = await state.get_data()
    mode = data.get("broadcast_mode")
    source_chat_id = data.get("source_chat_id")
    source_message_id = data.get("source_message_id")
    selected_buttons = data.get("selected_buttons") or []
    preview_sent = data.get("preview_sent") is True
    if mode not in {"all", "no_sub"} or not source_chat_id or not source_message_id:
        await callback.answer("Данные рассылки устарели", show_alert=True)
        await state.clear()
        return
    if not preview_sent:
        await callback.answer("Сначала отправь предпросмотр", show_alert=True)
        return

    try:
        users = await _get_broadcast_users(mode)
        if not users:
            text = "❌ В БД нет пользователей" if mode == "all" else "❌ Не найдено пользователей без подписки"
            await callback.message.answer(text)
            await state.clear()
            await callback.answer()
            return

        await callback.message.edit_text("✅ <b>Рассылка подтверждена</b>")
        await callback.answer("Запускаю рассылку")
        broadcast_id = uuid.uuid4().hex
        ACTIVE_BROADCASTS[broadcast_id] = {
            "admin_id": admin_id,
            "mode": mode,
            "stop_requested": False,
            "started_at": datetime.utcnow(),
        }
        await _run_broadcast_copy(
            callback.bot,
            callback.message,
            users,
            source_chat_id,
            source_message_id,
            _build_broadcast_user_keyboard(selected_buttons),
            mode,
            broadcast_id,
        )
        logger.info(f"Admin {admin_id} completed selected broadcast mode={mode} buttons={selected_buttons}")
    except Exception as e:
        logger.error(f"Selected broadcast error: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка при рассылке: {str(e)[:100]}")
    finally:
        if 'broadcast_id' in locals():
            ACTIVE_BROADCASTS.pop(broadcast_id, None)
        await state.clear()
