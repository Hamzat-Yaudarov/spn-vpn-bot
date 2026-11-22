import base64
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ops/ip-rotation-five/install_eu_relays.py"
SPEC = importlib.util.spec_from_file_location("eu_relay_setup", SCRIPT)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
REMOTE = setup.worker_namespace()
SERVICE = "21e508f0-2ef2-47da-aee7-15dc8c66eb15"
BLOB = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([2]) * 32
KEY = "ssh-ed25519 " + base64.b64encode(BLOB).decode()


def request():
    return {"service_id": SERVICE, "public_key": KEY, "ip": "8.8.8.8", "apply": True}


class Accounts:
    def __init__(self):
        self.current = None
        self.created = 0
        self.password_locked = True

    def lookup(self):
        return self.current

    def create(self, commands):
        self.created += 1
        self.current = {"name": "spn-ip-relay", "uid": os.getuid(), "gid": os.getgid(),
                        "home": "/var/lib/spn-ip-relay", "shell": "/bin/sh"}

    def locked(self, _commands):
        if not self.password_locked:
            raise ValueError("password is not locked")


class Commands:
    def __init__(self, root):
        self.root = root
        self.events = []
        self.fail_syntax = False
        self.fail_root_policy = False
        self.fail_reload = False
        self.broad_forwarding = False
        self.fail_loopback_root_policy = False
        self.broad_loopback_forwarding = False
        self.contexts = []

    @property
    def policy(self):
        return self.root / REMOTE["DROPIN"].lstrip("/")

    def syntax(self):
        self.events.append("sshd -t")
        if self.fail_syntax and self.policy.exists():
            raise ValueError("unsupported Match syntax")

    def service_ready(self):
        self.events.append("reload-capability")

    def effective(self, user, ip, source="185.9.24.53"):
        self.events.append("effective:" + user)
        self.contexts.append((user, ip, source))
        if user == "root":
            changed = self.fail_root_policy or self.fail_loopback_root_policy and source == "127.0.0.1"
            return b"unchanged root policy " + source.encode() + b"\n" + (b"changed\n" if changed and self.policy.exists() else b"")
        expected = {**REMOTE["EXPECTED"], "disableforwarding": "no"}
        if self.broad_forwarding or self.broad_loopback_forwarding and source == "127.0.0.1":
            expected["allowtcpforwarding"] = "yes"
        return "".join(key + " " + value + "\n" for key, value in expected.items()).encode()

    def reload(self):
        self.events.append("reload ssh")
        if (self.root / "var/lib/spn-ip-relay/.ssh/authorized_keys").exists():
            raise AssertionError("Key was published before restrictions were loaded")
        if self.fail_reload:
            raise RuntimeError("reload response unavailable")


class RelayInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        for name in ("opt", "opt/spn-ip-failover-guest", "etc", "etc/ssh", "etc/ssh/sshd_config.d", "var", "var/lib"):
            (self.root / name).mkdir(mode=0o700)
        marker = self.root / "opt/spn-ip-failover-guest/.service-id"
        marker.write_text(SERVICE + "\n")
        marker.chmod(0o600)
        self.fs = REMOTE["Files"](self.root, os.getuid())
        self.accounts = Accounts()
        self.commands = Commands(self.root)

    @property
    def auth(self):
        return self.root / "var/lib/spn-ip-relay/.ssh/authorized_keys"

    @property
    def state(self):
        return self.root / "var/lib/spn-ip-relay-setup"

    def install(self, payload=None):
        return REMOTE["install"](payload or request(), self.fs, self.accounts, self.commands)

    def test_only_dedicated_non_root_user_reload_and_idempotent_access(self):
        report = self.install()
        self.assertTrue(report["ok"])
        self.assertTrue(report["root_policy_unchanged"])
        self.assertFalse(report["vpn_restarted"])
        self.assertFalse(report["network_restarted"])
        self.assertEqual(self.accounts.created, 1)
        self.assertEqual(self.commands.policy.read_bytes(), setup.POLICY)
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.auth.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn(b'restrict,port-forwarding,from="185.9.24.53,127.0.0.1",permitopen="*:443",command="/usr/bin/false"', self.auth.read_bytes())
        for user in ("root", "spn-ip-relay"):
            self.assertIn((user, "8.8.8.8", "185.9.24.53"), self.commands.contexts)
            self.assertIn((user, "127.0.0.1", "127.0.0.1"), self.commands.contexts)
        # Root is compared before, after the drop-in, and after reload on both paths.
        for ip, source in (("8.8.8.8", "185.9.24.53"), ("127.0.0.1", "127.0.0.1")):
            self.assertEqual(self.commands.contexts.count(("root", ip, source)), 3)
        before = self.auth.read_bytes(), self.auth.stat().st_ino
        self.assertTrue(self.install()["already_installed"])
        self.assertEqual((self.auth.read_bytes(), self.auth.stat().st_ino), before)
        self.assertEqual(self.commands.events.count("reload ssh"), 1)
        self.assertTrue((self.state / "complete.json").exists())

    def test_syntax_or_root_policy_change_rolls_back_new_dropin_before_reload(self):
        for attribute in ("fail_syntax", "fail_root_policy", "broad_forwarding", "fail_loopback_root_policy", "broad_loopback_forwarding"):
            with self.subTest(attribute=attribute):
                # Separate fixture so immutable account/intent journals are valid.
                self.setUp()
                setattr(self.commands, attribute, True)
                with self.assertRaises(ValueError):
                    self.install()
                self.assertFalse(self.commands.policy.exists())
                self.assertFalse(self.auth.exists())
                self.assertNotIn("reload ssh", self.commands.events)
                self.assertTrue(json.loads((self.state / "ssh-dropin.before.json").read_bytes())["was_absent"])

    def test_preexisting_foreign_dropin_or_user_is_never_overwritten(self):
        self.commands.policy.write_bytes(b"# operator's unrelated policy\n")
        self.commands.policy.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "foreign"):
            self.install()
        self.assertEqual(self.commands.policy.read_bytes(), b"# operator's unrelated policy\n")
        self.assertNotIn("reload ssh", self.commands.events)
        self.commands.policy.unlink()
        (self.state / "account.json").unlink()
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.install()
        self.assertFalse(self.auth.exists())

    def test_identical_preexisting_dropin_is_preserved_not_deleted_on_failure(self):
        self.commands.policy.write_bytes(setup.POLICY)
        self.commands.policy.chmod(0o644)
        self.commands.fail_syntax = True
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.commands.policy.read_bytes(), setup.POLICY)
        self.assertEqual(self.commands.policy.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.auth.exists())
        self.assertNotIn("reload ssh", self.commands.events)

    def test_reload_failure_never_publishes_key_or_restarts_service(self):
        self.commands.fail_reload = True
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertFalse(self.auth.exists())
        self.assertFalse((self.state / "complete.json").exists())
        self.assertEqual(self.commands.events.count("reload ssh"), 1)
        self.assertTrue(self.commands.policy.exists())
        self.assertFalse(any("restart" in event for event in self.commands.events))
        self.commands.fail_reload = False
        self.assertTrue(self.install()["ok"])
        self.assertEqual(self.accounts.created, 1)

    def test_removed_completed_policy_or_key_requires_manual_review(self):
        self.install()
        self.auth.unlink()
        with self.assertRaisesRegex(ValueError, "removed"):
            self.install()
        self.assertFalse(self.auth.exists())
        self.assertEqual(self.commands.events.count("reload ssh"), 1)

    def test_existing_same_key_with_rf_only_options_is_foreign_and_not_migrated(self):
        self.install()
        previous = self.auth.read_bytes().replace(b'from="185.9.24.53,127.0.0.1"', b'from="185.9.24.53"')
        self.auth.write_bytes(previous)
        with self.assertRaisesRegex(ValueError, "foreign or different options"):
            self.install()
        self.assertEqual(self.auth.read_bytes(), previous)
        self.assertEqual(self.commands.events.count("reload ssh"), 1)

    def test_unlocked_password_and_non_selected_service_fail_without_access(self):
        self.accounts.password_locked = False
        with self.assertRaisesRegex(ValueError, "not locked"):
            self.install()
        self.assertFalse(self.auth.exists())
        self.assertFalse(self.commands.policy.exists())
        payload = request()
        payload["service_id"] = "f5c0287a-636e-4981-95c7-80b24c9559c0"
        with self.assertRaisesRegex(ValueError, "two-service"):
            self.install(payload)


