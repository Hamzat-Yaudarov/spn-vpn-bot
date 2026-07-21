import asyncio
import asyncpg
import logging
from datetime import datetime
from config import (
    DATABASE_URL,
    PAYMENT_EXPIRY_TIME,
    TRACKING_ATTRIBUTION_DAYS,
    GIFT_REQUEST_COOLDOWN,
    PROMO_REQUEST_COOLDOWN,
    PAYMENT_CHECK_COOLDOWN,
    BYPASS_BASE_TRAFFIC_GB,
    BYPASS_HWID_DEVICE_LIMIT,
    GB_BYTES,
    REGULAR_HWID_DEVICE_LIMIT,
)


MAX_SUBSCRIPTIONS_PER_USER = 5


# Глобальный пул подключений
_pool = None

# Блокировки пользователя на уровне процесса.
# Бот, webhook-сервер и фоновые задачи работают в одном процессе,
# поэтому asyncio.Lock надёжнее, чем advisory lock через разные соединения пула.
_user_locks: dict[int, asyncio.Lock] = {}
_user_locks_guard = asyncio.Lock()


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
                'tracking_code': {'type': 'TEXT', 'nullable': True},
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
                'subscription_id': {'type': 'BIGINT', 'nullable': True},
                'payment_target': {'type': 'TEXT', 'nullable': False, 'default': "'new'"},
                'target_slot_number': {'type': 'INT', 'nullable': True},
                'payment_kind': {'type': 'TEXT', 'nullable': False, 'default': "'subscription'"},
                'traffic_package_code': {'type': 'TEXT', 'nullable': True},
                'tracking_code': {'type': 'TEXT', 'nullable': True},
                'refund_requested_at': {'type': 'TIMESTAMP', 'nullable': True},
                'refund_status': {'type': 'TEXT', 'nullable': True},
                'status': {'type': 'TEXT', 'nullable': False, 'default': "'pending'"},
            }

            expected_subscriptions_columns = {
                'tg_id': {'type': 'BIGINT', 'nullable': False},
                'slot_number': {'type': 'INT', 'nullable': False},
                'remnawave_uuid': {'type': 'UUID', 'nullable': True},
                'remnawave_username': {'type': 'TEXT', 'nullable': True},
                'subscription_until': {'type': 'TIMESTAMP', 'nullable': True},
                'squad_uuid': {'type': 'UUID', 'nullable': True},
                'is_active': {'type': 'BOOLEAN', 'nullable': False, 'default': 'TRUE'},
                'plan_kind': {'type': 'TEXT', 'nullable': True},
                'generation': {'type': 'TEXT', 'nullable': False, 'default': "'legacy'"},
                'is_visible': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'is_renewable': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'type_index': {'type': 'INT', 'nullable': True},
                'purchase_days': {'type': 'INT', 'nullable': True},
                'traffic_enabled': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'base_traffic_bytes': {'type': 'BIGINT', 'nullable': False, 'default': '0'},
                'current_paid_traffic_bytes': {'type': 'BIGINT', 'nullable': False, 'default': '0'},
                'carried_traffic_bytes': {'type': 'BIGINT', 'nullable': False, 'default': '0'},
                'current_period_limit_bytes': {'type': 'BIGINT', 'nullable': False, 'default': '0'},
                'traffic_reset_at': {'type': 'TIMESTAMP', 'nullable': True},
                'last_known_used_traffic_bytes': {'type': 'BIGINT', 'nullable': False, 'default': '0'},
                'last_traffic_sync_at': {'type': 'TIMESTAMP', 'nullable': True},
                'legacy_readonly': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'legacy_limit_removal_pending': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'hwid_device_limit': {'type': 'INT', 'nullable': False, 'default': '5'},
                'next_notification_time': {'type': 'TIMESTAMP', 'nullable': True},
                'notification_type': {'type': 'TEXT', 'nullable': True},
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

            expected_discounts_columns = {
                'name': {'type': 'TEXT', 'nullable': False},
                'discount_type': {'type': 'TEXT', 'nullable': False},
                'value': {'type': 'NUMERIC', 'nullable': False},
                'target_type': {'type': 'TEXT', 'nullable': False},
                'target_code': {'type': 'TEXT', 'nullable': True},
                'starts_at': {'type': 'TIMESTAMP', 'nullable': False},
                'ends_at': {'type': 'TIMESTAMP', 'nullable': False},
                'active': {'type': 'BOOLEAN', 'nullable': False, 'default': 'TRUE'},
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

            expected_referral_earnings_columns = {
                'referrer_id': {'type': 'BIGINT', 'nullable': False},
                'referred_user_id': {'type': 'BIGINT', 'nullable': False},
                'tariff_code': {'type': 'TEXT', 'nullable': False},
                'amount': {'type': 'NUMERIC', 'nullable': False},
                'referral_share': {'type': 'NUMERIC', 'nullable': False},
                'is_first_purchase': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
            }

            expected_referral_withdrawals_columns = {
                'referrer_id': {'type': 'BIGINT', 'nullable': False},
                'amount': {'type': 'NUMERIC', 'nullable': False},
                'withdrawal_type': {'type': 'TEXT', 'nullable': False},
                'bank_name': {'type': 'TEXT', 'nullable': True},
                'phone_number': {'type': 'TEXT', 'nullable': True},
                'usdt_address': {'type': 'TEXT', 'nullable': True},
                'status': {'type': 'TEXT', 'nullable': False, 'default': "'pending'"},
            }

            expected_traffic_purchases_columns = {
                'subscription_id': {'type': 'BIGINT', 'nullable': False},
                'package_code': {'type': 'TEXT', 'nullable': False},
                'traffic_bytes': {'type': 'BIGINT', 'nullable': False},
                'amount': {'type': 'NUMERIC', 'nullable': False},
                'provider': {'type': 'TEXT', 'nullable': True},
                'invoice_id': {'type': 'TEXT', 'nullable': True},
                'status': {'type': 'TEXT', 'nullable': False, 'default': "'pending'"},
                'activated_at': {'type': 'TIMESTAMP', 'nullable': True},
            }

            expected_device_addon_purchases_columns = {
                'subscription_id': {'type': 'BIGINT', 'nullable': False},
                'device_count': {'type': 'INT', 'nullable': False},
                'amount': {'type': 'NUMERIC', 'nullable': False},
                'provider': {'type': 'TEXT', 'nullable': True},
                'invoice_id': {'type': 'TEXT', 'nullable': True},
                'valid_until': {'type': 'TIMESTAMP', 'nullable': False},
                'status': {'type': 'TEXT', 'nullable': False, 'default': "'pending'"},
                'activated_at': {'type': 'TIMESTAMP', 'nullable': True},
                'expired_processed_at': {'type': 'TIMESTAMP', 'nullable': True},
            }

            expected_traffic_cycles_columns = {
                'subscription_id': {'type': 'BIGINT', 'nullable': False},
                'period_start': {'type': 'TIMESTAMP', 'nullable': False},
                'period_end': {'type': 'TIMESTAMP', 'nullable': False},
                'base_traffic_bytes': {'type': 'BIGINT', 'nullable': False},
                'carried_traffic_bytes': {'type': 'BIGINT', 'nullable': False},
                'paid_traffic_bytes': {'type': 'BIGINT', 'nullable': False},
                'used_traffic_bytes_before_reset': {'type': 'BIGINT', 'nullable': False},
                'remaining_paid_traffic_bytes': {'type': 'BIGINT', 'nullable': False},
                'reset_processed_at': {'type': 'TIMESTAMP', 'nullable': True},
            }

            expected_tracking_links_columns = {
                'code': {'type': 'TEXT', 'nullable': False},
                'title': {'type': 'TEXT', 'nullable': True},
                'created_by': {'type': 'BIGINT', 'nullable': False},
                'is_active': {'type': 'BOOLEAN', 'nullable': False, 'default': 'TRUE'},
            }

            expected_tracking_clicks_columns = {
                'code': {'type': 'TEXT', 'nullable': False},
                'tg_id': {'type': 'BIGINT', 'nullable': False},
                'is_new_user': {'type': 'BOOLEAN', 'nullable': False, 'default': 'FALSE'},
                'clicked_at': {'type': 'TIMESTAMP', 'nullable': True, 'default': 'now()'},
            }

            expected_notification_state_columns = {
                'tg_id': {'type': 'BIGINT', 'nullable': False},
                'subscription_id': {'type': 'BIGINT', 'nullable': False, 'default': '0'},
                'notification_type': {'type': 'TEXT', 'nullable': False},
                'last_sent_at': {'type': 'TIMESTAMP', 'nullable': False, 'default': 'now()'},
                'created_at': {'type': 'TIMESTAMP', 'nullable': True, 'default': 'now()'},
                'updated_at': {'type': 'TIMESTAMP', 'nullable': True, 'default': 'now()'},
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
                    tracking_code TEXT,
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

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS web_accounts (
                    id BIGSERIAL PRIMARY KEY,
                    login TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    service_user_id BIGINT UNIQUE,
                    tracking_code TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now(),
                    last_login_at TIMESTAMP
                )
            """)
            await conn.execute("ALTER TABLE web_accounts ADD COLUMN IF NOT EXISTS tracking_code TEXT")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id BIGSERIAL PRIMARY KEY,
                    account_id BIGINT NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
                    token_hash TEXT UNIQUE NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT now(),
                    last_seen_at TIMESTAMP DEFAULT now()
                )
            """)
            logging.info("✅ Таблицы веб-аккаунтов созданы или уже существуют")

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

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id BIGSERIAL PRIMARY KEY,
                    tg_id BIGINT NOT NULL,
                    slot_number INT NOT NULL,
                    remnawave_uuid UUID,
                    remnawave_username TEXT,
                    subscription_until TIMESTAMP,
                    squad_uuid UUID,
                    is_active BOOLEAN DEFAULT TRUE,
                    plan_kind TEXT,
                    generation TEXT DEFAULT 'legacy',
                    is_visible BOOLEAN DEFAULT FALSE,
                    is_renewable BOOLEAN DEFAULT FALSE,
                    type_index INT,
                    purchase_days INT,
                    traffic_enabled BOOLEAN DEFAULT FALSE,
                    base_traffic_bytes BIGINT DEFAULT 0,
                    current_paid_traffic_bytes BIGINT DEFAULT 0,
                    carried_traffic_bytes BIGINT DEFAULT 0,
                    current_period_limit_bytes BIGINT DEFAULT 0,
                    traffic_reset_at TIMESTAMP,
                    last_known_used_traffic_bytes BIGINT DEFAULT 0,
                    last_traffic_sync_at TIMESTAMP,
                    legacy_readonly BOOLEAN DEFAULT FALSE,
                    legacy_limit_removal_pending BOOLEAN DEFAULT FALSE,
                    hwid_device_limit INT DEFAULT 5,
                    next_notification_time TIMESTAMP,
                    notification_type TEXT,
                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now(),
                    UNIQUE(tg_id, slot_number)
                )
            """)
            logging.info("✅ Таблица 'subscriptions' создана или уже существует")

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

            # Таблица реферальных заработков
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS referral_earnings (
                    id BIGSERIAL PRIMARY KEY,
                    referrer_id BIGINT NOT NULL,
                    referred_user_id BIGINT NOT NULL,
                    tariff_code TEXT NOT NULL,
                    amount NUMERIC NOT NULL,
                    referral_share NUMERIC NOT NULL,
                    is_first_purchase BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT now(),
                    FOREIGN KEY (referrer_id) REFERENCES users(tg_id) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'referral_earnings' создана или уже существует")

            # Таблица запросов на вывод средств рефералами
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS referral_withdrawals (
                    id BIGSERIAL PRIMARY KEY,
                    referrer_id BIGINT NOT NULL,
                    amount NUMERIC NOT NULL,
                    withdrawal_type TEXT NOT NULL,
                    bank_name TEXT,
                    phone_number TEXT,
                    usdt_address TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT now(),
                    FOREIGN KEY (referrer_id) REFERENCES users(tg_id) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'referral_withdrawals' создана или уже существует")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS traffic_purchases (
                    id BIGSERIAL PRIMARY KEY,
                    subscription_id BIGINT NOT NULL,
                    package_code TEXT NOT NULL,
                    traffic_bytes BIGINT NOT NULL,
                    amount NUMERIC NOT NULL,
                    provider TEXT,
                    invoice_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT now(),
                    activated_at TIMESTAMP
                )
            """)
            logging.info("✅ Таблица 'traffic_purchases' создана или уже существует")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS device_addon_purchases (
                    id BIGSERIAL PRIMARY KEY,
                    subscription_id BIGINT NOT NULL,
                    device_count INT NOT NULL,
                    amount NUMERIC NOT NULL,
                    provider TEXT,
                    invoice_id TEXT,
                    valid_until TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT now(),
                    activated_at TIMESTAMP,
                    expired_processed_at TIMESTAMP
                )
            """)
            logging.info("✅ Таблица 'device_addon_purchases' создана или уже существует")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS subscription_traffic_cycles (
                    id BIGSERIAL PRIMARY KEY,
                    subscription_id BIGINT NOT NULL,
                    period_start TIMESTAMP NOT NULL,
                    period_end TIMESTAMP NOT NULL,
                    base_traffic_bytes BIGINT NOT NULL,
                    carried_traffic_bytes BIGINT NOT NULL,
                    paid_traffic_bytes BIGINT NOT NULL,
                    used_traffic_bytes_before_reset BIGINT NOT NULL,
                    remaining_paid_traffic_bytes BIGINT NOT NULL,
                    reset_processed_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT now()
                )
            """)
            logging.info("✅ Таблица 'subscription_traffic_cycles' создана или уже существует")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tracking_links (
                    id BIGSERIAL PRIMARY KEY,
                    code TEXT UNIQUE NOT NULL,
                    title TEXT,
                    created_by BIGINT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now()
                )
            """)
            logging.info("✅ Таблица 'tracking_links' создана или уже существует")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tracking_link_clicks (
                    id BIGSERIAL PRIMARY KEY,
                    code TEXT NOT NULL,
                    tg_id BIGINT NOT NULL,
                    is_new_user BOOLEAN DEFAULT FALSE,
                    clicked_at TIMESTAMP DEFAULT now()
                )
            """)
            logging.info("✅ Таблица 'tracking_link_clicks' создана или уже существует")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_state (
                    id BIGSERIAL PRIMARY KEY,
                    tg_id BIGINT NOT NULL,
                    subscription_id BIGINT DEFAULT 0 NOT NULL,
                    notification_type TEXT NOT NULL,
                    last_sent_at TIMESTAMP DEFAULT now() NOT NULL,
                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now(),
                    UNIQUE(tg_id, subscription_id, notification_type)
                )
            """)
            logging.info("✅ Таблица 'notification_state' создана или уже существует")

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
                    subscription_id BIGINT,
                    payment_target TEXT DEFAULT 'new',
                    target_slot_number INT,
                    payment_kind TEXT DEFAULT 'subscription',
                    traffic_package_code TEXT,
                    tracking_code TEXT,
                    refund_requested_at TIMESTAMP,
                    refund_status TEXT,
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

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS discounts (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    discount_type TEXT NOT NULL,
                    value NUMERIC NOT NULL,
                    target_type TEXT NOT NULL,
                    target_code TEXT,
                    starts_at TIMESTAMP NOT NULL,
                    ends_at TIMESTAMP NOT NULL,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now()
                )
            """)
            logging.info("✅ Таблица 'discounts' создана или уже существует")

            # Одноразовые Telegram-challenge и мобильные сессии Way VPN.
            # Секреты сохраняются только как SHA-256 хэши.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mobile_auth_challenges (
                    id UUID PRIMARY KEY,
                    start_token_hash TEXT UNIQUE NOT NULL,
                    code_challenge TEXT NOT NULL,
                    device_name TEXT,
                    candidate_tg_id BIGINT,
                    approved_tg_id BIGINT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT now(),
                    approved_at TIMESTAMP,
                    consumed_at TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mobile_sessions (
                    id UUID PRIMARY KEY,
                    tg_id BIGINT NOT NULL,
                    device_name TEXT,
                    access_token_hash TEXT UNIQUE NOT NULL,
                    access_expires_at TIMESTAMP NOT NULL,
                    refresh_token_hash TEXT UNIQUE NOT NULL,
                    refresh_expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT now(),
                    updated_at TIMESTAMP NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMP NOT NULL DEFAULT now(),
                    revoked_at TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mobile_access_keys (
                    id UUID PRIMARY KEY,
                    tg_id BIGINT UNIQUE NOT NULL,
                    key_hash TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT now(),
                    last_used_at TIMESTAMP,
                    revoked_at TIMESTAMP
                )
            """)
            logging.info("✅ Таблицы мобильной авторизации созданы или уже существуют")

            # ═══════════════════════════════════════════════════════════
            # ЭТАП 2: СОЗДАНИЕ ИНДЕКСОВ (для быстрого поиска)
            # ═══════════════════════════════════════════════════════════

            index_queries = [
                # users индексы
                "CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_users_remnawave_uuid ON users(remnawave_uuid);",
                "CREATE INDEX IF NOT EXISTS idx_users_referrer_id ON users(referrer_id);",
                "CREATE INDEX IF NOT EXISTS idx_users_tracking_code ON users(tracking_code);",
                "CREATE INDEX IF NOT EXISTS idx_users_next_notification ON users(next_notification_time) WHERE next_notification_time IS NOT NULL;",
                "CREATE INDEX IF NOT EXISTS idx_web_accounts_login ON web_accounts(login);",
                "CREATE INDEX IF NOT EXISTS idx_web_accounts_service_user ON web_accounts(service_user_id);",
                "CREATE INDEX IF NOT EXISTS idx_web_accounts_tracking_code ON web_accounts(tracking_code);",
                "CREATE INDEX IF NOT EXISTS idx_web_sessions_token ON web_sessions(token_hash);",
                "CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at);",
                "CREATE INDEX IF NOT EXISTS idx_mobile_auth_start_token ON mobile_auth_challenges(start_token_hash);",
                "CREATE INDEX IF NOT EXISTS idx_mobile_auth_candidate ON mobile_auth_challenges(candidate_tg_id, expires_at);",
                "CREATE INDEX IF NOT EXISTS idx_mobile_auth_expiry ON mobile_auth_challenges(expires_at);",
                "CREATE INDEX IF NOT EXISTS idx_mobile_sessions_access ON mobile_sessions(access_token_hash);",
                "CREATE INDEX IF NOT EXISTS idx_mobile_sessions_refresh ON mobile_sessions(refresh_token_hash);",
                "CREATE INDEX IF NOT EXISTS idx_mobile_sessions_user ON mobile_sessions(tg_id, revoked_at);",
                "CREATE INDEX IF NOT EXISTS idx_mobile_access_keys_hash ON mobile_access_keys(key_hash);",

                # subscriptions индексы
                "CREATE INDEX IF NOT EXISTS idx_subscriptions_tg_id ON subscriptions(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_subscriptions_uuid ON subscriptions(remnawave_uuid);",
                "CREATE INDEX IF NOT EXISTS idx_subscriptions_kind_visible ON subscriptions(tg_id, plan_kind, is_visible);",
                "CREATE INDEX IF NOT EXISTS idx_subscriptions_notification ON subscriptions(next_notification_time) WHERE next_notification_time IS NOT NULL;",

                # payments индексы
                "CREATE INDEX IF NOT EXISTS idx_payments_tg_id ON payments(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);",
                "CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider);",
                "CREATE INDEX IF NOT EXISTS idx_payments_subscription_id ON payments(subscription_id);",
                "CREATE INDEX IF NOT EXISTS idx_payments_tracking_code ON payments(tracking_code);",
                "CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);",

                # notification_state индексы
                "CREATE INDEX IF NOT EXISTS idx_notification_state_lookup ON notification_state(tg_id, subscription_id, notification_type);",
                "CREATE INDEX IF NOT EXISTS idx_notification_state_last_sent ON notification_state(last_sent_at);",

                # promo_codes индексы
                "CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);",
                "CREATE INDEX IF NOT EXISTS idx_discounts_active_period ON discounts(active, starts_at, ends_at);",
                "CREATE INDEX IF NOT EXISTS idx_promo_code_users_tg_id ON promo_code_users(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_promo_code_users_code ON promo_code_users(promo_code);",

                # partnership индексы
                "CREATE INDEX IF NOT EXISTS idx_partnerships_tg_id ON partnerships(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_partner_referrals_partner_id ON partner_referrals(partner_id);",
                "CREATE INDEX IF NOT EXISTS idx_partner_earnings_partner_id ON partner_earnings(partner_id);",
                "CREATE INDEX IF NOT EXISTS idx_partner_withdrawals_partner_id ON partner_withdrawals(partner_id);",

                # referral индексы
                "CREATE INDEX IF NOT EXISTS idx_referral_earnings_referrer_id ON referral_earnings(referrer_id);",
                "CREATE INDEX IF NOT EXISTS idx_referral_earnings_referred_user_id ON referral_earnings(referred_user_id);",
                "CREATE INDEX IF NOT EXISTS idx_referral_withdrawals_referrer_id ON referral_withdrawals(referrer_id);",
                "CREATE INDEX IF NOT EXISTS idx_traffic_purchases_subscription_id ON traffic_purchases(subscription_id);",
                "CREATE INDEX IF NOT EXISTS idx_device_addon_purchases_subscription_id ON device_addon_purchases(subscription_id);",
                "CREATE INDEX IF NOT EXISTS idx_device_addon_purchases_invoice ON device_addon_purchases(invoice_id);",
                "CREATE INDEX IF NOT EXISTS idx_device_addon_purchases_expiry ON device_addon_purchases(valid_until, status, expired_processed_at);",
                "CREATE INDEX IF NOT EXISTS idx_traffic_cycles_subscription_id ON subscription_traffic_cycles(subscription_id);",
                "CREATE INDEX IF NOT EXISTS idx_tracking_links_code ON tracking_links(code);",
                "CREATE INDEX IF NOT EXISTS idx_tracking_clicks_code ON tracking_link_clicks(code);",
                "CREATE INDEX IF NOT EXISTS idx_tracking_clicks_tg_id ON tracking_link_clicks(tg_id);",
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
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS subscription_id BIGINT;",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_target TEXT DEFAULT 'new';",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS target_slot_number INT;",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_kind TEXT DEFAULT 'subscription';",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS traffic_package_code TEXT;",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS tracking_code TEXT;",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_requested_at TIMESTAMP;",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_status TEXT;",
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
            await sync_table_schema(conn, 'subscriptions', expected_subscriptions_columns)
            await sync_table_schema(conn, 'payments', expected_payments_columns)
            await sync_table_schema(conn, 'promo_codes', expected_promo_columns)
            await sync_table_schema(conn, 'promo_code_users', expected_promo_usage_columns)
            await sync_table_schema(conn, 'discounts', expected_discounts_columns)
            await sync_table_schema(conn, 'partnerships', expected_partnerships_columns)
            await sync_table_schema(conn, 'partner_referrals', expected_partner_referrals_columns)
            await sync_table_schema(conn, 'partner_earnings', expected_partner_earnings_columns)
            await sync_table_schema(conn, 'partner_withdrawals', expected_partner_withdrawals_columns)
            await sync_table_schema(conn, 'referral_earnings', expected_referral_earnings_columns)
            await sync_table_schema(conn, 'referral_withdrawals', expected_referral_withdrawals_columns)
            await sync_table_schema(conn, 'traffic_purchases', expected_traffic_purchases_columns)
            await sync_table_schema(conn, 'device_addon_purchases', expected_device_addon_purchases_columns)
            await sync_table_schema(conn, 'subscription_traffic_cycles', expected_traffic_cycles_columns)
            await sync_table_schema(conn, 'tracking_links', expected_tracking_links_columns)
            await sync_table_schema(conn, 'tracking_link_clicks', expected_tracking_clicks_columns)
            await sync_table_schema(conn, 'notification_state', expected_notification_state_columns)

            normalized_links = await conn.fetchval(
                """
                WITH candidates AS (
                    SELECT id, code, LOWER(TRIM(code)) AS normalized_code
                    FROM tracking_links
                    WHERE code <> LOWER(TRIM(code))
                ),
                safe_candidates AS (
                    SELECT candidates.*
                    FROM candidates
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM tracking_links existing
                        WHERE existing.code = candidates.normalized_code
                          AND existing.id <> candidates.id
                    )
                ),
                updated AS (
                    UPDATE tracking_links link
                    SET code = safe_candidates.normalized_code,
                        updated_at = now()
                    FROM safe_candidates
                    WHERE link.id = safe_candidates.id
                    RETURNING link.id
                )
                SELECT COUNT(*) FROM updated
                """
            )
            if normalized_links:
                logging.info("✅ Tracking-ссылки приведены к нижнему регистру: %s", normalized_links)

            await conn.execute(
                """
                UPDATE tracking_link_clicks
                SET code = LOWER(TRIM(code))
                WHERE code <> LOWER(TRIM(code))
                """
            )
            await conn.execute(
                """
                UPDATE users
                SET tracking_code = LOWER(TRIM(tracking_code))
                WHERE tracking_code IS NOT NULL
                  AND tracking_code <> LOWER(TRIM(tracking_code))
                """
            )
            await conn.execute(
                """
                UPDATE web_accounts
                SET tracking_code = LOWER(TRIM(tracking_code))
                WHERE tracking_code IS NOT NULL
                  AND tracking_code <> LOWER(TRIM(tracking_code))
                """
            )
            await conn.execute(
                """
                UPDATE payments
                SET tracking_code = LOWER(TRIM(tracking_code))
                WHERE tracking_code IS NOT NULL
                  AND tracking_code <> LOWER(TRIM(tracking_code))
                """
            )

            restored_tracking_users = await conn.fetchval(
                """
                WITH first_click AS (
                    SELECT DISTINCT ON (click.tg_id)
                        click.tg_id,
                        click.code
                    FROM tracking_link_clicks click
                    JOIN tracking_links link ON link.code = click.code
                    WHERE click.tg_id > 0
                      AND link.is_active = TRUE
                    ORDER BY click.tg_id, click.clicked_at ASC, click.id ASC
                ),
                restored AS (
                    UPDATE users user_row
                    SET tracking_code = first_click.code
                    FROM first_click
                    WHERE user_row.tg_id = first_click.tg_id
                      AND user_row.tracking_code IS NULL
                    RETURNING user_row.tg_id
                )
                SELECT COUNT(*) FROM restored
                """
            )
            if restored_tracking_users:
                logging.info(
                    "✅ Восстановлены tracking-коды пользователей по старым кликам: %s",
                    restored_tracking_users,
                )

            await conn.execute(
                """
                INSERT INTO subscriptions (
                    tg_id,
                    slot_number,
                    remnawave_uuid,
                    remnawave_username,
                    subscription_until,
                    squad_uuid,
                    is_active,
                    generation,
                    is_visible,
                    is_renewable,
                    next_notification_time,
                    notification_type
                )
                SELECT
                    u.tg_id,
                    1,
                    u.remnawave_uuid,
                    u.remnawave_username,
                    u.subscription_until,
                    u.squad_uuid,
                    TRUE,
                    'legacy',
                    FALSE,
                    FALSE,
                    u.next_notification_time,
                    u.notification_type
                FROM users u
                WHERE u.remnawave_uuid IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM subscriptions s
                      WHERE s.tg_id = u.tg_id AND s.slot_number = 1
                  )
                """
            )

            migrated_legacy = await conn.fetchval(
                """
                WITH
                migrated AS (
                    UPDATE subscriptions AS subscription
                    SET plan_kind = 'bypass',
                        generation = 'legacy',
                        is_visible = FALSE,
                        is_renewable = FALSE,
                        legacy_readonly = TRUE,
                        legacy_limit_removal_pending = subscription.remnawave_uuid IS NOT NULL,
                        traffic_enabled = FALSE,
                        base_traffic_bytes = 0,
                        current_paid_traffic_bytes = 0,
                        carried_traffic_bytes = 0,
                        current_period_limit_bytes = 0,
                        traffic_reset_at = NULL,
                        last_traffic_sync_at = NULL,
                        next_notification_time = NULL,
                        notification_type = NULL,
                        updated_at = now()
                    WHERE subscription.remnawave_uuid IS NOT NULL
                      AND (
                            LOWER(TRIM(COALESCE(subscription.plan_kind, ''))) NOT IN ('regular', 'bypass')
                         OR (
                                subscription.plan_kind = 'bypass'
                            AND subscription.generation = 'v2'
                            AND subscription.is_visible = TRUE
                            AND COALESCE(subscription.remnawave_username, '') !~
                                '^(tg_[0-9]+|web_[0-9]+)_bypass_[0-9]+$'
                         )
                      )
                    RETURNING subscription.id
                )
                SELECT COUNT(*) FROM migrated
                """
            )
            if migrated_legacy:
                logging.info(
                    "✅ Старые подписки переведены в режим только для просмотра в боте: %s",
                    migrated_legacy,
                )

            await conn.execute(
                """
                UPDATE subscriptions
                SET generation = 'legacy',
                    is_visible = FALSE,
                    is_renewable = FALSE
                WHERE generation = 'legacy'
                   OR generation IS NULL
                """
            )

            await conn.execute(
                """
                UPDATE subscriptions
                SET hwid_device_limit = CASE
                    WHEN plan_kind = 'bypass' THEN GREATEST(COALESCE(hwid_device_limit, 0), $1::INT)
                    ELSE GREATEST(COALESCE(hwid_device_limit, 0), $2::INT)
                END
                WHERE generation = 'v2'
                  AND is_visible = TRUE
                  AND (
                        COALESCE(hwid_device_limit, 0) <= 0
                     OR (plan_kind = 'bypass' AND COALESCE(hwid_device_limit, 0) < $1::INT)
                     OR (COALESCE(plan_kind, 'regular') <> 'bypass' AND COALESCE(hwid_device_limit, 0) < $2::INT)
                  )
                """,
                BYPASS_HWID_DEVICE_LIMIT,
                REGULAR_HWID_DEVICE_LIMIT,
            )

            await conn.execute(
                """
                UPDATE subscriptions
                SET base_traffic_bytes = $1,
                    current_period_limit_bytes = GREATEST(
                        COALESCE(current_period_limit_bytes, 0),
                        $1 + COALESCE(carried_traffic_bytes, 0) + COALESCE(current_paid_traffic_bytes, 0)
                    ),
                    last_traffic_sync_at = NULL,
                    updated_at = now()
                WHERE generation = 'v2'
                  AND plan_kind = 'bypass'
                  AND is_visible = TRUE
                  AND COALESCE(base_traffic_bytes, 0) < $1
                """,
                BYPASS_BASE_TRAFFIC_GB * GB_BYTES,
            )

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


# ────────────────────────────────────────────────
#                  WEB ACCOUNTS
# ────────────────────────────────────────────────

async def create_web_account(login: str, password_hash: str, tracking_code: str | None = None):
    """Создать веб-аккаунт и совместимого внутреннего пользователя атомарно."""
    tracking_code = tracking_code.strip().lower() if tracking_code else None
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                account = await conn.fetchrow(
                    """
                    INSERT INTO web_accounts (login, password_hash, tracking_code)
                    VALUES ($1, $2, $3)
                    RETURNING *
                    """,
                    login,
                    password_hash,
                    tracking_code,
                )
            except asyncpg.UniqueViolationError:
                return None

            service_user_id = -8_000_000_000_000_000_000 + int(account["id"])
            await conn.execute(
                "UPDATE web_accounts SET service_user_id = $1 WHERE id = $2",
                service_user_id,
                account["id"],
            )
            await conn.execute(
                """
                INSERT INTO users (tg_id, username, accepted_terms, tracking_code)
                VALUES ($1, $2, TRUE, $3)
                """,
                service_user_id,
                f"web:{login}",
                tracking_code,
            )
            return await conn.fetchrow("SELECT * FROM web_accounts WHERE id = $1", account["id"])


async def get_web_account_by_login(login: str):
    return await db_execute(
        "SELECT * FROM web_accounts WHERE login = $1 LIMIT 1",
        (login,),
        fetch_one=True,
    )


async def create_web_session(account_id: int, token_hash: str, expires_at):
    await db_execute(
        "DELETE FROM web_sessions WHERE expires_at <= now() AT TIME ZONE 'UTC'",
    )
    session = await db_execute(
        """
        INSERT INTO web_sessions (account_id, token_hash, expires_at)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        (account_id, token_hash, expires_at),
        fetch_one=True,
    )
    await db_execute(
        """
        DELETE FROM web_sessions
        WHERE account_id = $1
          AND id NOT IN (
              SELECT id FROM web_sessions
              WHERE account_id = $1
              ORDER BY created_at DESC
              LIMIT 10
          )
        """,
        (account_id,),
    )
    return session


