"""Offline code-only deployment invariants. No root, credentials or network."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


ui = module("presentation_upgrade_test_subject", HERE / "upgrade_bot_presentation.py")
base = module("presentation_reviewed_ops", HERE / "upgrade_private_pilot.py")
installer = module("presentation_reviewed_installer", HERE / "install_staging.py")


def entry(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def manifest(files):
    return {"format": 1, "archive_root": "spn-partner-staging", "runtime_default": "disabled",
            "contains_credentials": False, "files": {name: entry(raw) for name, raw in files.items()}}


class PresentationUpgradeTests(unittest.TestCase):
    def test_default_is_pure_plan_even_with_untrusted_paths(self):
        output = io.StringIO()
        with patch.object(ui, "load_ops") as load, patch.object(ui, "read_protected") as read, \
             patch.object(ui, "upgrade") as run, contextlib.redirect_stdout(output):
            self.assertEqual(ui.main(["--archive", "/never/read", "--sha256", "SECRET"]), 0)
        load.assert_not_called(); read.assert_not_called(); run.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertNotIn("SECRET", output.getvalue())

    def test_known_helper_hash_before_execution_and_features_must_be_true(self):
        raw = (HERE / "upgrade_private_pilot.py").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ui.HELPER_SHA256)
        with patch.object(ui, "read_protected", return_value=b"raise Exception('SECRET')"), self.assertRaises(ui.UpgradeError):
            ui.load_ops()
        with patch.object(ui, "read_protected", return_value=raw):
            reviewed = ui.load_ops()
        self.assertEqual(reviewed.OLD_RELEASE, ui.OLD_RELEASE)
        with self.assertRaises(reviewed.UpgradeError):
            reviewed.validate_config(b'{"runtime":{},"staging_addon_catalog":null}')
        settings = {name: True for name in reviewed.ON_FLAGS}
        settings.update({name: False for name in reviewed.OFF_FLAGS})
        settings.update({name: True for name in reviewed.FEATURES})
        settings.update(mode="staging", manager_allowed_user_ids=[reviewed.OWNER], operator_ids=[reviewed.OWNER],
            admin_recipient_id=reviewed.OWNER, database_project_ref=reviewed.PROJECT, database_connection_mode="session",
            database_ca_file="/etc/spn-partner-ca/supabase.crt", base_domain="sub.wayspn.online",
            manager_bot_username="spn_franchise_bot", manager_origin="https://partners.wayspn.online",
            webhook_origin="https://partners.wayspn.online", manager_bot_token="KEEP", encryption_keys=["KEEP"],
            webhook_key_hex="KEEP", subscription_key_hex="KEEP", yookassa_shop_id="KEEP", yookassa_secret_key="KEEP",
            database_dsn="postgresql://spn_partner_runtime." + reviewed.PROJECT + ":KEEP@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres")
        value = {"runtime": settings, "staging_addon_catalog": None}
        reviewed.validate_config(json.dumps(value))
        for flag in reviewed.FEATURES:
            settings[flag] = False
            with self.assertRaises(reviewed.UpgradeError): reviewed.validate_config(json.dumps(value))
            settings[flag] = True
        # Strict adapter does not change the reviewed helper source/constructor.
        self.assertIsNone(reviewed.installer)
        reviewed.installer = installer
        with patch.object(reviewed.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"{}")) as run:
            reviewed.command(["/release/.venv/bin/python", "-I", "scripts/partner_runtime.py", "--check"])
        self.assertEqual(run.call_args.args[0][:3], ["/release/.venv/bin/python", "-B", "-I"])

    def test_digest_lowercase_and_new_immutable_date(self):
        self.assertEqual(ui.release_for("a" * 64).name, "20261023-" + "a" * 12)
        for value in (None, "A" * 64, "a" * 63, "../" + "a" * 64):
            with self.subTest(value=value), self.assertRaises(ui.UpgradeError): ui.release_for(value)

    def diff_case(self, *, change=None):
        before = {"services/partner_bot.py": b"old", installer.REQUIREMENTS: b"pinned unchanged", "services/__init__.py": b""}
        after = {**before, "services/partner_bot.py": b"new", ui.NEW_HELPER: b"pure instructions"}
        with tempfile.TemporaryDirectory() as temp:
            old = Path(temp)
            for name, raw in before.items():
                path = old / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
            (old / "PACKAGE_MANIFEST.json").write_text(json.dumps(manifest(before)))
            if change is not None: change(before, after, old)
            files = {**after, "PACKAGE_MANIFEST.json": json.dumps(manifest(after)).encode()}
            with patch.object(ui, "OLD_RELEASE", old), patch.object(ui, "read_protected", side_effect=lambda path, **kw: path.read_bytes()):
                return ui.presentation_diff(SimpleNamespace(document=base.document, installer=installer), files)

    def test_manifest_accepts_only_presentation_changes_and_one_existing_pure_helper(self):
        self.assertEqual(self.diff_case(), sorted({ui.NEW_HELPER, "services/partner_bot.py"}))
        for mutate in (
            lambda before, after, old: after.update({"services/partner_runtime.py": b"UNAUTHORIZED"}),
            lambda before, after, old: after.update({installer.REQUIREMENTS: b"dependency drift"}),
            lambda before, after, old: after.pop("services/__init__.py"),
            lambda before, after, old: after.update({"scripts/partner_package.py": b"not shipped"}),
            lambda before, after, old: (old / "services/partner_bot.py").write_bytes(b"tampered"),
        ):
            with self.subTest(mutate=mutate), self.assertRaises(ui.UpgradeError): self.diff_case(change=mutate)

    def test_virtualenv_copy_has_no_dependency_install_and_checks_exact_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp) / "new"
            commands = []
            def run(args, **kwargs):
                commands.append(args)
                if "-c" in args:
                    return json.dumps({"prefix": str(release / ".venv"), "executable": str(release / ".venv/bin/python")})
                return ""
            ops = SimpleNamespace(command=run, document=base.document, selected_release=lambda release: contextlib.nullcontext(),
                                  installer=SimpleNamespace(protect_release=Mock()))
            with patch.object(ui, "tree_signature", return_value={"bin/python": ("file", 1, "sha")}), \
                 patch.object(ui, "versions", side_effect=[{"pip": "24.0"}, {"pip": "24.0"}]), \
                 patch.object(ui.shutil, "copytree") as copy:
                ui.copy_environment(ops, release, 100)
            copy.assert_called_once_with(ui.OLD_RELEASE / ".venv", release / ".venv", symlinks=True)
            self.assertTrue(any("check" in args for args in commands))
            self.assertFalse(any("install" in args or "https://" in " ".join(args) for args in commands))
            with patch.object(ui, "tree_signature", return_value={"f": ("file",)}), \
                 patch.object(ui, "versions", side_effect=[{"pip": "24.0"}, {"pip": "25.0"}]), \
                 patch.object(ui.shutil, "copytree"), self.assertRaises(ui.UpgradeError):
                ui.copy_environment(ops, release, 100)

    def test_virtualenv_rejects_external_and_absolute_symlinks(self):
        original_lstat = Path.lstat
        def owned(path):
            actual = original_lstat(path)
            return SimpleNamespace(st_mode=actual.st_mode, st_uid=0)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "venv"; root.mkdir(); (root / "lib").mkdir()
            (root / "lib64").symlink_to("lib")
            with patch.object(Path, "lstat", owned):
                self.assertIn("lib64", ui.tree_signature(root))
                (root / "bad").symlink_to("/usr/bin/python3")
                with self.assertRaises(ui.UpgradeError): ui.tree_signature(root)
                (root / "bad").unlink(); (root / "bad").symlink_to("../outside")
                with self.assertRaises(ui.UpgradeError): ui.tree_signature(root)

    def test_read_protected_accepts_empty_code_but_rejects_hardlinks(self):
        original_fstat = os.fstat
        def owned(fd):
            actual = original_fstat(fd)
            values = {name: getattr(actual, name) for name in ("st_mode", "st_nlink", "st_dev", "st_ino", "st_size", "st_mtime_ns")}
            return SimpleNamespace(st_uid=0, **values)
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            path = Path(temp) / "empty"; path.write_bytes(b"")
            with patch.object(ui.os, "fstat", side_effect=owned):
                self.assertEqual(ui.read_protected(path), b"")
                os.link(path, path.with_name("linked"))
                with self.assertRaises(ui.UpgradeError): ui.read_protected(path)

    def test_failed_http_restores_units_and_services_without_touching_master(self):
        sha = "b" * 64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); private, releases, units = [root / name for name in ("private", "releases", "units")]
            for path in (private, releases, units): path.mkdir()
            master = private / "staging.json"; raw = b"EXACT-PRIVATE-MASTER"; master.write_bytes(raw)
            old = {name: ("old-" + name).encode() for name in base.UNITS}
            for name, value in old.items(): (units / name).write_bytes(value)
            commands, actions = [], []
            def run(args, **kwargs): commands.append(args); return ""
            def extract(files, gid): installer.RELEASE.mkdir()
            patches = [patch.object(ui, "RELEASES", releases), patch.object(ui, "PRIVATE", private),
                 patch.object(ui, "load_ops", return_value=base), patch.object(ui, "read_protected", return_value=b"archive"),
                 patch.object(ui, "presentation_diff", return_value=[ui.NEW_HELPER]), patch.object(ui, "copy_environment"),
                 patch.object(base, "installer", installer), patch.object(base, "CONFIG", master),
                 patch.object(base, "UNIT_DIRECTORY", units), patch.object(base, "preflight", return_value=(100, raw)),
                 patch.object(base, "runtime", side_effect=lambda release, config, action: actions.append(action)),
                 patch.object(base, "service_state"), patch.object(base, "command", side_effect=run),
                 patch.object(base, "http_ready", side_effect=[ui.UpgradeError("http"), None]),
                 patch.object(base, "read_regular", side_effect=lambda path, **kw: path.read_bytes()),
                 patch.object(base, "atomic", side_effect=lambda path, value, *args: path.write_bytes(value)),
                 patch.object(installer, "validate_archive", return_value={}),
                 patch.object(installer, "extract_release", side_effect=extract),
                 patch.object(installer, "write_exclusive", side_effect=lambda path, value, **kw: path.write_bytes(value)),
                 patch.object(ui.os, "chown")]
            with contextlib.ExitStack() as stack:
                for item in patches: stack.enter_context(item)
                with self.assertRaises(ui.UpgradeError): ui.upgrade(Path("/root/archive"), sha, ui.TARGET)
                self.assertTrue(ui.release_for(sha).is_dir())
            self.assertEqual(master.read_bytes(), raw)
            self.assertEqual({name: (units / name).read_bytes() for name in base.UNITS}, old)
            self.assertEqual(actions, ["--check", "--probe"])
            self.assertEqual(commands[-1], ["/usr/bin/systemctl", "start", base.UNITS[1]])
            self.assertFalse(any("enable" in args or "disable" in args for args in commands))
            self.assertEqual((private / ("presentation-before-20261023-" + sha[:12]) / "staging.json").read_bytes(), raw)

    def test_cli_redacts_errors_and_rejects_wrong_target_before_helper(self):
        error = io.StringIO()
        with patch.object(ui, "load_ops") as load, self.assertRaises(ui.UpgradeError):
            ui.upgrade(Path("/root/archive"), "b" * 64, "192.0.2.10")
        load.assert_not_called()
        with contextlib.redirect_stderr(error), patch.object(ui, "upgrade", side_effect=RuntimeError("TOKEN-SECRET")):
            self.assertEqual(ui.main(["--upgrade", "--archive", "/root/file", "--sha256", "b" * 64]), 2)
            self.assertEqual(ui.main(["--invalid", "TOKEN-SECRET"]), 2)
        self.assertNotIn("TOKEN-SECRET", error.getvalue())


if __name__ == "__main__":
    unittest.main()
