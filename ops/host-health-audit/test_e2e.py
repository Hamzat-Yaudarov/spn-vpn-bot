"""Synthetic-only tests: no engine, resolver or network is ever invoked."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("isolated_e2e", Path(__file__).with_name("e2e.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
SECRET = "synthetic-credential-do-not-print"


def outbound(**updates):
    value = {"tag": "Test VPN", "protocol": "vless", "settings": {"vnext": [
        {"address": "vpn.example.org", "port": 443, "users": [{"id": SECRET, "encryption": "none"}]}]},
        "streamSettings": {"network": "ws", "security": "tls", "tlsSettings": {"serverName": "vpn.example.org"},
                           "wsSettings": {"path": "/vpn/"}}}
    value.update(updates)
    return value


def host(**updates):
    value = {"uuid": "host-1", "name": "Test VPN", "address": "vpn.example.org", "port": 443,
             "path": "/vpn/", "sni": "vpn.example.org", "securityLayer": "DEFAULT",
             "inbound": {"protocol": "vless", "network": "ws", "security": "tls"}}
    value.update(updates)
    return value


class Child:
    pid = 424242
    def __init__(self): self.exited = False
    def poll(self): return 1 if self.exited else None


class E2ETests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
    def tearDown(self): self.temporary.cleanup()

    def test_default_plan_is_pure(self):
        with mock.patch.object(audit, "protected_json", side_effect=AssertionError), \
             mock.patch.object(audit.subprocess, "run", side_effect=AssertionError), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(audit.main([]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"plan_only": True, "network": False, "deletions": 0})

    def test_single_config_has_no_original_fallback_dns_or_balancer(self):
        original = outbound(mux={"enabled": False}, proxySettings={})
        result = audit.single_config(original, 12345)
        self.assertEqual(len(result["outbounds"]), 1)
        self.assertEqual(result["outbounds"][0]["settings"], original["settings"])
        self.assertEqual(result["routing"]["rules"][0]["outboundTag"], "audit-selected")
        self.assertNotIn("dns", result)
        self.assertNotIn("balancers", result["routing"])
        self.assertEqual(result["inbounds"][0]["listen"], "127.0.0.1")
        self.assertFalse(result["inbounds"][0]["settings"]["udp"])
        self.assertNotIn("mux", result["outbounds"][0])
        self.assertEqual(original["tag"], "Test VPN")

    def test_chains_mux_templates_and_insecure_transport_are_uncovered(self):
        samples = [(outbound(proxySettings={"tag": "other"}), "dependent_or_bound_outbound"),
                   (outbound(mux={"enabled": True}), "unsupported_mux"),
                   ({"protocol": "freedom", "tag": "direct"}, "unsupported_or_template_outbound")]
        insecure = outbound()
        insecure["streamSettings"]["tlsSettings"]["allowInsecure"] = True
        samples.append((insecure, "insecure_transport"))
        for value, expected in samples:
            self.assertEqual(audit.endpoint(value)[1], expected)
        linked = outbound()
        linked["streamSettings"]["sockopt"] = {"dialerProxy": "other"}
        self.assertEqual(audit.endpoint(linked)[1], "dependent_or_bound_outbound")
        split = outbound()
        split["streamSettings"]["xhttpSettings"] = {"downloadSettings": {"address": "other.example.org"}}
        self.assertEqual(audit.endpoint(split)[1], "separate_download_endpoint")

    def test_relay_preserves_original_transport_and_credentials_without_fallback(self):
        original = outbound()
        original["streamSettings"]["sockopt"] = {"tcpFastOpen": True}
        before = copy.deepcopy(original)
        result = audit.single_config(original, 12345, transport_proxy=23456)
        self.assertEqual(original, before)
        leaf, relay = result["outbounds"]
        expected = copy.deepcopy(before)
        expected["tag"] = "audit-selected"
        expected["streamSettings"]["sockopt"]["dialerProxy"] = "spn-eu-relay"
        self.assertEqual(leaf, expected)
        self.assertEqual(relay, {"tag": "spn-eu-relay", "protocol": "socks", "settings": {
            "servers": [{"address": "127.0.0.1", "port": 23456}]}})
        self.assertEqual(result["routing"]["rules"], [{"type": "field", "inboundTag": ["audit-socks"],
                         "outboundTag": "audit-selected"}])
        self.assertNotIn("dns", result)

    def test_relay_validates_original_and_rejects_udp_and_existing_chain(self):
        chain = outbound()
        chain["streamSettings"]["sockopt"] = {"dialerProxy": "untrusted"}
        udp = outbound()
        udp["streamSettings"]["network"] = "kcp"
        for value in (chain, udp, {"protocol": "freedom"}):
            with self.subTest(value=value), self.assertRaises(audit.E2EError):
                audit.single_config(value, 12345, transport_proxy=23456)
        for port in (True, 22, 65536, "23456"):
            with self.subTest(port=port), self.assertRaises(audit.E2EError):
                audit.single_config(outbound(), 12345, transport_proxy=port)

    def test_internet_controls_have_no_client_credentials(self):
        direct = audit.single_config(None, 12345, direct_https=True)
        self.assertEqual(direct["outbounds"], [{"tag": "audit-selected", "protocol": "freedom", "settings": {}}])
        outside = audit.single_config(None, 12345, direct_https=True, transport_proxy=23456)
        self.assertEqual(outside["outbounds"], [{"tag": "audit-selected", "protocol": "socks", "settings": {
            "servers": [{"address": "127.0.0.1", "port": 23456}]}}])
        self.assertNotIn(SECRET, json.dumps(direct) + json.dumps(outside))
        with self.assertRaises(audit.E2EError):
            audit.single_config(outbound(), 12345, direct_https=True)

    def test_private_loopback_and_special_literal_endpoints_are_uncovered(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "[::1]", "localhost", "x.local"):
            value = outbound()
            value["settings"]["vnext"][0]["address"] = address
            self.assertEqual(audit.endpoint(value)[1], "non_public_endpoint")

    def test_hysteria_is_only_a_candidate_until_engine_accepts_it(self):
        value = {"tag": "HY2", "protocol": "hysteria2", "settings": {"address": "hy.example.org", "port": 443},
                 "streamSettings": {"network": "hysteria", "security": "tls", "tlsSettings": {"serverName": "hy.example.org"}}}
        endpoint, reason = audit.endpoint(value)
        self.assertIsNone(reason)
        self.assertEqual(endpoint["protocol"], "hysteria2")

    def test_mapping_rejects_path_security_sni_drift_and_ambiguity(self):
        metadata, _ = audit.endpoint(outbound())
        self.assertEqual(audit.matching(metadata, outbound(), [host()])[0]["uuid"], "host-1")
        for changed in (host(path="/other/"), host(securityLayer="REALITY"), host(sni="other.example.org"),
                        host(inbound={"network": "grpc", "protocol": "vless", "security": "tls"})):
            self.assertEqual(audit.matching(metadata, outbound(), [changed]), [])
        no_path = outbound(); no_path["streamSettings"]["wsSettings"].pop("path")
        self.assertEqual(audit.matching(audit.endpoint(no_path)[0], no_path, [host()]), [])
        plan, _ = audit.jobs({"outbounds": [outbound()]}, {"hosts": [host(), host(uuid="host-2")]})
        self.assertEqual(plan[0]["uncoveredReason"], "ambiguous_or_missing_host_mapping")
        self.assertIsNone(plan[0]["outbound"])

    def test_protected_inputs_and_engine_pin_fail_closed(self):
        private = self.directory / "config.json"
        audit.write_private(private, {"outbounds": []})
        self.assertEqual(audit.protected_json(private), {"outbounds": []})
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        private.chmod(0o644)
        with self.assertRaises(audit.E2EError): audit.protected_json(private)
        engine = self.directory / "engine"
        engine.write_bytes(b"not-an-engine-never-run"); engine.chmod(0o700)
        digest = hashlib.sha256(engine.read_bytes()).hexdigest()
        audit.pinned_engine(engine, digest)
        with self.assertRaises(audit.E2EError): audit.pinned_engine(engine, "0" * 64)
        engine.chmod(0o722)
        with self.assertRaises(audit.E2EError): audit.pinned_engine(engine, digest)

    def test_duplicate_json_and_symlink_inputs_are_rejected(self):
        private = self.directory / "bad.json"
        private.write_bytes(b'{"a":1,"a":2}'); private.chmod(0o600)
        with self.assertRaises(audit.E2EError): audit.protected_json(private)
        link = self.directory / "link.json"; link.symlink_to(private)
        with self.assertRaises(audit.E2EError): audit.protected_json(link)

    def test_curl_is_fixed_verified_remote_dns_and_env_contains_no_credentials(self):
        args = audit.curl_args(12345)
        self.assertEqual(args[:2], ["/usr/bin/curl", "--disable"])
        self.assertEqual(args[args.index("--socks5-hostname") + 1], "127.0.0.1:12345")
        self.assertEqual(args[-1], "https://www.gstatic.com/generate_204")
        self.assertNotIn("--insecure", args); self.assertNotIn("-k", args)
        self.assertEqual(audit.ENV, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/nonexistent", "LANG": "C.UTF-8"})

    def run_fake(self, *, code=0, body=b"204 0.123", reject=False, die=False):
        calls, child = [], Child()
        def run(args, **kwargs):
            calls.append((args, kwargs))
            self.assertNotIn(SECRET, json.dumps(args) + json.dumps(kwargs["env"]))
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            if args[0] != audit.CURL:
                saved = json.loads(Path(args[-1]).read_bytes())
                self.assertIn(SECRET, json.dumps(saved))
                self.assertEqual(len(saved["outbounds"]), 1)
                self.assertEqual(stat.S_IMODE(Path(args[-1]).stat().st_mode), 0o600)
                return subprocess.CompletedProcess(args, 1 if reject else 0)
            if die: child.exited = True
            return subprocess.CompletedProcess(args, code, body)
        reservation = mock.MagicMock()
        reservation.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
        connected = mock.MagicMock()
        with mock.patch.object(audit.socket, "socket", return_value=reservation), \
             mock.patch.object(audit.socket, "create_connection", return_value=connected), \
             mock.patch.object(audit.subprocess, "run", side_effect=run), \
             mock.patch.object(audit.subprocess, "Popen", return_value=child) as popen, \
             mock.patch.object(audit, "stop") as stop:
            result = audit.test_one(Path("/private/pinned-xray"), outbound(), self.directory, 0)
        self.assertFalse((self.directory / "config-0.json").exists())
        stop.assert_called_once_with(None if reject else child)
        if not reject:
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertNotIn(SECRET, json.dumps(popen.call_args.args[0]))
        return result, calls

    def test_mocked_auth_success_keeps_all_secrets_in_private_config(self):
        previous = os.umask(0)
        try: result, calls = self.run_fake()
        finally: os.umask(previous)
        self.assertEqual(result, {"status": "passed", "reason": "https_204", "elapsedMs": 123})
        self.assertEqual(len(calls), 2)
        for path in self.directory.iterdir(): self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_core_rejection_is_uncovered_not_host_failure(self):
        result, _ = self.run_fake(reject=True)
        self.assertEqual(result, {"status": "uncovered", "reason": "engine_config_rejected"})

    def test_local_socks_failure_is_uncovered(self):
        result, _ = self.run_fake(code=7)
        self.assertEqual(result, {"status": "uncovered", "reason": "local_socks_unavailable"})

    def test_core_exit_during_curl_is_uncovered(self):
        result, _ = self.run_fake(code=28, die=True)
        self.assertEqual(result, {"status": "uncovered", "reason": "engine_exited_during_probe"})

    def test_inspect_never_executes_or_exposes_outbound_credentials(self):
        config, inventory = self.directory / "config.json", self.directory / "inventory.json"
        audit.write_private(config, {"outbounds": [outbound(), {"protocol": "freedom"}]})
        audit.write_private(inventory, {"hosts": [host()]})
        with mock.patch.object(audit, "pinned_engine", side_effect=AssertionError), \
             mock.patch.object(audit, "test_one", side_effect=AssertionError):
            result = audit.execute(config, inventory, None, None, inspect=True)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["counts"], {"passed": 0, "failed": 0, "uncovered": 2})
        self.assertEqual(result["uncoveredHosts"], ["host-1"])
        self.assertIn("not deletion decisions", result["notice"])

    def test_errors_are_redacted(self):
        with mock.patch.object(audit, "execute", side_effect=RuntimeError(SECRET)), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(audit.main(["--inspect", "--config", "/x", "--inventory", "/y"]), 2)
        self.assertNotIn(SECRET, errors.getvalue())


if __name__ == "__main__": unittest.main()
