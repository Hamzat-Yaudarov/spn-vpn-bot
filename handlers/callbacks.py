import logging
from pathlib import Path
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import SUPPORT_URL, NEWS_CHANNEL_USERNAME
from states import UserStates
import database as db
from handlers.start import show_main_menu
from services.image_handler import edit_text_with_photo, edit_media_to_video


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

    # Проверяем является ли пользователь партнёром
    partner_info = await db.get_partner_info(tg_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔐 Моя подписка", callback_data="my_subscription")],
        [InlineKeyboardButton(text="📲 Как подключиться", callback_data="how_to_connect")],
        [InlineKeyboardButton(text="📢 Новостной канал", url=f"https://t.me/{NEWS_CHANNEL_USERNAME}")],
        [InlineKeyboardButton(text="👥 Бонус за друга", callback_data="referral")],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo")],
    ])

    # Добавляем кнопку партнёрства если пользователь партнёр
    if partner_info:
        kb.inline_keyboard.insert(4, [InlineKeyboardButton(text="🤝 Партнёрство", callback_data="partnership")])

    kb.inline_keyboard.append([InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)])

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
        "👥 За каждого приглашённого пользователя,\n"
        "активировавшего доступ, вы получаете +7 дней"
        "</blockquote>"
    )

    await edit_text_with_photo(callback, text, kb, "Главное меню")


@router.callback_query(F.data == "how_to_connect")
async def process_how_to_connect(callback: CallbackQuery, state: FSMContext):
    """Показать выбор устройства для инструкции подключения"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: how_to_connect")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 iPhone", callback_data="device_iphone")],
        [InlineKeyboardButton(text="🤖 Android", callback_data="device_android")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu")]
    ])

    text = "Выберите устройство, для которого нужна инструкция:"

    await edit_text_with_photo(callback, text, kb, "Как подключиться")
    await state.set_state(UserStates.choosing_device)


@router.callback_query(F.data == "device_iphone", UserStates.choosing_device)
async def process_device_iphone(callback: CallbackQuery, state: FSMContext):
    """Показать инструкцию для iPhone с видео"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} selected: device_iphone")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад к выбору устройства", callback_data="how_to_connect")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu")]
    ])

    text = (
        "📱 <b>Инструкция для iPhone</b>\n\n"
        "Следуйте инструкциям на видео для установки VPN.\n\n"
        "<b>Ссылки:</b>\n"
        "• <a href=\"https://play.google.com/store/apps/details?id=com.v2raytun.android\">V2rayTUN</a>\n"
        "• <a href=\"https://apps.apple.com/app/id6446114838\">Happ</a>"
    )

    video_path = Path(__file__).parent.parent / "video_instructions3" / "ios.mp4"

    await edit_media_to_video(callback, video_path, text, kb)
    await state.set_state(UserStates.choosing_device)


@router.callback_query(F.data == "device_android", UserStates.choosing_device)
async def process_device_android(callback: CallbackQuery, state: FSMContext):
    """Показать инструкцию для Android с видео"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} selected: device_android")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад к выбору устройства", callback_data="how_to_connect")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu")]
    ])

    text = (
        "🤖 <b>Инструкция для Android</b>\n\n"
        "Следуйте инструкциям на изображении/видео ниже для установки VPN.\n\n"
        "<b>Ссылки:</b>\n"
        "• <a href=\"https://play.google.com/store/apps/details?id=com.v2raytun.android\">V2rayTUN</a>\n"
        "• <a href=\"https://apps.apple.com/app/id6446114838\">Happ</a>"
    )

    video_path = Path(__file__).parent.parent / "video_instructions3" / "android.mp4"

    await edit_media_to_video(callback, video_path, text, kb)
    await state.set_state(UserStates.choosing_device)
