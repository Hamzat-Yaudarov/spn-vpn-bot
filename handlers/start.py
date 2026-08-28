import html
import logging
from aiogram import Router, Bot
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import ADMIN_ID, ADMIN_PANEL_URL, MINIAPP_URL, TELEGRAPH_AGREEMENT_URL, SUPPORT_URL, NEWS_CHANNEL_USERNAME
from states import UserStates
import database as db
from services.image_handler import send_text_with_photo
from services.notification_delivery import clear_telegram_delivery_blocked
from services.mobile_auth import claim_challenge
from services.custom_emoji import custom_emoji_button


logger = logging.getLogger(__name__)

router = Router()


def mobile_auth_keyboard(challenge_id: str, device_name: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    device_label = html.escape((device_name or "Android-устройство").strip()[:80])
    text = (
        "<b>Вход в Way VPN</b>\n\n"
        f"Устройство: <code>{device_label}</code>\n\n"
        "Подтверждайте вход только если вы сами открыли приложение. "
        "Кнопка одноразовая и действует несколько минут."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтвердить вход",
            callback_data=f"mobile_auth_approve:{challenge_id}",
            style="success",
        )],
    ])
    return text, keyboard


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    """Обработчик команды /start"""
    tg_id = message.from_user.id
    username = message.from_user.username
    user_already_exists = await db.user_exists(tg_id)

    # Проверяем наличие реферальной ссылки или партнёрской ссылки
    args = message.text.split()
    start_payload = args[1].strip() if len(args) > 1 else None
    normalized_payload = start_payload.lower() if start_payload else None
    is_mobile_auth = bool(start_payload and start_payload.startswith("app_"))
    referrer_id = None
    partner_id = None
    tracking_code = None

    logger.info(
        "Start command received: user=%s username=%s is_new=%s payload=%s normalized_payload=%s",
        tg_id,
        username or "",
        not user_already_exists,
        "app_[redacted]" if is_mobile_auth else (start_payload or ""),
        "app_[redacted]" if is_mobile_auth else (normalized_payload or ""),
    )

    if len(args) > 1:
        if is_mobile_auth:
            pass
        elif args[1].startswith("ref_"):
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
        else:
            code = normalized_payload
            if await db.record_tracking_link_click(code, tg_id, is_new_user=not user_already_exists):
                if not user_already_exists:
                    tracking_code = code
                    logging.info(f"New user {tg_id} joined via tracking link {code}")
                else:
                    logging.info(f"Existing user {tg_id} clicked tracking link {code}")
            else:
                logger.warning(
                    "Unknown or inactive tracking link payload: user=%s username=%s is_new=%s payload=%s normalized_payload=%s",
                    tg_id,
                    username or "",
                    not user_already_exists,
                    start_payload or "",
                    code or "",
                )

    # Создаём пользователя если его нет
    await db.create_user(
        tg_id,
        username,
        referrer_id,
        tracking_code,
        require_news_channel_onboarding=True,
    )
    await clear_telegram_delivery_blocked(tg_id)
    logger.info(
        "User ensured in database after /start: user=%s username=%s was_new=%s tracking_code=%s referrer_id=%s",
        tg_id,
        username or "",
        not user_already_exists,
        tracking_code or "",
        referrer_id or "",
    )

    if referrer_id is not None and not user_already_exists:
        await db.update_referral_count(referrer_id)

    mobile_challenge = None
    if is_mobile_auth:
        mobile_challenge = await claim_challenge(start_payload[4:], tg_id)
        if not mobile_challenge:
            await message.answer(
                "Ссылка входа в Way VPN недействительна или уже истекла. Создайте новую ссылку в приложении."
            )
            return

    # Проверяем принял ли пользователь условия
    if not await db.has_accepted_terms(tg_id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять условия", callback_data="accept_terms", style="success")],
            [InlineKeyboardButton(text="📄 Открыть условия", url=TELEGRAPH_AGREEMENT_URL, style="primary")]
        ])
        await message.answer(
            "Чтобы пользоваться ботом, примите пользовательское соглашение.",
            reply_markup=kb
        )
        await state.set_state(UserStates.waiting_for_agreement)
    elif await db.needs_news_channel_onboarding(tg_id):
        await send_news_channel_offer(bot, message.chat.id)
    elif mobile_challenge:
        text, keyboard = mobile_auth_keyboard(
            str(mobile_challenge["id"]),
            mobile_challenge.get("device_name"),
        )
        await message.answer(text, reply_markup=keyboard)
    else:
        await show_main_menu(message)


