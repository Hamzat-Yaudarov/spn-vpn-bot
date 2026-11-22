"""Offline safety checks; never invoke Nginx, Certbot or read server credentials."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "prepare_manager_https", Path(__file__).with_name("prepare_manager_https.py")
)
PREP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREP)


class ManagerHTTPSPreparationTests(unittest.TestCase):
    def test_default_is_plan_without_preflight_or_changes(self):
        with patch.object(PREP, "apply") as apply, patch("builtins.print"):
            self.assertEqual(PREP.main([]), 0)
        apply.assert_not_called()

    def test_unconfirmed_target_fails_before_account_or_commands(self):
        with patch.object(PREP, "existing_account") as account:
            with self.assertRaises(PREP.PreparationError):
                PREP.preflight("wrong-target")
        account.assert_not_called()

    def test_missing_account_fails_before_any_scoped_write(self):
        with (
            patch.object(PREP.sys, "platform", "linux"),
            patch.object(PREP.os, "geteuid", return_value=0),
            patch.object(PREP, "existing_account", side_effect=PREP.PreparationError("needs_user")),
            patch.object(PREP, "write_new") as write,
            patch.object(PREP, "activate_nginx") as activate,
            patch.object(PREP, "command") as command,
            patch.object(PREP, "WEBROOT") as webroot,
        ):
            with self.assertRaises(PREP.PreparationError):
                PREP.apply(PREP.TARGET)
        write.assert_not_called()
        activate.assert_not_called()
        command.assert_not_called()
        webroot.mkdir.assert_not_called()

    def test_exact_host_acme_and_https_boundary(self):
        http, https = PREP.http_config(), PREP.https_config()
        self.assertIn("location ^~ /.well-known/acme-challenge/", http)
        self.assertIn("location / { return 503; }", http)
        self.assertIn("ssl_reject_handshake on;", https)
        self.assertIn("if ($ssl_server_name != partners.wayspn.online)", https)
        self.assertIn("if ($http_host !~* ^partners\\.wayspn\\.online(:443)?$)", https)
        self.assertIn("proxy_pass http://127.0.0.1:8966;", https)
        self.assertIn("proxy_set_header Host partners.wayspn.online;", https)
        for header in ("Forwarded", "X-Forwarded-Host", "X-Forwarded-For", "X-Real-IP"):
            self.assertIn(f'proxy_set_header {header} "";', https)
        self.assertNotIn("00-franchise-closed.conf", str(PREP.HTTP))
        self.assertNotIn("00-franchise-closed.conf", str(PREP.HTTPS))

    def test_issuance_uses_only_preexisting_account_and_verifies_before_https(self):
        phases = []
        webroot = Mock()
        hook = Mock()
        hook.parent.exists.return_value = True
        with (
            patch.object(PREP, "preflight", return_value=("a" * 32, {"enabled": False, "active": False})),
            patch.object(PREP, "WEBROOT", webroot),
            patch.object(PREP, "HOOK", hook),
            patch.object(PREP, "write_new"),
            patch.object(PREP, "activate_nginx", side_effect=lambda path, text: phases.append(path)),
            patch.object(PREP, "command", side_effect=lambda args, **kwargs: phases.append(args)),
            patch.object(PREP, "certificate_metadata", side_effect=lambda: phases.append("verified") or "digest"),
        ):
            result = PREP.apply(PREP.TARGET)
        self.assertEqual(phases[0], PREP.HTTP)
        args = phases[1]
        self.assertEqual(args[0:2], ["/usr/bin/certbot", "certonly"])
        self.assertEqual(args[args.index("--account") + 1], "a" * 32)
        self.assertEqual(args[args.index("--server") + 1], PREP.SERVER)
        self.assertIn("--non-interactive", args)
        self.assertIn("--no-directory-hooks", args)
        for forbidden in ("register", "--agree-tos", "--email", "--register-unsafely-without-email"):
            self.assertNotIn(forbidden, args)
        self.assertEqual(phases[-2:], ["verified", PREP.HTTPS])
        webroot.chmod.assert_called_once_with(0o755)
        self.assertFalse(result["application_started"])
        self.assertFalse(result["old_configs_overwritten"])
        self.assertTrue(result["needs_timer_activation"])
        self.assertFalse(result["renewal_timer_active"])
        self.assertFalse(result["renewal_timer_enabled"])
        self.assertNotIn("renewal-hooks", str(PREP.HOOK))

    def test_failed_nginx_validation_removes_only_new_config(self):
        path = Mock()
        with (
            patch.object(PREP, "write_new") as write,
            patch.object(PREP, "nginx_test", side_effect=[PREP.PreparationError("invalid"), None]),
            patch.object(PREP, "command") as command,
        ):
            with self.assertRaises(PREP.PreparationError):
                PREP.activate_nginx(path, "new config")
        write.assert_called_once_with(path, "new config", 0o644)
        path.unlink.assert_called_once_with()
        command.assert_called_once_with(["/usr/bin/systemctl", "reload", "nginx"])

    def test_disabled_inactive_timer_is_reported_without_activation(self):
        with patch.object(PREP, "command", side_effect=[
            SimpleNamespace(returncode=1), SimpleNamespace(returncode=3)
        ]) as command:
            self.assertEqual(PREP.renewal_timer_status(), {"enabled": False, "active": False})
        self.assertEqual([call.args[0][1] for call in command.call_args_list], ["is-enabled", "is-active"])
        self.assertNotIn("start", str(command.call_args_list))
        self.assertNotIn("enable --", str(command.call_args_list))

    def test_active_enabled_timer_remains_unchanged(self):
        with patch.object(PREP, "command", return_value=SimpleNamespace(returncode=0)) as command:
            self.assertEqual(PREP.renewal_timer_status(), {"enabled": True, "active": True})
        self.assertEqual(command.call_count, 2)

    def test_local_address_must_match_actual_fixed_target(self):
        for output, passes in ((b'[{"addr_info":[{"family":"inet","local":"92.42.102.103"}]}]', True),
                               (b'[{"addr_info":[{"family":"inet","local":"127.0.0.1"}]}]', False),
                               (b'invalid', False)):
            with self.subTest(output=output), patch.object(PREP, "command", return_value=SimpleNamespace(stdout=output)):
                if passes:
                    PREP.verify_local_target()
                else:
                    with self.assertRaises(PREP.PreparationError):
                        PREP.verify_local_target()

    def test_renewal_hook_is_lineage_scoped_and_never_starts_nginx(self):
        hook = PREP.deploy_hook()
        self.assertIn(f'"{PREP.LIVE}" ] || exit 0', hook)
        self.assertIn("nginx -t", hook)
        self.assertIn("systemctl reload nginx", hook)
        self.assertNotIn("start nginx", hook)

    def test_exclusive_write_preserves_existing_file(self):
        with patch.object(PREP.os, "open", side_effect=FileExistsError), patch.object(Path, "unlink") as unlink:
            with self.assertRaises(FileExistsError):
                PREP.write_new(Path("/fake/existing"), "ignored", 0o644)
        unlink.assert_not_called()

    def test_nginx_warnings_fail_closed(self):
        with patch.object(PREP, "command", return_value=SimpleNamespace(stderr=b"[warn] conflict", stdout=b"")):
            with self.assertRaises(PREP.PreparationError):
                PREP.nginx_test()


if __name__ == "__main__":
    unittest.main()
