"""Offline active-pilot upgrade/feature guards; no secrets, network or services."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_partner_staging_install import make_archive


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("private_pilot_upgrade", ROOT / "ops/partner-platform/upgrade_private_pilot.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
helper_spec = importlib.util.spec_from_file_location("private_pilot_test_helper", ROOT / "ops/partner-platform/install_staging.py")
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)


def config(**changes):
    settings = {name: True for name in ops.ON_FLAGS}
    settings.update({name: False for name in ops.OFF_FLAGS})
    settings.update(mode="staging", manager_allowed_user_ids=[ops.OWNER], operator_ids=[ops.OWNER],
                    admin_recipient_id=ops.OWNER, database_project_ref=ops.PROJECT,
                    database_connection_mode="session", database_ca_file="/etc/spn-partner-ca/supabase.crt",
                    base_domain="sub.wayspn.online", manager_bot_username="spn_franchise_bot",
                    manager_origin="https://partners.wayspn.online", webhook_origin="https://partners.wayspn.online",
                    database_dsn="postgresql://spn_partner_runtime." + ops.PROJECT + ":SECRET@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres",
                    manager_bot_token="BOT-SECRET", encryption_keys=["KEY-SECRET"], webhook_key_hex="WEBHOOK-SECRET",
                    subscription_key_hex="SUB-SECRET", yookassa_shop_id="merchant", yookassa_secret_key="MERCHANT-SECRET")
    settings.update(changes)
    return json.dumps({"runtime": settings, "staging_addon_catalog": None}).encode()


class PrivatePilotUpgradeTests(unittest.TestCase):
    def test_default_plan_reads_nothing_and_does_not_import_helper(self):
        output = io.StringIO()
        with patch.object(ops, "read_regular") as read, patch.object(ops, "command") as command, \
             patch.object(ops, "load_installer") as load, contextlib.redirect_stdout(output):
            self.assertEqual(ops.main(["--archive", "/never/read", "--sha256", "DO-NOT-PRINT"]), 0)
        read.assert_not_called(); command.assert_not_called(); load.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertNotIn("DO-NOT-PRINT", output.getvalue())

    def test_helper_is_authenticated_before_any_execution(self):
        with patch.object(ops, "read_regular", return_value=b"raise RuntimeError('MALICIOUS')"), \
             self.assertRaises(ops.UpgradeError) as error:
            ops.load_installer()
        self.assertNotIn("MALICIOUS", str(error.exception))
        self.assertEqual(hashlib.sha256((ROOT / "ops/partner-platform/install_staging.py").read_bytes()).hexdigest(), ops.HELPER_SHA256)

    def test_candidate_changes_only_two_flags_preserving_all_values(self):
        raw = config(future_secret={"nested": "KEEP"})
        before, after = json.loads(raw), json.loads(ops.candidate(raw))
        for name in ops.FEATURES: before["runtime"][name] = True
        self.assertEqual(before, after)
        self.assertEqual(after["runtime"]["future_secret"], {"nested": "KEEP"})
        ops.validate_config(ops.candidate(raw), features=True)
        with self.assertRaises(ops.UpgradeError): ops.candidate(ops.candidate(raw))

    def test_source_scope_money_guards_and_boolean_types_fail_closed(self):
        ops.validate_config(config())
        changes = [{name: False} for name in ops.ON_FLAGS]
        changes += [{name: True} for name in ops.OFF_FLAGS]
        changes += [{name: value} for name in ops.FEATURES for value in (True, 0, None, "false")]
        changes += [{"operator_ids": [1]}, {"manager_allowed_user_ids": [ops.OWNER, 1]},
                    {"admin_recipient_id": 1}, {"mode": "production"}, {"database_project_ref": "MAIN"},
                    {"manager_origin": "https://elsewhere.example"}, {"remnawave_api_token": "NO"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ops.UpgradeError):
                ops.validate_config(config(**change))
        with self.assertRaises(ops.UpgradeError): ops.validate_config(b'{"runtime":{},"runtime":{}}')

    def test_explicit_lowercase_archive_and_backup_digest_required(self):
        self.assertEqual(ops.release_for("a" * 64).name, "20261022-" + "a" * 12)
        for value in (None, "a" * 63, "A" * 64, "../" + "a" * 64):
            with self.subTest(value=value), self.assertRaises(ops.UpgradeError): ops.release_for(value)
        raw = json.dumps({"format": 1, "project_ref": ops.PROJECT,
                          "tables": {"wl_" + chr(97 + i): [] for i in range(26)}}).encode()
        with patch.object(ops, "read_regular", return_value=raw):
            with self.assertRaises(ops.UpgradeError): ops.data_backup(hashlib.sha256(raw).hexdigest())
            with self.assertRaises(ops.UpgradeError): ops.data_backup("0" * 64)

    def test_new_unit_only_changes_release_path(self):
        with patch.object(ops, "installer", helper):
            for kind in ("http", "worker"):
                old, new = ops.OLD_RELEASE, ops.release_for("b" * 64)
                rendered = ops.render_unit(new, kind)
                self.assertEqual(rendered, ops.render_unit(old, kind).replace(str(old).encode(), str(new).encode()))
                self.assertIn(b"LoadCredential=staging.json:/etc/spn-partner-private/staging.json", rendered)
                self.assertIn(b"ProtectSystem=strict", rendered)

    def test_bad_archive_cannot_extract_or_stop_worker(self):
        with patch.object(ops, "installer", helper), patch.object(ops, "preflight", return_value=(100, config())), \
             patch.object(ops, "unused"), patch.object(ops, "read_regular", return_value=make_archive()), \
             patch.object(helper, "extract_release") as extract, patch.object(ops, "command") as command:
            with self.assertRaises(helper.InstallError):
                ops.upgrade(Path("/root/archive"), "0" * 64, ops.TARGET, "1" * 64)
        extract.assert_not_called(); command.assert_not_called()

    def test_failed_code_upgrade_restores_exact_units_and_active_worker(self):
        blob = make_archive(); sha = hashlib.sha256(blob).hexdigest(); raw = config()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); releases, private, units = [root / name for name in ("releases", "private", "units")]
            for path in (releases, private, units): path.mkdir()
            master = private / "staging.json"; master.write_bytes(raw)
            archive = root / "archive"; archive.write_bytes(blob)
            with patch.object(ops, "installer", helper):
                old = {name: ops.render_unit(ops.OLD_RELEASE, kind) for name, kind in zip(ops.UNITS, ("http", "worker"))}
            for name, value in old.items(): (units / name).write_bytes(value)
            commands = []
            def run(args, **kwargs): commands.append(args); return ""
            def extract(files, gid): helper.RELEASE.mkdir()
            def write(path, value, **kwargs): path.write_bytes(value)
            with patch.object(ops, "installer", helper), patch.object(ops, "RELEASES", releases), \
                 patch.object(ops, "PRIVATE", private), patch.object(ops, "UNIT_DIRECTORY", units), patch.object(ops, "CONFIG", master), \
                 patch.object(ops, "preflight", return_value=(100, raw)), patch.object(ops, "runtime"), \
                 patch.object(ops, "service_state"), patch.object(ops, "command", side_effect=run), \
                 patch.object(ops, "http_ready", side_effect=[ops.UpgradeError("readiness"), None]), \
                 patch.object(ops, "read_regular", side_effect=lambda path, **kw: path.read_bytes()), \
                 patch.object(ops, "atomic", side_effect=lambda path, value, *args: path.write_bytes(value)), \
                 patch.object(helper, "write_exclusive", side_effect=write), patch.object(helper, "extract_release", side_effect=extract), \
                 patch.object(helper, "protect_release"), patch.object(ops.os, "chown"):
                with self.assertRaises(ops.UpgradeError): ops.upgrade(archive, sha, ops.TARGET, "1" * 64)
                self.assertTrue(ops.release_for(sha).is_dir())
            self.assertEqual(master.read_bytes(), raw)
            self.assertEqual({name: (units / name).read_bytes() for name in ops.UNITS}, old)
            self.assertEqual(commands[-1], ["/usr/bin/systemctl", "start", ops.UNITS[1]])
            self.assertFalse(any("enable" in item or "disable" in item for item in commands))
            self.assertEqual((private / ("upgrade-private-before-" + sha[:12]) / "staging.json").read_bytes(), raw)

    def feature_case(self, *, failure=None, existing_backup=False):
        raw, events = config(), []
        with tempfile.TemporaryDirectory() as temp:
            private = Path(temp); master = private / "staging.json"; master.write_bytes(raw)
            pending, backup = private / "pending.json", private / "before.json"
            if existing_backup: backup.write_bytes(b"KEEP-ORIGINAL")
            def runtime(release, path, action):
                events.append((action, path, master.read_bytes()))
                if action == "--bootstrap-manager" and (failure == "rollback" or (path == pending and failure == "bootstrap")):
                    raise ops.UpgradeError("accepted-but-response-lost")
            def run(args, **kwargs): events.append(("command", args)); return ""
            with patch.object(ops, "preflight", return_value=(100, raw)), patch.object(ops, "CONFIG", master), \
                 patch.object(ops, "PENDING", pending), patch.object(ops, "FEATURE_BACKUP", backup), \
                 patch.object(ops, "read_regular", side_effect=lambda path, **kw: path.read_bytes()), \
                 patch.object(ops, "write_new", side_effect=lambda path, value: path.write_bytes(value)), \
                 patch.object(ops, "atomic", side_effect=lambda path, value: path.write_bytes(value)), \
                 patch.object(ops, "runtime", side_effect=runtime), patch.object(ops, "manager_capability"), \
                 patch.object(ops, "service_state"), patch.object(ops, "command", side_effect=run), \
                 patch.object(ops, "http_ready", side_effect=[ops.UpgradeError("http"), None] if failure == "http" else None):
                if failure or existing_backup:
                    with self.assertRaises(ops.UpgradeError): ops.enable_features("b" * 64, ops.TARGET, "1" * 64)
                else:
                    result = ops.enable_features("b" * 64, ops.TARGET, "1" * 64)
                    self.assertEqual(result["features_enabled"], list(ops.FEATURES))
            self.assertEqual(master.read_bytes(), ops.candidate(raw) if not (failure or existing_backup) else raw)
            self.assertEqual(backup.read_bytes(), b"KEEP-ORIGINAL" if existing_backup else raw)
            return events, pending, master

    def test_features_success_pauses_worker_before_bootstrap_and_preserves_keys(self):
        events, pending, master = self.feature_case()
        bootstrap = [row for row in events if row[0] == "--bootstrap-manager"]
        self.assertEqual(len(bootstrap), 1); self.assertEqual(bootstrap[0][1], pending)
        self.assertEqual(bootstrap[0][2], config())
        stop = next(i for i, row in enumerate(events) if row[0] == "command" and "stop" in row[1])
        start = next(i for i, row in enumerate(events) if row[0] == "command" and "start" in row[1])
        boot = events.index(bootstrap[0]); self.assertLess(stop, boot); self.assertLess(boot, start)

    def test_lost_bootstrap_response_and_failed_http_restore_flags_and_allowed_updates(self):
        for failure in ("bootstrap", "http"):
            with self.subTest(failure=failure):
                events, pending, master = self.feature_case(failure=failure)
                calls = [row for row in events if row[0] == "--bootstrap-manager"]
                self.assertEqual([row[1] for row in calls], [pending, master])
                self.assertEqual(calls[-1][2], config())
                self.assertEqual(events[-1], ("command", ["/usr/bin/systemctl", "start", ops.UNITS[1]]))

    def test_existing_feature_backup_never_overwritten_or_used_implicitly(self):
        events, _, _ = self.feature_case(existing_backup=True)
        self.assertEqual(events, [])

    def test_failed_remote_rollback_still_restarts_http_with_old_flags_but_keeps_worker_stopped(self):
        events, _, master = self.feature_case(failure="rollback")
        self.assertEqual(events[-1], ("command", ["/usr/bin/systemctl", "restart", ops.UNITS[0]]))
        self.assertFalse(any(row[0] == "command" and "start" in row[1] for row in events))
        restored = [row for row in events if row[0] == "--bootstrap-manager" and row[1] == master]
        self.assertEqual(restored[0][2], config())

    def test_cli_failures_do_not_echo_private_values(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error), patch.object(ops, "enable_features", side_effect=RuntimeError("TOKEN-SECRET")):
            self.assertEqual(ops.main(["--enable-new-features", "--sha256", "b" * 64]), 2)
            self.assertEqual(ops.main(["--mistyped", "TOKEN-SECRET"]), 2)
        self.assertNotIn("TOKEN-SECRET", error.getvalue())


if __name__ == "__main__":
    unittest.main()
