-- Explicit, local/staging partner-finance migration AFTER subscriptions.sql.
-- Existing SPN tables and historical partner commissions are never recalculated.
BEGIN;

CREATE TABLE IF NOT EXISTS wl_withdrawals (
    id UUID PRIMARY KEY,
    number BIGSERIAL UNIQUE,
    project_id UUID NOT NULL,
    owner_tg_id BIGINT NOT NULL,
    request_key UUID NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks BETWEEN 150000 AND 9000000000000000),
    method TEXT NOT NULL CHECK (method IN ('sbp','usdt')),
    encrypted_requisites TEXT NOT NULL CHECK (char_length(encrypted_requisites) BETWEEN 32 AND 16384),
    destination_hint TEXT NOT NULL CHECK (char_length(destination_hint) BETWEEN 1 AND 120),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','paid','rejected')),
    processing_operator_id BIGINT CHECK (processing_operator_id > 0),
    rejection_reason TEXT,
    no_transfer_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    paid_kopecks BIGINT,
    transfer_reference_hash TEXT UNIQUE CHECK (transfer_reference_hash ~ '^[0-9a-f]{64}$'),
    encrypted_transfer_reference TEXT,
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,request_key),
    UNIQUE(project_id,id),
    FOREIGN KEY(project_id,owner_tg_id) REFERENCES wl_projects(id,owner_tg_id) ON DELETE RESTRICT,
    CHECK ((status='paid')=(paid_at IS NOT NULL)),
    CHECK ((status='paid')=(paid_kopecks IS NOT NULL)),
    CHECK (paid_kopecks IS NULL OR paid_kopecks=amount_kopecks),
    CHECK ((status='paid')=(transfer_reference_hash IS NOT NULL)),
    CHECK ((status='paid')=(encrypted_transfer_reference IS NOT NULL)),
    CHECK (status NOT IN ('processing','paid') OR processing_operator_id IS NOT NULL),
    CHECK ((status='rejected')=(rejection_reason IS NOT NULL))
);
ALTER TABLE wl_withdrawals ADD COLUMN IF NOT EXISTS no_transfer_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS wl_withdrawals_project_created ON wl_withdrawals(project_id,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS wl_withdrawals_review ON wl_withdrawals(status,created_at,id);
CREATE UNIQUE INDEX IF NOT EXISTS wl_withdrawals_one_unfinished ON wl_withdrawals(project_id)
    WHERE status IN ('pending','processing');

-- A confirmed external reversal, not a refund application. Never initiates a refund.
-- Each event stores the incremental proportional reversal of the original split.
CREATE TABLE IF NOT EXISTS wl_revenue_reversals (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    order_id UUID NOT NULL,
    request_key UUID NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    event_reference_hash TEXT NOT NULL UNIQUE CHECK (event_reference_hash ~ '^[0-9a-f]{64}$'),
    encrypted_evidence TEXT NOT NULL CHECK (char_length(encrypted_evidence) BETWEEN 32 AND 16384),
    kind TEXT NOT NULL CHECK (kind IN ('chargeback','confirmed_refund')),
    amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks>0),
    partner_kopecks BIGINT NOT NULL CHECK (partner_kopecks>=0),
    platform_kopecks BIGINT NOT NULL CHECK (platform_kopecks>=0),
    operator_tg_id BIGINT NOT NULL CHECK (operator_tg_id>0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,request_key),
    FOREIGN KEY(project_id,order_id) REFERENCES wl_revenue_ledger(project_id,order_id) ON DELETE RESTRICT,
    CHECK (partner_kopecks+platform_kopecks=amount_kopecks)
);

