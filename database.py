import asyncpg
import logging
from config import DATABASE_URL


# Глобальный пул подключений
_pool = None


async def init_db():
    """Инициализировать пул подключений и создать таблицы если нужно"""
    global _pool
    
    try:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=5,
            max_size=20,
            command_timeout=60
        )
        logging.info("Database pool initialized successfully")
    except Exception as e:
        logging.error(f"Failed to initialize database pool: {e}")
        raise


async def close_db():
    """Закрыть пул подключений"""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logging.info("Database pool closed")


async def get_pool():
    """Получить пул подключений"""
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db() first.")
    return _pool


async def db_execute(query, params=(), fetch_one=False, fetch_all=False):
    """
    Выполнить SQL запрос
    
    Args:
        query: SQL запрос
        params: Параметры для запроса (кортеж или список)
        fetch_one: Получить одну строку результата
        fetch_all: Получить все строки результата
        
    Returns:
        Результат запроса или None
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            if fetch_one:
                return await conn.fetchrow(query, *params)
            elif fetch_all:
                return await conn.fetch(query, *params)
            else:
                await conn.execute(query, *params)
                return None
        except Exception as e:
            logging.error(f"Database error: {e}")
            raise


async def acquire_user_lock(tg_id: int) -> bool:
    """
    Получить блокировку пользователя используя PostgreSQL advisory lock
    
    Args:
        tg_id: ID пользователя Telegram
        
    Returns:
        True если удалось получить блокировку, False иначе
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            # Используем advisory lock с ID пользователя
            result = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1);",
                tg_id
            )
            return result is True
        except Exception as e:
            logging.error(f"Lock error: {e}")
            return False