async def get_web_account_by_session(token_hash: str):
    return await db_execute(
        """
        UPDATE web_sessions AS session
        SET last_seen_at = now()
        FROM web_accounts AS account
        WHERE session.token_hash = $1
          AND session.expires_at > now() AT TIME ZONE 'UTC'
          AND account.id = session.account_id
          AND account.is_active = TRUE
        RETURNING account.id, account.login, account.service_user_id,
                  account.created_at, account.last_login_at
        """,
        (token_hash,),
        fetch_one=True,
    )


async def mark_web_account_login(account_id: int):
    await db_execute(
        "UPDATE web_accounts SET last_login_at = now(), updated_at = now() WHERE id = $1",
        (account_id,),
    )


async def delete_web_session(token_hash: str):
    await db_execute("DELETE FROM web_sessions WHERE token_hash = $1", (token_hash,))


async def list_web_account_payments(service_user_id: int, limit: int = 30):
    return await db_execute(
        """
        SELECT invoice_id, tariff_code, amount, provider, status, payment_target,
               payment_kind, traffic_package_code, subscription_id, target_slot_number, created_at, updated_at
        FROM payments
        WHERE tg_id = $1
        ORDER BY created_at DESC, id DESC
        LIMIT $2
        """,
        (service_user_id, limit),
        fetch_all=True,
    )


