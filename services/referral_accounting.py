"""Invoice-linked referral ledger, manual confirmed returns and wallet holds.

No provider transfers are performed here. Settlement is called inside the same
transaction that changes a successfully activated subscription payment to paid.
Legacy earnings are deliberately never inferred or back-credited.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
from uuid import uuid4

import database as db
from config import ADMIN_ID

EXTERNAL_PROVIDERS = ('cryptobot', 'yookassa')
WALLET_PROVIDER = 'referral_balance'
CENT = Decimal('0.01')
CUTOVER_KEY = 'invoice-linked-referrals-v1'
logger = logging.getLogger(__name__)


class RequestConflict(ValueError):
    """A durable request identity cannot be reused for a different intent."""


class InsufficientBalance(ValueError):
    def __init__(self, available, amount):
        self.available = Decimal(str(available))
        self.amount = Decimal(str(amount))
        super().__init__(f'Недостаточно средств: доступно {self.available:.2f} ₽.')

SCHEMA = '''
ALTER TABLE referral_earnings ADD COLUMN IF NOT EXISTS payment_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS referral_earnings_payment_id
    ON referral_earnings(payment_id) WHERE payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS referral_qualified_paid_history ON payments(tg_id,id)
    WHERE status='paid' AND amount>0 AND COALESCE(payment_kind,'subscription')='subscription'
      AND provider IN ('cryptobot','yookassa');
CREATE TABLE IF NOT EXISTS referral_accounting_migrations (
    migration_key TEXT PRIMARY KEY,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS referral_payment_settlements (
    payment_id BIGINT PRIMARY KEY REFERENCES payments(id),
    invoice_id TEXT NOT NULL UNIQUE,
    payer_id BIGINT NOT NULL,
    referrer_id BIGINT,
    amount NUMERIC NOT NULL,
    qualified BOOLEAN NOT NULL DEFAULT FALSE,
    is_first_purchase BOOLEAN NOT NULL DEFAULT FALSE,
    outcome TEXT NOT NULL,
    earning_id BIGINT REFERENCES referral_earnings(id),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS referral_confirmed_refunds (
    id BIGSERIAL PRIMARY KEY,
    payment_id BIGINT NOT NULL REFERENCES payments(id),
    invoice_id TEXT NOT NULL,
    amount NUMERIC NOT NULL CHECK(amount > 0),
    reference TEXT NOT NULL UNIQUE,
    confirmed_by BIGINT NOT NULL,
    note TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS referral_confirmed_refunds_payment
    ON referral_confirmed_refunds(payment_id);
CREATE TABLE IF NOT EXISTS referral_earning_reversals (
    id BIGSERIAL PRIMARY KEY,
    refund_id BIGINT NOT NULL UNIQUE REFERENCES referral_confirmed_refunds(id),
    earning_id BIGINT NOT NULL REFERENCES referral_earnings(id),
    payment_id BIGINT NOT NULL REFERENCES payments(id),
    referrer_id BIGINT NOT NULL,
    amount NUMERIC NOT NULL CHECK(amount >= 0),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS referral_earning_reversals_owner
    ON referral_earning_reversals(referrer_id);
CREATE TABLE IF NOT EXISTS referral_wallet_reservations (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    invoice_id TEXT NOT NULL UNIQUE REFERENCES payments(invoice_id),
    tariff_code TEXT NOT NULL,
    amount NUMERIC NOT NULL CHECK(amount > 0),
    purchase_mode TEXT NOT NULL,
    subscription_id BIGINT,
    target_slot_number INT,
    status TEXT NOT NULL DEFAULT 'held' CHECK(status IN ('held','consumed','cancelled')),
    withdrawal_id BIGINT UNIQUE REFERENCES referral_withdrawals(id),
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS referral_wallet_reservations_owner
    ON referral_wallet_reservations(user_id) WHERE status='held';
CREATE TABLE IF NOT EXISTS referral_cabinet_views (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    surface TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
'''


def money(value, *, positive=True):
    """Validate an actual currency amount without payout's 1,500 RUB minimum."""
    try:
        amount = Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Укажите корректную сумму.') from exc
    if (not amount.is_finite() or amount > Decimal('1000000000')
            or amount < 0 or (positive and amount == 0)):
        raise ValueError('Сумма должна быть положительной и конечной.')
    if amount != amount.quantize(CENT):
        raise ValueError('Укажите не больше двух знаков после запятой.')
    return amount


