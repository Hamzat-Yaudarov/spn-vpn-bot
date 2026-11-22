-- Explicit staging extension AFTER entitlements.sql. No SPN tables/traffic reset.
BEGIN;
ALTER TABLE wl_subscriptions ADD COLUMN IF NOT EXISTS traffic_cycle_baseline_bytes BIGINT;
ALTER TABLE wl_subscriptions ADD COLUMN IF NOT EXISTS traffic_cycle_started_at TIMESTAMPTZ;
ALTER TABLE wl_subscriptions ADD COLUMN IF NOT EXISTS traffic_paid_credit_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE wl_subscriptions ADD COLUMN IF NOT EXISTS traffic_last_used_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE wl_subscriptions ADD COLUMN IF NOT EXISTS traffic_cycle_hold TEXT;
ALTER TABLE wl_subscriptions DROP CONSTRAINT IF EXISTS wl_traffic_state_check;
ALTER TABLE wl_subscriptions ADD CONSTRAINT wl_traffic_state_check CHECK (
    traffic_paid_credit_bytes BETWEEN 0 AND 9007199254740991
    AND traffic_last_used_bytes BETWEEN 0 AND 9007199254740991
    AND ((traffic_cycle_baseline_bytes IS NULL AND traffic_cycle_started_at IS NULL)
        OR (traffic_cycle_baseline_bytes IS NOT NULL AND traffic_cycle_baseline_bytes BETWEEN 0 AND 9007199254740991 AND traffic_cycle_started_at IS NOT NULL))
    AND (traffic_cycle_hold IS NULL OR traffic_cycle_hold IN ('legacy_review','counter_changed','late_observation')));
-- Old active accounts need an explicit baseline audit, never a blind reset or
-- an invented zero. This does not change their existing quota or paid GB.
UPDATE wl_subscriptions SET traffic_cycle_hold='legacy_review'
    WHERE desired_expires_at IS NOT NULL AND traffic_cycle_baseline_bytes IS NULL
      AND traffic_cycle_hold IS NULL;

ALTER TABLE wl_activation_jobs DROP CONSTRAINT IF EXISTS wl_activation_jobs_kind_check;
ALTER TABLE wl_activation_jobs ADD CONSTRAINT wl_activation_jobs_kind_check CHECK (
    (kind='order' AND order_id IS NOT NULL AND source_id IS NULL)
    OR (kind IN ('device_expiry','referral_bonus','traffic_cycle') AND order_id IS NULL AND source_id IS NOT NULL));
ALTER TABLE wl_activation_jobs ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS wl_activation_jobs_retry_due ON wl_activation_jobs(next_attempt_at,sequence)
    WHERE status<>'complete';

CREATE TABLE IF NOT EXISTS wl_traffic_credits (
    project_id UUID NOT NULL,
    order_id UUID NOT NULL,
    subscription_id UUID NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('waiting','prepared','applied')),
    activation_job_id UUID,
    observed_usage_bytes BIGINT,
    remaining_paid_bytes BIGINT,
    new_limit_bytes BIGINT,
    prepared_at TIMESTAMPTZ,
    PRIMARY KEY(project_id,order_id),
    FOREIGN KEY(project_id,order_id) REFERENCES wl_orders(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,subscription_id) REFERENCES wl_subscriptions(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,activation_job_id) REFERENCES wl_activation_jobs(project_id,job_id) ON DELETE RESTRICT,
    CHECK(expires_at=available_at+interval '30 days'),
    CHECK((state='waiting' AND activation_job_id IS NULL AND prepared_at IS NULL
        AND observed_usage_bytes IS NULL AND remaining_paid_bytes IS NULL AND new_limit_bytes IS NULL)
        OR (state<>'waiting' AND activation_job_id IS NOT NULL AND prepared_at IS NOT NULL
            AND observed_usage_bytes IS NOT NULL AND remaining_paid_bytes IS NOT NULL AND new_limit_bytes IS NOT NULL
            AND observed_usage_bytes BETWEEN 0 AND 9007199254740991
            AND remaining_paid_bytes BETWEEN 0 AND 9007199254740991
            AND new_limit_bytes BETWEEN 322122547200 AND 9007199254740991))
);
CREATE INDEX IF NOT EXISTS wl_traffic_credits_due ON wl_traffic_credits(available_at)
    WHERE state='waiting';
ALTER TABLE wl_traffic_credits ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE wl_traffic_credits ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0);
COMMIT;
