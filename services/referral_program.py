"""Shared referral copy and validation; no network or database side effects."""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
import re

MIN_WITHDRAWAL = Decimal('1500')
MIN_WITHDRAWAL_TEXT = '1 500 ₽'
SHARE_TEXT = (
    'Попробуй Way SPN — VPN с подключением через Telegram.\n'
    'Если купишь подписку по моей ссылке, я получу бонус.'
)
RULES = (
    '1. Отправьте другу свою ссылку.\n'
    '2. Друг впервые открывает бота по ней и покупает подписку.\n'
    '3. Деньги автоматически приходят на ваш баланс.\n\n'
    '<blockquote><b>35%</b> — с первой покупки друга\n'
    '<b>15%</b> — с его следующих покупок</blockquote>\n\n'
    '<b>Пример</b>\n'
    'Друг оплатил 300 ₽ — вам <b>105 ₽</b>.\n'
    'Снова оплатил 300 ₽ — вам <b>45 ₽</b>.\n'
    'Если друг платит со скидкой, процент считаем от этой суммы.\n\n'
    '<b>Как потратить деньги</b>\n'
    '• Оплатить свою подписку — достаточно её стоимости.\n'
    '• Вывести на карту (СБП) или в USDT — от <b>1 500 ₽</b>.\n\n'
    'Выплату отправим после проверки заявки.'
)
STATUSES = {'pending': 'Проверяем заявку', 'completed': 'Выплачено', 'rejected': 'Отклонено — деньги снова на балансе'}


def referral_link(username: str, user_id: int) -> str:
    return f'https://t.me/{username.lstrip("@")}?' + urlencode({'start': f'ref_{user_id}'})


def share_link(link: str) -> str:
    return 'https://t.me/share/url?' + urlencode({'url': link, 'text': SHARE_TEXT})


def parse_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value or '').strip().replace(' ', '').replace('\u00a0', '').replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError('Введите сумму числом, например 1500 или 1500,50.') from exc
    if not amount.is_finite() or amount < MIN_WITHDRAWAL or amount > Decimal('1000000000'):
        raise ValueError(f'Минимальная сумма вывода — {MIN_WITHDRAWAL_TEXT}.')
    if amount != amount.quantize(Decimal('0.01')):
        raise ValueError('Укажите не больше двух знаков после запятой.')
    return amount


def validate_details(method: str, bank: str = '', phone: str = '', address: str = '') -> tuple[str, str, str]:
    bank, address = bank.strip(), address.strip()
    phone = re.sub(r'[\s()\-]', '', phone)
    if method == 'sbp':
        if not 2 <= len(bank) <= 120:
            raise ValueError('Укажите название банка (от 2 до 120 символов).')
        if not re.fullmatch(r'\+[1-9]\d{9,14}', phone):
            raise ValueError('Введите телефон с кодом страны, например +79991234567.')
        return bank, phone, ''
    if method == 'usdt':
        if not (re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', address) or re.fullmatch(r'0x[0-9a-fA-F]{40}', address)):
            raise ValueError('Нужен адрес USDT в сети TRC-20 (начинается с T) или ERC-20 (с 0x).')
        return '', '', address
    raise ValueError('Неизвестный способ вывода.')
