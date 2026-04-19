-- Базовая схема PostgreSQL/Supabase для SPN VPN Bot.
-- Runtime-миграции в database.py остаются источником истины для дообновления схемы.

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    tg_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    accepted_terms BOOLEAN DEFAULT FALSE,
    remnawave_uuid UUID,
    remnawave_username TEXT,
    subscription_until TIMESTAMP,
    squad_uuid UUID,
    referrer_id BIGINT,
    first_payment BOOLEAN DEFAULT FALSE,
    referral_count INT DEFAULT 0,
    active_referrals INT DEFAULT 0,
    gift_received BOOLEAN DEFAULT FALSE,
    next_notification_time TIMESTAMP,
    notification_type TEXT,
    last_gift_attempt TIMESTAMP,
    last_promo_attempt TIMESTAMP,
    last_payment_check TIMESTAMP
);

CREATE TABLE IF NOT EXISTS partnerships (
    id BIGSERIAL PRIMARY KEY,
    tg_id BIGINT UNIQUE NOT NULL,
    percentage INT NOT NULL,
    agreement_accepted BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id BIGSERIAL PRIMARY KEY,
    tg_id BIGINT NOT NULL,
    slot_number INT NOT NULL,
    remnawave_uuid UUID,
    remnawave_username TEXT,
    subscription_until TIMESTAMP,
    squad_uuid UUID,
    is_active BOOLEAN DEFAULT TRUE,
    next_notification_time TIMESTAMP,
    notification_type TEXT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(tg_id, slot_number)
);

CREATE TABLE IF NOT EXISTS partner_referrals (
    id BIGSERIAL PRIMARY KEY,
    partner_id BIGINT NOT NULL,
    referred_user_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE(partner_id, referred_user_id),
    FOREIGN KEY (partner_id) REFERENCES partnerships(tg_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS partner_earnings (
    id BIGSERIAL PRIMARY KEY,
    partner_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    tariff_code TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    partner_share NUMERIC NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    FOREIGN KEY (partner_id) REFERENCES partnerships(tg_id) ON DELETE CASCADE
);

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
);

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
);

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
);

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
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS promo_codes (
    id BIGSERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    days INT NOT NULL,
    max_uses INT NOT NULL,
    used_count INT DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS promo_code_users (
    id BIGSERIAL PRIMARY KEY,
    tg_id BIGINT NOT NULL,
    promo_code TEXT NOT NULL,
    used_at TIMESTAMP DEFAULT now(),
    UNIQUE(tg_id, promo_code),
    FOREIGN KEY (promo_code) REFERENCES promo_codes(code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);
CREATE INDEX IF NOT EXISTS idx_users_remnawave_uuid ON users(remnawave_uuid);
CREATE INDEX IF NOT EXISTS idx_users_referrer_id ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_users_next_notification ON users(next_notification_time) WHERE next_notification_time IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_partnerships_tg_id ON partnerships(tg_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_tg_id ON subscriptions(tg_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_uuid ON subscriptions(remnawave_uuid);
CREATE INDEX IF NOT EXISTS idx_subscriptions_notification ON subscriptions(next_notification_time) WHERE next_notification_time IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_partner_referrals_partner_id ON partner_referrals(partner_id);
CREATE INDEX IF NOT EXISTS idx_partner_earnings_partner_id ON partner_earnings(partner_id);
CREATE INDEX IF NOT EXISTS idx_partner_withdrawals_partner_id ON partner_withdrawals(partner_id);

CREATE INDEX IF NOT EXISTS idx_referral_earnings_referrer_id ON referral_earnings(referrer_id);
CREATE INDEX IF NOT EXISTS idx_referral_earnings_referred_user_id ON referral_earnings(referred_user_id);
CREATE INDEX IF NOT EXISTS idx_referral_withdrawals_referrer_id ON referral_withdrawals(referrer_id);

CREATE INDEX IF NOT EXISTS idx_payments_tg_id ON payments(tg_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider);
CREATE INDEX IF NOT EXISTS idx_payments_subscription_id ON payments(subscription_id);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);

CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);
CREATE INDEX IF NOT EXISTS idx_promo_code_users_tg_id ON promo_code_users(tg_id);
CREATE INDEX IF NOT EXISTS idx_promo_code_users_code ON promo_code_users(promo_code);
