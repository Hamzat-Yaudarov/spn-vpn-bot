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
    tracking_code TEXT,
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
);

CREATE TABLE IF NOT EXISTS web_sessions (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    last_seen_at TIMESTAMP DEFAULT now()
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
    payment_kind TEXT DEFAULT 'subscription',
    traffic_package_code TEXT,
    tracking_code TEXT,
    refund_requested_at TIMESTAMP,
    refund_status TEXT,
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
);

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
);

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
);

CREATE TABLE IF NOT EXISTS tracking_links (
    id BIGSERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    title TEXT,
    created_by BIGINT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tracking_link_clicks (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL,
    tg_id BIGINT NOT NULL,
    is_new_user BOOLEAN DEFAULT FALSE,
    clicked_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);
CREATE INDEX IF NOT EXISTS idx_web_accounts_login ON web_accounts(login);
CREATE INDEX IF NOT EXISTS idx_web_accounts_service_user ON web_accounts(service_user_id);
CREATE INDEX IF NOT EXISTS idx_web_accounts_tracking_code ON web_accounts(tracking_code);
CREATE INDEX IF NOT EXISTS idx_web_sessions_token ON web_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_users_remnawave_uuid ON users(remnawave_uuid);
CREATE INDEX IF NOT EXISTS idx_users_referrer_id ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_users_tracking_code ON users(tracking_code);
CREATE INDEX IF NOT EXISTS idx_users_next_notification ON users(next_notification_time) WHERE next_notification_time IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_partnerships_tg_id ON partnerships(tg_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_tg_id ON subscriptions(tg_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_uuid ON subscriptions(remnawave_uuid);
CREATE INDEX IF NOT EXISTS idx_subscriptions_kind_visible ON subscriptions(tg_id, plan_kind, is_visible);
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
CREATE INDEX IF NOT EXISTS idx_payments_tracking_code ON payments(tracking_code);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);

CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);
CREATE INDEX IF NOT EXISTS idx_discounts_active_period ON discounts(active, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_promo_code_users_tg_id ON promo_code_users(tg_id);
CREATE INDEX IF NOT EXISTS idx_promo_code_users_code ON promo_code_users(promo_code);
CREATE INDEX IF NOT EXISTS idx_traffic_purchases_subscription_id ON traffic_purchases(subscription_id);
CREATE INDEX IF NOT EXISTS idx_traffic_cycles_subscription_id ON subscription_traffic_cycles(subscription_id);
CREATE INDEX IF NOT EXISTS idx_tracking_links_code ON tracking_links(code);
CREATE INDEX IF NOT EXISTS idx_tracking_clicks_code ON tracking_link_clicks(code);
CREATE INDEX IF NOT EXISTS idx_tracking_clicks_tg_id ON tracking_link_clicks(tg_id);
