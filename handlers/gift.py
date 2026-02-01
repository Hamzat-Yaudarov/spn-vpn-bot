import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import NEWS_CHANNEL_USERNAME


router = Router()


@router.callback_query(F.data == "get_gift")
async def process_get_gift(callback: CallbackQuery):
    """Обработчик получения подарка - теперь просто перенаправляет на новостной канал"""
    tg_id = callback.from_user.id
    username = callback.from_user.username
    logging.info(f"User {tg_id}(@{username}) clicked gift button (redirected to news channel)")

    # Просто перенаправляем на новостной канал с сообщением
    text = (
        "📢 <b>Новостной канал SPN VPN</b>\n\n"
        "Следите за новостями и обновлениями нашего сервиса!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Перейти на канал", url=f"https://t.me/{NEWS_CHANNEL_USERNAME}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    await callback.message.answer(text, reply_markup=kb)
