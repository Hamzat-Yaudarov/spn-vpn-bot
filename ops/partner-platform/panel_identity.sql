-- Explicit cutover only; no panel calls, renames, balance or entitlement changes.
-- Existing and feature-off writer subscriptions keep their legacy identity,
-- including a potentially created remote user whose response was lost.
BEGIN;

ALTER TABLE wl_subscriptions ADD COLUMN IF NOT EXISTS remote_identity JSONB;

UPDATE wl_subscriptions SET remote_identity=jsonb_build_object(
    'version',1,'username','wl_' || replace(id::text,'-',''),
    'description','SPN-WL:' || project_id::text || ':' || id::text)
WHERE remote_identity IS NULL;

CREATE OR REPLACE FUNCTION wl_preserve_panel_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' AND NEW.remote_identity IS NULL THEN
        NEW.remote_identity := jsonb_build_object(
            'version',1,'username','wl_' || replace(NEW.id::text,'-',''),
            'description','SPN-WL:' || NEW.project_id::text || ':' || NEW.id::text);
    ELSIF TG_OP='UPDATE' AND
        (OLD.remote_identity IS DISTINCT FROM NEW.remote_identity OR
         OLD.id IS DISTINCT FROM NEW.id OR OLD.project_id IS DISTINCT FROM NEW.project_id OR
         OLD.tg_id IS DISTINCT FROM NEW.tg_id) THEN
        RAISE EXCEPTION 'Panel identity is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS wl_preserve_panel_identity ON wl_subscriptions;
CREATE TRIGGER wl_preserve_panel_identity BEFORE INSERT OR UPDATE ON wl_subscriptions
FOR EACH ROW EXECUTE FUNCTION wl_preserve_panel_identity();

ALTER TABLE wl_subscriptions ALTER COLUMN remote_identity SET NOT NULL;
ALTER TABLE wl_subscriptions DROP CONSTRAINT IF EXISTS wl_subscriptions_panel_identity_shape;
ALTER TABLE wl_subscriptions ADD CONSTRAINT wl_subscriptions_panel_identity_shape CHECK (
    jsonb_typeof(remote_identity)='object' AND
    remote_identity ?& ARRAY['version','username','description'] AND
    remote_identity->>'version' IN ('1','2') AND
    remote_identity->>'username' ~ '^[a-zA-Z0-9_]{3,36}$' AND
    char_length(remote_identity->>'description') BETWEEN 1 AND 256
);
CREATE UNIQUE INDEX IF NOT EXISTS wl_subscriptions_panel_username
ON wl_subscriptions((remote_identity->>'username'));

COMMIT;
