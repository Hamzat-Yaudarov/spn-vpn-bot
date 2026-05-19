import asyncio
import logging
import aiohttp
import asyncio
import html
import re
from datetime import datetime, timedelta, timezone
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from config import (
    ADMIN_ID,
    BYPASS_BASE_TRAFFIC_GB,
    BYPASS_HWID_DEVICE_LIMIT,
    BYPASS_SQUAD_UUID,
    DEFAULT_SQUAD_UUID,
    GB_BYTES,
    REGULAR_HWID_DEVICE_LIMIT,
    REGULAR_SQUAD_UUID,
)
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_set_subscription_expiry,
    remnawave_get_user_info,
    remnawave_get_subscription_url,
)

logger = logging.getLogger(__name__)

router = Router()


class BroadcastStates(StatesGroup):
    """Состояния для рассылок сообщений"""
    waiting_for_broadcast_all = State()
    waiting_for_broadcast_no_sub = State()


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
    link = f"https://t.me/{bot_username}?start={code}"
    await message.answer(
        "✅ <b>Tracking-ссылка создана</b>\n\n"
        f"<b>Код:</b> <code>{code}</code>\n"
        f"<b>Название:</b> {html.escape(title) if title else 'не указано'}\n\n"
        f"<b>Ссылка:</b>\n<code>{link}</code>\n\n"
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
    url = f"https://t.me/{bot_username}?start={code}"
    await message.answer(
        f"📊 <b>Статистика ссылки <code>{code}</code></b>\n\n"
        f"<b>Название:</b> {html.escape(link['title']) if link.get('title') else 'не указано'}\n"
        f"<b>Статус:</b> {'активна' if link['is_active'] else 'выключена'}\n"
        f"<b>Ссылка:</b> <code>{url}</code>\n\n"
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

        if len(code) < 3:
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
        traffic_limit_bytes = subscription.get("current_period_limit_bytes") or base_traffic_bytes if plan_kind == "bypass" else 0
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
                    traffic_reset_at = COALESCE(traffic_reset_at, $7),
                    hwid_device_limit = $8,
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
                    now + timedelta(days=30) if plan_kind == "bypass" else None,
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
        "<i>Я скопирую это сообщение всем пользователям</i>"
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

    try:
        # Получаем всех пользователей из БД
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            users = await conn.fetch("SELECT tg_id FROM users ORDER BY tg_id")

        if not users:
            await message.answer("❌ В БД нет пользователей")
            logger.warning(f"Admin {admin_id} /all_sms - no users found")
            await state.clear()
            return

        total_users = len(users)
        sent_count = 0
        error_count = 0
        blocked_count = 0
        unreachable_count = 0
        rate_limited_delay = 0.3

        await message.answer(
            f"📤 <b>Начинаю рассылку всем {total_users} пользователям...</b>\n\n"
            f"<i>Это может занять некоторое время...</i>"
        )

        # Копируем сообщение каждому пользователю с rate limiting
        for user_record in users:
            user_id = user_record['tg_id']
            try:
                await message.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                sent_count += 1
                logger.info(f"Broadcast message copied to user {user_id}")
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

            await asyncio.sleep(rate_limited_delay)

        await message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• ✅ Отправлено: {sent_count}/{total_users}\n"
            f"• 🚫 Заблокировано: {blocked_count}\n"
            f"• 📴 Недоступно: {unreachable_count}\n"
            f"• ❌ Ошибок: {error_count}"
        )

        logger.info(f"Admin {admin_id} completed /all_sms broadcast: sent={sent_count}, blocked={blocked_count}, unreachable={unreachable_count}, errors={error_count}")

    except Exception as e:
        logger.error(f"Broadcast all error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при рассылке: {str(e)[:100]}")
    finally:
        await state.clear()


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
        "<i>Я скопирую это сообщение всем пользователям без активной подписки</i>"
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

    try:
        # Получаем пользователей БЕЗ активной подписки
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            users = await conn.fetch(
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

        if not users:
            await message.answer("❌ Не найдено пользователей без подписки")
            logger.info(f"Admin {admin_id} /not_sub_sms - no users without subscription found")
            await state.clear()
            return

        total_users = len(users)
        sent_count = 0
        error_count = 0
        blocked_count = 0
        unreachable_count = 0
        rate_limited_delay = 0.3

        await message.answer(
            f"📤 <b>Начинаю рассылку {total_users} пользователям без подписки...</b>\n\n"
            f"<i>Это может занять некоторое время...</i>"
        )

        # Копируем сообщение каждому пользователю с rate limiting
        for user_record in users:
            user_id = user_record['tg_id']
            try:
                await message.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                sent_count += 1
                logger.info(f"Broadcast message copied to user {user_id} (no subscription)")
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

            await asyncio.sleep(rate_limited_delay)

        await message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• ✅ Отправлено: {sent_count}/{total_users}\n"
            f"• 🚫 Заблокировано: {blocked_count}\n"
            f"• 📴 Недоступно: {unreachable_count}\n"
            f"• ❌ Ошибок: {error_count}"
        )

        logger.info(f"Admin {admin_id} completed /not_sub_sms broadcast: sent={sent_count}, blocked={blocked_count}, unreachable={unreachable_count}, errors={error_count}")

    except Exception as e:
        logger.error(f"Broadcast no subscription error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при рассылке: {str(e)[:100]}")
    finally:
        await state.clear()
