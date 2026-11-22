-- MANUAL isolated partner migration; never applied by application startup.
-- Apply after enrollment.sql + notifications.sql, then reapply the existing
-- supabase_security.sql. No money, prices, paid states or old deliveries change.
BEGIN;
LOCK TABLE public.wl_partner_outbox IN ACCESS EXCLUSIVE MODE;
DO $$
DECLARE expression TEXT; valid BOOLEAN;
BEGIN
    SELECT pg_get_expr(conbin,conrelid),convalidated INTO expression,valid
      FROM pg_constraint WHERE conrelid='public.wl_partner_outbox'::regclass
       AND conname='wl_partner_outbox_kind_check' AND contype='c';
    IF NOT FOUND OR NOT valid THEN
        RAISE EXCEPTION 'Validated partner outbox kind constraint required';
    END IF;
    expression := lower(regexp_replace(replace(replace(expression,'::text',''),'''',''),'[[:space:]]','','g'));
    IF expression NOT IN ('(kind=entry_paid)','((kind=entry_paid)or(kind=entry_launched))') THEN
        RAISE EXCEPTION 'Unexpected partner outbox kinds; refusing expansion';
    END IF;
END $$;
ALTER TABLE public.wl_partner_outbox DROP CONSTRAINT wl_partner_outbox_kind_check;
ALTER TABLE public.wl_partner_outbox ADD CONSTRAINT wl_partner_outbox_kind_check
    CHECK(kind='entry_paid' OR kind='entry_launched');
CREATE INDEX IF NOT EXISTS wl_entry_orders_launched_notice_candidates
    ON public.wl_entry_orders(launched_at,project_id,id)
    WHERE status='launched' AND paid_at IS NOT NULL AND launched_at IS NOT NULL;
COMMIT;