async def release_user_lock(tg_id: int):
    """
    Освободить блокировку пользователя
    
    Args:
        tg_id: ID пользователя Telegram
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "SELECT pg_advisory_unlock($1);",
                tg_id
            )
        except Exception as e:
            logging.error(f"Unlock error: {e}")


# ────────────────────────────────────────────────
#                USER MANAGEMENT
# ────────────────────────────────────────────────

async def get_user(tg_id: int):
    """Получить информацию о пользователе"""
    return await db_execute(
        "SELECT * FROM users WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )


async def user_exists(tg_id: int) -> bool:
    """Проверить существует ли пользователь"""
    result = await db_execute(
        "SELECT 1 FROM users WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )
    return result is not None


async def create_user(tg_id: int, username: str, referrer_id=None):
    """Создать или обновить пользователя"""
    await db_execute(
        """
        INSERT INTO users (tg_id, username, referrer_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (tg_id) DO NOTHING
        """,
        (tg_id, username, referrer_id)
    )


# ────────────────────────────────────────────────
#           TERMS AND CONDITIONS
# ────────────────────────────────────────────────

async def accept_terms(tg_id: int):
    """Пользователь принял условия использования"""
    await db_execute(
        "UPDATE users SET accepted_terms = TRUE WHERE tg_id = $1",
        (tg_id,)
    )


async def has_accepted_terms(tg_id: int) -> bool:
    """Проверить принял ли пользователь условия"""
    user = await get_user(tg_id)
    return user and user['accepted_terms']


# ────────────────────────────────────────────────
#             SUBSCRIPTION MANAGEMENT
# ────────────────────────────────────────────────

async def update_subscription(tg_id: int, uuid: str, username: str, subscription_until: str, squad_uuid: str):
    """Обновить подписку пользователя"""
    await db_execute(
        """
        UPDATE users 
        SET remnawave_uuid = $1, 
            remnawave_username = $2, 
            subscription_until = $3, 
            squad_uuid = $4 
        WHERE tg_id = $5
        """,
        (uuid, username, subscription_until, squad_uuid, tg_id)
    )


async def has_subscription(tg_id: int) -> bool:
    """Проверить есть ли активная подписка"""
    user = await get_user(tg_id)
    return user and user['remnawave_uuid'] is not None


# ────────────────────────────────────────────────
#               PAYMENT MANAGEMENT
# ────────────────────────────────────────────────

async def create_payment(tg_id: int, tariff_code: str, amount: float, provider: str, invoice_id: str):
    """Создать запись о платеже"""
    from datetime import datetime
    await db_execute(
        """
        INSERT INTO payments (tg_id, tariff_code, amount, created_at, provider, invoice_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        (tg_id, tariff_code, amount, datetime.utcnow().isoformat(), provider, str(invoice_id))
    )


async def get_pending_payments():
    """Получить все ожидающие платежи"""
    return await db_execute(
        "SELECT id, tg_id, invoice_id, tariff_code FROM payments WHERE status = 'pending' AND provider = 'cryptobot' ORDER BY id",
        fetch_all=True
    )


async def get_last_pending_payment(tg_id: int):
    """Получить последний ожидающий платеж пользователя"""
    result = await db_execute(
        """
        SELECT invoice_id, tariff_code 
        FROM payments 
        WHERE tg_id = $1 AND status = 'pending' AND provider = 'cryptobot' 
        ORDER BY id DESC 
        LIMIT 1
        """,
        (tg_id,),
        fetch_one=True
    )
    return (result['invoice_id'], result['tariff_code']) if result else None


async def update_payment_status(payment_id: int, status: str):
    """Обновить статус платежа"""
    await db_execute(
        "UPDATE payments SET status = $1 WHERE id = $2",
        (status, payment_id)
    )


async def update_payment_status_by_invoice(invoice_id: str, status: str):
    """Обновить статус платежа по invoice_id"""
    await db_execute(
        "UPDATE payments SET status = $1 WHERE invoice_id = $2",
        (status, invoice_id)
    )


# ────────────────────────────────────────────────
#               REFERRAL MANAGEMENT
# ────────────────────────────────────────────────

async def update_referral_count(tg_id: int):
    """Увеличить счётчик рефералов"""
    await db_execute(
        "UPDATE users SET referral_count = referral_count + 1 WHERE tg_id = $1",
        (tg_id,)
    )


async def increment_active_referrals(tg_id: int):
    """Увеличить счётчик активных рефералов"""
    await db_execute(
        "UPDATE users SET active_referrals = active_referrals + 1 WHERE tg_id = $1",
        (tg_id,)
    )


async def get_referral_stats(tg_id: int):
    """Получить статистику рефералов пользователя"""
    result = await db_execute(
        "SELECT referral_count, active_referrals FROM users WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )
    return (result['referral_count'], result['active_referrals']) if result else (0, 0)


async def get_referrer(tg_id: int):
    """Получить информацию о рефералите"""
    result = await db_execute(
        "SELECT referrer_id, first_payment FROM users WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )
    return (result['referrer_id'], result['first_payment']) if result else (None, False)


async def mark_first_payment(tg_id: int):
    """Отметить что пользователь сделал первый платёж"""
    await db_execute(
        "UPDATE users SET first_payment = TRUE WHERE tg_id = $1",
        (tg_id,)
    )


# ────────────────────────────────────────────────
#                GIFT MANAGEMENT
# ────────────────────────────────────────────────

async def is_gift_received(tg_id: int) -> bool:
    """Проверить получил ли пользователь подарок"""
    user = await get_user(tg_id)
    return user and user['gift_received']


async def mark_gift_received(tg_id: int):
    """Отметить что пользователь получил подарок"""
    await db_execute(
        "UPDATE users SET gift_received = TRUE WHERE tg_id = $1",
        (tg_id,)
    )


# ────────────────────────────────────────────────
#              PROMO CODE MANAGEMENT
# ────────────────────────────────────────────────

async def get_promo_code(code: str):
    """Получить информацию о промокоде"""
    result = await db_execute(
        "SELECT days, max_uses, used_count, active FROM promo_codes WHERE code = $1",
        (code,),
        fetch_one=True
    )
    return (result['days'], result['max_uses'], result['used_count'], result['active']) if result else None


async def create_promo_code(code: str, days: int, max_uses: int):
    """Создать новый промокод"""
    await db_execute(
        "INSERT INTO promo_codes (code, days, max_uses, active) VALUES ($1, $2, $3, TRUE) ON CONFLICT (code) DO UPDATE SET days = $2, max_uses = $3, active = TRUE",
        (code.upper(), days, max_uses)
    )


async def increment_promo_usage(code: str):
    """Увеличить счётчик использования промокода"""
    await db_execute(
        "UPDATE promo_codes SET used_count = used_count + 1 WHERE code = $1",
        (code,)
    )
