-- Explicit local/staging extension after schema.sql, commerce.sql, entitlements.sql, traffic.sql.
-- No startup migration, real sends, main-SPN changes, or free entitlement grants.
BEGIN;

CREATE TABLE IF NOT EXISTS wl_manager_identities (
    tg_id BIGINT PRIMARY KEY CHECK(tg_id>0),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS wl_project_members (
    project_id UUID NOT NULL REFERENCES wl_projects(id) ON DELETE RESTRICT,
    tg_id BIGINT NOT NULL REFERENCES wl_manager_identities(tg_id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK(role IN ('viewer','support','marketer')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
    active BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id,tg_id)
);
CREATE TABLE IF NOT EXISTS wl_management_requests (
    project_id UUID NOT NULL REFERENCES wl_projects(id) ON DELETE RESTRICT,
    actor_tg_id BIGINT NOT NULL CHECK(actor_tg_id>0),
    request_key UUID NOT NULL,
    fingerprint TEXT NOT NULL CHECK(fingerprint ~ '^[0-9a-f]{64}$'),
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id,actor_tg_id,request_key)
);
CREATE TABLE IF NOT EXISTS wl_campaigns (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES wl_projects(id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK(char_length(btrim(title)) BETWEEN 1 AND 80),
    code TEXT NOT NULL UNIQUE CHECK(code ~ '^[a-z0-9]{20}$'),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,id)
);
CREATE TABLE IF NOT EXISTS wl_campaign_attributions (
    project_id UUID NOT NULL,
    tg_id BIGINT NOT NULL,
    campaign_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id,tg_id),
    FOREIGN KEY(project_id,tg_id) REFERENCES wl_customers(project_id,tg_id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,campaign_id) REFERENCES wl_campaigns(project_id,id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS wl_campaign_attributions_campaign ON wl_campaign_attributions(project_id,campaign_id);
CREATE TABLE IF NOT EXISTS wl_marketing_preferences (
    project_id UUID NOT NULL,
    tg_id BIGINT NOT NULL,
    opted_in BOOLEAN NOT NULL DEFAULT false,
    blocked BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id,tg_id),
    FOREIGN KEY(project_id,tg_id) REFERENCES wl_customers(project_id,tg_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS wl_marketing_broadcasts (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES wl_projects(id) ON DELETE RESTRICT,
    text TEXT NOT NULL CHECK(char_length(btrim(text)) BETWEEN 1 AND 3000),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','review','queued','canceled','complete')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
    review_token_hash TEXT CHECK(review_token_hash ~ '^[0-9a-f]{64}$'),
    reviewed_at TIMESTAMPTZ,
    queued_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,id)
);
-- Reviewed audience is a fixed snapshot. An opt-out/block still wins at confirmation
-- and MUST win again immediately before a future explicitly-enabled sender sends.
CREATE TABLE IF NOT EXISTS wl_marketing_recipients (
    project_id UUID NOT NULL,
    broadcast_id UUID NOT NULL,
    tg_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'review' CHECK(status IN ('review','queued','leased','sending','canceled','delivered','blocked','uncertain')),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    telegram_message_id BIGINT CHECK(telegram_message_id>0),
    PRIMARY KEY(project_id,broadcast_id,tg_id),
    FOREIGN KEY(project_id,broadcast_id) REFERENCES wl_marketing_broadcasts(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,tg_id) REFERENCES wl_customers(project_id,tg_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS wl_marketing_broadcasts_project ON wl_marketing_broadcasts(project_id,created_at DESC,id);
CREATE INDEX IF NOT EXISTS wl_marketing_recipients_due ON wl_marketing_recipients(next_attempt_at,broadcast_id,tg_id) WHERE status='queued';
CREATE TABLE IF NOT EXISTS wl_support_resend_intents (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    tg_id BIGINT NOT NULL,
    subscription_id UUID NOT NULL,
    actor_tg_id BIGINT NOT NULL CHECK(actor_tg_id>0),
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','leased','sending','delivered','blocked','uncertain')),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    telegram_message_id BIGINT CHECK(telegram_message_id>0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(project_id,subscription_id,tg_id) REFERENCES wl_subscriptions(project_id,id,tg_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS wl_support_resend_due ON wl_support_resend_intents(next_attempt_at,created_at,id) WHERE status='queued';
COMMIT;
