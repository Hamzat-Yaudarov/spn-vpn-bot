import copy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import database as db
from config import GB_BYTES, TARIFFS
from services import payment_activation as activation, payment_processing as processing
from scripts.recover_paid_subscriptions import verified_paid, parse_args, require_stopped, save_backup


def payment(**kwargs):
    return dict({"id": 1, "invoice_id": "invoice-1", "tg_id": 123, "tariff_code": "bypass_1m",
                 "provider": "yookassa", "amount": Decimal("300"), "status": "pending",
                 "payment_kind": "subscription", "payment_target": "new", "subscription_id": None,
                 "target_slot_number": 1, "created_at": datetime(2026, 8, 31)}, **kwargs)


def subscription(**kwargs):
    return dict({"id": 10, "tg_id": 123, "slot_number": 1, "type_index": 1, "plan_kind": "bypass",
                 "generation": "v2", "is_visible": True, "is_renewable": True, "is_active": False,
                 "remnawave_uuid": None, "remnawave_username": None, "subscription_until": None}, **kwargs)


class TargetTests(unittest.TestCase):
    def test_reuses_oldest_empty_placeholder_even_if_ten_slots_taken(self):
        subs = [subscription(id=i, slot_number=i) for i in range(1, 11)]
        target, index = activation.choose_target(payment(), subs, set(range(1, 11)), "bypass")
        self.assertEqual((target["id"], index), (1, 1))

    def test_preserves_linked_target_on_retry(self):
        sub = subscription(remnawave_uuid="uuid", subscription_until=datetime(2030, 1, 1))
        chosen, _ = activation.choose_target(payment(subscription_id=10), [sub], set(), "bypass")
        self.assertEqual(chosen, sub)

    def test_new_payment_cannot_steal_existing_key_or_other_invoices_slot(self):
        for matches in ([subscription(remnawave_uuid="uuid")],
                        [subscription(id=10), subscription(id=11)]):
            with self.assertRaises(activation.ActivationError):
                activation.choose_target(payment(), matches, {11}, "bypass")

    def test_hidden_deleted_wrong_kind_and_unlinked_renewal_are_rejected(self):
        for subs in ([], [subscription(is_visible=False)], [subscription(is_renewable=False)],
                     [subscription(plan_kind="regular")]):
            with self.assertRaises(activation.ActivationError):
                activation.choose_target(payment(subscription_id=10), subs, set(), "bypass")
        with self.assertRaises(activation.ActivationError):
            activation.choose_target(payment(payment_target="renew"), [], set(), "bypass")

    def test_business_limit_stays_five_per_type(self):
        subs = [subscription(id=i, type_index=i) for i in range(1, 6)]
        with self.assertRaises(activation.ActivationError):
            activation.choose_target(payment(target_slot_number=None), subs, set(), "bypass")
        chosen, index = activation.choose_target(payment(target_slot_number=None), subs, set(), "regular")
        self.assertIsNone(chosen)
        self.assertEqual(index, 1)
        for invalid in (0, 6, True, "1"):
            with self.assertRaises(activation.ActivationError):
                activation.choose_target(payment(target_slot_number=invalid), [], set(), "bypass")


class Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.snapshot = copy.deepcopy((self.conn.payment, self.conn.subs, self.conn.reserved))

    async def __aexit__(self, exc_type, *args):
        if exc_type:
            self.conn.payment, self.conn.subs, self.conn.reserved = self.snapshot
        return False


