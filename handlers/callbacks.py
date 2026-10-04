import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import SUPPORT_URL, PUBLIC_SITE_URL
import database as db
from handlers.start import (
    build_main_menu,
    build_more_menu,
    mobile_auth_keyboard,
    news_channel_chat,
    send_news_channel_offer,
    show_main_menu,
)
from services.image_handler import edit_text_with_photo
from services.mobile_auth import approve_challenge, pending_challenge_for_user
from services.custom_emoji import semantic_button
from services.connection_instructions import (
    ANDROID_PLATFORM,
    IPHONE_PLATFORM,
    build_connection_instruction,
    connection_app_button_text,
    connection_app_url,
)


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

    if await db.needs_news_channel_onboarding(tg_id):
        await callback.answer()
        await send_news_channel_offer(callback.bot, callback.message.chat.id)
        return

    pending_challenge = await pending_challenge_for_user(tg_id)
    if pending_challenge:
        text, keyboard = mobile_auth_keyboard(
            str(pending_challenge["id"]),
            pending_challenge.get("device_name"),
        )
        await callback.bot.send_message(callback.message.chat.id, text, reply_markup=keyboard)
    else:
        await show_main_menu(callback.message, callback.from_user.id)


@router.callback_query(F.data == "check_news_channel")
async def process_check_news_channel(callback: CallbackQuery, state: FSMContext):
    """Проверить подписку нового пользователя и завершить welcome-экран."""
    tg_id = callback.from_user.id
    chat_id = callback.message.chat.id
    await callback.answer()

    try:
        await callback.message.delete()
    except Exception as exc:
        logger.debug("Could not delete news-channel prompt for user %s: %s", tg_id, exc)

    if not await db.needs_news_channel_onboarding(tg_id):
        await state.clear()
        await show_main_menu(callback.message, tg_id)
        return

    try:
        member = await callback.bot.get_chat_member(news_channel_chat(), tg_id)
        status = getattr(member.status, "value", member.status)
    except Exception as exc:
        logger.error("Failed to check news-channel membership for user %s: %s", tg_id, exc)
        await callback.bot.send_message(
            chat_id,
            "Не удалось проверить подписку. Попробуйте ещё раз через несколько секунд.",
        )
        await send_news_channel_offer(callback.bot, chat_id)
        return

    if status not in {"member", "administrator", "creator"}:
        logger.info("News-channel membership not found for user %s: %s", tg_id, status)
        await send_news_channel_offer(callback.bot, chat_id, retry=True)
        return

    try:
        completed = await db.complete_news_channel_onboarding(tg_id)
    except Exception as exc:
        logger.exception("News-channel onboarding completion failed for user %s: %s", tg_id, exc)
        completed = False

    if not completed and await db.needs_news_channel_onboarding(tg_id):
        await callback.bot.send_message(
            chat_id,
            "Не удалось завершить проверку. Попробуйте ещё раз чуть позже.",
        )
        await send_news_channel_offer(callback.bot, chat_id)
        return

    await state.clear()
    await show_main_menu(callback.message, tg_id, welcome=True)

    pending_challenge = await pending_challenge_for_user(tg_id)
    if pending_challenge:
        text, keyboard = mobile_auth_keyboard(
            str(pending_challenge["id"]),
            pending_challenge.get("device_name"),
        )
        await callback.bot.send_message(chat_id, text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("mobile_auth_approve:"))
async def process_mobile_auth_approval(callback: CallbackQuery, state: FSMContext):
    """Явное одноразовое подтверждение входа в Android-приложение."""
    challenge_id = callback.data.split(":", 1)[1]
    if not await db.has_accepted_terms(callback.from_user.id):
        await callback.answer("Сначала примите пользовательское соглашение", show_alert=True)
        return
    if not await approve_challenge(challenge_id, callback.from_user.id):
        await callback.answer("Запрос входа истёк или уже использован", show_alert=True)
        return

    await callback.answer("Вход подтверждён")
    await callback.message.edit_text(
        "✅ <b>Вход в Way VPN подтверждён.</b>\n\nНажмите кнопку ниже, чтобы безопасно вернуться в приложение.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            semantic_button(
                text="↩️ Вернуться в Way VPN",
                url=f"{PUBLIC_SITE_URL.rstrip('/')}/mobile/auth-return",
                style="success",
            )
        ]]),
    )
    await state.clear()


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} returned to main menu")

    await state.clear()
    text, keyboard = build_main_menu()
    await edit_text_with_photo(callback, text, keyboard, "Главное меню")


