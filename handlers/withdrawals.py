"""Shared, retry-safe SBP / USDT forms for both earning programs."""
from decimal import Decimal
from html import escape
import logging
from uuid import uuid4
from aiogram import F
from aiogram.types import InlineKeyboardMarkup
import database as db
from services import referral_store as store
from services.referral_program import MIN_WITHDRAWAL, MIN_WITHDRAWAL_TEXT, parse_amount, validate_details
from services.custom_emoji import semantic_button
from states import UserStates

logger = logging.getLogger(__name__)


def install_withdrawal_handlers(router, program):
    prefix = 'referral' if program == 'referral' else 'partnership'
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        semantic_button(text='Отмена', callback_data=prefix, style='primary')]])
    states = {name: getattr(UserStates, f'{prefix}_waiting_{name}') for name in
              ('sbp_amount', 'sbp_bank', 'sbp_phone', 'usdt_amount', 'usdt_address')}

    async def stats(user_id):
        return await (db.get_referral_stats(user_id) if program == 'referral' else db.get_partner_stats(user_id))

    async def start(callback, state):
        current = await stats(callback.from_user.id)
        if not current or current['current_balance'] < MIN_WITHDRAWAL:
            await callback.answer(f'Вывод от {MIN_WITHDRAWAL_TEXT}. Баланс пока меньше этой суммы.', show_alert=True)
            return
        method = 'sbp' if callback.data.endswith('_sbp') else 'usdt'
        await state.clear()
        await state.update_data(withdrawal_request_key=str(uuid4()), withdrawal_method=method)
        await state.set_state(states[f'{method}_amount'])
        await callback.answer()
        await callback.message.answer(
            f'<b>Вывод {"через СБП" if method == "sbp" else "в USDT"}</b>\n'
            f'Доступно: {current["current_balance"]:.2f} ₽. Минимум: {MIN_WITHDRAWAL_TEXT}.\n'
            'Введите сумму в рублях. Заявку проверит администратор; перевод не мгновенный.', reply_markup=keyboard)

    async def amount(message, state):
        try:
            value = parse_amount(message.text)
            current = await stats(message.from_user.id)
            if not current or value > Decimal(str(current['current_balance'])):
                raise ValueError('Сумма больше доступного баланса.')
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=keyboard)
            return
        data = await state.get_data()
        method = data.get('withdrawal_method')
        if method not in ('sbp', 'usdt'):
            await state.clear()
            await message.answer('Оформление устарело. Начните вывод заново.')
            return
        await state.update_data(withdrawal_amount=str(value))
        await state.set_state(states['sbp_bank' if method == 'sbp' else 'usdt_address'])
        await message.answer('Введите название банка.' if method == 'sbp' else
            'Введите адрес USDT: TRC-20 (T…) или ERC-20 (0x…). Сумма заявки указана в рублях; '
            'сеть, курс и итоговую сумму перевода подтвердит администратор.', reply_markup=keyboard)

    async def bank(message, state):
        value = (message.text or '').strip()
        if not 2 <= len(value) <= 120:
            await message.answer('Название банка — от 2 до 120 символов.', reply_markup=keyboard)
            return
        await state.update_data(bank_name=value)
        await state.set_state(states['sbp_phone'])
        await message.answer('Введите телефон для СБП с кодом страны, например +79991234567.', reply_markup=keyboard)

    async def finish(message, state):
        user_id = message.from_user.id
        if not await db.acquire_user_lock(user_id):
            await message.answer('Предыдущая операция ещё выполняется. Повторите через несколько секунд.')
            return
        try:
            data = await state.get_data()
            if not data.get('withdrawal_request_key') or not data.get('withdrawal_amount'):
                await message.answer('Заявка уже отправлена или оформление устарело. Проверьте «Историю» в разделе заработка.')
                return
            method = data['withdrawal_method']
            bank_name, phone, address = validate_details(method, data.get('bank_name', ''),
                (message.text or '') if method == 'sbp' else '', (message.text or '') if method == 'usdt' else '')
            row = await store.create_request(program, user_id, data['withdrawal_amount'], method,
                data['withdrawal_request_key'], bank=bank_name, phone=phone, address=address)
            await state.clear()
            number = f'{"R" if program == "referral" else "P"}-{row["id"]}'
            await message.answer(f'<b>Заявка №{number} принята</b>\nСумма: {row["amount"]:.2f} ₽.\n'
                'Средства зарезервированы. Статус появится в истории; администратор получит уведомление. '
                'Не создавайте повторную заявку на эту сумму.', reply_markup=keyboard)
            logger.info('Withdrawal saved: program=%s id=%s user=%s', program, row['id'], user_id)
        except ValueError as exc:
            await message.answer(escape(str(exc)), reply_markup=keyboard)
        except Exception:
            logger.exception('Withdrawal flow failed for user %s', user_id)
            await message.answer('Не удалось завершить действие. Проверьте историю заявок, затем повторите. '
                'Если заявка уже есть в истории, повторять её не нужно.', reply_markup=keyboard)
        finally:
            await db.release_user_lock(user_id)

    router.callback_query(F.data.in_({f'{prefix}_withdraw_sbp', f'{prefix}_withdraw_usdt'}))(start)
    for name in ('sbp_amount', 'usdt_amount'):
        router.message(states[name])(amount)
    router.message(states['sbp_bank'])(bank)
    for name in ('sbp_phone', 'usdt_address'):
        router.message(states[name])(finish)
