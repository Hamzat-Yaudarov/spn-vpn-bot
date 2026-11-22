"""Synthetic tests only: no DNS, network, panel, or server calls."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location("host_health_probe", Path(__file__).with_name("probe.py"))
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def host(**changes):
    value = {"uuid": "c1614c56-403d-4a67-b917-928a04600db8", "name": "Example VPN",
             "address": "edge.example.com", "port": 443, "sni": "edge.example.com", "alpn": "h2,http/1.1",
             "securityLayer": "TLS", "inbound": {"network": "xhttp", "protocol": "vless", "security": "none"}}
    value.update(changes)
    return value


class ProbeTests(unittest.TestCase):
    def test_default_has_no_io(self):
        with patch.object(probe, "execute") as execute, patch("builtins.print"):
            self.assertEqual(probe.main([]), 0)
        execute.assert_not_called()

    def test_special_and_private_addresses_rejected(self):
        for value in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "fc00::1",
                      "ff02::1", "::ffff:8.8.8.8", "8.8.8.8%eth0", "localhost", "{{ADDRESS}}", "x.local", "x.internal", "https://edge.example.com"):
            with self.subTest(value=value), patch.object(probe.subprocess, "run") as run:
                status, addresses = probe.resolve_public(value)
                self.assertEqual(status, "unsafe_or_invalid_address")
                self.assertEqual(addresses, [])
                run.assert_not_called()

    def test_public_literal_needs_no_dns(self):
        with patch.object(probe.subprocess, "run") as run:
            self.assertEqual(probe.resolve_public("8.8.8.8"), ("literal_public_ip", ["8.8.8.8"]))
        run.assert_not_called()

    def test_dns_filters_private_and_pins_public(self):
        response = subprocess.CompletedProcess([], 0, json.dumps(["127.0.0.1", "10.0.0.1", "8.8.8.8", "8.8.8.8"]), "")
        with patch.object(probe.subprocess, "run", return_value=response) as run:
            self.assertEqual(probe.resolve_public("edge.example.com"), ("resolved_public", ["8.8.8.8"]))
        self.assertEqual(run.call_args.kwargs["timeout"], 5.0)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_dns_timeout_has_constant_error(self):
        with patch.object(probe.subprocess, "run", side_effect=subprocess.TimeoutExpired("private contents", 5)):
            self.assertEqual(probe.resolve_public("edge.example.com"), ("dns_timeout", []))

    def test_udp_skipped_before_resolution(self):
        for network in ("hysteria2", "quic", "kcp", "tuic"):
            with self.subTest(network=network), patch.object(probe, "resolve_public") as resolve:
                result = probe.probe_host(host(inbound={"network": network, "protocol": "hysteria2", "security": "tls"}))
                self.assertEqual(result["result"], "unsupported_udp")
                self.assertFalse(result["deleteRecommended"])
                resolve.assert_not_called()

    def test_reality_uses_tcp_only_even_if_tls_layer(self):
        value = host(inbound={"network": "grpc", "protocol": "vless", "security": "reality"})
        self.assertEqual(probe.transport_mode(value), "reality_tcp_only")
        with patch.object(probe, "resolve_public", return_value=("resolved_public", ["8.8.8.8"])), \
             patch.object(probe, "probe_address", return_value={"tcp": "connected", "tls": "not_tested"}) as call:
            result = probe.probe_host(value)
        self.assertEqual(call.call_args.args[2], "reality_tcp_only")
        self.assertEqual(result["result"], "tcp_reachable_not_vpn_verified")
        self.assertFalse(result["e2eVerified"])

    def test_tls_uses_sni_without_sni_dns(self):
        value = host(address="8.8.8.8", sni="cdn.example.com")
        with patch.object(probe, "resolve_public", return_value=("literal_public_ip", ["8.8.8.8"])) as resolve, \
             patch.object(probe, "probe_address", return_value={"tcp": "connected", "tls": "verified"}) as call:
            result = probe.probe_host(value)
        resolve.assert_called_once_with("8.8.8.8")
        self.assertEqual(call.call_args.args[3], "cdn.example.com")
        self.assertEqual(result["result"], "tls_reachable_not_vpn_verified")
        self.assertFalse(result["e2eVerified"])

    def test_connection_is_numeric_and_tls_verified(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        encrypted = MagicMock()
        encrypted.__enter__.return_value = encrypted
        encrypted.selected_alpn_protocol.return_value = "h2"
        context = MagicMock()
        context.wrap_socket.return_value = encrypted
        with patch.object(probe.socket, "socket", return_value=connection), \
             patch.object(probe, "tls_context", return_value=context):
            result = probe.probe_address("8.8.8.8", 443, "verified_tls", "cdn.example.com", ["h2"])
        connection.connect.assert_called_once_with(("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(connection, server_hostname="cdn.example.com")
        self.assertEqual(result["tls"], "verified")
        self.assertEqual(result["alpn"], "h2")

    def test_tls_failure_does_not_mean_vpn_dead(self):
        with patch.object(probe, "resolve_public", return_value=("resolved_public", ["8.8.8.8"])), \
             patch.object(probe, "probe_address", return_value={"tcp": "connected", "tls": "certificate_invalid"}):
            result = probe.probe_host(host())
        self.assertEqual(result["result"], "tcp_reachable_tls_unverified")
        self.assertFalse(result["deleteRecommended"])

    def test_all_failed_are_network_local_evidence_only(self):
        with patch.object(probe, "resolve_public", return_value=("resolved_public", ["8.8.8.8"])), \
             patch.object(probe, "probe_address", return_value={"tcp": "timeout", "tls": "not_tested"}):
            result = probe.probe_host(host())
        self.assertEqual(result["result"], "unreachable_from_audit_network")
        self.assertFalse(result["deleteRecommended"])

    def test_invalid_port_never_resolves(self):
        for port in (0, 65536, True, "443", None):
            with self.subTest(port=port), patch.object(probe, "resolve_public") as resolve:
                self.assertEqual(probe.probe_host(host(port=port))["result"], "unsafe_or_invalid_port")
                resolve.assert_not_called()

    def test_alpn_and_names_restricted(self):
        self.assertEqual(probe.alpn_protocols("evil,h2,http/1.1,h2"), ["h2", "http/1.1"])
        self.assertIsNone(probe.tls_name(host(sni="{{ SNI }}"), "edge.example.com"))
        self.assertIsNone(probe.hostname("-bad.example.com"))

    def test_private_report_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            destination = directory / "round1.json"
            probe.write_report(destination, {"safe": True})
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(probe.ProbeError):
                probe.write_report(destination, {})

    def test_input_schema_rejects_raw_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "inventory.json"
            source.write_text(json.dumps({"format": 1, "hosts": [host()], "profiles": []}))
            source.chmod(0o600)
            with self.assertRaises(probe.ProbeError):
                probe.load_inventory(source)


if __name__ == "__main__":
    unittest.main()
