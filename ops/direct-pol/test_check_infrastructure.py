"""Offline checker contracts: no sockets, secrets, SSH or panel calls."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("direct_pol_checks", Path(__file__).with_name("check_infrastructure.py"))
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class Stream:
    def __init__(self, response=b""):
        self.response = response
        self.sent = b""

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def settimeout(self, value): self.timeout = value
    def sendall(self, value): self.sent += value
    def recv(self, amount):
        result, self.response = self.response[:amount], self.response[amount:]
        return result
    def version(self): return "TLSv1.3"
    def selected_alpn_protocol(self): return "h2"
    def close(self): pass


class InfrastructureCheckerTests(unittest.TestCase):
    def test_health_uses_fixed_host_with_trusted_ip_tls_and_expected_body(self):
        body = b'{"status":"ok"}\n'
        stream = Stream(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        with patch.object(checks, "trusted_tls", return_value=stream) as tls:
            self.assertEqual(checks.health(), "trusted_tls_expected_health")
        self.assertEqual(tls.call_args.args[:2], (443, ["http/1.1"]))
        self.assertIn(b"Host: pol.tekst.site\r\n", stream.sent)
        self.assertNotIn(b"Authorization", stream.sent)

    def test_ws_uses_random_16_byte_nonce_and_checks_exact_accept(self):
        nonce = base64.b64encode(b"w" * 16).decode()
        accept = base64.b64encode(hashlib.sha1((nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        stream = Stream(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        with patch.object(checks.secrets, "token_bytes", return_value=b"w" * 16) as random, patch.object(checks, "trusted_tls", return_value=stream):
            self.assertIn("101_correct_accept", checks.websocket())
        random.assert_called_once_with(16)
        self.assertTrue(stream.sent.startswith(b"GET /direct/ws/ HTTP/1.1\r\n"))
        stream = Stream(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: WRONG\r\n\r\n")
        with patch.object(checks, "trusted_tls", return_value=stream), self.assertRaises(checks.ProbeFailure):
            checks.websocket()

    def test_reality_requires_tls13_and_h2(self):
        stream = Stream()
        with patch.object(checks, "trusted_tls", return_value=stream) as tls:
            checks.reality_fallback()
        self.assertEqual(tls.call_args.args[:2], (2083, ["h2"]))
        stream.version = lambda: "TLSv1.2"
        with patch.object(checks, "trusted_tls", return_value=stream), self.assertRaises(checks.ProbeFailure):
            checks.reality_fallback()

    def test_trusted_tls_pins_ip_sni_certifi_and_bounded_timeout(self):
        context, raw = Mock(), Mock()
        with patch.object(checks.ssl, "create_default_context", return_value=context) as factory, patch.object(checks.certifi, "where", return_value="public-ca-bundle"), patch.object(checks.socket, "create_connection", return_value=raw) as connect:
            checks.trusted_tls(443, ["http/1.1"], checks.time.monotonic() + 5)
        factory.assert_called_once_with(cafile="public-ca-bundle")
        self.assertEqual(connect.call_args.args, (("191.217.245.62", 443),))
        self.assertLessEqual(connect.call_args.kwargs["timeout"], 5)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="pol.tekst.site")

    def test_exposed_api_tls_or_ws_upstream_fails(self):
        context = Mock()
        context.wrap_socket.return_value = Stream()
        with patch.object(checks.socket, "create_connection", return_value=Stream()), patch.object(checks.ssl, "SSLContext", return_value=context), self.assertRaisesRegex(checks.ProbeFailure, "node_api_tls_exposed"):
            checks.node_api_not_tls()
        with patch.object(checks.socket, "create_connection", return_value=Stream()), self.assertRaisesRegex(checks.ProbeFailure, "ws_loopback_upstream_exposed"):
            checks.ws_upstream_closed()

    def test_closed_ports_and_deadline(self):
        with patch.object(checks.socket, "create_connection", side_effect=TimeoutError):
            self.assertEqual(checks.node_api_not_tls(), "no_external_tcp_response")
            self.assertEqual(checks.ws_upstream_closed(), "no_external_tcp_response")
        with self.assertRaises(TimeoutError):
            checks.remaining(checks.time.monotonic() - 1)

    def test_json_and_failures_never_echo_arbitrary_remote_error(self):
        patches = [patch.object(checks, name, return_value="keyless_check") for name in ("websocket", "reality_fallback", "node_api_not_tls", "ws_upstream_closed")]
        for mocked in patches: mocked.start(); self.addCleanup(mocked.stop)
        with patch.object(checks, "health", side_effect=RuntimeError("PRIVATE-ERROR-NOT-OUTPUT")), patch("sys.stdout", new_callable=io.StringIO) as output:
            code = checks.main(["--json"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertFalse(result["dns_checked"])
        self.assertFalse(result["subscriber_traffic_tested"])
        self.assertFalse(result["firewall_rules_audited"])
        self.assertNotIn("PRIVATE-ERROR", output.getvalue())
        self.assertEqual(result["checks"][0]["detail"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
