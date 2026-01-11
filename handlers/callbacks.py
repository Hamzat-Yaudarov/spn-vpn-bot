import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import SUPPORT_URL
from states import UserStates
import database as db
from handlers.start import show_main_menu


router = Router()


@router.callback_query(F.data == "accept_terms")
async def process_accept_terms(callback: CallbackQuery, state: FSMContext):
    """Обработчик принятия условий использования"""
    tg_id = callback.from_user.id
    db.accept_terms(tg_id)

    await callback.message.delete()
    await state.clear()

    await callback.bot.send_message(
        callback.message.chat.id,
        "Соглашение принято! Добро пожаловать!"
    )

    await show_main_menu(callback.message)


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔐 Моя подписка", callback_data="my_subscription")],
        [InlineKeyboardButton(text="📲 Как подключиться", callback_data="how_to_connect")],
        [InlineKeyboardButton(text="🎁 Получить подарок", callback_data="get_gift")],
        [InlineKeyboardButton(text="👥 Бонус за друга", callback_data="referral")],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)]
    ])

    text = (
        "🚀 Добро пожаловать в SPN VPN\n"
        "Быстрый и стабильный VPN без логов\n"
        "Работает на мобильных и ПК"
    )

    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "how_to_connect")
async def process_how_to_connect(callback: CallbackQuery):
    """Показать инструкцию по подключению"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Как подключиться</b>\n\n"
        "Способ 1 — v2RayTun\n"
        "• Скачай приложение\n"
        "• Вставь свою ссылку подписки\n"
        "• Готово!\n\n"
        "Способ 2 — Happ\n"
        "• Скачай приложение\n"
        "• Вставь свою ссылку подписки\n"
        "• Готово!\n\n"
        "Ничего настраивать вручную не нужно!"
    )

    await callback.message.edit_text(text, reply_markup=kb)
