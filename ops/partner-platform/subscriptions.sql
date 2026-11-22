-- Stage 6, explicit LOCAL/STAGING migration AFTER traffic.sql. No startup DDL.
-- A generation invalidates old public capability links; no bearer link stored.
BEGIN;
ALTER TABLE wl_subscriptions ADD COLUMN IF NOT EXISTS public_link_generation
    BIGINT NOT NULL DEFAULT 0 CHECK (public_link_generation BETWEEN 0 AND 2147483647);
COMMIT;
