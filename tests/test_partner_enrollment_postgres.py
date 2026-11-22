"""Actual isolated PostgreSQL/WASM enrollment SQL; no live DSN/env/network.

Uses RegistryBridge with only test schemas. Single bridge cannot establish
native multi-connection PostgreSQL concurrency correctness; lease tests emulate
bounded pools separately and public release still needs native staging races.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

import asyncpg

from test_partner_store_postgres import RegistryBridge
from services.partner_store import PartnerStore, ProjectConflict
from services.partner_enrollment_store import (
    PartnerEnrollmentStore, InvalidEnrollment, EnrollmentNotFound, EnrollmentConflict,
)


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM module is not configured")
class EnrollmentPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        base = Path(__file__).resolve().parents[1] / "ops/partner-platform"
        self.schema = (base / "enrollment.sql").read_text()
        await self.sql.query((base / "commerce.sql").read_text(), exec=True)
        await self.sql.query(self.schema, exec=True)
        self.registry = PartnerStore(lambda: self.sql)
        self.store = PartnerEnrollmentStore(lambda: self.sql)
        self.project = await self.registry.create_project(42, "enroll-create-123", "enroll-test", "Мой VPN")
        self.pid = self.project["id"]
        self.project = await self.registry.save_bot_connection(self.pid, 42, 1, 123456, "EnrollBot", "cipher:" + "x" * 100)
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.webhook = "https://bots.example.org/partners/hooks/" + self.pid
        self.fingerprint = "a" * 64

    async def create(self, *, key=None):
        return await self.store.create_entry_order(self.pid, 42, key or str(uuid4()))

    async def paid(self, order=None, *, provider=None):
        order = order or await self.create()
        provider = provider or "entry-" + order["id"]
        await self.store.save_invoice(self.pid, 42, order["id"], provider, "https://pay.example.org/" + provider)
        return await self.store.record_verified_payment(self.pid, order["id"], provider, 999000, "RUB", now=self.now)

    async def complete(self, order):
        return await self.store.complete_launch(self.pid, order["id"], bot_id=123456,
            webhook_url=self.webhook, secret_fingerprint=self.fingerprint)

    async def test_schema_reapplication_constraints_and_tenant_ownership(self):
        await self.sql.query(self.schema, exec=True)
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.sql.execute("""INSERT INTO wl_entry_orders(id,project_id,owner_tg_id,request_key,bot_id)
                VALUES($1,$2,99,$3,123456)""", str(uuid4()), self.pid, str(uuid4()))
        order = await self.create()
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("UPDATE wl_entry_orders SET price_kopecks=19900 WHERE id=$1", order["id"])
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("UPDATE wl_entry_orders SET status='paid_pending_launch' WHERE id=$1", order["id"])
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("UPDATE wl_entry_orders SET provider_payment_id='half-invoice' WHERE id=$1", order["id"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 1)
        self.assertEqual((await self.registry.get_project(self.pid, 42))["status"], "draft")

    async def test_idempotent_request_restart_and_only_one_uncanceled_order(self):
        key = str(uuid4())
        first = await self.create(key=key)
        restarted = PartnerEnrollmentStore(lambda: self.sql)
        self.assertEqual(first, await restarted.create_entry_order(self.pid, 42, key))
        self.assertEqual(first, await restarted.get_current_entry_order(self.pid, 42))
        self.assertEqual(first["tg_id"], 42)
        self.assertEqual(first["purpose"], "entry")
        self.assertEqual(first["price_kopecks"], 999000)
        with self.assertRaises(EnrollmentConflict):
            await self.create()
        second = await self.registry.create_project(42, "second-create-123", "enroll-second", "Другой VPN")
        await self.registry.save_bot_connection(second["id"], 42, 1, 654321, "OtherEnrollBot", "cipher:" + "y" * 100)
        with self.assertRaises(EnrollmentConflict):
            await self.store.create_entry_order(second["id"], 42, key)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 1)

    async def test_missing_bot_and_non_draft_states_forbid_new_entry(self):
        other = await self.registry.create_project(42, "unconnected-create", "unconnected", "Без бота")
        with self.assertRaises(EnrollmentConflict):
            await self.store.create_entry_order(other["id"], 42, str(uuid4()))
        for status in ("active", "suspended", "closed"):
            await self.sql.execute("UPDATE wl_projects SET status=$2 WHERE id=$1", self.pid, status)
            with self.assertRaises(EnrollmentConflict):
                await self.create()
        await self.sql.execute("UPDATE wl_projects SET status='ready' WHERE id=$1", self.pid)
        self.assertEqual((await self.create())["status"], "created")

    async def test_owner_and_foreign_project_cannot_access_invoice_or_entry(self):
        order = await self.create()
        for operation, args in (
            (self.store.get_entry_order, (self.pid, 99, order["id"])),
            (self.store.get_current_entry_order, (self.pid, 99)),
            (self.store.create_entry_order, (self.pid, 99, str(uuid4()))),
            (self.store.save_invoice, (self.pid, 99, order["id"], "p", "")),
            (self.store.get_entry_order, (str(uuid4()), 42, order["id"])),
        ):
            with self.assertRaises(EnrollmentNotFound):
                await operation(*args)
        self.assertIsNone((await self.store.get_entry_order(self.pid, 42, order["id"]))["provider_payment_id"])

    async def test_new_checkout_guard_rechecks_project_before_provider_invoice(self):
        order = await self.create()
        self.assertEqual(await self.store.get_checkout_order(self.pid, 42, order["id"]), order)
        with self.assertRaises(EnrollmentNotFound):
            await self.store.get_checkout_order(self.pid, 99, order["id"])
        for status in ("suspended", "closed", "active"):
            await self.sql.execute("UPDATE wl_projects SET status=$2 WHERE id=$1", self.pid, status)
            with self.assertRaises(EnrollmentConflict):
                await self.store.get_checkout_order(self.pid, 42, order["id"])
            self.assertIsNone((await self.store.get_entry_order(self.pid, 42, order["id"]))["provider_payment_id"])
        await self.sql.execute("UPDATE wl_projects SET status='ready' WHERE id=$1", self.pid)
        self.assertEqual(await self.store.get_checkout_order(self.pid, 42, order["id"]), order)
        await self.store.save_invoice(self.pid, 42, order["id"], "entry-already-bound", "")
        with self.assertRaises(EnrollmentConflict):
            await self.store.get_checkout_order(self.pid, 42, order["id"])
        await self.sql.execute("UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid)
        # Issued invoice stays owned and may settle even after suspension.
        paid = await self.store.record_verified_payment(self.pid, order["id"], "entry-already-bound", 999000, "RUB", now=self.now)
        self.assertEqual(paid["status"], "paid_pending_launch")
        self.assertEqual((await self.registry.get_project(self.pid, 42))["status"], "suspended")

    async def test_invoice_frozen_binding_and_global_entry_provider_uniqueness(self):
        order = await self.create()
        invoice = await self.store.save_invoice(self.pid, 42, order["id"], "entry-unique", "")
        self.assertEqual(invoice, await self.store.save_invoice(self.pid, 42, order["id"], "entry-unique", ""))
        with self.assertRaises(EnrollmentConflict):
            await self.store.save_invoice(self.pid, 42, order["id"], "entry-other", "")
        with self.assertRaises(EnrollmentConflict):
            await self.store.save_invoice(self.pid, 42, order["id"], "entry-unique", "https://pay.example.org/new")
        await self.store.cancel_verified_invoice(self.pid, order["id"], "entry-unique")
        other = await self.create()
        with self.assertRaises(EnrollmentConflict):
            await self.store.save_invoice(self.pid, 42, other["id"], "entry-unique", "")
        self.assertIsNone((await self.store.get_entry_order(self.pid, 42, other["id"]))["provider_payment_id"])

    async def test_paid_entry_atomic_job_outbox_no_partner_share_or_subscription(self):
        paid = await self.paid()
        self.assertEqual(paid["status"], "paid_pending_launch")
        self.assertEqual(paid["paid_at"], self.now)
        self.assertEqual((await self.registry.get_project(self.pid, 42))["status"], "draft")
        for table in ("wl_orders", "wl_customers", "wl_subscriptions", "wl_revenue_ledger", "wl_activation_jobs"):
            self.assertEqual(await self.sql.fetchval(f"SELECT count(*) FROM {table}"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox WHERE kind='entry_paid' AND owner_tg_id=42"), 1)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.sql.execute("UPDATE wl_launch_jobs SET status='complete',completed_at=now() WHERE order_id=$1", paid["id"])
        again = await self.store.record_verified_payment(self.pid, paid["id"], paid["provider_payment_id"], 999000, "RUB", now=self.now+timedelta(days=1))
        self.assertEqual(again, paid)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)

    async def test_notification_failure_rolls_back_paid_mark_and_job(self):
        order = await self.create()
        await self.store.save_invoice(self.pid, 42, order["id"], "entry-failure", "")
        original = self.sql.execute

        async def fail_notification(sql, *params):
            if "INSERT INTO wl_partner_outbox" in sql:
                raise RuntimeError("notification-insert-failure")
            return await original(sql, *params)

        self.sql.execute = fail_notification
        with self.assertRaisesRegex(RuntimeError, "notification-insert-failure"):
            await self.store.record_verified_payment(self.pid, order["id"], "entry-failure", 999000, "RUB", now=self.now)
        self.sql.execute = original
        self.assertEqual((await self.store.get_entry_order(self.pid, 42, order["id"]))["status"], "created")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 0)
        self.assertEqual((await self.store.record_verified_payment(self.pid, order["id"], "entry-failure", 999000, "RUB", now=self.now))["status"], "paid_pending_launch")

    async def test_cancel_requires_saved_invoice_unpaid_and_canceled_order_reopens(self):
        order = await self.create()
        await self.store.save_invoice(self.pid, 42, order["id"], "cancel-me", "")
        with self.assertRaises(EnrollmentConflict):
            await self.store.cancel_verified_invoice(self.pid, order["id"], "wrong")
        canceled = await self.store.cancel_verified_invoice(self.pid, order["id"], "cancel-me")
        self.assertEqual(canceled["status"], "canceled")
        self.assertIsNone(await self.store.get_current_entry_order(self.pid, 42))
        self.assertEqual(canceled, await self.store.create_entry_order(self.pid, 42, order["request_key"]))
        with self.assertRaises(EnrollmentConflict):
            await self.store.record_verified_payment(self.pid, order["id"], "cancel-me", 999000, "RUB")
        paid = await self.paid()
        self.assertNotEqual(paid["id"], canceled["id"])
        self.assertEqual(await self.store.cancel_verified_invoice(self.pid, paid["id"], paid["provider_payment_id"]), paid)
        with self.assertRaises(EnrollmentConflict):
            await self.create()

    async def test_unpaid_wrong_invoice_and_wrong_launch_are_never_activated(self):
        order = await self.create()
        with self.assertRaises(EnrollmentConflict):
            await self.store.record_verified_payment(self.pid, order["id"], "unbound", 999000, "RUB")
        with self.assertRaises(EnrollmentConflict):
            await self.complete(order)
        with self.assertRaises(EnrollmentNotFound):
            await self.store.get_launch(self.pid, order["id"])
        paid = await self.paid(order)
        with self.assertRaises(EnrollmentConflict):
            await self.store.record_verified_payment(self.pid, paid["id"], "wrong", 999000, "RUB")
        with self.assertRaises(EnrollmentConflict):
            await self.store.complete_launch(self.pid, paid["id"], bot_id=999999, webhook_url=self.webhook, secret_fingerprint=self.fingerprint)
        self.assertEqual((await self.registry.get_project(self.pid, 42))["status"], "draft")

    async def test_private_launch_current_token_rotation_and_no_public_ciphertext(self):
        order = await self.paid()
        self.assertNotIn("cipher:", str(order))
        private = await self.store.get_launch(self.pid, order["id"])
        self.assertTrue(private["encrypted_token"].startswith("cipher:"))
        self.assertEqual(private["bot_id"], 123456)
        self.assertEqual(private["project_version"], 2)
        changed = await self.registry.save_bot_connection(self.pid, 42, 2, 123456, "RotatedEnrollBot", "rotated-cipher:" + "z" * 100)
        private = await self.store.get_launch(self.pid, order["id"])
        self.assertEqual(private["project_version"], changed["version"])
        self.assertTrue(private["encrypted_token"].startswith("rotated-cipher:"))
        self.assertEqual(private["bot_username"], "RotatedEnrollBot")
        self.assertNotIn("cipher", str(await self.store.get_current_entry_order(self.pid, 42)))
        self.assertNotIn("cipher", str(await self.store.list_recoverable_launches()))

    async def test_bot_identity_trigger_locks_after_order_no_partial_registry_update(self):
        order = await self.create()
        before = await self.registry.get_project(self.pid, 42)
        audit_count = await self.sql.fetchval("SELECT count(*) FROM wl_project_audit")
        # Registry maps the additive trigger to ProjectConflict in the full
        # integration. Permit raw DB CheckViolation in standalone store tests.
        with self.assertRaises((asyncpg.CheckViolationError, ProjectConflict)):
            await self.registry.save_bot_connection(self.pid, 42, before["version"], 555555, "DifferentEnrollBot", "other-cipher:" + "y" * 100)
        self.assertEqual(await self.registry.get_project(self.pid, 42), before)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_project_audit"), audit_count)
        self.assertTrue((await self.sql.fetchval("SELECT encrypted_token FROM wl_projects WHERE id=$1", self.pid)).startswith("cipher:"))
        await self.store.save_invoice(self.pid, 42, order["id"], "cancel-bot-change", "")
        await self.store.cancel_verified_invoice(self.pid, order["id"], "cancel-bot-change")
        changed = await self.registry.save_bot_connection(self.pid, 42, before["version"], 555555, "DifferentEnrollBot", "other-cipher:" + "y" * 100)
        self.assertEqual(changed["bot"]["id"], 555555)

    async def test_snapshot_mismatch_and_suspension_cannot_complete_or_discard_paid(self):
        order = await self.paid()
        # Corruption/legacy upgrade defence beyond the installed identity guard.
        await self.sql.execute("ALTER TABLE wl_projects DISABLE TRIGGER wl_entry_bot_identity_guard")
        await self.sql.execute("UPDATE wl_projects SET bot_id=555555 WHERE id=$1", self.pid)
        with self.assertRaises(EnrollmentConflict):
            await self.store.get_launch(self.pid, order["id"])
        with self.assertRaises(EnrollmentConflict):
            await self.complete(order)
        await self.sql.execute("UPDATE wl_projects SET bot_id=123456,status='suspended' WHERE id=$1", self.pid)
        with self.assertRaises(EnrollmentConflict):
            await self.store.get_launch(self.pid, order["id"])
        with self.assertRaises(EnrollmentConflict):
            await self.complete(order)
        self.assertEqual((await self.store.get_entry_order(self.pid, 42, order["id"]))["status"], "paid_pending_launch")

    async def test_launch_completion_atomic_idempotent_with_safe_durable_metadata(self):
        paid = await self.paid()
        launched = await self.complete(paid)
        project = await self.registry.get_project(self.pid, 42)
        self.assertEqual(project["status"], "active")
        self.assertEqual(project["version"], 3)
        self.assertEqual(launched["status"], "launched")
        self.assertIsNotNone(launched["launched_at"])
        self.assertEqual(await self.complete(paid), launched)
        self.assertEqual((await self.registry.get_project(self.pid, 42))["version"], 3)
        job = await self.sql.fetchrow("SELECT * FROM wl_launch_jobs")
        self.assertEqual(job["webhook_url"], self.webhook)
        self.assertEqual(job["secret_fingerprint"], self.fingerprint)
        self.assertEqual(job["status"], "complete")
        self.assertNotIn("webhook_url", launched)
        self.assertNotIn("secret_fingerprint", launched)
        self.assertNotIn("cipher:", str(job))
        self.assertNotIn(self.webhook, str(await self.sql.fetch("SELECT details FROM wl_project_audit")))
        with self.assertRaises(EnrollmentConflict):
            await self.store.complete_launch(self.pid, paid["id"], bot_id=123456, webhook_url=self.webhook + "/changed", secret_fingerprint=self.fingerprint)
        with self.assertRaises(EnrollmentConflict):
            await self.store.get_launch(self.pid, paid["id"])
        self.assertEqual(await self.store.list_recoverable_launches(), [])
        self.assertEqual(await self.store.list_recoverable_entry_orders(), [])
        self.assertEqual((await self.store.record_verified_payment(self.pid, paid["id"], paid["provider_payment_id"], 999000, "RUB"))["status"], "launched")

    async def test_launch_failure_rolls_back_project_active_and_recovers_after_restart(self):
        paid = await self.paid()
        original = self.sql.execute

        async def fail_job_completion(sql, *params):
            if "UPDATE wl_launch_jobs SET status='complete'" in sql:
                raise RuntimeError("launch-job-write-failure")
            return await original(sql, *params)

        self.sql.execute = fail_job_completion
        with self.assertRaisesRegex(RuntimeError, "launch-job-write-failure"):
            await self.complete(paid)
        self.sql.execute = original
        self.assertEqual((await self.registry.get_project(self.pid, 42))["status"], "draft")
        self.assertEqual((await self.registry.get_project(self.pid, 42))["version"], 2)
        restarted = PartnerEnrollmentStore(lambda: self.sql)
        self.assertEqual((await restarted.get_entry_order(self.pid, 42, paid["id"]))["status"], "paid_pending_launch")
        work = await restarted.list_recoverable_launches()
        self.assertEqual(work, [{"project_id": self.pid, "owner_tg_id": 42, "order_id": paid["id"]}])
        self.assertEqual((await self.complete(paid))["status"], "launched")

    async def test_backoff_consent_and_combined_recovery_have_no_secret_values(self):
        order = await self.create()
        self.assertEqual(await self.store.list_recoverable_entry_orders(), [])
        await self.store.save_invoice(self.pid, 42, order["id"], "entry-recover", "")
        self.assertEqual(await self.store.list_recoverable_entry_orders(), [{"project_id": self.pid, "owner_tg_id": 42, "id": order["id"]}])
        paid = await self.store.record_verified_payment(self.pid, order["id"], "entry-recover", 999000, "RUB", now=self.now)
        await self.store.mark_launch_retry(self.pid, paid["id"], now=self.now)
        job = await self.sql.fetchrow("SELECT * FROM wl_launch_jobs")
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["next_attempt_at"], self.now + timedelta(seconds=30))
        for _ in range(9):
            await self.store.mark_launch_retry(self.pid, paid["id"], now=self.now)
        self.assertEqual((await self.sql.fetchrow("SELECT * FROM wl_launch_jobs"))["next_attempt_at"], self.now + timedelta(hours=1))
        deferred = await self.store.mark_launch_retry(self.pid, paid["id"], needs_webhook_replace=True, now=self.now)
        self.assertTrue(deferred["needs_webhook_replace"])
        self.assertTrue((await self.store.get_entry_order(self.pid, 42, paid["id"]))["needs_webhook_replace"])
        self.assertEqual(await self.store.list_recoverable_launches(), [])
        self.assertEqual(await self.store.list_recoverable_entry_orders(), [])
        await self.store.mark_launch_retry(self.pid, paid["id"], needs_webhook_replace=False, now=self.now)
        self.assertEqual(len(await self.store.list_recoverable_entry_orders()), 1)
        await self.complete(paid)
        self.assertEqual((await self.store.mark_launch_retry(self.pid, paid["id"], needs_webhook_replace=True))["status"], "launched")
        self.assertFalse((await self.store.get_entry_order(self.pid, 42, paid["id"]))["needs_webhook_replace"])

    async def test_legacy_active_bot_identity_stays_frozen_without_entry(self):
        await self.sql.execute("UPDATE wl_projects SET status='active' WHERE id=$1", self.pid)
        with self.assertRaises((asyncpg.CheckViolationError, ProjectConflict)):
            await self.registry.save_bot_connection(self.pid, 42, 2, 555555, "OtherEnrollBot", "cipher:" + "x" * 100)
        self.assertEqual((await self.registry.save_bot_connection(self.pid, 42, 2, 123456, "EnrollBot", "cipher:" + "z" * 100))["version"], 3)
