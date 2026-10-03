import importlib.util
from datetime import datetime
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compensate_bypass_outage.py"
SPEC = importlib.util.spec_from_file_location("compensate_bypass_outage", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BypassCompensationTests(unittest.TestCase):
    def test_active_subscription_gets_days_after_current_expiry(self):
        now = datetime(2026, 9, 25, 12, 0)
        current = datetime(2026, 10, 10, 8, 30)
        self.assertEqual(
            MODULE.compensation_expiry(current, now, 5),
            datetime(2026, 10, 15, 8, 30),
        )

    def test_expired_subscription_gets_five_usable_days(self):
        now = datetime(2026, 9, 25, 12, 0)
        current = datetime(2026, 9, 18, 8, 30)
        self.assertEqual(
            MODULE.compensation_expiry(current, now, 5),
            datetime(2026, 9, 30, 12, 0),
        )

    def test_singular_message(self):
        text = MODULE.compensation_message(1, 5)
        self.assertIn("ваша подписка с антиглушилкой продлена", text.lower())
        self.assertNotIn("(1)", text)

    def test_plural_message(self):
        text = MODULE.compensation_message(3, 5)
        self.assertIn("ваши подписки с антиглушилкой (3) продлены", text.lower())


if __name__ == "__main__":
    unittest.main()
