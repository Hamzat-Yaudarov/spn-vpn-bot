"""Native referral actions reuse the durable bot ledger; never transfer money."""
from decimal import Decimal
import logging
from uuid import UUID

import database as db
from config import TARIFFS
from services import referral_accounting as accounting, referral_store as store
from services.discounts import current_price
from services.subscription_checkout import SpecialDiscountCheckoutError, special_discount_fields, subscription_checkout
from services.payment_reconciliation import reconcile_payment
from services.payment_summary import build_payment_success_summary
from services.referral_program import parse_amount, validate_details

logger = logging.getLogger(__name__)


class ActionError(ValueError):
    def __init__(self, status, code, message, **fields):
        super().__init__(message)
        self.status = status
        self.detail = {'code': code, 'message': message, **fields}


def request_key(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError('Некорректный номер запроса.')
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError('Некорректный номер запроса.') from exc


def amount_text(value):
    return f'{Decimal(str(value)):.2f}'


def _body(value, allowed):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('Некорректные данные запроса.')
    return value


def purchase_details(body):
    body = _body(body, {'request_key', 'tariff_code', 'payment_target', 'subscription_id', 'expected_amount'})
    key = request_key(body.get('request_key'))
    tariff = body.get('tariff_code')
    mode = body.get('payment_target')
    sid = body.get('subscription_id')
    expected = body.get('expected_amount')
    if not isinstance(tariff, str) or not 1 <= len(tariff) <= 80 or mode not in ('new', 'renew'):
        raise ValueError('Выберите тариф и способ покупки.')
    if sid is not None and (type(sid) is not int or not 0 < sid < 2**63):
        raise ValueError('Некорректная подписка.')
    if (mode == 'new' and sid is not None) or (mode == 'renew' and sid is None):
        raise ValueError('Выберите подписку для продления или новую покупку.')
    if not isinstance(expected, str) or len(expected) > 40:
        raise ValueError('Укажите стоимость строкой.')
    return key, tariff, mode, sid, accounting.money(expected)


def withdrawal_details(body):
    body = _body(body, {'request_key', 'amount', 'method', 'bank', 'phone', 'address'})
    key = request_key(body.get('request_key'))
    amount = body.get('amount')
    method = body.get('method')
    if not isinstance(amount, str) or len(amount) > 40:
        raise ValueError('Укажите сумму строкой.')
    if not isinstance(method, str) or method not in ('sbp', 'usdt'):
        raise ValueError('Выберите СБП или USDT.')
    details = [body.get(field, '') for field in ('bank', 'phone', 'address')]
    if any(not isinstance(value, str) or len(value) > 200 for value in details):
        raise ValueError('Некорректные реквизиты.')
    bank, phone, address = validate_details(method, *details)
    return key, parse_amount(amount), method, bank, phone, address


def _mask(value, *, phone=False):
    if not value:
        return None
    if phone:
        return f'+*** *** ** {value[-2:]}'
    return f'{value[:4]}…{value[-4:]}'


def withdrawal_response(key, row):
    return {
        'request_key': key, 'id': row['id'], 'number': f'R-{row["id"]}',
        'status': row['status'], 'amount': amount_text(row['amount']),
        'method': row['withdrawal_type'], 'bank': row.get('bank_name') or None,
        'phone': _mask(row.get('phone_number'), phone=True),
        'address': _mask(row.get('usdt_address')), 'decision_note': row.get('decision_note'),
    }


async def create_withdrawal(user_id, body):
    key, amount, method, bank, phone, address = withdrawal_details(body)
    row = await store.create_request('referral', user_id, amount, method, key,
                                     bank=bank, phone=phone, address=address)
    return withdrawal_response(key, row)


async def get_withdrawal(user_id, key):
    key = request_key(key)
    row = await store.get_request(user_id, key)
    if not row:
        raise ActionError(404, 'not_found', 'Заявка не найдена.')
    return withdrawal_response(key, row)


def _same_purchase(order, tariff, mode, sid, expected):
    if (order['tariff_code'] != tariff or order['purchase_mode'] != mode
            or order['subscription_id'] != sid or Decimal(str(order['amount'])) != expected):
        raise accounting.RequestConflict('Номер запроса уже использован для другой оплаты.')


async def create_purchase(user_id, body, bot):
    key, tariff_code, mode, sid, expected = purchase_details(body)
    order = await accounting.get_balance_purchase(user_id, key)
    if order:
        # Discounts/visibility can change after confirmation. The durable
        # intent and its saved amount, not the current catalogue, win retries.
        _same_purchase(order, tariff_code, mode, sid, expected)
    else:
        tariff = TARIFFS.get(tariff_code)
        if not tariff:
            raise ValueError('Неизвестный тариф.')
        if bot is None:
            raise ActionError(503, 'temporarily_unavailable', 'Оплата пока недоступна. Попробуйте позже.')
        price = await current_price(tariff['price'], product_type='subscription', code=tariff_code,
                                    plan_kind=tariff['kind'], tg_id=user_id, payment_target=mode)
        try:
            async with subscription_checkout(user_id, tariff_code, mode, sid, accounting.WALLET_PROVIDER, price):
                amount = accounting.money(price['price'])
                if amount != expected:
                    # A simultaneous retry may have committed the original quote.
                    order = await accounting.get_balance_purchase(user_id, key)
                    if not order:
                        raise ActionError(409, 'price_changed', 'Стоимость изменилась. Проверьте новую сумму перед оплатой.',
                                          amount=amount_text(amount))
                    _same_purchase(order, tariff_code, mode, sid, expected)
                else:
                    order = await accounting.reserve_balance_purchase(user_id, key, tariff_code, amount,
                        purchase_mode=mode, subscription_id=sid, validate_selection=True, **special_discount_fields(price))
        except SpecialDiscountCheckoutError as exc:
            order = await accounting.get_balance_purchase(user_id, key)
            if not order:
                raise ActionError(409, 'discount_checkout', str(exc)) from exc
            _same_purchase(order, tariff_code, mode, sid, expected)
    return await _purchase_response(user_id, key, order, bot)


async def get_purchase(user_id, key, bot):
    key = request_key(key)
    order = await accounting.get_balance_purchase(user_id, key)
    if not order:
        raise ActionError(404, 'not_found', 'Оплата не найдена.')
    return await _purchase_response(user_id, key, order, bot)


async def _purchase_response(user_id, key, order, bot):
    payment = await db.get_payment_by_invoice(order['invoice_id'])
    if (not payment or payment['tg_id'] != user_id or payment.get('provider') != accounting.WALLET_PROVIDER
            or Decimal(str(payment['amount'])) != Decimal(str(order['amount']))):
        raise ActionError(409, 'request_conflict', 'Не удалось подтвердить сохранённую оплату. Обратитесь в поддержку.')
    if order['status'] != 'cancelled' and payment['status'] == 'pending' and bot is not None:
        try:
            await reconcile_payment(bot, order['invoice_id'], expected_tg_id=user_id)
        except Exception as exc:
            # A saved reservation remains recoverable. A remote timeout is not
            # authorization to cancel, create another order, or debit twice.
            logger.warning('Native wallet activation deferred: %s', type(exc).__name__)
        payment = await db.get_payment_by_invoice(order['invoice_id']) or payment
    status = 'paid' if payment['status'] == 'paid' else (
        'cancelled' if order['status'] == 'cancelled' or payment['status'] in ('cancelled', 'canceled') else 'pending')
    summary = await build_payment_success_summary(payment) if status == 'paid' else None
    return {'request_key': key, 'invoice_id': order['invoice_id'], 'status': status,
            'amount': amount_text(order['amount']), 'summary': summary}
