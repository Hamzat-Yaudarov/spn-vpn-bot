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

                    -- Партнёрская программа
                    is_partner BOOLEAN DEFAULT FALSE,
                    partnership_percent INT,
                    partnership_started TIMESTAMP,
                    partnership_until TIMESTAMP,
                    partnership_accepted BOOLEAN DEFAULT FALSE,
                    partner_balance NUMERIC DEFAULT 0,
                    partner_earned_total NUMERIC DEFAULT 0,
                    partner_withdrawn_total NUMERIC DEFAULT 0,

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

            # Таблица партнёрских ссылок и трафика
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS partnership_links (
                    id BIGSERIAL PRIMARY KEY,
                    partner_tg_id BIGINT NOT NULL UNIQUE,
                    referred_tg_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT now(),
                    FOREIGN KEY (partner_tg_id) REFERENCES users(tg_id) ON DELETE CASCADE,
                    FOREIGN KEY (referred_tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'partnership_links' создана или уже существует")

            # Таблица партнёрских доходов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS partnership_earnings (
                    id BIGSERIAL PRIMARY KEY,
                    partner_tg_id BIGINT NOT NULL,
                    referred_tg_id BIGINT NOT NULL,
                    tariff_code TEXT NOT NULL,
                    amount NUMERIC NOT NULL,
                    commission NUMERIC NOT NULL,
                    payment_invoice_id TEXT,
                    created_at TIMESTAMP DEFAULT now(),
                    FOREIGN KEY (partner_tg_id) REFERENCES users(tg_id) ON DELETE CASCADE,
                    FOREIGN KEY (referred_tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'partnership_earnings' создана или уже существует")

            # Таблица партнёрских выводов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS partnership_withdrawals (
                    id BIGSERIAL PRIMARY KEY,
                    partner_tg_id BIGINT NOT NULL,
                    amount NUMERIC NOT NULL,
                    withdrawal_type TEXT NOT NULL,
                    bank_name TEXT,
                    phone_number TEXT,
                    usdt_address TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT now(),
                    FOREIGN KEY (partner_tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
                )
            """)
            logging.info("✅ Таблица 'partnership_withdrawals' создана или уже существует")

            # ═══════════════════════════════════════════════════════════
            # ЭТАП 2: СОЗДАНИЕ ИНДЕКСОВ (для быстрого поиска)
            # ═══════════════════════════════════════════════════════════

            index_queries = [
                # users индексы
                "CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_users_remnawave_uuid ON users(remnawave_uuid);",
                "CREATE INDEX IF NOT EXISTS idx_users_referrer_id ON users(referrer_id);",
                "CREATE INDEX IF NOT EXISTS idx_users_next_notification ON users(next_notification_time) WHERE next_notification_time IS NOT NULL;",
                "CREATE INDEX IF NOT EXISTS idx_users_is_partner ON users(is_partner) WHERE is_partner = TRUE;",

                # payments индексы
                "CREATE INDEX IF NOT EXISTS idx_payments_tg_id ON payments(tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);",
                "CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider);",
                "CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);",

                # promo_codes индексы
                "CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);",

                # partnership indixes
                "CREATE INDEX IF NOT EXISTS idx_partnership_links_partner ON partnership_links(partner_tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_partnership_links_referred ON partnership_links(referred_tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_partnership_earnings_partner ON partnership_earnings(partner_tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_partnership_earnings_referred ON partnership_earnings(referred_tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_partnership_withdrawals_partner ON partnership_withdrawals(partner_tg_id);",
                "CREATE INDEX IF NOT EXISTS idx_partnership_withdrawals_status ON partnership_withdrawals(status);",
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
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS next_notification_time TIMESTAMP;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_type TEXT;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_partner BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS partnership_percent INT;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS partnership_started TIMESTAMP;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS partnership_until TIMESTAMP;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS partnership_accepted BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS partner_balance NUMERIC DEFAULT 0;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS partner_earned_total NUMERIC DEFAULT 0;",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS partner_withdrawn_total NUMERIC DEFAULT 0;",
                "ALTER TABLE partnership_withdrawals ADD COLUMN IF NOT EXISTS phone_number TEXT;",
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

async def update_subscription(tg_id: int, uuid: str, username: str, subscription_until, squad_uuid: str):
    """
    Обновить подписку пользователя

    Также автоматически устанавливает время для уведомления о заканчивающейся подписке
    """
    from datetime import datetime, timedelta

    # Рассчитываем время следующего уведомления
    next_notification = None
    notification_type = None

    if subscription_until:
        now = datetime.utcnow()
        time_left = subscription_until - now
        total_hours = time_left.total_seconds() / 3600

        if total_hours > 36:  # Больше чем 1.5 дня
            # Первое уведомление за 1.5 дня до конца
            next_notification = subscription_until - timedelta(days=1.5)
            notification_type = "1day_left"
        elif total_hours > 0:
            # Подписка в пределах 36 часов, отправляем уведомление "below1day" сейчас
            next_notification = now
            notification_type = "below1day"
        else:
            # Подписка уже истекла
            next_notification = now
            notification_type = "expired"

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
#        SUBSCRIPTION NOTIFICATION MANAGEMENT
# ────────────────────────────────────────────────

async def get_users_needing_notification():
    """
    Получить пользователей которым нужно отправить уведомление о заканчивающейся подписке

    Returns:
        Список пользователей у которых next_notification_time <= now и есть активная подписка
    """
    from datetime import datetime

    return await db_execute(
        """
        SELECT tg_id, remnawave_uuid, subscription_until, notification_type
        FROM users
        WHERE next_notification_time IS NOT NULL
        AND next_notification_time <= $1
        AND subscription_until IS NOT NULL
        ORDER BY next_notification_time ASC
        """,
        (datetime.utcnow(),),
        fetch_all=True
    )


async def mark_notification_sent(tg_id: int):
    """Отметить что уведомление было отправлено пользователю и установить следующее"""
    from datetime import datetime, timedelta

    # Получаем текущую подписку пользователя
    user = await get_user(tg_id)
    if not user or not user.get('subscription_until'):
        # Если подписки нет, очищаем поле уведомления
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
    current_type = user.get('notification_type')

    # Определяем следующее уведомление в зависимости от текущего типа
    next_notification = None
    next_type = None

    if current_type == "1day_left":
        # Переходим к уведомлению "below1day" (за 1 день до конца)
        next_notification_time = subscription_until - timedelta(days=1)
        # Но проверяем, не прошло ли это время уже
        if next_notification_time > now:
            next_notification = next_notification_time
            next_type = "below1day"
        else:
            # Если время уже прошло, отправляем "below1day" в следующем цикле (через 30 минут + 1 секунда)
            # Это предотвращает множественные отправки в цикле
            next_notification = now + timedelta(seconds=1)
            next_type = "below1day"

    elif current_type == "below1day":
        # Переходим к уведомлению "expired" (в момент окончания подписки)
        next_notification_time = subscription_until
        # Но проверяем, не прошло ли это время уже
        if next_notification_time > now:
            next_notification = next_notification_time
            next_type = "expired"
        else:
            # Если время уже прошло, отправляем "expired" в следующем цикле
            next_notification = now + timedelta(seconds=1)
            next_type = "expired"

    elif current_type == "expired":
        # После уведомления об истечении, очищаем полностью
        # Это гарантирует, что "expired" отправляется только один раз
        next_notification = None
        next_type = None

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
#           PARTNERSHIP MANAGEMENT
# ────────────────────────────────────────────────

async def enable_partnership(tg_id: int, percent: int, days: int = 90):
    """
    Включить партнёрство для пользователя

    Args:
        tg_id: ID пользователя
        percent: % доля партнёра
        days: Количество дней на которое включить партнёрство (по умолчанию 90 дней)
    """
    from datetime import datetime, timedelta

    started = datetime.utcnow()
    until = started + timedelta(days=days)

    await db_execute(
        """
        UPDATE users
        SET is_partner = TRUE,
            partnership_percent = $1,
            partnership_started = $2,
            partnership_until = $3,
            partnership_accepted = FALSE
        WHERE tg_id = $4
        """,
        (percent, started, until, tg_id)
    )


async def extend_partnership(tg_id: int, days: int):
    """Продлить партнёрство на указанное количество дней"""
    from datetime import timedelta

    await db_execute(
        """
        UPDATE users
        SET partnership_until = partnership_until + INTERVAL '1 day' * $1
        WHERE tg_id = $2 AND is_partner = TRUE
        """,
        (days, tg_id)
    )


async def get_partner_info(tg_id: int):
    """Получить информацию о партнёре"""
    return await db_execute(
        """
        SELECT
            tg_id, partnership_percent, partnership_started, partnership_until,
            partnership_accepted, partner_balance, partner_earned_total, partner_withdrawn_total
        FROM users
        WHERE tg_id = $1 AND is_partner = TRUE
        """,
        (tg_id,),
        fetch_one=True
    )


async def accept_partnership_agreement(tg_id: int):
    """Отметить что партнёр принял соглашение"""
    await db_execute(
        """
        UPDATE users
        SET partnership_accepted = TRUE
        WHERE tg_id = $1
        """,
        (tg_id,)
    )


async def register_partnership_link(partner_tg_id: int, referred_tg_id: int):
    """Зарегистрировать партнёрскую ссылку (новый пользователь пришёл от партнёра)"""
    await db_execute(
        """
        INSERT INTO partnership_links (partner_tg_id, referred_tg_id)
        VALUES ($1, $2)
        ON CONFLICT (referred_tg_id) DO NOTHING
        """,
        (partner_tg_id, referred_tg_id)
    )


async def get_partnership_stats(partner_tg_id: int):
    """Получить статистику партнёра по привлечённым пользователям и покупкам"""
    from datetime import datetime

    stats = await db_execute(
        """
        SELECT
            COUNT(DISTINCT pl.referred_tg_id)::INT as total_users,
            COUNT(CASE WHEN pe.tariff_code = '1m' THEN 1 END)::INT as purchases_1m,
            COUNT(CASE WHEN pe.tariff_code = '3m' THEN 1 END)::INT as purchases_3m,
            COUNT(CASE WHEN pe.tariff_code = '6m' THEN 1 END)::INT as purchases_6m,
            COUNT(CASE WHEN pe.tariff_code = '12m' THEN 1 END)::INT as purchases_12m,
            COALESCE(SUM(pe.commission), 0)::NUMERIC as total_earned
        FROM partnership_links pl
        LEFT JOIN partnership_earnings pe ON pl.partner_tg_id = pe.partner_tg_id AND pl.referred_tg_id = pe.referred_tg_id
        WHERE pl.partner_tg_id = $1
        """,
        (partner_tg_id,),
        fetch_one=True
    )

    return stats


async def add_partnership_earnings(partner_tg_id: int, referred_tg_id: int, tariff_code: str, amount: float, commission: float, payment_invoice_id: str):
    """Добавить заработок партнёру за покупку привлечённого им пользователя"""
    from datetime import datetime

    await db_execute(
        """
        INSERT INTO partnership_earnings (partner_tg_id, referred_tg_id, tariff_code, amount, commission, payment_invoice_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        (partner_tg_id, referred_tg_id, tariff_code, amount, commission, payment_invoice_id)
    )

    # Обновляем баланс партнёра
    await db_execute(
        """
        UPDATE users
        SET partner_balance = partner_balance + $1,
            partner_earned_total = partner_earned_total + $1
        WHERE tg_id = $2
        """,
        (commission, partner_tg_id)
    )


async def create_withdrawal_request(partner_tg_id: int, amount: float, withdrawal_type: str, bank_name: str = None, phone_number: str = None, usdt_address: str = None):
    """Создать запрос на вывод средств"""
    from datetime import datetime

    await db_execute(
        """
        INSERT INTO partnership_withdrawals (partner_tg_id, amount, withdrawal_type, bank_name, phone_number, usdt_address)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        (partner_tg_id, amount, withdrawal_type, bank_name, phone_number, usdt_address)
    )

    # Вычитаем из баланса
    await db_execute(
        """
        UPDATE users
        SET partner_balance = partner_balance - $1,
            partner_withdrawn_total = partner_withdrawn_total + $1
        WHERE tg_id = $2
        """,
        (amount, partner_tg_id)
    )


async def get_pending_withdrawals():
    """Получить все ожидающие выводы"""
    return await db_execute(
        """
        SELECT id, partner_tg_id, amount, withdrawal_type, bank_name, usdt_address, created_at
        FROM partnership_withdrawals
        WHERE status = 'pending'
        ORDER BY created_at
        """,
        fetch_all=True
    )
