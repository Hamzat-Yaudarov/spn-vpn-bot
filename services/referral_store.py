"""Durable payout requests and Telegram outbox. Does not transfer money."""
import asyncio
from decimal import Decimal
from html import escape
import logging

import database as db
from config import ADMIN_ID
from services.referral_program import parse_amount, validate_details, STATUSES
from services import referral_accounting as accounting

logger = logging.getLogger(__name__)
TABLES = {
    'referral': ('referral_withdrawals', 'referrer_id', 'referral_earnings', 'referral_share'),
    'partner': ('partner_withdrawals', 'partner_id', 'partner_earnings', 'partner_share'),
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS referral_withdrawal_meta (
    program TEXT NOT NULL,
    withdrawal_id BIGINT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    resolved_at TIMESTAMP,
    resolved_by BIGINT,
    decision_note TEXT,
    PRIMARY KEY (program, withdrawal_id)
);
CREATE TABLE IF NOT EXISTS referral_outbox (
    event_key TEXT PRIMARY KEY,
    recipient_id BIGINT NOT NULL,
    body TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT now(),
    lease_until TIMESTAMP,
    delivered_at TIMESTAMP,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS referral_outbox_pending ON referral_outbox(next_attempt_at)
    WHERE delivered_at IS NULL;
'''


def table_info(program):
    if program not in TABLES:
        raise ValueError('Неизвестная программа.')
    return TABLES[program]


async def enqueue(conn, key, recipient, body):
    if recipient <= 0:
        return
    await conn.execute('''INSERT INTO referral_outbox(event_key,recipient_id,body)
        VALUES($1,$2,$3) ON CONFLICT(event_key) DO NOTHING''', key, recipient, body)


def admin_notice(program, row, user_id):
    number = f'{"R" if program == "referral" else "P"}-{row["id"]}'
    details = (f'Банк: {escape(row["bank_name"] or "")}\nТелефон: {escape(row["phone_number"] or "")}'
               if row['withdrawal_type'] == 'sbp' else f'USDT: <code>{escape(row["usdt_address"] or "")}</code>')
    return (f'<b>Заявка на вывод №{number}</b>\nПользователь: <code>{user_id}</code>\n'
            f'Сумма: <b>{row["amount"]:.2f} ₽</b>\n{details}\n\n'
            'Откройте «Ещё → Админ-панель → Выплаты». '
            'Отмечайте выплату только после реального перевода. Повторное уведомление с тем же номером — не новая заявка.')


async def initialize():
    if isinstance(ADMIN_ID, bool) or not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        raise ValueError('Для реферальных выплат требуется положительный целочисленный ADMIN_ID.')
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
        await accounting.initialize(conn)
        # Recover old pending requests too; subscriptions are never payout requests.
        async with conn.transaction():
            for program, (table, owner, _, _) in TABLES.items():
                rows = await conn.fetch(f"SELECT * FROM {table} WHERE status='pending' AND withdrawal_type IN ('sbp','usdt')")
                for row in rows:
                    await enqueue(conn, f'withdraw:{program}:{row["id"]}', ADMIN_ID, admin_notice(program, row, row[owner]))


async def create_request(program, user_id, amount, method, request_key, *, bank='', phone='', address=''):
    table, owner, earnings, share = table_info(program)
    amount = parse_amount(amount)
    bank, phone, address = validate_details(method, bank, phone, address)
    if not request_key or len(request_key) > 100:
        raise ValueError('Начните оформление заявки заново.')
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serialize balance checks and reservation; ledger is the source of truth.
            await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))', f'referral-withdrawal:{request_key}')
            if not await conn.fetchrow('SELECT tg_id FROM users WHERE tg_id=$1 FOR UPDATE', user_id):
                raise ValueError('Пользователь не найден.')
            existing = await conn.fetchrow(f'''SELECT w.*,m.decision_note FROM referral_withdrawal_meta m
                JOIN {table} w ON w.id=m.withdrawal_id
                WHERE m.request_key=$1 AND m.program=$2 AND w.{owner}=$3''', request_key, program, user_id)
            if existing:
                if (Decimal(str(existing['amount'])) != amount or existing['withdrawal_type'] != method
                        or (existing['bank_name'] or '') != bank or (existing['phone_number'] or '') != phone
                        or (existing['usdt_address'] or '') != address):
                    raise accounting.RequestConflict('Номер запроса уже использован для другой заявки.')
                return dict(existing)
            # The global unique key may belong to another owner/program. Never
            # return its details, and never let a uniqueness error leak them.
            if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM referral_withdrawal_meta WHERE request_key=$1)', request_key):
                raise accounting.RequestConflict('Номер запроса уже использован для другой заявки.')
            if program == 'partner' and not await conn.fetchval(
                'SELECT agreement_accepted FROM partnerships WHERE tg_id=$1', user_id
            ):
                raise ValueError('Сначала примите партнёрское соглашение.')
            if program == 'referral':
                available = await accounting.balance(conn, user_id)
            else:
                earned = await conn.fetchval(f'SELECT COALESCE(SUM({share}),0) FROM {earnings} WHERE {owner}=$1', user_id)
                reserved = await conn.fetchval(f"SELECT COALESCE(SUM(amount),0) FROM {table} WHERE {owner}=$1 AND status IN ('pending','completed')", user_id)
                available = earned - reserved
            if amount > available:
                raise accounting.InsufficientBalance(available, amount)
            row = await conn.fetchrow(f'''INSERT INTO {table}({owner},amount,withdrawal_type,bank_name,phone_number,usdt_address)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *''', user_id, amount, method, bank or None, phone or None, address or None)
            await conn.execute('INSERT INTO referral_withdrawal_meta(program,withdrawal_id,request_key) VALUES($1,$2,$3)', program, row['id'], request_key)
            await enqueue(conn, f'withdraw:{program}:{row["id"]}', ADMIN_ID, admin_notice(program, row, user_id))
            return dict(row)


async def get_request(user_id, request_key, program='referral'):
    """Look up one owned durable cash request; a foreign key is indistinguishable."""
    table, owner, _, _ = table_info(program)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f'''SELECT w.*,m.request_key,m.decision_note FROM referral_withdrawal_meta m
            JOIN {table} w ON w.id=m.withdrawal_id
            WHERE m.request_key=$1 AND m.program=$2 AND w.{owner}=$3
              AND w.withdrawal_type IN ('sbp','usdt')''', request_key, program, user_id)
        return dict(row) if row else None


async def history(user_id, program='referral', *, offset=0, limit=10):
    table, owner, earnings, share = table_info(program)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if program == 'referral':
            return [dict(row) for row in await conn.fetch('''SELECT * FROM (
                SELECT 'earning' AS kind,e.id,e.referral_share AS amount,'credited' AS status,
                    e.tariff_code AS detail,e.created_at
                FROM referral_earnings e WHERE e.referrer_id=$1
                UNION ALL
                SELECT 'reversal',r.id,r.amount,'confirmed',p.tariff_code,r.created_at
                FROM referral_earning_reversals r JOIN payments p ON p.id=r.payment_id WHERE r.referrer_id=$1
                UNION ALL
                SELECT 'withdrawal',w.id,w.amount,w.status,w.withdrawal_type,w.created_at
                FROM referral_withdrawals w
                WHERE w.referrer_id=$1
            ) h ORDER BY created_at DESC,id DESC,kind LIMIT $2 OFFSET $3''', user_id, limit, offset)]
        return [dict(row) for row in await conn.fetch(f'''
            SELECT * FROM (
                SELECT 'earning' AS kind,id,{share} AS amount,'credited' AS status,tariff_code AS detail,created_at
                FROM {earnings} WHERE {owner}=$1
                UNION ALL
                SELECT 'withdrawal',id,amount,status,withdrawal_type,created_at
                FROM {table} WHERE {owner}=$1
            ) h ORDER BY created_at DESC,id DESC,kind LIMIT $2 OFFSET $3''', user_id, limit, offset)]


async def list_requests(status='pending', offset=0, limit=30):
    if status not in ('pending', 'completed', 'rejected', 'all'):
        raise ValueError('Неизвестный статус.')
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch('''WITH selected AS (
            SELECT * FROM (
                SELECT 'referral' AS program,id,referrer_id AS user_id,amount,withdrawal_type,bank_name,phone_number,usdt_address,status,created_at FROM referral_withdrawals
                UNION ALL
                SELECT 'partner',id,partner_id,amount,withdrawal_type,bank_name,phone_number,usdt_address,status,created_at FROM partner_withdrawals
            ) requests WHERE withdrawal_type IN ('sbp','usdt') AND ($1='all' OR status=$1)
            ORDER BY created_at DESC,program,id DESC LIMIT $2 OFFSET $3
            ), owners AS (
                SELECT DISTINCT user_id FROM selected WHERE program='referral' AND status='pending'
            ), ledger AS (
                SELECT owners.user_id,
                    COALESCE((SELECT SUM(referral_share) FROM referral_earnings WHERE referrer_id=owners.user_id),0)
                    - COALESCE((SELECT SUM(amount) FROM referral_earning_reversals WHERE referrer_id=owners.user_id),0)
                    - COALESCE((SELECT SUM(amount) FROM referral_withdrawals
                        WHERE referrer_id=owners.user_id AND status IN ('pending','completed')),0)
                    - COALESCE((SELECT SUM(amount) FROM referral_wallet_reservations
                        WHERE user_id=owners.user_id AND status='held'),0) AS available
                FROM owners
            ) SELECT w.*,u.username,m.decision_note,m.resolved_at,
            o.delivered_at AS notified_at,o.attempts AS notification_attempts,o.last_error AS notification_error,
            ledger.available AS current_available_balance,
            (w.program='referral' AND w.status='pending' AND COALESCE(ledger.available<0,FALSE)) AS payout_blocked
            FROM selected w LEFT JOIN users u ON u.tg_id=w.user_id
            LEFT JOIN ledger ON ledger.user_id=w.user_id AND w.program='referral' AND w.status='pending'
            LEFT JOIN referral_withdrawal_meta m ON m.program=w.program AND m.withdrawal_id=w.id
            LEFT JOIN referral_outbox o ON o.event_key='withdraw:'||w.program||':'||w.id::text
            ORDER BY w.created_at DESC,w.program,w.id DESC''', status, limit, offset)]


async def resolve_request(program, withdrawal_id, status, actor, note=''):
    if actor != ADMIN_ID:
        raise PermissionError('Только администратор может обработать заявку.')
    if status not in ('completed', 'rejected'):
        raise ValueError('Неизвестный статус.')
    note = note.strip()
    if len(note) > 500 or (status == 'rejected' and not note):
        raise ValueError('Для отклонения укажите причину (до 500 символов).')
    table, owner, _, _ = table_info(program)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # All balance mutations lock the user before any withdrawal row.
            candidate = await conn.fetchrow(f"SELECT * FROM {table} WHERE id=$1 AND withdrawal_type IN ('sbp','usdt')", withdrawal_id)
            if not candidate:
                raise ValueError('Заявка не найдена.')
            await conn.fetchrow('SELECT tg_id FROM users WHERE tg_id=$1 FOR UPDATE', candidate[owner])
            row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id=$1 AND withdrawal_type IN ('sbp','usdt') FOR UPDATE", withdrawal_id)
            if not row:
                raise ValueError('Заявка не найдена.')
            if row['status'] == status:
                return dict(row)
            if row['status'] != 'pending':
                raise ValueError('Заявка уже обработана. Обновите список.')
            if status == 'completed' and program == 'referral' and await accounting.balance(conn, row[owner]) < 0:
                raise ValueError('Выплата заблокирована: после возврата оплаты средств недостаточно. Не переводите деньги; отклоните заявку или дождитесь новых начислений.')
            await conn.execute(f'UPDATE {table} SET status=$1 WHERE id=$2', status, withdrawal_id)
            await conn.execute('''INSERT INTO referral_withdrawal_meta(program,withdrawal_id,request_key,resolved_at,resolved_by,decision_note)
                VALUES($1,$2,$3,now(),$4,$5) ON CONFLICT(program,withdrawal_id)
                DO UPDATE SET resolved_at=now(),resolved_by=$4,decision_note=$5''', program, withdrawal_id, f'legacy:{program}:{withdrawal_id}', actor, note)
            number = f'{"R" if program == "referral" else "P"}-{withdrawal_id}'
            await enqueue(conn, f'resolved:{program}:{withdrawal_id}', row[owner],
                f'<b>{STATUSES[status]}</b>\n\n'
                f'<blockquote>Сумма: <b>{row["amount"]:.2f} ₽</b>\nЗаявка: {number}</blockquote>'
                + (f'\n\n{escape(note)}' if note else ''))
            return {**dict(row), 'status': status}


async def credit_referral(referrer, referred_user, tariff, amount, first=False):
    """Earning and its notification commit together. Existing payment activation owns deduplication."""
    share = accounting.calculate_share(amount, first=first)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await accounting._lock_users(conn, referred_user, referrer)
            earning_id = await conn.fetchval('''INSERT INTO referral_earnings
                (referrer_id,referred_user_id,tariff_code,amount,referral_share,is_first_purchase)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING id''', referrer, referred_user, tariff, Decimal(str(amount)), share, first)
            await conn.execute('UPDATE users SET first_payment=TRUE WHERE tg_id=$1', referred_user)
            await enqueue(conn, f'earning:{earning_id}', referrer,
                '<b>🎉 Друг купил подписку!</b>\n\n'
                f'<blockquote>Вам на баланс: <b>+{share:.2f} ₽</b>\n'
                f'{"35% с первой" if first else "15% с повторной"} покупки друга</blockquote>\n\n'
                'Баланс и вывод — в разделе «💰 Заработать».')
    return True


async def deliver_once(bot):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''WITH ready AS (
            SELECT event_key FROM referral_outbox WHERE delivered_at IS NULL AND next_attempt_at<=now()
            AND (lease_until IS NULL OR lease_until<now()) ORDER BY next_attempt_at LIMIT 10 FOR UPDATE SKIP LOCKED
        ) UPDATE referral_outbox o SET lease_until=now()+interval '5 minutes',attempts=attempts+1
          FROM ready WHERE o.event_key=ready.event_key RETURNING o.*''')
    for row in rows:
        try:
            await bot.send_message(row['recipient_id'], row['body'], parse_mode='HTML', request_timeout=20)
        except Exception as exc:
            delay = max(int(getattr(exc, 'retry_after', 0)) + 1, min(3600, 30 * 2 ** min(row['attempts'], 7)))
            # Never persist raw exception strings, which may contain personal data or tokens.
            async with pool.acquire() as conn:
                await conn.execute('''UPDATE referral_outbox SET lease_until=NULL,last_error=$2,
                    next_attempt_at=now()+$3*interval '1 second' WHERE event_key=$1''', row['event_key'], type(exc).__name__, delay)
            logger.warning('Referral notification retry: event=%s error=%s', row['event_key'], type(exc).__name__)
        else:
            async with pool.acquire() as conn:
                await conn.execute('UPDATE referral_outbox SET delivered_at=now(),lease_until=NULL,last_error=NULL WHERE event_key=$1', row['event_key'])


async def run_delivery_loop(bot):
    while True:
        try:
            await deliver_once(bot)
        except Exception:
            logger.exception('Referral outbox iteration failed; will retry')
        await asyncio.sleep(5)