async def acquire_user_lock(tg_id: int) -> bool:
    """
    Получить блокировку пользователя используя PostgreSQL advisory lock
    
    Args:
        tg_id: ID пользователя Telegram
        
    Returns:
        True если удалось получить блокировку, False иначе
    """
    try:
        async with _user_locks_guard:
            lock = _user_locks.get(tg_id)
            if lock is None:
                lock = asyncio.Lock()
                _user_locks[tg_id] = lock

            if lock.locked():
                return False

            await lock.acquire()
            return True
    except Exception as e:
        logging.error(f"Lock error: {e}")
        return False


async def release_user_lock(tg_id: int):
    """
    Освободить блокировку пользователя
    
    Args:
        tg_id: ID пользователя Telegram
    """
    try:
        async with _user_locks_guard:
            lock = _user_locks.get(tg_id)
            if lock and lock.locked():
                lock.release()
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


async def create_user(tg_id: int, username: str, referrer_id=None, tracking_code: str | None = None):
    """Создать или обновить пользователя"""
    tracking_code = tracking_code.strip().lower() if tracking_code else None
    await db_execute(
        """
        INSERT INTO users (tg_id, username, referrer_id, tracking_code)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (tg_id) DO UPDATE
        SET username = COALESCE(EXCLUDED.username, users.username),
            tracking_code = CASE
                WHEN users.tracking_code IS NULL AND EXCLUDED.tracking_code IS NOT NULL
                THEN EXCLUDED.tracking_code
                ELSE users.tracking_code
            END
        """,
        (tg_id, username, referrer_id, tracking_code)
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

def _calculate_notification_fields(subscription_until):
    """Рассчитать время и тип следующего уведомления."""
    from datetime import datetime, timedelta

    if subscription_until:
        now = datetime.utcnow()
        next_notification = subscription_until - timedelta(days=1.5)

        if next_notification <= now:
            time_left = (subscription_until - now).total_seconds()

            if time_left > 86400:
                next_notification = subscription_until - timedelta(days=1)
                notification_type = "below1day"
            elif time_left > 0:
                next_notification = subscription_until
                notification_type = "expired"
            else:
                next_notification = None
                notification_type = None
        else:
            notification_type = "1day_left"
    else:
        next_notification = None
        notification_type = None

    return next_notification, notification_type


async def get_user_subscriptions(tg_id: int):
    """Получить все подписки пользователя."""
    return await db_execute(
        "SELECT * FROM subscriptions WHERE tg_id = $1 ORDER BY slot_number ASC, id ASC",
        (tg_id,),
        fetch_all=True
    )


async def get_subscriptions_with_remnawave_uuid(active_only: bool = False):
    """Получить подписки, для которых можно перевыпустить ссылку в Remnawave."""
    where_active = "AND subscription_until IS NOT NULL AND subscription_until > now() AT TIME ZONE 'UTC'" if active_only else ""
    return await db_execute(
        f"""
        SELECT id, tg_id, slot_number, type_index, plan_kind, remnawave_uuid, remnawave_username, subscription_until
        FROM subscriptions
        WHERE remnawave_uuid IS NOT NULL
          {where_active}
        ORDER BY tg_id ASC, id ASC
        """,
        fetch_all=True,
    )


async def get_visible_subscriptions(tg_id: int):
    """Получить видимые пользователю подписки новой модели."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE tg_id = $1 AND generation = 'v2' AND is_visible = TRUE
        ORDER BY plan_kind ASC, type_index ASC, id ASC
        """,
        (tg_id,),
        fetch_all=True
    )


