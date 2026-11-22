"""Offline upgrade guards and rollback. No root, network, service or secret I/O."""
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


SOURCE = Path(__file__).resolve().parents[1] / "ops/partner-platform/upgrade_staging.py"
spec = importlib.util.spec_from_file_location("partner_staging_upgrade", SOURCE)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def private_config(**changes):
    return json.dumps({"runtime": {"mode": "staging", "enable_http": True,
        "enable_manager_webhook": True, "manager_allowed_user_ids": [upgrade.OWNER], **changes},
        "staging_addon_catalog": None}).encode()


class StagingUpgradeTests(unittest.TestCase):
    def test_default_plan_is_pure_even_with_supplied_untrusted_paths(self):
        output = io.StringIO()
        with patch.object(upgrade, "upgrade") as perform, patch.object(upgrade, "read_regular") as read, \
             patch.object(upgrade, "command") as command, contextlib.redirect_stdout(output):
            self.assertEqual(upgrade.main(["--archive", "/never/read", "--sha256", "secret"]), 0)
        perform.assert_not_called()
        read.assert_not_called()
        command.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])

    def test_digest_and_fixed_release_format(self):
        digest = "a" * 64
        self.assertEqual(upgrade.release_for(digest).name, "20261022-" + digest[:12])
        for value in (None, "a" * 63, "A" * 64, "../" + "a" * 64, 1):
            with self.subTest(value=value), self.assertRaises(upgrade.UpgradeError):
                upgrade.release_for(value)

    def test_existing_closed_configuration_only(self):
        upgrade.validate_config(private_config())
        for field in upgrade.OFF_FLAGS:
            for value in (True, 1, "false", None):
                with self.subTest(field=field, value=value), self.assertRaises(upgrade.UpgradeError):
                    upgrade.validate_config(private_config(**{field: value}))
        for values in ({"enable_http": False}, {"enable_manager_webhook": False},
                       {"manager_allowed_user_ids": []}, {"manager_allowed_user_ids": [1]},
                       {"manager_allowed_user_ids": [upgrade.OWNER, 1]}, {"mode": "live"}):
            with self.subTest(values=values), self.assertRaises(upgrade.UpgradeError):
                upgrade.validate_config(private_config(**values))
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.validate_config(b'{"runtime":{},"runtime":{}}')

    def test_new_unit_changes_only_release_paths(self):
        old = upgrade.OLD_RELEASE
        previous = upgrade.installer.RELEASE
        new = upgrade.release_for("b" * 64)
        for kind in ("http", "worker"):
            original = upgrade.render_unit(old, kind)
            rendered = upgrade.render_unit(new, kind)
            self.assertEqual(rendered, original.replace(str(old).encode(), str(new).encode()))
            self.assertIn(b"LoadCredential=staging.json:/etc/spn-partner-private/staging.json", rendered)
            self.assertIn(b"ProtectSystem=strict", rendered)
            self.assertIn(b"StandardOutput=null\nStandardError=null", rendered)
        self.assertEqual(upgrade.installer.RELEASE, previous)

    def test_config_hash_is_rechecked_and_values_never_echoed(self):
        raw = private_config()
        digest = hashlib.sha256(raw).hexdigest()
        with patch.object(upgrade, "read_regular", return_value=raw):
            upgrade.unchanged_config(digest)
        with patch.object(upgrade, "read_regular", return_value=private_config(future_secret="DO-NOT-PRINT")):
            with self.assertRaises(upgrade.UpgradeError) as error:
                upgrade.unchanged_config(digest)
        self.assertNotIn("DO-NOT-PRINT", str(error.exception))

    def test_worker_state_must_be_disabled_and_inactive(self):
        def command(args, **kwargs):
            if "DropInPaths" in " ".join(args): return ""
            if "FragmentPath" in " ".join(args): return str(upgrade.UNIT_DIRECTORY / args[-1])
            if "is-active" in args: return "active" if args[-1] == upgrade.UNITS[0] else "inactive"
            if "is-enabled" in args: return "enabled" if args[-1] == upgrade.UNITS[0] else "disabled"
            self.fail(args)
        with patch.object(upgrade, "command", side_effect=command):
            upgrade.unit_state()
        def bad(args, **kwargs):
            return "active" if "is-active" in args and args[-1] == upgrade.UNITS[1] else command(args, **kwargs)
        with patch.object(upgrade, "command", side_effect=bad), self.assertRaises(upgrade.UpgradeError):
            upgrade.unit_state()

    def test_wrong_archive_hash_stops_before_extract_pip_or_unit_changes(self):
        blob = make_archive()
        with patch.object(upgrade, "preflight", return_value=(100, "x")), \
             patch.object(upgrade, "read_regular", return_value=blob), \
             patch.object(upgrade.installer, "extract_release") as extract, \
             patch.object(upgrade, "command") as command:
            with self.assertRaises(upgrade.UpgradeError):
                upgrade.upgrade(Path("/root/payload.tgz"), "0" * 64, upgrade.TARGET)
        extract.assert_not_called()
        command.assert_not_called()

    def test_restore_units_never_starts_or_enables_worker(self):
        backups = {upgrade.PRIVATE / "backup" / name: name.encode() for name in upgrade.UNITS}
        with patch.object(upgrade, "read_regular", side_effect=lambda path, **kw: backups[path]), \
             patch.object(upgrade, "atomic_unit") as atomic, patch.object(upgrade, "command") as command:
            upgrade.restore_units(upgrade.PRIVATE / "backup")
        self.assertEqual(atomic.call_count, 2)
        self.assertEqual(command.call_args_list[0].args[0], ["/usr/bin/systemctl", "daemon-reload"])
        self.assertEqual(command.call_args_list[1].args[0], ["/usr/bin/systemctl", "restart", upgrade.UNITS[0]])

    def test_failed_http_restart_restores_both_old_units_and_keeps_release(self):
        blob = make_archive()
        digest = hashlib.sha256(blob).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            releases, private, units = (base / name for name in ("releases", "private", "units"))
            for folder in (releases, private, units): folder.mkdir()
            old = {name: upgrade.render_unit(upgrade.OLD_RELEASE, kind)
                   for name, kind in zip(upgrade.UNITS, ("http", "worker"))}
            for name, raw in old.items(): (units / name).write_bytes(raw)
            archive = base / "payload.tar.gz"
            archive.write_bytes(blob)
            restarts = []

            def extract(files, gid):
                upgrade.installer.RELEASE.mkdir()

            def write(path, data, **kwargs):
                path.write_bytes(data)

            def command(args, **kwargs):
                if "--check" in args:
                    return json.dumps({"mode": "staging" if "--config" in args else "disabled",
                                       "configured": True, "production_ready": False, "blockers": []})
                if args[:2] == ["/usr/bin/systemctl", "restart"]:
                    restarts.append(args[-1])
                    if len(restarts) == 1: raise upgrade.UpgradeError("restart failed")
                return ""

            with patch.object(upgrade.installer, "RELEASES", releases), \
                 patch.object(upgrade, "PRIVATE", private), patch.object(upgrade, "UNIT_DIRECTORY", units), \
                 patch.object(upgrade, "preflight", return_value=(100, "config-hash")), \
                 patch.object(upgrade, "read_regular", side_effect=lambda path, **kw: path.read_bytes()), \
                 patch.object(upgrade, "unchanged_config"), patch.object(upgrade, "unit_state"), \
                 patch.object(upgrade.installer, "extract_release", side_effect=extract), \
                 patch.object(upgrade.installer, "protect_release"), \
                 patch.object(upgrade.installer, "write_exclusive", side_effect=write), \
                 patch.object(upgrade.os, "chown"), patch.object(upgrade, "command", side_effect=command), \
                 patch.object(upgrade, "atomic_unit", side_effect=lambda path, data: path.write_bytes(data)):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.upgrade(archive, digest, upgrade.TARGET)
                self.assertTrue(upgrade.release_for(digest).is_dir())
            self.assertEqual(restarts, [upgrade.UNITS[0], upgrade.UNITS[0]])
            self.assertEqual({name: (units / name).read_bytes() for name in upgrade.UNITS}, old)
            backup = private / ("upgrade-before-20261022-" + digest[:12])
            self.assertEqual({name: (backup / name).read_bytes() for name in upgrade.UNITS}, old)

    def test_main_refuses_upgrade_without_archive_and_redacts_errors(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch.object(upgrade, "upgrade") as perform:
            self.assertEqual(upgrade.main(["--upgrade"]), 2)
        perform.assert_not_called()
        with patch.object(upgrade, "upgrade", side_effect=RuntimeError("PRIVATE-TOKEN")), contextlib.redirect_stderr(output):
            self.assertEqual(upgrade.main(["--upgrade", "--archive", "/root/file", "--sha256", "a" * 64]), 2)
        self.assertNotIn("PRIVATE-TOKEN", output.getvalue())


if __name__ == "__main__":
    unittest.main()
