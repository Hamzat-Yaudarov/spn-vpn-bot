import html
import logging
from datetime import timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from config import SUPPORT_URL
from services.reactivation_campaigns import activate_reactivation_offer


logger = logging.getLogger(__name__)
router = Router()
MSK = ZoneInfo("Europe/Moscow")


ERROR_MESSAGES = {
    "not_found": "Это предложение не найдено.",
    "already_claimed": "Вы уже получили бесплатный доступ по этой акции.",
    "not_available": "Это предложение больше недоступно.",
    "active_subscription": "У вас уже есть активная подписка.",
    "purchase_found": "Предложение закрыто, потому что после его получения у вас уже была покупка.",
    "no_slot": "Нет свободного места для новой подписки. Удалите ненужную истёкшую подписку и попробуйте снова.",
    "remnawave_unavailable": "Не удалось активировать доступ. Попробуйте нажать кнопку ещё раз немного позже.",
}


async def _delete_message_safely(bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as exc:
        logger.debug("Could not delete claimed reactivation message %s: %s", message_id, exc)


@router.callback_query(F.data.startswith("reactivation_claim:"))
async def process_reactivation_claim(callback: CallbackQuery):
    tg_id = callback.from_user.id
    try:
        offer_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректное предложение", show_alert=True)
        return

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Активация уже выполняется", show_alert=True)
        return

    await callback.answer("Активируем бесплатный доступ…")
    try:
        result = await activate_reactivation_offer(offer_id, tg_id)
    except Exception as exc:
        logger.error("Reactivation claim failed for user %s: %s", tg_id, exc, exc_info=True)
        result = {"error": "remnawave_unavailable"}
    finally:
        await db.release_user_lock(tg_id)

    error = result.get("error")
    if error:
        await callback.message.answer(ERROR_MESSAGES.get(error, "Не удалось активировать доступ. Попробуйте позже."))
        return

    current_message_id = getattr(callback.message, "message_id", None)
    last_message_id = result.get("last_message_id")
    await _delete_message_safely(callback.bot, callback.message.chat.id, current_message_id)
    if last_message_id and last_message_id != current_message_id:
        await _delete_message_safely(callback.bot, callback.message.chat.id, last_message_id)

    subscription_id = result["subscription_id"]
    expires_at = result["expires_at"]
    expires_at_utc = expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at.astimezone(timezone.utc)
    expires_at_msk = expires_at_utc.astimezone(MSK)
    support_url = SUPPORT_URL or "https://t.me/wayspn_support"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🤖 Подключить Android",
            callback_data=f"subscription_instruction_android_{subscription_id}",
            style="success",
        )],
        [InlineKeyboardButton(
            text="🍎 Подключить iPhone",
            callback_data=f"subscription_instruction_iphone_{subscription_id}",
            style="success",
        )],
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=support_url, style="primary")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu", style="primary")],
    ])
    await callback.bot.send_message(
        callback.message.chat.id,
        (
            "✅ <b>Бесплатный доступ активирован!</b>\n\n"
            f"Срок: <b>{result['days']} дн.</b>\n"
            f"Действует до: <b>{expires_at_msk.strftime('%d.%m.%Y %H:%M')} МСК</b>\n"
            f"Трафик: <b>{result['traffic_gb']} ГБ</b>\n"
            "Тип: <b>с антиглушилкой</b>\n\n"
            "Выберите своё устройство — бот покажет приложение и короткую инструкцию.\n\n"
            "<b>Ваш ключ:</b>\n"
            f"<code>{html.escape(result['subscription_url'])}</code>"
        ),
        reply_markup=keyboard,
    )