def qualified_payment(payment):
    """Only actual positive provider-paid subscriptions determine 35% / 15%."""
    return (payment.get('provider') in EXTERNAL_PROVIDERS
            and (payment.get('payment_kind') or 'subscription') == 'subscription'
            and Decimal(str(payment['amount'])) > 0)


def calculate_share(amount, first=False):
    return (money(amount) * Decimal('0.35' if first else '0.15')).quantize(CENT, rounding=ROUND_HALF_UP)


async def initialize(conn):
    """Additive migration: paid-at-cutover invoices can never be auto replayed."""
    await conn.execute(SCHEMA)
    async with conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))', CUTOVER_KEY)
        inserted = await conn.fetchval('''INSERT INTO referral_accounting_migrations(migration_key)
            VALUES($1) ON CONFLICT(migration_key) DO NOTHING RETURNING migration_key''', CUTOVER_KEY)
        if inserted:
            await conn.execute('''INSERT INTO referral_payment_settlements
                (payment_id,invoice_id,payer_id,amount,outcome)
                SELECT id,invoice_id,tg_id,amount,'legacy_excluded' FROM payments
                WHERE status='paid' ON CONFLICT(payment_id) DO NOTHING''')


async def balance_summary(conn, user_id):
    row = await conn.fetchrow('''SELECT
        (SELECT COALESCE(SUM(referral_share),0) FROM referral_earnings WHERE referrer_id=$1) AS earned,
        (SELECT COALESCE(SUM(amount),0) FROM referral_earning_reversals WHERE referrer_id=$1) AS reversed,
        (SELECT COALESCE(SUM(amount),0) FROM referral_withdrawals
            WHERE referrer_id=$1 AND status IN ('pending','completed')) AS withdrawn,
        (SELECT COALESCE(SUM(amount),0) FROM referral_wallet_reservations
            WHERE user_id=$1 AND status='held') AS held''', user_id)
    result = {key: Decimal(str(value)) for key, value in row.items()}
    result['available'] = result['earned'] - result['reversed'] - result['withdrawn'] - result['held']
    return result


async def balance(conn, user_id):
    return (await balance_summary(conn, user_id))['available']


async def _lock_users(conn, payer_id, referrer_id=None):
    # One total order also prevents deadlocks for reciprocal legacy attribution.
    ids = sorted({int(value) for value in (payer_id, referrer_id) if value is not None})
    rows = await conn.fetch('SELECT * FROM users WHERE tg_id=ANY($1::bigint[]) ORDER BY tg_id FOR UPDATE', ids)
    return {row['tg_id']: row for row in rows}


async def _save_settlement(conn, payment, outcome, *, referrer=None, first=False,
                           qualified=False, earning_id=None):
    return dict(await conn.fetchrow('''INSERT INTO referral_payment_settlements
        (payment_id,invoice_id,payer_id,referrer_id,amount,qualified,is_first_purchase,outcome,earning_id)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *''', payment['id'], payment['invoice_id'],
        payment['tg_id'], referrer, Decimal(str(payment['amount'])), qualified, first, outcome, earning_id))


