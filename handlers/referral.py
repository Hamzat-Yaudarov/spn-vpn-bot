from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import database as db


router = Router()


@router.callback_query(F.data == "referral")
async def process_referral(callback: CallbackQuery):
    """Показать информацию о реферальной программе"""
    tg_id = callback.from_user.id
    
    # Получаем реферальную ссылку
    bot_username = (await callback.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{tg_id}"

    # Получаем статистику рефералов
    stats = db.get_referral_stats(tg_id)
    ref_count = stats[0] if stats else 0
    active_count = stats[1] if stats else 0

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Реферальная программа:</b>\n\n"
        "За каждого приглашённого друга, оформившего подписку,\n"
        "вы получаете +7 дней подписки!\n\n"
        f"Всего рефералов: {ref_count}\n"
        f"Всего рефералов, активировавших подписку: {active_count}\n\n"
        "Копируй свою реферальную ссылку и начинай зарабатывать!\n\n"
        f"<code>{referral_link}</code>"
    )

    await callback.message.edit_text(text, reply_markup=kb)
