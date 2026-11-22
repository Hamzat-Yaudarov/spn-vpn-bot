import base64
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "ops/ip-rotation-five/install_reverse_management.py"
SPEC = importlib.util.spec_from_file_location("reverse_setup", SCRIPT)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
model = setup.model
SERVICES = list(model.SERVICE_PORTS)
SERVICE = SERVICES[1]


def key(seed):
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


CLIENT_KEY, RPC_KEY, HOST_KEY = key(21), key(22), key(23)


class Accounts:
    def __init__(self, w):
        self.w = w
        self.current = None
        self.created = 0
        self.password_locked = True

    def lookup(self):
        return self.current

    def create(self, _commands):
        self.created += 1
        self.current = {"name": self.w["USER"], "uid": os.getuid(), "gid": os.getgid(),
                        "home": self.w["HOME"], "shell": "/bin/sh"}

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
        self.key = CLIENT_KEY
        self.keygen_count = 0

    @property
    def policy(self):
        return self.root / model.RF_DROPIN.lstrip("/")

    def syntax(self):
        self.events.append("sshd -t")
        if self.fail_syntax and self.policy.exists():
            raise ValueError("unsupported Match syntax")

    def service_ready(self):
        self.events.append("reload-capability")

    def effective(self, user, _ip):
        self.events.append("effective:" + user)
        if user == "root":
            return b"root unchanged\n" + (b"drift\n" if self.fail_root_policy and self.policy.exists() else b"")
        values = {**model.RF_EXPECTED, "disableforwarding": "no"}
        if self.broad_forwarding:
            values["allowtcpforwarding"] = "yes"
        return "".join(k + " " + v + "\n" for k, v in values.items()).encode()

    def reload(self):
        self.events.append("reload ssh")
        if (self.root / (model.RF_HOME + "/.ssh/authorized_keys").lstrip("/")).exists():
            raise AssertionError("Access published before loaded restrictions")
        if self.fail_reload:
            raise RuntimeError("reload reply lost")

    def run(self, argv):
        self.events.append(tuple(argv))
        if "/usr/bin/ssh-keygen" in argv:
            if "-y" in argv:
                return (self.key + "\n").encode()
            self.keygen_count += 1
            path = Path(argv[argv.index("-f") + 1])
            path.write_bytes(b"NOT A REAL PRIVATE KEY: NEVER EXPORT")
            path.chmod(0o600)
            path.with_name(path.name + ".pub").write_text(self.key + " fixture\n")
            path.with_name(path.name + ".pub").chmod(0o644)
        return b""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        for name in ("opt", "opt/wayspn", "opt/spn-ip-failover-guest", "etc", "etc/ssh", "etc/ssh/sshd_config.d",
                     "etc/systemd", "etc/systemd/system", "var", "var/lib", "root", "root/.ssh"):
            (self.root / name).mkdir(mode=0o700)
        self.put("/opt/wayspn/.controller-deployment", (setup.CONTROLLER_DEPLOYMENT + "\n").encode())
        self.put("/opt/spn-ip-failover-guest/.service-id", (SERVICE + "\n").encode())
        self.put("/etc/ssh/ssh_host_ed25519_key.pub", (HOST_KEY + " fixture\n").encode(), 0o644)
        self.w = setup.worker("rf")
        self.fs = self.w["Files"](self.root, os.getuid())
        self.accounts = Accounts(self.w)
        self.commands = Commands(self.root)

    def put(self, name, raw, mode=0o600):
        path = self.root / name.lstrip("/")
        path.write_bytes(raw)
        path.chmod(mode)
        return path

    @property
    def auth(self):
        return self.root / (model.RF_HOME + "/.ssh/authorized_keys").lstrip("/")

    @property
    def state(self):
        return self.root / setup.RF_STATE.lstrip("/")

    def register(self, service=SERVICE, pub=CLIENT_KEY):
        return setup.rf_register({"service_id": service, "public_key": pub, "apply": True},
                                 w=self.w, fs=self.fs, accounts=self.accounts, commands=self.commands)

    def guest_fixture(self):
        self.w = setup.worker("guest")
        self.fs = self.w["Files"](self.root, os.getuid())
        self.accounts = Accounts(self.w)
        self.commands = Commands(self.root)
        self.put("/root/.ssh/authorized_keys", b"# operator key kept byte-for-byte\r\n"
                 + model.RPC_OPTIONS_OLD + b" " + RPC_KEY.encode() + b" fixed-rpc\r\n")

    def prepare(self):
        return setup.guest_prepare({"service_id": SERVICE, "apply": True},
                                  w=self.w, fs=self.fs, accounts=self.accounts, commands=self.commands, readiness=False)

    def activate(self, registration=None):
        registration = registration or {"registered": True, "service_id": SERVICE, "port": model.service_port(SERVICE),
                                        "controller": model.CONTROLLER, "controller_host_key": HOST_KEY,
                                        "client_public_key_sha256": setup.sha(CLIENT_KEY.encode())}
        return setup.guest_activate({"service_id": SERVICE, "controller_rpc_public_key": RPC_KEY,
                                    "registration": registration, "apply": True},
                                   w=self.w, fs=self.fs, accounts=self.accounts, commands=self.commands, readiness=False)


