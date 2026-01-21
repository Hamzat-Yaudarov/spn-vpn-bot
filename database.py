import asyncpg
import logging
from config import (
    DATABASE_URL,
    PAYMENT_EXPIRY_TIME,
    GIFT_REQUEST_COOLDOWN,
    PROMO_REQUEST_COOLDOWN,
    PAYMENT_CHECK_COOLDOWN
)


# Глобальный пул подключений
_pool = None


async def run_migrations():
    """Запустить автоматические миграции при старте бота"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            logging.info("Running migrations...")

            # ═══════════════════════════════════════════════════════════
            # ЭТАП 1: СОЗДАНИЕ ТАБЛИЦ (если не существуют)
            # ═══════════════════════════════════════════════════════════

            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    tg_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now(),

                    -- Условия и подписка (Обычная подписка через Remnawave)
                    accepted_terms BOOLEAN DEFAULT FALSE,
                    remnawave_uuid UUID,
                    remnawave_username TEXT,
                    subscription_until TIMESTAMP,
                    squad_uuid UUID,

                    -- VIP подписка через 3X-UI
                    xui_email TEXT,
                    xui_uuid TEXT,
                    xui_subscription_id TEXT,
                    vip_subscription_until TIMESTAMP,

                    -- Баланс в боте
                    balance NUMERIC DEFAULT 0,

                    -- Реферальная программа
                    referrer_id BIGINT,
                    first_payment BOOLEAN DEFAULT FALSE,
                    referral_count INT DEFAULT 0,
                    active_referrals INT DEFAULT 0,
                    referral_commission NUMERIC DEFAULT 0,

                    -- Подарки
                    gift_received BOOLEAN DEFAULT FALSE,

                    -- Anti-spam тайм-стемпы
                    last_gift_attempt TIMESTAMP,
                    last_promo_attempt TIMESTAMP,
                    last_payment_check TIMESTAMP
                )
            """)
            logging.info("✅ Таблица 'users' создана или уже существует")

            # Таблица платежей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id BIGSERIAL PRIMARY KEY,
                    tg_id BIGINT NOT NULL,
                    tariff_code TEXT NOT NULL,
                    amount NUMERIC NOT NULL,
                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now(),
                    provider TEXT NOT NULL,
                    invoice_id TEXT UNIQUE NOT NULL,
                    status TEXT DEFAULT 'pending'
                )
            """)
            logging.info("✅ Таблица 'payments' создана или уже существует")

            # Таблица промокодов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    id BIGSERIAL PRIMARY KEY,
                    code TEXT UNIQUE NOT NULL,
                    days INT NOT NULL,
                    max_uses INT NOT NULL,
                    used_count INT DEFAULT 0,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT now()
                )
            """)
            logging.info("✅ Таблица 'promo_codes' создана или уже существует")

            # ═══════════════════════════════════════════════════════════
            # ЭТАП 2: СОЗДАНИЕ ИНДЕКСОВ (для быстрого поиска)
            # ═══════════════════════════════════════════════════════════

            index_queries = [
                # users индексы
                "CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_users_remnawave_uuid ON users(remnawave_uuid);",
                "CREATE INDEX IF NOT EXISTS idx_users_referrer_id ON users(referrer_id);",
                "CREATE INDEX IF NOT EXISTS idx_users_xui_email ON users(xui_email);",
                "CREATE INDEX IF NOT EXISTS idx_users_xui_uuid ON users(xui_uuid);",

                # payments индексы
                "CREATE INDEX IF NOT EXISTS idx_payments_tg_id ON payments(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);",
                "CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider);",
                "CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);",

                # promo_codes индексы
                "CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);",
            ]

            for query in index_queries:
                try:
                    await conn.execute(query)
                except Exception as e:
                    # Индекс уже может существовать - это нормально
                    if "already exists" not in str(e).lower():
                        logging.debug(f"Index creation note: {e}")

            logging.info("✅ Индексы созданы или уже существуют")

            # ═══════════════════════════════════════════════════════════
            # ЭТАП 3: ДОБАВЛЕНИЕ НЕДОСТАЮЩИХ СТОЛБЦОВ
            # ═══════════════════════════════════════════════════════════

            # Эти столбцы могут не существовать в старых БД - добавляем их
            alter_queries = [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_gift_attempt TIMESTAMP;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_promo_attempt TIMESTAMP;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_payment_check TIMESTAMP;",
                # VIP подписка через 3X-UI
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS xui_email TEXT;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS xui_uuid TEXT;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS xui_subscription_id TEXT;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_subscription_until TIMESTAMP;",
                # Баланс
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance NUMERIC DEFAULT 0;",
                # Реферальные комиссии
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_commission NUMERIC DEFAULT 0;",
            ]

            for query in alter_queries:
                try:
                    await conn.execute(query)
                    logging.info(f"✅ Столбец добавлен: {query.strip()}")
                except Exception as e:
                    if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                        logging.debug(f"Столбец уже существует, пропускаем: {query.strip()}")
                    else:
                        logging.warning(f"⚠️ Ошибка миграции: {e}")

            logging.info("━" * 60)
            logging.info("✨ ВСЕ МИГРАЦИИ УСПЕШНО ЗАВЕРШЕНЫ ✨")
            logging.info("━" * 60)

        except Exception as e:
            logging.error(f"❌ ОШИБКА МИГРАЦИИ: {e}")
            raise


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

        # Запускаем миграции при инициализации БД
        await run_migrations()

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
        (tg_id, tariff_code, amount, datetime.utcnow(), provider, str(invoice_id))
    )


