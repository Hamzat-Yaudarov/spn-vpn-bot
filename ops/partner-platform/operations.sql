-- Explicit local/staging AFTER subscriptions.sql. No historical grants/charges.
BEGIN;
ALTER TABLE wl_orders ADD COLUMN IF NOT EXISTS invoice_requested_at TIMESTAMPTZ;
ALTER TABLE wl_orders DROP CONSTRAINT IF EXISTS wl_orders_activation_issue_check;
ALTER TABLE wl_orders ADD CONSTRAINT wl_orders_activation_issue_check CHECK (
    activation_issue IS NULL OR activation_issue IN ('addon_expired','device_limit','activation_expired'));
ALTER TABLE wl_activation_jobs DROP CONSTRAINT IF EXISTS wl_activation_jobs_blocked_reason_check;
ALTER TABLE wl_activation_jobs ADD CONSTRAINT wl_activation_jobs_blocked_reason_check CHECK (
    blocked_reason IS NULL OR blocked_reason='activation_expired'
    OR (kind='order' AND blocked_reason IN ('addon_expired','device_limit')));
COMMIT;