@router.callback_query(F.data == "more_menu")
async def process_more_menu(callback: CallbackQuery, state: FSMContext):
    """Показать второстепенные функции."""
    await state.clear()
    text, keyboard = await build_more_menu(callback.from_user.id)
    await edit_text_with_photo(callback, text, keyboard, "Главное меню")


@router.callback_query(F.data == "how_to_connect")
async def process_how_to_connect(callback: CallbackQuery, state: FSMContext):
    """Показать раздел с инструкциями."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: how_to_connect")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [semantic_button(text="🤖 Android", callback_data="instruction_connect_android", style="primary")],
        [semantic_button(text="🍎 iPhone", callback_data="instruction_connect_iphone", style="primary")],
        [semantic_button(text="← Назад", callback_data="back_to_menu", style="primary")]
    ])

    text = (
        "📲 <b>Как подключить</b>\n\nВыберите ваше устройство."
    )

    await edit_text_with_photo(callback, text, kb, "Как подключиться")
    await state.clear()


@router.callback_query(F.data == "instruction_buy")
async def process_instruction_buy(callback: CallbackQuery, state: FSMContext):
    """Показать инструкцию покупки подписки."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened buy instruction")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [semantic_button(text="🛒 Купить подписку", callback_data="buy_subscription", style="success")],
        [semantic_button(text="← Назад", callback_data="how_to_connect", style="primary")]
    ])

    text = (
        "🛒 <b>Как купить подписку</b>\n\n"
        "1. Нажмите <b>Купить подписку</b>\n"
        "2. Выберите тип и срок\n"
        "3. Оплатите счёт\n"
        "4. Бот сразу выдаст ключ и инструкцию\n\n"
        f"По всем вопросам: {SUPPORT_URL}"
    )

    await edit_text_with_photo(callback, text, kb, "Как подключиться")
    await state.clear()


@router.callback_query(F.data == "instruction_connect")
async def process_instruction_connect(callback: CallbackQuery, state: FSMContext):
    """Предложить выбрать устройство для инструкции подключения."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened connection platform selection")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [semantic_button(text="🤖 Android", callback_data="instruction_connect_android", style="primary")],
        [semantic_button(text="🍎 iPhone", callback_data="instruction_connect_iphone", style="primary")],
        [semantic_button(text="← Назад", callback_data="back_to_menu", style="primary")]
    ])

    text = (
        "📲 <b>Как подключить</b>\n\nВыберите ваше устройство."
    )

    await edit_text_with_photo(callback, text, kb, "Как подключиться")
    await state.clear()


@router.callback_query(F.data.in_({"instruction_connect_android", "instruction_connect_iphone"}))
async def process_instruction_connect_platform(callback: CallbackQuery, state: FSMContext):
    """Показать инструкцию и ссылку на приложение для выбранной платформы."""
    platform = ANDROID_PLATFORM if callback.data.endswith("_android") else IPHONE_PLATFORM
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened {platform} connection instruction")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [semantic_button(
            text=connection_app_button_text(platform),
            url=connection_app_url(platform),
            style="success",
        )],
        [semantic_button(text="🔑 Мои подписки", callback_data="my_subscriptions", style="primary")],
        [semantic_button(text="📱 Другое устройство", callback_data="instruction_connect", style="primary")],
        [semantic_button(text="🏠 Главное меню", callback_data="back_to_menu", style="primary")],
    ])

    await edit_text_with_photo(
        callback,
        build_connection_instruction(platform, support_url=SUPPORT_URL),
        kb,
        "Как подключиться",
    )
    await state.clear()
