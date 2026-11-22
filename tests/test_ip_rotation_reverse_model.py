import ast
import base64
import importlib.util
from pathlib import Path
import shlex
import struct
import unittest
from unittest.mock import Mock

from spn_ip_failover.provider_gateway import Gateway


SCRIPT = Path(__file__).resolve().parents[1] / "ops/ip-rotation-five/reverse_management_model.py"
SPEC = importlib.util.spec_from_file_location("reverse_management_model_test", SCRIPT)
model = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(model)
SERVICE = "f5c0287a-636e-4981-95c7-80b24c9559c0"
BLOB = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([1]) * 32
KEY = "ssh-ed25519 " + base64.b64encode(BLOB).decode("ascii")


class ReverseManagementScopeTests(unittest.TestCase):
    def test_static_uuid_ports_match_the_five_selected_order(self):
        self.assertEqual(list(model.SERVICE_PORTS.items()), [
            ("f5c0287a-636e-4981-95c7-80b24c9559c0", 23001),
            ("21e508f0-2ef2-47da-aee7-15dc8c66eb15", 23002),
            ("9b0316d5-fa83-4bf8-b6dc-8202b66622c9", 23003),
            ("626633f5-3bf3-48c7-aaba-9bbf43f8ea9d", 23004),
            ("4d6f358a-e75f-4dd8-a635-c6ad320f2732", 23005),
        ])
        monitor = ast.parse(SCRIPT.with_name("install_ro_monitor.py").read_text())
        selected = next(ast.literal_eval(node.value) for node in monitor.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "SELECTED" for target in node.targets))
        self.assertEqual(list(model.SERVICE_PORTS), [row["service_id"] for row in selected])
        self.assertEqual(model.SERVICES, frozenset(model.SERVICE_PORTS))
        for service, port in model.SERVICE_PORTS.items():
            self.assertEqual(model.service_port(service), port)
        for invalid in (None, 23001, [], {}, SERVICE.upper(), "outside-scope", SERVICE + "\n"):
            for render in (model.service_port, model.guest_unit, model.gateway_ssh):
                with self.subTest(invalid=invalid, renderer=render.__name__), self.assertRaises(ValueError):
                    render(invalid)
            with self.assertRaises(ValueError):
                model.rf_key_line(invalid, KEY)
            with self.assertRaises(ValueError):
                model.rpc_key_upgrade(b"", KEY, invalid)

    def test_keys_are_canonical_wire_encoded_ed25519_not_just_matching_text(self):
        self.assertEqual(model.public_key(KEY), KEY)
        self.assertEqual(model.public_key(KEY + " generated tunnel key"), KEY)
        malformed = [None, KEY.encode(), "", "private key", KEY + "\n", KEY + "\rcomment",
                     KEY + "\x00", KEY + "\tcomment", " " + KEY, KEY.replace(" ", "\t", 1),
                     KEY.replace("ssh-ed25519", "ssh-rsa"), KEY + "=", KEY[:-1],
                     "ssh-ed25519 " + base64.b64encode(b"x" * 51).decode(),
                     "ssh-ed25519 " + base64.b64encode(BLOB + b"x").decode(),
                     "ssh-ed25519 " + base64.b64encode(BLOB[:15] + struct.pack(">I", 31) + BLOB[19:]).decode(),
                     KEY + " " + "x" * 2048]
        for invalid in malformed:
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                model.public_key(invalid)


