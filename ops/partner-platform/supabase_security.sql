-- LAST manual migration in the separate partner Supabase project only.
-- No password, role creation, payment, provider call or startup DDL here.
-- A database owner first provisions spn_partner_runtime as a dedicated role
-- with NO SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS. The only allowed
-- membership is PostgreSQL 16+'s creator-admin relation to trusted postgres,
-- with neither inherited access nor SET ROLE permission (see setup guide).
-- Reapply after EVERY future wl_* schema migration; fail closed before serving.
BEGIN;

DO $$
DECLARE runtime_role RECORD;
BEGIN
    SELECT * INTO runtime_role FROM pg_roles WHERE rolname='spn_partner_runtime';
    IF runtime_role IS NULL
       OR runtime_role.rolsuper OR runtime_role.rolcreatedb
       OR runtime_role.rolcreaterole OR runtime_role.rolreplication
       OR runtime_role.rolbypassrls
       OR EXISTS (
           SELECT 1 FROM pg_auth_members
           WHERE member=runtime_role.oid OR (roleid=runtime_role.oid AND NOT (
               member=(SELECT oid FROM pg_roles WHERE rolname='postgres')
               AND grantor IN (SELECT oid FROM pg_roles WHERE rolname IN ('postgres','supabase_admin'))
               AND admin_option
               AND (to_jsonb(pg_auth_members)->>'inherit_option')::boolean IS FALSE
               AND (to_jsonb(pg_auth_members)->>'set_option')::boolean IS FALSE
           ))
       ) THEN
        RAISE EXCEPTION 'Dedicated non-privileged partner runtime role required';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname LIKE 'wl\_%' ESCAPE '\'
          AND c.relowner=runtime_role.oid
    ) OR EXISTS (
        SELECT 1 FROM pg_namespace WHERE nspname='public' AND nspowner=runtime_role.oid
    ) THEN
        RAISE EXCEPTION 'Runtime must not own partner tables or the public schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns WHERE table_schema='public'
          AND table_name IN ('users','payments','subscriptions')
          AND column_name='tg_id'
    ) THEN
        RAISE EXCEPTION 'Refusing to modify the main SPN database';
    END IF;
END $$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM spn_partner_runtime;
GRANT USAGE ON SCHEMA public TO spn_partner_runtime;

DO $$
DECLARE obj RECORD; api_role TEXT;
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO spn_partner_runtime', current_database());
    FOREACH api_role IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=api_role) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', api_role);
        END IF;
    END LOOP;
    FOR obj IN
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND c.relname LIKE 'wl\_%' ESCAPE '\'
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', obj.relname);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', obj.relname);
        FOREACH api_role IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=api_role) THEN
                EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %I', obj.relname, api_role);
            END IF;
        END LOOP;
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM spn_partner_runtime', obj.relname);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO spn_partner_runtime', obj.relname);
        EXECUTE format('DROP POLICY IF EXISTS wl_runtime_server ON public.%I', obj.relname);
        EXECUTE format('CREATE POLICY wl_runtime_server ON public.%I FOR ALL TO spn_partner_runtime USING (true) WITH CHECK (true)', obj.relname);
    END LOOP;
    FOR obj IN
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S'
          AND c.relname LIKE 'wl\_%' ESCAPE '\'
    LOOP
        EXECUTE format('REVOKE ALL ON SEQUENCE public.%I FROM PUBLIC', obj.relname);
        FOREACH api_role IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=api_role) THEN
                EXECUTE format('REVOKE ALL ON SEQUENCE public.%I FROM %I', obj.relname, api_role);
            END IF;
        END LOOP;
        EXECUTE format('REVOKE ALL ON SEQUENCE public.%I FROM spn_partner_runtime', obj.relname);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE public.%I TO spn_partner_runtime', obj.relname);
    END LOOP;
    FOR obj IN
        SELECT p.oid::regprocedure AS signature FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname LIKE 'wl\_%' ESCAPE '\'
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', obj.signature);
        FOREACH api_role IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=api_role) THEN
                EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I', obj.signature, api_role);
            END IF;
        END LOOP;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO spn_partner_runtime', obj.signature);
    END LOOP;
    -- New objects still require this migration's RLS/grants. These defaults
    -- remove automatic API grants by THIS migration owner, not other owners.
    FOREACH api_role IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=api_role) THEN
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', api_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', api_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I', api_role);
        END IF;
    END LOOP;
END $$;

ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC;
COMMIT;
