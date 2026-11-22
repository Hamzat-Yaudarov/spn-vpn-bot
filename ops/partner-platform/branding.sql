-- Explicit isolated/staging migration after schema.sql. Never auto-applied.
-- One current re-encoded logo per tenant; no filesystem paths/external fetch.
BEGIN;
CREATE TABLE IF NOT EXISTS wl_brand_assets (
    project_id UUID PRIMARY KEY REFERENCES wl_projects(id) ON DELETE RESTRICT,
    asset_id TEXT NOT NULL CHECK(asset_id ~ '^[0-9a-f]{64}$'),
    png BYTEA NOT NULL CHECK(octet_length(png) BETWEEN 32 AND 270000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS wl_brand_asset_audit (
    id BIGSERIAL PRIMARY KEY,
    project_id UUID NOT NULL,
    owner_tg_id BIGINT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('uploaded','removed')),
    asset_id TEXT CHECK(asset_id ~ '^[0-9a-f]{64}$'),
    project_version INTEGER NOT NULL CHECK(project_version>0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(project_id,owner_tg_id) REFERENCES wl_projects(id,owner_tg_id) ON DELETE RESTRICT
);
COMMIT;