def news_channel_chat() -> str:
    """Telegram-идентификатор канала для getChatMember."""
    return f"@{NEWS_CHANNEL_USERNAME.lstrip('@')}"


def news_channel_url() -> str:
    """Публичная ссылка на новостной канал."""
    return f"https://t.me/{NEWS_CHANNEL_USERNAME.lstrip('@')}"


async def send_news_channel_offer(bot: Bot, chat_id: int, *, retry: bool = False):
    """Отправить обязательный welcome-экран подписки на канал."""
    prefix = "Подписка пока не найдена.\n\n" if retry else ""
    text = (
        f"{prefix}📢 <b>Подпишитесь на наш новостной канал</b>\n\n"
        "Так вы не пропустите новости и важные изменения Way SPN.\n\n"
        "После подписки нажмите <b>«Я подписался»</b>."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Открыть канал", url=news_channel_url(), style="primary")],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_news_channel", style="success")],
    ])
    await bot.send_message(chat_id, text, reply_markup=keyboard)


def build_main_menu(*, welcome: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Единый короткий главный экран для /start и callback-возврата."""
    support_url = SUPPORT_URL or "https://t.me/wayspn_support"
    text = (
        "✅ <b>Всё готово!</b>\n\n"
        "Чтобы начать, нажмите <b>«Купить подписку»</b>.\n"
        "После оплаты бот выдаст ключ и покажет, как подключиться."
        if welcome else
        "🏠 <b>Way SPN</b>\n\nВыберите нужное действие."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [custom_emoji_button("Купить подписку", emoji_key="buy", fallback_emoji="🛒", callback_data="buy_subscription", style="success")],
        [custom_emoji_button("Мои подписки", emoji_key="subscriptions", fallback_emoji="🔑", callback_data="my_subscriptions", style="primary")],
        [custom_emoji_button("Как подключить", emoji_key="connect", fallback_emoji="📲", callback_data="how_to_connect", style="primary")],
        [custom_emoji_button("Помощь", emoji_key="support", fallback_emoji="🆘", url=support_url, style="primary")],
        [custom_emoji_button("Ещё", emoji_key="more", fallback_emoji="⋯", callback_data="more_menu", style="primary")],
    ])
    return text, keyboard


async def build_more_menu(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Второстепенные функции, не перегружающие главный экран."""
    keyboard = [
        [custom_emoji_button("Личный кабинет", emoji_key="device", fallback_emoji="📱", web_app=WebAppInfo(url=MINIAPP_URL), style="primary")],
        [custom_emoji_button("Новости", emoji_key="news", fallback_emoji="📢", url=news_channel_url(), style="primary")],
        [custom_emoji_button("Пригласить друга", emoji_key="invite", fallback_emoji="👥", callback_data="referral", style="primary")],
    ]
    if await db.is_partner(tg_id):
        keyboard.append([custom_emoji_button("Партнёрство", emoji_key="invite", fallback_emoji="🤝", callback_data="partnership", style="primary")])
    if tg_id == ADMIN_ID:
        keyboard.append([custom_emoji_button(
            "Админ-панель",
            emoji_key="settings",
            fallback_emoji="🛠",
            web_app=WebAppInfo(url=ADMIN_PANEL_URL),
            style="primary",
        )])
    keyboard.append([custom_emoji_button("Назад", emoji_key="back", fallback_emoji="←", callback_data="back_to_menu", style="primary")])
    return "⋯ <b>Ещё</b>\n\nВыберите нужный раздел.", InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_main_menu(message: Message, user_id: int | None = None, *, welcome: bool = False):
    """Отправить единый главный экран."""
    text, keyboard = build_main_menu(welcome=welcome)
    await send_text_with_photo(message, text, keyboard, "Главное меню")