async def get_pending_payments():
    """Получить все ожидающие платежи"""
    return await db_execute(
        "SELECT id, tg_id, invoice_id, tariff_code FROM payments WHERE status = 'pending' AND provider = 'cryptobot' ORDER BY id",
        fetch_all=True
    )


async def get_pending_payments_by_provider(provider: str):
    """Получить все ожидающие платежи по конкретному провайдеру"""
    return await db_execute(
        "SELECT id, tg_id, invoice_id, tariff_code FROM payments WHERE status = 'pending' AND provider = $1 ORDER BY id",
        (provider,),
        fetch_all=True
    )


async def get_active_payment_for_user_and_tariff(tg_id: int, tariff_code: str, provider: str):
    """
    Получить существующий неоплаченный счёт пользователя для конкретного тарифа и провайдера

    Args:
        tg_id: ID пользователя Telegram
        tariff_code: Код тарифа
        provider: Провайдер платежа (cryptobot, yookassa)

    Returns:
        Кортеж (invoice_id, pay_url) или None если нет активного счёта
    """
    from datetime import datetime, timedelta, timezone

    result = await db_execute(
        """
        SELECT id, invoice_id, created_at FROM payments
        WHERE tg_id = $1 AND tariff_code = $2 AND status = 'pending' AND provider = $3
        ORDER BY id DESC
        LIMIT 1
        """,
        (tg_id, tariff_code, provider),
        fetch_one=True
    )

    if not result:
        return None

    # Проверяем, не истёк ли счёт
    created_at = result['created_at']
    age = datetime.utcnow() - created_at

    if age.total_seconds() > PAYMENT_EXPIRY_TIME:
        # Счёт истёк, удаляем его
        await delete_payment(result['id'])
        return None

    # Счёт ещё активен
    return result['invoice_id']


async def delete_payment(payment_id: int):
    """Удалить платёж из БД"""
    await db_execute(
        "DELETE FROM payments WHERE id = $1",
        (payment_id,)
    )


