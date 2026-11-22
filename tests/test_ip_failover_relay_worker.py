"""Offline tests: no SSH, Xray, DNS or internet connections are performed."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from spn_ip_failover import relay_worker, worker


def outbound():
    return {"protocol": "vless", "settings": {"vnext": [
        {"address": "vpn.example.com", "port": 443, "users": [{"id": "synthetic-test-secret"}]}]},
        "streamSettings": {"network": "ws", "security": "tls", "tlsSettings": {"serverName": "vpn.example.com"},
                           "wsSettings": {"host": "vpn.example.com", "path": "/vpn"}}}


class RelayWorkerTests(unittest.TestCase):
    def setUp(self):
        self.audit = worker.load_audit_helper()
        self.temporary = tempfile.TemporaryDirectory()
        # Resolve macOS's /var alias so inputs themselves contain no symlinks.
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.key, self.known = self.directory / "key", self.directory / "known_hosts"
        for path in (self.key, self.known):
            path.write_text("synthetic-only-never-used\n")
            path.chmod(0o600)
        self.relay_config = {"ssh_command": ["/usr/bin/ssh", "-i", str(self.key), "-o",
                                            "UserKnownHostsFile=" + str(self.known)],
                             "host": "spnrelay@144.31.170.147"}
        self.config = {"routes": {"vpn-1": {"probe": outbound()}}, "xray": "/private/example/xray",
                       "sha256": "a" * 64, "control_mode": "direct_https"}
        self.request = {"action": "probe", "key": "vpn-1", "address": "8.8.8.8", "port": 443}

    def tearDown(self):
        self.temporary.cleanup()

    def test_fixed_ssh_contract_private_key_and_ipv6_destination(self):
        config = copy.deepcopy(self.relay_config)
        config.pop("host")
        config["ssh_command"] += ["-l", "spnrelay", "-o", "HostKeyAlias=spn-eu-relay", "2a12:bec4:1485:645::2"]
        args = relay_worker.ssh_arguments(self.audit, config, 23456)
        self.assertEqual(args[:6], ["/usr/bin/ssh", "-F", "/dev/null", "-N", "-D", "127.0.0.1:23456"])
        self.assertEqual(args[-1], "2a12:bec4:1485:645::2")
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ExitOnForwardFailure=yes",
                       "IdentitiesOnly=yes", "IdentityAgent=none", "PasswordAuthentication=no",
                       "KbdInteractiveAuthentication=no", "ConnectionAttempts=1", "ConnectTimeout=5"):
            self.assertIn(option, args)
        self.assertIn("HostKeyAlias=spn-eu-relay", args)
        self.assertNotIn("synthetic-test-secret", json.dumps(args))

    def test_unsafe_options_and_unprotected_files_fail_closed(self):
        for option in (["-R", "0:127.0.0.1:22"], ["-o", "StrictHostKeyChecking=no"],
                       ["-o", "ProxyCommand=anything"], ["-D", "0.0.0.0:12345"], ["-t", "bash"]):
            config = copy.deepcopy(self.relay_config)
            config["ssh_command"] += option
            with self.subTest(option=option), self.assertRaises(ValueError):
                relay_worker.ssh_arguments(self.audit, config, 23456)
        self.known.chmod(0o644)
        with self.assertRaises(ValueError):
            relay_worker.ssh_arguments(self.audit, self.relay_config, 23456)

    def test_reviewed_reverse_transport_keeps_public_logical_host_and_pinned_alias(self):
        service = '21e508f0-2ef2-47da-aee7-15dc8c66eb15'
        config = copy.deepcopy(self.relay_config)
        config.update(host='spn-ip-relay@144.31.170.147', reverse_service=service)
        config['ssh_command'] += ['-p', '22', '-o', 'HostKeyAlias=spn-ip-rotation-relay-' + service]
        args = relay_worker.ssh_arguments(self.audit, config, 23456)
        self.assertIn('HostName=127.0.0.1', args)
        self.assertEqual(args[args.index('-p') + 1], '23002')
        self.assertEqual(args[-1], 'spn-ip-relay@144.31.170.147')
        for changes in ({'reverse_service': 'other'}, {'host': 'root@144.31.170.147'},
                        {'host': 'spn-ip-relay@127.0.0.1'}):
            invalid = {**config, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                relay_worker.ssh_arguments(self.audit, invalid, 23456)
        invalid = copy.deepcopy(config)
        invalid['ssh_command'][-1] = 'HostKeyAlias=other'
        with self.assertRaises(ValueError):
            relay_worker.ssh_arguments(self.audit, invalid, 23456)
        self.known.chmod(0o600)
        self.key.unlink()
        self.key.symlink_to(self.known)
        with self.assertRaises(ValueError):
            relay_worker.ssh_arguments(self.audit, self.relay_config, 23456)

    def fake_network(self):
        reservation = MagicMock()
        reservation.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 23456)
        return reservation, MagicMock()

    def test_relay_startup_has_one_attempt_and_always_cleans_group(self):
        reservation, connected = self.fake_network()
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(relay_worker.socket, "socket", return_value=reservation), \
             patch.object(relay_worker.socket, "create_connection", return_value=connected), \
             patch.object(relay_worker.subprocess, "Popen", return_value=child) as popen, \
             patch.object(self.audit, "stop") as stop:
            with self.assertRaises(KeyboardInterrupt):
                with relay_worker.relay(self.audit, self.relay_config) as (port, started):
                    self.assertEqual(port, 23456)
                    self.assertIs(started, child)
                    raise KeyboardInterrupt
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["env"], self.audit.ENV)
        stop.assert_called_once_with(child)

    def test_relay_startup_timeout_does_not_retry_or_leak_child(self):
        reservation, _ = self.fake_network()
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(relay_worker.socket, "socket", return_value=reservation), \
             patch.object(relay_worker.socket, "create_connection", side_effect=OSError), \
             patch.object(relay_worker.time, "monotonic", side_effect=[0, 0, 9]), \
             patch.object(relay_worker.time, "sleep"), \
             patch.object(relay_worker.subprocess, "Popen", return_value=child) as popen, \
             patch.object(self.audit, "stop") as stop, self.assertRaises(RuntimeError):
            with relay_worker.relay(self.audit, self.relay_config):
                self.fail("Timed out relay must not be used")
        popen.assert_called_once()
        stop.assert_called_once_with(child)

    def test_ssh_exit_during_startup_is_unknown_and_cleaned(self):
        reservation, _ = self.fake_network()
        child = MagicMock()
        child.poll.return_value = 255
        with patch.object(relay_worker.socket, "socket", return_value=reservation), \
             patch.object(relay_worker.subprocess, "Popen", return_value=child), \
             patch.object(self.audit, "stop") as stop, \
             patch.object(self.audit, "pinned_engine"), \
             patch.object(self.audit, "test_one") as test, self.assertRaises(RuntimeError):
            relay_worker.check(self.audit, self.config, self.relay_config, self.request)
        stop.assert_called_once_with(child)
        test.assert_not_called()

    def test_cli_ssh_failure_is_unknown_without_diagnostics_or_leaked_group(self):
        reservation, _ = self.fake_network()
        child = MagicMock()
        child.poll.return_value = 255
        with patch.object(relay_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(self.request).encode()))), \
             patch.object(worker, "load_audit_helper", return_value=self.audit), \
             patch.object(self.audit, "protected_json", side_effect=[self.config, self.relay_config]), \
             patch.object(self.audit, "pinned_engine"), patch.object(self.audit, "stop") as stop, \
             patch.object(relay_worker.socket, "socket", return_value=reservation), \
             patch.object(relay_worker.subprocess, "Popen", return_value=child), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            relay_worker.main(["--config", "/private/config", "--relay-config", "/private/relay"])
        self.assertEqual(json.loads(output.getvalue()), {"status": "unknown"})
        self.assertEqual(errors.getvalue(), "")
        stop.assert_called_once_with(child)

    def test_cleanup_escalates_to_whole_group_kill_after_grace_period(self):
        child = MagicMock(pid=424242)
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("ssh", 3), 0]
        with patch.object(os, "killpg") as kill:
            self.audit.stop(child)
        self.assertEqual(kill.call_args_list, [call(424242, signal.SIGTERM), call(424242, signal.SIGKILL)])
        self.assertEqual(child.wait.call_args_list, [call(timeout=3), call(timeout=3)])

    def test_real_leaf_and_internet_control_use_same_pin_and_relay_port(self):
        child = MagicMock()
        child.poll.return_value = None
        context = MagicMock()
        context.__enter__.return_value = (23456, child)
        with patch.object(relay_worker, "relay", return_value=context), \
             patch.object(self.audit, "pinned_engine") as pin, \
             patch.object(self.audit, "test_one", return_value={"status": "passed"}) as test:
            self.assertEqual(relay_worker.check(self.audit, self.config, self.relay_config, self.request), "pass")
            selected = test.call_args.args[1]
            expected = outbound()
            expected["settings"]["vnext"][0]["address"] = "8.8.8.8"
            self.assertEqual(selected, expected)
            self.assertEqual(test.call_args.kwargs, {"transport_proxy": 23456, "direct_https": False})
            self.assertEqual(relay_worker.check(self.audit, self.config, self.relay_config,
                             {**self.request, "action": "control"}), "pass")
            self.assertIsNone(test.call_args.args[1])
            self.assertEqual(test.call_args.kwargs, {"transport_proxy": 23456, "direct_https": True})
        self.assertEqual(pin.call_count, 2)
        self.assertEqual(pin.call_args.args, (Path("/private/example/xray"), "a" * 64))

    def test_dead_relay_cannot_report_candidate_down_or_pass(self):
        child = MagicMock()
        child.poll.return_value = 255
        context = MagicMock()
        context.__enter__.return_value = (23456, child)
        with patch.object(relay_worker, "relay", return_value=context), patch.object(self.audit, "pinned_engine"):
            for result in ({"status": "passed"}, {"status": "failed", "reason": "https_timeout"}):
                with self.subTest(result=result), patch.object(self.audit, "test_one", return_value=result):
                    self.assertEqual(relay_worker.check(self.audit, self.config, self.relay_config, self.request), "unknown")

    def test_bad_pin_and_bad_leaf_never_start_ssh(self):
        with patch.object(relay_worker, "relay") as start, \
             patch.object(self.audit, "pinned_engine", side_effect=ValueError), self.assertRaises(ValueError):
            relay_worker.check(self.audit, self.config, self.relay_config, self.request)
        start.assert_not_called()
        self.config["routes"]["vpn-1"]["probe"]["proxySettings"] = {"tag": "untrusted"}
        with patch.object(relay_worker, "relay") as start, self.assertRaises(ValueError):
            relay_worker.check(self.audit, self.config, self.relay_config, self.request)
        start.assert_not_called()

    def test_main_redacts_failures_and_bounds_input(self):
        for raw in (b"x" * 4097, json.dumps(self.request).encode()):
            with patch.object(relay_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))), \
                 patch.object(worker, "load_audit_helper", return_value=self.audit), \
                 patch.object(self.audit, "protected_json", side_effect=RuntimeError("synthetic-test-secret")), \
                 redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(relay_worker.main(["--config", "/private/config", "--relay-config", "/private/relay"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"status": "unknown"})
            self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
