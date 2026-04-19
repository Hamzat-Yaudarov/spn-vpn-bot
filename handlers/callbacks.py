import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import SUPPORT_URL, NEWS_CHANNEL_USERNAME
import database as db
from handlers.start import show_main_menu
from services.image_handler import edit_text_with_photo


logger = logging.getLogger(__name__)


router = Router()


@router.callback_query(F.data == "accept_terms")
async def process_accept_terms(callback: CallbackQuery, state: FSMContext):
    """Обработчик принятия условий использования"""
    tg_id = callback.from_user.id
    username = callback.from_user.username
    logging.info(f"User {tg_id}(@{username}) accepted terms")

    await db.accept_terms(tg_id)

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
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} returned to main menu")

    await state.clear()
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
        "<b>Реферальная программа:</b>\n"
        "<blockquote>"
        "👥 За каждого приглашённого пользователя:\n"
        "💰 35% от первой покупки\n"
        "💰 15% от повторных покупок"
        "</blockquote>"
    )

    await edit_text_with_photo(callback, text, kb, "Главное меню")


@router.callback_query(F.data == "how_to_connect")
async def process_how_to_connect(callback: CallbackQuery, state: FSMContext):
    """Показать раздел с инструкциями."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: how_to_connect")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Как купить подписку", callback_data="instruction_buy")],
        [InlineKeyboardButton(text="📲 Как подключить VPN", callback_data="instruction_connect")],
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu")]
    ])

    text = (
        "📲 <b>Инструкция</b>\n\n"
        "Здесь собраны самые важные шаги:\n"
        "• как купить подписку\n"
        "• как подключить VPN\n\n"
        "Выбери нужный раздел ниже."
    )

    await edit_text_with_photo(callback, text, kb, "Как подключиться")
    await state.clear()


@router.callback_query(F.data == "instruction_buy")
async def process_instruction_buy(callback: CallbackQuery, state: FSMContext):
    """Показать инструкцию покупки подписки."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened buy instruction")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="📲 Как подключить VPN", callback_data="instruction_connect")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu")]
    ])

    text = (
        "🛒 <b>Как купить подписку</b>\n\n"
        "1. Открой раздел <b>🔐 Мои подписки</b>\n"
        "2. Нажми <b>Купить первую подписку</b> или <b>Купить ещё подписку</b>\n"
        "3. Выбери срок подписки\n"
        "4. Выбери удобный способ оплаты\n"
        "5. Оплати счёт\n"
        "6. После оплаты бот сразу пришлёт тебе ключ и кнопку <b>📲 Инструкция</b>\n\n"
        "Если уже есть подписка, ты можешь открыть её и продлить отдельно.\n\n"
        f"По всем вопросам: {SUPPORT_URL}"
    )

    await edit_text_with_photo(callback, text, kb, "Как подключиться")
    await state.clear()


@router.callback_query(F.data == "instruction_connect")
async def process_instruction_connect(callback: CallbackQuery, state: FSMContext):
    """Показать инструкцию подключения VPN."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened connect instruction")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🛒 Как купить подписку", callback_data="instruction_buy")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu")]
    ])

    text = (
        "📲 <b>Как подключить VPN</b>\n\n"
        "1. Открой раздел <b>🔐 Мои подписки</b>\n"
        "2. Выбери нужную подписку\n"
        "3. Открой её и скопируй ключ\n"
        "4. Скачай <b>Happ Plus</b> из Google Play или App Store\n"
        "5. Открой приложение <b>Happ Plus</b>\n"
        "6. Нажми <b>+</b> в правом верхнем углу\n"
        "7. Выбери <b>Вставить из буфера обмена</b>\n"
        "8. Подтверди добавление конфигурации\n"
        "9. Включи подключение\n\n"
        "Если приложение просит разрешения, просто подтверди их.\n\n"
        f"Если что-то не получается: {SUPPORT_URL}"
    )

    await edit_text_with_photo(callback, text, kb, "Как подключиться")
    await state.clear()