class Connection:
    """Stateful failure-injection fixture, not a replacement for PostgreSQL QA."""
    def __init__(self, pay=None, subs=None):
        self.payment = pay or payment()
        self.subs = subs or []
        self.reserved = None
        self.queries = []
        self.fail_paid = False
        self.reusable = {s["id"] for s in self.subs if not s.get("remnawave_uuid")}

    def transaction(self):
        return Transaction(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        if "SELECT s.id" in sql:
            # Match the important production predicate: no row linked to ANY payment.
            return [{"id": i} for i in self.reusable if i != self.payment.get("subscription_id")]
        if "SELECT * FROM subscriptions" in sql:
            return copy.deepcopy(self.subs)
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        if "SELECT * FROM payments" in sql:
            return copy.deepcopy(self.payment)
        if "SELECT * FROM payment_subscription_activations" in sql:
            return copy.deepcopy(self.reserved)
        if "INSERT INTO subscriptions" in sql:
            sub = subscription(id=max((s["id"] for s in self.subs), default=0) + 1,
                               tg_id=args[0], slot_number=args[1], plan_kind=args[2], type_index=args[3])
            self.subs.append(sub)
            return copy.deepcopy(sub)
        if "UPDATE subscriptions SET remnawave_uuid" in sql:
            sub = next((s for s in self.subs if s["id"] == args[15] and s["is_visible"] and s["is_renewable"]), None)
            if sub is None:
                return None
            sub.update(remnawave_uuid=args[0], remnawave_username=args[1], subscription_until=args[2],
                       is_active=True, current_period_limit_bytes=args[8])
            return {"id": sub["id"]}
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.queries.append(sql)
        if "INSERT INTO payment_subscription_activations" in sql:
            self.reserved = {"invoice_id": args[0], "subscription_id": args[1], "expires_at": args[2]}
        elif "UPDATE payments SET subscription_id" in sql:
            self.payment.update(subscription_id=args[1], target_slot_number=args[2])
        elif "UPDATE subscriptions SET is_active = FALSE" in sql:
            for s in self.subs:
                if s["id"] in args[0]:
                    s.update(is_active=False, is_visible=False, is_renewable=False)
        elif "UPDATE payments SET status = 'paid'" in sql:
            if self.fail_paid:
                raise RuntimeError("simulated failed commit")
            self.payment["status"] = "paid"
        else:
            raise AssertionError(sql)


class ReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = Connection()
        self.pool = SimpleNamespace(acquire=lambda: self.conn)
        self.enterContext(patch.object(db, "get_pool", AsyncMock(return_value=self.pool)))

    async def test_failed_remote_retry_keeps_one_slot_and_one_fixed_deadline(self):
        first = await activation.reserve(123, "invoice-1", TARIFFS["bypass_1m"])
        second = await activation.reserve(123, "invoice-1", TARIFFS["bypass_1m"])
        self.assertEqual(first, second)
        self.assertEqual(len(self.conn.subs), 1)
        self.assertEqual(self.conn.payment["subscription_id"], first["id"])
        self.assertEqual(self.conn.payment["status"], "pending")

    async def test_existing_empty_duplicates_are_archived_without_deletion(self):
        self.conn.subs = [subscription(id=i, slot_number=i) for i in range(1, 11)]
        self.conn.reusable = set(range(1, 11))
        chosen = await activation.reserve(123, "invoice-1", TARIFFS["bypass_1m"])
        self.assertEqual(chosen["id"], 1)
        self.assertEqual(len(self.conn.subs), 10)
        self.assertEqual(sum(s["is_visible"] for s in self.conn.subs), 1)
        self.assertFalse(any("DELETE " in sql for sql in self.conn.queries))

    async def test_renewal_keeps_fixed_deadline_across_retry(self):
        old_until = datetime.utcnow() + timedelta(days=5)
        self.conn.subs = [subscription(subscription_until=old_until, remnawave_uuid="uuid")]
        self.conn.payment.update(payment_target="renew", subscription_id=10)
        first = await activation.reserve(123, "invoice-1", TARIFFS["bypass_1m"])
        second = await activation.reserve(123, "invoice-1", TARIFFS["bypass_1m"])
        self.assertEqual(first["payment_expires_at"], old_until + timedelta(days=30))
        self.assertEqual(second, first)

    async def test_unrelated_history_does_not_exhaust_physical_slots(self):
        self.conn.subs = [subscription(id=i, slot_number=i, is_visible=False) for i in range(1, 11)]
        self.assertEqual((await activation.reserve(123, "invoice-1", TARIFFS["bypass_1m"]))["slot_number"], 11)

    async def test_wrong_owner_refund_or_canceled_payment_never_reserves(self):
        for changes in ({"tg_id": 999}, {"refund_requested_at": datetime.utcnow()}, {"status": "canceled"}):
            self.conn.payment = payment(**changes)
            with self.assertRaises(activation.ActivationError):
                await activation.reserve(123, "invoice-1", TARIFFS["bypass_1m"])
            self.assertEqual(self.conn.subs, [])

    async def test_local_commit_failure_rolls_back_subscription_and_preserves_pending(self):
        sub = await activation.reserve(123, "invoice-1", TARIFFS["bypass_1m"])
        traffic = processing.build_traffic_period_state(sub, "bypass")
        self.conn.fail_paid = True
        with self.assertRaises(RuntimeError):
            await activation.complete(123, "invoice-1", sub, "uuid", "tg_123_bypass_1", None, traffic, 3, 30)
        self.assertIsNone(self.conn.subs[0]["subscription_until"])
        self.assertEqual(self.conn.payment["status"], "pending")
        self.conn.fail_paid = False
        await activation.complete(123, "invoice-1", sub, "uuid", "tg_123_bypass_1", None, traffic, 3, 30)
        self.assertEqual(self.conn.payment["status"], "paid")
        self.assertEqual(self.conn.subs[0]["subscription_until"], sub["payment_expires_at"])


class PaymentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pay = payment()
        self.sub = subscription(payment_expires_at=datetime.utcnow() + timedelta(days=30))
        self.bot = AsyncMock()
        for name in ("release_lock",):
            self.enterContext(patch.object(activation, name, AsyncMock()))
        self.enterContext(patch.object(activation, "acquire_lock", AsyncMock(return_value=object())))
        self.reserve = self.enterContext(patch.object(activation, "reserve", AsyncMock(return_value=self.sub)))
        self.complete = self.enterContext(patch.object(activation, "complete", AsyncMock()))
        self.enterContext(patch.object(db, "acquire_user_lock", AsyncMock(return_value=True)))
        self.enterContext(patch.object(db, "release_user_lock", AsyncMock()))
        self.enterContext(patch.object(db, "get_payment_by_invoice", AsyncMock(return_value=self.pay)))
        self.enterContext(patch.object(db, "get_active_device_addon_count", AsyncMock(return_value=0)))
        self.enterContext(patch.object(db, "get_referrer", AsyncMock(return_value=None)))
        self.enterContext(patch.object(db, "db_execute", AsyncMock(return_value=None)))
        self.enterContext(patch.object(db, "sync_primary_subscription_to_user", AsyncMock()))
        self.enterContext(patch.object(processing, "_cancel_reactivation_safely", AsyncMock()))
        self.remote = self.enterContext(patch.object(processing, "remnawave_get_or_create_user", AsyncMock(return_value=("uuid", "tg_123_bypass_1"))))
        self.expiry = self.enterContext(patch.object(processing, "remnawave_set_subscription_expiry", AsyncMock(return_value=True)))
        self.url = self.enterContext(patch.object(processing, "remnawave_get_subscription_url", AsyncMock(return_value="https://sub.example/key")))
        self.reset = self.enterContext(patch.object(processing, "remnawave_reset_user_traffic", AsyncMock(return_value=True)))

    async def test_success_uses_fixed_expiry_commits_and_sends_key(self):
        self.assertTrue(await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m"))
        self.assertFalse(self.remote.await_args.kwargs["extend_if_exists"])
        self.assertEqual(self.expiry.await_args.args[2], self.sub["payment_expires_at"])
        self.complete.assert_awaited_once()
        self.assertIn("https://sub.example/key", self.bot.send_message.await_args.args[1])

    async def test_failed_url_or_expiry_leaves_pending_and_sends_no_success(self):
        for component, value in ((self.url, None), (self.expiry, False), (self.remote, (None, None))):
            original = component.return_value
            component.return_value = value
            self.assertFalse(await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m"))
            self.complete.assert_not_awaited()
            self.bot.send_message.assert_not_awaited()
            component.return_value = original

    async def test_retry_does_not_increment_remote_expiry_and_paid_does_not_reactivate(self):
        self.url.side_effect = [None, "https://sub.example/key"]
        self.assertFalse(await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m"))
        self.assertTrue(await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m"))
        self.assertEqual(self.expiry.await_args_list[0].args[2], self.expiry.await_args_list[1].args[2])
        self.pay["status"] = "paid"
        self.assertTrue(await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m"))
        self.complete.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_notification_failure_does_not_undo_purchase(self):
        self.bot.send_message.side_effect = RuntimeError("blocked")
        self.assertTrue(await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m"))
        self.complete.assert_awaited_once()

    async def test_database_lock_conflict_prevents_any_activation(self):
        with patch.object(activation, "acquire_lock", AsyncMock(return_value=None)):
            self.assertFalse(await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m"))
        self.reserve.assert_not_awaited()
        self.remote.assert_not_awaited()

    async def test_active_renewal_preserves_extra_traffic(self):
        self.pay["payment_target"] = "renew"
        self.sub.update(subscription_until=datetime.utcnow() + timedelta(days=5), remnawave_uuid="uuid",
                        base_traffic_bytes=200 * GB_BYTES, current_paid_traffic_bytes=10 * GB_BYTES,
                        current_period_limit_bytes=210 * GB_BYTES)
        self.assertTrue(await processing.process_paid_payment(self.bot, 123, "invoice-1", "bypass_1m"))
        self.assertEqual(self.remote.await_args.kwargs["traffic_limit_bytes"], 210 * GB_BYTES)
        self.reset.assert_not_awaited()


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_never_deletes_pending_payments(self):
        with patch.object(db, "db_execute", AsyncMock()) as execute:
            await db.delete_expired_payments(1)
        execute.assert_not_awaited()

    async def test_stale_invoice_is_not_deleted_when_new_link_is_requested(self):
        with (patch.object(db, "db_execute", AsyncMock(return_value={"id": 1, "created_at": datetime(2020, 1, 1)})),
              patch.object(db, "delete_payment", AsyncMock()) as delete):
            self.assertIsNone(await db.get_active_payment_for_user_and_tariff(123, "bypass_1m", "yookassa"))
        delete.assert_not_awaited()


class RecoverySafetyTests(unittest.TestCase):
    def remote(self, **kwargs):
        return dict({"id": "invoice-1", "status": "succeeded", "amount": {"value": "300.00", "currency": "RUB"},
                     "metadata": {"tg_id": "123", "tariff_code": "bypass_1m"}}, **kwargs)

    def test_only_matching_paid_provider_records_are_accepted(self):
        self.assertTrue(verified_paid(payment(), self.remote()))
        for bad in (None, {}, self.remote(id="other"), self.remote(status="pending"),
                    self.remote(amount={"value": "300", "currency": "USD"}),
                    self.remote(amount={"value": "299", "currency": "RUB"}),
                    self.remote(metadata={}), self.remote(metadata={"tg_id": "999", "tariff_code": "bypass_1m"})):
            self.assertFalse(verified_paid(payment(), bad))

    def test_apply_requires_explicit_selection_and_stopped_service(self):
        with self.assertRaises(SystemExit):
            parse_args(["--apply"])
        self.assertFalse(parse_args([]).apply)
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="active\n")):
            with self.assertRaises(RuntimeError):
                require_stopped("spn-bot")

    def test_backup_is_private_and_cannot_overwrite_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_backup(Path(tmp), payment(), [subscription()])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                save_backup(Path(tmp), payment(), [])


if __name__ == "__main__":
    unittest.main()