CREATE TABLE IF NOT EXISTS wl_finance_audit (
    id BIGSERIAL PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES wl_projects(id) ON DELETE RESTRICT,
    withdrawal_id UUID,
    reversal_id UUID REFERENCES wl_revenue_reversals(id) ON DELETE RESTRICT,
    actor_tg_id BIGINT NOT NULL CHECK(actor_tg_id>0),
    action TEXT NOT NULL CHECK (action IN ('withdrawal_requested','withdrawal_claimed',
        'withdrawal_details_viewed','withdrawal_paid','withdrawal_rejected','reversal_confirmed')),
    amount_kopecks BIGINT NOT NULL DEFAULT 0 CHECK(amount_kopecks>=0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(project_id,withdrawal_id) REFERENCES wl_withdrawals(project_id,id) ON DELETE RESTRICT
);

-- Intent is atomic with the finance event. A separate explicit worker may notify
-- the central-manager bot; this migration/service never sends messages.
CREATE TABLE IF NOT EXISTS wl_finance_outbox (
    id BIGSERIAL PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES wl_projects(id) ON DELETE RESTRICT,
    event_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('withdrawal_requested','withdrawal_paid','withdrawal_rejected','reversal_confirmed')),
    recipient_tg_id BIGINT NOT NULL CHECK(recipient_tg_id>0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered')),
    delivery_state TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_state IN ('pending','leased','sending','uncertain','blocked','delivered')),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    telegram_message_id BIGINT CHECK(telegram_message_id>0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(kind,event_id,recipient_tg_id)
);
ALTER TABLE wl_finance_outbox ADD COLUMN IF NOT EXISTS delivery_state TEXT NOT NULL DEFAULT 'pending'
    CHECK(delivery_state IN ('pending','leased','sending','uncertain','blocked','delivered'));
ALTER TABLE wl_finance_outbox ADD COLUMN IF NOT EXISTS lease_token UUID;
ALTER TABLE wl_finance_outbox ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE wl_finance_outbox ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE wl_finance_outbox ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0);
ALTER TABLE wl_finance_outbox ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT CHECK(telegram_message_id>0);
UPDATE wl_finance_outbox SET delivery_state='delivered' WHERE status='delivered' AND delivery_state='pending';
ALTER TABLE wl_finance_outbox DROP CONSTRAINT IF EXISTS wl_finance_outbox_delivery_consistent;
ALTER TABLE wl_finance_outbox ADD CONSTRAINT wl_finance_outbox_delivery_consistent
    CHECK ((status='delivered')=(delivery_state='delivered'));
CREATE INDEX IF NOT EXISTS wl_finance_outbox_due ON wl_finance_outbox(next_attempt_at,id) WHERE delivery_state='pending';
CREATE INDEX IF NOT EXISTS wl_finance_outbox_lease ON wl_finance_outbox(lease_until,id) WHERE delivery_state IN ('leased','sending');

CREATE OR REPLACE FUNCTION wl_finance_append_only() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Financial events are append-only';
END;
$$;
DROP TRIGGER IF EXISTS wl_revenue_reversals_append_only ON wl_revenue_reversals;
CREATE TRIGGER wl_revenue_reversals_append_only BEFORE UPDATE OR DELETE ON wl_revenue_reversals
    FOR EACH ROW EXECUTE FUNCTION wl_finance_append_only();
DROP TRIGGER IF EXISTS wl_finance_audit_append_only ON wl_finance_audit;
CREATE TRIGGER wl_finance_audit_append_only BEFORE UPDATE OR DELETE ON wl_finance_audit
    FOR EACH ROW EXECUTE FUNCTION wl_finance_append_only();
DROP TRIGGER IF EXISTS wl_revenue_ledger_append_only ON wl_revenue_ledger;
CREATE TRIGGER wl_revenue_ledger_append_only BEFORE UPDATE OR DELETE ON wl_revenue_ledger
    FOR EACH ROW EXECUTE FUNCTION wl_finance_append_only();

CREATE OR REPLACE FUNCTION wl_withdrawal_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Withdrawal history cannot be deleted'; END IF;
    IF OLD.id IS DISTINCT FROM NEW.id OR OLD.project_id IS DISTINCT FROM NEW.project_id
       OR OLD.owner_tg_id IS DISTINCT FROM NEW.owner_tg_id OR OLD.number IS DISTINCT FROM NEW.number
       OR OLD.request_key IS DISTINCT FROM NEW.request_key OR OLD.request_fingerprint IS DISTINCT FROM NEW.request_fingerprint
       OR OLD.amount_kopecks IS DISTINCT FROM NEW.amount_kopecks OR OLD.method IS DISTINCT FROM NEW.method
       OR OLD.encrypted_requisites IS DISTINCT FROM NEW.encrypted_requisites
       OR OLD.destination_hint IS DISTINCT FROM NEW.destination_hint OR OLD.created_at IS DISTINCT FROM NEW.created_at
       OR (OLD.status IN ('paid','rejected') AND OLD IS DISTINCT FROM NEW)
       OR (OLD.status='processing' AND NEW.status='pending')
    THEN RAISE EXCEPTION 'Withdrawal identity or final state is immutable'; END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS wl_withdrawals_immutable ON wl_withdrawals;
CREATE TRIGGER wl_withdrawals_immutable BEFORE UPDATE OR DELETE ON wl_withdrawals
    FOR EACH ROW EXECUTE FUNCTION wl_withdrawal_immutable();
COMMIT;
