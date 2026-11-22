-- Explicit isolated/staging migration AFTER operations.sql. No automatic run.
-- Alias a page's confirmed UUID request to one existing invoice/order, including
-- recovery after a new browser request key without creating a second invoice.
BEGIN;
CREATE TABLE IF NOT EXISTS wl_public_renewal_requests (
    project_id UUID NOT NULL,
    subscription_id UUID NOT NULL,
    request_key UUID NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK(request_fingerprint ~ '^[0-9a-f]{64}$'),
    order_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id,subscription_id,request_key),
    FOREIGN KEY(project_id,subscription_id) REFERENCES wl_subscriptions(project_id,id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id,order_id) REFERENCES wl_orders(project_id,id) ON DELETE RESTRICT
);
COMMIT;
