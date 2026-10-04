from html import escape
from aiogram import Router, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
import database as db
from services.custom_emoji import semantic_button
from services import referral_store as store
from services.referral_program import MIN_WITHDRAWAL, MIN_WITHDRAWAL_TEXT, RULES, STATUSES, referral_link, share_link
from handlers.withdrawals import install_withdrawal_handlers
from services.image_handler import edit_message_text, edit_message_with_photo, send_text_with_photo

router = Router()


def earning_screen(stats, link):
    balance = stats['current_balance']
    remaining = max(0, float(MIN_WITHDRAWAL) - balance)
    progress = f'До вывода осталось {remaining:.2f} ₽.' if remaining else 'Уже можно вывести деньги.'
    text = (
        '<b>💰 Зарабатывать</b>\n\n'
        'Приглашайте друзей — получайте деньги.\n\n'
        '<blockquote><b>35%</b> — с первой покупки друга\n'
        '<b>15%</b> — с его следующих покупок</blockquote>\n\n'
        '1. Отправьте другу свою ссылку.\n'
        '2. Друг впервые открывает бота по ней и покупает подписку.\n'
        '3. Вы получаете деньги на баланс.\n\n'
        f'<blockquote>Доступно: <b>{balance:.2f} ₽</b>\n'
        f'Всего заработано: <b>{stats["total_earned"]:.2f} ₽</b>\n'
        f'Друзей с покупками: <b>{stats["active_referrals"]}</b></blockquote>\n'
        f'{progress}\n\n'
        f'Вывод на карту (СБП) или в USDT — от <b>{MIN_WITHDRAWAL_TEXT}</b>.\n'
        'Или оплатите свою подписку с баланса.\n\n'
        f'<b>Ваша ссылка</b>\n<code>{escape(link)}</code>'
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        # These actions should not inherit the shared "invite" premium icon.
        [InlineKeyboardButton(text='📨 Пригласить друга', url=share_link(link), style='success')],
        [InlineKeyboardButton(text='🔗 Скопировать ссылку', copy_text=CopyTextButton(text=link), style='primary')],
        [semantic_button(text='🛒 Оплатить подписку', emoji_key='buy', callback_data='referral_spend', style='primary')],
        [semantic_button(text='🏦 Вывести деньги', callback_data='referral_withdraw', style='primary')],
        [InlineKeyboardButton(text='📋 История', callback_data='referral_history:0', style='primary'),
         InlineKeyboardButton(text='Как это работает', callback_data='referral_rules', style='primary')],
        [semantic_button(text='← Назад', callback_data='back_to_menu', style='primary')],
    ])
    return text, keyboard


async def send_earning_screen(message, user_id, bot, *, edit=False):
    stats = await db.get_referral_stats(user_id)
    link = referral_link((await bot.get_me()).username, user_id)
    text, keyboard = earning_screen(stats, link)
    if edit:
        await edit_message_with_photo(message, text, keyboard, 'Зарабатывать')
    else:
        await send_text_with_photo(message, text, keyboard, 'Зарабатывать')


@router.callback_query(F.data == 'referral')
async def process_referral(callback, state):
    await state.clear()
    await callback.answer()
    await send_earning_screen(callback.message, callback.from_user.id, callback.bot, edit=True)


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[semantic_button(text='← Зарабатывать', callback_data='referral', style='primary')]])


@router.callback_query(F.data == 'referral_rules')
async def rules(callback, state):
    await state.clear()
    await callback.answer()
    await edit_message_text(callback.message, '<b>Как работает заработок</b>\n\n' + RULES, reply_markup=back_keyboard())


@router.callback_query(F.data == 'referral_spend')
async def spend(callback, state):
    await state.clear()
    await callback.answer()
    await edit_message_text(callback.message, '<b>🛒 Подписка за заработанные деньги</b>\n\n'
        '1. Выберите подписку.\n'
        '2. В оплате выберите реферальный баланс.\n\n'
        '<blockquote>Нужно накопить только стоимость подписки.\n'
        'Ждать 1 500 ₽ не нужно.</blockquote>',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [semantic_button(text='Выбрать подписку', callback_data='buy_subscription', style='success')],
            [semantic_button(text='← Назад', callback_data='referral', style='primary')]]))


@router.callback_query(F.data == 'referral_withdraw')
async def withdraw_start(callback, state):
    await state.clear()
    stats = await db.get_referral_stats(callback.from_user.id)
    if stats['current_balance'] < MIN_WITHDRAWAL:
        await callback.answer(f'Вывод от {MIN_WITHDRAWAL_TEXT}. Доступно {stats["current_balance"]:.2f} ₽.', show_alert=True)
        return
    await callback.answer()
    await edit_message_text(callback.message, '<b>🏦 Вывести деньги</b>\n\n'
        f'<blockquote>Доступно: <b>{stats["current_balance"]:.2f} ₽</b>\n'
        f'Вывод — от <b>{MIN_WITHDRAWAL_TEXT}</b></blockquote>\n\n'
        'Выберите, куда отправить деньги.\nВыплату отправим после проверки заявки.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [semantic_button(text='🏦 СБП', callback_data='referral_withdraw_sbp', style='success')],
            [semantic_button(text='💎 USDT', callback_data='referral_withdraw_usdt', style='success')],
            [semantic_button(text='← Назад', callback_data='referral', style='primary')]]))


@router.callback_query(F.data.startswith('referral_history:') | F.data.startswith('partner_history:'))
async def show_history(callback, state):
    await state.clear()
    program = 'partner' if callback.data.startswith('partner_') else 'referral'
    try:
        offset = min(100000, max(0, int(callback.data.split(':')[1])))
    except ValueError:
        await callback.answer('Откройте историю заново.')
        return
    rows = await store.history(callback.from_user.id, program, offset=offset, limit=11)
    lines = ['<b>📋 История</b>']
    for row in rows[:10]:
        when = row['created_at'].strftime('%d.%m.%Y')
        if row['kind'] == 'earning':
            label, sign = 'За покупку друга', '+'
        elif row['detail'].startswith('subscription_'):
            label, sign = 'Оплата своей подписки', '−'
        else:
            label = f'№{"R" if program == "referral" else "P"}-{row["id"]} · {STATUSES.get(row["status"], row["status"])}'
            sign = ''
        lines.append(f'<blockquote><b>{sign}{row["amount"]:.2f} ₽</b> · {when}\n{escape(label)}</blockquote>')
    if not rows:
        lines.append('Здесь будут ваши начисления и выплаты.')
    buttons = []
    if offset:
        buttons.append(InlineKeyboardButton(text='Новее', callback_data=f'{program}_history:{max(0,offset-10)}', style='primary'))
    if len(rows) > 10:
        buttons.append(InlineKeyboardButton(text='Ранее', callback_data=f'{program}_history:{offset+10}', style='primary'))
    keyboard = ([buttons] if buttons else []) + [[semantic_button(text='← Назад', callback_data='partnership' if program == 'partner' else 'referral', style='primary')]]
    await callback.answer()
    await edit_message_text(callback.message, '\n\n'.join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


install_withdrawal_handlers(router, 'referral')