class RFTests(Fixture):
    def test_two_registrations_and_repeats_preserve_other_service(self):
        first = self.register()
        self.assertTrue(first["registered"])
        self.assertEqual(first["controller_host_key"], HOST_KEY)
        self.assertNotIn("PRIVATE", json.dumps(first))
        self.assertEqual(self.commands.events.count("reload ssh"), 1)
        self.register(SERVICES[2], key(24))
        before = self.auth.read_bytes(), self.auth.stat().st_ino
        self.register()
        self.register(SERVICES[2], key(24))
        self.assertEqual((self.auth.read_bytes(), self.auth.stat().st_ino), before)
        self.assertEqual(self.accounts.created, 1)
        self.assertEqual(self.commands.events.count("reload ssh"), 1)
        registry = json.loads((self.state / "registry.json").read_bytes())
        self.assertEqual(set(registry["services"]), {SERVICE, SERVICES[2]})
        self.assertEqual(self.auth.read_bytes(), setup.registry_authorized(registry))
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)

    def test_partial_auth_commit_resumes_without_overwriting_backup(self):
        replace = self.fs.replace
        def crash_registry(path, before, metadata, after):
            if path.name == "registry.json":
                raise RuntimeError("simulated reply lost before registry commit")
            return replace(path, before, metadata, after)
        with patch.object(self.fs, "replace", side_effect=crash_registry):
            with self.assertRaises(RuntimeError):
                self.register()
        backup = (self.state / SERVICE / "authorized_keys.before.json").read_bytes()
        self.assertTrue(self.auth.exists())
        with self.assertRaisesRegex(ValueError, "pending"):
            self.register(SERVICES[2], key(24))
        self.assertTrue(self.register()["registered"])
        self.assertEqual((self.state / SERVICE / "authorized_keys.before.json").read_bytes(), backup)
        self.assertTrue(json.loads(backup)["was_absent"])
        self.assertEqual(self.commands.events.count("reload ssh"), 1)

    def test_registry_commit_without_complete_receipt_resumes(self):
        once = self.fs.once
        def crash_complete(path, data):
            if path.name == "complete.json":
                raise RuntimeError("lost reply after registry commit")
            return once(path, data)
        with patch.object(self.fs, "once", side_effect=crash_complete):
            with self.assertRaises(RuntimeError):
                self.register()
        self.assertTrue(self.register()["registered"])
        self.assertTrue((self.state / SERVICE / "complete.json").exists())

    def test_foreign_key_scope_duplicate_and_completed_removal_are_closed(self):
        self.register()
        original = self.auth.read_bytes()
        for service, pub in ((SERVICE, key(25)), (SERVICES[2], CLIENT_KEY), ("foreign", key(25))):
            with self.subTest(service=service, pub=pub):
                with self.assertRaises(ValueError):
                    self.register(service, pub)
                self.assertEqual(self.auth.read_bytes(), original)
        self.commands.policy.unlink()
        with self.assertRaisesRegex(ValueError, "removed"):
            self.register()
        self.assertFalse(self.commands.policy.exists())

    def test_validation_failure_rolls_back_only_new_policy_no_reload(self):
        for attribute in ("fail_syntax", "fail_root_policy", "broad_forwarding"):
            with self.subTest(attribute=attribute):
                self.setUp()
                setattr(self.commands, attribute, True)
                with self.assertRaises(ValueError):
                    self.register()
                self.assertFalse(self.commands.policy.exists())
                self.assertFalse(self.auth.exists())
                self.assertNotIn("reload ssh", self.commands.events)

    def test_reload_failure_publishes_no_key_and_retry_is_safe(self):
        self.commands.fail_reload = True
        with self.assertRaises(RuntimeError):
            self.register()
        self.assertFalse(self.auth.exists())
        self.commands.fail_reload = False
        self.assertTrue(self.register()["registered"])

    def test_foreign_auth_or_intent_is_not_silently_adopted(self):
        self.register()
        self.put(model.RF_HOME + "/.ssh/authorized_keys", self.auth.read_bytes() + b"# foreign edit\n")
        with self.assertRaisesRegex(ValueError, "outside"):
            self.register()
        self.assertTrue(self.auth.read_bytes().endswith(b"# foreign edit\n"))

    def test_unsafe_lock_rejected(self):
        self.state.mkdir(mode=0o700)
        self.put(setup.RF_STATE + "/lock", b"", 0o666)
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            self.register()


