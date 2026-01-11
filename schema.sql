-- Удаляем таблицы если они существуют (опционально)
-- DROP TABLE IF EXISTS promo_codes CASCADE;
-- DROP TABLE IF EXISTS payments CASCADE;
-- DROP TABLE IF EXISTS users CASCADE;

-- ────────────────────────────────────────────────
--                    USERS TABLE
-- ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    tg_id BIGINT PRIMARY KEY,
    username TEXT,
    accepted_terms BOOLEAN DEFAULT FALSE,
    remnawave_uuid TEXT UNIQUE,
    remnawave_username TEXT,
    subscription_until TEXT,
    squad_uuid TEXT,
    referrer_id BIGINT REFERENCES users(tg_id) ON DELETE SET NULL,
    gift_received BOOLEAN DEFAULT FALSE,
    referral_count INTEGER DEFAULT 0,
    active_referrals INTEGER DEFAULT 0,
    first_payment BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Индексы для оптимизации запросов
CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);
CREATE INDEX IF NOT EXISTS idx_users_remnawave_uuid ON users(remnawave_uuid);
CREATE INDEX IF NOT EXISTS idx_users_referrer_id ON users(referrer_id);


-- ────────────────────────────────────────────────
--                  PAYMENTS TABLE
-- ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    tariff_code TEXT NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    provider TEXT,
    invoice_id TEXT UNIQUE,
    payload TEXT
);

-- Индексы для оптимизации запросов
CREATE INDEX IF NOT EXISTS idx_payments_tg_id ON payments(tg_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider);
CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id);


-- ────────────────────────────────────────────────
--               PROMO CODES TABLE
-- ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    days INTEGER NOT NULL,
    max_uses INTEGER NOT NULL,
    used_count INTEGER DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Индексы для оптимизации запросов
CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);
CREATE INDEX IF NOT EXISTS idx_promo_codes_active ON promo_codes(active);


-- ────────────────────────────────────────────────
--          TRIGGER FOR UPDATED_AT (опционально)
-- ────────────────────────────────────────────────

-- Функция для обновления updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Триггер для users
CREATE TRIGGER update_users_updated_at BEFORE UPDATE
    ON users FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Триггер для promo_codes
CREATE TRIGGER update_promo_codes_updated_at BEFORE UPDATE
    ON promo_codes FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