async def delete_expired_payments(seconds: int = None):
    """
    Удалить все неоплаченные счёты старше N секунд

    Args:
        seconds: Время в секундах (по умолчанию PAYMENT_EXPIRY_TIME из конфига)
    """
    from datetime import datetime, timedelta, timezone

    if seconds is None:
        seconds = PAYMENT_EXPIRY_TIME

    cutoff_time = datetime.utcnow() - timedelta(seconds=seconds)

    await db_execute(
        "DELETE FROM payments WHERE status = 'pending' AND created_at < $1",
        (cutoff_time,)
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


async def mark_gift_received_atomic(tg_id: int) -> bool:
    """
    Атомарно проверить и отметить что пользователь получил подарок.
    Возвращает True если удалось отметить (подарок не был получен), False иначе
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Атомарно получить статус и обновить
            result = await conn.fetchval(
                """
                UPDATE users
                SET gift_received = TRUE
                WHERE tg_id = $1 AND gift_received = FALSE
                RETURNING 1
                """,
                tg_id
            )
            return result is not None


async def can_request_gift(tg_id: int) -> tuple[bool, str]:
    """
    Проверить может ли пользователь получить подарок.
    Возвращает (True/False, сообщение об ошибке если есть)
    """
    from datetime import datetime, timezone, timedelta

    user = await get_user(tg_id)

    if not user:
        return False, "Пользователь не найден"

    if user.get('gift_received', False):
        return False, "Ты уже получал подарок"

    # Проверяем anti-spam: не более одной попытки за GIFT_REQUEST_COOLDOWN секунд
    last_gift_attempt = user.get('last_gift_attempt')
    if last_gift_attempt:
        time_since_attempt = datetime.utcnow() - last_gift_attempt
        if time_since_attempt < timedelta(seconds=GIFT_REQUEST_COOLDOWN):
            return False, "Подожди пару секунд ⏳"

    return True, ""


async def update_last_gift_attempt(tg_id: int):
    """Обновить время последней попытки получить подарок"""
    from datetime import datetime
    await db_execute(
        "UPDATE users SET last_gift_attempt = $1 WHERE tg_id = $2",
        (datetime.utcnow(), tg_id)
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


async def increment_promo_usage_atomic(code: str) -> tuple[bool, str]:
    """
    Атомарно проверить и увеличить счётчик использования промокода.
    Возвращает (True, "") если удалось, (False, ошибка) иначе
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Проверяем валидность промокода
            promo = await conn.fetchrow(
                "SELECT days, max_uses, used_count, active FROM promo_codes WHERE code = $1 FOR UPDATE",
                code
            )

            if not promo:
                return False, "Неверный промокод"

            if not promo['active']:
                return False, "Промокод неактивен"

            if promo['used_count'] >= promo['max_uses']:
                return False, "Промокод исчерпан"

            # Увеличиваем счётчик использования
            await conn.execute(
                "UPDATE promo_codes SET used_count = used_count + 1 WHERE code = $1",
                code
            )

            return True, ""


async def can_request_promo(tg_id: int) -> tuple[bool, str]:
    """
    Проверить может ли пользователь активировать промокод сейчас.
    Возвращает (True/False, сообщение об ошибке если есть)
    """
    from datetime import datetime, timezone, timedelta

    user = await get_user(tg_id)

    if not user:
        return False, "Пользователь не найден"

    # Проверяем anti-spam: не более одной попытки за PROMO_REQUEST_COOLDOWN секунд
    last_promo_attempt = user.get('last_promo_attempt')
    if last_promo_attempt:
        time_since_attempt = datetime.utcnow() - last_promo_attempt
        if time_since_attempt < timedelta(seconds=PROMO_REQUEST_COOLDOWN):
            return False, "Подожди пару секунд ⏳"

    return True, ""


async def update_last_promo_attempt(tg_id: int):
    """Обновить время последней попытки активировать промокод"""
    from datetime import datetime
    await db_execute(
        "UPDATE users SET last_promo_attempt = $1 WHERE tg_id = $2",
        (datetime.utcnow(), tg_id)
    )


async def can_check_payment(tg_id: int) -> tuple[bool, str]:
    """
    Проверить может ли пользователь проверить платёж сейчас.
    Возвращает (True/False, сообщение об ошибке если есть)
    """
    from datetime import datetime, timezone, timedelta

    user = await get_user(tg_id)

    if not user:
        return False, "Пользователь не найден"

    # Проверяем anti-spam: не более одной проверки за PAYMENT_CHECK_COOLDOWN секунд
    last_payment_check = user.get('last_payment_check')
    if last_payment_check:
        time_since_check = datetime.utcnow() - last_payment_check
        if time_since_check < timedelta(seconds=PAYMENT_CHECK_COOLDOWN):
            return False, "Подожди пару секунд ⏳"

    return True, ""


async def update_last_payment_check(tg_id: int):
    """Обновить время последней проверки платежа"""
    from datetime import datetime
    await db_execute(
        "UPDATE users SET last_payment_check = $1 WHERE tg_id = $2",
        (datetime.utcnow(), tg_id)
    )


# ────────────────────────────────────────────────
#                BALANCE MANAGEMENT
# ────────────────────────────────────────────────

async def get_balance(tg_id: int) -> float:
    """Получить баланс пользователя"""
    result = await db_execute(
        "SELECT balance FROM users WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )
    return float(result['balance']) if result else 0.0


async def add_balance(tg_id: int, amount: float) -> float:
    """Добавить деньги на баланс. Возвращает новый баланс"""
    await db_execute(
        "UPDATE users SET balance = balance + $1 WHERE tg_id = $2",
        (amount, tg_id)
    )
    return await get_balance(tg_id)


async def subtract_balance(tg_id: int, amount: float) -> bool:
    """Вычесть деньги со счёта. Возвращает True если успешно"""
    result = await db_execute(
        """
        UPDATE users
        SET balance = balance - $1
        WHERE tg_id = $2 AND balance >= $1
        RETURNING balance
        """,
        (amount, tg_id),
        fetch_one=True
    )
    return result is not None


async def get_user_balance_atomic(tg_id: int) -> float:
    """Получить баланс пользователя атомарно (для проверки перед покупкой)"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT balance FROM users WHERE tg_id = $1",
            tg_id
        )
        return float(result) if result else 0.0


# ────────────────────────────────────────────────
#             VIP SUBSCRIPTION MANAGEMENT
# ────────────────────────────────────────────────

async def update_vip_subscription(tg_id: int, xui_email: str, xui_uuid: str, subscription_id: str, vip_until: str):
    """Обновить VIP подписку пользователя"""
    await db_execute(
        """
        UPDATE users
        SET xui_email = $1,
            xui_uuid = $2,
            xui_subscription_id = $3,
            vip_subscription_until = $4
        WHERE tg_id = $5
        """,
        (xui_email, xui_uuid, subscription_id, vip_until, tg_id)
    )


async def has_vip_subscription(tg_id: int) -> bool:
    """Проверить есть ли активная VIP подписка"""
    from datetime import datetime, timezone
    user = await get_user(tg_id)
    if not user or not user['xui_uuid']:
        return False

    vip_until = user.get('vip_subscription_until')
    if not vip_until:
        return False

    from datetime import datetime, timezone
    if isinstance(vip_until, str):
        vip_dt = datetime.fromisoformat(vip_until.replace('Z', '+00:00'))
    else:
        vip_dt = vip_until.replace(tzinfo=timezone.utc)

    return vip_dt > datetime.now(timezone.utc)


async def get_vip_subscription_info(tg_id: int):
    """Получить информацию о VIP подписке"""
    user = await get_user(tg_id)
    if not user:
        return None

    return {
        'xui_email': user.get('xui_email'),
        'xui_uuid': user.get('xui_uuid'),
        'xui_subscription_id': user.get('xui_subscription_id'),
        'vip_subscription_until': user.get('vip_subscription_until')
    }


# ────────────────────────────────────────────────
#           REFERRAL COMMISSION MANAGEMENT
# ────────────────────────────────────────────────

async def add_referral_commission(tg_id: int, amount: float):
    """Добавить комиссию рефералу"""
    await db_execute(
        "UPDATE users SET referral_commission = referral_commission + $1 WHERE tg_id = $2",
        (amount, tg_id)
    )


async def get_referral_commission(tg_id: int) -> float:
    """Получить накопленную комиссию рефералу"""
    result = await db_execute(
        "SELECT referral_commission FROM users WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )
    return float(result['referral_commission']) if result else 0.0


async def withdraw_referral_commission(tg_id: int) -> float:
    """Снять накопленную комиссию на баланс"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            """
            UPDATE users
            SET balance = balance + referral_commission,
                referral_commission = 0
            WHERE tg_id = $1
            RETURNING balance, referral_commission
            """,
            tg_id
        )
        return float(result['balance']) if result else 0.0
