"""Owner-serialized server defaults, with SQL contract and isolated PostgreSQL."""
import os
from pathlib import Path
import unittest
from uuid import uuid4

from services.partner_store import PartnerStore, InvalidProject, ProjectConflict
from test_partner_store import RecordingConnection, project_row
from test_partner_store_postgres import RegistryBridge


class PayFirstRegistryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_project_reused_under_owner_lock_without_writes(self):
        conn = RecordingConnection(rows=[project_row()])
        result = await PartnerStore(lambda: conn).prepare_enrollment_project(42, str(uuid4()))
        self.assertEqual(result["owner_tg_id"], 42)
        self.assertIn("pg_advisory_xact_lock", conn.calls[0][1])
        self.assertIn("p.owner_tg_id=$1", conn.calls[1][1])
        self.assertIn("FOR UPDATE OF p", conn.calls[1][1])
        self.assertFalse(any("INSERT" in sql for _, sql, _ in conn.calls))

    async def test_new_project_defaults_and_key_are_atomic_with_audit(self):
        def inserted(sql, args):
            return project_row(id=args[0], slug=args[2], title=args[3], price_kopecks=args[4])
        key = str(uuid4())
        conn = RecordingConnection(rows=[None, None, inserted])
        result = await PartnerStore(lambda: conn).prepare_enrollment_project(42, key)
        self.assertTrue(conn.committed)
        self.assertEqual((result["title"], result["price_kopecks"]), ("Мой сервис", 19900))
        self.assertTrue(result["slug"].startswith("service-"))
        self.assertTrue(any("INSERT INTO wl_project_create_requests" in sql for _, sql, _ in conn.calls))
        self.assertTrue(any("INSERT INTO wl_project_audit" in sql for _, sql, _ in conn.calls))

    async def test_invalid_owner_key_and_missing_prior_project_cannot_create(self):
        conn = RecordingConnection()
        for owner, key in ((True, str(uuid4())), (0, str(uuid4())), (42, "invalid")):
            with self.assertRaises(InvalidProject):
                await PartnerStore(lambda: conn).prepare_enrollment_project(owner, key)
        self.assertEqual(conn.calls, [])
        conn = RecordingConnection(rows=[None, {"project_id": uuid4(), "fingerprint": "0" * 64}])
        with self.assertRaises(ProjectConflict):
            await PartnerStore(lambda: conn).prepare_enrollment_project(42, str(uuid4()))
        self.assertTrue(conn.rolled_back)


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class PayFirstRegistryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        base = Path(__file__).resolve().parents[1] / "ops/partner-platform"
        await self.sql.query((base / "enrollment.sql").read_text(), exec=True)
        self.store = PartnerStore(lambda: self.sql)

    async def test_different_keys_and_restart_recover_same_owner_draft(self):
        first = await self.store.prepare_enrollment_project(42, str(uuid4()))
        retry = await PartnerStore(lambda: self.sql).prepare_enrollment_project(42, str(uuid4()))
        self.assertEqual(first["id"], retry["id"])
        foreign = await self.store.prepare_enrollment_project(99, str(uuid4()))
        self.assertNotEqual(foreign["id"], first["id"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 2)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_create_requests"), 2)

    async def test_existing_paid_project_wins_over_newer_empty_draft(self):
        first = await self.store.create_project(42, "paid-project", "paid-project", "Сервис")
        await self.store.save_bot_connection(first["id"], 42, 1, 777, "PaidBot", "cipher:" + "x" * 100)
        await self.sql.execute("""INSERT INTO wl_entry_orders
            (id, project_id, owner_tg_id, request_key, bot_id, price_kopecks, status, paid_at, provider_payment_id, confirmation_url)
            VALUES ($1,$2,42,$3,777,999000,'paid_pending_launch',now(),'saved-paid-receipt','')""",
            uuid4(), first["id"], uuid4())
        await self.store.create_project(42, "new-project", "new-project", "Новый")
        recovered = await self.store.prepare_enrollment_project(42, str(uuid4()))
        self.assertEqual(recovered["id"], first["id"])
        listing = await self.store.list_enrollment_projects(42)
        self.assertEqual(listing[0]["id"], first["id"])
        self.assertEqual(len(listing), 2)

    async def test_closed_project_is_returned_for_explicit_support_not_new_charge(self):
        first = await self.store.prepare_enrollment_project(42, str(uuid4()))
        await self.sql.execute("UPDATE wl_projects SET status='closed' WHERE id=$1", first["id"])
        recovered = await self.store.prepare_enrollment_project(42, str(uuid4()))
        self.assertEqual(recovered["id"], first["id"])
        self.assertEqual(recovered["status"], "closed")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_projects"), 1)


if __name__ == "__main__":
    unittest.main()
