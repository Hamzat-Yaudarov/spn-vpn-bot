from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch

import database as db
from services import payment_reconciliation as reconciliation


def payment(**changes):
    value = {
        "invoice_id": "invoice-1",
        "tg_id": 123,
        "tariff_code": "regular_1m",
        "provider": "yookassa",
        "amount": Decimal("200.00"),
        "status": "pending",
        "refund_requested_at": None,
    }
    value.update(changes)
    return value


class PaymentReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_paid_invoice_is_activated_once_across_repeated_checks(self):
        record = payment()

        async def activate(*args, **kwargs):
            record["status"] = "paid"
            return True

        with (
            patch.object(db, "get_payment_by_invoice", AsyncMock(side_effect=lambda _: dict(record))),
            patch.object(reconciliation, "_provider_state", AsyncMock(return_value=("paid", "200.00"))),
            patch.object(reconciliation, "process_paid_payment", AsyncMock(side_effect=activate)) as process,
        ):
            first = await reconciliation.reconcile_payment(None, "invoice-1", expected_tg_id=123)
            second = await reconciliation.reconcile_payment(None, "invoice-1", expected_tg_id=123)

        self.assertEqual((first.status, second.status), ("paid", "paid"))
        self.assertTrue(first.activated)
        self.assertFalse(second.activated)
        process.assert_awaited_once()

    async def test_amount_mismatch_never_activates(self):
        with (
            patch.object(db, "get_payment_by_invoice", AsyncMock(return_value=payment())),
            patch.object(reconciliation, "_provider_state", AsyncMock(return_value=("paid", "1.00"))),
            patch.object(reconciliation, "process_paid_payment", AsyncMock()) as process,
        ):
            result = await reconciliation.reconcile_payment(None, "invoice-1", expected_tg_id=123)

        self.assertEqual(result.status, "pending")
        self.assertEqual(result.reason, "amount_mismatch")
        process.assert_not_awaited()

    async def test_wrong_user_cannot_check_or_activate_invoice(self):
        with (
            patch.object(db, "get_payment_by_invoice", AsyncMock(return_value=payment())),
            patch.object(reconciliation, "_provider_state", AsyncMock()) as provider,
        ):
            result = await reconciliation.reconcile_payment(None, "invoice-1", expected_tg_id=999)

        self.assertEqual(result.status, "not_found")
        provider.assert_not_awaited()

    async def test_canceled_status_only_updates_still_pending_row(self):
        with (
            patch.object(db, "get_payment_by_invoice", AsyncMock(return_value=payment())),
            patch.object(reconciliation, "_provider_state", AsyncMock(return_value=("canceled", None))),
            patch.object(db, "update_payment_status_if_pending", AsyncMock()) as update,
        ):
            result = await reconciliation.reconcile_payment(None, "invoice-1", expected_tg_id=123)

        self.assertEqual(result.status, "canceled")
        update.assert_awaited_once_with("invoice-1", "canceled")

    async def test_every_pending_invoice_is_checked_oldest_first(self):
        rows = [{"invoice_id": "old-paid"}, {"invoice_id": "new-pending"}]
        results = [
            reconciliation.ReconciliationResult("old-paid", "paid", activated=True),
            reconciliation.ReconciliationResult("new-pending", "pending"),
        ]
        with (
            patch.object(db, "get_pending_payments_for_user", AsyncMock(return_value=rows)),
            patch.object(reconciliation, "reconcile_payment", AsyncMock(side_effect=results)) as reconcile,
        ):
            actual = await reconciliation.reconcile_user_pending_payments(None, 123)

        self.assertEqual(actual, results)
        self.assertEqual(
            [call.args[1] for call in reconcile.await_args_list],
            ["old-paid", "new-pending"],
        )


if __name__ == "__main__":
    unittest.main()
