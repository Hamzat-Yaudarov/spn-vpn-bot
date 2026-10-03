import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import PARTNERSHIP_AGREEMENTS
from handlers.withdrawals import install_withdrawal_handlers
from services.referral_program import MIN_WITHDRAWAL, MIN_WITHDRAWAL_TEXT, share_link
from states import UserStates
import database as db
from services.image_handler import edit_text_with_photo, send_text_with_photo
from services.custom_emoji import semantic_button


logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == "partnership")
async def process_partnership_button(callback: CallbackQuery, state: FSMContext):
    """Обработчик нажатия на кнопку 'Партнёрство'"""
    await state.clear()
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} clicked: partnership")

    partnership = await db.get_partnership(tg_id)
    if not partnership:
        await callback.answer("❌ Партнёрство не активировано", show_alert=True)
        return

    # Проверяем принял ли партнёр соглашение
    if not partnership['agreement_accepted']:
        # Показываем соглашение
        await show_partnership_agreement(callback, state, tg_id, partnership['percentage'])
    else:
        # Показываем личный кабинет
        await show_partnership_cabinet(callback, tg_id)


async def show_partnership_agreement(callback: CallbackQuery, state: FSMContext, tg_id: int, percentage: int):
    """Показать соглашение партнёрства"""
    agreement_url = PARTNERSHIP_AGREEMENTS.get(percentage)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [semantic_button(text="📄 Открыть соглашение", url=agreement_url, style="primary")],
        [semantic_button(text="✅ Принять соглашение", callback_data="accept_partnership_agreement", style="success")],
        [semantic_button(text="← Назад", callback_data="back_to_menu", style="primary")]
    ])

    text = (
        "<b>Партнёрское соглашение</b>\n\n"
        "⚠️ <b>Внимание!</b> Перед началом работы необходимо ознакомиться и принять соглашение:\n\n"
    )

    if percentage == 15:
        text += "📋 <b>Соглашение для партнёров с 15% доходом</b>\n"
        text += "от всех транзакций приведённых ими пользователей\n\n"
    elif percentage == 20:
        text += "📋 <b>Соглашение для партнёров с 20% доходом</b>\n"
        text += "от всех транзакций приведённых ими пользователей\n\n"
    elif percentage == 25:
        text += "📋 <b>Соглашение для партнёров с 25% доходом</b>\n"
        text += "от всех транзакций приведённых ими пользователей\n\n"
    elif percentage == 30:
        text += "📋 <b>Соглашение для партнёров с 30% доходом</b>\n"
        text += "от всех транзакций приведённых ими пользователей\n\n"

    text += "Нажмите кнопку выше, чтобы прочитать соглашение."

    await send_text_with_photo(callback.message, text, kb, "Партнёрское соглашение")
    await state.set_state(UserStates.partnership_viewing_agreement)


@router.callback_query(F.data == "accept_partnership_agreement")
async def process_accept_partnership_agreement(callback: CallbackQuery, state: FSMContext):
    """Обработчик принятия соглашения партнёрства"""
    tg_id = callback.from_user.id
    logging.info(f"User {tg_id} accepted partnership agreement")

    # Отмечаем что соглашение принято
    await db.accept_partnership_agreement(tg_id)

    # Показываем личный кабинет
    await show_partnership_cabinet(callback, tg_id)


async def show_partnership_cabinet(callback: CallbackQuery, tg_id: int):
    """Показать личный кабинет партнёра"""
    partnership = await db.get_partnership(tg_id)
    if not partnership:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    # Получаем статистику
    stats = await db.get_partner_stats(tg_id)

    # Получаем партнёрскую ссылку
    bot_username = (await callback.bot.get_me()).username
    partner_link = f"https://t.me/{bot_username}?start=partner_{tg_id}"

    # Подсчитываем покупки по тарифам
    tariff_counts = {
        '1m': 0,
        '3m': 0,
        '6m': 0,
        '12m': 0
    }

    if stats['earnings_by_tariff']:
        for earning in stats['earnings_by_tariff']:
            tariff_code = earning['tariff_code']
            count = earning['purchase_count']
            if tariff_code in tariff_counts:
                tariff_counts[tariff_code] = count

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [semantic_button(text="📨 Пригласить", url=share_link(partner_link), style="primary")],
        [semantic_button(text="🏦 На карту (СБП)", callback_data="partnership_withdraw_sbp", style="success")],
        [semantic_button(text="💎 В USDT", callback_data="partnership_withdraw_usdt", style="success")],
        [semantic_button(text="← Назад", callback_data="back_to_menu", style="primary")]
    ])

    text = (
        "<b>👤 Личный кабинет партнёра</b>\n\n"
        "<b>Ваша партнёрская ссылка:</b>\n"
        f"<code>{partner_link}</code>\n\n"
        f"<b>Процент дохода:</b> <b>{stats['percentage']}%</b>\n"
        f"<b>Всего пользователей по ссылке:</b> <b>{stats['total_referrals']}</b>\n\n"
        "<b>📊 Покупки по тарифам:</b>\n"
        f"• 1 месяц: <b>{tariff_counts['1m']}</b>\n"
        f"• 3 месяца: <b>{tariff_counts['3m']}</b>\n"
        f"• 6 месяцев: <b>{tariff_counts['6m']}</b>\n"
        f"• 12 месяцев: <b>{tariff_counts['12m']}</b>\n\n"
        f"<b>💰 Всего заработано:</b> <b>{stats['total_earned']:.2f} ₽</b>\n"
        f"<b>💸 Выведено / зарезервировано:</b> <b>{stats['total_withdrawn']:.2f} ₽</b>\n"
        f"<b>🪙 Текущий баланс:</b> <b>{stats['current_balance']:.2f} ₽</b>\n\n"
        f"<i>Минимальная сумма вывода: {MIN_WITHDRAWAL_TEXT}</i>"
    )

    # Если баланс меньше минимума, отключаем кнопки вывода
    if stats['current_balance'] < MIN_WITHDRAWAL:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [semantic_button(text="📨 Пригласить", url=share_link(partner_link), style="primary")],
            [semantic_button(text="🏦 На карту (СБП)", callback_data="partnership_withdraw_sbp", style="success")],
            [semantic_button(text="💎 В USDT", callback_data="partnership_withdraw_usdt", style="success")],
            [semantic_button(text="← Назад", callback_data="back_to_menu", style="primary")]
        ])
        text += f"\n\n⚠️ <i>Баланс меньше минимальной суммы вывода ({MIN_WITHDRAWAL_TEXT})</i>"

    kb.inline_keyboard.insert(-1, [semantic_button(text='📋 История', callback_data='partner_history:0', style='primary')])
    await send_text_with_photo(callback.message, text, kb, "Личный кабинет партнёра")



install_withdrawal_handlers(router, 'partner')