async def get_bot_visible_subscriptions(tg_id: int):
    """Получить подписки для бота, включая активные старые подписки только для просмотра."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE tg_id = $1
          AND (
                (generation = 'v2' AND is_visible = TRUE)
             OR (
                    legacy_readonly = TRUE
                AND subscription_until IS NOT NULL
                AND subscription_until > now() AT TIME ZONE 'UTC'
             )
          )
        ORDER BY plan_kind ASC, type_index ASC NULLS LAST, slot_number ASC, id ASC
        """,
        (tg_id,),
        fetch_all=True,
    )


async def get_renewable_subscriptions(tg_id: int):
    """Получить подписки, которые можно продлевать."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE tg_id = $1
          AND generation = 'v2'
          AND is_visible = TRUE
          AND is_renewable = TRUE
        ORDER BY plan_kind ASC, type_index ASC, id ASC
        """,
        (tg_id,),
        fetch_all=True
    )


async def get_active_bypass_subscriptions(tg_id: int):
    """Получить активные bypass-подписки пользователя."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE tg_id = $1
          AND generation = 'v2'
          AND is_visible = TRUE
          AND is_renewable = TRUE
          AND plan_kind = 'bypass'
          AND subscription_until IS NOT NULL
          AND subscription_until > now() AT TIME ZONE 'UTC'
        ORDER BY type_index ASC, id ASC
        """,
        (tg_id,),
        fetch_all=True
    )


async def get_next_type_index(tg_id: int, plan_kind: str) -> int | None:
    """Получить следующий номер подписки внутри типа regular/bypass."""
    rows = await db_execute(
        """
        SELECT type_index FROM subscriptions
        WHERE tg_id = $1
          AND plan_kind = $2
          AND generation = 'v2'
          AND is_visible = TRUE
        """,
        (tg_id, plan_kind),
        fetch_all=True
    )
    taken = {row['type_index'] for row in rows if row['type_index'] is not None}

    for index in range(1, MAX_SUBSCRIPTIONS_PER_USER + 1):
        if index not in taken:
            return index

    return None


async def count_visible_subscriptions_by_kind(tg_id: int, plan_kind: str) -> int:
    """Посчитать видимые подписки конкретного типа."""
    result = await db_execute(
        """
        SELECT COUNT(*) AS count FROM subscriptions
        WHERE tg_id = $1
          AND plan_kind = $2
          AND generation = 'v2'
          AND is_visible = TRUE
        """,
        (tg_id, plan_kind),
        fetch_one=True
    )
    return result['count'] if result else 0


async def get_subscription_by_id(subscription_id: int, tg_id: int | None = None):
    """Получить подписку по ID."""
    if tg_id is None:
        return await db_execute(
            "SELECT * FROM subscriptions WHERE id = $1 LIMIT 1",
            (subscription_id,),
            fetch_one=True
        )

    return await db_execute(
        "SELECT * FROM subscriptions WHERE id = $1 AND tg_id = $2 LIMIT 1",
        (subscription_id, tg_id),
        fetch_one=True
    )


async def get_subscription_by_slot(tg_id: int, slot_number: int):
    """Получить подписку пользователя по номеру слота."""
    return await db_execute(
        "SELECT * FROM subscriptions WHERE tg_id = $1 AND slot_number = $2 LIMIT 1",
        (tg_id, slot_number),
        fetch_one=True
    )


async def get_subscription_by_type_index(tg_id: int, plan_kind: str, type_index: int):
    """Получить видимую v2-подписку по типу и номеру внутри типа."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE tg_id = $1
          AND plan_kind = $2
          AND type_index = $3
          AND generation = 'v2'
          AND is_visible = TRUE
        LIMIT 1
        """,
        (tg_id, plan_kind, type_index),
        fetch_one=True
    )


async def get_subscription_by_uuid(remnawave_uuid: str):
    """Получить подписку по UUID Remnawave."""
    return await db_execute(
        "SELECT * FROM subscriptions WHERE remnawave_uuid = $1 LIMIT 1",
        (remnawave_uuid,),
        fetch_one=True
    )


async def get_next_subscription_slot(tg_id: int) -> int | None:
    """Получить следующий свободный слот подписки."""
    subscriptions = await get_user_subscriptions(tg_id)
    taken_slots = {sub['slot_number'] for sub in subscriptions}

    for slot in range(1, MAX_SUBSCRIPTIONS_PER_USER * 2 + 1):
        if slot not in taken_slots:
            return slot

    return None


async def create_subscription_record(
    tg_id: int,
    slot_number: int,
    *,
    plan_kind: str | None = None,
    type_index: int | None = None,
    generation: str = 'legacy',
    is_visible: bool = False,
    is_renewable: bool = False,
    purchase_days: int | None = None,
):
    """Создать запись подписки без VPN-данных."""
    return await db_execute(
        """
        INSERT INTO subscriptions (
            tg_id,
            slot_number,
            is_active,
            plan_kind,
            type_index,
            generation,
            is_visible,
            is_renewable,
            purchase_days
        )
        VALUES ($1, $2, TRUE, $3, $4, $5, $6, $7, $8)
        RETURNING *
        """,
        (tg_id, slot_number, plan_kind, type_index, generation, is_visible, is_renewable, purchase_days),
        fetch_one=True
    )


