"""Offline account-wide purchase budget tests; no browser or provider calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from spn_ip_failover.budget import AccountBudget, BudgetDenied, BudgetError, BudgetSettings


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(TemporaryDirectory())
        self.root = Path(self.directory)
        self.clock = [datetime(2026, 11, 14, 19, 59, 59, tzinfo=timezone.utc).timestamp()]
        self.settings = BudgetSettings(self.root / "budget", Decimal("3000.00"))
        self.budget = self.make()

    def make(self, settings=None, account="one-shared-browser-profile"):
        return AccountBudget(settings or self.settings, account, now=lambda: self.clock[0])

    def day_after(self, count=1):
        self.clock[0] += 86400 * count

    def test_default_zero_limit_disables_even_a_free_order(self):
        budget = self.make(BudgetSettings(self.root / "zero", Decimal("0.00")))
        with self.assertRaises(BudgetDenied):
            budget.reserve("server", "request", Decimal("0.00"))

    def test_all_five_servers_share_one_gross_limit(self):
        for index in range(10):
            budget = self.make()
            budget.reserve("server-" + str(index % 5), "request-" + str(index), Decimal("300.00"))
        with self.assertRaises(BudgetDenied):
            self.make().reserve("server-4", "request-11", Decimal("0.01"))
        self.assertEqual(self.budget.summary()["available_rub"], "0.00")

    def test_reserved_request_is_idempotent_but_cannot_change_price(self):
        key = self.budget.reserve("server", "same", Decimal("10.00"))
        self.assertEqual(self.make().reserve("server", "same", Decimal("10.00")), key)
        with self.assertRaises(BudgetError):
            self.budget.reserve("server", "same", Decimal("10.01"))
        self.assertEqual(self.budget.summary()["used_rub"], "10.00")

    def test_submitted_unknown_request_cannot_authorize_another_dispatch(self):
        self.budget.reserve("server", "same", Decimal("300.00"))
        self.budget.transition("server", "same", "submitting")
        with self.assertRaises(BudgetError):
            self.make().reserve("server", "same", Decimal("300.00"))
        self.assertEqual(self.budget.record("server", "same")["phase"], "submitting")

    def test_saratov_midnight_not_utc_midnight_starts_new_day(self):
        self.budget.reserve("server", "yesterday", Decimal("3000.00"))
        self.budget.transition("server", "yesterday", "submitting")
        self.budget.transition("server", "yesterday", "confirmed")
        self.assertEqual(self.budget.summary()["day"], "2026-11-14")
        self.clock[0] += 2
        self.assertEqual(self.budget.summary()["day"], "2026-11-15")
        self.budget.reserve("server", "today", Decimal("3000.00"))

    def test_unknown_reservation_never_expires_at_midnight(self):
        self.budget.reserve("server", "unknown", Decimal("3000.00"))
        self.budget.transition("server", "unknown", "submitting")
        self.day_after(10)
        with self.assertRaises(BudgetDenied):
            self.make().reserve("other", "new", Decimal("0.01"))
        self.assertEqual(self.budget.summary()["used_rub"], "3000.00")

    def test_delayed_confirmation_keeps_todays_consumed_quota(self):
        self.budget.reserve("server", "unknown", Decimal("3000.00"))
        self.budget.transition("server", "unknown", "submitting")
        self.day_after()
        self.budget.transition("server", "unknown", "confirmed")
        with self.assertRaises(BudgetDenied):
            self.budget.reserve("other", "new", Decimal("0.01"))
        self.assertEqual(self.budget.record("server", "unknown")["days"], ["2026-11-14", "2026-11-15"])
        self.day_after()
        self.assertEqual(self.budget.summary()["used_rub"], "0.00")

    def test_replaying_confirmed_request_does_not_charge_another_day(self):
        self.budget.reserve("server", "request", Decimal("300.00"))
        self.budget.transition("server", "request", "confirmed")
        self.day_after()
        self.make().transition("server", "request", "confirmed")
        self.assertEqual(self.budget.summary()["used_rub"], "0.00")

    def test_no_purchase_reconciliation_does_not_restore_todays_quota(self):
        self.budget.reserve("server", "request", Decimal("3000.00"))
        self.budget.transition("server", "request", "submitting")
        self.day_after()
        self.budget.transition("server", "request", "no_purchase")
        with self.assertRaises(BudgetDenied):
            self.budget.reserve("other", "request", Decimal("0.01"))
        self.day_after()
        self.assertEqual(self.budget.summary()["available_rub"], "3000.00")

    def test_confirmed_purchase_cannot_be_reclassified_as_no_purchase(self):
        self.budget.reserve("server", "request", Decimal("100.00"))
        self.budget.transition("server", "request", "confirmed")
        with self.assertRaises(BudgetError):
            self.budget.transition("server", "request", "no_purchase")
        self.assertEqual(self.budget.summary()["used_rub"], "100.00")

    def test_concurrent_servers_never_over_reserve_shared_account(self):
        def reserve(index):
            try:
                self.make().reserve("server-" + str(index % 5), "request-" + str(index), Decimal("300.00"))
                return True
            except BudgetDenied:
                return False
        with ThreadPoolExecutor(max_workers=5) as pool:
            admitted = list(pool.map(reserve, range(20)))
        self.assertEqual(sum(admitted), 10)
        self.assertEqual(self.budget.summary()["used_rub"], "3000.00")

    def test_account_and_timezone_are_pinned_to_shared_journal(self):
        self.budget.reserve("server", "request", Decimal("100.00"))
        with self.assertRaises(BudgetError):
            self.make(account="another-browser-profile").summary()
        with self.assertRaises(ValueError):
            BudgetSettings(self.root / "budget", Decimal("3000.00"), "UTC")

    def test_lowered_daily_limit_blocks_dispatch_of_old_reservation(self):
        self.budget.reserve("server", "request", Decimal("300.00"))
        lowered = self.make(BudgetSettings(self.settings.journal, Decimal("100.00")))
        with self.assertRaises(BudgetDenied):
            lowered.transition("server", "request", "submitting")
        self.assertEqual(self.budget.record("server", "request")["phase"], "reserved")

    def test_quotes_must_be_finite_decimal_rubles(self):
        for amount in (1.0, Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal("1.001")):
            with self.subTest(amount=str(amount)), self.assertRaises((ValueError, BudgetError)):
                self.budget.reserve("server", "request", amount)


if __name__ == "__main__":
    unittest.main()