async def _consume_wallet(conn, payment):
    reservation = await conn.fetchrow('''SELECT * FROM referral_wallet_reservations
        WHERE invoice_id=$1 FOR UPDATE''', payment['invoice_id'])
    if (not reservation or reservation['user_id'] != payment['tg_id']
            or reservation['status'] != 'held'
            or reservation['amount'] != Decimal(str(payment['amount']))
            or reservation['tariff_code'] != payment['tariff_code']):
        raise ValueError('Резерв оплаты с баланса не найден или изменён.')
    # A confirmed return can make an already-held purchase negative. Consume the
    # committed hold after provisioning, never issue an unaccounted/free key.
    withdrawal_id = await conn.fetchval('''INSERT INTO referral_withdrawals
        (referrer_id,amount,withdrawal_type,status) VALUES($1,$2,$3,'completed') RETURNING id''',
        payment['tg_id'], reservation['amount'], f'subscription_{payment["tariff_code"]}')
    await conn.execute('''UPDATE referral_wallet_reservations SET status='consumed',
        withdrawal_id=$2,resolved_at=now() WHERE id=$1''', reservation['id'], withdrawal_id)
    return await _save_settlement(conn, payment, 'internal_balance')


async def settle_subscription(conn, payment):
    """Caller owns transaction and payment FOR UPDATE lock; no remote operations.

    A persisted no-referrer result is final: retrying never attaches a reward to
    a referrer assigned later. Unique payment linkage is a second dedup barrier.
    """
    existing = await conn.fetchrow('SELECT * FROM referral_payment_settlements WHERE payment_id=$1', payment['id'])
    if existing:
        return dict(existing)
    if payment['status'] == 'paid':
        return await _save_settlement(conn, payment, 'legacy_excluded')
    if payment['status'] != 'pending':
        raise ValueError('Платёж не ожидает завершения.')
    payer = await conn.fetchrow('SELECT tg_id,referrer_id FROM users WHERE tg_id=$1', payment['tg_id'])
    if not payer:
        raise ValueError('Плательщик не найден.')
    users = await _lock_users(conn, payment['tg_id'], payer['referrer_id'])
    locked_payer = users[payment['tg_id']]
    if locked_payer['referrer_id'] != payer['referrer_id']:
        # Retry the whole activation if attribution changed while locks waited.
        raise ValueError('Приглашение изменилось. Повторите завершение платежа.')
    if payment.get('provider') == WALLET_PROVIDER:
        return await _consume_wallet(conn, payment)
    if not qualified_payment(payment):
        return await _save_settlement(conn, payment, 'excluded')
    prior = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM payments
        WHERE tg_id=$1 AND id<>$2 AND status='paid' AND amount>0
          AND COALESCE(payment_kind,'subscription')='subscription'
          AND provider IN ('cryptobot','yookassa'))''', payment['tg_id'], payment['id'])
    first = not prior
    await conn.execute('UPDATE users SET first_payment=TRUE WHERE tg_id=$1', payment['tg_id'])
    referrer = locked_payer['referrer_id']
    if referrer is None:
        return await _save_settlement(conn, payment, 'no_referrer', first=first, qualified=True)
    if referrer == payment['tg_id'] or referrer not in users:
        return await _save_settlement(conn, payment, 'invalid_referrer', first=first, qualified=True)
    amount = Decimal(str(payment['amount']))
    share = calculate_share(amount, first=first)
    earning_id = await conn.fetchval('''INSERT INTO referral_earnings
        (referrer_id,referred_user_id,tariff_code,amount,referral_share,is_first_purchase,payment_id)
        VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id''', referrer, payment['tg_id'],
        payment['tariff_code'], amount, share, first, payment['id'])
    from services.referral_store import enqueue
    await enqueue(conn, f'earning:{earning_id}', referrer,
        '<b>🎉 Друг купил подписку!</b>\n\n'
        f'<blockquote>Вам на баланс: <b>+{share:.2f} ₽</b>\n'
        f'{"35% с первой" if first else "15% с повторной"} покупки друга</blockquote>\n\n'
        'Баланс и вывод — в разделе «💰 Заработать».')
    return await _save_settlement(conn, payment, 'credited', referrer=referrer, first=first,
                                  qualified=True, earning_id=earning_id)


async def reserve_balance_purchase(user_id, request_key, tariff_code, amount,
                                   purchase_mode='new', subscription_id=None, target_slot_number=None,
                                   *, validate_selection=False, special_discount_id=None):
    """Reserve once under the same user lock used by cash withdrawal requests."""
    amount = money(amount)
    if not request_key or len(request_key) > 100:
        raise ValueError('Начните оформление оплаты заново.')
    if purchase_mode not in ('new', 'renew') or not tariff_code:
        raise ValueError('Неизвестная покупка.')
    if purchase_mode == 'renew' and not subscription_id:
        raise ValueError('Выберите подписку для продления.')
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchval('SELECT pg_advisory_xact_lock(hashtext($1))', f'referral-wallet:{request_key}')
        if not await conn.fetchrow('SELECT tg_id FROM users WHERE tg_id=$1 FOR UPDATE', user_id):
            raise ValueError('Пользователь не найден.')
        existing = await conn.fetchrow('SELECT * FROM referral_wallet_reservations WHERE request_key=$1', request_key)
        if existing:
            if (existing['user_id'] != user_id or existing['tariff_code'] != tariff_code
                    or existing['amount'] != amount or existing['purchase_mode'] != purchase_mode
                    or existing['subscription_id'] != subscription_id
                    or (not validate_selection and existing['target_slot_number'] != target_slot_number)):
                raise RequestConflict('Номер запроса уже использован для другой оплаты.')
            return dict(existing)
        if validate_selection:
            # Native MiniApp inputs are not trusted bot checkout state. Resolve
            # the target under the funds lock, including not-yet-activated holds.
            from config import TARIFFS
            tariff = TARIFFS.get(tariff_code)
            if not tariff:
                raise ValueError('Неизвестный тариф.')
            kind = tariff['kind']
            if purchase_mode == 'renew':
                subscription = await conn.fetchrow('''SELECT * FROM subscriptions
                    WHERE id=$1 AND tg_id=$2''', subscription_id, user_id)
                if (not subscription or subscription['generation'] != 'v2'
                        or not subscription['is_visible'] or not subscription['is_renewable']
                        or subscription['plan_kind'] != kind
                        or type(subscription['type_index']) is not int
                        or not 1 <= subscription['type_index'] <= db.MAX_SUBSCRIPTIONS_PER_USER):
                    raise ValueError('Подписка недоступна для продления.')
                target_slot_number = subscription['type_index']
            else:
                if subscription_id is not None:
                    raise ValueError('Для новой покупки не выбирайте существующую подписку.')
                rows = await conn.fetch('''SELECT id,type_index FROM subscriptions
                    WHERE tg_id=$1 AND plan_kind=$2 AND generation='v2' AND is_visible''', user_id, kind)
                taken = {row['type_index'] for row in rows if row['type_index'] is not None}
                visible_ids = {row['id'] for row in rows}
                holds = await conn.fetch('''SELECT r.tariff_code,r.target_slot_number,a.subscription_id
                    FROM referral_wallet_reservations r LEFT JOIN payment_subscription_activations a
                    ON a.invoice_id=r.invoice_id
                    WHERE r.user_id=$1 AND r.purchase_mode='new' AND r.status='held' ''', user_id)
                pending = [row for row in holds if (TARIFFS.get(row['tariff_code']) or {}).get('kind') == kind
                           and row['subscription_id'] not in visible_ids]
                taken.update(row['target_slot_number'] for row in pending if row['target_slot_number'] is not None)
                target_slot_number = next((index for index in range(1, db.MAX_SUBSCRIPTIONS_PER_USER + 1)
                                           if index not in taken), None)
                if target_slot_number is None or len(rows) + len(pending) >= db.MAX_SUBSCRIPTIONS_PER_USER:
                    raise ValueError(f'Достигнут лимит: максимум {db.MAX_SUBSCRIPTIONS_PER_USER} подписок этого типа.')
        available = await balance(conn, user_id)
        if available < amount:
            raise InsufficientBalance(available, amount)
        if subscription_id and not await conn.fetchval('''SELECT id FROM subscriptions
            WHERE id=$1 AND tg_id=$2 AND is_visible AND is_renewable''', subscription_id, user_id):
            raise ValueError('Подписка недоступна для оплаты.')
        if special_discount_id is not None:
            await db.validate_special_discount_checkout(conn, user_id, special_discount_id, tariff_code, purchase_mode, amount)
        invoice_id = f'referral-balance-{uuid4().hex}'
        await conn.execute('''INSERT INTO payments(tg_id,tariff_code,amount,provider,invoice_id,
            payment_kind,payment_target,subscription_id,target_slot_number,special_discount_id)
            VALUES($1,$2,$3,$4,$5,'subscription',$6,$7,$8,$9)''', user_id, tariff_code, amount,
            WALLET_PROVIDER, invoice_id, purchase_mode, subscription_id, target_slot_number, special_discount_id)
        return dict(await conn.fetchrow('''INSERT INTO referral_wallet_reservations
            (user_id,request_key,invoice_id,tariff_code,amount,purchase_mode,subscription_id,target_slot_number)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *''', user_id, request_key, invoice_id,
            tariff_code, amount, purchase_mode, subscription_id, target_slot_number))


async def pending_balance_purchases(limit=30, *, after_id=0):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        return [dict(row) for row in await conn.fetch('''SELECT p.* FROM payments p
            JOIN referral_wallet_reservations r ON r.invoice_id=p.invoice_id
            WHERE p.status='pending' AND p.provider=$1 AND r.status='held' AND p.id>$2
            ORDER BY p.id LIMIT $3''', WALLET_PROVIDER, max(int(after_id), 0), min(max(int(limit), 1), 100))]


async def get_balance_purchase(user_id, request_key):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT * FROM referral_wallet_reservations
            WHERE user_id=$1 AND request_key=$2''', user_id, request_key)
        return dict(row) if row else None


