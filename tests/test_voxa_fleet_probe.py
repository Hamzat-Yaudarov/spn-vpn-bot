"""Offline probe tests: no network, production SSH, subscriptions or Xray needed."""

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from voxa_fleet import probe
from voxa_fleet.policy import classify_health


TARGET = "8.8.8.8"
OBSERVER = "9.9.9.9"
PRIVATE_UUID = "49955017-09cf-4af0-9bf9-ed9958e67a55"
PATH = {"interface": "eth0", "source": "192.168.1.2", "backend": "Linux"}


def outbound():
    return {"protocol": "vless", "settings": {"vnext": [{"address": "old.example.com", "port": 443,
        "users": [{"id": PRIVATE_UUID, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
        "streamSettings": {"network": "raw", "security": "reality", "realitySettings": {
            "serverName": "www.cloudflare.com", "fingerprint": "safari", "publicKey": "A" * 43,
            "shortId": "1234567890abcdef"}}}


def outside_reply(**changes):
    return {"target_ip": TARGET, "expected_exit_ip": TARGET, "independent": True,
            "observer_ip": OBSERVER, "vpn_ok": True, "controls_ok": True,
            "observed_at": time.time(), **changes}


class ProbeTests(unittest.TestCase):
    def runner(self, *, outside=None, interface="eth0"):
        runner = probe.ProbeRunner("/owner/pinned/xray", "a" * 64,
                                   interface=interface, outside_command=outside)
        runner._audit.pinned_engine = MagicMock()
        return runner

    def check_with(self, runner, *, control=True, vpn=True, outside=None, ssh=False, paths=None):
        with patch.object(runner, "_physical_path", side_effect=paths or [PATH, PATH]), \
             patch.object(runner, "_control", return_value=control), \
             patch.object(runner, "_vpn", return_value=vpn), \
             patch.object(runner, "_ssh_banner", return_value=ssh), \
             patch.object(runner, "_outside", return_value=outside or []):
            return runner.check({}, TARGET, outbound=outbound())

    def test_unknown_without_independent_outside_even_if_ssh_and_vpn_pass(self):
        result = self.check_with(self.runner(), ssh=True)
        self.assertTrue(result["controls_ok"])
        self.assertTrue(result["rf"][0]["vpn_ok"])
        self.assertEqual(result["outside"], [])
        self.assertEqual(classify_health(result)["verdict"], "unknown")
        self.assertNotIn(PRIVATE_UUID, json.dumps(result))

    def test_full_authenticated_round_maps_to_policy_without_ssh_eligibility(self):
        row = {"vpn_ok": True, "controls_ok": True, "observed_at": time.time()}
        result = self.check_with(self.runner(outside=["/owner/worker"]), outside=[row], ssh=False)
        self.assertEqual(classify_health(result)["verdict"], "healthy")
        self.assertFalse(result["ssh_ok"])
        failed = self.check_with(self.runner(outside=["/owner/worker"]), vpn=False, outside=[row], ssh=True)
        self.assertEqual(classify_health(failed)["verdict"], "ru_unreachable")

    def test_direct_control_failure_and_engine_failure_are_unknown_not_down(self):
        result = self.check_with(self.runner(), control=False)
        self.assertFalse(result["controls_ok"])
        self.assertEqual(result["rf"], [])
        runner = self.runner()
        runner._audit.pinned_engine.side_effect = RuntimeError(PRIVATE_UUID)
        result = runner.check({}, TARGET, outbound=outbound())
        self.assertEqual(classify_health(result)["verdict"], "unknown")
        self.assertNotIn(PRIVATE_UUID, json.dumps(result))

    def test_path_change_invalidates_all_evidence(self):
        result = self.check_with(self.runner(), paths=[PATH, {**PATH, "source": "192.168.1.3"}])
        self.assertFalse(result["controls_ok"])
        self.assertEqual(result["rf"], [])
        self.assertEqual(result["outside"], [])

    def test_private_leaf_preserves_identity_and_only_retargets_transport(self):
        runner = self.runner()
        source = outbound()
        result = runner._leaf(source, TARGET)
        self.assertEqual(result["settings"]["vnext"][0]["address"], TARGET)
        self.assertEqual(result["settings"]["vnext"][0]["users"], source["settings"]["vnext"][0]["users"])
        self.assertEqual(result["streamSettings"], source["streamSettings"])
        self.assertEqual(source["settings"]["vnext"][0]["address"], "old.example.com")

    def test_wrong_vpn_method_or_chained_outbound_refused(self):
        runner = self.runner()
        cases = []
        for key, value in (("fingerprint", "chrome"), ("publicKey", "bad"), ("shortId", "xyz")):
            item = outbound()
            item["streamSettings"]["realitySettings"][key] = value
            cases.append(item)
        item = outbound()
        item["settings"]["vnext"][0]["users"][0]["flow"] = ""
        cases.append(item)
        item = outbound()
        item["streamSettings"]["sockopt"] = {"dialerProxy": "DIRECT"}
        cases.append(item)
        item = outbound()
        item["sendThrough"] = "127.0.0.1"
        cases.append(item)
        item = outbound()
        item["settings"]["vnext"][0]["port"] = 22
        cases.append(item)
        for item in cases:
            with self.assertRaises(probe.ProbeError):
                runner._leaf(item, TARGET)

    def test_unconfigured_preview_never_runs_network_and_is_unknown(self):
        runner = self.runner(interface=None)
        with patch.object(probe, "_bounded_run") as command:
            result = runner.check({}, TARGET, outbound=outbound())
        command.assert_not_called()
        self.assertFalse(result["controls_ok"])
        self.assertFalse(runner.configured)

    def test_tun_and_private_targets_rejected(self):
        for interface in ("tun0", "utun4", "wg0", "lo", "tap0", "eth0;sh", "eth 0"):
            with self.assertRaises(probe.ProbeError):
                self.runner(interface=interface)
        runner = self.runner()
        for target in ("127.0.0.1", "10.0.0.1", "192.168.0.1", "localhost", "::1"):
            with patch.object(runner, "_physical_path") as physical:
                result = runner.check({}, target, outbound=outbound())
            physical.assert_not_called()
            self.assertFalse(result["controls_ok"])

    def test_linux_uses_only_iproute2_route_and_link_and_validates_source(self):
        runner = self.runner()
        route = [{"dst": TARGET, "dev": "eth0", "prefsrc": "192.168.1.2"}]
        link = [{"ifname": "eth0", "link_type": "ether", "flags": ["UP", "LOWER_UP"]}]
        with patch.object(probe.platform, "system", return_value="Linux"), \
             patch.object(probe.Path, "is_file", return_value=True), \
             patch.object(probe, "_bounded_run", side_effect=[(0, json.dumps(route).encode()),
                                                             (0, json.dumps(link).encode())]) as run:
            self.assertEqual(runner._physical_path(TARGET), PATH)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][1:], ["-j", "-4", "route", "get", TARGET])
        self.assertEqual(commands[1][1:], ["-j", "link", "show", "dev", "eth0"])
        self.assertNotIn("ipconfig", json.dumps(commands))
        self.assertNotIn("/sbin/route", json.dumps(commands))

    def test_linux_tun_effective_route_and_virtual_link_are_unknown(self):
        runner = self.runner()
        for route in ([{"dev": "tun0", "prefsrc": "10.0.0.2"}],
                      [{"dev": "eth0", "prefsrc": "127.0.0.1"}], []):
            with patch.object(probe.platform, "system", return_value="Linux"), \
                 patch.object(probe.Path, "is_file", return_value=True), \
                 patch.object(probe, "_bounded_run", return_value=(0, json.dumps(route).encode())):
                with self.assertRaises(probe.ProbeError):
                    runner._physical_path(TARGET)
        route = [{"dev": "eth0", "prefsrc": "192.168.1.2"}]
        link = [{"ifname": "eth0", "link_type": "ether", "flags": ["UP"],
                 "linkinfo": {"info_kind": "wireguard"}}]
        with patch.object(probe.platform, "system", return_value="Linux"), \
             patch.object(probe.Path, "is_file", return_value=True), \
             patch.object(probe, "_bounded_run", side_effect=[(0, json.dumps(route).encode()), (0, json.dumps(link).encode())]):
            with self.assertRaises(probe.ProbeError):
                runner._physical_path(TARGET)

    def test_trace_requires_tls_https_200_exact_unique_expected_exit(self):
        self.assertTrue(probe.ProbeRunner._trace_ok(0, b"fl=abc\nip=8.8.8.8\n\n200", TARGET))
        for rc, raw in ((0, b"ip=8.8.4.4\n200"), (0, b"ip=8.8.8.8\nip=8.8.8.8\n200"),
                        (0, b"ip=8.8.8.8\n302"), (0, b"ip=127.0.0.1\n200"), (28, b"ip=8.8.8.8\n200")):
            self.assertFalse(probe.ProbeRunner._trace_ok(rc, raw, TARGET))
        args = probe.ProbeRunner._trace_args(port=12345)
        self.assertIn("--socks5-hostname", args)
        self.assertNotIn("--insecure", args)
        self.assertNotIn("--location", args)
        self.assertEqual(args[-1], probe.TRACE_URL)
        direct = probe.ProbeRunner._trace_args(interface="eth0")
        self.assertEqual(direct[direct.index("--interface") + 1], "eth0")
        self.assertEqual(direct[direct.index("--noproxy") + 1], "*")

    def test_real_engine_path_writes_private_isolated_config_and_checks_expected_exit(self):
        runner = self.runner()
        child = MagicMock()
        child.poll.return_value = None
        runner._audit.stop = MagicMock()
        reservation = MagicMock()
        reservation.__enter__.return_value = reservation
        reservation.getsockname.return_value = ("127.0.0.1", 12455)
        config_paths = []

        def run(argv, **kwargs):
            self.assertNotIn(PRIVATE_UUID, json.dumps(argv))
            if argv[0] == str(runner.engine):
                self.assertIn("-test", argv)
                config_path = Path(argv[-1])
                config_paths.append(config_path)
                self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(config_path.parent.stat().st_mode), 0o700)
                document = json.loads(config_path.read_bytes())
                self.assertEqual(len(document["outbounds"]), 1)
                selected = document["outbounds"][0]
                self.assertEqual(selected["protocol"], "vless")
                self.assertEqual(selected["sendThrough"], PATH["source"])
                self.assertEqual(selected["streamSettings"]["sockopt"], {"interface": "eth0"})
                self.assertNotIn("freedom", json.dumps(document))
                self.assertEqual(document["inbounds"][0]["listen"], "127.0.0.1")
                return 0, b""
            self.assertIn("--socks5-hostname", argv)
            self.assertEqual(argv[-1], probe.TRACE_URL)
            return 0, b"ip=8.8.8.8\n200"

        with patch.object(probe.socket, "socket", return_value=reservation), \
             patch.object(probe.socket, "create_connection", return_value=MagicMock()), \
             patch.object(probe.subprocess, "Popen", return_value=child) as spawn, \
             patch.object(probe, "_bounded_run", side_effect=run):
            self.assertTrue(runner._vpn(outbound(), PATH, TARGET))
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(spawn.call_args.kwargs["stdout"], probe.subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["stderr"], probe.subprocess.DEVNULL)
        runner._audit.stop.assert_called_once_with(child)
        self.assertTrue(config_paths)
        self.assertFalse(config_paths[0].exists())

    def test_engine_config_refusal_never_launches_proxy_or_confirms_vpn(self):
        runner = self.runner()
        runner._audit.stop = MagicMock()
        reservation = MagicMock()
        reservation.__enter__.return_value = reservation
        reservation.getsockname.return_value = ("127.0.0.1", 12455)
        with patch.object(probe.socket, "socket", return_value=reservation), \
             patch.object(probe.subprocess, "Popen") as spawn, \
             patch.object(probe, "_bounded_run", return_value=(1, PRIVATE_UUID.encode())):
            with self.assertRaises(probe.ProbeError) as caught:
                runner._vpn(outbound(), PATH, TARGET)
        self.assertNotIn(PRIVATE_UUID, str(caught.exception))
        spawn.assert_not_called()

    def test_linux_ssh_banner_is_device_bound_and_never_sends_credentials(self):
        runner = self.runner()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.return_value = b"SSH-2.0-OpenSSH_fixture\r\n"
        with patch.object(probe.socket, "socket", return_value=connection):
            self.assertTrue(runner._ssh_banner(TARGET, PATH))
        connection.setsockopt.assert_called_once_with(probe.socket.SOL_SOCKET,
            getattr(probe.socket, "SO_BINDTODEVICE", 25), b"eth0\0")
        connection.bind.assert_called_once_with((PATH["source"], 0))
        connection.connect.assert_called_once_with((TARGET, 22))
        connection.send.assert_not_called()
        connection.sendall.assert_not_called()

    def test_outside_protocol_stdin_only_fresh_bound_target_and_independent_observer(self):
        runner = self.runner(outside=["/owner/worker", "fixed-profile"])
        reply = outside_reply()
        with patch.object(probe, "_command", side_effect=lambda value: value), \
             patch.object(probe, "_bounded_run", return_value=(0, json.dumps(reply).encode())) as run:
            rows = runner._outside(TARGET, TARGET, outbound())
        args = run.call_args.args[0]
        self.assertEqual(args, ["/owner/worker", "fixed-profile"])
        self.assertNotIn(PRIVATE_UUID, json.dumps(args))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["outbound"], outbound())
        self.assertNotIn(PRIVATE_UUID, json.dumps(rows))
        self.assertEqual(rows[0]["vpn_ok"], True)

    def test_outside_cannot_be_target_or_stale_ignored_filter_or_ssh_tunnel(self):
        runner = self.runner(outside=["/owner/worker"])
        cases = [outside_reply(observer_ip=TARGET), outside_reply(independent=False),
                 outside_reply(target_ip="8.8.4.4"), outside_reply(expected_exit_ip="8.8.4.4"),
                 outside_reply(observed_at=time.time() - 1000), outside_reply(vpn_ok=1),
                 outside_reply(controls_ok=None), outside_reply(observer_ip="127.0.0.1")]
        for reply in cases:
            with patch.object(probe, "_command", side_effect=lambda value: value), \
                 patch.object(probe, "_bounded_run", return_value=(0, json.dumps(reply).encode())):
                with self.assertRaises(probe.ProbeError):
                    runner._outside(TARGET, TARGET, outbound())
        for command in (["/usr/bin/ssh", "-D", "1234", "host"], ["/bin/sh", "-c", "anything"],
                        "arbitrary command", ["/owner/wrapper", "contains spaces"]):
            with self.assertRaises((probe.ProbeError, OSError)):
                probe._command(command)

    def test_readiness_hashes_engine_but_performs_no_private_probe(self):
        runner = self.runner(outside=["/owner/worker"])
        with patch.object(probe.platform, "system", return_value="Linux"), \
             patch.object(probe, "_command", return_value=["/owner/worker"]), \
             patch.object(probe, "_bounded_run") as run:
            result = runner.readiness()
        run.assert_not_called()
        self.assertTrue(result["configured"])
        self.assertTrue(result["engine_verified"])
        self.assertTrue(result["linux_backend"])

    def test_pinned_asset_verification_is_real_and_no_download_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            os.chmod(directory, 0o700)
            executable = directory / "xray"
            executable.write_bytes(b"offline-test-fixture-not-an-engine")
            executable.chmod(0o700)
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            runner = probe.ProbeRunner(executable, digest, interface="eth0")
            with patch.object(probe.platform, "system", return_value="Linux"):
                self.assertTrue(runner.readiness()["engine_verified"])
            executable.chmod(0o777)
            with patch.object(probe.platform, "system", return_value="Linux"):
                self.assertFalse(runner.readiness()["engine_verified"])

    def test_bounded_process_io_limit_and_timeout_without_secret_diagnostics(self):
        # These are offline local commands, never Xray or network operations.
        rc, raw = probe._bounded_run(["/usr/bin/printf", "safe-fixture"], timeout=2, limit=64)
        self.assertEqual((rc, raw), (0, b"safe-fixture"))
        with self.assertRaises(probe.ProbeError):
            probe._bounded_run(["/usr/bin/printf", "X" * 1000], timeout=2, limit=10)
        with self.assertRaises(probe.ProbeError) as caught:
            probe._bounded_run(["/bin/sleep", "1"], timeout=.01)
        self.assertNotIn(PRIVATE_UUID, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
