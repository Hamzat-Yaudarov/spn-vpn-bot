"""Offline public-entry deployment gates: no credentials, root or network."""
from copy import deepcopy
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("public_entry_upgrade_subject", HERE / "upgrade_public_entry.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class PublicEntryUpgradeTests(unittest.TestCase):
    def config(self):
        return {"runtime": {"enable_owner_entry_pilot": True,
                "manager_allowed_user_ids": [subject.OWNER], "operator_ids": [subject.OWNER],
                "admin_recipient_id": subject.OWNER, "database_dsn": "PRIVATE",
                "enable_provisioning": False, "enable_public_checkout": False,
                "encryption_keys": ["KEEP"], "yookassa_secret_key": "KEEP"},
                "staging_addon_catalog": None}

    def test_no_arguments_plan_reads_nothing(self):
        output = io.StringIO()
        with patch.object(subject, "load_ops") as load, contextlib.redirect_stdout(output):
            self.assertEqual(subject.main([]), 0)
        load.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])

    def test_candidate_changes_only_three_authorized_settings(self):
        before = self.config()
        after = json.loads(subject.candidate(json.dumps(before)))
        expected = deepcopy(before)
        expected["runtime"].update(enable_owner_entry_pilot=False,
                                   enable_public_entry_trial=True, manager_allowed_user_ids=None)
        self.assertEqual(after, expected)
        self.assertEqual(before, self.config())

    def test_candidate_refuses_unreviewed_access_or_repeated_transition(self):
        for changes in ({"manager_allowed_user_ids": None}, {"operator_ids": []},
                        {"admin_recipient_id": 42}, {"enable_public_entry_trial": True},
                        {"enable_owner_entry_pilot": False}):
            before = self.config(); before["runtime"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(subject.UpgradeError):
                subject.candidate(json.dumps(before))

    def test_pinned_reviewed_helpers_match_local_bytes(self):
        for name, sha in subject.HELPER_HASHES.items():
            self.assertEqual(hashlib.sha256((HERE / name).read_bytes()).hexdigest(), sha)

    def test_allowlist_excludes_main_bot_credentials_and_dependencies(self):
        for name in ("handlers/start.py", "config.py", ".env", "requirements.txt",
                     "ops/partner-platform/requirements-staging.txt", "database.py"):
            self.assertNotIn(name, subject.ALLOWED_CHANGED)

    def test_failures_are_redacted(self):
        output = io.StringIO()
        with patch.object(subject, "backup_database", side_effect=ValueError("SECRET")), \
             contextlib.redirect_stderr(output):
            self.assertEqual(subject.main(["--backup", "--confirm-target", "92.42.102.103"]), 2)
        self.assertNotIn("SECRET", output.getvalue())

    def test_cli_argument_errors_do_not_echo_private_values(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(subject.main(["--unrecognized", "PRIVATE-SECRET"]), 2)
            self.assertEqual(subject.main(["--backup", "--upgrade", "PRIVATE-SECRET"]), 2)
        self.assertNotIn("PRIVATE-SECRET", output.getvalue())

    def deployment_case(self, *, failure=None, bad_backup=False):
        """Only temporary fake files/ports; no credential, service or network I/O."""
        raw = json.dumps(self.config()).encode()
        blob = b"fixed-fake-archive"
        sha = hashlib.sha256(blob).hexdigest()
        events = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            releases, private, units = (root / name for name in ("releases", "private", "units"))
            for folder in (releases, private, units): folder.mkdir()
            old = releases / subject.OLD_RELEASE.name
            old.mkdir()
            release = releases / ("20261025-" + sha[:12])
            config = private / "staging.json"
            config.write_bytes(raw)
            data_backup = private / "data.json"
            data_backup.write_bytes(json.dumps({"project_ref": subject.PROJECT,
                "tables": {"wl_table_" + str(index): [] for index in range(32)}}).encode())
            backup_sha = hashlib.sha256(data_backup.read_bytes()).hexdigest()
            archive = root / "archive"
            archive.write_bytes(blob)
            unit_names = ("spn-partner-http.service", "spn-partner-worker.service")

            def render_unit(path, kind): return (str(path) + ":" + kind).encode()
            old_units = {name: render_unit(old, kind) for name, kind in zip(unit_names, ("http", "worker"))}
            new_units = {name: render_unit(release, kind) for name, kind in zip(unit_names, ("http", "worker"))}
            for name, value in old_units.items(): (units / name).write_bytes(value)

            def unused(path): subject.require(not path.exists())
            def unchanged(value): subject.require(config.read_bytes() == value)
            def write(path, value, *args):
                path.write_bytes(value)
            def command(args):
                events.append(("command", args, config.read_bytes()))
                if failure == "stop" and args[:2] == ["/usr/bin/systemctl", "stop"] and len([
                    row for row in events if row[0] == "command" and row[1][:2] == args[:2]
                ]) == 1:
                    raise subject.UpgradeError("stop response lost")
                if failure == "worker_start" and args == ["/usr/bin/systemctl", "start", unit_names[1]] and len([
                    row for row in events if row[0] == "command" and row[1] == args
                ]) == 1:
                    raise subject.UpgradeError("worker response lost")
                return ""
            def runtime(path, candidate, action):
                events.append(("runtime", path, action, candidate, candidate.read_bytes(), config.read_bytes()))
                if failure == "probe" and candidate != config and action == "--probe":
                    raise subject.UpgradeError("public probe refused")
            def http_ready():
                events.append(("http_ready", config.read_bytes()))
                if failure == "http" and len([row for row in events if row[0] == "http_ready"]) == 1:
                    raise subject.UpgradeError("readiness failed")
            def validate(blob, **kwargs):
                events.append(("validate_archive",))
                return {}
            installer = SimpleNamespace(validate_archive=validate,
                MAX_ARCHIVE=100_000, extract_release=lambda files, gid: release.mkdir(),
                write_exclusive=lambda path, value, **kwargs: write(path, value))
            ops = SimpleNamespace(preflight=lambda *args: (100, raw), read_regular=lambda path, **kwargs: path.read_bytes(),
                unused=unused, document=json.loads, installer=installer,
                selected_release=lambda path: contextlib.nullcontext(),
                write_new=write, atomic=write, command=command, runtime=runtime,
                render_unit=render_unit, UNITS=unit_names, UNIT_DIRECTORY=units,
                unchanged=unchanged, service_state=lambda path: events.append(("service_state", path)),
                http_ready=http_ready)
            presentation = SimpleNamespace(copy_environment=lambda *args: None)
            expected_failure = failure is not None or bad_backup
            with patch.object(subject, "load_ops", return_value=(presentation, ops)), \
                 patch.object(subject, "OLD_RELEASE", old), patch.object(subject, "PRIVATE", private), \
                 patch.object(subject, "CONFIG", config), patch.object(subject, "DATA_BACKUP", data_backup), \
                 patch.object(subject, "release_diff", return_value=["services/partner_runtime.py"]), \
                 patch.object(subject.os, "chown"):
                if expected_failure:
                    with self.assertRaises(subject.UpgradeError):
                        subject.upgrade(archive, sha, subject.TARGET, "0" * 64 if bad_backup else backup_sha)
                else:
                    report = subject.upgrade(archive, sha, subject.TARGET, backup_sha)
                    self.assertEqual(report["manager_access"], "public")
                    self.assertEqual(report["entry_price_rub"], 10)
                    self.assertFalse(report["customer_billing"])
                    self.assertFalse(report["vpn_provisioning"])
            final_config = config.read_bytes()
            final_units = {name: (units / name).read_bytes() for name in unit_names}
            backups = private / ("public-entry-before-20261025-" + sha[:12])
            if bad_backup or failure == "probe":
                self.assertFalse(backups.exists())
                self.assertEqual(final_units, old_units)
            else:
                self.assertEqual((backups / "staging.json").read_bytes(), raw)
                self.assertEqual({name: (backups / name).read_bytes() for name in unit_names}, old_units)
                # After the SQL transition, fallback MUST retain reviewed new
                # code, not an old binary which cannot probe the new schema.
                self.assertEqual(final_units, new_units)
            self.assertEqual(final_config, raw if expected_failure else subject.candidate(raw))
            if expected_failure:
                restored = json.loads(final_config)["runtime"]
                self.assertTrue(restored["enable_owner_entry_pilot"])
                self.assertEqual(restored["manager_allowed_user_ids"], [subject.OWNER])
                self.assertFalse(restored.get("enable_public_entry_trial", False))
            return events, raw

    def test_success_probes_both_access_modes_before_stopping_and_preserves_backup(self):
        events, raw = self.deployment_case()
        probes = [row for row in events if row[0] == "runtime"]
        self.assertEqual([row[2] for row in probes], ["--check", "--probe", "--probe"])
        for row in probes: self.assertEqual(row[-1], raw)
        self.assertEqual(json.loads(probes[0][4])["runtime"]["manager_allowed_user_ids"], None)
        self.assertEqual(json.loads(probes[2][4])["runtime"]["manager_allowed_user_ids"], [subject.OWNER])
        stop = next(index for index, row in enumerate(events) if row[0] == "command" and "stop" in row[1])
        self.assertGreater(stop, max(events.index(row) for row in probes))
        self.assertEqual(events[stop][-1], raw)
        self.assertFalse(any(row[0] == "command" and ("enable" in row[1] or "disable" in row[1]) for row in events))

    def test_bad_backup_digest_never_extracts_replaces_config_or_stops_services(self):
        events, _ = self.deployment_case(bad_backup=True)
        self.assertEqual(events, [])

    def test_failed_public_probe_leaves_old_code_and_owner_only_config_untouched(self):
        events, _ = self.deployment_case(failure="probe")
        self.assertFalse(any(row[0] == "command" for row in events))

    def test_stop_timeout_http_failure_and_worker_failure_fall_back_to_private_new_code(self):
        for failure in ("stop", "http", "worker_start"):
            with self.subTest(failure=failure):
                events, raw = self.deployment_case(failure=failure)
                self.assertEqual(events[-1][0:2], ("command", ["/usr/bin/systemctl", "start", "spn-partner-worker.service"]))
                self.assertEqual(events[-1][-1], raw)


if __name__ == "__main__":
    unittest.main()
