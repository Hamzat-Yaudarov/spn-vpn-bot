"""Payment-first enrollment on isolated PostgreSQL/WASM; no real I/O/money."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
from cryptography.fernet import Fernet

from services.partner_enrollment_store import PartnerEnrollmentStore, EnrollmentConflict
from services.partner_entry_checkout import PartnerEntryCheckoutService, recover_partner_entries_once
from services.partner_entry_pricing import EntryPricingPolicy
from services.partner_managed_bots import ManagedBotStore, ManagedBotConflict, PartnerManagedBotService
from services.partner_launcher import PartnerLaunchService
from services.partner_security import TokenVault
from services.partner_store import PartnerStore, ProjectConflict
from test_partner_enrollment_integration import FeePayments, Telegram as LaunchTelegram
from test_partner_managed_bots import MANAGER, TENANT, event
from test_partner_store_postgres import RegistryBridge


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class PaymentFirstPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        self.base = Path(__file__).resolve().parents[1] / "ops/partner-platform"
        for name in ("enrollment.sql", "owner_entry_pilot.sql", "public_entry_trial.sql",
                     "managed_bots.sql", "payment_first_entry.sql"):
            await self.sql.query((self.base / name).read_text(), exec=True)
        self.now = datetime.now(timezone.utc)
        self.registry = PartnerStore(lambda: self.sql)
        self.store = PartnerEnrollmentStore(lambda: self.sql, payment_first_enabled=True)
        self.managed = ManagedBotStore(lambda: self.sql, payment_first_enabled=True)
        self.project = await self.registry.create_project(42, "pay-first", "pay-first", "Мой сервис")
        self.pid = self.project["id"]
        self.provider = FeePayments()
        self.launcher = SimpleNamespace(ready=True, launch=AsyncMock())
        self.checkout = PartnerEntryCheckoutService(self.store, self.provider, self.launcher,
            clock=lambda: self.now)
        self.telegram = AsyncMock()

        async def me(token):
            return ({"id": 100001, "is_bot": True, "username": "ManagerBot", "can_manage_bots": True}
                    if token == MANAGER else {"id": 200002, "is_bot": True,
                        "username": "TenantBot", "first_name": "Личный сервис"})

        self.telegram.get_me.side_effect = me
        self.telegram.get_managed_bot_token.return_value = TENANT
        self.vault = TokenVault((Fernet.generate_key().decode(),))
        self.service = PartnerManagedBotService(self.managed,
            self.vault, MANAGER, "ManagerBot",
            self.telegram, enabled=True, clock=lambda: self.now)

    async def invoice(self):
        self.order = await self.store.create_entry_order(self.pid, 42, str(uuid4()), 999000)
        await self.checkout.create_checkout(self.pid, 42, self.order["id"])
        return self.order

    async def paid(self):
        await self.invoice()
        self.provider.paid(self.order["id"])
        return await self.checkout.reconcile(self.pid, 42, self.order["id"])

    async def request(self):
        project = await self.registry.get_project(self.pid, 42)
        return await self.service.request_creation(self.pid, 42, project["version"])

    async def test_pay_first_no_bot_no_launch_and_duplicate_recheck_is_safe(self):
        result = await self.paid()
        self.assertEqual(result["launch_state"], "awaiting_bot")
        self.assertTrue(result["order"]["creation_available"])
        self.assertEqual(result["order"]["price_kopecks"], 999000)
        self.launcher.launch.assert_not_awaited()
        self.assertIsNone((await self.registry.get_project(self.pid, 42))["bot"]["id"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)
        await self.checkout.reconcile(self.pid, 42, self.order["id"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)
        self.assertEqual((await recover_partner_entries_once(self.store, self.checkout))["checked"], 0)

    async def test_unpaid_canceled_or_absent_receipt_cannot_create_or_bind(self):
        with self.assertRaises(ManagedBotConflict):
            await self.request()
        await self.invoice()
        with self.assertRaises(ManagedBotConflict):
            await self.request()
        self.assertFalse(await self.service.handle_update(event()))
        self.telegram.get_managed_bot_token.assert_not_awaited()
        saved = await self.store.get_entry_order(self.pid, 42, self.order["id"])
        await self.store.cancel_verified_invoice(self.pid, self.order["id"], saved["provider_payment_id"])
        with self.assertRaises(ManagedBotConflict):
            await self.request()

    async def test_verified_bot_bind_creates_one_recoverable_job_atomically(self):
        await self.paid()
        await self.request()
        self.assertTrue(await self.service.handle_update(event()))
        self.assertFalse(await self.service.handle_update(event()))
        order = await self.store.get_entry_order(self.pid, 42, self.order["id"])
        self.assertEqual(order["bot_id"], 200002)
        self.assertEqual(order["status"], "paid_pending_launch")
        self.assertEqual((await self.registry.get_project(self.pid, 42))["title"], "Личный сервис")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 1)
        due = await self.store.list_recoverable_entry_orders()
        self.assertEqual([row["id"] for row in due], [self.order["id"]])
        with self.assertRaises(ManagedBotConflict):
            await self.request()
        for sql in ("UPDATE wl_entry_orders SET bot_id=300003 WHERE project_id=$1",
                    "UPDATE wl_entry_orders SET bot_id=NULL WHERE project_id=$1",
                    "UPDATE wl_projects SET bot_id=300003 WHERE id=$1"):
            with self.assertRaises(asyncpg.CheckViolationError):
                await self.sql.execute(sql, self.pid)

    async def test_reserved_bot_survives_network_error_expiry_and_restart(self):
        await self.paid()
        await self.request()
        self.telegram.get_managed_bot_token.side_effect = RuntimeError("ambiguous Telegram response")
        with self.assertRaises(RuntimeError):
            await self.service.handle_update(event())
        self.assertEqual(await self.sql.fetchval("SELECT bot_id FROM wl_managed_bot_requests"), 200002)
        self.assertIsNone(await self.sql.fetchval("SELECT bot_id FROM wl_entry_orders"))
        self.now += timedelta(hours=1)
        self.telegram.get_managed_bot_token.side_effect = None
        self.service.store = ManagedBotStore(lambda: self.sql, payment_first_enabled=True)
        self.assertTrue(await self.service.retry_pending(self.pid, 42))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 1)

    async def test_final_binding_failure_rolls_back_identity_receipt_and_job(self):
        await self.paid()
        await self.request()
        self.sql.fail_audit = True
        with self.assertRaises(RuntimeError):
            await self.service.handle_update(event())
        self.assertIsNone(await self.sql.fetchval("SELECT bot_id FROM wl_projects"))
        self.assertIsNone(await self.sql.fetchval("SELECT bot_id FROM wl_entry_orders"))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT status FROM wl_managed_bot_requests"), "pending")
        self.sql.fail_audit = False
        self.assertTrue(await self.service.retry_pending(self.pid, 42))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 1)

    async def test_paid_receipt_rolls_back_with_outbox_failure_then_reconciles_once(self):
        await self.invoice()
        self.provider.paid(self.order["id"])
        execute = self.sql.execute

        async def fail_outbox_once(query, *params):
            if "INSERT INTO wl_partner_outbox" in query:
                raise RuntimeError("injected local transaction failure")
            return await execute(query, *params)

        self.sql.execute = fail_outbox_once
        with self.assertRaises(RuntimeError):
            await self.checkout.reconcile(self.pid, 42, self.order["id"])
        self.sql.execute = execute
        saved = await self.store.get_entry_order(self.pid, 42, self.order["id"])
        self.assertEqual((saved["status"], saved["paid_at"]), ("created", None))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 0)
        with self.assertRaises(ManagedBotConflict):
            await self.request()
        restarted = PartnerEntryCheckoutService(
            PartnerEnrollmentStore(lambda: self.sql, payment_first_enabled=True),
            self.provider, self.launcher)
        result = await restarted.reconcile(self.pid, 42, self.order["id"])
        self.assertEqual(result["launch_state"], "awaiting_bot")
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)
        self.assertEqual(len(self.provider.creates), 1)

    async def test_bound_paid_bot_recovers_launch_after_ambiguous_telegram_response(self):
        await self.paid()
        await self.request()
        await self.service.handle_update(event())
        telegram = LaunchTelegram()

        async def verified_identity(token):
            self.assertEqual(token, TENANT)
            telegram.calls.append("getMe")
            return {"id": 200002, "is_bot": True, "username": "TenantBot"}

        telegram.get_me = verified_identity
        telegram.fail_after_set_once = True
        launcher = PartnerLaunchService(self.store, self.vault, telegram,
            webhook_base_url="https://bots.example.test",
            webhook_secret_key=b"isolated-payment-first-launch-key-32bytes",
            clients_enabled=True)
        checkout = PartnerEntryCheckoutService(self.store, self.provider, launcher)
        result = await checkout.reconcile(self.pid, 42, self.order["id"])
        self.assertEqual(result["launch_state"], "retry")
        self.assertEqual((await self.registry.get_project(self.pid, 42))["status"], "draft")
        secret = telegram.secret
        restarted_store = PartnerEnrollmentStore(lambda: self.sql, payment_first_enabled=True)
        launcher.store = restarted_store
        restarted = PartnerEntryCheckoutService(restarted_store, None, launcher)
        result = await restarted.reconcile(self.pid, 42, self.order["id"])
        self.assertEqual(result["launch_state"], "launched")
        self.assertFalse(result["order"]["creation_available"])
        self.assertEqual((await self.registry.get_project(self.pid, 42))["status"], "active")
        self.assertEqual(telegram.secret, secret)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)
        self.assertEqual(len(self.provider.creates), 1)
        calls = list(telegram.calls)
        await restarted.reconcile(self.pid, 42, self.order["id"])
        self.assertEqual(telegram.calls, calls)

    async def test_wrong_canonical_amount_or_owner_cannot_unlock_creation(self):
        await self.invoice()
        saved = await self.store.get_entry_order(self.pid, 42, self.order["id"])
        for payment_id, amount in ((saved["provider_payment_id"], 1000),
                                   ("different-receipt", 999000)):
            with self.assertRaises(EnrollmentConflict):
                await self.store.record_verified_payment(self.pid, self.order["id"],
                    payment_id, amount, "RUB")
        project = await self.registry.get_project(self.pid, 42)
        from services.partner_store import ProjectNotFound
        with self.assertRaises(ProjectNotFound):
            await self.service.request_creation(self.pid, 99, project["version"])
        self.assertIsNone(await self.sql.fetchval("SELECT paid_at FROM wl_entry_orders"))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_managed_bot_requests"), 0)

    async def test_normal_price_does_not_reprice_or_replace_previously_issued_trial_invoice(self):
        trial = PartnerEnrollmentStore(lambda: self.sql, payment_first_enabled=True,
            entry_pricing=EntryPricingPolicy(public_trial_enabled=True))
        order = await trial.create_entry_order(self.pid, 42, str(uuid4()), 1000)
        trial_checkout = PartnerEntryCheckoutService(trial, self.provider, self.launcher)
        await trial_checkout.create_checkout(self.pid, 42, order["id"])
        self.provider.paid(order["id"])
        result = await self.checkout.reconcile(self.pid, 42, order["id"])
        self.assertEqual((result["launch_state"], result["order"]["price_kopecks"]),
                         ("awaiting_bot", 1000))
        await self.request()
        await self.service.handle_update(event())
        self.assertEqual(await self.sql.fetchval("SELECT price_kopecks FROM wl_entry_orders"), 1000)
        self.assertEqual(await self.sql.fetchval("SELECT bot_id FROM wl_entry_orders"), 200002)
        self.assertEqual(len(self.provider.creates), 1)

    async def test_untrusted_binding_and_financial_mutations_are_rejected_by_database(self):
        await self.paid()
        for sql in ("UPDATE wl_entry_orders SET bot_id=200002 WHERE project_id=$1",
                    "UPDATE wl_entry_orders SET price_kopecks=1000 WHERE project_id=$1"):
            with self.assertRaises(asyncpg.CheckViolationError):
                await self.sql.execute(sql, self.pid)
        # A project write alone cannot consume a paid entitlement either.
        with self.assertRaises(ProjectConflict):
            await self.registry.save_bot_connection(self.pid, 42, 1, 200002, "TenantBot", "x" * 80)
        await self.request()
        self.assertFalse(await self.service.handle_update(event(owner=99)))
        self.assertIsNone(await self.sql.fetchval("SELECT bot_id FROM wl_entry_orders"))

    async def test_legacy_bot_first_and_saved_trial_price_survive_migration(self):
        prior = await self.registry.create_project(6910097562, "legacy-key", "legacy", "Legacy")
        prior = await self.registry.save_bot_connection(prior["id"], 6910097562,
            prior["version"], 300003, "LegacyBot", "x" * 80)
        pilot = PartnerEnrollmentStore(lambda: self.sql,
            entry_pricing=EntryPricingPolicy(owner_pilot_enabled=True))
        order = await pilot.create_entry_order(prior["id"], 6910097562, str(uuid4()), 1000)
        order = await pilot.save_invoice(prior["id"], 6910097562, order["id"], "legacy-payment", "")
        # Normal pricing still reconciles an already-issued 10 RUB receipt.
        paid = await self.store.record_verified_payment(prior["id"], order["id"],
            "legacy-payment", 1000, "RUB", now=self.now)
        self.assertEqual((paid["price_kopecks"], paid["bot_id"]), (1000, 300003))
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 1)
        with self.assertRaises(EnrollmentConflict):
            await PartnerEnrollmentStore(lambda: self.sql).create_entry_order(self.pid, 42, str(uuid4()))
        await self.sql.query((self.base / "payment_first_entry.sql").read_text(), exec=True)
        self.assertEqual(await self.sql.fetchval("SELECT price_kopecks FROM wl_entry_orders WHERE id=$1", order["id"]), 1000)

    async def test_migration_preserves_all_preexisting_receipt_job_and_project_values(self):
        # Start at the actual old schema rather than inserting legacy-shaped
        # rows only after migration. Compare complete rows, not just prices.
        prior_sql = await RegistryBridge().start()
        self.addAsyncCleanup(prior_sql.close)
        for name in ("enrollment.sql", "owner_entry_pilot.sql", "public_entry_trial.sql",
                     "managed_bots.sql"):
            await prior_sql.query((self.base / name).read_text(), exec=True)
        registry = PartnerStore(lambda: prior_sql)
        project = await registry.create_project(6910097562, "before-migration", "before-migration", "Legacy")
        project = await registry.save_bot_connection(project["id"], 6910097562,
            project["version"], 300003, "LegacyBot", "x" * 80)
        store = PartnerEnrollmentStore(lambda: prior_sql,
            entry_pricing=EntryPricingPolicy(owner_pilot_enabled=True))
        order = await store.create_entry_order(project["id"], 6910097562, str(uuid4()), 1000)
        await store.save_invoice(project["id"], 6910097562, order["id"], "old-receipt", "")
        await store.record_verified_payment(project["id"], order["id"], "old-receipt", 1000, "RUB")
        snapshots = {}
        for table in ("wl_projects", "wl_entry_orders", "wl_launch_jobs", "wl_partner_outbox",
                      "wl_project_audit", "wl_managed_bot_requests"):
            snapshots[table] = await prior_sql.fetch(f"SELECT * FROM {table}")
        await prior_sql.query((self.base / "payment_first_entry.sql").read_text(), exec=True)
        for table, before in snapshots.items():
            self.assertEqual(await prior_sql.fetch(f"SELECT * FROM {table}"), before, table)


if __name__ == "__main__":
    unittest.main()
