from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover.onecent_auth import OneCentLogin, OneCentLoginError
from spn_ip_failover.state import Journal


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory())) / "authentication"
        self.page = Mock()
        self.page.url = "https://bill.1cent.host/login"
        self.heading, self.email, self.password, self.submit = [Mock() for _ in range(4)]
        for element in (self.heading, self.email, self.password, self.submit):
            element.count.return_value = 1
            element.is_visible.return_value = True
            element.is_enabled.return_value = True
        self.password.get_attribute.return_value = "password"
        for field in (self.email, self.password):
            field.locator.return_value.count.return_value = 0
        self.page.get_by_role.side_effect = lambda role, **kw: self.heading if role == "heading" else self.submit
        self.page.get_by_label.side_effect = lambda label, **kw: self.email if label == "Почта" else self.password
        self.page.locator.return_value.count.return_value = 0
        self.page.get_by_text.return_value.count.return_value = 0
        self.login = OneCentLogin(self.page, "test@example.invalid", "fixture-secret", self.directory)

    def test_refresh_uses_exact_fields_and_one_submit(self):
        def after_click(**_):
            self.page.url = "https://bill.1cent.host/dashboard/services"

        self.submit.click.side_effect = after_click
        self.assertTrue(self.login())
        self.email.fill.assert_called_once_with("test@example.invalid", timeout=3000)
        self.password.fill.assert_called_once_with("fixture-secret", timeout=3000)
        self.submit.click.assert_called_once_with(timeout=5000)
        self.assertIn("last_attempt", Journal(self.directory).snapshot()["servers"]["1cent"])

    def test_unknown_origin_and_nonlogin_page_never_receive_credentials(self):
        for address in ("https://bill.1cent.host.evil/login", "http://bill.1cent.host/login",
                        "https://bill.1cent.host/dashboard/services"):
            self.page.url = address
            self.assertFalse(self.login())
        self.email.fill.assert_not_called()
        self.password.fill.assert_not_called()
        self.submit.click.assert_not_called()

    def test_first_account_login_creates_private_missing_journal_parent(self):
        nested = self.directory / "authentication"
        login = OneCentLogin(self.page, "test@example.invalid", "fixture-secret", nested)
        with patch.object(login, "_wait_for_dashboard", return_value=True):
            self.assertTrue(login())
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(nested.stat().st_mode & 0o777, 0o700)
        self.assertIn("last_attempt", Journal(nested).snapshot()["servers"]["1cent"])

    def test_challenge_or_foreign_form_destination_never_submit(self):
        challenge = self.page.get_by_text.return_value
        challenge.count.return_value = 1
        challenge.nth.return_value.is_visible.return_value = True
        with self.assertRaises(OneCentLoginError):
            self.login()
        self.email.fill.assert_not_called()
        challenge.count.return_value = 0
        form = self.email.locator.return_value
        form.count.return_value = 1
        form.get_attribute.return_value = "https://different.example.invalid/login"
        with self.assertRaises(OneCentLoginError):
            self.login()
        self.password.fill.assert_not_called()
        self.submit.click.assert_not_called()

    def test_failed_attempt_is_rate_limited_across_restarts(self):
        self.submit.click.side_effect = RuntimeError("fixture-secret must never be reported")
        with self.assertRaises(OneCentLoginError) as failure:
            self.login()
        self.assertNotIn("fixture-secret", str(failure.exception))
        restarted = OneCentLogin(self.page, "test@example.invalid", "fixture-secret", self.directory)
        self.email.fill.reset_mock()
        with self.assertRaisesRegex(OneCentLoginError, "Недавно"):
            restarted()
        self.email.fill.assert_not_called()
        self.assertEqual(self.submit.click.call_count, 1)

    def test_login_query_is_removed_before_credentials_are_entered(self):
        self.page.url = "https://bill.1cent.host/login?next=https://outside.example"

        def navigate(url, **_):
            self.page.url = url
            return Mock(ok=True)

        self.page.goto.side_effect = navigate
        with patch.object(self.login, "_wait_for_dashboard", return_value=True):
            self.assertTrue(self.login())
        self.page.goto.assert_called_once_with(
            "https://bill.1cent.host/login", wait_until="domcontentloaded", timeout=12_000,
        )


if __name__ == "__main__":
    unittest.main()
