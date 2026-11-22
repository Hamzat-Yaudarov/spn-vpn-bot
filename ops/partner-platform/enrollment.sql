-- Isolated partner entry orders and bot launch recovery. Apply explicitly AFTER
-- schema.sql. No existing SPN tables, subscription grants or 70/30 entries.
BEGIN;

CREATE TABLE IF NOT EXISTS wl_entry_orders (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    owner_tg_id BIGINT NOT NULL CHECK (owner_tg_id > 0),
    request_key UUID NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'entry' CHECK (purpose='entry'),
    bot_id BIGINT NOT NULL CHECK (bot_id > 0),
    price_kopecks BIGINT NOT NULL DEFAULT 999000 CHECK (price_kopecks=999000),
    currency TEXT NOT NULL DEFAULT 'RUB' CHECK (currency='RUB'),
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created','paid_pending_launch','launched','canceled')),
    provider_payment_id TEXT UNIQUE,
    confirmation_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at TIMESTAMPTZ,
    launched_at TIMESTAMPTZ,
    UNIQUE(owner_tg_id,request_key),
    UNIQUE(project_id,id),
    UNIQUE(project_id,id,owner_tg_id),
    FOREIGN KEY(project_id,owner_tg_id) REFERENCES wl_projects(id,owner_tg_id) ON DELETE RESTRICT,
    CHECK ((provider_payment_id IS NULL AND confirmation_url IS NULL)
        OR (provider_payment_id IS NOT NULL AND confirmation_url IS NOT NULL
            AND char_length(provider_payment_id) BETWEEN 1 AND 200 AND (confirmation_url='' OR confirmation_url ~ '^https://'))),
    CHECK ((status IN ('created','canceled') AND paid_at IS NULL AND launched_at IS NULL)
        OR (status='paid_pending_launch' AND paid_at IS NOT NULL AND launched_at IS NULL AND provider_payment_id IS NOT NULL)
        OR (status='launched' AND paid_at IS NOT NULL AND launched_at IS NOT NULL AND provider_payment_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS wl_entry_orders_one_open_project
    ON wl_entry_orders(project_id) WHERE status<>'canceled';

-- Freeze bot identity as soon as an invoice workflow exists. A same-bot token
-- rotation remains allowed. Cancellation must be confirmed by the provider
-- before a different bot can replace the unpaid one.
CREATE OR REPLACE FUNCTION wl_guard_entry_bot_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.bot_id IS DISTINCT FROM NEW.bot_id AND
       (OLD.status='active' OR EXISTS(SELECT 1 FROM wl_entry_orders
            WHERE project_id=OLD.id AND status<>'canceled')) THEN
        RAISE EXCEPTION 'Bot identity is locked by an entry order'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_bot_identity_locked';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS wl_entry_bot_identity_guard ON wl_projects;
CREATE TRIGGER wl_entry_bot_identity_guard BEFORE UPDATE OF bot_id ON wl_projects
    FOR EACH ROW EXECUTE FUNCTION wl_guard_entry_bot_identity();

CREATE TABLE IF NOT EXISTS wl_launch_jobs (
    project_id UUID NOT NULL,
    order_id UUID NOT NULL,
    owner_tg_id BIGINT NOT NULL,
    bot_id BIGINT NOT NULL CHECK (bot_id > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','retry','complete')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts>=0),
    needs_webhook_replace BOOLEAN NOT NULL DEFAULT FALSE,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    webhook_url TEXT,
    secret_fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY(project_id,order_id),
    FOREIGN KEY(project_id,order_id,owner_tg_id) REFERENCES wl_entry_orders(project_id,id,owner_tg_id) ON DELETE RESTRICT,
    CHECK ((status='complete' AND completed_at IS NOT NULL AND webhook_url IS NOT NULL
            AND secret_fingerprint IS NOT NULL AND webhook_url ~ '^https://'
            AND secret_fingerprint ~ '^[0-9a-f]{64}$' AND NOT needs_webhook_replace)
        OR (status<>'complete' AND completed_at IS NULL AND webhook_url IS NULL AND secret_fingerprint IS NULL))
);
CREATE INDEX IF NOT EXISTS wl_launch_jobs_due ON wl_launch_jobs(next_attempt_at,project_id,order_id)
    WHERE status<>'complete' AND NOT needs_webhook_replace;

CREATE TABLE IF NOT EXISTS wl_partner_outbox (
    id BIGSERIAL PRIMARY KEY,
    project_id UUID NOT NULL,
    order_id UUID NOT NULL,
    owner_tg_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK(kind='entry_paid'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,order_id,kind),
    FOREIGN KEY(project_id,order_id,owner_tg_id) REFERENCES wl_entry_orders(project_id,id,owner_tg_id) ON DELETE RESTRICT
);
COMMIT;
