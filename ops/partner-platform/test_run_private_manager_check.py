"""Offline checks only: no source credential reads, DB, systemd or Telegram."""

import importlib.util
import io
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "run_private_manager_check", Path(__file__).with_name("run_private_manager_check.py")
)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class PrivateManagerCheckTests(unittest.TestCase):
    def test_default_plan_never_runs_preflight_commands_or_reads_credentials(self):
        for action in RUNNER.ACTIONS:
            with self.subTest(action=action), patch.object(RUNNER, "run") as run, patch.object(RUNNER, "preflight") as preflight, patch.object(RUNNER.subprocess, "run") as command, patch("builtins.print"):
                self.assertEqual(RUNNER.main(["--action", action]), 0)
                run.assert_not_called()
                preflight.assert_not_called()
                command.assert_not_called()

    def test_unsupported_action_and_webhook_replacement_are_rejected(self):
        for args in (("--action", "serve"), ("--action", "worker"), ("--action", "bootstrap-manager", "--replace-manager-webhook")):
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
                RUNNER.main(list(args))
            self.assertEqual(raised.exception.code, 2)
        with self.assertRaises(RUNNER.CheckError):
            RUNNER.build_argv("serve")

    def test_transient_unit_has_expected_identity_credential_and_sandbox(self):
        for action in RUNNER.ACTIONS:
            argv = RUNNER.build_argv(action)
            self.assertEqual(argv[0], "/usr/bin/systemd-run")
            for flag in ("--wait", "--pipe", "--collect", "--quiet"):
                self.assertIn(flag, argv)
            for setting in ("User=spn-partner", "Group=spn-partner", "NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=yes", "UMask=0077", "LimitCORE=0", "RuntimeDirectoryMode=0700", "RuntimeDirectoryPreserve=no", f"RuntimeDirectory=spn-partner-{action}", f"LoadCredential=staging.json:{RUNNER.SOURCE}", "RuntimeMaxSec=165s", "KillMode=control-group"):
                self.assertIn("--property=" + setting, argv)
            self.assertIn("--setenv=PYTHON_DOTENV_DISABLED=1", argv)
            self.assertIn("--setenv=PYTHONDONTWRITEBYTECODE=1", argv)
            self.assertEqual(argv[-5:-1], [str(RUNNER.PYTHON), "-I", "-B", "-c"])

    def test_child_uses_environment_without_shell_and_executes_only_fixed_action(self):
        for action in RUNNER.ACTIONS:
            code = RUNNER.child_code(action)
            compile(code, "<credential-copy>", "exec")
            self.assertIn("os.environ['CREDENTIALS_DIRECTORY']", code)
            self.assertIn("os.fchmod(fd, 0o600)", code)
            self.assertIn("os.O_EXCL | os.O_NOFOLLOW", code)
            self.assertIn(f"/run/spn-partner-{action}/staging.json", code)
            self.assertIn(f"'--{action}'", code)
            self.assertIn("os.execv(", code)
            for forbidden in ("${", "shell=True", "--replace-manager-webhook", "--serve", "--worker", "print(data", "str(error"):
                self.assertNotIn(forbidden, code)

    def test_explicit_run_uses_shell_false_timeout_and_inherited_stdout(self):
        with patch.object(RUNNER, "preflight") as preflight, patch.object(RUNNER.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as command:
            self.assertEqual(RUNNER.run("probe"), 0)
        preflight.assert_called_once_with()
        self.assertFalse(command.call_args.kwargs["shell"])
        self.assertEqual(command.call_args.kwargs["timeout"], 180)
        self.assertNotIn("stdout", command.call_args.kwargs)
        self.assertNotIn("capture_output", command.call_args.kwargs)

    def test_preflight_failure_never_starts_transient_unit(self):
        with patch.object(RUNNER, "preflight", side_effect=RUNNER.CheckError("unsafe")), patch.object(RUNNER.subprocess, "run") as command, patch("builtins.print"):
            self.assertEqual(RUNNER.main(["--action", "probe", "--run"]), 2)
        command.assert_not_called()

    def test_execution_errors_and_timeouts_never_echo_raw_exception(self):
        for error in (RuntimeError("sensitive-value"), RUNNER.subprocess.TimeoutExpired("sensitive-value", 180)):
            with self.subTest(error=type(error).__name__), patch.object(RUNNER, "run", side_effect=error), patch("builtins.print") as output:
                self.assertEqual(RUNNER.main(["--action", "probe", "--run"]), 2)
            self.assertNotIn("sensitive-value", str(output.call_args))

    def test_failed_runtime_returns_generic_status_without_raw_output(self):
        with patch.object(RUNNER, "preflight"), patch.object(RUNNER.subprocess, "run", return_value=SimpleNamespace(returncode=2)), patch("builtins.print") as output:
            self.assertEqual(RUNNER.run("bootstrap-manager"), 2)
        self.assertIn("private_runtime_check_failed", output.call_args.args[0])

    def test_root_preflight_uses_only_config_metadata_and_local_address(self):
        private = Mock()
        private.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)
        source = Mock()
        source.lstat.return_value = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1, st_size=32
        )
        runtime, python = Mock(), Mock()
        for path in (runtime, python.resolve.return_value):
            path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
        with (
            patch.object(RUNNER.sys, "platform", "linux"),
            patch.object(RUNNER.os, "geteuid", return_value=0),
            patch.object(RUNNER, "trusted_parents"),
            patch.object(RUNNER, "PRIVATE", private), patch.object(RUNNER, "SOURCE", source),
            patch.object(RUNNER, "RUNTIME", runtime), patch.object(RUNNER, "PYTHON", python),
            patch.object(RUNNER.subprocess, "run", return_value=SimpleNamespace(
                stdout=b'[{"addr_info":[{"family":"inet","local":"92.42.102.103"}]}]'
            )) as command,
            patch("builtins.open", side_effect=AssertionError("no credential reads")),
        ):
            RUNNER.preflight()
        self.assertEqual([call[0] for call in source.mock_calls], ["lstat"])
        self.assertEqual(command.call_args.args[0], ["/usr/sbin/ip", "-j", "-4", "address", "show"])

    def test_nonroot_fails_before_any_file_or_command(self):
        with patch.object(RUNNER.sys, "platform", "linux"), patch.object(RUNNER.os, "geteuid", return_value=123), patch.object(RUNNER, "trusted_parents") as metadata, patch.object(RUNNER.subprocess, "run") as command:
            with self.assertRaises(RUNNER.CheckError):
                RUNNER.preflight()
        metadata.assert_not_called()
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
