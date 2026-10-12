"""Shared referral copy, current price illustrations and input validation."""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
import re

MIN_WITHDRAWAL = Decimal('1500')
MIN_WITHDRAWAL_TEXT = '1 500 ₽'
SHARE_TEXT = 'Лучший VPN:'
# Compatibility copy for integrations importing RULES; bot screens use build_rules
# with prices from the same active discounts as the subscription checkout.
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
ENTRY_INTENTS = frozenset({'earn', 'earn_spend', 'earn_withdraw'})


def pricing_examples(discounts=None) -> list[dict]:
    """Monthly illustrations, never an alternate pricing/commission policy."""
    from config import TARIFFS
    from services.discounts import calculate_discounted_price
    from services.referral_accounting import calculate_share

    examples = []
    for code, tariff in TARIFFS.items():
        if tariff.get('days') != 30:
            continue
        price = calculate_discounted_price(
            tariff['price'], discounts or [], product_type='subscription',
            code=code, plan_kind=tariff.get('kind'),
        )
        examples.append({
            'tariff_code': code,
            'title': tariff['title'],
            'price': price['price'],
            'original_price': price['original_price'],
            'first_commission': float(calculate_share(price['price'], first=True)),
            'repeat_commission': float(calculate_share(price['price'], first=False)),
        })
    return examples


async def current_pricing_examples() -> list[dict]:
    import database as db
    return pricing_examples(await db.get_active_discounts())


def _rub(value) -> str:
    amount = Decimal(str(value)).quantize(Decimal('0.01'))
    return f'{amount:f}'.rstrip('0').rstrip('.') if amount % 1 else str(int(amount))


def build_rules(examples=()) -> str:
    """Short Telegram-caption rules with actual currently available prices."""
    lines = [
        '1. Отправьте другу свою ссылку.',
        '2. Друг впервые регистрируется в боте по ней и покупает подписку.',
        '3. Получайте деньги с его покупок и продлений.\n',
        '<blockquote><b>35%</b> — с первой покупки друга\n'
        '<b>15%</b> — с его следующих покупок</blockquote>\n',
    ]
    if examples:
        lines.append('<b>Примеры с текущих месячных цен</b>')
        for example in examples:
            lines.append(
                f'Оплата {_rub(example["price"])} ₽ → '
                f'<b>{_rub(example["first_commission"])} ₽</b> вам; '
                f'следующая — <b>{_rub(example["repeat_commission"])} ₽</b>.'
            )
        lines.append('')
    lines.extend([
        'Процент — от фактически оплаченной суммы, в том числе со скидкой.\n',
        'Покупки за реферальный баланс не участвуют.\n',
        '• Оплатить свою подписку — достаточно её стоимости.',
        f'• Вывести через СБП или USDT — от <b>{MIN_WITHDRAWAL_TEXT}</b>.',
        'Выплату отправим после проверки заявки.\n',
        'Учитывается только первая регистрация по ссылке. '
        'Уже зарегистрированного пользователя перепривязать нельзя. '
        'Без покупки и за самоприглашение начислений нет.',
    ])
    return '\n'.join(lines)


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