async def cancel_balance_purchase(user_id, invoice_id):
    """Release only a provably unattempted hold; activation attempts need recovery."""
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        payment = await conn.fetchrow('SELECT * FROM payments WHERE invoice_id=$1 FOR UPDATE', invoice_id)
        if not payment or payment['tg_id'] != user_id or payment['provider'] != WALLET_PROVIDER:
            raise ValueError('Оплата не найдена.')
        await _lock_users(conn, user_id)
        reservation = await conn.fetchrow('SELECT * FROM referral_wallet_reservations WHERE invoice_id=$1 FOR UPDATE', invoice_id)
        if reservation and reservation['status'] == 'cancelled':
            return dict(reservation)
        if (not reservation or reservation['status'] != 'held' or payment['status'] != 'pending'
                or await conn.fetchval('SELECT invoice_id FROM payment_subscription_activations WHERE invoice_id=$1', invoice_id)):
            raise ValueError('Активация уже началась: оплату нужно завершить, а не отменять.')
        await conn.execute("UPDATE payments SET status='cancelled',updated_at=now() WHERE invoice_id=$1", invoice_id)
        return dict(await conn.fetchrow('''UPDATE referral_wallet_reservations
            SET status='cancelled',resolved_at=now() WHERE invoice_id=$1 RETURNING *''', invoice_id))


