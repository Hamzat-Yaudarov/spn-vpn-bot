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

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Скопировать ссылку", callback_data="copy_referral_link")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Реферальная программа</b>\n\n"
        "<blockquote>"
        "Приглашайте друзей и получайте бонусы за их активацию.\n\n"
        "🎁 За каждого приглашённого пользователя,\n"
        "который оформит доступ, вы получаете <b>+7 дней</b>.\n"
        "</blockquote>\n\n"
        f"👥 Всего приглашено: <b>{ref_count}</b>\n"
        f"✅ Активировали доступ: <b>{active_count}</b>\n\n"
        "🔗 <b>Ваша персональная ссылка:</b>\n"
        f"<code>{referral_link}</code>\n\n"
        "ℹ️ <i>Чем больше активных пользователей — тем дольше ваш доступ.</i>"
    )

    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "copy_referral_link")
async def copy_referral_link(callback: CallbackQuery):
    """Скопировать реферальную ссылку в буфер обмена"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} copied referral link")

    # Получаем реферальную ссылку
    bot_username = (await callback.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{tg_id}"

    # Отправляем уведомление с сообщением "Скопировано"
    await callback.answer("✅ Ссылка скопирована в буфер обмена!", show_alert=False)

    # Отправляем сообщение с ссылкой, которую пользователь сможет легко скопировать
    await callback.bot.send_message(
        tg_id,
        f"<b>Ваша реферальная ссылка:</b>\n\n<code>{referral_link}</code>\n\n"
        "Нажми на код выше, чтобы скопировать ссылку.",
        parse_mode="HTML"
    )