async def sync_primary_subscription_to_user(tg_id: int):
    """Синхронизировать слот #1 в legacy-поля users."""
    primary = await get_subscription_by_slot(tg_id, 1)

    if primary:
        await db_execute(
            """
            UPDATE users
            SET remnawave_uuid = $1,
                remnawave_username = $2,
                subscription_until = $3,
                squad_uuid = $4,
                next_notification_time = $5,
                notification_type = $6
            WHERE tg_id = $7
            """,
            (
                primary['remnawave_uuid'],
                primary['remnawave_username'],
                primary['subscription_until'],
                primary['squad_uuid'],
                primary['next_notification_time'],
                primary['notification_type'],
                tg_id,
            )
        )
    else:
        await db_execute(
            """
            UPDATE users
            SET remnawave_uuid = NULL,
                remnawave_username = NULL,
                subscription_until = NULL,
                squad_uuid = NULL,
                next_notification_time = NULL,
                notification_type = NULL
            WHERE tg_id = $1
            """,
            (tg_id,)
        )


async def update_subscription_record(subscription_id: int, uuid: str, username: str, subscription_until, squad_uuid: str | None):
    """Обновить конкретную подписку пользователя."""
    next_notification, notification_type = _calculate_notification_fields(subscription_until)

    await db_execute(
        """
        UPDATE subscriptions
        SET remnawave_uuid = $1,
            remnawave_username = $2,
            subscription_until = $3,
            squad_uuid = $4,
            next_notification_time = $6,
            notification_type = $7,
            is_active = TRUE,
            updated_at = now()
        WHERE id = $5
        """,
        (uuid, username, subscription_until, squad_uuid, subscription_id, next_notification, notification_type)
    )

    subscription = await get_subscription_by_id(subscription_id)
    if subscription:
        await sync_primary_subscription_to_user(subscription['tg_id'])


async def sync_subscription_expiry(subscription_id: int, subscription_until):
    """Синхронизировать фактический срок подписки из Remnawave."""
    next_notification, notification_type = _calculate_notification_fields(subscription_until)
    is_active = bool(subscription_until and subscription_until > datetime.utcnow())

    await db_execute(
        """
        UPDATE subscriptions
        SET subscription_until = $1,
            is_active = $2,
            next_notification_time = $3,
            notification_type = $4,
            updated_at = now()
        WHERE id = $5
        """,
        (subscription_until, is_active, next_notification, notification_type, subscription_id),
    )

    subscription = await get_subscription_by_id(subscription_id)
    if subscription:
        await sync_primary_subscription_to_user(subscription['tg_id'])


async def update_subscription(
    tg_id: int,
    uuid: str,
    username: str,
    subscription_until,
    squad_uuid: str | None,
    *,
    subscription_id: int | None = None,
    slot_number: int | None = 1,
):
    """Совместимый wrapper обновления подписки."""
    target_subscription = None

    if subscription_id is not None:
        target_subscription = await get_subscription_by_id(subscription_id, tg_id)

    if target_subscription is None and uuid:
        target_subscription = await get_subscription_by_uuid(uuid)
        if target_subscription and target_subscription['tg_id'] != tg_id:
            target_subscription = None

    if target_subscription is None and slot_number is not None:
        target_subscription = await get_subscription_by_slot(tg_id, slot_number)

    if target_subscription is None:
        resolved_slot = slot_number if slot_number is not None else await get_next_subscription_slot(tg_id)
        if resolved_slot is None:
            raise RuntimeError(f"No free subscription slots for user {tg_id}")
        target_subscription = await create_subscription_record(tg_id, resolved_slot)

    await update_subscription_record(target_subscription['id'], uuid, username, subscription_until, squad_uuid)
    return target_subscription['id']


async def delete_subscription_record(subscription_id: int):
    """Удалить запись подписки и синхронизировать legacy-поля."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            subscription = await conn.fetchrow(
                "SELECT * FROM subscriptions WHERE id = $1 FOR UPDATE",
                subscription_id,
            )
            if not subscription:
                return False

            tg_id = subscription["tg_id"]
            await conn.execute(
                "DELETE FROM notification_state WHERE subscription_id = $1",
                subscription_id,
            )
            await conn.execute(
                "UPDATE payments SET subscription_id = NULL, updated_at = now() WHERE subscription_id = $1",
                subscription_id,
            )
            await conn.execute(
                "DELETE FROM traffic_purchases WHERE subscription_id = $1",
                subscription_id,
            )
            await conn.execute(
                "DELETE FROM device_addon_purchases WHERE subscription_id = $1",
                subscription_id,
            )
            await conn.execute(
                "DELETE FROM subscription_traffic_cycles WHERE subscription_id = $1",
                subscription_id,
            )
            await conn.execute("DELETE FROM subscriptions WHERE id = $1", subscription_id)

    await sync_primary_subscription_to_user(tg_id)
    return True


async def has_subscription(tg_id: int) -> bool:
    """Проверить есть ли хотя бы одна подписка."""
    result = await db_execute(
        "SELECT 1 FROM subscriptions WHERE tg_id = $1 LIMIT 1",
        (tg_id,),
        fetch_one=True
    )
    return result is not None


# ────────────────────────────────────────────────
#               TRACKING LINKS
# ────────────────────────────────────────────────

async def create_tracking_link(code: str, title: str | None, created_by: int):
    """Создать или включить tracking-ссылку."""
    code = code.strip().lower()
    return await db_execute(
        """
        INSERT INTO tracking_links (code, title, created_by, is_active)
        VALUES ($1, $2, $3, TRUE)
        ON CONFLICT (code) DO UPDATE
        SET title = EXCLUDED.title,
            is_active = TRUE,
            updated_at = now()
        RETURNING *
        """,
        (code, title, created_by),
        fetch_one=True
    )


async def get_tracking_link(code: str):
    """Получить tracking-ссылку по коду."""
    code = code.strip().lower()
    return await db_execute(
        "SELECT * FROM tracking_links WHERE code = $1 LIMIT 1",
        (code,),
        fetch_one=True
    )


async def list_tracking_links():
    """Получить список tracking-ссылок."""
    return await db_execute(
        """
        SELECT code, title, is_active, created_at
        FROM tracking_links
        ORDER BY created_at DESC, code ASC
        """,
        fetch_all=True
    )


async def set_tracking_link_active(code: str, is_active: bool) -> bool:
    """Включить или отключить tracking-ссылку."""
    code = code.strip().lower()
    result = await db_execute(
        """
        UPDATE tracking_links
        SET is_active = $2, updated_at = now()
        WHERE code = $1
        RETURNING 1
        """,
        (code, is_active),
        fetch_one=True
    )
    return result is not None


async def record_tracking_link_click(code: str, tg_id: int, is_new_user: bool) -> bool:
    """Записать переход по активной tracking-ссылке."""
    code = code.strip().lower()
    link = await get_tracking_link(code)
    if not link or not link['is_active']:
        return False

    await db_execute(
        """
        INSERT INTO tracking_link_clicks (code, tg_id, is_new_user)
        VALUES ($1, $2, $3)
        """,
        (code, tg_id, is_new_user)
    )
    return True


async def get_user_tracking_code(tg_id: int) -> str | None:
    """Получить first-touch tracking-код пользователя."""
    result = await db_execute(
        "SELECT tracking_code FROM users WHERE tg_id = $1",
        (tg_id,),
        fetch_one=True
    )
    return result['tracking_code'] if result and result['tracking_code'] else None


async def get_payment_tracking_code(tg_id: int) -> str | None:
    """Получить last-touch код для платежа с fallback на first-touch пользователя."""
    recent_click = await db_execute(
        """
        SELECT click.code
        FROM tracking_link_clicks click
        JOIN tracking_links link ON link.code = click.code
        WHERE click.tg_id = $1
          AND link.is_active = TRUE
          AND click.clicked_at >= now() - ($2::integer * INTERVAL '1 day')
        ORDER BY click.clicked_at DESC, click.id DESC
        LIMIT 1
        """,
        (tg_id, TRACKING_ATTRIBUTION_DAYS),
        fetch_one=True,
    )
    if recent_click and recent_click['code']:
        return recent_click['code']
    return await get_user_tracking_code(tg_id)


async def get_tracking_link_stats(code: str):
    """Получить статистику tracking-ссылки."""
    code = code.strip().lower()
    link = await get_tracking_link(code)
    if not link:
        return None

    clicks = await db_execute(
        """
        SELECT
            COUNT(*) AS total_clicks,
            COUNT(DISTINCT tg_id) AS unique_clicks,
            COUNT(DISTINCT tg_id) FILTER (WHERE is_new_user = TRUE) AS new_clicks
        FROM tracking_link_clicks
        WHERE code = $1
        """,
        (code,),
        fetch_one=True
    )

    users_count = await db_execute(
        "SELECT COUNT(*) AS count FROM users WHERE tracking_code = $1",
        (code,),
        fetch_one=True
    )

    payments_summary = await db_execute(
        """
        SELECT
            COUNT(*) AS paid_payments,
            COUNT(*) FILTER (
                WHERE payment_kind = 'subscription'
                  AND refund_requested_at IS NULL
            ) AS paid_subscriptions,
            COUNT(*) FILTER (
                WHERE payment_kind = 'subscription'
                  AND payment_target = 'new'
                  AND refund_requested_at IS NULL
            ) AS new_subscriptions,
            COUNT(DISTINCT tg_id) FILTER (
                WHERE payment_kind = 'subscription'
                  AND refund_requested_at IS NULL
            ) AS unique_payers,
            COALESCE(SUM(amount) FILTER (
                WHERE refund_requested_at IS NULL
            ), 0) AS revenue,
            COALESCE(SUM(amount) FILTER (
                WHERE payment_kind = 'subscription'
                  AND refund_requested_at IS NULL
            ), 0) AS subscription_revenue
        FROM payments
        WHERE tracking_code = $1
          AND status = 'paid'
        """,
        (code,),
        fetch_one=True
    )

    payments_by_tariff = await db_execute(
        """
        SELECT tariff_code, payment_kind, COUNT(*) AS purchase_count, COALESCE(SUM(amount), 0) AS revenue
        FROM payments
        WHERE tracking_code = $1
          AND status = 'paid'
          AND refund_requested_at IS NULL
        GROUP BY tariff_code, payment_kind
        ORDER BY payment_kind ASC, tariff_code ASC
        """,
        (code,),
        fetch_all=True
    )

    return {
        'link': link,
        'total_clicks': clicks['total_clicks'] if clicks else 0,
        'unique_clicks': clicks['unique_clicks'] if clicks else 0,
        'new_clicks': clicks['new_clicks'] if clicks else 0,
        'attributed_users': users_count['count'] if users_count else 0,
        'paid_payments': payments_summary['paid_payments'] if payments_summary else 0,
        'paid_subscriptions': payments_summary['paid_subscriptions'] if payments_summary else 0,
        'new_subscriptions': payments_summary['new_subscriptions'] if payments_summary else 0,
        'unique_payers': payments_summary['unique_payers'] if payments_summary else 0,
        'revenue': float(payments_summary['revenue'] or 0) if payments_summary else 0,
        'subscription_revenue': float(payments_summary['subscription_revenue'] or 0) if payments_summary else 0,
        'payments_by_tariff': payments_by_tariff or [],
    }


# ────────────────────────────────────────────────
#               PAYMENT MANAGEMENT
# ────────────────────────────────────────────────

async def create_payment(
    tg_id: int,
    tariff_code: str,
    amount: float,
    provider: str,
    invoice_id: str,
    *,
    subscription_id: int | None = None,
    payment_target: str = 'new',
    target_slot_number: int | None = None,
    payment_kind: str = 'subscription',
    traffic_package_code: str | None = None,
):
    """Создать запись о платеже"""
    from datetime import datetime
    tracking_code = await get_payment_tracking_code(tg_id)
    await db_execute(
        """
        INSERT INTO payments (
            tg_id,
            tariff_code,
            amount,
            created_at,
            provider,
            invoice_id,
            subscription_id,
            payment_target,
            target_slot_number,
            payment_kind,
            traffic_package_code,
            tracking_code
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        (
            tg_id,
            tariff_code,
            amount,
            datetime.utcnow(),
            provider,
            str(invoice_id),
            subscription_id,
            payment_target,
            target_slot_number,
            payment_kind,
            traffic_package_code,
            tracking_code,
        )
    )


