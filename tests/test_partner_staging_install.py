"""Installer validation without privileges, network, systemd, pip or remote calls."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "ops/partner-platform/install_staging.py"
spec = importlib.util.spec_from_file_location("partner_staging_installer", SOURCE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def make_archive(*, extra=(), changed_manifest=None, duplicate=False):
    files = {
        "scripts/partner_runtime.py": b"# runtime placeholder\n",
        "scripts/partner_setup.py": b"# setup placeholder\n",
        installer.REQUIREMENTS: b"aiohttp>=3.8.5,<4\n",
    }
    manifest = {
        "format": 1, "archive_root": installer.ARCHIVE_ROOT,
        "runtime_default": "disabled", "contains_credentials": False,
        "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in files.items()},
    }
    if changed_manifest:
        changed_manifest(manifest)
    files["PACKAGE_MANIFEST.json"] = json.dumps(manifest).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(installer.ARCHIVE_ROOT + "/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            if duplicate and name.startswith("scripts/"):
                archive.addfile(info, io.BytesIO(data))
        for info, data in extra:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return stream.getvalue()


def validate(blob):
    return installer.validate_archive(blob, expected_sha256=hashlib.sha256(blob).hexdigest())


class StagingInstallerTests(unittest.TestCase):
    def test_validated_payload_matches_manifest(self):
        files = validate(make_archive())
        self.assertEqual(set(files), {
            "scripts/partner_runtime.py", "scripts/partner_setup.py",
            installer.REQUIREMENTS, "PACKAGE_MANIFEST.json",
        })

    def test_pinned_release_hash_cannot_be_bypassed_by_a_valid_manifest(self):
        with self.assertRaises(installer.InstallError):
            installer.validate_archive(make_archive())

    def test_traversal_links_hidden_config_and_main_spn_refused(self):
        paths = (
            "/etc/passwd", "../outside", installer.ARCHIVE_ROOT + "/../../outside",
            installer.ARCHIVE_ROOT + "/.env", installer.ARCHIVE_ROOT + "/.git/config",
            installer.ARCHIVE_ROOT + "/ops/staging.json", installer.ARCHIVE_ROOT + "/config.py",
            installer.ARCHIVE_ROOT + "/database.py", installer.ARCHIVE_ROOT + "/secret.pem",
            installer.ARCHIVE_ROOT + "/a\\b", installer.ARCHIVE_ROOT + "//a.py",
        )
        for path in paths:
            info = tarfile.TarInfo(path)
            info.size = 1
            with self.subTest(path=path), self.assertRaises(installer.InstallError):
                validate(make_archive(extra=[(info, b"x")]))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE):
            info = tarfile.TarInfo(installer.ARCHIVE_ROOT + "/services/unsafe.py")
            info.type = kind
            info.linkname = "/etc/passwd"
            with self.subTest(kind=kind), self.assertRaises(installer.InstallError):
                validate(make_archive(extra=[(info, None)]))

    def test_duplicate_and_manifest_digest_mismatch_refused(self):
        with self.assertRaises(installer.InstallError):
            validate(make_archive(duplicate=True))
        for modify in (
            lambda item: item.update(runtime_default="staging"),
            lambda item: item.update(contains_credentials=True),
            lambda item: item["files"]["scripts/partner_runtime.py"].update(sha256="0" * 64),
            lambda item: item["files"].pop("scripts/partner_setup.py"),
        ):
            with self.assertRaises(installer.InstallError):
                validate(make_archive(changed_manifest=modify))

    def test_approved_actual_archive_validates_when_available(self):
        archive = Path("/private/tmp/spn-partner-staging-20261021.tar.gz")
        if not archive.is_file():
            self.skipTest("Pinned deployment archive is a separate local artifact")
        files = installer.validate_archive(archive.read_bytes())
        self.assertEqual(len(files), 77)
        self.assertNotIn("config.py", files)
        self.assertNotIn("database.py", files)

    def test_default_plan_does_not_execute_or_read_archive(self):
        output = io.StringIO()
        with patch.object(installer, "install") as install, patch.object(
            installer.subprocess, "run"
        ) as command, contextlib.redirect_stdout(output):
            self.assertEqual(installer.main([]), 0)
        install.assert_not_called()
        command.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertTrue(plan["plan_only"])
        self.assertEqual(plan["services_after_install"], "disabled_and_inactive")
        self.assertTrue(plan["no_cleanup"])

    def test_units_use_private_delivery_owned_copy_and_loopback_runtime(self):
        for kind in ("http", "worker"):
            unit = installer.render_unit(kind)
            self.assertIn("User=spn-partner\nGroup=spn-partner", unit)
            self.assertIn("LoadCredential=staging.json:/etc/spn-partner-private/staging.json", unit)
            self.assertIn("RuntimeDirectoryMode=0700", unit)
            self.assertIn("RuntimeDirectoryPreserve=no", unit)
            self.assertIn("--mode=0600 --no-target-directory ${CREDENTIALS_DIRECTORY}/staging.json", unit)
            self.assertIn(f"--config /run/spn-partner-{kind}/staging.json", unit)
            self.assertIn("ConditionPathExists=/etc/spn-partner-private/staging.json", unit)
            self.assertIn("ProtectSystem=strict", unit)
            self.assertIn("StandardOutput=null\nStandardError=null", unit)
            self.assertNotIn("EnvironmentFile", unit)
            self.assertNotIn("+", unit)
            if kind == "http":
                self.assertIn("--serve --port 8966", unit)
            else:
                self.assertIn("--worker", unit)

    def test_command_environment_ignores_pip_configuration_and_does_not_echo_error(self):
        result = subprocess.CompletedProcess([], 1, b"private stdout", b"private stderr")
        with patch.object(installer.subprocess, "run", return_value=result) as run:
            with self.assertRaises(installer.InstallError) as error:
                installer.checked_command(["example-command"])
        self.assertNotIn("private", str(error.exception))
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["PIP_CONFIG_FILE"], "/dev/null")
        self.assertNotIn("PIP_EXTRA_INDEX_URL", environment)
        self.assertNotIn("DATABASE_URL", environment)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_target_and_os_guard_precedes_all_mutation(self):
        with patch.object(installer.sys, "platform", "linux"), patch.object(
            installer.os, "geteuid", return_value=0
        ), patch.object(installer.pwd, "getpwnam") as account:
            with self.assertRaises(installer.InstallError):
                installer.preflight("not-the-approved-server")
        account.assert_not_called()

    def test_residual_direct_inherited_and_generic_dropins_are_rejected(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            search = Path(temporary)
            with patch.object(installer, "UNIT_SEARCH", (search,)):
                installer.no_existing_unit_or_override("spn-partner-http.service")
                for name in ("spn-partner-http.service.d", "spn-partner-.service.d",
                             "spn-.service.d", "service.d"):
                    path = search / name
                    path.mkdir()
                    with self.subTest(name=name), self.assertRaises(installer.InstallError):
                        installer.no_existing_unit_or_override("spn-partner-http.service")
                    path.rmdir()

    def test_exclusive_failed_write_removes_only_its_new_file(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            path = Path(temporary) / "new.service"
            with patch.object(installer.os, "fchown"), patch.object(
                installer.os, "fsync", side_effect=OSError("simulated")
            ):
                with self.assertRaises(OSError):
                    installer.write_exclusive(path, b"new", mode=0o600, gid=20)
            self.assertFalse(path.exists())
            path.write_bytes(b"pre-existing")
            with self.assertRaises(FileExistsError):
                installer.write_exclusive(path, b"replacement", mode=0o600, gid=20)
            self.assertEqual(path.read_bytes(), b"pre-existing")

    def test_mocked_install_never_enables_starts_migrates_or_installs_nginx(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            folder = Path(temporary)
            release, units = folder / "release", folder / "units"
            units.mkdir()
            archive = folder / "package.tar.gz"
            archive.write_bytes(b"not read as real deployment bytes")
            files = {
                "scripts/partner_runtime.py": b"# runtime\n",
                "scripts/partner_setup.py": b"# setup\n",
                installer.REQUIREMENTS: b"aiohttp>=3.8.5,<4\n",
            }
            commands = []

            def extract(payload, gid):
                release.mkdir()
                for name, data in payload.items():
                    path = release / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)

            def command(argv, **kwargs):
                commands.append(argv)
                if "scripts/partner_runtime.py" in argv:
                    return json.dumps({"mode": "disabled", "production_ready": False,
                                       "blockers": ["runtime_disabled"]})
                if "is-enabled" in argv:
                    return "disabled"
                if "is-active" in argv:
                    return "inactive"
                return ""

            metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0,
                                       st_nlink=1, st_size=archive.stat().st_size)
            with patch.object(installer, "RELEASE", release), patch.object(
                installer, "UNIT_DIRECTORY", units
            ), patch.object(installer, "preflight", return_value=(501, 20)), patch.object(
                installer, "validate_archive", return_value=files
            ), patch.object(installer.os, "fstat", return_value=metadata), patch.object(
                installer, "extract_release", side_effect=extract
            ), patch.object(installer, "protect_release"), patch.object(
                installer, "write_exclusive", side_effect=lambda path, data, **kwargs: path.write_bytes(data)
            ), patch.object(installer.os, "chown"), patch.object(
                installer.os, "umask"
            ), patch.object(installer, "checked_command", side_effect=command), contextlib.redirect_stdout(io.StringIO()):
                installer.install(archive, installer.TARGET)
            self.assertEqual(set(path.name for path in units.iterdir()), set(installer.UNITS))
            for argv in commands:
                self.assertNotIn("enable", argv)
                self.assertNotIn("start", argv)
                self.assertNotIn("restart", argv)
                self.assertNotIn("nginx", argv)
                self.assertNotIn("psql", argv)
            pip = next(argv for argv in commands if "install" in argv)
            self.assertIn("https://pypi.org/simple", pip)
            self.assertIn("--only-binary=:all:", pip)
            self.assertIn("--require-virtualenv", pip)


if __name__ == "__main__":
    unittest.main()
