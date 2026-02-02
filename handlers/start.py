import logging
import urllib.parse
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

    logger.info(f"✅ CMD_START HANDLER TRIGGERED for user {tg_id}")
    logger.info(f"Full message text: '{message.text}'")
    logger.info(f"Message.payload: {getattr(message, 'payload', 'N/A')}")

    # Проверяем наличие реферальной или партнёрской ссылки
    args = message.text.split()
    referrer_id = None
    partner_id = None

    logger.info(f"User {tg_id} triggered /start. Full message: '{message.text}', Args: {args}")

    if len(args) > 1:
        param = args[1]
        # Обработка URL-encoded параметров (на случай если Telegram отправляет в кодированном виде)
        param = urllib.parse.unquote(param)
        logger.info(f"Parsed parameter: '{param}' (URL-decoded)")

        if param.startswith("ref_"):
            # Обычная реферальная ссылка
            try:
                referrer_id = int(param.split("_")[1])
                await db.update_referral_count(referrer_id)
                logger.info(f"✅ User {tg_id} joined via referral link from {referrer_id}")
            except (ValueError, IndexError) as e:
                logger.warning(f"❌ Failed to parse referral link: {param}, error: {e}")
                referrer_id = None

        elif param.startswith("partner_"):
            # Партнёрская ссылка
            logger.info(f"🤝 Processing partner link: {param}")
            try:
                partner_id = int(param.split("_")[1])
                logger.info(f"Extracted partner_id: {partner_id}")

                # Регистрируем партнёрскую ссылку
                partner = await db.get_partner_info(partner_id)
                logger.info(f"Partner info lookup result: {partner}")

                if partner:
                    if partner.get('is_partner'):
                        await db.register_partnership_link(partner_id, tg_id)
                        logger.info(f"✅ User {tg_id} joined via partner link from {partner_id}")
                        logger.info(f"✅ Partnership link registered in database")
                    else:
                        logger.warning(f"⚠️ Partner {partner_id} exists but is_partner=False")
                else:
                    logger.warning(f"⚠️ Partner {partner_id} not found in database")
            except (ValueError, IndexError) as e:
                logger.warning(f"❌ Failed to parse partner link: {param}, error: {e}")
                partner_id = None
        else:
            logger.warning(f"⚠️ Unknown parameter format: {param}")
    else:
        logger.info(f"No parameters provided in /start command")

    try:
        # Создаём пользователя если его нет
        await db.create_user(tg_id, username, referrer_id)

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
    except Exception as e:
        logger.error(f"Error in cmd_start for user {tg_id}: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при обработке команды. Попробуй ещё раз.")


@router.message(lambda msg: msg.text and msg.text.startswith('/start'))
async def cmd_start_fallback(message: Message, state: FSMContext, bot: Bot):
    """
    СТРАХОВКА: Обработчик для ловли всех /start команд, которые почему-то не попали в основной обработчик.
    Это помогает отловить edge cases с deep links.
    """
    logger.warning(f"⚠️ FALLBACK /start handler triggered for user {message.from_user.id}")
    logger.warning(f"Message text: '{message.text}'")
    logger.warning(f"Message text length: {len(message.text) if message.text else 0}")

    # Просто перенаправляем в основной обработчик
    await cmd_start(message, state, bot)


async def show_main_menu(message: Message):
    """Показать главное меню"""
    tg_id = message.from_user.id

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

    await send_text_with_photo(message, text, kb, "Главное меню")
