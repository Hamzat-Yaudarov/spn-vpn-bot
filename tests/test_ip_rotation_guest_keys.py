import base64
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "ops/ip-rotation-five/install_guest_keys.py"
SPEC = importlib.util.spec_from_file_location("guest_key_setup", SCRIPT)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
REMOTE = setup.worker_namespace()
SERVICE = "f5c0287a-636e-4981-95c7-80b24c9559c0"
BLOB = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([1]) * 32
KEY = "ssh-ed25519 " + base64.b64encode(BLOB).decode()
RPC = b"# trusted test RPC source\n"


def request():
    return {"service_id": SERVICE, "public_key": KEY,
            "rpc_sha256": REMOTE["digest"](RPC), "apply": True}


class GuestAuthorizedKeyRenderTests(unittest.TestCase):
    def test_exact_restrictions_and_preserving_unrelated_raw_bytes(self):
        before = b"# existing root keys\nssh-rsa not-our-key comment\n# unicode \xd0\x90"
        result, changed = REMOTE["render_authorized"](before, KEY, SERVICE)
        self.assertTrue(changed)
        self.assertTrue(result.startswith(before + b"\n"))
        self.assertIn(b'restrict,from="185.9.24.53",command="/usr/bin/python3 -I /opt/spn-ip-failover-guest/guest_rpc.py" ', result)
        self.assertTrue(result.endswith(("spn-ip-failover:" + SERVICE + "\n").encode()))
        self.assertEqual(REMOTE["render_authorized"](result, KEY, SERVICE), (result, False))

    def test_preexisting_equally_restricted_key_is_not_duplicated_or_reformatted(self):
        line = REMOTE["OPTIONS"] + b" " + KEY.encode() + b" manually-added-comment\n"
        self.assertEqual(REMOTE["render_authorized"](line, KEY, SERVICE), (line, False))
        duplicate = line + line
        self.assertEqual(REMOTE["render_authorized"](duplicate, KEY, SERVICE), (duplicate, False))

    def test_same_key_with_foreign_options_is_never_overwritten_or_shadowed(self):
        own = REMOTE["OPTIONS"] + b" " + KEY.encode() + b" own\n"
        for line in (KEY.encode(), b'from="1.1.1.1" ' + KEY.encode(),
                     own.replace(b"restrict,", b"no-pty,"),
                     own.replace(b"185.9.24.53", b"185.9.24.54"),
                     own.replace(b"guest_rpc.py", b"different.py"),
                     own + KEY.encode(), b"# " + KEY.encode()):
            with self.assertRaises(ValueError):
                REMOTE["render_authorized"](line, KEY, SERVICE)

    def test_only_well_formed_ed25519_public_key_and_fixed_service_are_accepted(self):
        self.assertEqual(REMOTE["public_key"](KEY), KEY.encode())
        for key in (KEY + "\n", "ssh-rsa " + KEY.split()[1], "PRIVATE KEY", KEY[:-4],
                    "ssh-ed25519 " + base64.b64encode(b"x" * 51).decode()):
            with self.assertRaises((ValueError, TypeError)):
                REMOTE["public_key"](key)
        with self.assertRaises(ValueError):
            REMOTE["render_authorized"](b"", KEY, "outside-scope")


class GuestAuthorizedKeyInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        for name in ("root", "root/.ssh", "opt", "opt/spn-ip-failover-guest", "etc", "var", "var/lib"):
            (self.root / name).mkdir(mode=0o700)
        self.fs = REMOTE["Files"](self.root, os.getuid())
        prefix = self.root / "opt/spn-ip-failover-guest"
        self.write(prefix / ".service-id", (SERVICE + "\n").encode())
        self.write(prefix / "guest_rpc.py", RPC, mode=0o644)
        self.write(self.root / "etc/spn-ip-failover-guest.json", json.dumps({"server_id": SERVICE, "method": "netplan"}).encode())
        self.path = self.root / "root/.ssh/authorized_keys"
        self.before = b"# preserve\nssh-rsa unrelated non-secret public comment\n"
        self.write(self.path, self.before)
        self.check = Mock()

    def write(self, path, raw, mode=0o600):
        path.write_bytes(raw)
        path.chmod(mode)

    def install(self, payload=None):
        return REMOTE["install"](payload or request(), self.fs, self.check)

    @property
    def state(self):
        return self.root / "var/lib/spn-ip-failover-key-enrollment" / SERVICE

    def test_backups_readiness_readback_and_idempotent_repeat(self):
        result = self.install()
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_installed"])
        self.check.assert_called_once_with(SERVICE)
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(self.before))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        before = json.loads((self.state / "authorized_keys.before.json").read_bytes())
        self.assertEqual(base64.b64decode(before["base64"]), self.before)
        self.assertFalse(before["was_absent"])
        metadata = self.path.stat().st_ino, self.path.stat().st_mtime_ns
        self.assertTrue(self.install()["already_installed"])
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual((self.path.stat().st_ino, self.path.stat().st_mtime_ns), metadata)

    def test_absent_authorized_keys_is_recorded_without_changing_other_ssh_files(self):
        self.path.unlink()
        unrelated = self.path.parent / "config"
        self.write(unrelated, b"# operator config\n")
        self.assertTrue(self.install()["ok"])
        before = json.loads((self.state / "authorized_keys.before.json").read_bytes())
        self.assertTrue(before["was_absent"])
        self.assertEqual(base64.b64decode(before["base64"]), b"")
        self.assertEqual(unrelated.read_bytes(), b"# operator config\n")

    def test_foreign_same_key_readiness_failure_or_wrong_staging_never_modifies_access(self):
        self.write(self.path, KEY.encode() + b" unrestricted\n")
        with self.assertRaisesRegex(ValueError, "foreign"):
            self.install()
        self.assertEqual(self.path.read_bytes(), KEY.encode() + b" unrestricted\n")
        self.write(self.path, self.before)
        self.check.side_effect = ValueError("not ready")
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.install()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.check.side_effect = None
        bad = request()
        bad["rpc_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "reviewed helper"):
            self.install(bad)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_unsafe_ssh_mode_or_symlink_is_rejected_not_repaired(self):
        self.path.parent.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private directory"):
            self.install()
        self.path.parent.chmod(0o700)
        self.path.unlink()
        target = self.root / "unrelated"
        self.write(target, self.before)
        self.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            self.install()
        self.assertEqual(target.read_bytes(), self.before)

    def test_concurrent_unrelated_addition_is_preserved_and_not_overwritten(self):
        original = self.fs.replace
        def concurrent(path, before, metadata, after):
            self.write(path, before + b"# operator added another key\n")
            return original(path, before, metadata, after)
        self.fs.replace = concurrent
        with self.assertRaisesRegex(ValueError, "concurrently"):
            self.install()
        self.assertEqual(self.path.read_bytes(), self.before + b"# operator added another key\n")
        self.assertFalse((self.state / "complete.json").exists())

    def test_lost_reply_after_atomic_replace_replays_without_duplicate_and_finishes_receipt(self):
        original = self.fs.once
        def interrupted(path, raw):
            if path.name == "complete.json":
                raise RuntimeError("interrupted before receipt")
            original(path, raw)
        self.fs.once = interrupted
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.install()
        raw = self.path.read_bytes()
        self.fs.once = original
        self.assertTrue(self.install()["already_installed"])
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertTrue((self.state / "complete.json").exists())

    def test_manually_removed_previously_installed_key_requires_review(self):
        self.install()
        self.write(self.path, self.before)
        with self.assertRaisesRegex(ValueError, "removed"):
            self.install()
        self.assertEqual(self.path.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
