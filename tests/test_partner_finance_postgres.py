"""Optional isolated PostgreSQL/WASM accounting; no live settings or database.

The bridge tests real SQL/transactions but is one connection, not a substitute
for independent native PostgreSQL race/deadlock tests before production.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

from cryptography.fernet import Fernet

from test_partner_store_postgres import RegistryBridge
from services.partner_store import PartnerStore
from services.partner_commerce_store import PartnerCommerceStore
from services.partner_finance import (
    FinanceConflict,
    FinanceForbidden,
    FinanceNotFound,
    FinanceUnavailable,
    FinanceVault,
    InvalidFinance,
    PartnerFinanceStore,
)


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"),
    "Optional isolated PostgreSQL/WASM module is not configured",
)
class FinancePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        root = Path(__file__).resolve().parents[1]
        await self.sql.query(
            (root / "ops/partner-platform/commerce.sql").read_text(), exec=True
        )
        self.schema = (root / "ops/partner-platform/finance.sql").read_text()
        await self.sql.query(self.schema, exec=True)
        self.registry = PartnerStore(lambda: self.sql)
        project = await self.registry.create_project(
            42, "finance-create-123", "finance-test", "Бренд", 1000000
        )
        self.pid = project["id"]
        await self.registry.save_bot_connection(
            self.pid, 42, 1, 123456, "FinanceBot", "cipher:" + "x" * 100
        )
        await self.sql.execute(
            "UPDATE wl_projects SET status='active' WHERE id=$1", self.pid
        )
        self.now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        self.key = Fernet.generate_key().decode()
        self.vault = FinanceVault([self.key])
        self.store = self.restarted()
        self.commerce = PartnerCommerceStore(lambda: self.sql)
        self.requisites = {"bank": "Банк", "phone": "+79991234567"}
        self.first = await self.earn()

    def restarted(self, **changes):
        options = {
            "vault": self.vault,
            "operator_ids": (99, 98),
            "admin_recipient_id": 99,
            "enabled": True,
        } | changes
        return PartnerFinanceStore(lambda: self.sql, **options)

    async def earn(self, tg=100):
        await self.commerce.register_customer(self.pid, tg)
        subscriptions = await self.commerce.list_subscriptions(self.pid, tg)
        subscription_id = subscriptions[0]["id"] if subscriptions else None
        order = await self.commerce.create_order(
            self.pid, tg, str(uuid4()), 1000000, subscription_id=subscription_id
        )
        await self.commerce.save_invoice(
            self.pid, tg, order["id"], "p-" + order["id"], ""
        )
        await self.commerce.record_verified_payment(
            self.pid, order["id"], "p-" + order["id"], 1000000, "RUB", now=self.now
        )
        await self.commerce.complete_activation(
            self.pid,
            order["id"],
            remote_user_id="remote-" + str(tg),
            subscription_url="https://sub.test/private",
        )
        return order

    async def request(self, amount=150000, *, key=None, method="sbp", requisites=None):
        return await self.store.request_withdrawal(
            self.pid,
            42,
            key or str(uuid4()),
            amount,
            method,
            requisites or self.requisites,
            now=self.now,
        )

    async def reverse(self, amount=1000000, *, order=None, key=None, event=None):
        return await self.store.confirm_reversal(
            self.pid,
            (order or self.first)["id"],
            99,
            key or str(uuid4()),
            amount,
            kind="chargeback",
            event_reference=event or "provider:" + str(uuid4()),
            reason="Подтверждено выпиской",
            now=self.now,
        )

    async def test_migration_reapply_and_summary_uses_actual_ledger(self):
        await self.sql.query(self.schema, exec=True)
        result = await self.store.summary(self.pid, 42)
        self.assertEqual(result["earned_kopecks"], 700000)
        self.assertEqual(result["available_kopecks"], 700000)
        self.assertEqual(result["reserved_kopecks"], 0)
        self.assertEqual(result["debt_kopecks"], 0)
        self.assertEqual(result["minimum_withdrawal_kopecks"], 150000)
        await self.commerce.create_order(
            self.pid,
            100,
            str(uuid4()),
            1000000,
            subscription_id=self.first["subscription_id"],
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["earned_kopecks"], 700000
        )

    async def test_subscription_renewal_and_addon_shares_included_but_entry_excluded(
        self,
    ):
        from test_partner_entitlements_postgres import quotes

        root = Path(__file__).resolve().parents[1] / "ops/partner-platform"
        for name in ("entitlements.sql", "enrollment.sql"):
            await self.sql.query((root / name).read_text(), exec=True)
        self.commerce = PartnerCommerceStore(
            lambda: self.sql, quotes, entitlements_enabled=True
        )
        for purpose, package, price in (
            ("traffic", "gb_10", 2400),
            ("devices", "1", 10000),
        ):
            order = await self.commerce.create_addon_order(
                self.pid,
                100,
                str(uuid4()),
                price,
                subscription_id=self.first["subscription_id"],
                purpose=purpose,
                package_id=package,
                now=self.now,
            )
            payment = "addon-" + order["id"]
            await self.commerce.save_invoice(self.pid, 100, order["id"], payment, "")
            await self.commerce.record_verified_payment(
                self.pid, order["id"], payment, price, "RUB", now=self.now
            )
            await self.commerce.complete_activation(
                self.pid,
                order["id"],
                remote_user_id="remote-100",
                subscription_url="https://sub.test/private",
            )
        await self.earn()
        await self.sql.execute(
            """INSERT INTO wl_entry_orders(id,project_id,owner_tg_id,request_key,bot_id,status,
            provider_payment_id,confirmation_url,paid_at) VALUES($1,$2,42,$3,123456,'paid_pending_launch','entry-paid-id','',$4)""",
            str(uuid4()),
            self.pid,
            str(uuid4()),
            self.now,
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["earned_kopecks"], 1408680
        )

    async def test_request_atomic_reservation_masked_and_restart_replay(self):
        key = str(uuid4())
        row = await self.request(key=key)
        self.assertEqual(row["status"], "pending")
        self.assertTrue(row["number"].startswith("P-"))
        self.assertNotIn("7999123", str(row))
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 550000
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["reserved_kopecks"], 150000
        )
        self.store = self.restarted()
        self.assertEqual(row, await self.request(key=key))
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_withdrawals"), 1
        )
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_finance_outbox"), 1
        )
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_finance_audit"), 1
        )
        encrypted = await self.sql.fetchval(
            "SELECT encrypted_requisites FROM wl_withdrawals"
        )
        self.assertNotIn("7999123", encrypted)
        with self.assertRaises(FinanceConflict):
            await self.request(150001, key=key)
        with self.assertRaises(FinanceConflict):
            await self.request(
                key=key, requisites={"bank": "Другой", "phone": "+79991234567"}
            )

    async def test_key_rotation_keeps_idempotency(self):
        key = str(uuid4())
        original = await self.request(key=key)
        self.vault = FinanceVault([Fernet.generate_key().decode(), self.key])
        self.store = self.restarted()
        self.assertEqual(original, await self.request(key=key))
        detail = await self.store.operator_details(
            self.pid, original["id"], 99, now=self.now
        )
        self.assertEqual(detail["requisites"], self.requisites)

    async def test_tenant_owner_and_operator_isolation(self):
        row = await self.request()
        foreign = await self.registry.create_project(
            50, "finance-foreign-123", "finance-foreign", "Чужой"
        )
        for call in (
            self.store.summary(self.pid, 50),
            self.store.list_withdrawals(self.pid, 50),
            self.store.get_withdrawal(foreign["id"], 50, row["request_key"]),
            self.store.operator_details(foreign["id"], row["id"], 99),
        ):
            with self.assertRaises(FinanceNotFound):
                await call
        with self.assertRaises(FinanceForbidden):
            await self.store.claim_withdrawal(self.pid, row["id"], 42)
        with self.assertRaises(FinanceForbidden):
            await self.store.list_operator_withdrawals(42)

    async def test_limits_negative_float_bool_and_available(self):
        for amount in (0, -1, 149999, True, 150000.0, 9000000000000001):
            with self.assertRaises(InvalidFinance):
                await self.request(amount)
        with self.assertRaises(FinanceConflict):
            await self.request(700001)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_withdrawals"), 0
        )
        await self.request(550000)
        with self.assertRaises(FinanceConflict):
            await self.request()

    async def test_rejection_releases_once_retains_history(self):
        row = await self.request(500000)
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 200000
        )
        rejected = await self.store.reject_withdrawal(
            self.pid, row["id"], 99, "Проверьте банк", now=self.now
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(
            rejected,
            await self.store.reject_withdrawal(
                self.pid, row["id"], 99, "Проверьте банк", now=self.now
            ),
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 700000
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["reserved_kopecks"], 0
        )
        with self.assertRaises(FinanceConflict):
            await self.store.reject_withdrawal(
                self.pid, row["id"], 99, "Другая причина"
            )
        self.assertEqual(
            len((await self.store.list_withdrawals(self.pid, 42))["items"]), 1
        )

    async def test_manual_claim_confirm_exact_once_no_automatic_release(self):
        row = await self.request(500000)
        with self.assertRaises(FinanceConflict):
            await self.store.confirm_withdrawal_paid(
                self.pid, row["id"], 99, 500000, "bank:tx-001"
            )
        claimed = await self.store.claim_withdrawal(
            self.pid, row["id"], 99, now=self.now
        )
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(
            claimed,
            await self.store.claim_withdrawal(self.pid, row["id"], 99, now=self.now),
        )
        with self.assertRaises(FinanceConflict):
            await self.store.claim_withdrawal(self.pid, row["id"], 98)
        with self.assertRaises(FinanceConflict):
            await self.store.reject_withdrawal(
                self.pid, row["id"], 99, "Нельзя угадывать"
            )
        with self.assertRaises(FinanceConflict):
            await self.store.confirm_withdrawal_paid(
                self.pid, row["id"], 99, 499999, "bank:tx-001"
            )
        paid = await self.store.confirm_withdrawal_paid(
            self.pid, row["id"], 99, 500000, "bank:tx-001", now=self.now
        )
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(
            paid,
            await self.restarted().confirm_withdrawal_paid(
                self.pid, row["id"], 99, 500000, "bank:tx-001", now=self.now
            ),
        )
        summary = await self.store.summary(self.pid, 42)
        self.assertEqual(
            (
                summary["paid_kopecks"],
                summary["reserved_kopecks"],
                summary["available_kopecks"],
            ),
            (500000, 0, 200000),
        )
        self.assertNotIn("bank:tx", str(paid))
        detail = await self.store.operator_details(self.pid, row["id"], 99)
        self.assertEqual(detail["transfer_reference"], "bank:tx-001")
        with self.assertRaises(FinanceConflict):
            await self.store.confirm_withdrawal_paid(
                self.pid, row["id"], 99, 500000, "bank:tx-002"
            )

    async def test_duplicate_transfer_reference_does_not_pay_second_request(self):
        a = await self.request()
        await self.store.claim_withdrawal(self.pid, a["id"], 99)
        await self.store.confirm_withdrawal_paid(
            self.pid, a["id"], 99, 150000, "bank:same-external-tx"
        )
        b = await self.request()
        await self.store.claim_withdrawal(self.pid, b["id"], 99)
        with self.assertRaises(FinanceConflict):
            await self.store.confirm_withdrawal_paid(
                self.pid, b["id"], 99, 150000, "bank:same-external-tx"
            )
        self.assertEqual(
            (await self.store.get_withdrawal(self.pid, 42, b["request_key"]))["status"],
            "processing",
        )

    async def test_processing_release_only_explicit_verified_no_transfer_by_same_operator(
        self,
    ):
        row = await self.request()
        await self.store.claim_withdrawal(self.pid, row["id"], 99)
        with self.assertRaises(FinanceConflict):
            await self.store.reject_withdrawal(
                self.pid,
                row["id"],
                98,
                "Банк подтвердил отсутствие перевода",
                confirmed_no_transfer=True,
            )
        with self.assertRaises(FinanceConflict):
            await self.store.reject_withdrawal(
                self.pid, row["id"], 99, "Банк подтвердил отсутствие перевода"
            )
        result = await self.store.reject_withdrawal(
            self.pid,
            row["id"],
            99,
            "Банк подтвердил отсутствие перевода",
            confirmed_no_transfer=True,
        )
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(
            await self.sql.fetchval(
                "SELECT no_transfer_confirmed FROM wl_withdrawals WHERE id=$1",
                row["id"],
            )
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 700000
        )

    async def test_partial_reversal_once_debt_blocks_new_request_and_claim(self):
        row = await self.request(600000)
        key, event = str(uuid4()), "provider:partial-001"
        reversal = await self.reverse(500000, key=key, event=event)
        self.assertEqual(
            (reversal["partner_kopecks"], reversal["platform_kopecks"]),
            (350000, 150000),
        )
        self.assertEqual(reversal, await self.reverse(500000, key=key, event=event))
        self.assertEqual(reversal, await self.reverse(500000, event=event))
        summary = await self.store.summary(self.pid, 42)
        self.assertEqual(
            (summary["available_kopecks"], summary["debt_kopecks"]), (0, 250000)
        )
        with self.assertRaises(FinanceConflict):
            await self.request()
        with self.assertRaises(FinanceConflict):
            await self.store.claim_withdrawal(self.pid, row["id"], 99)
        await self.store.reject_withdrawal(
            self.pid, row["id"], 99, "Нужна сверка после сторно"
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 350000
        )
        with self.assertRaises(FinanceConflict):
            await self.reverse(499999, key=key, event=event)
        with self.assertRaises(InvalidFinance):
            await self.reverse(500001)
        await self.reverse(500000)
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 0
        )

    async def test_paid_then_chargeback_debt_repaid_by_future_sales(self):
        row = await self.request(700000)
        await self.store.claim_withdrawal(self.pid, row["id"], 99)
        await self.store.confirm_withdrawal_paid(
            self.pid, row["id"], 99, 700000, "bank:paid-before-dispute"
        )
        await self.reverse()
        summary = await self.store.summary(self.pid, 42)
        self.assertEqual(
            (summary["debt_kopecks"], summary["paid_kopecks"]), (700000, 700000)
        )
        await self.earn()
        self.assertEqual((await self.store.summary(self.pid, 42))["debt_kopecks"], 0)
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 0
        )
        await self.earn()
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 700000
        )

    async def test_historical_actual_transfer_recorded_even_if_debt_arises_after_claim(
        self,
    ):
        row = await self.request(700000)
        await self.store.claim_withdrawal(self.pid, row["id"], 99)
        await self.reverse()
        await self.store.confirm_withdrawal_paid(
            self.pid, row["id"], 99, 700000, "bank:already-executed-transfer"
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["debt_kopecks"], 700000
        )

    async def test_outbox_failure_rolls_back_reservation_and_audit(self):
        original = self.sql.execute

        async def fail(sql, *args):
            if "INSERT INTO wl_finance_outbox" in sql:
                raise RuntimeError("injected outbox failure")
            return await original(sql, *args)

        self.sql.execute = fail
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await self.request()
        self.sql.execute = original
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_withdrawals"), 0
        )
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_finance_audit"), 0
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 700000
        )

    async def test_reversal_outbox_failure_rolls_back_commission_storno(self):
        original = self.sql.execute

        async def fail(sql, *args):
            if "INSERT INTO wl_finance_outbox" in sql:
                raise RuntimeError("injected outbox failure")
            return await original(sql, *args)

        self.sql.execute = fail
        with self.assertRaises(RuntimeError):
            await self.reverse()
        self.sql.execute = original
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_reversals"), 0
        )
        self.assertEqual(
            (await self.store.summary(self.pid, 42))["available_kopecks"], 700000
        )

    async def test_admin_recipient_configuration_required_before_reserving(self):
        for store in (
            self.restarted(admin_recipient_id=None),
            self.restarted(admin_recipient_id=10),
            self.restarted(vault=None),
        ):
            with self.assertRaises(FinanceUnavailable):
                await store.request_withdrawal(
                    self.pid, 42, str(uuid4()), 150000, "sbp", self.requisites
                )
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_withdrawals"), 0
        )

    async def test_existing_request_replays_when_project_suspended_new_request_forbidden(
        self,
    ):
        key = str(uuid4())
        row = await self.request(key=key)
        await self.sql.execute(
            "UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid
        )
        self.assertEqual(row, await self.request(key=key))
        with self.assertRaises(FinanceUnavailable):
            await self.request()

    async def test_append_only_events_and_immutable_paid_withdrawal(self):
        row = await self.request()
        await self.store.claim_withdrawal(self.pid, row["id"], 99)
        await self.store.confirm_withdrawal_paid(
            self.pid, row["id"], 99, 150000, "bank:immutable-transfer"
        )
        reversal = await self.reverse(100)
        for query, args in (
            (
                "UPDATE wl_withdrawals SET amount_kopecks=160000 WHERE id=$1",
                (row["id"],),
            ),
            ("UPDATE wl_withdrawals SET status='pending' WHERE id=$1", (row["id"],)),
            ("DELETE FROM wl_withdrawals WHERE id=$1", (row["id"],)),
            ("DELETE FROM wl_revenue_reversals WHERE id=$1", (reversal["id"],)),
            ("DELETE FROM wl_finance_audit", ()),
        ):
            with self.assertRaises(RuntimeError):
                await self.sql.execute(query, *args)

    async def test_history_lists_redacted_bounded_and_reversal_owner_scope(self):
        first = await self.request()
        await self.store.reject_withdrawal(self.pid, first["id"], 99, "Проверьте банк")
        await self.request(method="usdt", requisites={"address": "T" + "a" * 33})
        await self.reverse(123)
        history = await self.store.list_withdrawals(self.pid, 42, limit=1)
        self.assertEqual(len(history["items"]), 1)
        self.assertTrue(history["truncated"])
        operator = await self.store.list_operator_withdrawals(99)
        self.assertEqual(len(operator["items"]), 2)
        self.assertNotIn("encrypted", str(operator))
        self.assertNotIn("7999123", str(operator))
        self.assertNotIn("a" * 20, str(operator))
        reversals = await self.store.list_reversals(self.pid, 42)
        self.assertEqual(len(reversals["items"]), 1)
        self.assertNotIn("evidence", str(reversals))
        with self.assertRaises(FinanceNotFound):
            await self.store.list_reversals(self.pid, 99)