async def get_pending_payments():
    """Получить все ожидающие платежи"""
    return await db_execute(
        "SELECT id, tg_id, invoice_id, tariff_code, subscription_id, payment_target, target_slot_number, payment_kind, traffic_package_code FROM payments WHERE status = 'pending' AND provider = 'cryptobot' ORDER BY id",
        fetch_all=True
    )


async def get_pending_payments_by_provider(provider: str):
    """Получить все ожидающие платежи по конкретному провайдеру"""
    return await db_execute(
        "SELECT id, tg_id, invoice_id, tariff_code, amount, subscription_id, payment_target, target_slot_number, payment_kind, traffic_package_code FROM payments WHERE status = 'pending' AND provider = $1 ORDER BY id",
        (provider,),
        fetch_all=True
    )


async def get_active_payment_for_user_and_tariff(
    tg_id: int,
    tariff_code: str,
    provider: str,
    *,
    amount: float | None = None,
    subscription_id: int | None = None,
    payment_target: str = 'new',
    target_slot_number: int | None = None,
):
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
        WHERE tg_id = $1
          AND tariff_code = $2
          AND status = 'pending'
          AND provider = $3
          AND subscription_id IS NOT DISTINCT FROM $4
          AND payment_target = $5
          AND target_slot_number IS NOT DISTINCT FROM $6
          AND ($7::numeric IS NULL OR amount = $7)
        ORDER BY id DESC
        LIMIT 1
        """,
        (tg_id, tariff_code, provider, subscription_id, payment_target, target_slot_number, amount),
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
        SELECT invoice_id, tariff_code, provider, subscription_id, payment_target, target_slot_number, payment_kind, traffic_package_code
        FROM payments 
        WHERE tg_id = $1 AND status = 'pending'
        ORDER BY id DESC 
        LIMIT 1
        """,
        (tg_id,),
        fetch_one=True
    )
    return result


async def get_payment_by_invoice(invoice_id: str):
    """Получить запись платежа по invoice_id."""
    return await db_execute(
        "SELECT * FROM payments WHERE invoice_id = $1 LIMIT 1",
        (invoice_id,),
        fetch_one=True
    )


async def list_active_subscriptions_for_refund(tg_id: int):
    """Получить активные подписки, по которым пользователь может выбрать возврат."""
    return await db_execute(
        """
        SELECT *
        FROM subscriptions
        WHERE tg_id = $1
          AND generation = 'v2'
          AND is_visible = TRUE
          AND is_renewable = TRUE
          AND subscription_until IS NOT NULL
          AND subscription_until > now() AT TIME ZONE 'UTC'
        ORDER BY plan_kind ASC, type_index ASC, id ASC
        """,
        (tg_id,),
        fetch_all=True,
    )


