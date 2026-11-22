"""Actual isolated PG/WASM RLS and privileges, never a real Supabase project."""

import os
from pathlib import Path
import unittest

from services.partner_runtime import SUPABASE_ACCESS_CHECK_SQL
from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge
from test_partner_runtime_postgres import ReadOnlyBridge
from test_partner_database_isolation import supabase
from services.partner_runtime import PartnerRuntime


ROOT = Path(__file__).resolve().parents[1] / "ops/partner-platform"


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class SupabaseSecurityPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        await self.sql.query(
            """CREATE ROLE spn_partner_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            CREATE ROLE anon NOLOGIN;
            CREATE ROLE authenticated NOLOGIN;
            CREATE ROLE service_role NOLOGIN BYPASSRLS;""",
            exec=True,
        )
        for name in ("commerce.sql", "finance.sql", "enrollment.sql"):
            await self.sql.query((ROOT / name).read_text(), exec=True)
        # Reproduce Supabase's pre-existing public API grants before hardening.
        await self.sql.query(
            """GRANT USAGE, CREATE ON SCHEMA public TO PUBLIC;
            GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated, service_role;
            GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;
            GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO anon, authenticated, service_role;""",
            exec=True,
        )

    async def harden(self):
        await self.sql.query((ROOT / "supabase_security.sql").read_text(), exec=True)

    async def test_idempotent_rls_runtime_dml_and_anon_api_denied(self):
        await self.harden()
        await self.harden()
        await self.sql.execute("SET ROLE spn_partner_runtime")
        try:
            check = await self.sql.fetchrow(SUPABASE_ACCESS_CHECK_SQL)
            self.assertTrue(all(value is True for value in check.values()), check)
            store = PartnerStore(lambda: self.sql)
            project = await store.create_project(
                42, "isolated-request", "sever", "Север VPN", 19900
            )
            self.assertEqual(project["slug"], "sever")
            updated = await store.update_project(
                project["id"], 42, 1, {"title": "Север Плюс"}
            )
            self.assertEqual(updated["version"], 2)
            self.assertEqual(len(await store.list_projects(42)), 1)
            for query in (
                "CREATE TABLE public.stolen (secret text)",
                "ALTER TABLE wl_projects ADD COLUMN stolen text",
                "ALTER TABLE wl_projects DISABLE ROW LEVEL SECURITY",
                "TRUNCATE wl_projects CASCADE",
            ):
                with self.subTest(query=query), self.assertRaises(RuntimeError):
                    await self.sql.execute(query)
        finally:
            await self.sql.execute("RESET ROLE")
        for role in ("anon", "authenticated", "service_role"):
            await self.sql.execute(f"SET ROLE {role}")
            try:
                for query in (
                    "SELECT * FROM wl_projects",
                    "SELECT * FROM wl_orders",
                    "SELECT * FROM wl_withdrawals",
                    "SELECT nextval('wl_project_audit_id_seq')",
                    "SELECT public.wl_finance_append_only()",
                ):
                    with self.subTest(role=role, query=query), self.assertRaises(
                        RuntimeError
                    ):
                        await self.sql.execute(query)
            finally:
                await self.sql.execute("RESET ROLE")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 1)

    async def test_no_role_owner_or_main_spn_schema_can_be_silently_accepted(self):
        await self.sql.execute("ALTER ROLE spn_partner_runtime BYPASSRLS")
        with self.assertRaises(RuntimeError):
            await self.harden()
        await self.sql.execute("ROLLBACK")
        await self.sql.execute("ALTER ROLE spn_partner_runtime NOBYPASSRLS")
        await self.sql.execute("ALTER TABLE wl_projects OWNER TO spn_partner_runtime")
        with self.assertRaises(RuntimeError):
            await self.harden()
        await self.sql.execute("ROLLBACK")
        await self.sql.execute("ALTER TABLE wl_projects OWNER TO postgres")
        await self.sql.execute("CREATE TABLE users (tg_id bigint)")
        with self.assertRaises(RuntimeError):
            await self.harden()
        await self.sql.execute("ROLLBACK")
        self.assertFalse(
            await self.sql.fetchval(
                "SELECT relrowsecurity FROM pg_class WHERE relname='wl_projects'"
            )
        )

    async def test_future_table_not_ready_until_security_migration_reapplied(self):
        await self.harden()
        await self.sql.execute(
            "CREATE TABLE wl_future_secret (id int, private_value text)"
        )
        await self.sql.execute("SET ROLE spn_partner_runtime")
        try:
            check = await self.sql.fetchrow(SUPABASE_ACCESS_CHECK_SQL)
            self.assertFalse(check["runtime_tables_safe"])
            with self.assertRaises(RuntimeError):
                await self.sql.execute("SELECT * FROM wl_future_secret")
        finally:
            await self.sql.execute("RESET ROLE")
        await self.harden()
        await self.sql.execute("SET ROLE spn_partner_runtime")
        try:
            check = await self.sql.fetchrow(SUPABASE_ACCESS_CHECK_SQL)
            self.assertTrue(all(value is True for value in check.values()), check)
            await self.sql.execute(
                "INSERT INTO wl_future_secret VALUES(1,'server only')"
            )
            self.assertEqual(
                await self.sql.fetchval("SELECT count(*) FROM wl_future_secret"), 1
            )
        finally:
            await self.sql.execute("RESET ROLE")

    async def test_role_memberships_in_both_directions_fail_closed(self):
        await self.harden()
        for parent, member in (
            ("spn_partner_runtime", "authenticated"),
            ("authenticated", "spn_partner_runtime"),
        ):
            with self.subTest(parent=parent, member=member):
                await self.sql.execute(f"GRANT {parent} TO {member}")
                await self.sql.execute("SET ROLE spn_partner_runtime")
                try:
                    check = await self.sql.fetchrow(SUPABASE_ACCESS_CHECK_SQL)
                    self.assertFalse(check["role_safe"], check)
                finally:
                    await self.sql.execute("RESET ROLE")
                if member == "authenticated":
                    # Direct table/API revokes do not remove inherited runtime
                    # DML and its server RLS policy. The guard must reject this
                    # configuration, not report the direct ACLs as sufficient.
                    await self.sql.execute("SET ROLE authenticated")
                    try:
                        self.assertEqual(
                            await self.sql.fetchval("SELECT count(*) FROM wl_projects"),
                            0,
                        )
                    finally:
                        await self.sql.execute("RESET ROLE")
                for name in ("supabase_runtime_role.sql", "supabase_security.sql"):
                    with self.subTest(script=name), self.assertRaises(RuntimeError):
                        await self.sql.query((ROOT / name).read_text(), exec=True)
                    await self.sql.execute("ROLLBACK")
                self.assertTrue(
                    await self.sql.fetchval(
                        """SELECT EXISTS(SELECT 1 FROM pg_auth_members m
                        JOIN pg_roles p ON p.oid=m.roleid
                        JOIN pg_roles r ON r.oid=m.member
                        WHERE p.rolname=$1 AND r.rolname=$2)""",
                        parent,
                        member,
                    )
                )
                # Repair is an explicit operator action, never migration magic.
                await self.sql.execute(f"REVOKE {parent} FROM {member}")
                await self.sql.execute("SET ROLE spn_partner_runtime")
                try:
                    check = await self.sql.fetchrow(SUPABASE_ACCESS_CHECK_SQL)
                    self.assertTrue(
                        all(value is True for value in check.values()), check
                    )
                finally:
                    await self.sql.execute("RESET ROLE")

    async def test_only_trusted_creator_admin_membership_without_access_is_allowed(
        self,
    ):
        await self.harden()
        await self.sql.execute("CREATE ROLE foreign_member NOLOGIN")

        async def assert_probe(expected):
            await self.sql.execute("SET ROLE spn_partner_runtime")
            try:
                check = await self.sql.fetchrow(SUPABASE_ACCESS_CHECK_SQL)
                self.assertEqual(check["role_safe"], expected, check)
            finally:
                await self.sql.execute("RESET ROLE")

        for grantor in ("postgres",):
            with self.subTest(allowed_grantor=grantor):
                await self.sql.execute(
                    "GRANT spn_partner_runtime TO postgres WITH ADMIN TRUE, "
                    f"INHERIT FALSE, SET FALSE GRANTED BY {grantor}"
                )
                for name in ("supabase_runtime_role.sql", "supabase_security.sql"):
                    await self.sql.query((ROOT / name).read_text(), exec=True)
                await assert_probe(True)
                await self.sql.execute(
                    f"REVOKE spn_partner_runtime FROM postgres GRANTED BY {grantor}"
                )

        # An inherited policy, SET ROLE permission or any other member/grantor
        # is not the automatic administrative-only creator relationship.
        cases = (
            ("postgres", "postgres", True, True, False),
            ("postgres", "postgres", True, False, True),
            ("postgres", "postgres", False, False, False),
            ("foreign_member", "postgres", True, False, False),
            ("authenticated", "postgres", True, False, False),
        )
        for member, grantor, admin, inherit, set_role in cases:
            with self.subTest(
                member=member, grantor=grantor, inherit=inherit, set_role=set_role
            ):
                await self.sql.execute(
                    f"GRANT spn_partner_runtime TO {member} WITH ADMIN {str(admin).upper()}, "
                    f"INHERIT {str(inherit).upper()}, SET {str(set_role).upper()} GRANTED BY {grantor}"
                )
                await assert_probe(False)
                for name in ("supabase_runtime_role.sql", "supabase_security.sql"):
                    with self.subTest(script=name), self.assertRaises(RuntimeError):
                        await self.sql.query((ROOT / name).read_text(), exec=True)
                    await self.sql.execute("ROLLBACK")
                await self.sql.execute(
                    f"REVOKE spn_partner_runtime FROM {member} GRANTED BY {grantor}"
                )
                await assert_probe(True)

    async def test_pg16_creator_relation_matches_supabase_but_rejects_unknown_grantor(
        self,
    ):
        for bootstrap, allowed in (
            ("supabase_admin", True),
            ("foreign_grantor", False),
        ):
            with self.subTest(bootstrap=bootstrap):
                sql = await ReadOnlyBridge().start()
                self.addAsyncCleanup(sql.close)
                # PostgreSQL records creator-admin grants as the bootstrap
                # superuser. Reproduce Supabase rather than forging catalog rows.
                await sql.query(
                    f"""CREATE ROLE test_owner SUPERUSER NOLOGIN;
                    SET SESSION AUTHORIZATION test_owner;
                    ALTER ROLE postgres RENAME TO {bootstrap};
                    CREATE ROLE postgres LOGIN CREATEROLE BYPASSRLS;
                    ALTER SCHEMA public OWNER TO postgres;
                    ALTER TABLE wl_projects OWNER TO postgres;
                    ALTER TABLE wl_project_create_requests OWNER TO postgres;
                    ALTER TABLE wl_project_audit OWNER TO postgres;
                    SET ROLE postgres;""",
                    exec=True,
                )
                await sql.query(
                    (ROOT / "supabase_runtime_role.sql").read_text(), exec=True
                )
                row = await sql.fetchrow(
                    """SELECT u.rolname AS member,g.rolname AS grantor,
                    m.admin_option,m.inherit_option,m.set_option
                    FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid
                    JOIN pg_roles u ON u.oid=m.member JOIN pg_roles g ON g.oid=m.grantor
                    WHERE p.rolname='spn_partner_runtime'"""
                )
                self.assertEqual(
                    dict(row),
                    {
                        "member": "postgres",
                        "grantor": bootstrap,
                        "admin_option": True,
                        "inherit_option": False,
                        "set_option": False,
                    },
                )
                if allowed:
                    await sql.query(
                        (ROOT / "supabase_runtime_role.sql").read_text(), exec=True
                    )
                    await sql.query(
                        (ROOT / "supabase_security.sql").read_text(), exec=True
                    )
                else:
                    for name in ("supabase_runtime_role.sql", "supabase_security.sql"):
                        with self.assertRaises(RuntimeError):
                            await sql.query((ROOT / name).read_text(), exec=True)
                        await sql.execute("ROLLBACK")
                await sql.execute("ALTER ROLE spn_partner_runtime LOGIN")
                await sql.execute("RESET ROLE")
                await sql.execute("SET ROLE spn_partner_runtime")
                check = await sql.fetchrow(SUPABASE_ACCESS_CHECK_SQL)
                self.assertEqual(check["role_safe"], allowed, check)
                await sql.execute("RESET ROLE")

    async def test_supabase_probe_rejects_owner_and_accepts_only_hardened_runtime_role(
        self,
    ):
        # A separate bridge supports actual BEGIN READ ONLY, not mocked rows.
        sql = await ReadOnlyBridge().start()
        self.addAsyncCleanup(sql.close)
        for name in (
            "commerce.sql",
            "enrollment.sql",
            "entitlements.sql",
            "traffic.sql",
            "notifications.sql",
            "subscriptions.sql",
            "finance.sql",
            "management.sql",
            "branding.sql",
            "operations.sql",
            "public_checkout.sql",
            "panel_identity.sql",
        ):
            await sql.query((ROOT / name).read_text(), exec=True)
        await sql.query((ROOT / "supabase_runtime_role.sql").read_text(), exec=True)
        await sql.query((ROOT / "supabase_runtime_role.sql").read_text(), exec=True)
        await sql.query((ROOT / "supabase_security.sql").read_text(), exec=True)
        self.assertFalse(
            await sql.fetchval(
                "SELECT rolcanlogin FROM pg_roles WHERE rolname='spn_partner_runtime'"
            )
        )
        runtime = PartnerRuntime(supabase(panel_identity_enabled=True), lambda: sql)
        owner = await runtime.probe()
        self.assertFalse(owner["database_ready"])
        self.assertEqual(owner["blockers"], ["supabase_access_policy_required"])
        await sql.execute("ALTER ROLE spn_partner_runtime LOGIN")
        await sql.execute("SET ROLE spn_partner_runtime")
        try:
            ready = await runtime.probe()
            self.assertTrue(ready["database_ready"], ready)
            self.assertFalse(ready["production_ready"])
            self.assertEqual(ready["manual_review"]["activation_expired"], 0)
            self.assertEqual(await sql.fetchval("SELECT count(*) FROM wl_projects"), 0)
        finally:
            await sql.execute("RESET ROLE")


if __name__ == "__main__":
    unittest.main()