class RelayPolicyAndPlanningTests(unittest.TestCase):
    def test_effective_context_uses_both_source_and_local_address_and_exact_key_options(self):
        commands = REMOTE["Commands"]()
        calls = []
        commands.run = lambda argv: calls.append(argv) or b""
        commands.effective("spn-ip-relay", "127.0.0.1", "127.0.0.1")
        self.assertEqual(calls[0], ["/usr/sbin/sshd", "-T", "-C",
                                  "user=spn-ip-relay,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22"])
        commands.effective("root", "8.8.8.8")
        self.assertIn("addr=185.9.24.53,laddr=8.8.8.8", calls[-1][-1])
        with self.assertRaises(ValueError):
            commands.effective("spn-ip-relay", "8.8.8.8", "0.0.0.0")
        self.assertEqual(REMOTE["OPTIONS"],
                         b'restrict,port-forwarding,from="185.9.24.53,127.0.0.1",permitopen="*:443",command="/usr/bin/false"')

    def test_unix_remote_tunnel_pty_shell_and_password_paths_are_closed(self):
        REMOTE["restrictions"]("".join(key + " " + value + "\n" for key, value in REMOTE["EXPECTED"].items()).encode())
        standard = "".join(key + " " + value + "\n" for key, value in REMOTE["EXPECTED"].items()).encode()
        REMOTE["restrictions"](standard + b"listenaddress 0.0.0.0:22\nlistenaddress [::]:22\n")
        with self.assertRaises(ValueError):
            REMOTE["restrictions"](standard + b"allowtcpforwarding yes\n")
        for key in ("allowtcpforwarding", "allowstreamlocalforwarding", "permittunnel", "permittty", "forcecommand",
                    "permitlisten", "permitopen", "passwordauthentication", "authenticationmethods"):
            values = {**REMOTE["EXPECTED"], key: "yes"}
            with self.assertRaises(ValueError):
                REMOTE["restrictions"]("".join(name + " " + value + "\n" for name, value in values.items()).encode())

    def test_default_plan_has_no_network_or_credentials(self):
        with patch.object(setup.sys, "argv", ["install_eu_relays.py"]), \
                patch.object(setup.subprocess, "run", side_effect=AssertionError("No external calls")), \
                patch("builtins.print") as output:
            setup.main()
        report = json.loads(output.call_args.args[0])
        self.assertFalse(report["network_used"])
        self.assertFalse(report["credentials_read"])
        self.assertFalse(report["remote_writes"])
        self.assertEqual(report["service_count"], 2)

    def test_rf_setup_never_reads_or_serializes_private_key_bytes(self):
        compile(setup.CONTROLLER_KEYS, "<controller-public-key-setup>", "exec")
        self.assertIn("os.open(pub,", setup.CONTROLLER_KEYS)
        self.assertNotIn("os.open(key,", setup.CONTROLLER_KEYS)
        self.assertNotIn("key.read_bytes", setup.CONTROLLER_KEYS)
        self.assertIn("'-y','-f',str(key)", setup.CONTROLLER_KEYS)
        self.assertEqual(setup.SERVICES, {SERVICE, "4d6f358a-e75f-4dd8-a635-c6ad320f2732"})


if __name__ == "__main__":
    unittest.main()