class ReverseManagementRenderTests(unittest.TestCase):
    def test_controller_policy_scopes_all_and_only_loopback_ports_and_terminates_match(self):
        policy = model.controller_policy().decode("ascii")
        self.assertIn("Match User " + model.RF_USER + "\n", policy)
        self.assertTrue(policy.endswith("Match all\n"))
        listeners = next(line.strip() for line in policy.splitlines() if "PermitListen" in line)
        self.assertEqual(listeners, "PermitListen " + " ".join(
            "127.0.0.1:" + str(port) for port in range(23001, 23006)))
        for required in ("AllowTcpForwarding remote", "AllowStreamLocalForwarding no", "PermitOpen none",
                         "GatewayPorts no", "PermitTunnel no", "PermitTTY no", "AllowAgentForwarding no",
                         "X11Forwarding no", "PasswordAuthentication no", "KbdInteractiveAuthentication no",
                         "AuthenticationMethods publickey", "ForceCommand /usr/bin/false", "MaxSessions 0",
                         "AuthorizedKeysFile " + model.RF_HOME + "/.ssh/authorized_keys",
                         "ClientAliveInterval 15", "ClientAliveCountMax 3"):
            with self.subTest(directive=required):
                self.assertIn("    " + required + "\n", policy)
        self.assertNotIn("0.0.0.0", policy)
        self.assertNotIn("*:", policy)
        parsed = {line.strip().split(" ", 1)[0].lower(): line.strip().split(" ", 1)[1]
                  for line in policy.splitlines() if line.startswith("    ")}
        self.assertEqual(parsed, model.RF_EXPECTED)

    def test_each_rf_key_can_listen_only_on_its_own_port_without_source_ip_pin(self):
        for service, port in model.SERVICE_PORTS.items():
            line = model.rf_key_line(service, KEY + " ignored comment").decode("ascii")
            self.assertEqual(line, f'restrict,port-forwarding,permitlisten="127.0.0.1:{port}",'
                             f'command="/usr/bin/false" {KEY} spn-reverse-management:{service}\n')
            self.assertEqual(line.count("permitlisten="), 1)
            self.assertNotIn("from=", line)
            self.assertNotIn("permitopen=", line)
            self.assertNotIn("0.0.0.0", line)
            self.assertNotIn("*", line)
            for other in set(model.SERVICE_PORTS.values()) - {port}:
                self.assertNotIn(str(other), line)

    def test_guest_unit_is_nonroot_persistent_hardened_and_has_only_one_reverse_forward(self):
        for service, port in model.SERVICE_PORTS.items():
            unit = model.guest_unit(service).decode("ascii")
            for directive in ("User=" + model.GUEST_USER, "Group=" + model.GUEST_USER,
                              "Restart=always", "RestartSec=3", "StartLimitIntervalSec=0",
                              "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes"):
                self.assertIn(directive + "\n", unit)
            self.assertNotIn("User=root", unit)
            self.assertNotIn("ExecStartPre=", unit)
            self.assertNotIn("ExecStop=", unit)
            self.assertNotIn("systemctl", unit)
            self.assertNotIn("netplan", unit)
            self.assertNotIn("xray", unit)
            command = shlex.split(next(line[len("ExecStart="):] for line in unit.splitlines()
                                       if line.startswith("ExecStart=")))
            self.assertEqual(command[:5], ["/usr/bin/ssh", "-4", "-NT", "-F", "/dev/null"])
            self.assertEqual(command[-1], model.RF_USER + "@" + model.CONTROLLER)
            self.assertEqual(command[command.index("-R") + 1], f"127.0.0.1:{port}:127.0.0.1:22")
            self.assertEqual(command.count("-R"), 1)
            self.assertNotIn("-L", command)
            self.assertNotIn("-D", command)
            self.assertIn(model.GUEST_HOME + "/.ssh/tunnel_ed25519", command)
            for option in ("StrictHostKeyChecking=yes", "HostKeyAlias=spn-ip-rotation-controller",
                           "UserKnownHostsFile=" + model.GUEST_HOME + "/.ssh/controller_known_hosts",
                           "GlobalKnownHostsFile=/dev/null", "BatchMode=yes", "IdentitiesOnly=yes",
                           "ExitOnForwardFailure=yes", "ServerAliveInterval=15", "ServerAliveCountMax=3",
                           "ConnectTimeout=15", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no"):
                self.assertIn(option, command)

    def test_gateway_keeps_logical_address_and_service_host_alias_while_ssh_uses_loopback(self):
        for service, port in model.SERVICE_PORTS.items():
            command = model.gateway_ssh(service)
            self.assertLessEqual(len(command), 48)
            self.assertEqual(command.count("{address}"), 1)
            self.assertEqual(command[-1], "{address}")
            self.assertEqual(command[command.index("-p") + 1], str(port))
            self.assertEqual(command[command.index("-l") + 1], "root")
            self.assertIn("HostName=127.0.0.1", command)
            self.assertIn("HostKeyAlias=" + service, command)
            self.assertIn("/var/lib/spn-ip-failover/.ssh/guest_" + service + "_ed25519", command)
            self.assertIn("UserKnownHostsFile=/var/lib/spn-ip-failover/.ssh/guest_known_hosts", command)
            for option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes",
                           "GlobalKnownHostsFile=/dev/null", "ClearAllForwardings=yes"):
                self.assertIn(option, command)
            self.assertIn("-T", command)
            self.assertNotIn("-N", command)
            request = {"operation": "check", "server_id": service}
            run = Mock(return_value={"ok": True})
            settings = {"version": 1, "remnawave": {}, "servers": {service: {
                "billing_command": ["/usr/bin/false"], "guest_ssh": command,
                "node_uuid": "5c472650-1f93-470a-abdb-0caadef059b7", "supports_retire": False}}}
            gateway = Gateway(settings, object(), run=run)
            gateway.guest(gateway.servers[service], "8.8.8.8", request)
            actual, actual_request = run.call_args.args
            self.assertEqual(actual[-1], "8.8.8.8")
            self.assertEqual(actual_request, request)
            self.assertIn("HostName=127.0.0.1", actual)
            self.assertIn("HostKeyAlias=" + service, actual)


