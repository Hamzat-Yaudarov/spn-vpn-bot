-- Local/staging white-label checkout, separate from every existing SPN table.
-- Apply AFTER schema.sql, explicitly; never migrated automatically on startup.
BEGIN;

CREATE TABLE IF NOT EXISTS wl_customers (
    id UUID NOT NULL UNIQUE,
    project_id UUID NOT NULL REFERENCES wl_projects(id) ON DELETE RESTRICT,
    tg_id BIGINT NOT NULL CHECK (tg_id > 0),
    referrer_tg_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id, tg_id),
    FOREIGN KEY(project_id, referrer_tg_id) REFERENCES wl_customers(project_id, tg_id) ON DELETE RESTRICT,
    CHECK (referrer_tg_id IS NULL OR referrer_tg_id <> tg_id)
);

CREATE TABLE IF NOT EXISTS wl_subscriptions (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    tg_id BIGINT NOT NULL,
    expires_at TIMESTAMPTZ,
    desired_expires_at TIMESTAMPTZ,
    traffic_limit_bytes BIGINT NOT NULL DEFAULT 322122547200 CHECK (traffic_limit_bytes >= 322122547200),
    device_limit INTEGER NOT NULL DEFAULT 3 CHECK (device_limit BETWEEN 3 AND 10),
    remote_user_id TEXT,
    subscription_url TEXT,
    last_activation_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_activation_sequence >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(project_id,tg_id) REFERENCES wl_customers(project_id,tg_id) ON DELETE RESTRICT,
    UNIQUE(project_id,tg_id),
    UNIQUE(project_id,id),
    UNIQUE(project_id,id,tg_id),
    CHECK (expires_at IS NULL OR (desired_expires_at IS NOT NULL AND expires_at <= desired_expires_at))
);

CREATE TABLE IF NOT EXISTS wl_orders (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    tg_id BIGINT NOT NULL,
    subscription_id UUID NOT NULL,
    request_key UUID NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    price_kopecks BIGINT NOT NULL CHECK (price_kopecks BETWEEN 19900 AND 10000000),
    days INTEGER NOT NULL DEFAULT 30 CHECK (days=30),
    currency TEXT NOT NULL DEFAULT 'RUB' CHECK (currency='RUB'),
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created','canceled','paid_pending_activation','active')),
    provider_payment_id TEXT UNIQUE,
    confirmation_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at TIMESTAMPTZ,
    UNIQUE(project_id,tg_id,request_key),
    UNIQUE(project_id,id),
    FOREIGN KEY(project_id,subscription_id,tg_id) REFERENCES wl_subscriptions(project_id,id,tg_id) ON DELETE RESTRICT,
    CHECK ((provider_payment_id IS NULL AND confirmation_url IS NULL)
        OR (char_length(provider_payment_id) BETWEEN 1 AND 200 AND (confirmation_url='' OR confirmation_url ~ '^https://'))),
    CHECK ((status IN ('created','canceled') AND paid_at IS NULL)
        OR (status IN ('paid_pending_activation','active') AND paid_at IS NOT NULL AND provider_payment_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS wl_orders_project_paid ON wl_orders(project_id,paid_at DESC);

CREATE TABLE IF NOT EXISTS wl_activation_jobs (
    sequence BIGSERIAL UNIQUE,
    project_id UUID NOT NULL,
    order_id UUID NOT NULL,
    subscription_id UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    traffic_limit_bytes BIGINT NOT NULL CHECK (traffic_limit_bytes >= 322122547200),
    device_limit INTEGER NOT NULL CHECK (device_limit BETWEEN 3 AND 10),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','retry','complete')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts>=0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY(project_id,order_id),
    FOREIGN KEY(project_id,order_id) REFERENCES wl_orders(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,subscription_id) REFERENCES wl_subscriptions(project_id,id) ON DELETE RESTRICT,
    CHECK ((status='complete')=(completed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS wl_activation_jobs_subscription ON wl_activation_jobs(project_id,subscription_id,sequence);

CREATE TABLE IF NOT EXISTS wl_revenue_ledger (
    project_id UUID NOT NULL,
    order_id UUID NOT NULL,
    amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks>0),
    partner_kopecks BIGINT NOT NULL CHECK (partner_kopecks>=0),
    platform_kopecks BIGINT NOT NULL CHECK (platform_kopecks>=0),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(project_id,order_id),
    FOREIGN KEY(project_id,order_id) REFERENCES wl_orders(project_id,id) ON DELETE RESTRICT,
    CHECK (partner_kopecks+platform_kopecks=amount_kopecks)
);

CREATE TABLE IF NOT EXISTS wl_customer_outbox (
    id BIGSERIAL PRIMARY KEY,
    project_id UUID NOT NULL,
    order_id UUID NOT NULL,
    tg_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK(kind='purchase_paid'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,order_id,kind),
    FOREIGN KEY(project_id,order_id) REFERENCES wl_orders(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,tg_id) REFERENCES wl_customers(project_id,tg_id) ON DELETE RESTRICT
);
COMMIT;
