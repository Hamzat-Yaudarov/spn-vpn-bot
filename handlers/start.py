import logging
from aiogram import Router, Bot
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import TELEGRAPH_AGREEMENT_URL, SUPPORT_URL, NEWS_CHANNEL_USERNAME
from states import UserStates
import database as db
from services.image_handler import send_text_with_photo


logger = logging.getLogger(__name__)

router = Router()


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    """Обработчик команды /start"""
    tg_id = message.from_user.id
    username = message.from_user.username
    user_already_exists = await db.user_exists(tg_id)

    # Проверяем наличие реферальной ссылки или партнёрской ссылки
    args = message.text.split()
    referrer_id = None
    partner_id = None

    if len(args) > 1:
        if args[1].startswith("ref_"):
            try:
                referrer_id = int(args[1].split("_")[1])
                # Проверяем что это не сам пользователь
                if referrer_id != tg_id:
                    if not user_already_exists:
                        logging.info(f"User {tg_id} joined via referral link from {referrer_id}")
                    else:
                        logging.warning(f"User {tg_id} is not new, ignoring referral link from {referrer_id}")
                        referrer_id = None
                else:
                    logging.warning(f"User {tg_id} tried to use their own referral link")
                    referrer_id = None
            except (ValueError, IndexError):
                referrer_id = None
        elif args[1].startswith("partner_"):
            try:
                partner_id = int(args[1].split("_")[1])
                # add_partner_referral теперь возвращает bool и делает все проверки
                success = await db.add_partner_referral(partner_id, tg_id)
                if success:
                    logging.info(f"User {tg_id} joined via partner link from {partner_id}")
                # Если функция вернула False, логирование уже сделано внутри функции
            except (ValueError, IndexError):
                partner_id = None

    # Создаём пользователя если его нет
    await db.create_user(tg_id, username, referrer_id)

    if referrer_id is not None and not user_already_exists:
        await db.update_referral_count(referrer_id)

    # Проверяем принял ли пользователь условия
    if not await db.has_accepted_terms(tg_id):
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
    tg_id = message.from_user.id
    is_partner = await db.is_partner(tg_id)

    keyboard = [
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="buy_subscription", style="success")],
        [InlineKeyboardButton(text="📲 Инструкция", callback_data="how_to_connect")],
        [InlineKeyboardButton(text="📢 Новостной канал", url=f"https://t.me/{NEWS_CHANNEL_USERNAME}")],
        [InlineKeyboardButton(text="👥 Бонус за друга", callback_data="referral")],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo")],
    ]

    # Добавляем кнопку партнёрства если пользователь партнёр
    if is_partner:
        keyboard.append([InlineKeyboardButton(text="🤝 Партнёрство", callback_data="partnership")])

    keyboard.append([InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)])

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    text = (
        "<b>SPN — стабильное и быстрое интернет-соединение</b>\n\n"
        "<b>Что вы получаете:</b>\n"
        "<blockquote>"
        "• Улучшенную работу сайтов, мессенджеров и онлайн-сервисов\n"
        "• Более стабильное соединение даже при перегрузках сети\n"
        "• Поддержку Android, iOS, Windows, macOS и Linux\n"
        "• Простое подключение за 1–2 минуты\n"
        "• Защиту и оптимизацию интернет-трафика"
        "</blockquote>\n\n"
        "<b>После активации:</b>\n"
        "<blockquote>"
        "🔐 Персональный доступ SPN на выбранный срок\n"
        "📥 Пошаговую инструкцию по подключению\n"
        "🛟 Поддержку в Telegram\n"
        "🌍 Свободную и стабильную работу в интернете"
        "</blockquote>\n\n"
        "<b>Реферальная программа:</b>\n"
        "<blockquote>"
        "👥 За каждого приглашённого пользователя:\n"
        "💰 35% от первой покупки\n"
        "💰 15% от повторных покупок"
        "</blockquote>"
    )

    await send_text_with_photo(message, text, kb, "Главное меню")