class ReverseRpcUpgradeTests(unittest.TestCase):
    def old(self, suffix=b" comment\n"):
        return model.RPC_OPTIONS_OLD + b" " + KEY.encode("ascii") + suffix

    def test_upgrade_changes_only_source_restriction_and_preserves_all_other_bytes(self):
        prefix = b"# preexisting keys\r\nssh-rsa unrelated-key untouched\n# non-utf8 \xff\n"
        target = self.old(b"\tspn-ip-failover:" + SERVICE.encode() + b" comment\r\n")
        suffix = b"# retain trailing bytes and missing newline \xfe"
        before = prefix + target + suffix
        after, changed = model.rpc_key_upgrade(before, KEY + " discarded public comment", SERVICE)
        self.assertTrue(changed)
        self.assertEqual(after, prefix + target.replace(model.RPC_OPTIONS_OLD, model.RPC_OPTIONS_NEW, 1) + suffix)
        self.assertEqual(model.rpc_key_upgrade(after, KEY, SERVICE), (after, False))

    def test_missing_final_newline_and_existing_restricted_duplicates_are_preserved(self):
        for before in (self.old(b""), self.old(b"\r\n") + self.old(b" second\n")):
            expected = before.replace(model.RPC_OPTIONS_OLD, model.RPC_OPTIONS_NEW)
            self.assertEqual(model.rpc_key_upgrade(before, KEY, SERVICE), (expected, True))
            self.assertEqual(model.rpc_key_upgrade(expected, KEY, SERVICE), (expected, False))

    def test_missing_key_is_not_appended_even_to_an_empty_file(self):
        for before in (b"", b"# unrelated file\n", b"ssh-rsa unrelated public key\n"):
            with self.assertRaisesRegex(ValueError, "missing; no key was appended"):
                model.rpc_key_upgrade(before, KEY, SERVICE)

    def test_foreign_same_key_options_and_comments_are_rejected_without_shadowing(self):
        own = self.old()
        foreign = [KEY.encode(), b'from="1.1.1.1" ' + KEY.encode(),
                   own.replace(b"restrict,", b"no-pty,"),
                   own.replace(b"185.9.24.53", b"185.9.24.54"),
                   own.replace(b"python3 -I ", b"python3 "),
                   own.replace(b"guest_rpc.py", b"different.py"),
                   own.replace(b'restrict,from=', b'port-forwarding,restrict,from='),
                   b"# " + KEY.encode(), own + KEY.encode(),
                   self.old(b" comment " + KEY.encode()),
                   own.replace(KEY.encode(), KEY.encode() + b"="),
                   own.replace(b" " + KEY.encode(), b"\t" + KEY.encode())]
        for line in foreign:
            for before in (line, own + line):
                with self.subTest(line=line), self.assertRaisesRegex(ValueError, "foreign or different options"):
                    model.rpc_key_upgrade(before, KEY, SERVICE)

    def test_invalid_authorized_keys_input_fails_closed(self):
        for before in (None, "not bytes", b"\x00" + self.old(), b"x" * (model.MAX_AUTHORIZED_KEYS + 1)):
            with self.subTest(type=type(before)), self.assertRaises(ValueError):
                model.rpc_key_upgrade(before, KEY, SERVICE)


if __name__ == "__main__":
    unittest.main()
