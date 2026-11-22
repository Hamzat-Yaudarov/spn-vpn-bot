"""Real in-memory PostgreSQL after-launch events and delivery leases, no live DB."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from services.partner_enrollment_store import InvalidEnrollment, PartnerEnrollmentStore
from services.partner_entry_notifications import EntryLaunchNotificationStore, EntryLaunchNotificationService
from services.partner_notifications import NotificationResult, PartnerNotificationStore
from services.partner_launcher import PartnerLaunchService
from services.partner_security import TokenVault
from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge


ROOT = Path(__file__).resolve().parents[1] / "ops/partner-platform"
MANAGER = "987654:" + "y" * 32


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured")
class EntryLaunchNotificationPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        for name in ("commerce.sql", "enrollment.sql", "notifications.sql", "entry_launch_notifications.sql"):
            await self.sql.query((ROOT / name).read_text(), exec=True)
        self.registry = PartnerStore(lambda: self.sql)
        project = await self.registry.create_project(42, "launch-notice", "launch-notice", "Мой VPN")
        self.project = await self.registry.save_bot_connection(project["id"], 42, 1, 123456, "OwnedTenantBot", "cipher:" + "x" * 100)
        self.pid = self.project["id"]
        self.store = PartnerEnrollmentStore(lambda: self.sql, entry_launch_notifications_enabled=True)
        self.order = await self.store.create_entry_order(self.pid, 42, str(uuid4()))
        self.oid = self.order["id"]
        self.outbox = EntryLaunchNotificationStore(lambda: self.sql, enabled=True)
        self.now = datetime.now(timezone.utc) + timedelta(minutes=1)

    async def paid(self):
        await self.store.save_invoice(self.pid, 42, self.oid, "entry-invoice", "https://pay.example.test/entry")
        return await self.store.record_verified_payment(self.pid, self.oid, "entry-invoice", 999000, "RUB")

    async def complete(self, store=None):
        return await (store or self.store).complete_launch(self.pid, self.oid, bot_id=123456,
            webhook_url="https://bots.example.test/partner-bots/" + self.pid + "/webhook", secret_fingerprint="a" * 64)

    async def count(self):
        return await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox WHERE kind='entry_launched'")

    async def test_no_notice_for_unpaid_or_paid_but_not_created_project(self):
        self.assertEqual(await self.outbox.enqueue_completed(), 0)
        self.assertIsNone(await self.outbox.claim(now=self.now))
        await self.paid()
        self.assertEqual(await self.outbox.enqueue_completed(), 0)
        self.assertEqual(await self.count(), 0)
        self.assertIsNone(await self.outbox.claim(now=self.now))

    async def test_complete_launch_enqueues_same_transaction_once_and_retry_preserves_one_event(self):
        await self.paid()
        await self.complete()
        self.assertEqual(await self.count(), 1)
        await self.complete()
        self.assertEqual(await self.count(), 1)
        self.assertEqual(await self.outbox.enqueue_completed(), 0)
        row = await self.outbox.claim(now=self.now)
        self.assertEqual((row["recipient_id"], row["owner_tg_id"], row["bot_username"]), (42, 42, "OwnedTenantBot"))
        self.assertIsNone(await self.outbox.claim(now=self.now))

    async def test_verified_renamed_bot_is_saved_before_notice_resolves_url(self):
        from cryptography.fernet import Fernet

        vault = TokenVault((Fernet.generate_key().decode(),))
        token = "123456:" + "x" * 32
        cipher = vault.encrypt(self.pid, 123456, token)
        await self.sql.execute("UPDATE wl_projects SET encrypted_token=$2 WHERE id=$1", self.pid, cipher)
        await self.paid()
        for invalid in ("", "bad/name", True):
            with self.subTest(invalid=invalid), self.assertRaises(InvalidEnrollment):
                await self.store.complete_launch(self.pid, self.oid, bot_id=123456,
                    webhook_url="https://bots.example.test/partner-bots/" + self.pid + "/webhook",
                    secret_fingerprint="a" * 64, bot_username=invalid)
        self.assertEqual(await self.count(), 0)
        telegram = type("Telegram", (), {"ready": True})()
        telegram.get_me = AsyncMock(return_value={"id": 123456, "is_bot": True, "username": "RenamedTenantBot"})
        target = "https://bots.example.test/partner-bots/" + self.pid + "/webhook"
        telegram.get_webhook_info = AsyncMock(side_effect=[{"url": ""}, {
            "url": target, "allowed_updates": ["message", "callback_query"],
        }])
        telegram.set_webhook = AsyncMock()
        launcher = PartnerLaunchService(self.store, vault, telegram,
            webhook_base_url="https://bots.example.test", webhook_secret_key=b"s" * 32, clients_enabled=True)
        self.assertEqual(await launcher.launch(self.pid, 42, self.oid), {"launch_state": "launched"})
        project = await self.registry.get_project(self.pid, 42)
        self.assertEqual(project["bot"], {"id": 123456, "username": "RenamedTenantBot"})
        saved = await self.store.get_entry_order(self.pid, 42, self.oid)
        self.assertEqual((saved["bot_id"], saved["price_kopecks"]), (123456, 999000))
        sender = type("Sender", (), {})()
        sender.verify = AsyncMock()
        sender.send = AsyncMock(return_value=NotificationResult("sent", 99))
        service = EntryLaunchNotificationService(self.outbox, sender, manager_bot_token=MANAGER,
                                                enabled=True, clock=lambda: self.now)
        self.assertEqual((await service.recover_once(limit=1))["sent"], 1)
        self.assertEqual(sender.send.await_args.kwargs["bot_url"], "https://t.me/RenamedTenantBot")

    async def test_event_failure_rolls_back_activation_then_same_paid_order_recovers(self):
        await self.paid()
        original = self.sql.execute
        async def fail_event(sql, *args):
            if "INSERT INTO wl_partner_outbox" in sql and "entry_launched" in sql:
                raise RuntimeError("injected outbox failure")
            return await original(sql, *args)
        self.sql.execute = fail_event
        with self.assertRaises(RuntimeError): await self.complete()
        self.sql.execute = original
        self.assertEqual((await self.registry.get_project(self.pid, 42))["status"], "draft")
        self.assertEqual((await self.store.get_entry_order(self.pid, 42, self.oid))["status"], "paid_pending_launch")
        self.assertEqual(await self.count(), 0)
        await self.complete()
        self.assertEqual(await self.count(), 1)

    async def test_worker_backfills_already_committed_launch_without_duplicate(self):
        await self.paid()
        await self.complete(PartnerEnrollmentStore(lambda: self.sql))
        self.assertEqual(await self.count(), 0)
        self.assertEqual(await self.outbox.enqueue_completed(), 1)
        self.assertEqual(await self.outbox.enqueue_completed(), 0)
        self.assertEqual(await self.count(), 1)

    async def test_lease_crash_before_send_retries_but_sending_crash_stays_uncertain(self):
        await self.paid(); await self.complete()
        first = await self.outbox.claim(now=self.now)
        later = self.now + timedelta(minutes=3)
        second = await self.outbox.claim(now=later)
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertFalse(await self.outbox.begin_send(first, now=later))
        self.assertTrue(await self.outbox.begin_send(second, now=later))
        self.assertIsNone(await self.outbox.claim(now=later + timedelta(minutes=3)))
        self.assertEqual(await self.sql.fetchval("SELECT delivery_state FROM wl_partner_outbox WHERE id=$1", second["id"]), "uncertain")
        self.assertIsNone(await self.outbox.claim(now=later + timedelta(days=1)))

    async def test_recovery_sends_exactly_once_and_survives_known_pre_send_failure(self):
        await self.paid(); await self.complete()
        sender = type("Sender", (), {})()
        sender.verify = AsyncMock(side_effect=RuntimeError("temporary verify failure"))
        sender.send = AsyncMock(return_value=NotificationResult("sent", 99))
        service = EntryLaunchNotificationService(self.outbox, sender, manager_bot_token=MANAGER,
            enabled=True, clock=lambda: self.now)
        self.assertEqual((await service.recover_once())["retry"], 1)
        sender.send.assert_not_awaited()
        sender.verify.side_effect = None
        service.clock = lambda: self.now + timedelta(seconds=61)
        self.assertEqual((await service.recover_once())["sent"], 1)
        self.assertEqual((await service.recover_once())["checked"], 0)
        sender.send.assert_awaited_once()
        self.assertEqual(await self.count(), 1)
        self.assertEqual(await self.sql.fetchval("SELECT telegram_message_id FROM wl_partner_outbox WHERE kind='entry_launched'"), 99)

    async def test_inactive_or_wrong_project_bot_never_claims_created_notice(self):
        await self.paid(); await self.complete()
        await self.sql.execute("UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid)
        self.assertIsNone(await self.outbox.claim(now=self.now))
        self.assertEqual(await self.sql.fetchval("SELECT delivery_state FROM wl_partner_outbox WHERE kind='entry_launched'"), "pending")

    async def test_migration_rerun_preserves_prices_paid_state_and_delivery_rows(self):
        await self.paid(); await self.complete()
        before = await self.store.get_entry_order(self.pid, 42, self.oid)
        await self.sql.query((ROOT / "entry_launch_notifications.sql").read_text(), exec=True)
        self.assertEqual(await self.store.get_entry_order(self.pid, 42, self.oid), before)
        self.assertEqual(await self.count(), 1)

    async def test_generic_paid_notices_cannot_steal_created_notice_without_url_button(self):
        await self.paid(); await self.complete()
        generic = PartnerNotificationStore(lambda: self.sql, enabled=True)
        paid = await generic.claim("partner", now=self.now)
        self.assertIsNotNone(paid)
        self.assertEqual(await self.sql.fetchval("SELECT kind FROM wl_partner_outbox WHERE id=$1", paid["id"]), "entry_paid")
        self.assertIsNone(await generic.claim("partner", now=self.now))
        created = await self.outbox.claim(now=self.now)
        self.assertIsNotNone(created)
        self.assertEqual(await self.sql.fetchval("SELECT kind FROM wl_partner_outbox WHERE id=$1", created["id"]), "entry_launched")


if __name__ == "__main__": unittest.main()
