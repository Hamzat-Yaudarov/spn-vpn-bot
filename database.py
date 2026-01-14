import asyncpg
import logging
from config import DATABASE_URL


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

                    -- Условия и подписка
                    accepted_terms BOOLEAN DEFAULT FALSE,
                    remnawave_uuid UUID,
                    remnawave_username TEXT,
                    subscription_until TIMESTAMP,
                    squad_uuid UUID,

                    -- Реферальная программа
                    referrer_id BIGINT,
                    first_payment BOOLEAN DEFAULT FALSE,
                    referral_count INT DEFAULT 0,
                    active_referrals INT DEFAULT 0,

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


async def acquire_user_lock(tg_id: int, timeout: float = 5.0) -> bool:
    """
    Получить блокировку пользователя используя PostgreSQL advisory lock

    Args:
        tg_id: ID пользователя Telegram
        timeout: Таймаут ожидания блокировки в секундах

    Returns:
        True если удалось получить блокировку, False иначе
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            # Используем advisory lock с ID пользователя
            # pg_try_advisory_lock - неблокирующая версия
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


class UserLockContext:
    """Асинхронный context manager для блокировок пользователя с гарантией освобождения"""

    def __init__(self, tg_id: int, max_retries: int = 50, retry_delay: float = 0.1):
        """
        Args:
            tg_id: ID пользователя Telegram
            max_retries: Максимальное количество попыток получить блокировку
            retry_delay: Задержка между попытками в секундах
        """
        self.tg_id = tg_id
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.acquired = False

    async def __aenter__(self):
        """Получить блокировку с повторными попытками"""
        import asyncio

        for attempt in range(self.max_retries):
            if await acquire_user_lock(self.tg_id):
                self.acquired = True
                logging.debug(f"Lock acquired for user {self.tg_id}")
                return True

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_delay)

        logging.warning(f"Failed to acquire lock for user {self.tg_id} after {self.max_retries} retries")
        return False

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Освободить блокировку (гарантированно вызывается даже при исключениях)"""
        if self.acquired:
            try:
                await release_user_lock(self.tg_id)
                logging.debug(f"Lock released for user {self.tg_id}")
            except Exception as e:
                logging.error(f"Error releasing lock for user {self.tg_id}: {e}")

        # Не подавляем исключения
        return False


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
    from datetime import datetime, timezone
    await db_execute(
        """
        INSERT INTO payments (tg_id, tariff_code, amount, created_at, provider, invoice_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        (tg_id, tariff_code, amount, datetime.now(timezone.utc), provider, str(invoice_id))
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

    # Проверяем, не истёк ли счёт (старше 10 минут)
    created_at = result['created_at']
    age = datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)

    if age.total_seconds() > 600:  # 10 минут = 600 секунд
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


async def delete_expired_payments(minutes: int = 10):
    """
    Удалить все неоплаченные счёты старше N минут

    Args:
        minutes: Время в минутах (по умолчанию 10)
    """
    from datetime import datetime, timedelta, timezone

    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=minutes)

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

    # Проверяем anti-spam: не более одной попытки в 2 секунды
    last_gift_attempt = user.get('last_gift_attempt')
    if last_gift_attempt:
        time_since_attempt = datetime.now(timezone.utc) - last_gift_attempt.replace(tzinfo=timezone.utc)
        if time_since_attempt < timedelta(seconds=2):
            return False, "Подожди пару секунд ⏳"

    return True, ""


async def update_last_gift_attempt(tg_id: int):
    """Обновить время последней попытки получить подарок"""
    from datetime import datetime, timezone
    await db_execute(
        "UPDATE users SET last_gift_attempt = $1 WHERE tg_id = $2",
        (datetime.now(timezone.utc), tg_id)
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

    # Проверяем anti-spam: не более одной попытки в 1.5 секунды
    last_promo_attempt = user.get('last_promo_attempt')
    if last_promo_attempt:
        time_since_attempt = datetime.now(timezone.utc) - last_promo_attempt.replace(tzinfo=timezone.utc)
        if time_since_attempt < timedelta(seconds=1.5):
            return False, "Подожди пару секунд ⏳"

    return True, ""


async def update_last_promo_attempt(tg_id: int):
    """Обновить время последней попытки активировать промокод"""
    from datetime import datetime, timezone
    await db_execute(
        "UPDATE users SET last_promo_attempt = $1 WHERE tg_id = $2",
        (datetime.now(timezone.utc), tg_id)
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

    # Проверяем anti-spam: не более одной проверки в 1 секунду
    last_payment_check = user.get('last_payment_check')
    if last_payment_check:
        time_since_check = datetime.now(timezone.utc) - last_payment_check.replace(tzinfo=timezone.utc)
        if time_since_check < timedelta(seconds=1):
            return False, "Подожди пару секунд ⏳"

    return True, ""


async def update_last_payment_check(tg_id: int):
    """Обновить время последней проверки платежа"""
    from datetime import datetime, timezone
    await db_execute(
        "UPDATE users SET last_payment_check = $1 WHERE tg_id = $2",
        (datetime.now(timezone.utc), tg_id)
    )


# ────────────────────────────────────────────────
#           PAYMENT IDEMPOTENCY CHECK
# ────────────────────────────────────────────────

async def is_payment_already_processed(invoice_id: str) -> bool:
    """
    Проверить был ли платеж уже обработан (идемпотентность)

    Args:
        invoice_id: ID платежа

    Returns:
        True если платеж уже обработан (статус = 'paid'), False иначе
    """
    result = await db_execute(
        "SELECT status FROM payments WHERE invoice_id = $1",
        (invoice_id,),
        fetch_one=True
    )

    if result:
        return result['status'] == 'paid'
    return False


async def mark_payment_processed(invoice_id: str) -> bool:
    """
    Атомарно отметить платеж как обработанный (статус 'paid')
    Гарантирует что только один обработчик обработает платеж

    Args:
        invoice_id: ID платежа

    Returns:
        True если успешно отметили (платеж был 'pending'), False иначе
    """
    result = await db_execute(
        """
        UPDATE payments
        SET status = 'paid'
        WHERE invoice_id = $1 AND status = 'pending'
        RETURNING 1
        """,
        (invoice_id,),
        fetch_one=True
    )
    return result is not None


# ────────────────────────────────────────────────
#              STATISTICS & ANALYTICS
# ────────────────────────────────────────────────

async def get_overall_stats() -> dict | None:
    """
    Получить общую статистику бота

    Returns:
        Словарь со статистикой или None если ошибка
    """
    from datetime import datetime, timezone

    try:
        result = await db_execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) as total_users,
                (SELECT COUNT(*) FROM users WHERE subscription_until IS NOT NULL
                 AND subscription_until > $1) as active_subscriptions,
                (SELECT COUNT(*) FROM users WHERE accepted_terms = TRUE) as accepted_terms,
                (SELECT COUNT(*) FROM payments WHERE status = 'paid') as paid_payments,
                (SELECT COUNT(*) FROM payments WHERE status = 'pending') as pending_payments,
                (SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid') as total_revenue,
                (SELECT COUNT(*) FROM users WHERE gift_received = TRUE) as gifts_given,
                (SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL) as total_referrals,
                (SELECT COALESCE(SUM(active_referrals), 0) FROM users) as active_referrals,
                (SELECT COUNT(*) FROM promo_codes WHERE used_count > 0) as promos_used
            """,
            (datetime.now(timezone.utc),),
            fetch_one=True
        )

        if result:
            return {
                'total_users': result['total_users'] or 0,
                'active_subscriptions': result['active_subscriptions'] or 0,
                'accepted_terms': result['accepted_terms'] or 0,
                'paid_payments': result['paid_payments'] or 0,
                'pending_payments': result['pending_payments'] or 0,
                'total_revenue': float(result['total_revenue'] or 0),
                'gifts_given': result['gifts_given'] or 0,
                'total_referrals': result['total_referrals'] or 0,
                'active_referrals': result['active_referrals'] or 0,
                'promos_used': result['promos_used'] or 0,
            }
    except Exception as e:
        logging.error(f"Get overall stats error: {e}", exc_info=True)

    return None