async def confirm_refund(actor, invoice_id, amount, reference, note=''):
    """Audit an already-realized manual return, never initiate one at a provider."""
    if actor != ADMIN_ID or not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        raise PermissionError('Только администратор может подтвердить возврат.')
    amount = money(amount)
    reference, note = str(reference or '').strip(), str(note or '').strip()
    if not 3 <= len(reference) <= 160 or len(note) > 500:
        raise ValueError('Укажите уникальный номер реального перевода (3–160 символов) и примечание до 500 символов.')
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))', f'referral-refund:{reference}')
        payment = await conn.fetchrow('SELECT * FROM payments WHERE invoice_id=$1 FOR UPDATE', invoice_id)
        if (not payment or payment['status'] != 'paid' or payment['provider'] not in EXTERNAL_PROVIDERS
                or Decimal(str(payment['amount'])) <= 0):
            raise ValueError('Возврат доступен только для реального оплаченного счёта провайдера.')
        duplicate = await conn.fetchrow('SELECT * FROM referral_confirmed_refunds WHERE reference=$1', reference)
        if duplicate:
            if duplicate['payment_id'] != payment['id'] or duplicate['amount'] != amount:
                raise ValueError('Этот номер возврата уже использован с другой суммой или счётом.')
            return await _refund_result(conn, duplicate, payment)
        earning = await conn.fetchrow('SELECT * FROM referral_earnings WHERE payment_id=$1', payment['id'])
        await _lock_users(conn, payment['tg_id'], earning['referrer_id'] if earning else None)
        already = await conn.fetchval('''SELECT COALESCE(SUM(amount),0)
            FROM referral_confirmed_refunds WHERE payment_id=$1''', payment['id'])
        total = Decimal(str(already)) + amount
        paid = Decimal(str(payment['amount']))
        if total > paid:
            raise ValueError('Сумма подтверждённых возвратов превышает оплату.')
        refund = await conn.fetchrow('''INSERT INTO referral_confirmed_refunds
            (payment_id,invoice_id,amount,reference,confirmed_by,note)
            VALUES($1,$2,$3,$4,$5,$6) RETURNING *''', payment['id'], invoice_id, amount, reference, actor, note)
        reversed_amount = Decimal('0')
        if earning:
            prior_reversed = Decimal(str(await conn.fetchval('''SELECT COALESCE(SUM(amount),0)
                FROM referral_earning_reversals WHERE earning_id=$1''', earning['id'])))
            target = (Decimal(str(earning['referral_share'])) * total / paid).quantize(CENT, rounding=ROUND_HALF_UP)
            reversed_amount = target - prior_reversed
            await conn.execute('''INSERT INTO referral_earning_reversals
                (refund_id,earning_id,payment_id,referrer_id,amount) VALUES($1,$2,$3,$4,$5)''',
                refund['id'], earning['id'], payment['id'], earning['referrer_id'], reversed_amount)
            from services.referral_store import enqueue
            available = await balance(conn, earning['referrer_id'])
            await enqueue(conn, f'refund:{refund["id"]}', earning['referrer_id'],
                '<b>Возврат оплаты друга</b>\n\n'
                f'Начисление уменьшено на <b>{reversed_amount:.2f} ₽</b> пропорционально реально возвращённой сумме.\n'
                f'Доступный баланс: <b>{available:.2f} ₽</b>.'
                + ('\nНовые начисления сначала покроют отрицательный остаток. Вывод временно недоступен.' if available < 0 else ''))
        await conn.execute('''UPDATE payments SET refund_status=$2,updated_at=now() WHERE id=$1''',
                           payment['id'], 'confirmed' if total == paid else 'partially_confirmed')
        return await _refund_result(conn, refund, payment)


