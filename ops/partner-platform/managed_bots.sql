-- Optional isolated franchise Managed Bots onboarding. Apply manually after
-- enrollment.sql, then reapply supabase_security.sql. No money rows changed.
BEGIN;
CREATE TABLE IF NOT EXISTS public.wl_managed_bot_requests (
    project_id UUID PRIMARY KEY,
    owner_tg_id BIGINT NOT NULL CHECK(owner_tg_id>0),
    project_version INTEGER NOT NULL CHECK(project_version>0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','bound','expired')),
    bot_id BIGINT UNIQUE CHECK(bot_id>0),
    last_update_id BIGINT CHECK(last_update_id>=0),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL CHECK(expires_at>created_at),
    bound_at TIMESTAMPTZ,
    FOREIGN KEY(project_id,owner_tg_id) REFERENCES public.wl_projects(id,owner_tg_id) ON DELETE RESTRICT,
    CHECK((status='bound' AND bot_id IS NOT NULL AND last_update_id IS NOT NULL AND bound_at IS NOT NULL)
       OR (status IN ('pending','expired') AND last_update_id IS NULL AND bound_at IS NULL)),
    CHECK(status<>'expired' OR bot_id IS NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS wl_managed_bot_one_pending_owner
    ON public.wl_managed_bot_requests(owner_tg_id) WHERE status='pending';
ALTER TABLE public.wl_managed_bot_requests ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.wl_managed_bot_requests FROM PUBLIC;
COMMIT;
