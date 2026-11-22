-- Pay first, then create one Managed Bot. Apply explicitly to the separate
-- franchise database after public_entry_trial.sql and managed_bots.sql.
-- Existing prices, receipts, owners, requests, balances and launch jobs remain
-- unchanged. The sole new identity transition is NULL -> verified paid bot.
BEGIN;

LOCK TABLE public.wl_projects, public.wl_entry_orders,
    public.wl_managed_bot_requests, public.wl_launch_jobs IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.wl_entry_orders'::regclass
        AND tgname='wl_entry_financial_snapshot_guard' AND NOT tgisinternal) THEN
        RAISE EXCEPTION 'Existing immutable entry financial guard required';
    END IF;
END $$;

ALTER TABLE public.wl_entry_orders ALTER COLUMN bot_id DROP NOT NULL;
ALTER TABLE public.wl_entry_orders
    DROP CONSTRAINT IF EXISTS wl_entry_order_launched_bot_required;
ALTER TABLE public.wl_entry_orders
    ADD CONSTRAINT wl_entry_order_launched_bot_required
    CHECK (status<>'launched' OR bot_id IS NOT NULL);

CREATE OR REPLACE FUNCTION public.wl_guard_entry_financial_snapshot()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
    IF ROW(OLD.price_kopecks, OLD.owner_tg_id, OLD.project_id, OLD.request_key,
           OLD.purpose, OLD.currency)
       IS DISTINCT FROM
       ROW(NEW.price_kopecks, NEW.owner_tg_id, NEW.project_id, NEW.request_key,
           NEW.purpose, NEW.currency) THEN
        RAISE EXCEPTION 'Entry financial snapshot is immutable'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_financial_snapshot_immutable';
    END IF;
    IF OLD.bot_id IS DISTINCT FROM NEW.bot_id AND NOT (
        OLD.bot_id IS NULL AND NEW.bot_id IS NOT NULL
        AND OLD.status='paid_pending_launch' AND NEW.status=OLD.status
        AND OLD.paid_at IS NOT NULL AND NEW.paid_at=OLD.paid_at
        AND OLD.provider_payment_id IS NOT NULL
        AND NEW.provider_payment_id=OLD.provider_payment_id
        AND EXISTS (
            SELECT 1 FROM public.wl_managed_bot_requests r
            JOIN public.wl_projects p ON p.id=r.project_id AND p.owner_tg_id=r.owner_tg_id
            WHERE r.project_id=OLD.project_id AND r.owner_tg_id=OLD.owner_tg_id
              AND r.status='pending' AND r.bot_id=NEW.bot_id
              AND p.bot_id=NEW.bot_id AND p.status IN ('draft','ready')
        )
    ) THEN
        RAISE EXCEPTION 'Entry bot identity is immutable'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_financial_snapshot_immutable';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.wl_guard_entry_financial_snapshot() FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.wl_guard_entry_bot_identity()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
    IF OLD.bot_id IS DISTINCT FROM NEW.bot_id AND
       (OLD.status='active' OR EXISTS(SELECT 1 FROM public.wl_entry_orders
            WHERE project_id=OLD.id AND status<>'canceled')) AND NOT (
        OLD.bot_id IS NULL AND NEW.bot_id IS NOT NULL
        AND OLD.status IN ('draft','ready') AND NEW.status=OLD.status
        AND EXISTS (
            SELECT 1 FROM public.wl_entry_orders o
            JOIN public.wl_managed_bot_requests r
              ON r.project_id=o.project_id AND r.owner_tg_id=o.owner_tg_id
            WHERE o.project_id=OLD.id AND o.owner_tg_id=OLD.owner_tg_id
              AND o.status='paid_pending_launch' AND o.bot_id IS NULL
              AND o.paid_at IS NOT NULL AND o.provider_payment_id IS NOT NULL
              AND r.status='pending' AND r.bot_id=NEW.bot_id
        )
    ) THEN
        RAISE EXCEPTION 'Bot identity is locked by an entry order'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_bot_identity_locked';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.wl_guard_entry_bot_identity() FROM PUBLIC;

COMMIT;