async def _refund_result(conn, refund, payment):
    reversed_amount = await conn.fetchval('''SELECT COALESCE(SUM(amount),0)
        FROM referral_earning_reversals WHERE refund_id=$1''', refund['id'])
    total = await conn.fetchval('''SELECT COALESCE(SUM(amount),0)
        FROM referral_confirmed_refunds WHERE payment_id=$1''', payment['id'])
    preview = await _refund_preview(conn, payment)
    return {**dict(refund), 'reversed_amount': Decimal(str(reversed_amount)),
            'total_refunded': Decimal(str(total)), 'legacy_review_required': preview['manual_review_required']}


async def _refund_preview(conn, payment):
    if (payment['status'] != 'paid' or payment['provider'] not in EXTERNAL_PROVIDERS
            or Decimal(str(payment['amount'])) <= 0):
        raise ValueError('Возврат доступен только для реального оплаченного счёта провайдера.')
    refunded = Decimal(str(await conn.fetchval('''SELECT COALESCE(SUM(amount),0)
        FROM referral_confirmed_refunds WHERE payment_id=$1''', payment['id'])))
    earning = await conn.fetchrow('SELECT id,referral_share FROM referral_earnings WHERE payment_id=$1', payment['id'])
    settlement = await conn.fetchrow('SELECT outcome FROM referral_payment_settlements WHERE payment_id=$1', payment['id'])
    paid = Decimal(str(payment['amount']))
    review = bool(not earning and qualified_payment(payment)
                  and (not settlement or settlement['outcome'] == 'legacy_excluded'))
    confirmed = await conn.fetch('''SELECT amount,reference,note,created_at
        FROM referral_confirmed_refunds WHERE payment_id=$1 ORDER BY created_at,id''', payment['id'])
    reversed_share = await conn.fetchval('''SELECT COALESCE(SUM(amount),0)
        FROM referral_earning_reversals WHERE payment_id=$1''', payment['id'])
    return {'invoice_id': payment['invoice_id'], 'payment_id': payment['id'],
            'actual_paid_rub': paid, 'refunded_rub': refunded, 'remaining_rub': paid - refunded,
            'buyer_id': payment['tg_id'], 'linked': bool(earning), 'settled': bool(settlement),
            'manual_review_required': review, 'confirmed_refunds': [dict(row) for row in confirmed],
            'commission_rub': Decimal(str(earning['referral_share'])) if earning else Decimal('0'),
            'reversed_commission_rub': Decimal(str(reversed_share))}


