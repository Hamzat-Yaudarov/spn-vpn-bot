"""Offline setup, with artificial secrets and isolated temporary files only."""

import contextlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import warnings

from scripts import partner_setup as setup
from scripts.partner_runtime import load_config

TOKEN = "123456789:" + "T" * 35
PASSWORD = "not-a-real-db-secret:@/ +&%"


class PartnerSetupTests(unittest.TestCase):
    def build(self, **changes):
        values = dict(owner_id=42, database_password=PASSWORD, manager_token=TOKEN)
        values.update(changes)
        return setup.build_config(**values)

    def test_disabled_private_owner_config_and_encoded_dsn(self):
        config = self.build()
        runtime = config["runtime"]
        self.assertEqual(runtime["mode"], "disabled")
        self.assertEqual(runtime["manager_allowed_user_ids"], [42])
        self.assertFalse(runtime["allow_staging_network"])
        self.assertFalse(runtime["allow_staging_writes"])
        self.assertFalse(runtime["staging_isolation_confirmed"])
        self.assertNotIn("enable_billing", runtime)
        parsed = urlsplit(runtime["database_dsn"])
        self.assertEqual(unquote(parsed.password), PASSWORD)
        self.assertEqual(parsed.port, 5432)
        self.assertEqual(parsed.hostname, "aws-0-ap-northeast-1.pooler.supabase.com")
        self.assertEqual(parsed.username, "spn_partner_runtime.crrmaqiakrqpfvhiwtkq")
        self.assertEqual(runtime["database_connection_mode"], "session")
        self.assertEqual(runtime["manager_bot_username"], "spn_franchise_bot")
        self.assertEqual(runtime["manager_bot_token"], TOKEN)
        self.assertNotEqual(runtime["webhook_key_hex"], runtime["subscription_key_hex"])
        self.assertNotEqual(
            runtime["encryption_keys"], self.build()["runtime"]["encryption_keys"]
        )

    def test_invalid_values_have_no_secret_echo(self):
        for change in (
            {"owner_id": 0},
            {"owner_id": True},
            {"owner_id": "42"},
            {"database_password": "secret"},
            {"database_password": PASSWORD + "\n"},
            {"manager_token": "invalid-sensitive-token"},
        ):
            with self.subTest(change=list(change)), self.assertRaises(
                setup.SetupError
            ) as error:
                self.build(**change)
            for secret in (PASSWORD, TOKEN, "invalid-sensitive-token"):
                self.assertNotIn(secret, str(error.exception))

    def test_exclusive_private_file_loads_without_starting_runtime(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            target = Path(tmp) / "private" / "staging.json"
            config = self.build()
            setup.write_private_config(target, config)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            settings, _ = load_config(target)
            self.assertEqual(settings.mode, "disabled")
            self.assertEqual(settings.manager_allowed_user_ids, (42,))
            before = target.read_bytes()
            with self.assertRaises(setup.SetupError):
                setup.write_private_config(target, self.build())
            self.assertEqual(target.read_bytes(), before)

    def test_repo_env_and_symlink_paths_refused(self):
        for target in (setup.ROOT / "credentials.json", setup.ROOT / ".env.json"):
            with self.subTest(path=target.name), self.assertRaises(setup.SetupError):
                setup.destination(target)
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            folder = Path(tmp)
            real = folder / "real"
            real.mkdir(mode=0o700)
            link = folder / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(setup.SetupError):
                setup.write_private_config(link / "config.json", self.build())
            existing = real / "exists.json"
            existing.write_text("unchanged")
            with self.assertRaises(setup.SetupError):
                setup.write_private_config(existing, self.build())
            self.assertEqual(existing.read_text(), "unchanged")

    def test_shared_parent_refused(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            folder = Path(tmp) / "shared"
            folder.mkdir(mode=0o755)
            with self.assertRaises(setup.SetupError):
                setup.write_private_config(folder / "config.json", self.build())
            self.assertFalse((folder / "config.json").exists())

    def test_partial_write_cleans_only_new_file(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            target = Path(tmp) / "private" / "config.json"
            with patch.object(setup.os, "write", side_effect=OSError("simulated")):
                with self.assertRaises(OSError):
                    setup.write_private_config(target, self.build())
            self.assertFalse(target.exists())

    def test_cli_refuses_pipe_without_prompting_or_secret_output(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            target = Path(tmp) / "private" / "config.json"
            with patch.object(
                setup.sys.stdin, "isatty", return_value=False
            ), patch.object(
                setup.getpass, "getpass"
            ) as prompt, contextlib.redirect_stderr(
                output
            ):
                self.assertEqual(setup.main(["--config", str(target)]), 2)
            prompt.assert_not_called()
            self.assertFalse(target.exists())
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertNotIn(PASSWORD, output.getvalue())

    def test_cli_success_uses_hidden_inputs_and_prints_no_values(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            target = Path(tmp) / "private" / "config.json"
            with patch.object(setup.sys.stdin, "isatty", return_value=True), patch(
                "builtins.input", return_value="42"
            ), patch.object(
                setup.getpass, "getpass", side_effect=[PASSWORD, PASSWORD, TOKEN]
            ), contextlib.redirect_stdout(
                output
            ):
                self.assertEqual(setup.main(["--config", str(target)]), 0)
            self.assertEqual(
                json.loads(target.read_text())["runtime"]["manager_allowed_user_ids"],
                [42],
            )
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertNotIn(PASSWORD, output.getvalue())

    def test_cli_never_falls_back_to_visible_password_input(self):
        def cannot_hide(*args):
            warnings.warn("cannot hide input", setup.getpass.GetPassWarning)
            self.fail("The warning must stop getpass before echoed input")

        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            target = Path(tmp) / "private" / "config.json"
            with patch.object(setup.sys.stdin, "isatty", return_value=True), patch(
                "builtins.input", return_value="42"
            ), patch.object(
                setup.getpass, "getpass", side_effect=cannot_hide
            ), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(setup.main(["--config", str(target)]), 2)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
