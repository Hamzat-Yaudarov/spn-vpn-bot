-- MANUAL, authorized public 10 RUB enrollment trial in the separate franchise
-- database only. Apply AFTER owner_entry_pilot.sql, while owner-only application
-- access remains enforced; enable public access only after the new code probe.
-- No price/owner/history rewrite, new table, grants or customer billing.
-- Restore ordinary pricing using the application flag, NEVER by rewriting
-- existing invoices. The 10 RUB snapshot remains valid for reconciliation.
BEGIN;
LOCK TABLE public.wl_entry_orders IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE current_check RECORD; normalized TEXT;
BEGIN
    SELECT convalidated, pg_get_expr(conbin, conrelid) AS expression
      INTO current_check FROM pg_constraint
     WHERE conrelid='public.wl_entry_orders'::regclass
       AND conname='wl_entry_orders_price_kopecks_check' AND contype='c';
    IF NOT FOUND OR NOT current_check.convalidated THEN
        RAISE EXCEPTION 'Validated owner entry pilot constraint required';
    END IF;
    normalized := lower(regexp_replace(
        replace(replace(current_check.expression, '::bigint', ''), '''', ''),
        '[[:space:]]', '', 'g'));
    IF normalized NOT IN (
        '((price_kopecks=999000)or((price_kopecks=1000)and(owner_tg_id=6910097562)))',
        '((price_kopecks=999000)or(price_kopecks=1000))'
    ) THEN
        RAISE EXCEPTION 'Unexpected entry price constraint; refusing public trial';
    END IF;
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
        RAISE EXCEPTION 'Exact one trial project per owner index required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE t.tgrelid='public.wl_entry_orders'::regclass
          AND t.tgname='wl_entry_financial_snapshot_guard'
          AND t.tgenabled IN ('O','A') AND t.tgtype=19 AND NOT t.tgisinternal
          AND n.nspname='public' AND p.proname='wl_guard_entry_financial_snapshot'
          AND NOT p.prosecdef
    ) THEN
        RAISE EXCEPTION 'Existing immutable entry financial snapshot required';
    END IF;
END $$;

ALTER TABLE public.wl_entry_orders
    DROP CONSTRAINT wl_entry_orders_price_kopecks_check;
ALTER TABLE public.wl_entry_orders
    ADD CONSTRAINT wl_entry_orders_price_kopecks_check CHECK (
        price_kopecks=999000 OR price_kopecks=1000
    );
-- The pre-existing unique per-owner index and immutable financial trigger are
-- deliberately retained byte-for-byte. The runtime verifies their exact form.
COMMIT;
