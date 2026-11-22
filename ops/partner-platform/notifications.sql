-- Apply explicitly AFTER entitlements.sql and enrollment.sql. No SPN outbox touched.
BEGIN;
DO $$ DECLARE tab text; BEGIN
    FOREACH tab IN ARRAY ARRAY['wl_customer_outbox','wl_partner_outbox'] LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS delivery_state TEXT NOT NULL DEFAULT ''pending''',tab);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS lease_token UUID',tab);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ',tab);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()',tab);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0',tab);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT',tab);
        EXECUTE format('UPDATE %I SET delivery_state=''delivered'' WHERE status=''delivered'' AND delivery_state=''pending''',tab);
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I',tab,tab||'_delivery_check');
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I CHECK(
            delivery_state IN (''pending'',''leased'',''sending'',''uncertain'',''blocked'',''delivered'')
            AND attempts>=0 AND ((status=''delivered'')=(delivery_state=''delivered''))
            AND ((delivery_state IN (''leased'',''sending'',''uncertain'') AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
                OR delivery_state IN (''pending'',''blocked'',''delivered''))
            AND (telegram_message_id IS NULL OR telegram_message_id>0))',tab,tab||'_delivery_check');
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I(next_attempt_at,id) WHERE delivery_state=''pending''',tab||'_delivery_due',tab);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I(lease_until,id) WHERE delivery_state IN (''leased'',''sending'')',tab||'_delivery_leases',tab);
    END LOOP;
END $$;
COMMIT;