async def refund_preview(invoice_id):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow('SELECT * FROM payments WHERE invoice_id=$1', invoice_id)
        if not payment:
            raise ValueError('Счёт не найден.')
        return await _refund_preview(conn, payment)


async def record_view(user_id, surface='earn'):
    if surface not in ('bot', 'telegram', 'miniapp', 'earn'):
        raise ValueError('Неизвестный источник просмотра.')
    # Optional analytics never opens a live connection or blocks the cabinet.
    if db._pool is None:
        return False
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute('INSERT INTO referral_cabinet_views(user_id,surface) VALUES($1,$2)', user_id, surface)
        return True
    except Exception as exc:
        logger.warning('Referral view not recorded: %s', type(exc).__name__)
        return False


async def legacy_review_report(offset=0, limit=30):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        return [dict(row) for row in await conn.fetch('''SELECT id,referrer_id,referred_user_id,
            tariff_code,amount,referral_share,is_first_purchase,created_at,
            'unlinked_legacy_earning' AS review_reason FROM referral_earnings
            WHERE payment_id IS NULL ORDER BY created_at DESC,id DESC LIMIT $1 OFFSET $2''',
            min(max(int(limit), 1), 100), max(int(offset), 0))]


async def review_report(limit=30, offset=0):
    """Missing invoice identity is a review issue, never a matching heuristic."""
    limit, offset = min(max(int(limit), 1), 100), max(int(offset), 0)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''SELECT * FROM (
            SELECT 'unlinked_legacy_earning' AS kind,NULL::bigint AS payment_id,
                NULL::text AS invoice_id,referred_user_id AS buyer_id,referral_share AS amount,
                'Старое начисление без связи со счётом; не сопоставляйте автоматически.' AS detail,
                created_at,id AS source_id FROM referral_earnings WHERE payment_id IS NULL
            UNION ALL
            SELECT 'unlinked_confirmed_refund',r.payment_id,r.invoice_id,p.tg_id,r.amount,
                'Фактический возврат старого счёта; начисление нельзя безопасно восстановить или отменить автоматически.',
                r.created_at,r.id FROM referral_confirmed_refunds r JOIN payments p ON p.id=r.payment_id
                LEFT JOIN referral_payment_settlements s ON s.payment_id=p.id
                WHERE COALESCE(p.payment_kind,'subscription')='subscription'
                  AND (s.payment_id IS NULL OR s.outcome='legacy_excluded')
                  AND NOT EXISTS(SELECT 1 FROM referral_earnings e WHERE e.payment_id=p.id)
            UNION ALL
            SELECT 'legacy_invoice_excluded',p.id,p.invoice_id,p.tg_id,p.amount,
                'Счёт уже был оплачен до миграции. Старую комиссию нельзя надёжно сопоставить; автоматическое доначисление исключено.',
                p.updated_at,p.id FROM payments p JOIN referral_payment_settlements s ON s.payment_id=p.id
                JOIN users u ON u.tg_id=p.tg_id JOIN users inviter ON inviter.tg_id=u.referrer_id
                WHERE s.outcome='legacy_excluded' AND p.provider IN ('cryptobot','yookassa')
                  AND COALESCE(p.payment_kind,'subscription')='subscription' AND p.amount>0
                  AND u.tg_id<>u.referrer_id
                  AND NOT EXISTS(SELECT 1 FROM referral_earnings e WHERE e.payment_id=p.id)
            UNION ALL
            SELECT 'missing_settlement',p.id,p.invoice_id,p.tg_id,p.amount,
                'Оплаченный счёт без результата settlement: ручная проверка, не повторное начисление.',
                p.updated_at,p.id FROM payments p LEFT JOIN referral_payment_settlements s ON s.payment_id=p.id
                WHERE p.status='paid' AND s.payment_id IS NULL
                  AND p.provider IN ('cryptobot','yookassa')
                  AND COALESCE(p.payment_kind,'subscription')='subscription' AND p.amount>0
            ) report ORDER BY created_at DESC,kind,source_id DESC LIMIT $1 OFFSET $2''', limit + 1, offset)
        return {'items': [dict(row) for row in rows[:limit]], 'has_more': len(rows) > limit}


async def funnel_stats():
    """Observed views and outcomes only; no claim to measuring link shares."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT
            (SELECT COUNT(*) FROM referral_cabinet_views) AS views,
            (SELECT COUNT(DISTINCT user_id) FROM referral_cabinet_views) AS unique_viewers,
            (SELECT COUNT(*) FROM users u JOIN users r ON r.tg_id=u.referrer_id
                WHERE u.tg_id<>u.referrer_id) AS attributed_registrations,
            (SELECT COUNT(*) FROM referral_payment_settlements WHERE outcome='credited' AND is_first_purchase) AS first_payments,
            (SELECT COUNT(*) FROM referral_payment_settlements WHERE outcome='credited' AND NOT is_first_purchase) AS repeat_payments,
            (SELECT COALESCE(SUM(amount),0) FROM referral_payment_settlements WHERE outcome='credited') AS external_paid_amount,
            (SELECT COALESCE(SUM(referral_share),0) FROM referral_earnings WHERE payment_id IS NOT NULL) AS credited_earnings,
            (SELECT COALESCE(SUM(amount),0) FROM referral_earning_reversals) AS reversed_earnings,
            (SELECT COALESCE(SUM(amount),0) FROM referral_withdrawals
                WHERE status='completed' AND withdrawal_type LIKE 'subscription_%') AS wallet_spend,
            (SELECT COALESCE(SUM(amount),0) FROM referral_withdrawals
                WHERE status='completed' AND withdrawal_type IN ('sbp','usdt')) AS cash_payouts,
            (SELECT COALESCE(SUM(amount),0) FROM referral_withdrawals
                WHERE status='pending' AND withdrawal_type IN ('sbp','usdt')) AS pending_cash_payouts,
            (SELECT COUNT(*) FROM referral_earnings WHERE payment_id IS NULL) AS unlinked_legacy_earnings,
            (SELECT COALESCE(SUM(referral_share),0) FROM referral_earnings WHERE payment_id IS NULL) AS unlinked_legacy_amount''')
        return dict(row)
