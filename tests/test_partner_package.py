"""Local allowlist packaging only: never SSH, install, connect or start a service."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import partner_package as package


class PartnerPackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.source = self.folder / "source"
        self.source.mkdir()
        for name in package.PACKAGE_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package.ROOT / name, target)
        self.output = self.folder / "staging.tar.gz"

    def build(self, **kwargs):
        return package.build_package(self.output, source_root=self.source, **kwargs)

    def test_exact_regular_file_manifest_and_content_hashes(self):
        self.assertEqual(self.build(), self.output)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        prefix = package.ARCHIVE_ROOT + "/"
        with tarfile.open(self.output, "r:gz") as archive:
            names = archive.getnames()
            self.assertEqual(
                set(names),
                {prefix + name for name in package.PACKAGE_FILES}
                | {prefix + package.MANIFEST_NAME},
            )
            self.assertEqual(len(names), len(set(names)))
            manifest = json.load(archive.extractfile(prefix + package.MANIFEST_NAME))
            self.assertFalse(manifest["contains_credentials"])
            self.assertEqual(manifest["runtime_default"], "disabled")
            self.assertEqual(set(manifest["files"]), set(package.PACKAGE_FILES))
            for info in archive.getmembers():
                self.assertTrue(info.isfile())
                self.assertFalse(info.issym() or info.islnk())
                self.assertTrue(info.name.startswith(prefix))
                self.assertFalse(PurePosixPath(info.name).is_absolute())
                self.assertNotIn("..", PurePosixPath(info.name).parts)
                self.assertEqual((info.uid, info.gid, info.mtime), (0, 0, 0))
                self.assertEqual((info.uname, info.gname, info.mode), ("", "", 0o644))
                if info.name == prefix + package.MANIFEST_NAME:
                    continue
                data = archive.extractfile(info).read()
                expected = manifest["files"][info.name.removeprefix(prefix)]
                self.assertEqual(len(data), expected["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected["sha256"])
                self.assertEqual(data, (self.source / info.name.removeprefix(prefix)).read_bytes())
                self.assertNotIn(str(package.ROOT).encode(), data)

    def test_secrets_configs_main_bot_and_other_ops_never_discovered(self):
        marker = b"PRIVATE-INPUT-MUST-NOT-SHIP-773862"
        excluded = (
            ".env", ".env.example", ".git/config", ".release-secrets/staging.json",
            "runtime.staging.json", "ops/partner-platform/staging.json",
            "ops/partner-platform/runtime.private.json", "ops/partner-platform/id_rsa",
            "ops/partner-platform/new-unreviewed.md", "services/__pycache__/leak.pyc",
            "config.py", "database.py", "main.py", "services/partner_addons.py",
            "ops/de1-xhttp-cdn/secret.conf", "release/secret.key", "data.sqlite",
        )
        for name in excluded:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(marker)
        self.build()
        with tarfile.open(self.output, "r:gz") as archive:
            for member in archive:
                self.assertNotIn(marker, archive.extractfile(member).read())
                self.assertNotIn(member.name.removeprefix(package.ARCHIVE_ROOT + "/"), excluded)

    def test_json_templates_are_only_disabled_public_examples(self):
        self.build()
        with tarfile.open(self.output, "r:gz") as archive:
            configs = [item for item in archive if item.name.endswith(".json")
                       and not item.name.endswith(package.MANIFEST_NAME)]
            self.assertEqual(len(configs), 2)
            for info in configs:
                self.assertTrue(info.name.endswith(".example.json"))
                runtime = json.load(archive.extractfile(info))["runtime"]
                self.assertEqual(runtime["mode"], "disabled")
                self.assertFalse(runtime["allow_staging_network"])
                self.assertFalse(runtime["allow_staging_writes"])
                self.assertEqual(runtime["manager_bot_token"], "")
                self.assertEqual(runtime["encryption_keys"], [])

    def test_archive_is_deterministic(self):
        self.build()
        second = self.folder / "second.tar.gz"
        package.build_package(second, source_root=self.source)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())

    def test_example_cannot_be_repurposed_as_enabled_or_secret_input_config(self):
        target = self.source / "ops/partner-platform/runtime.example.json"
        original = json.loads(target.read_bytes())
        for changes in (
            {"mode": "staging"}, {"allow_staging_network": True},
            {"enable_billing": True}, {"manager_bot_token": "short-secret"},
            {"encryption_keys": ["short-secret"]},
        ):
            config = {"runtime": {**original["runtime"], **changes},
                      "staging_addon_catalog": None}
            target.write_text(json.dumps(config))
            with self.subTest(fields=list(changes)), self.assertRaises(package.PackageError):
                self.build()
            self.assertFalse(self.output.exists())

    def test_path_traversal_absolute_hidden_and_private_config_names_rejected(self):
        invalid = (
            "../outside.py", "/etc/passwd", "services/../../.env",
            "services//file.py", "./partner_web.py", "services\\file.py",
            ".env", ".env.example", ".release-secrets/a.json", ".git/config",
            "services/__pycache__/file.pyc", "ops/partner-platform/staging.json",
            "ops/partner-platform/private.key", "services/a\n.py",
        )
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(package.PackageError):
                package.member_name(name)

    def test_manifest_traversal_and_duplicates_rejected_before_output(self):
        for names in (("../outside.py",), ("partner_web.py", "partner_web.py")):
            with self.subTest(names=names), patch.object(package, "PACKAGE_FILES", names):
                with self.assertRaises(package.PackageError):
                    self.build()
                self.assertFalse(self.output.exists())

    def test_missing_source_is_not_silently_omitted(self):
        (self.source / "partner_web.py").unlink()
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_file_directory_and_root_symlinks_are_refused(self):
        target = self.source / "partner_web.py"
        target.unlink()
        target.symlink_to(package.ROOT / "partner_web.py")
        with self.assertRaises(package.PackageError):
            self.build()
        target.unlink()
        shutil.copyfile(package.ROOT / "partner_web.py", target)
        assets = self.source / "static/partners/assets"
        shutil.rmtree(assets)
        assets.symlink_to(package.ROOT / "static/partners/assets", target_is_directory=True)
        with self.assertRaises(package.PackageError):
            self.build()
        root_link = self.folder / "root-link"
        root_link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(package.PackageError):
            package.build_package(self.output, source_root=root_link)
        self.assertFalse(self.output.exists())

    def test_hardlinks_and_non_regular_sources_are_refused(self):
        target = self.source / "partner_web.py"
        target.unlink()
        outside = self.folder / "outside.py"
        outside.write_text("not a credential")
        os.link(outside, target)
        with self.assertRaises(package.PackageError):
            self.build()
        target.unlink()
        os.mkfifo(target)
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_detectable_embedded_credentials_refused_without_echo(self):
        target = self.source / "partner_web.py"
        original = target.read_bytes()
        values = (
            b"-----BEGIN " + b"PRIVATE KEY-----\nartificial-key\n",
            b"123456789:" + b"T" * 35,
            b"sb_secret_" + b"x" * 32,
            b"postgresql://owner:artificial-sensitive-password@db.example.com/staging",
        )
        for value in values:
            with self.subTest(kind=value[:8]):
                target.write_bytes(original + b"\n# " + value)
                with self.assertRaises(package.PackageError) as error:
                    self.build()
                self.assertNotIn(value.decode(), str(error.exception))
                self.assertFalse(self.output.exists())
        target.write_bytes(original)

    def test_existing_or_symlink_output_never_overwritten(self):
        self.output.write_bytes(b"existing")
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertEqual(self.output.read_bytes(), b"existing")
        self.output.unlink()
        existing = self.folder / "existing.tar.gz"
        existing.write_bytes(b"original")
        self.output.symlink_to(existing)
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertEqual(existing.read_bytes(), b"original")
        for name in ("/etc/staging.tar.gz", str(self.folder / "bad.zip")):
            with self.assertRaises(package.PackageError):
                package.output_path(name)

    def test_partial_archive_failure_removes_only_new_output(self):
        with patch.object(package.tarfile.TarFile, "addfile", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self.build()
        self.assertFalse(self.output.exists())
        self.assertTrue((self.source / "partner_web.py").is_file())

    def test_extracted_entrypoints_run_offline_without_input_configuration(self):
        self.build()
        destination = self.folder / "unpacked"
        with tarfile.open(self.output, "r:gz") as archive:
            archive.extractall(destination, filter="data")
        root = destination / package.ARCHIVE_ROOT
        self.assertFalse((root / "config.py").exists())
        self.assertFalse((root / "database.py").exists())
        self.assertFalse((root / ".env").exists())
        # A misleading env file at destination must also remain undiscovered.
        (root / ".env").write_text("PARTNER_PLATFORM_ENABLED=true\nINVALID_SECRET=input")
        commands = (
            [sys.executable, "-I", "scripts/partner_runtime.py", "--check"],
            [sys.executable, "-I", "scripts/partner_setup.py", "--help"],
        )
        for command in commands:
            result = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=15,
                env={**os.environ, "PYTHONPATH": "", "DATABASE_URL": "INVALID_SECRET"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("INVALID_SECRET", result.stdout + result.stderr)
            if "--check" in command:
                report = json.loads(result.stdout)
                self.assertEqual(report["mode"], "disabled")
                self.assertFalse(report["production_ready"])
                self.assertIn("runtime_disabled", report["blockers"])
            else:
                self.assertIn("--config", result.stdout)

    @unittest.skipUnless(
        all(importlib.util.find_spec(name) for name in ("asyncpg", "fastapi")),
        "Optional full-import audit requires installed staging dependencies",
    )
    def test_shipped_python_modules_import_without_checkout_or_main_spn(self):
        self.build()
        destination = self.folder / "import-audit"
        with tarfile.open(self.output, "r:gz") as archive:
            archive.extractall(destination, filter="data")
        root = destination / package.ARCHIVE_ROOT
        modules = [name[:-3].replace("/", ".") for name in package.PACKAGE_FILES
                   if name.endswith(".py") and not name.startswith("scripts/")]
        code = (
            "import importlib,json,sys; sys.path.insert(0,sys.argv[1]); "
            "[importlib.import_module(name) for name in json.loads(sys.argv[2])]; "
            "assert 'config' not in sys.modules; assert 'database' not in sys.modules; "
            "assert 'dotenv' not in sys.modules; print('standalone_imports_ok')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(root), json.dumps(modules)],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "standalone_imports_ok")

    def test_cli_prints_metadata_only(self):
        output = io.StringIO()
        with patch.object(package, "ROOT", self.source), contextlib.redirect_stdout(output):
            # build_package's explicit source_root default stays the real source
            # checkout; this command still has no runtime or secret discovery.
            self.assertEqual(package.main(["--output", str(self.output)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"archive", "sha256", "source_files"})
        self.assertEqual(result["source_files"], len(package.PACKAGE_FILES))
        self.assertEqual(result["sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
