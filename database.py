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


async def get_table_columns(conn, table_name: str) -> dict:
    """Получить информацию о столбцах таблицы"""
    result = await conn.fetch("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = $1
        ORDER BY ordinal_position
    """, table_name)

    return {row['column_name']: {
        'type': row['data_type'],
        'nullable': row['is_nullable']
    } for row in result}


def normalize_pg_type(pg_type: str) -> str:
    """Нормализировать тип данных PostgreSQL для сравнения"""
    pg_type = pg_type.lower().strip()

    # Нормализируем варианты типов
    type_mapping = {
        'integer': 'integer',
        'int': 'integer',
        'int4': 'integer',
        'bigint': 'bigint',
        'int8': 'bigint',
        'text': 'text',
        'varchar': 'text',
        'boolean': 'boolean',
        'bool': 'boolean',
        'uuid': 'uuid',
        'numeric': 'numeric',
        'decimal': 'numeric',
        'timestamp': 'timestamp',
        'timestamp without time zone': 'timestamp',
        'timestamp with time zone': 'timestamp',
    }

    for key, normalized in type_mapping.items():
        if key in pg_type:
            return normalized

    return pg_type


async def sync_table_schema(conn, table_name: str, expected_columns: dict):
    """Синхронизировать схему таблицы: добавить недостающие, удалить лишние столбцы"""
    try:
        current_columns = await get_table_columns(conn, table_name)
    except Exception as e:
        logging.debug(f"Таблица {table_name} не существует или ошибка доступа: {e}")
        return

    # Добавляем недостающие столбцы
    for col_name, col_def in expected_columns.items():
        if col_name not in current_columns:
            data_type = col_def.get('type', 'TEXT')
            nullable = col_def.get('nullable', True)
            default = col_def.get('default', '')

            alter_query = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {data_type}"

            if default:
                alter_query += f" DEFAULT {default}"

            if not nullable:
                alter_query += " NOT NULL"

            try:
                await conn.execute(alter_query)
                logging.info(f"✅ Добавлен столбец {table_name}.{col_name}")
            except Exception as e:
                logging.warning(f"⚠️ Ошибка при добавлении столбца {table_name}.{col_name}: {e}")

    # Удаляем лишние столбцы (которые есть в таблице но не в expected_columns)
    system_columns = {'id', 'created_at', 'updated_at'}  # Системные столбцы не трогаем

    for col_name in current_columns:
        if col_name not in expected_columns and col_name not in system_columns:
            try:
                await conn.execute(f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS {col_name} CASCADE")
                logging.info(f"✅ Удалён лишний столбец {table_name}.{col_name}")
            except Exception as e:
                logging.warning(f"⚠️ Ошибка при удалении столбца {table_name}.{col_name}: {e}")


async def run_migrations():
    """Запустить автоматические миграции при старте бота"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            logging.info("Running migrations...")

            # ═══════════════════════════════════════════════════════════
            # ОПРЕДЕЛЯЕМ ОЖИДАЕМУЮ СТРУКТУРУ ТАБЛИЦ
            # ═══════════════════════════════════════════════════════════

            expected_users_columns = {
                'tg_id': {'type': 'BIGINT', 'nullable': False},
                'username': {'type': 'TEXT', 'nullable': True},
                'accepted_terms': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'remnawave_uuid': {'type': 'UUID', 'nullable': True},
                'remnawave_username': {'type': 'TEXT', 'nullable': True},
                'subscription_until': {'type': 'TIMESTAMP', 'nullable': True},
                'squad_uuid': {'type': 'UUID', 'nullable': True},
                'referrer_id': {'type': 'BIGINT', 'nullable': True},
                'first_payment': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'referral_count': {'type': 'INT', 'nullable': False, 'default': '0'},
                'active_referrals': {'type': 'INT', 'nullable': False, 'default': '0'},
                'gift_received': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'next_notification_time': {'type': 'TIMESTAMP', 'nullable': True},
                'notification_type': {'type': 'TEXT', 'nullable': True},
                'last_gift_attempt': {'type': 'TIMESTAMP', 'nullable': True},
                'last_promo_attempt': {'type': 'TIMESTAMP', 'nullable': True},
                'last_payment_check': {'type': 'TIMESTAMP', 'nullable': True},
            }

            expected_payments_columns = {
                'tg_id': {'type': 'BIGINT', 'nullable': False},
                'tariff_code': {'type': 'TEXT', 'nullable': False},
                'amount': {'type': 'NUMERIC', 'nullable': False},
                'provider': {'type': 'TEXT', 'nullable': False},
                'invoice_id': {'type': 'TEXT', 'nullable': False},
                'status': {'type': 'TEXT', 'nullable': False, 'default': "'pending'"},
            }

            expected_promo_columns = {
                'code': {'type': 'TEXT', 'nullable': False},
                'days': {'type': 'INT', 'nullable': False},
                'max_uses': {'type': 'INT', 'nullable': False},
                'used_count': {'type': 'INT', 'nullable': False, 'default': '0'},
                'active': {'type': 'BOOLEAN', 'nullable': False, 'default': 'TRUE'},
            }

            expected_promo_usage_columns = {
                'tg_id': {'type': 'BIGINT', 'nullable': False},
                'promo_code': {'type': 'TEXT', 'nullable': False},
            }

            expected_partnerships_columns = {
                'tg_id': {'type': 'BIGINT', 'nullable': False},
                'percentage': {'type': 'INT', 'nullable': False},
                'agreement_accepted': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'status': {'type': 'TEXT', 'nullable': False, 'default': "'active'"},
            }

            expected_partner_referrals_columns = {
                'partner_id': {'type': 'BIGINT', 'nullable': False},
                'referred_user_id': {'type': 'BIGINT', 'nullable': False},
            }

            expected_partner_earnings_columns = {
                'partner_id': {'type': 'BIGINT', 'nullable': False},
                'user_id': {'type': 'BIGINT', 'nullable': False},
                'tariff_code': {'type': 'TEXT', 'nullable': False},
                'amount': {'type': 'NUMERIC', 'nullable': False},
                'partner_share': {'type': 'NUMERIC', 'nullable': False},
            }

            expected_partner_withdrawals_columns = {
                'partner_id': {'type': 'BIGINT', 'nullable': False},
                'amount': {'type': 'NUMERIC', 'nullable': False},
                'withdrawal_type': {'type': 'TEXT', 'nullable': False},
                'bank_name': {'type': 'TEXT', 'nullable': True},
                'phone_number': {'type': 'TEXT', 'nullable': True},
                'usdt_address': {'type': 'TEXT', 'nullable': True},
                'status': {'type': 'TEXT', 'nullable': False, 'default': "'pending'"},
            }

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

                    -- Уведомления о подписке
                    next_notification_time TIMESTAMP,
                    notification_type TEXT,

                    -- Anti-spam тайм-стемпы
                    last_gift_attempt TIMESTAMP,
                    last_promo_attempt TIMESTAMP,
                    last_payment_check TIMESTAMP
                )
            """)
            logging.info("✅ Таблица 'users' создана или уже существует")

            # Таблица партнёрства
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS partnerships (
                    id BIGSERIAL PRIMARY KEY,
                    tg_id BIGINT UNIQUE NOT NULL,
                    percentage INT NOT NULL,
                    agreement_accepted BOOLEAN DEFAULT FALSE,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now()
                )
            """)
            logging.info("✅ Таблица 'partnerships' создана или уже существует")

            # Таблица партнёрских рефералов и покупок
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS partner_referrals (
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT NOT NULL,
                    referred_user_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT now(),
                    UNIQUE(partner_id, referred_user_id),
                    FOREIGN KEY (partner_id) REFERENCES partnerships(tg_id) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'partner_referrals' создана или уже существует")

            # Таблица партнёрских покупок и заработков
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS partner_earnings (
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    tariff_code TEXT NOT NULL,
                    amount NUMERIC NOT NULL,
                    partner_share NUMERIC NOT NULL,
                    created_at TIMESTAMP DEFAULT now(),
                    FOREIGN KEY (partner_id) REFERENCES partnerships(tg_id) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'partner_earnings' создана или уже существует")

            # Таблица запросов на вывод средств
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS partner_withdrawals (
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT NOT NULL,
                    amount NUMERIC NOT NULL,
                    withdrawal_type TEXT NOT NULL,
                    bank_name TEXT,
                    phone_number TEXT,
                    usdt_address TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT now(),
                    FOREIGN KEY (partner_id) REFERENCES partnerships(tg_id) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'partner_withdrawals' создана или уже существует")
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

            # Таблица использования промокодов пользователями
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS promo_code_users (
                    id BIGSERIAL PRIMARY KEY,
                    tg_id BIGINT NOT NULL,
                    promo_code TEXT NOT NULL,
                    used_at TIMESTAMP DEFAULT now(),
                    UNIQUE(tg_id, promo_code),
                    FOREIGN KEY (promo_code) REFERENCES promo_codes(code) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'promo_code_users' создана или уже существует")

            # ═══════════════════════════════════════════════════════════
            # ЭТАП 2: СОЗДАНИЕ ИНДЕКСОВ (для быстрого поиска)
            # ═══════════════════════════════════════════════════════════

            index_queries = [
                # users индексы
                "CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_users_remnawave_uuid ON users(remnawave_uuid);",
                "CREATE INDEX IF NOT EXISTS idx_users_referrer_id ON users(referrer_id);",
                "CREATE INDEX IF NOT EXISTS idx_users_next_notification ON users(next_notification_time) WHERE next_notification_time IS NOT NULL;",

                # payments индексы
                "CREATE INDEX IF NOT EXISTS idx_payments_tg_id ON payments(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);",
                "CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider);",
                "CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);",

                # promo_codes индексы
                "CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);",
                "CREATE INDEX IF NOT EXISTS idx_promo_code_users_tg_id ON promo_code_users(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_promo_code_users_code ON promo_code_users(promo_code);",

                # partnership индексы
                "CREATE INDEX IF NOT EXISTS idx_partnerships_tg_id ON partnerships(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_partner_referrals_partner_id ON partner_referrals(partner_id);",
                "CREATE INDEX IF NOT EXISTS idx_partner_earnings_partner_id ON partner_earnings(partner_id);",
                "CREATE INDEX IF NOT EXISTS idx_partner_withdrawals_partner_id ON partner_withdrawals(partner_id);",
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

            # ═══════════════════════════════════════════════════════════
            # ЭТАП 3.5: ОЧИСТКА ДУБЛИКАТОВ И ДОБАВЛЕНИЕ CONSTRAINTS
            # ═══════════════════════════════════════════════════════════

            try:
                # Удаляем дубликаты в partner_referrals (оставляем только первый)
                await conn.execute("""
                    DELETE FROM partner_referrals WHERE id NOT IN (
                        SELECT MIN(id) FROM partner_referrals
                        GROUP BY partner_id, referred_user_id
                    )
                """)
                logging.info("✅ Удалены дубликаты в таблице partner_referrals")
            except Exception as e:
                logging.debug(f"Дубликатов не найдено или уже удалены: {e}")

            try:
                # Добавляем UNIQUE constraint если его ещё нет
                await conn.execute("""
                    ALTER TABLE partner_referrals
                    ADD CONSTRAINT unique_partner_referral UNIQUE (partner_id, referred_user_id)
                """)
                logging.info("✅ Добавлено UNIQUE ограничение на partner_referrals (partner_id, referred_user_id)")
            except Exception as e:
                if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                    logging.debug("UNIQUE ограничение уже существует")
                else:
                    logging.debug(f"Примечание по UNIQUE: {e}")

            # ═══════════════════════════════════════════════════════════
            # ЭТАП 4: СИНХРОНИЗАЦИЯ СХЕМЫ ТАБЛИЦ
            # ═══════════════════════════════════════════════════════════

            logging.info("Syncing table schemas...")

            # Синхронизируем таблицы
            await sync_table_schema(conn, 'users', expected_users_columns)
            await sync_table_schema(conn, 'payments', expected_payments_columns)
            await sync_table_schema(conn, 'promo_codes', expected_promo_columns)
            await sync_table_schema(conn, 'promo_code_users', expected_promo_usage_columns)
            await sync_table_schema(conn, 'partnerships', expected_partnerships_columns)
            await sync_table_schema(conn, 'partner_referrals', expected_partner_referrals_columns)
            await sync_table_schema(conn, 'partner_earnings', expected_partner_earnings_columns)
            await sync_table_schema(conn, 'partner_withdrawals', expected_partner_withdrawals_columns)

            logging.info("✅ Синхронизация схемы завершена")

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

async def update_subscription(tg_id: int, uuid: str, username: str, subscription_until, squad_uuid: str):
    """
    Обновить подписку пользователя

    Также автоматически устанавливает время для уведомления о заканчивающейся подписке
    (1.5 дня до окончания). Если это время в прошлом, уведомление рассчитывается корректно
    """
    from datetime import datetime, timedelta

    # Рассчитываем время следующего уведомления (1.5 дня до окончания подписки)
    if subscription_until:
        now = datetime.utcnow()
        next_notification = subscription_until - timedelta(days=1.5)

        # Если следующее уведомление уже в прошлом, установим его на более раннее время
        if next_notification <= now:
            # Проверяем сколько времени осталось до конца подписки
            time_left = (subscription_until - now).total_seconds()

            if time_left > 86400:  # Больше 1 дня
                # Установим уведомление на 1 день до конца
                next_notification = subscription_until - timedelta(days=1)
                notification_type = "below1day"
            elif time_left > 0:  # Менее 1 дня но подписка ещё активна
                # Установим уведомление на конец подписки
                next_notification = subscription_until
                notification_type = "expired"
            else:
                # Подписка уже истекла
                next_notification = None
                notification_type = None
        else:
            notification_type = "1day_left"
    else:
        next_notification = None
        notification_type = None

    await db_execute(
        """
        UPDATE users
        SET remnawave_uuid = $1,
            remnawave_username = $2,
            subscription_until = $3,
            squad_uuid = $4,
            next_notification_time = $6,
            notification_type = $7
        WHERE tg_id = $5
        """,
        (uuid, username, subscription_until, squad_uuid, tg_id, next_notification, notification_type)
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


async def increment_promo_usage_atomic(code: str, tg_id: int) -> tuple[bool, str]:
    """
    Атомарно проверить и увеличить счётчик использования промокода.
    Проверяет что:
    1. Промокод существует и активен
    2. Не исчерпан лимит использований
    3. Пользователь не использовал этот промокод раньше

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

            # Проверяем что пользователь не использовал этот промокод раньше
            existing_usage = await conn.fetchval(
                "SELECT 1 FROM promo_code_users WHERE tg_id = $1 AND promo_code = $2",
                tg_id, code
            )

            if existing_usage is not None:
                return False, "Ты уже использовал этот промокод"

            # Увеличиваем счётчик использования промокода
            await conn.execute(
                "UPDATE promo_codes SET used_count = used_count + 1 WHERE code = $1",
                code
            )

            # Записываем что пользователь использовал этот промокод
            await conn.execute(
                "INSERT INTO promo_code_users (tg_id, promo_code) VALUES ($1, $2)",
                tg_id, code
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
#        SUBSCRIPTION NOTIFICATION MANAGEMENT
# ────────────────────────────────────────────────

async def get_users_needing_notification():
    """
    Получить пользователей которым нужно отправить уведомление о заканчивающейся подписке

    Returns:
        Список пользователей у которых next_notification_time <= now и есть активная подписка
    """
    from datetime import datetime

    now = datetime.utcnow()

    # Сначала получаем всех с установленным временем уведомления для логирования
    all_with_notifications = await db_execute(
        """
        SELECT tg_id, next_notification_time, subscription_until, notification_type
        FROM users
        WHERE next_notification_time IS NOT NULL
        AND subscription_until IS NOT NULL
        ORDER BY next_notification_time ASC
        """,
        fetch_all=True
    )

    if all_with_notifications:
        logging.debug(f"Total users with notifications set: {len(all_with_notifications)}")
        for user in all_with_notifications[:5]:  # Показываем первых 5
            logging.debug(f"  User {user['tg_id']}: notification_time={user['next_notification_time']}, subscription_until={user['subscription_until']}, type={user['notification_type']}")

    # Теперь получаем только тех кому нужно отправить уведомление
    result = await db_execute(
        """
        SELECT tg_id, remnawave_uuid, subscription_until, notification_type
        FROM users
        WHERE next_notification_time IS NOT NULL
        AND next_notification_time <= $1
        AND subscription_until IS NOT NULL
        ORDER BY next_notification_time ASC
        """,
        (now,),
        fetch_all=True
    )

    if result:
        logging.info(f"Users needing notification (now={now}): {len(result)}")

    return result


async def mark_notification_sent(tg_id: int):
    """Отметить что уведомление было отправлено пользователю"""
    from datetime import datetime, timedelta

    # Получаем текущую подписку пользователя
    user = await get_user(tg_id)
    if not user or not user.get('subscription_until'):
        # Если подписки нет, очищаем поле уведомления
        logging.info(f"No subscription for user {tg_id}, clearing notifications")
        await db_execute(
            """
            UPDATE users
            SET next_notification_time = NULL, notification_type = NULL
            WHERE tg_id = $1
            """,
            (tg_id,)
        )
        return

    subscription_until = user['subscription_until']
    now = datetime.utcnow()
    time_until_expiry = (subscription_until - now).total_seconds()

    # Определяем следующее уведомление в зависимости от текущего типа
    current_type = user.get('notification_type')
    logging.info(f"User {tg_id} notification sent, current_type={current_type}, subscription_until={subscription_until}, time_until_expiry_seconds={time_until_expiry}")

    next_notification = None
    next_type = None

    if current_type == "1day_left":
        # Последнее уведомление было "1.5 дня осталось"
        # Теперь проверяем, осталось ли до 1 дня
        if time_until_expiry > 86400:  # Более 1 дня (86400 секунд)
            # Ещё много времени, установим следующее уведомление на 1 день до конца
            next_notification = subscription_until - timedelta(days=1)
            next_type = "below1day"
        else:
            # Уже менее 1 дня, установим следующее на конец подписки
            next_notification = subscription_until
            next_type = "expired"

    elif current_type == "below1day":
        # Последнее уведомление было "менее 1 дня осталось"
        # Следующее будет уведомление об истечении подписки
        if time_until_expiry > 0:
            # Подписка ещё активна, установим следующее на конец
            next_notification = subscription_until
            next_type = "expired"
        else:
            # Подписка уже истекла, очищаем
            next_notification = None
            next_type = None

    elif current_type == "expired":
        # Уведомление об истечении было отправлено
        # Очищаем уведомления если подписка активна (вероятно была продлена)
        next_notification = None
        next_type = None

    else:
        # Если это первое уведомление или что-то странное, просто очищаем
        next_notification = None
        next_type = None

    logging.info(f"User {tg_id} next notification will be at {next_notification}, type={next_type}")

    await db_execute(
        """
        UPDATE users
        SET next_notification_time = $1, notification_type = $2
        WHERE tg_id = $3
        """,
        (next_notification, next_type, tg_id)
    )


async def clear_notification(tg_id: int):
    """Очистить уведомление для пользователя"""
    await db_execute(
        """
        UPDATE users
        SET next_notification_time = NULL, notification_type = NULL
        WHERE tg_id = $1
        """,
        (tg_id,)
    )


# ────────────────────────────────────────────────
#             PARTNERSHIP MANAGEMENT
# ────────────────────────────────────────────────

async def create_partnership(tg_id: int, percentage: int):
    """Создать партнёрство для пользователя"""
    await db_execute(
        """
        INSERT INTO partnerships (tg_id, percentage, status)
        VALUES ($1, $2, 'active')
        ON CONFLICT (tg_id) DO UPDATE
        SET percentage = $2, status = 'active'
        """,
        (tg_id, percentage)
    )


async def get_partnership(tg_id: int):
    """Получить информацию о партнёрстве"""
    return await db_execute(
        "SELECT * FROM partnerships WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )


async def is_partner(tg_id: int) -> bool:
    """Проверить является ли пользователь партнёром"""
    try:
        result = await db_execute(
            "SELECT 1 FROM partnerships WHERE tg_id = $1",
            (tg_id,),
            fetch_one=True
        )
        return result is not None
    except Exception as e:
        logging.debug(f"Error checking partnership status for {tg_id}: {e}")
        return False


async def accept_partnership_agreement(tg_id: int):
    """Отметить что партнёр принял соглашение"""
    await db_execute(
        "UPDATE partnerships SET agreement_accepted = TRUE WHERE tg_id = $1",
        (tg_id,)
    )


async def has_accepted_partnership_agreement(tg_id: int) -> bool:
    """Проверить принял ли партнёр соглашение"""
    result = await db_execute(
        "SELECT agreement_accepted FROM partnerships WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )
    return result and result['agreement_accepted']


async def get_partner_for_user(referred_user_id: int) -> int | None:
    """Получить партнёра для пользователя (если уже засчитан)"""
    result = await db_execute(
        "SELECT partner_id FROM partner_referrals WHERE referred_user_id = $1 LIMIT 1",
        (referred_user_id,),
        fetch_one=True
    )
    return result['partner_id'] if result else None


async def add_partner_referral(partner_id: int, referred_user_id: int) -> bool:
    """
    Добавить партнёрского реферала с проверками

    Проверки:
    1. Партнёр не может быть рефералом себе
    2. Пользователь не может быть рефералом нескольких партнёров
    3. Пользователь должен быть НОВЫМ (впервые активирует бота)

    Returns:
        True если реферал добавлен, False если уже был или ошибка
    """
    # Проверка 1: партнёр не может быть рефералом себе
    if partner_id == referred_user_id:
        logging.warning(f"Partner {partner_id} tried to refer themselves")
        return False

    # Проверка 2: пользователь уже может быть рефералом другого партнёра
    existing_partner = await get_partner_for_user(referred_user_id)
    if existing_partner is not None:
        logging.warning(f"User {referred_user_id} is already a referral for partner {existing_partner}, skipping partner {partner_id}")
        return False

    # Проверка 3: пользователь должен быть НОВЫМ (впервые заходит в бота)
    user_was_found = await user_exists(referred_user_id)
    if user_was_found:
        logging.warning(f"User {referred_user_id} is not new (already visited bot before), cannot assign partner referral for partner {partner_id}")
        return False

    # Добавляем реферала (дубликаты одной пары игнорируются благодаря UNIQUE constraint)
    try:
        await db_execute(
            """
            INSERT INTO partner_referrals (partner_id, referred_user_id)
            VALUES ($1, $2)
            ON CONFLICT (partner_id, referred_user_id) DO NOTHING
            """,
            (partner_id, referred_user_id)
        )
        return True
    except Exception as e:
        logging.error(f"Error adding partner referral: {e}")
        return False


async def get_partner_referral_count(partner_id: int) -> int:
    """Получить количество партнёрских рефералов"""
    result = await db_execute(
        "SELECT COUNT(*) as count FROM partner_referrals WHERE partner_id = $1",
        (partner_id,),
        fetch_one=True
    )
    return result['count'] if result else 0


async def add_partner_earning(partner_id: int, user_id: int, tariff_code: str, amount: float, percentage: int):
    """Записать партнёрский доход"""
    partner_share = amount * percentage / 100
    await db_execute(
        """
        INSERT INTO partner_earnings (partner_id, user_id, tariff_code, amount, partner_share)
        VALUES ($1, $2, $3, $4, $5)
        """,
        (partner_id, user_id, tariff_code, amount, partner_share)
    )


async def get_partner_stats(partner_id: int):
    """Получить полную статистику партнёра"""
    partnership = await get_partnership(partner_id)
    if not partnership:
        return None

    # Всего рефералов
    total_referrals = await db_execute(
        "SELECT COUNT(DISTINCT referred_user_id) as count FROM partner_referrals WHERE partner_id = $1",
        (partner_id,),
        fetch_one=True
    )

    # Заработок по тарифам
    earnings = await db_execute(
        """
        SELECT
            tariff_code,
            COUNT(*) as purchase_count,
            SUM(partner_share) as total_share
        FROM partner_earnings
        WHERE partner_id = $1
        GROUP BY tariff_code
        """,
        (partner_id,),
        fetch_all=True
    )

    # Общий заработок
    total_earned = await db_execute(
        "SELECT SUM(partner_share) as total FROM partner_earnings WHERE partner_id = $1",
        (partner_id,),
        fetch_one=True
    )

    # Всего выведено
    total_withdrawn = await db_execute(
        "SELECT SUM(amount) as total FROM partner_withdrawals WHERE partner_id = $1 AND status = 'completed'",
        (partner_id,),
        fetch_one=True
    )

    # Текущий баланс = заработано - выведено
    earned = float(total_earned['total'] or 0)
    withdrawn = float(total_withdrawn['total'] or 0)
    balance = earned - withdrawn

    return {
        'percentage': partnership['percentage'],
        'total_referrals': total_referrals['count'] if total_referrals else 0,
        'earnings_by_tariff': earnings or [],
        'total_earned': earned,
        'total_withdrawn': withdrawn,
        'current_balance': balance
    }


async def create_withdrawal_request(partner_id: int, amount: float, withdrawal_type: str, **kwargs):
    """Создать запрос на вывод средств"""
    bank_name = kwargs.get('bank_name')
    phone_number = kwargs.get('phone_number')
    usdt_address = kwargs.get('usdt_address')

    await db_execute(
        """
        INSERT INTO partner_withdrawals (partner_id, amount, withdrawal_type, bank_name, phone_number, usdt_address)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        (partner_id, amount, withdrawal_type, bank_name, phone_number, usdt_address)
    )


async def get_pending_withdrawals():
    """Получить все ожидающие запросы на вывод"""
    return await db_execute(
        """
        SELECT
            pd.id,
            pd.partner_id,
            pd.amount,
            pd.withdrawal_type,
            pd.bank_name,
            pd.phone_number,
            pd.usdt_address,
            u.username
        FROM partner_withdrawals pd
        JOIN partnerships p ON pd.partner_id = p.tg_id
        LEFT JOIN users u ON p.tg_id = u.tg_id
        WHERE pd.status = 'pending'
        ORDER BY pd.created_at ASC
        """,
        fetch_all=True
    )


async def mark_withdrawal_completed(withdrawal_id: int):
    """Отметить вывод как выполненный"""
    await db_execute(
        "UPDATE partner_withdrawals SET status = 'completed' WHERE id = $1",
        (withdrawal_id,)
    )
