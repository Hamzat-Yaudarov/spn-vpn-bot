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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔐 Моя подписка", callback_data="my_subscription")],
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="balance")],
        [InlineKeyboardButton(text="📲 Как подключиться", callback_data="how_to_connect")],
        [InlineKeyboardButton(text="🎁 Получить подарок", callback_data="get_gift")],
        [InlineKeyboardButton(text="👥 Бонус за друга", callback_data="referral")],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)]
    ])

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

    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "how_to_connect")
async def process_how_to_connect(callback: CallbackQuery):
    """Показать инструкцию по подключению"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    text = (
        "<b>Как подключиться</b>\n\n"

        "<b>Способ 1 — v2RayTun</b>\n"
        "<blockquote>"
        "1️⃣ Установите приложение:\n"
        "• <a href=\"https://play.google.com/store/apps/details?id=com.v2raytun.android\">Android</a>\n"
        "• <a href=\"https://apps.apple.com/app/id6446114838\">iOS</a>\n\n"
        "2️⃣ Откройте приложение и добавьте новую конфигурацию\n"
        "3️⃣ Вставьте персональную ссылку доступа\n"
        "4️⃣ Активируйте соединение\n"
        "</blockquote>\n\n"

        "<b>Способ 2 — Happ</b>\n"
        "<blockquote>"
        "1️⃣ Установите приложение Happ из магазина приложений\n"
        "2️⃣ Откройте приложение и вставьте ссылку доступа\n"
        "3️⃣ Подтвердите подключение\n"
        "</blockquote>\n\n"

        "ℹ️ <i>Никаких ручных настроек и сложных параметров — всё работает автоматически.</i>"
    )

    await callback.message.edit_text(text, reply_markup=kb)