async def get_latest_subscription_payment_for_refund(subscription_id: int, tg_id: int):
    """Получить последнюю оплату/продление конкретной подписки."""
    subscription = await get_subscription_by_id(subscription_id, tg_id)
    if not subscription:
        return None

    plan_kind = subscription.get("plan_kind") or "regular"
    type_index = subscription.get("type_index") or subscription.get("slot_number")
    tariff_like = f"{plan_kind}_%"

    return await db_execute(
        """
        SELECT *
        FROM payments
        WHERE tg_id = $1
          AND payment_kind = 'subscription'
          AND status = 'paid'
          AND (
                subscription_id = $2
             OR (
                    subscription_id IS NULL
                AND payment_target = 'new'
                AND target_slot_number = $3
                AND tariff_code LIKE $4
             )
          )
        ORDER BY updated_at DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        (tg_id, subscription_id, type_index, tariff_like),
        fetch_one=True,
    )


async def request_payment_refund(payment_id: int, tg_id: int) -> bool:
    """Зафиксировать заявку на возврат по оплаченному платежу."""
    result = await db_execute(
        """
        UPDATE payments
        SET refund_requested_at = now(),
            refund_status = 'requested',
            updated_at = now()
        WHERE id = $1
          AND tg_id = $2
          AND status = 'paid'
          AND refund_requested_at IS NULL
        RETURNING id
        """,
        (payment_id, tg_id),
        fetch_one=True,
    )
    return result is not None


async def link_payment_to_subscription(invoice_id: str, subscription_id: int) -> None:
    """Привязать платёж к фактически созданной подписке."""
    await db_execute(
        """
        UPDATE payments
        SET subscription_id = $2,
            updated_at = now()
        WHERE invoice_id = $1
          AND subscription_id IS NULL
        """,
        (invoice_id, subscription_id),
    )


async def deactivate_subscription_for_refund(subscription_id: int, tg_id: int, expired_at) -> bool:
    """Деактивировать подписку после подтверждения заявки на возврат."""
    result = await db_execute(
        """
        UPDATE subscriptions
        SET subscription_until = $3,
            is_active = FALSE,
            is_visible = FALSE,
            is_renewable = FALSE,
            next_notification_time = NULL,
            notification_type = NULL,
            updated_at = now()
        WHERE id = $1
          AND tg_id = $2
          AND generation = 'v2'
        RETURNING id
        """,
        (subscription_id, tg_id, expired_at),
        fetch_one=True,
    )
    if result:
        await db_execute(
            "DELETE FROM notification_state WHERE tg_id = $1 AND subscription_id = $2",
            (tg_id, subscription_id),
        )
    return result is not None


async def create_traffic_purchase(subscription_id: int, package_code: str, traffic_bytes: int, amount: float, provider: str, invoice_id: str):
    """Создать запись покупки трафика."""
    return await db_execute(
        """
        INSERT INTO traffic_purchases (subscription_id, package_code, traffic_bytes, amount, provider, invoice_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        (subscription_id, package_code, traffic_bytes, amount, provider, str(invoice_id)),
        fetch_one=True
    )


async def activate_traffic_purchase(invoice_id: str) -> bool:
    """Отметить покупку трафика активированной."""
    await db_execute(
        """
        UPDATE traffic_purchases
        SET status = 'paid', activated_at = now()
        WHERE invoice_id = $1 AND status != 'paid'
        """,
        (str(invoice_id),)
    )
    return True


async def add_traffic_to_subscription(subscription_id: int, traffic_bytes: int) -> None:
    """Добавить купленный трафик к текущему периоду bypass-подписки."""
    await db_execute(
        """
        UPDATE subscriptions
        SET current_paid_traffic_bytes = current_paid_traffic_bytes + $1,
            current_period_limit_bytes = current_period_limit_bytes + $1,
            last_traffic_sync_at = now(),
            updated_at = now()
        WHERE id = $2
        """,
        (traffic_bytes, subscription_id)
    )


async def create_device_addon_purchase(
    subscription_id: int,
    device_count: int,
    amount: float,
    provider: str,
    invoice_id: str,
    valid_until,
):
    """Создать запись покупки дополнительных устройств."""
    return await db_execute(
        """
        INSERT INTO device_addon_purchases (
            subscription_id,
            device_count,
            amount,
            provider,
            invoice_id,
            valid_until
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        (subscription_id, device_count, amount, provider, str(invoice_id), valid_until),
        fetch_one=True,
    )


async def get_device_addon_purchase_by_invoice(invoice_id: str):
    """Получить покупку устройств по invoice_id."""
    return await db_execute(
        "SELECT * FROM device_addon_purchases WHERE invoice_id = $1 LIMIT 1",
        (str(invoice_id),),
        fetch_one=True,
    )


async def activate_device_addon_purchase(invoice_id: str) -> bool:
    """Отметить покупку устройств активированной."""
    await db_execute(
        """
        UPDATE device_addon_purchases
        SET status = 'paid',
            activated_at = now()
        WHERE invoice_id = $1
          AND status != 'paid'
        """,
        (str(invoice_id),),
    )
    return True


async def get_active_device_addon_count(subscription_id: int):
    """Посчитать активные докупленные устройства для подписки."""
    result = await db_execute(
        """
        SELECT COALESCE(SUM(device_count), 0) AS device_count
        FROM device_addon_purchases
        WHERE subscription_id = $1
          AND status = 'paid'
          AND valid_until > now() AT TIME ZONE 'UTC'
        """,
        (subscription_id,),
        fetch_one=True,
    )
    return int(result["device_count"] or 0) if result else 0


async def set_subscription_device_limit(subscription_id: int, device_limit: int) -> None:
    """Обновить локальный лимит устройств у подписки."""
    await db_execute(
        """
        UPDATE subscriptions
        SET hwid_device_limit = $1,
            updated_at = now()
        WHERE id = $2
        """,
        (device_limit, subscription_id),
    )


async def get_subscriptions_with_expired_device_addons():
    """Получить подписки, у которых истекли докупленные устройства и надо пересчитать лимит."""
    return await db_execute(
        """
        SELECT DISTINCT s.*
        FROM subscriptions s
        JOIN device_addon_purchases dap ON dap.subscription_id = s.id
        WHERE dap.status = 'paid'
          AND dap.valid_until <= now() AT TIME ZONE 'UTC'
          AND dap.expired_processed_at IS NULL
          AND s.generation = 'v2'
          AND s.remnawave_uuid IS NOT NULL
        ORDER BY s.id ASC
        """,
        fetch_all=True,
    )


async def mark_expired_device_addons_processed(subscription_id: int) -> None:
    """Отметить истёкшие покупки устройств обработанными."""
    await db_execute(
        """
        UPDATE device_addon_purchases
        SET expired_processed_at = now()
        WHERE subscription_id = $1
          AND status = 'paid'
          AND valid_until <= now() AT TIME ZONE 'UTC'
          AND expired_processed_at IS NULL
        """,
        (subscription_id,),
    )


async def get_legacy_subscriptions_pending_limit_removal():
    """Получить старые подписки, с которых надо убрать ранее установленный лимит."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE legacy_readonly = TRUE
          AND legacy_limit_removal_pending = TRUE
          AND remnawave_uuid IS NOT NULL
        ORDER BY id ASC
        """,
        fetch_all=True,
    )


async def mark_legacy_subscription_limit_removed(subscription_id: int) -> None:
    """Зафиксировать снятие лимита со старой подписки без сброса её трафика."""
    await db_execute(
        """
        UPDATE subscriptions
        SET legacy_limit_removal_pending = FALSE,
            traffic_enabled = FALSE,
            base_traffic_bytes = 0,
            current_paid_traffic_bytes = 0,
            carried_traffic_bytes = 0,
            current_period_limit_bytes = 0,
            traffic_reset_at = NULL,
            last_traffic_sync_at = now(),
            updated_at = now()
        WHERE id = $1
        """,
        (subscription_id,),
    )


async def get_bypass_subscriptions_for_limit_sync():
    """Получить активные bypass-подписки, лимит которых надо синхронизировать с Remnawave."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE generation = 'v2'
          AND plan_kind = 'bypass'
          AND is_visible = TRUE
          AND traffic_enabled = TRUE
          AND remnawave_uuid IS NOT NULL
          AND subscription_until IS NOT NULL
          AND subscription_until > now() AT TIME ZONE 'UTC'
          AND current_period_limit_bytes > 0
          AND (
              last_traffic_sync_at IS NULL
              OR updated_at > last_traffic_sync_at
          )
        ORDER BY updated_at ASC
        """,
        fetch_all=True,
    )


async def mark_traffic_limit_synced(subscription_id: int) -> None:
    """Отметить, что лимит подписки синхронизирован с Remnawave."""
    await db_execute(
        """
        UPDATE subscriptions
        SET last_traffic_sync_at = now()
        WHERE id = $1
        """,
        (subscription_id,)
    )


async def get_bypass_subscriptions_for_traffic_reset():
    """Получить bypass-подписки, которым пора сбросить трафик."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE generation = 'v2'
          AND plan_kind = 'bypass'
          AND is_visible = TRUE
          AND traffic_enabled = TRUE
          AND remnawave_uuid IS NOT NULL
          AND subscription_until IS NOT NULL
          AND subscription_until > now() AT TIME ZONE 'UTC'
          AND traffic_reset_at IS NOT NULL
          AND traffic_reset_at <= now() AT TIME ZONE 'UTC'
        ORDER BY traffic_reset_at ASC
        """,
        fetch_all=True
    )


async def get_active_bypass_subscriptions_for_manual_traffic_reset():
    """Получить все активные bypass-подписки для ручного массового сброса трафика."""
    return await db_execute(
        """
        SELECT * FROM subscriptions
        WHERE generation = 'v2'
          AND plan_kind = 'bypass'
          AND is_visible = TRUE
          AND traffic_enabled = TRUE
          AND remnawave_uuid IS NOT NULL
          AND subscription_until IS NOT NULL
          AND subscription_until > now() AT TIME ZONE 'UTC'
        ORDER BY tg_id ASC, type_index ASC, id ASC
        """,
        fetch_all=True,
    )


async def record_traffic_cycle(
    subscription_id: int,
    period_start,
    period_end,
    base_traffic_bytes: int,
    carried_traffic_bytes: int,
    paid_traffic_bytes: int,
    used_traffic_bytes_before_reset: int,
    remaining_paid_traffic_bytes: int,
):
    """Записать историю traffic-cycle перед сбросом."""
    await db_execute(
        """
        INSERT INTO subscription_traffic_cycles (
            subscription_id,
            period_start,
            period_end,
            base_traffic_bytes,
            carried_traffic_bytes,
            paid_traffic_bytes,
            used_traffic_bytes_before_reset,
            remaining_paid_traffic_bytes,
            reset_processed_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
        """,
        (
            subscription_id,
            period_start,
            period_end,
            base_traffic_bytes,
            carried_traffic_bytes,
            paid_traffic_bytes,
            used_traffic_bytes_before_reset,
            remaining_paid_traffic_bytes,
        )
    )


async def apply_traffic_reset(subscription_id: int, remaining_paid_traffic_bytes: int, next_reset_at, new_limit_bytes: int):
    """Применить новый traffic-cycle после сброса."""
    await db_execute(
        """
        UPDATE subscriptions
        SET carried_traffic_bytes = $1,
            current_paid_traffic_bytes = 0,
            current_period_limit_bytes = $2,
            last_known_used_traffic_bytes = 0,
            traffic_reset_at = $3,
            last_traffic_sync_at = now(),
            updated_at = now()
        WHERE id = $4
        """,
        (remaining_paid_traffic_bytes, new_limit_bytes, next_reset_at, subscription_id)
    )


async def update_subscription_traffic_period(
    subscription_id: int,
    *,
    traffic_enabled: bool,
    base_traffic_bytes: int,
    carried_traffic_bytes: int,
    current_paid_traffic_bytes: int,
    current_period_limit_bytes: int,
    traffic_reset_at,
    last_known_used_traffic_bytes: int,
) -> None:
    """Обновить traffic-cycle подписки после покупки/продления/ручной правки."""
    await db_execute(
        """
        UPDATE subscriptions
        SET traffic_enabled = $2,
            base_traffic_bytes = $3,
            carried_traffic_bytes = $4,
            current_paid_traffic_bytes = $5,
            current_period_limit_bytes = $6,
            traffic_reset_at = $7,
            last_known_used_traffic_bytes = $8,
            last_traffic_sync_at = now(),
            updated_at = now()
        WHERE id = $1
        """,
        (
            subscription_id,
            traffic_enabled,
            base_traffic_bytes,
            carried_traffic_bytes,
            current_paid_traffic_bytes,
            current_period_limit_bytes,
            traffic_reset_at,
            last_known_used_traffic_bytes,
        )
    )


async def update_payment_status(payment_id: int, status: str):
    """Обновить статус платежа"""
    await db_execute(
        "UPDATE payments SET status = $1, updated_at = now() WHERE id = $2",
        (status, payment_id)
    )


async def update_payment_status_by_invoice(invoice_id: str, status: str):
    """Обновить статус платежа по invoice_id"""
    await db_execute(
        "UPDATE payments SET status = $1, updated_at = now() WHERE invoice_id = $2",
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


async def get_referral_counters(tg_id: int):
    """Получить старые счётчики рефералов из users."""
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
        WHERE tg_id > 0
        AND next_notification_time IS NOT NULL
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
        WHERE tg_id > 0
        AND next_notification_time IS NOT NULL
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


async def get_notification_last_sent(tg_id: int, notification_type: str, subscription_id: int | None = None):
    """Вернуть время последней отправки уведомления конкретного типа."""
    row = await db_execute(
        """
        SELECT last_sent_at
        FROM notification_state
        WHERE tg_id = $1 AND subscription_id = $2 AND notification_type = $3
        """,
        (tg_id, subscription_id or 0, notification_type),
        fetch_one=True,
    )
    return row["last_sent_at"] if row else None


async def can_send_notification(tg_id: int, notification_type: str, cooldown_hours: int, subscription_id: int | None = None) -> bool:
    """Проверить cooldown уведомления."""
    from datetime import datetime, timedelta

    last_sent_at = await get_notification_last_sent(tg_id, notification_type, subscription_id)
    if not last_sent_at:
        return True

    return datetime.utcnow() - last_sent_at >= timedelta(hours=cooldown_hours)


async def mark_notification_state_sent(tg_id: int, notification_type: str, subscription_id: int | None = None):
    """Записать факт отправки уведомления."""
    await db_execute(
        """
        INSERT INTO notification_state (tg_id, subscription_id, notification_type, last_sent_at, updated_at)
        VALUES ($1, $2, $3, now(), now())
        ON CONFLICT (tg_id, subscription_id, notification_type)
        DO UPDATE SET last_sent_at = now(), updated_at = now()
        """,
        (tg_id, subscription_id or 0, notification_type),
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


# ────────────────────────────────────────────────
#            REFERRAL MANAGEMENT (NEW)
# ────────────────────────────────────────────────

async def add_referral_earning(
    referrer_id: int,
    referred_user_id: int,
    tariff_code: str,
    amount: float,
    is_first_purchase: bool = False
) -> bool:
    """
    Записать реферальный доход

    Args:
        referrer_id: ID рефератора (тот кто пригласил)
        referred_user_id: ID реферала (кого пригласили)
        tariff_code: Код тарифа
        amount: Сумма платежа
        is_first_purchase: Является ли это первой покупкой реферала

    Returns:
        True если успешно
    """
    # Определяем процент: 35% за первую покупку, 15% за последующие
    percentage = 35 if is_first_purchase else 15
    referral_share = amount * percentage / 100

    await db_execute(
        """
        INSERT INTO referral_earnings (referrer_id, referred_user_id, tariff_code, amount, referral_share, is_first_purchase)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        (referrer_id, referred_user_id, tariff_code, amount, referral_share, is_first_purchase)
    )
    return True


async def check_first_referral_purchase(referred_user_id: int, referrer_id: int) -> bool:
    """
    Проверить это первая покупка реферала или повторная

    Args:
        referred_user_id: ID реферала
        referrer_id: ID рефератора

    Returns:
        True если это первая покупка, False если уже были покупки
    """
    result = await db_execute(
        """
        SELECT referrer_id, first_payment
        FROM users
        WHERE tg_id = $1
        """,
        (referred_user_id,),
        fetch_one=True
    )

    if not result:
        return False

    if result['referrer_id'] != referrer_id:
        return False

    return not bool(result['first_payment'])


async def add_referral_without_duplicates(referrer_id: int, referred_user_id: int) -> bool:
    """
    Добавить реферала с проверками:
    1. Рефератор не может быть рефералом себе
    2. Пользователь не может иметь двух рефераторов

    Returns:
        True если добавлено, False если ошибка или дубликат
    """
    # Проверка 1: рефератор не может быть рефералом себе
    if referrer_id == referred_user_id:
        logging.warning(f"Referrer {referrer_id} tried to refer themselves")
        return False

    # Проверка 2: пользователь не должен уже иметь рефератора
    existing_referrer = await db_execute(
        "SELECT referrer_id FROM users WHERE tg_id = $1 AND referrer_id IS NOT NULL",
        (referred_user_id,),
        fetch_one=True
    )

    if existing_referrer is not None:
        logging.warning(
            f"User {referred_user_id} already has a referrer {existing_referrer['referrer_id']}, "
            f"cannot assign referrer {referrer_id}"
        )
        return False

    try:
        user = await get_user(referred_user_id)

        if user is None:
            # Для нового пользователя привязка будет записана при create_user(..., referrer_id)
            return True

        existing_referrer = user.get('referrer_id')
        if existing_referrer is not None:
            logging.warning(
                f"User {referred_user_id} already has a referrer {existing_referrer}, "
                f"cannot assign referrer {referrer_id}"
            )
            return existing_referrer == referrer_id

        await db_execute(
            """
            UPDATE users
            SET referrer_id = $1
            WHERE tg_id = $2 AND referrer_id IS NULL
            """,
            (referrer_id, referred_user_id)
        )

        return True
    except Exception as e:
        logging.error(f"Error checking referral: {e}")
        return False


async def get_referral_stats(referrer_id: int) -> dict:
    """
    Получить полную статистику рефератора

    Returns:
        Словарь с:
        - active_referrals: количество активных рефералов
        - earnings_by_tariff: заработки по каждому тарифу
        - total_earned: всего заработано
        - total_withdrawn: всего выведено
        - current_balance: текущий баланс
    """
    # Активные рефералы (те у кого есть хотя бы одна покупка)
    active_referrals = await db_execute(
        """
        SELECT COUNT(DISTINCT referred_user_id) as count
        FROM referral_earnings
        WHERE referrer_id = $1
        """,
        (referrer_id,),
        fetch_one=True
    )

    # Заработок по тарифам
    earnings = await db_execute(
        """
        SELECT
            tariff_code,
            COUNT(*) as purchase_count,
            SUM(referral_share) as total_share
        FROM referral_earnings
        WHERE referrer_id = $1
        GROUP BY tariff_code
        ORDER BY tariff_code
        """,
        (referrer_id,),
        fetch_all=True
    )

    # Общий заработок
    total_earned = await db_execute(
        "SELECT SUM(referral_share) as total FROM referral_earnings WHERE referrer_id = $1",
        (referrer_id,),
        fetch_one=True
    )

    # Всего выведено/зарезервировано на вывод
    total_withdrawn = await db_execute(
        "SELECT SUM(amount) as total FROM referral_withdrawals WHERE referrer_id = $1",
        (referrer_id,),
        fetch_one=True
    )

    earned = float(total_earned['total'] or 0)
    withdrawn = float(total_withdrawn['total'] or 0)
    balance = earned - withdrawn

    return {
        'active_referrals': active_referrals['count'] if active_referrals else 0,
        'earnings_by_tariff': earnings or [],
        'total_earned': earned,
        'total_withdrawn': withdrawn,
        'current_balance': balance
    }


async def create_referral_withdrawal_request(
    referrer_id: int,
    amount: float,
    withdrawal_type: str,
    **kwargs
) -> bool:
    """
    Создать запрос на вывод средств для реферала

    Args:
        referrer_id: ID рефератора
        amount: Сумма вывода
        withdrawal_type: Тип вывода (sbp или usdt)
        **kwargs: дополнительные параметры (bank_name, phone_number, usdt_address)
    """
    bank_name = kwargs.get('bank_name')
    phone_number = kwargs.get('phone_number')
    usdt_address = kwargs.get('usdt_address')

    await db_execute(
        """
        INSERT INTO referral_withdrawals (referrer_id, amount, withdrawal_type, bank_name, phone_number, usdt_address)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        (referrer_id, amount, withdrawal_type, bank_name, phone_number, usdt_address)
    )
    return True


async def spend_referral_balance_for_subscription(
    referrer_id: int,
    amount: float,
    tariff_code: str
) -> bool:
    """Списать баланс рефералов за оплату подписки"""
    await db_execute(
        """
        INSERT INTO referral_withdrawals (referrer_id, amount, withdrawal_type, status)
        VALUES ($1, $2, $3, 'completed')
        """,
        (referrer_id, amount, f'subscription_{tariff_code}')
    )
    return True


async def get_pending_referral_withdrawals():
    """Получить все ожидающие запросы на вывод от рефералов"""
    return await db_execute(
        """
        SELECT
            rw.id,
            rw.referrer_id,
            rw.amount,
            rw.withdrawal_type,
            rw.bank_name,
            rw.phone_number,
            rw.usdt_address,
            u.username
        FROM referral_withdrawals rw
        LEFT JOIN users u ON rw.referrer_id = u.tg_id
        WHERE rw.status = 'pending'
        ORDER BY rw.created_at ASC
        """,
        fetch_all=True
    )


async def mark_referral_withdrawal_completed(withdrawal_id: int):
    """Отметить реферальный вывод как выполненный"""
    await db_execute(
        "UPDATE referral_withdrawals SET status = 'completed' WHERE id = $1",
        (withdrawal_id,)
    )


# ────────────────────────────────────────────────
#                WEB ADMIN PANEL
# ────────────────────────────────────────────────

async def admin_dashboard_stats():
    """Ключевые показатели для веб-панели."""
    return await db_execute(
        """
        SELECT
            (SELECT COUNT(*) FROM users) AS total_users,
            (SELECT COUNT(*) FROM users WHERE created_at >= now() - interval '7 days') AS new_users_7d,
            (SELECT COUNT(*) FROM subscriptions WHERE is_visible = TRUE AND subscription_until > now() AT TIME ZONE 'UTC') AS active_subscriptions,
            (SELECT COUNT(*) FROM payments WHERE status = 'paid') AS paid_payments,
            (SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid') AS total_revenue,
            (SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid' AND updated_at >= now() - interval '30 days') AS revenue_30d
        """,
        fetch_one=True,
    )


async def admin_list_users(search: str = "", limit: int = 50, offset: int = 0):
    pattern = f"%{search.strip()}%"
    where = "WHERE CAST(u.tg_id AS TEXT) ILIKE $1 OR COALESCE(u.username, '') ILIKE $1 OR COALESCE(wa.login, '') ILIKE $1" if search.strip() else ""
    params = (pattern, limit, offset) if search.strip() else (limit, offset)
    limit_param = "$2" if search.strip() else "$1"
    offset_param = "$3" if search.strip() else "$2"

    users = await db_execute(
        f"""
        SELECT
            u.tg_id,
            u.username,
            wa.login AS web_login,
            CASE WHEN wa.id IS NULL THEN 'telegram' ELSE 'web' END AS account_type,
            u.created_at,
            u.tracking_code,
            u.first_payment,
            (SELECT COUNT(*) FROM subscriptions s WHERE s.tg_id = u.tg_id AND s.is_visible = TRUE) AS subscriptions_count,
            (SELECT COUNT(*) FROM subscriptions s WHERE s.tg_id = u.tg_id AND s.is_visible = TRUE AND s.subscription_until > now() AT TIME ZONE 'UTC') AS active_subscriptions,
            (SELECT MAX(s.subscription_until) FROM subscriptions s WHERE s.tg_id = u.tg_id AND s.is_visible = TRUE) AS latest_expiry,
            (SELECT COALESCE(SUM(p.amount), 0) FROM payments p WHERE p.tg_id = u.tg_id AND p.status = 'paid') AS revenue
        FROM users u
        LEFT JOIN web_accounts wa ON wa.service_user_id = u.tg_id
        {where}
        ORDER BY u.created_at DESC, u.tg_id DESC
        LIMIT {limit_param} OFFSET {offset_param}
        """,
        params,
        fetch_all=True,
    )

    count_params = (pattern,) if search.strip() else ()
    total = await db_execute(
        f"SELECT COUNT(*) AS count FROM users u LEFT JOIN web_accounts wa ON wa.service_user_id = u.tg_id {where}",
        count_params,
        fetch_one=True,
    )
    return {"items": users or [], "total": total["count"] if total else 0}


async def admin_get_user_bundle(tg_id: int):
    user = await get_user(tg_id)
    if not user:
        return None
    subscriptions = await get_user_subscriptions(tg_id)
    payments = await db_execute(
        """
        SELECT id, tariff_code, amount, provider, status, payment_kind, created_at, updated_at
        FROM payments
        WHERE tg_id = $1
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (tg_id,),
        fetch_all=True,
    )
    web_account = await db_execute(
        "SELECT id, login, is_active, created_at, last_login_at FROM web_accounts WHERE service_user_id = $1 LIMIT 1",
        (tg_id,),
        fetch_one=True,
    )
    return {"user": user, "web_account": web_account, "subscriptions": subscriptions or [], "payments": payments or []}


async def list_promo_codes():
    return await db_execute(
        """
        SELECT code, days, max_uses, used_count, active, created_at
        FROM promo_codes
        ORDER BY created_at DESC, code ASC
        """,
        fetch_all=True,
    )


async def set_promo_code_active(code: str, active: bool) -> bool:
    result = await db_execute(
        "UPDATE promo_codes SET active = $2 WHERE code = $1 RETURNING 1",
        (code.upper(), active),
        fetch_one=True,
    )
    return result is not None


async def list_tracking_links_with_stats():
    return await db_execute(
        """
        SELECT
            l.code,
            l.title,
            l.is_active,
            l.created_at,
            (SELECT COUNT(*) FROM tracking_link_clicks c WHERE c.code = l.code) AS clicks,
            (SELECT COUNT(DISTINCT c.tg_id) FROM tracking_link_clicks c WHERE c.code = l.code) AS unique_clicks,
            (SELECT COUNT(*) FROM users u WHERE u.tracking_code = l.code) AS users_count,
            (SELECT COALESCE(SUM(p.amount), 0) FROM payments p WHERE p.tracking_code = l.code AND p.status = 'paid') AS revenue
        FROM tracking_links l
        ORDER BY l.created_at DESC, l.code ASC
        """,
        fetch_all=True,
    )


async def create_discount(
    name: str,
    discount_type: str,
    value: float,
    target_type: str,
    target_code: str | None,
    starts_at,
    ends_at,
):
    return await db_execute(
        """
        INSERT INTO discounts (name, discount_type, value, target_type, target_code, starts_at, ends_at, active)
        VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
        RETURNING *
        """,
        (name, discount_type, value, target_type, target_code, starts_at, ends_at),
        fetch_one=True,
    )


async def list_discounts():
    return await db_execute(
        "SELECT * FROM discounts ORDER BY created_at DESC, id DESC",
        fetch_all=True,
    )


async def get_active_discounts(at_time=None):
    at_time = at_time or datetime.utcnow()
    return await db_execute(
        """
        SELECT * FROM discounts
        WHERE active = TRUE AND starts_at <= $1 AND ends_at >= $1
        ORDER BY value DESC, id DESC
        """,
        (at_time,),
        fetch_all=True,
    )


async def set_discount_active(discount_id: int, active: bool) -> bool:
    result = await db_execute(
        "UPDATE discounts SET active = $2, updated_at = now() WHERE id = $1 RETURNING 1",
        (discount_id, active),
        fetch_one=True,
    )
    return result is not None


async def delete_discount(discount_id: int) -> bool:
    result = await db_execute(
        "DELETE FROM discounts WHERE id = $1 RETURNING 1",
        (discount_id,),
        fetch_one=True,
    )
    return result is not None
