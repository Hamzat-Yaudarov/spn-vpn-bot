import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import database as db


router = Router()


@router.callback_query(F.data == "referral")
async def process_referral(callback: CallbackQuery):
    """Показать информацию о реферальной программе"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} viewing referral program")

    # Получаем реферальную ссылку
    bot_username = (await callback.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{tg_id}"

    # Получаем статистику рефералов
    stats = await db.get_referral_stats(tg_id)
    ref_count = stats[0] if stats else 0
    active_count = stats[1] if stats else 0

    # Получаем баланс пользователя
    balance = await db.get_balance(tg_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>👥 Реферальная программа</b>\n\n"
        "<blockquote>"
        "Приглашайте друзей и получайте кешбэк за их покупки.\n\n"
        "💰 За каждого приглашённого пользователя,\n"
        "который оформит доступ, вы получаете <b>25%</b> от суммы его покупки.\n"
        "</blockquote>\n\n"
        f"👥 Всего приглашено: <b>{ref_count}</b>\n"
        f"✅ Активировали доступ: <b>{active_count}</b>\n"
        f"💰 Баланс кешбэка: <b>{balance:.2f} ₽</b>\n\n"
        "🔗 <b>Ваша персональная ссылка:</b>\n"
        f"<code>{referral_link}</code>\n\n"
        "ℹ️ <i>Кешбэк автоматически добавляется на баланс при покупке реферала.</i>"
    )

    await callback.message.edit_text(text, reply_markup=kb)
