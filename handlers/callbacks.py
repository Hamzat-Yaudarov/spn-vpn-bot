import html
import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import ADMIN_ID, ADMIN_PANEL_URL, MINIAPP_URL, SUPPORT_URL, NEWS_CHANNEL_USERNAME, PUBLIC_SITE_URL
import database as db
from handlers.start import (
    mobile_auth_keyboard,
    news_channel_chat,
    send_news_channel_offer,
    show_main_menu,
)
from services.channel_bonus import activate_news_channel_bonus
from services.image_handler import edit_text_with_photo
from services.mobile_auth import approve_challenge, pending_challenge_for_user
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

    await callback.bot.send_message(
        callback.message.chat.id,
        "Соглашение принято! Добро пожаловать!"
    )

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
    """Проверить подписку нового пользователя и выдать однодневный бонус."""
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

    if not await db.acquire_user_lock(tg_id):
        await callback.bot.send_message(chat_id, "Проверка уже выполняется. Нажмите кнопку ещё раз.")
        await send_news_channel_offer(callback.bot, chat_id)
        return

    try:
        subscription_url = await activate_news_channel_bonus(tg_id)
    except Exception as exc:
        logger.exception("News-channel bonus activation failed for user %s: %s", tg_id, exc)
        subscription_url = None
    finally:
        await db.release_user_lock(tg_id)

    if not subscription_url:
        await callback.bot.send_message(
            chat_id,
            "Не удалось активировать подарочный день. Попробуйте ещё раз чуть позже.",
        )
        await send_news_channel_offer(callback.bot, chat_id)
        return

    await state.clear()
    support_url = "https://t.me/wayspn_support"
    support_username = "@wayspn_support"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu", style="primary")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=support_url, style="success")],
    ])
    await callback.bot.send_message(
        chat_id,
        (
            "✅ <b>Пробная подписка с антиглушилкой активирована на 1 день!</b>\n\n"
            "🔑 <b>Ваш пробный ключ:</b>\n"
            f"<code>{html.escape(subscription_url)}</code>\n\n"
            "Скачайте приложение <b>Happ Plus</b> или <b>INCY</b> и добавьте туда ключ.\n\n"
            "По любым вопросам пишите в поддержку:\n"
            f"{html.escape(support_username)}"
        ),
        reply_markup=keyboard,
    )

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
            InlineKeyboardButton(
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
    is_partner = await db.is_partner(tg_id)

    keyboard = [
        [InlineKeyboardButton(text="📱 Личный кабинет", web_app=WebAppInfo(url=MINIAPP_URL), style="primary")],
        [InlineKeyboardButton(text="💳 Купить / Продлить подписку", callback_data="buy_subscription", style="success")],
    ]
    if tg_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton(text="🛠 Админ-панель", web_app=WebAppInfo(url=ADMIN_PANEL_URL), style="primary")])
    visible_subscriptions = await db.get_bot_visible_subscriptions(tg_id)
    if visible_subscriptions:
        keyboard.append([InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")])

    active_bypass_subscriptions = await db.get_active_bypass_subscriptions(tg_id)
    if active_bypass_subscriptions:
        keyboard.append([InlineKeyboardButton(text="📦 Купить ГБ", callback_data="buy_gb", style="success")])

    keyboard.extend([
        [InlineKeyboardButton(text="📲 Инструкция", callback_data="how_to_connect", style="primary")],
        [InlineKeyboardButton(text="📢 Новостной канал", url=f"https://t.me/{NEWS_CHANNEL_USERNAME}", style="primary")],
        [InlineKeyboardButton(text="👥 Бонус за друга", callback_data="referral", style="primary")],
    ])

    # Добавляем кнопку партнёрства если пользователь партнёр
    if is_partner:
        keyboard.append([InlineKeyboardButton(text="🤝 Партнёрство", callback_data="partnership", style="primary")])

    keyboard.append([InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL, style="primary")])

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
        [InlineKeyboardButton(text="🤖 Android", callback_data="instruction_connect_android", style="primary")],
        [InlineKeyboardButton(text="🍎 iPhone", callback_data="instruction_connect_iphone", style="primary")],
        [InlineKeyboardButton(text="🛒 Как купить подписку", callback_data="instruction_buy", style="primary")],
        [InlineKeyboardButton(text="💳 Купить / Продлить подписку", callback_data="buy_subscription", style="success")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu", style="danger")]
    ])

    text = (
        "📲 <b>Инструкция</b>\n\n"
        "Выбери устройство, на котором хочешь подключить VPN.\n"
        "Бот покажет подходящее приложение и короткую инструкцию."
    )

    await edit_text_with_photo(callback, text, kb, "Как подключиться")
    await state.clear()


@router.callback_query(F.data == "instruction_buy")
async def process_instruction_buy(callback: CallbackQuery, state: FSMContext):
    """Показать инструкцию покупки подписки."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened buy instruction")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить / Продлить подписку", callback_data="buy_subscription", style="success")],
        [InlineKeyboardButton(text="📲 Как подключить VPN", callback_data="instruction_connect", style="primary")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu", style="danger")]
    ])

    text = (
        "🛒 <b>Как купить подписку</b>\n\n"
        "1. Открой раздел <b>💳 Купить / Продлить подписку</b>\n"
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
    """Предложить выбрать устройство для инструкции подключения."""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} opened connection platform selection")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Android", callback_data="instruction_connect_android", style="primary")],
        [InlineKeyboardButton(text="🍎 iPhone", callback_data="instruction_connect_iphone", style="primary")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="how_to_connect", style="danger")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu", style="danger")]
    ])

    text = (
        "📲 <b>Как подключить VPN</b>\n\n"
        "Выбери своё устройство — бот покажет подходящее приложение и короткую инструкцию."
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
        [InlineKeyboardButton(
            text=connection_app_button_text(platform),
            url=connection_app_url(platform),
            style="success",
        )],
        [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")],
        [InlineKeyboardButton(text="📱 Выбрать другое устройство", callback_data="instruction_connect", style="primary")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu", style="danger")],
    ])

    await edit_text_with_photo(
        callback,
        build_connection_instruction(platform, support_url=SUPPORT_URL),
        kb,
        "Как подключиться",
    )
    await state.clear()
