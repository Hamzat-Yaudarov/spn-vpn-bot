-- Explicit local/staging extension AFTER schema.sql and commerce.sql.
-- No main SPN tables or historical balances are modified.
BEGIN;

ALTER TABLE wl_orders ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'subscription';
ALTER TABLE wl_orders ADD COLUMN IF NOT EXISTS package_id TEXT;
ALTER TABLE wl_orders ADD COLUMN IF NOT EXISTS addon_quantity INTEGER;
ALTER TABLE wl_orders ADD COLUMN IF NOT EXISTS addon_expires_at TIMESTAMPTZ;
ALTER TABLE wl_orders ADD COLUMN IF NOT EXISTS activation_issue TEXT;
ALTER TABLE wl_orders DROP CONSTRAINT IF EXISTS wl_orders_price_kopecks_check;
ALTER TABLE wl_orders DROP CONSTRAINT IF EXISTS wl_orders_entitlement_shape;
ALTER TABLE wl_orders ADD CONSTRAINT wl_orders_entitlement_shape CHECK (
    price_kopecks BETWEEN 1 AND 10000000 AND (
        (purpose='subscription' AND price_kopecks>=19900 AND package_id IS NULL
            AND addon_quantity IS NULL AND addon_expires_at IS NULL)
        OR (purpose='traffic' AND package_id IS NOT NULL AND addon_quantity IS NOT NULL
            AND package_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'
            AND addon_quantity BETWEEN 1 AND 100000 AND addon_expires_at IS NULL)
        OR (purpose='devices' AND package_id IS NOT NULL AND addon_quantity IS NOT NULL
            AND package_id IN ('1','2','3') AND package_id=addon_quantity::text
            AND addon_quantity BETWEEN 1 AND 3 AND addon_expires_at IS NOT NULL)
    ));
ALTER TABLE wl_orders DROP CONSTRAINT IF EXISTS wl_orders_activation_issue_check;
ALTER TABLE wl_orders ADD CONSTRAINT wl_orders_activation_issue_check
    CHECK (activation_issue IS NULL OR activation_issue IN ('addon_expired','device_limit','activation_expired'));

-- Use UUIDs derived from existing order UUIDs for old rows. New non-order jobs
-- use random UUIDs; every kind continues to share the original FIFO sequence.
ALTER TABLE wl_activation_jobs ADD COLUMN IF NOT EXISTS job_id UUID;
UPDATE wl_activation_jobs SET job_id=order_id WHERE job_id IS NULL;
ALTER TABLE wl_activation_jobs ALTER COLUMN job_id SET NOT NULL;
ALTER TABLE wl_activation_jobs ALTER COLUMN job_id SET DEFAULT gen_random_uuid();
ALTER TABLE wl_activation_jobs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'order';
ALTER TABLE wl_activation_jobs ADD COLUMN IF NOT EXISTS source_id UUID;
ALTER TABLE wl_activation_jobs ADD COLUMN IF NOT EXISTS blocked_reason TEXT;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='wl_activation_jobs_pkey'
        AND conrelid='wl_activation_jobs'::regclass
        AND pg_get_constraintdef(oid) LIKE '%job_id%') THEN
        ALTER TABLE wl_activation_jobs DROP CONSTRAINT IF EXISTS wl_activation_jobs_pkey;
        ALTER TABLE wl_activation_jobs ADD CONSTRAINT wl_activation_jobs_pkey PRIMARY KEY(project_id,job_id);
    END IF;
END $$;
ALTER TABLE wl_activation_jobs ALTER COLUMN order_id DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS wl_activation_jobs_order_unique ON wl_activation_jobs(project_id,order_id);
CREATE UNIQUE INDEX IF NOT EXISTS wl_activation_jobs_source_unique ON wl_activation_jobs(project_id,kind,source_id);
ALTER TABLE wl_activation_jobs DROP CONSTRAINT IF EXISTS wl_activation_jobs_kind_check;
ALTER TABLE wl_activation_jobs ADD CONSTRAINT wl_activation_jobs_kind_check CHECK (
    (kind='order' AND order_id IS NOT NULL AND source_id IS NULL)
    OR (kind IN ('device_expiry','referral_bonus','traffic_cycle') AND order_id IS NULL AND source_id IS NOT NULL));
ALTER TABLE wl_activation_jobs DROP CONSTRAINT IF EXISTS wl_activation_jobs_blocked_reason_check;
ALTER TABLE wl_activation_jobs ADD CONSTRAINT wl_activation_jobs_blocked_reason_check CHECK (
    blocked_reason IS NULL OR blocked_reason='activation_expired'
    OR (kind='order' AND blocked_reason IN ('addon_expired','device_limit')));

CREATE TABLE IF NOT EXISTS wl_device_grants (
    project_id UUID NOT NULL,
    order_id UUID NOT NULL,
    subscription_id UUID NOT NULL,
    device_count INTEGER NOT NULL CHECK(device_count BETWEEN 1 AND 3),
    expires_at TIMESTAMPTZ NOT NULL,
    expiry_job_id UUID,
    PRIMARY KEY(project_id,order_id),
    FOREIGN KEY(project_id,order_id) REFERENCES wl_orders(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,subscription_id) REFERENCES wl_subscriptions(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,expiry_job_id) REFERENCES wl_activation_jobs(project_id,job_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS wl_device_grants_expiry ON wl_device_grants(expires_at) WHERE expiry_job_id IS NULL;

CREATE TABLE IF NOT EXISTS wl_referral_bonus_grants (
    id UUID NOT NULL UNIQUE,
    project_id UUID NOT NULL,
    referred_tg_id BIGINT NOT NULL,
    referrer_tg_id BIGINT NOT NULL,
    source_order_id UUID NOT NULL,
    days INTEGER NOT NULL DEFAULT 7 CHECK(days=7),
    activation_job_id UUID,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(project_id,referred_tg_id),
    UNIQUE(project_id,source_order_id),
    FOREIGN KEY(project_id,referred_tg_id) REFERENCES wl_customers(project_id,tg_id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,referrer_tg_id) REFERENCES wl_customers(project_id,tg_id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,source_order_id) REFERENCES wl_orders(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,activation_job_id) REFERENCES wl_activation_jobs(project_id,job_id) ON DELETE RESTRICT,
    CHECK(referred_tg_id<>referrer_tg_id)
);
CREATE INDEX IF NOT EXISTS wl_referral_bonus_bank ON wl_referral_bonus_grants(project_id,referrer_tg_id) WHERE activation_job_id IS NULL;

COMMIT;
