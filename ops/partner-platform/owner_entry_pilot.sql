-- OPTIONAL, MANUAL owner-only 10 RUB entry pilot. NOT applied by application
-- startup. Apply after enrollment.sql in the isolated partner database only,
-- then reapply supabase_security.sql before serving traffic. No invoice,
-- payment, existing price update, grant or additional money table is created.
-- Removing the application pilot flag restores new-order pricing; never
-- rewrite an already issued order's financial snapshot to restore 9990 RUB.
BEGIN;

LOCK TABLE public.wl_entry_orders IN ACCESS EXCLUSIVE MODE;

-- Fail closed on an absent, unvalidated or unexpectedly broadened predecessor.
-- Accept this migration's exact result too, so an explicit rerun is harmless.
DO $$
DECLARE current_check RECORD; normalized TEXT;
BEGIN
    SELECT convalidated, pg_get_expr(conbin, conrelid) AS expression
      INTO current_check FROM pg_constraint
     WHERE conrelid='public.wl_entry_orders'::regclass
       AND conname='wl_entry_orders_price_kopecks_check' AND contype='c';
    IF NOT FOUND OR NOT current_check.convalidated THEN
        RAISE EXCEPTION 'Validated original entry price constraint required';
    END IF;
    normalized := lower(regexp_replace(
        replace(replace(current_check.expression, '::bigint', ''), '''', ''),
        '[[:space:]]', '', 'g'));
    IF normalized NOT IN (
        '(price_kopecks=999000)',
        '((price_kopecks=999000)or((price_kopecks=1000)and(owner_tg_id=6910097562)))'
    ) THEN
        RAISE EXCEPTION 'Unexpected entry price constraint; refusing pilot';
    END IF;
END $$;

ALTER TABLE public.wl_entry_orders
    DROP CONSTRAINT wl_entry_orders_price_kopecks_check;
ALTER TABLE public.wl_entry_orders
    ADD CONSTRAINT wl_entry_orders_price_kopecks_check CHECK (
        price_kopecks=999000 OR
        (price_kopecks=1000 AND owner_tg_id=6910097562)
    );

-- Canceled invoices permit a fresh explicit attempt; paid/launched orders
-- continue to consume the owner's single pilot slot across ALL projects.
CREATE UNIQUE INDEX IF NOT EXISTS wl_entry_orders_one_open_owner_pilot
    ON public.wl_entry_orders(owner_tg_id)
    WHERE price_kopecks=1000 AND status<>'canceled';

-- IF NOT EXISTS must never silently accept a same-named ineffective index.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attname='owner_tg_id'
        WHERE n.nspname='public'
          AND c.relname='wl_entry_orders_one_open_owner_pilot'
          AND i.indrelid='public.wl_entry_orders'::regclass
          AND i.indisunique AND i.indisvalid AND i.indisready
          AND i.indkey::text=a.attnum::text
          AND lower(regexp_replace(
              replace(replace(replace(pg_get_expr(i.indpred, i.indrelid),
                  '::text', ''), '::bigint', ''), '''', ''),
              '[[:space:]]', '', 'g'))=
              '((price_kopecks=1000)and(status<>canceled))'
    ) THEN
        RAISE EXCEPTION 'Exact unique owner pilot index required';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.wl_guard_entry_financial_snapshot()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
    IF ROW(OLD.price_kopecks, OLD.owner_tg_id, OLD.project_id, OLD.request_key,
           OLD.bot_id, OLD.purpose, OLD.currency)
       IS DISTINCT FROM
       ROW(NEW.price_kopecks, NEW.owner_tg_id, NEW.project_id, NEW.request_key,
           NEW.bot_id, NEW.purpose, NEW.currency) THEN
        RAISE EXCEPTION 'Entry financial snapshot is immutable'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_financial_snapshot_immutable';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.wl_guard_entry_financial_snapshot() FROM PUBLIC;

DROP TRIGGER IF EXISTS wl_entry_financial_snapshot_guard ON public.wl_entry_orders;
CREATE TRIGGER wl_entry_financial_snapshot_guard
    BEFORE UPDATE ON public.wl_entry_orders
    FOR EACH ROW EXECUTE FUNCTION public.wl_guard_entry_financial_snapshot();

COMMIT;
