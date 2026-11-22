"""Offline central-manager finance notifications; real SQL via optional PGlite."""

from datetime import timedelta
import os
import unittest

from services.partner_finance_notifications import (
    FinanceNotificationService,
    FinanceNotificationStore,
    finance_notification_text,
)
from services.partner_notifications import NotificationResult
import test_partner_finance_postgres as fixtures


TOKEN = "123456:" + "x" * 35


class Sender:
    def __init__(self, result=None):
        self.result = result or NotificationResult("sent", message_id=77)
        self.calls = []
        self.verify_error = False

    async def verify(self, token, bot_id):
        self.calls.append(("verify", bot_id))
        if self.verify_error:
            raise RuntimeError("private transport detail")

    async def send(self, token, **kwargs):
        self.calls.append(("send", kwargs))
        return self.result


class FinanceNotificationPureTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_never_reads_database_or_sends(self):
        def forbidden():
            raise AssertionError("disabled")

        store = FinanceNotificationStore(forbidden, enabled=False)
        sender = Sender()
        service = FinanceNotificationService(store, sender, manager_bot_token=TOKEN)
        self.assertFalse((await service.recover_once())["enabled"])
        self.assertIsNone(await store.claim())
        self.assertEqual(await store.health(), {})
        self.assertEqual(sender.calls, [])

    async def test_factual_plain_text_no_requisites_or_promise_of_automatic_transfer(
        self,
    ):
        row = {
            "kind": "withdrawal_requested",
            "amount_kopecks": 150000,
            "number": 123,
            "withdrawal_status": "pending",
            "phone": "+79991234567",
            "bank": "Секретный банк",
            "address": "T" + "x" * 33,
        }
        text = finance_notification_text(row)
        self.assertIn("P-123", text)
        self.assertIn("1 500 ₽", text)
        self.assertIn("ожидает проверки", text)
        self.assertNotIn("7999", text)
        self.assertNotIn("Секретный", text)
        self.assertNotIn("x" * 20, text)
        row["kind"] = "withdrawal_paid"
        self.assertIn("подтверждена оператором", finance_notification_text(row))
        row["kind"] = "withdrawal_rejected"
        self.assertIn("Резерв освобождён", finance_notification_text(row))
        row["kind"] = "reversal_confirmed"
        self.assertIn("сторно", finance_notification_text(row))


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"),
    "Optional isolated PostgreSQL/WASM module is not configured",
)
class FinanceNotificationPostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FinancePostgresTests.asyncSetUp
    restarted = fixtures.FinancePostgresTests.restarted
    request = fixtures.FinancePostgresTests.request
    earn = fixtures.FinancePostgresTests.earn
    reverse = fixtures.FinancePostgresTests.reverse

    def notification_store(self, **kwargs):
        return FinanceNotificationStore(
            lambda: self.sql,
            admin_recipient_id=kwargs.pop("admin_recipient_id", 99),
            enabled=True,
            **kwargs
        )

    def service(self, store=None, sender=None):
        return FinanceNotificationService(
            store or self.notification_store(),
            sender or Sender(),
            manager_bot_token=TOKEN,
            enabled=True,
            clock=lambda: self.now,
        )

    async def test_one_bounded_delivery_current_admin_and_private_data_absent(self):
        await self.request()
        store, sender = self.notification_store(), Sender()
        result = await self.service(store, sender).recover_once(limit=1)
        self.assertEqual((result["checked"], result["sent"]), (1, 1))
        sent = sender.calls[-1][1]
        self.assertEqual(sent["chat_id"], 99)
        self.assertNotIn("7999", sent["text"])
        self.assertNotIn("bank", sent["text"])
        self.assertEqual(await store.health(), {"delivered": 1})
        self.assertEqual(
            (await self.service(store, sender).recover_once(limit=1))["checked"], 0
        )
        self.assertEqual(len([c for c in sender.calls if c[0] == "send"]), 1)

    async def test_crash_after_leasing_safe_retry_after_sending_uncertain(self):
        await self.request()
        store = self.notification_store()
        lease = await store.claim(now=self.now)
        later = self.now + timedelta(minutes=3)
        again = await store.claim(now=later)
        self.assertIsNotNone(again)
        self.assertNotEqual(lease["lease_token"], again["lease_token"])
        self.assertFalse(await store.begin_send(lease, now=later))
        self.assertTrue(await store.begin_send(again, now=later))
        self.assertIsNone(await store.claim(now=later + timedelta(minutes=3)))
        self.assertEqual(await store.health(), {"uncertain": 1})
        self.assertFalse(
            await store.finish(again, NotificationResult("retry"), now=later)
        )
        # Late verified success of the same attempt may resolve uncertainty.
        self.assertTrue(
            await store.finish(
                again, NotificationResult("sent", 88), now=later + timedelta(minutes=4)
            )
        )
        self.assertEqual(await store.health(), {"delivered": 1})

    async def test_lost_send_result_never_automatically_retried(self):
        await self.request()
        sender = Sender(NotificationResult("uncertain"))
        result = await self.service(sender=sender).recover_once(limit=5)
        self.assertEqual((result["checked"], result["uncertain"]), (1, 1))
        self.assertEqual(len([call for call in sender.calls if call[0] == "send"]), 1)
        self.assertEqual(await self.notification_store().health(), {"uncertain": 1})

    async def test_explicit_nonsent_retry_respects_delay_and_blocked_does_not_retry(
        self,
    ):
        await self.request()
        store = self.notification_store()
        sender = Sender(NotificationResult("retry", retry_after=120))
        result = await self.service(store, sender).recover_once(limit=2)
        self.assertEqual(result["retry"], 1)
        self.assertIsNone(await store.claim(now=self.now + timedelta(seconds=119)))
        lease = await store.claim(now=self.now + timedelta(seconds=121))
        self.assertIsNotNone(lease)
        self.assertTrue(
            await store.finish(lease, NotificationResult("blocked"), now=self.now)
        )
        self.assertIsNone(await store.claim(now=self.now + timedelta(days=1)))

    async def test_verifier_failure_is_safe_before_send_intent(self):
        await self.request()
        sender = Sender()
        sender.verify_error = True
        result = await self.service(sender=sender).recover_once(limit=1)
        self.assertEqual(result["retry"], 1)
        self.assertFalse(any(call[0] == "send" for call in sender.calls))
        self.assertEqual(await self.notification_store().health(), {"pending": 1})

    async def test_db_failure_after_accepted_message_retains_intent_then_uncertain(
        self,
    ):
        await self.request()
        store = self.notification_store()
        original = store.finish

        async def fail(*_args, **_kwargs):
            raise RuntimeError("injected storage failure")

        store.finish = fail
        sender = Sender()
        result = await self.service(store, sender).recover_once(limit=3)
        self.assertEqual((result["errors"], result["sent"]), (1, 0))
        self.assertEqual(await store.health(), {"sending": 1})
        store.finish = original
        self.assertIsNone(await store.claim(now=self.now + timedelta(minutes=3)))
        self.assertEqual(await store.health(), {"uncertain": 1})
        self.assertEqual(len([call for call in sender.calls if call[0] == "send"]), 1)

    async def test_retired_admin_not_reused_and_owner_destination_bound(self):
        row = await self.request()
        await self.store.reject_withdrawal(self.pid, row["id"], 99, "Уточните банк")
        await self.sql.execute(
            "UPDATE wl_finance_outbox SET recipient_tg_id=500 WHERE kind='withdrawal_rejected'"
        )
        store = self.notification_store(admin_recipient_id=98)
        self.assertIsNone(await store.claim(now=self.now))
        self.assertEqual(await store.health(), {"blocked": 2})

    async def test_paid_notification_uses_owner_and_reversed_amount_is_partner_share(
        self,
    ):
        row = await self.request()
        await self.store.claim_withdrawal(self.pid, row["id"], 99)
        await self.store.confirm_withdrawal_paid(
            self.pid, row["id"], 99, 150000, "bank:notify-paid-1", now=self.now
        )
        await self.reverse(100)
        sender = Sender()
        result = await self.service(sender=sender).recover_once(limit=5)
        self.assertEqual(result["sent"], 3)
        messages = [call[1] for call in sender.calls if call[0] == "send"]
        self.assertEqual([m["chat_id"] for m in messages], [99, 42, 99])
        self.assertIn("0,70 ₽", messages[-1]["text"])

    async def test_wrong_source_never_notified_as_real_paid_event(self):
        row = await self.request()
        await self.sql.execute(
            "UPDATE wl_finance_outbox SET kind='withdrawal_paid',recipient_tg_id=42"
        )
        sender = Sender()
        result = await self.service(sender=sender).recover_once(limit=1)
        self.assertEqual(result["checked"], 0)
        self.assertFalse(sender.calls)
