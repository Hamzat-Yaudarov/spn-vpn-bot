"""Activation orchestration tested with artificial files and read-only fakes."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import partner_enable_owner_pilot as prepare
from test_partner_enable_owner_pilot import source_config, SHOP, SECRET


SOURCE = Path(__file__).resolve().parents[1] / "ops/partner-platform/activate_owner_pilot.py"
spec = importlib.util.spec_from_file_location("partner_owner_pilot_activation", SOURCE)
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


class ActivationTests(unittest.TestCase):
    def test_default_plan_reads_nothing_and_performs_no_external_action(self):
        output = io.StringIO()
        with patch.object(activation, "activate") as activate, patch.object(activation, "private_read") as read, \
             patch.object(activation, "command") as command, patch.object(activation, "opener") as opener, \
             contextlib.redirect_stdout(output):
            self.assertEqual(activation.main([]), 0)
        for mock in (activate, read, command, opener): mock.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])

    def test_expected_units_are_exact_reviewed_new_release_units(self):
        source = SOURCE.with_name("upgrade_staging.py")
        spec = importlib.util.spec_from_file_location("_upgrade_for_pilot_test", source)
        upgrade = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(upgrade)
        for kind in ("http", "worker"):
            self.assertEqual(activation.expected_unit(kind), upgrade.render_unit(activation.RELEASE, kind))

    def candidate(self):
        return prepare.make_candidate(source_config(), shop_id=SHOP, secret_key=SECRET)

    def test_candidate_validation_rejects_changed_keys_and_extra_enabled_flag(self):
        source = json.dumps(source_config()).encode()
        pending = self.candidate()
        fake_spec = Mock()
        builder = Mock()
        builder.make_candidate = prepare.make_candidate
        with patch.object(activation, "private_read", return_value=b"reviewed builder"), \
             patch.object(activation.importlib.util, "spec_from_file_location", return_value=fake_spec), \
             patch.object(activation.importlib.util, "module_from_spec", return_value=builder):
            self.assertEqual(activation.candidate_for(source, json.dumps(pending).encode()), pending)
            for key, value in (("encryption_keys", ["changed"]), ("enable_provisioning", True),
                               ("manager_allowed_user_ids", [42]), ("database_dsn", "changed")):
                bad = json.loads(json.dumps(pending))
                bad["runtime"][key] = value
                with self.subTest(key=key), self.assertRaises(activation.ActivationError):
                    activation.candidate_for(source, json.dumps(bad).encode())

    def test_merchant_check_is_exact_live_account_and_get_only(self):
        safe = {"account_id": SHOP, "test": False, "status": "enabled", "fiscalization_enabled": False}
        captured = []
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        def open(request, **kw):
            captured.append(request)
            return response
        opener.open.side_effect = open
        with patch.object(activation, "opener", return_value=opener):
            response.read.return_value = json.dumps(safe).encode()
            activation.check_merchant(self.candidate())
            for fields in ({"test": True}, {"test": 0}, {"account_id": "other"},
                           {"status": "disabled"}, {"fiscalization_enabled": True},
                           {"fiscalization_enabled": None}):
                response.read.return_value = json.dumps({**safe, **fields}).encode()
                with self.subTest(fields=fields), self.assertRaises(activation.ActivationError):
                    activation.check_merchant(self.candidate())
        self.assertTrue(all(request.method == "GET" and request.full_url == "https://api.yookassa.ru/v3/me" for request in captured))
        self.assertTrue(all(request.data is None for request in captured))

    def test_database_must_report_real_probe_and_all_readiness_guards(self):
        safe = {"configured": True, "database_ready": True, "database_checked": True,
                "production_ready": False, "blockers": []}
        with patch.object(activation, "command", return_value=json.dumps(safe)) as command:
            activation.check_database()
            self.assertIn("--probe", command.call_args.args[0])
            self.assertNotIn("--worker", command.call_args.args[0])
        for key, value in (("configured", False), ("database_ready", False),
                           ("database_checked", False), ("production_ready", True),
                           ("blockers", ["owner_entry_pilot_migration_required"])):
            with patch.object(activation, "command", return_value=json.dumps({**safe, key: value})), self.assertRaises(activation.ActivationError):
                activation.check_database()

    def test_private_files_reject_symlink_hardlink_and_broad_mode(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp, patch.object(activation, "ROOT_UID", os.geteuid()):
            path = Path(tmp) / "config"
            path.write_bytes(b"secret")
            path.chmod(0o600)
            self.assertEqual(activation.private_read(path), b"secret")
            path.chmod(0o640)
            with self.assertRaises(activation.ActivationError): activation.private_read(path)
            path.chmod(0o600)
            link = path.with_name("link")
            os.link(path, link)
            with self.assertRaises(activation.ActivationError): activation.private_read(path)
            link.unlink()
            link.symlink_to(path)
            with self.assertRaises(activation.ActivationError): activation.private_read(link)

    def test_failed_new_file_flush_removes_only_its_exclusive_partial_file(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp:
            path = Path(tmp) / "new-backup"
            with patch.object(activation.os, "fsync", side_effect=OSError("PRIVATE")):
                with self.assertRaises(OSError): activation.write_new(path, b"private backup")
            self.assertFalse(path.exists())
            path.write_bytes(b"existing master")
            with self.assertRaises(FileExistsError): activation.write_new(path, b"overwrite")
            self.assertEqual(path.read_bytes(), b"existing master")

    def test_existing_backup_requires_explicit_resume_and_exact_disabled_master(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as tmp, patch.object(activation, "ROOT_UID", os.geteuid()):
            backup = Path(tmp) / "staging.before-owner-entry-pilot.json"
            with patch.object(activation, "BACKUP", backup):
                activation.verify_rollback_backup(b"master")
                with self.assertRaises(activation.ActivationError):
                    activation.verify_rollback_backup(b"master", resume_verified_rollback=True)
                backup.write_bytes(b"master")
                backup.chmod(0o600)
                with self.assertRaises(activation.ActivationError):
                    activation.verify_rollback_backup(b"master")
                activation.verify_rollback_backup(b"master", resume_verified_rollback=True)
                for wrong in (b"other master", b"master\n", b""):
                    with self.subTest(wrong=wrong), self.assertRaises(activation.ActivationError):
                        activation.verify_rollback_backup(wrong, resume_verified_rollback=True)
                backup.chmod(0o640)
                with self.assertRaises(activation.ActivationError):
                    activation.verify_rollback_backup(b"master", resume_verified_rollback=True)

    def orchestration(self, *, fail=None, existing_backup=False):
        source, pending = b"old disabled private config", b"new candidate private config"
        files = {activation.CONFIG: source, activation.PENDING: pending}
        if existing_backup: files[activation.BACKUP] = source
        events = []
        def read(path, **kwargs): return files[path]
        def write(path, data):
            if path in files: raise FileExistsError()
            files[path] = data
            events.append(("backup", data))
        def replace(data):
            files[activation.CONFIG] = data
            events.append(("config", data))
            if fail == "fsync" and data == pending: raise OSError("private file error")
        def command(args, **kwargs):
            events.append(tuple(args))
            if fail == "worker" and "enable" in args: raise activation.ActivationError()
            return ""
        def http():
            if fail == "http" and files[activation.CONFIG] == pending: raise activation.ActivationError()
        def verify_backup(raw, *, resume_verified_rollback=False):
            if resume_verified_rollback:
                activation.require(files.get(activation.BACKUP) == raw)
            else:
                activation.require(activation.BACKUP not in files)
        stack = contextlib.ExitStack()
        for name, kwargs in (
            ("preflight", {"return_value": (source, pending)}),
            ("candidate_for", {"return_value": self.candidate()}),
            ("check_merchant", {}), ("check_database", {}),
            ("private_read", {"side_effect": read}), ("write_new", {"side_effect": write}),
            ("replace_master", {"side_effect": replace}), ("command", {"side_effect": command}),
            ("http_ready", {"side_effect": http}), ("service_state", {}),
            ("verify_rollback_backup", {"side_effect": verify_backup}),
        ):
            stack.enter_context(patch.object(activation, name, **kwargs))
        return stack, files, events, source, pending

    def test_success_backups_exact_master_and_enables_only_fixed_worker(self):
        stack, files, events, source, pending = self.orchestration()
        with stack, contextlib.redirect_stdout(io.StringIO()): activation.activate(activation.TARGET)
        self.assertEqual(files[activation.CONFIG], pending)
        self.assertEqual(files[activation.BACKUP], source)
        self.assertEqual(files[activation.PENDING], pending)
        self.assertIn(("/usr/bin/systemctl", "enable", "--now", activation.UNITS[1]), events)
        self.assertNotIn(("/usr/bin/systemctl", "enable", "--now", activation.UNITS[0]), events)

    def test_failure_after_swap_or_worker_enable_rolls_back_only_master(self):
        for failure in ("http", "worker", "fsync"):
            with self.subTest(failure=failure):
                stack, files, events, source, pending = self.orchestration(fail=failure)
                with stack, self.assertRaises(Exception): activation.activate(activation.TARGET)
                self.assertEqual(files[activation.CONFIG], source)
                self.assertEqual(files[activation.BACKUP], source)
                self.assertEqual(files[activation.PENDING], pending)
                self.assertIn(("/usr/bin/systemctl", "stop", activation.UNITS[1]), events)
                self.assertIn(("/usr/bin/systemctl", "disable", activation.UNITS[1]), events)
                self.assertEqual(events[-1], ("/usr/bin/systemctl", "restart", activation.UNITS[0]))

    def test_explicit_resume_reuses_backup_without_rewriting_and_preserves_it_on_failure(self):
        for failure in (None, "worker", "http", "fsync"):
            with self.subTest(failure=failure):
                stack, files, events, source, pending = self.orchestration(fail=failure, existing_backup=True)
                with stack, contextlib.redirect_stdout(io.StringIO()):
                    if failure:
                        with self.assertRaises(Exception):
                            activation.activate(activation.TARGET, resume_verified_rollback=True)
                    else:
                        activation.activate(activation.TARGET, resume_verified_rollback=True)
                self.assertEqual(files[activation.BACKUP], source)
                self.assertEqual(files[activation.CONFIG], source if failure else pending)
                self.assertFalse(any(event[0] == "backup" for event in events))
        for resume in (False, True):
            stack, files, events, source, _ = self.orchestration(existing_backup=True)
            if resume: files[activation.BACKUP] = b"different previous master"
            with stack, self.assertRaises(activation.ActivationError):
                activation.activate(activation.TARGET, resume_verified_rollback=resume)
            self.assertEqual(files[activation.CONFIG], source)
            self.assertEqual(events, [])

    def test_merchant_or_probe_failure_stops_before_config_backup_or_swap(self):
        for failed in ("check_merchant", "check_database"):
            stack, files, events, source, _ = self.orchestration()
            with stack, patch.object(activation, failed, side_effect=RuntimeError("PRIVATE")), self.assertRaises(RuntimeError):
                activation.activate(activation.TARGET)
            self.assertEqual(files[activation.CONFIG], source)
            self.assertNotIn(activation.BACKUP, files)
            self.assertEqual(events, [])

    def test_main_redacts_arbitrary_exception_and_bad_argv(self):
        output = io.StringIO()
        with patch.object(activation, "activate", side_effect=RuntimeError(SECRET)), contextlib.redirect_stderr(output):
            self.assertEqual(activation.main(["--activate", "--confirm-target", activation.TARGET]), 2)
            self.assertEqual(activation.main(["--token", SECRET]), 2)
        self.assertNotIn(SECRET, output.getvalue())

    def test_cli_forwards_only_the_explicit_verified_rollback_option(self):
        with patch.object(activation, "activate") as activate:
            self.assertEqual(activation.main(["--activate", "--confirm-target", activation.TARGET,
                                              "--resume-verified-rollback"]), 0)
        activate.assert_called_once_with(activation.TARGET, resume_verified_rollback=True)


if __name__ == "__main__": unittest.main()
