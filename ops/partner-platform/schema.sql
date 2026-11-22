-- White-label registry ONLY. Apply explicitly to an isolated/staging database.
-- No automatic startup migration; no existing SPN users/payments are modified.
BEGIN;

CREATE TABLE IF NOT EXISTS wl_projects (
    id UUID PRIMARY KEY,
    owner_tg_id BIGINT NOT NULL CHECK (owner_tg_id > 0),
    slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$'),
    title TEXT NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 80),
    price_kopecks BIGINT NOT NULL DEFAULT 19900 CHECK (price_kopecks BETWEEN 19900 AND 10000000),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'ready', 'active', 'suspended', 'closed')),
    support_url TEXT NOT NULL DEFAULT '' CHECK (char_length(support_url) <= 500),
    news_url TEXT NOT NULL DEFAULT '' CHECK (char_length(news_url) <= 500),
    brand JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(brand) = 'object'),
    bot_id BIGINT UNIQUE CHECK (bot_id > 0),
    bot_username TEXT,
    encrypted_token TEXT,
    token_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, owner_tg_id),
    CHECK ((bot_id IS NULL AND bot_username IS NULL AND encrypted_token IS NULL AND token_updated_at IS NULL)
        OR (bot_id IS NOT NULL AND bot_username IS NOT NULL AND bot_username ~ '^[A-Za-z0-9_]{5,32}$'
            AND encrypted_token IS NOT NULL AND char_length(encrypted_token) BETWEEN 32 AND 16384
            AND token_updated_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS wl_projects_owner_created ON wl_projects(owner_tg_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS wl_project_create_requests (
    owner_tg_id BIGINT NOT NULL CHECK (owner_tg_id > 0),
    request_key TEXT NOT NULL CHECK (request_key ~ '^[A-Za-z0-9_-]{8,128}$'),
    fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    project_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_tg_id, request_key),
    FOREIGN KEY (project_id, owner_tg_id) REFERENCES wl_projects(id, owner_tg_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS wl_project_audit (
    id BIGSERIAL PRIMARY KEY,
    project_id UUID NOT NULL,
    owner_tg_id BIGINT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('created', 'settings_updated', 'bot_connected')),
    project_version INTEGER NOT NULL CHECK (project_version > 0),
    details JSONB NOT NULL DEFAULT '{"fields":[]}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, owner_tg_id) REFERENCES wl_projects(id, owner_tg_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS wl_project_audit_owner_project ON wl_project_audit(owner_tg_id, project_id, id DESC);

COMMIT;
