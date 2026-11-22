import copy
import socket
import unittest
from unittest.mock import patch

from spn_ip_failover import worker


def outbound(host):
    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": host, "port": 443, "users": [{"id": "test-id"}]}]},
        "streamSettings": {
            "network": "ws",
            "security": "tls",
            "tlsSettings": {"serverName": host},
            "wsSettings": {"host": host, "path": "/probe"},
        },
    }


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.audit = worker.load_audit_helper()
        self.probe = outbound("vpn.example.com")
        self.control = outbound("control.example.com")
        self.routes = {"vpn-1": {"probe": self.probe, "control": self.control}}
        self.request = {"action": "probe", "key": "vpn-1", "address": "8.8.8.8", "port": 443}

    def test_candidate_changes_only_address(self):
        expected = copy.deepcopy(self.probe)
        expected["settings"]["vnext"][0]["address"] = "8.8.8.8"
        selected = worker.select_outbound(self.audit, self.routes, self.request)
        self.assertEqual(selected, expected)
        self.assertEqual(self.probe["settings"]["vnext"][0]["address"], "vpn.example.com")

    def test_rejects_unconfigured_name_private_ip_wrong_port_and_chain(self):
        cases = ({"address": "other.example.com"}, {"address": "127.0.0.1"}, {"port": 22})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.select_outbound(self.audit, self.routes, {**self.request, **changes})
        self.probe["proxySettings"] = {"tag": "detour"}
        with self.assertRaises(ValueError):
            worker.select_outbound(self.audit, self.routes, self.request)

    def test_control_uses_own_fixed_route(self):
        selected = worker.select_outbound(self.audit, self.routes, {**self.request, "action": "control"})
        self.assertEqual(selected, self.control)

    def test_direct_https_needs_only_original_leaf_not_peer_vpn(self):
        routes = {"vpn-1": {"probe": self.probe}}
        selected = worker.select_outbound(self.audit, routes, self.request, control_mode="direct_https")
        self.assertEqual(selected["settings"]["vnext"][0]["address"], "8.8.8.8")
        self.assertIsNone(worker.select_outbound(self.audit, routes,
                          {**self.request, "action": "control"}, control_mode="direct_https"))
        with self.assertRaises(KeyError):
            worker.select_outbound(self.audit, routes, self.request)
        self.probe["proxySettings"] = {"tag": "unsupported"}
        with self.assertRaises(ValueError):
            worker.select_outbound(self.audit, routes, self.request, control_mode="direct_https")

    def test_direct_https_uses_same_pin_and_fixed_internet_control(self):
        config = {"routes": {"vpn-1": {"probe": self.probe}}, "xray": "/example/xray",
                  "sha256": "a" * 64, "control_mode": "direct_https"}
        with patch.object(self.audit, "pinned_engine") as pin, \
             patch.object(self.audit, "test_one", return_value={"status": "passed"}) as test:
            self.assertEqual(worker.check(self.audit, config, {**self.request, "action": "control"}), "pass")
        self.assertEqual(str(pin.call_args.args[0]), "/example/xray")
        self.assertEqual(pin.call_args.args[1], "a" * 64)
        self.assertIsNone(test.call_args.args[1])
        self.assertEqual(test.call_args.kwargs, {"direct_https": True})

    def test_unknown_control_mode_never_runs_a_network_check(self):
        config = {"routes": self.routes, "xray": "/example/xray", "sha256": "a" * 64,
                  "control_mode": "typo"}
        with patch.object(self.audit, "pinned_engine") as pin, patch.object(self.audit, "test_one") as test, \
             self.assertRaises(ValueError):
            worker.check(self.audit, config, self.request)
        pin.assert_not_called()
        test.assert_not_called()

    def test_dns_readback_pins_only_the_expected_address(self):
        request = {**self.request, "address": "vpn.example.com", "expected_ip": "8.8.8.8"}
        answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        other = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8888", 443, 0, 0))
        with patch.object(worker.socket, "getaddrinfo", return_value=[answer]) as resolve:
            selected = worker.select_outbound(self.audit, self.routes, request)
            resolve.assert_called_once_with("vpn.example.com", 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
            self.assertEqual(selected["settings"]["vnext"][0]["address"], "8.8.8.8")
        with patch.object(worker.socket, "getaddrinfo", return_value=[answer, other]), self.assertRaises(ValueError):
            worker.select_outbound(self.audit, self.routes, request)

    def test_local_failures_and_unexpected_http_status_are_unknown(self):
        config = {"routes": self.routes, "xray": "/example/xray", "sha256": "a" * 64}
        cases = [
            ({"status": "passed"}, "pass"),
            ({"status": "failed", "reason": "https_timeout"}, "down"),
            ({"status": "failed", "reason": "unexpected_https_status"}, "unknown"),
            ({"status": "uncovered", "reason": "engine_config_rejected"}, "unknown"),
        ]
        with patch.object(self.audit, "pinned_engine"):
            for result, expected in cases:
                with self.subTest(result=result), patch.object(self.audit, "test_one", return_value=result):
                    self.assertEqual(worker.check(self.audit, config, self.request), expected)
