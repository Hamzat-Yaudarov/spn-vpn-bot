-- Explicit owner-approved setup in the SEPARATE partner Supabase project.
-- Deliberately NOLOGIN and no password. No ready-to-use credential is created.
-- After grants/RLS verification the owner independently sets a strong password
-- through a protected channel and permits LOGIN, never pasting it into chat,
-- this repository, shared SQL history or frontend configuration.
BEGIN;
DO $$
DECLARE configured_role RECORD;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns WHERE table_schema='public'
          AND table_name IN ('users','payments','subscriptions') AND column_name='tg_id'
    ) THEN
        RAISE EXCEPTION 'Refusing to modify the main SPN database';
    END IF;
    SELECT * INTO configured_role FROM pg_roles WHERE rolname='spn_partner_runtime';
    IF configured_role IS NULL THEN
        CREATE ROLE spn_partner_runtime NOLOGIN NOINHERIT NOSUPERUSER
            NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 20;
    ELSIF configured_role.rolsuper OR configured_role.rolcreatedb
       OR configured_role.rolcreaterole OR configured_role.rolreplication
       OR configured_role.rolbypassrls
       OR EXISTS (
           SELECT 1 FROM pg_auth_members
           WHERE member=configured_role.oid OR (roleid=configured_role.oid AND NOT (
               member=(SELECT oid FROM pg_roles WHERE rolname='postgres')
               AND grantor IN (SELECT oid FROM pg_roles WHERE rolname IN ('postgres','supabase_admin'))
               AND admin_option
               AND (to_jsonb(pg_auth_members)->>'inherit_option')::boolean IS FALSE
               AND (to_jsonb(pg_auth_members)->>'set_option')::boolean IS FALSE
           ))
       ) THEN
        RAISE EXCEPTION 'Existing runtime role needs manual security review';
    END IF;
END $$;
COMMIT;
