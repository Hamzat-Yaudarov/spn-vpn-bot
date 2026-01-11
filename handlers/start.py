import logging
from aiogram import Router, Bot
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import TELEGRAPH_AGREEMENT_URL, SUPPORT_URL
from states import UserStates
import database as db


router = Router()


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    """Обработчик команды /start"""
    tg_id = message.from_user.id
    username = message.from_user.username

    # Проверяем наличие реферальной ссылки
    args = message.text.split()
    referrer_id = None
    
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1].split("_")[1])
            db.update_referral_count(referrer_id)
            logging.info(f"User {tg_id} joined via referral link from {referrer_id}")
        except (ValueError, IndexError):
            referrer_id = None

    # Создаём пользователя если его нет
    db.create_user(tg_id, username, referrer_id)

    # Проверяем принял ли пользователь условия
    if not db.has_accepted_terms(tg_id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять", callback_data="accept_terms")],
            [InlineKeyboardButton(text="📄 Прочитать соглашение", url=TELEGRAPH_AGREEMENT_URL)]
        ])
        await message.answer(
            "Перед использованием бота необходимо ознакомиться и принять пользовательское соглашение.",
            reply_markup=kb
        )
        await state.set_state(UserStates.waiting_for_agreement)
    else:
        await show_main_menu(message)


async def show_main_menu(message: Message):
    """Показать главное меню"""
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

    await message.answer(text, reply_markup=kb)