class GuestTests(Fixture):
    def setUp(self):
        super().setUp()
        self.guest_fixture()

    def test_preparation_keeps_private_key_guest_local_and_never_starts_client(self):
        first = self.prepare()
        self.assertTrue(first["prepared"])
        self.assertEqual(first["public_key"], CLIENT_KEY)
        self.assertFalse(first["private_key_exported"])
        self.assertFalse(first["client_started"])
        self.assertEqual(self.commands.keygen_count, 1)
        before = (self.root / setup.CLIENT_KEY.lstrip("/")).stat().st_ino
        self.prepare()
        self.assertEqual(self.commands.keygen_count, 1)
        self.assertEqual((self.root / setup.CLIENT_KEY.lstrip("/")).stat().st_ino, before)
        self.assertFalse(any("systemctl" in " ".join(e) for e in self.commands.events if isinstance(e, tuple)))

    def test_completed_preparation_does_not_regenerate_deleted_key(self):
        self.prepare()
        path = self.root / setup.CLIENT_KEY.lstrip("/")
        path.unlink()
        (self.root / (setup.CLIENT_KEY + ".pub").lstrip("/")).unlink()
        with self.assertRaisesRegex(ValueError, "removed"):
            self.prepare()
        self.assertFalse(path.exists())
        self.assertEqual(self.commands.keygen_count, 1)

    def test_activation_only_new_client_no_network_or_vpn_restart_and_repeat(self):
        self.prepare()
        original = (self.root / "root/.ssh/authorized_keys").read_bytes()
        report = self.activate()
        self.assertTrue(report["configured"])
        self.assertFalse(report["tunnel_verified"])
        self.assertFalse(report["network_restarted"])
        self.assertFalse(report["vpn_restarted"])
        auth = self.root / "root/.ssh/authorized_keys"
        self.assertEqual(auth.read_bytes(), original.replace(model.RPC_OPTIONS_OLD, model.RPC_OPTIONS_NEW))
        paths = [auth, self.root / setup.CLIENT_KNOWN.lstrip("/"), self.root / setup.CLIENT_UNIT.lstrip("/")]
        before = [(p.read_bytes(), p.stat().st_ino) for p in paths]
        self.activate()
        self.assertEqual([(p.read_bytes(), p.stat().st_ino) for p in paths], before)
        starts = [event for event in self.commands.events if isinstance(event, tuple) and "enable" in event]
        self.assertEqual(starts, [("/usr/bin/systemctl", "enable", "--now", model.GUEST_UNIT)])
        self.assertFalse(any("restart" in " ".join(e) for e in self.commands.events if isinstance(e, tuple)))

    def test_cross_service_registration_host_change_or_foreign_rpc_is_rejected(self):
        self.prepare()
        registration = {"registered": True, "service_id": SERVICES[2], "port": model.service_port(SERVICES[2]),
                        "controller": model.CONTROLLER, "controller_host_key": HOST_KEY,
                        "client_public_key_sha256": setup.sha(CLIENT_KEY.encode())}
        with self.assertRaisesRegex(ValueError, "another guest"):
            self.activate(registration)
        self.put("/root/.ssh/authorized_keys", RPC_KEY.encode() + b" unrestricted\n")
        with self.assertRaisesRegex(ValueError, "foreign"):
            self.activate()
        self.put("/root/.ssh/authorized_keys", model.RPC_OPTIONS_OLD + b" " + RPC_KEY.encode() + b"\n")
        self.activate()
        self.put(setup.CLIENT_KNOWN, ("spn-ip-rotation-controller " + key(99) + "\n").encode())
        with self.assertRaisesRegex(ValueError, "host key changed"):
            self.activate()

    def test_completed_client_unit_removal_requires_review(self):
        self.prepare()
        self.activate()
        unit = self.root / setup.CLIENT_UNIT.lstrip("/")
        unit.unlink()
        with self.assertRaisesRegex(ValueError, "Completed"):
            self.activate()
        self.assertFalse(unit.exists())

    def test_wrong_guest_marker_never_prepares_identity(self):
        self.put("/opt/spn-ip-failover-guest/.service-id", (SERVICES[2] + "\n").encode())
        with self.assertRaisesRegex(ValueError, "another hosting"):
            self.prepare()
        self.assertEqual(self.accounts.created, 0)


class PlanningTests(unittest.TestCase):
    def test_default_plan_never_reads_request_or_uses_network(self):
        with patch.object(setup.sys, "argv", [str(SCRIPT), "--role", "gateway-plan", "--service", SERVICE]), \
                patch("builtins.print") as output, \
                patch.object(setup.eu.subprocess, "run", side_effect=AssertionError("No calls")):
            setup.main()
        report = json.loads(output.call_args.args[0])
        self.assertFalse(report["network_used"])
        self.assertFalse(report["credentials_read"])
        self.assertFalse(report["remote_writes"])
        self.assertEqual(report["guest_ssh"][-1], "{address}")

    def test_duplicate_fields_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            json.loads('{"apply":true,"apply":false}', object_pairs_hook=setup.unique)


if __name__ == "__main__":
    unittest.main()
