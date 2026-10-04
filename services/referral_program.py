"""Shared referral copy and validation; no network or database side effects."""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
import re

MIN_WITHDRAWAL = Decimal('1500')
MIN_WITHDRAWAL_TEXT = '1 500 ₽'
SHARE_TEXT = (
    'Я пользуюсь Way SPN. Подключить можно через этого бота.'
)
RULES = (
    '<blockquote>'
    '1. Отправьте другу свою ссылку\n'
    '2. Друг покупает подписку\n'
    '3. Деньги приходят на ваш баланс'
    '</blockquote>\n\n'
    '<b>Ваш доход</b>\n'
    '35% с первой покупки\n'
    '15% со следующих покупок\n\n'
    'Баланс можно потратить на подписку. Вывод — от 1 500 ₽ через СБП или USDT.'
)
STATUSES = {'pending': 'На рассмотрении', 'completed': 'Выплачено', 'rejected': 'Отклонено — сумма возвращена на баланс'}


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
