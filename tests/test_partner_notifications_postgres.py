"""Actual isolated outbox lease/state SQL, not a claim of native PG races."""

from datetime import timedelta
import os
import unittest
from uuid import uuid4

from cryptography.fernet import Fernet
import test_partner_entitlements_postgres as fixtures
from services.partner_security import TokenVault
from services.partner_enrollment_store import PartnerEnrollmentStore
from services.partner_notifications import (
    NotificationResult,
    PartnerNotificationService,
    PartnerNotificationStore,
)

ROOT, NOW = fixtures.ROOT, fixtures.NOW
TOKEN = "123456:" + "x" * 32
MANAGER = "987654:" + "y" * 32


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class NotificationPostgresTests(unittest.IsolatedAsyncioTestCase):
    subscription = fixtures.EntitlementPostgresTests.subscription
    pay = fixtures.EntitlementPostgresTests.pay
    complete = fixtures.EntitlementPostgresTests.complete

    async def asyncSetUp(self):
        await fixtures.EntitlementPostgresTests.asyncSetUp(self)
        for name in ("enrollment.sql", "notifications.sql"):
            await self.sql.query(
                (ROOT / "ops/partner-platform" / name).read_text(), exec=True
            )
        self.outbox = PartnerNotificationStore(lambda: self.sql, enabled=True)
        self.vault = TokenVault([Fernet.generate_key().decode()])
        await self.sql.execute(
            "UPDATE wl_projects SET encrypted_token=$2 WHERE id=$1",
            self.pid,
            self.vault.encrypt(self.pid, 123456, TOKEN),
        )

    async def due(self):
        await self.subscription()
        await self.sql.execute("UPDATE wl_customer_outbox SET next_attempt_at=$1", NOW)

    async def test_claim_once_ack_with_same_lease_and_schema_reapply(self):
        await self.due()
        await self.sql.query(
            (ROOT / "ops/partner-platform/notifications.sql").read_text(), exec=True
        )
        row = await self.outbox.claim("customer", now=NOW)
        self.assertEqual((row["recipient_id"], row["bot_id"]), (100, 123456))
        self.assertEqual(
            self.vault.decrypt(
                row["project_id"], row["bot_id"], row["encrypted_token"]
            ),
            TOKEN,
        )
        self.assertIsNone(await self.outbox.claim("customer", now=NOW))
        self.assertTrue(await self.outbox.begin_send(row, now=NOW))
        wrong = {**row, "lease_token": str(uuid4())}
        self.assertFalse(
            await self.outbox.finish(wrong, NotificationResult("sent", 99), now=NOW)
        )
        self.assertTrue(
            await self.outbox.finish(row, NotificationResult("sent", 99), now=NOW)
        )
        self.assertIsNone(
            await self.outbox.claim("customer", now=NOW + timedelta(days=1))
        )
        state = await self.sql.fetchrow(
            "SELECT * FROM wl_customer_outbox WHERE id=$1", row["id"]
        )
        self.assertEqual(
            (state["status"], state["delivery_state"], state["telegram_message_id"]),
            ("delivered", "delivered", 99),
        )

    async def test_restart_before_send_reclaims_but_old_worker_cannot_send(self):
        await self.due()
        first = await self.outbox.claim("customer", now=NOW)
        later = NOW + timedelta(minutes=3)
        second = await self.outbox.claim("customer", now=later)
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertFalse(await self.outbox.begin_send(first, now=later))
        self.assertTrue(await self.outbox.begin_send(second, now=later))

    async def test_restart_after_send_intent_is_uncertain_and_never_auto_reclaimed(
        self,
    ):
        await self.due()
        row = await self.outbox.claim("customer", now=NOW)
        await self.outbox.begin_send(row, now=NOW)
        later = NOW + timedelta(minutes=3)
        self.assertIsNone(await self.outbox.claim("customer", now=later))
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT delivery_state FROM wl_customer_outbox WHERE id=$1", row["id"]
            ),
            "uncertain",
        )
        self.assertIsNone(
            await self.outbox.claim("customer", now=later + timedelta(days=2))
        )
        # A late verified response from the same original attempt can resolve it.
        self.assertTrue(
            await self.outbox.finish(row, NotificationResult("sent", 99), now=later)
        )

    async def test_known_rate_limit_retries_after_delay_and_block_is_bot_scoped(self):
        await self.due()
        row = await self.outbox.claim("customer", now=NOW)
        await self.outbox.begin_send(row, now=NOW)
        await self.outbox.finish(
            row, NotificationResult("retry", retry_after=90), now=NOW
        )
        self.assertIsNone(
            await self.outbox.claim("customer", now=NOW + timedelta(seconds=89))
        )
        retry = await self.outbox.claim("customer", now=NOW + timedelta(seconds=91))
        await self.outbox.begin_send(retry, now=NOW + timedelta(seconds=91))
        await self.outbox.finish(
            retry, NotificationResult("blocked"), now=NOW + timedelta(seconds=91)
        )
        self.assertIsNone(
            await self.outbox.claim("customer", now=NOW + timedelta(days=1))
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_customers WHERE tg_id=100"
            ),
            1,
        )

    async def test_end_to_end_uses_partner_bot_for_client_manager_bot_for_owner(self):
        await self.due()
        p = await self.registry.create_project(
            555, "entry-notify-test", "entry-notify", "Другой VPN"
        )
        await self.registry.save_bot_connection(
            p["id"], 555, 1, 111111, "NotifyPartnerBot", "cipher:" + "z" * 100
        )
        enrollment = PartnerEnrollmentStore(lambda: self.sql)
        order = await enrollment.create_entry_order(p["id"], 555, str(uuid4()))
        await enrollment.save_invoice(
            p["id"],
            555,
            order["id"],
            "entry-notify-payment",
            "https://pay.example.test/entry",
        )
        await enrollment.record_verified_payment(
            p["id"], order["id"], "entry-notify-payment", 999000, "RUB", now=NOW
        )
        await self.sql.execute("UPDATE wl_partner_outbox SET next_attempt_at=$1", NOW)
        sent = []

        class Sender:
            async def verify(self, token, bot_id):
                if int(token.split(":", 1)[0]) != bot_id:
                    raise ValueError()

            async def send(self, token, **kwargs):
                sent.append((token, kwargs))
                return NotificationResult("sent", len(sent))

        service = PartnerNotificationService(
            self.outbox,
            Sender(),
            vault=self.vault,
            manager_bot_token=MANAGER,
            enabled=True,
            clock=lambda: NOW,
        )
        result = await service.recover_once(limit=4)
        self.assertEqual((result["sent"], result["errors"]), (2, 0))
        self.assertEqual(
            [(token, kw["chat_id"]) for token, kw in sent],
            [(TOKEN, 100), (MANAGER, 555)],
        )
        self.assertNotIn(TOKEN, str(result))
        self.assertNotIn(MANAGER, str(result))
        self.assertEqual((await service.recover_once(limit=4))["sent"], 0)

    async def test_disabled_store_inert_and_unpaid_or_suspended_project_never_claimed(
        self,
    ):
        await self.due()
        disabled = PartnerNotificationStore(lambda: self.sql)
        self.assertIsNone(await disabled.claim("customer", now=NOW))
        self.assertFalse(await disabled.begin_send({}, now=NOW))
        await self.sql.execute(
            "UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid
        )
        self.assertIsNone(await self.outbox.claim("customer", now=NOW))
