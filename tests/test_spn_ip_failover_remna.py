import io
import json
import unittest
from urllib.error import HTTPError

from spn_ip_failover.remna import Remnawave, RemnawaveDriftError, RemnawaveError


RESOURCE_ID = "27a79da4-1f06-45f7-91a5-2059849c6080"
OLD_IP = "192.0.2.10"
NEW_IP = "192.0.2.11"


class MockOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *, timeout):
        payload = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), request.full_url, payload))
        response = next(self.responses)
        if isinstance(response, HTTPError):
            raise response
        return io.BytesIO(json.dumps({"response": response}).encode())


class RemnawaveTests(unittest.TestCase):
    def panel(self, *responses):
        panel = Remnawave("https://panel.example.test/api/", "fake-test-token")
        panel._opener = MockOpener(responses)
        return panel

    def resource(self, address=OLD_IP, *, disabled=False):
        return {"uuid": RESOURCE_ID, "address": address, "isDisabled": disabled, "isConnected": True}

    def test_host_address_change_preserves_disabled_host_and_verifies_with_get(self):
        before = self.resource(disabled=True)
        after = self.resource(NEW_IP, disabled=True)
        panel = self.panel(before, after, after)

        self.assertTrue(panel.replace_host_address(RESOURCE_ID, OLD_IP, NEW_IP))
        base = "https://panel.example.test/api/hosts"
        self.assertEqual(panel._opener.requests, [
            ("GET", f"{base}/{RESOURCE_ID}", None),
            ("PATCH", base, {"uuid": RESOURCE_ID, "address": NEW_IP}),
            ("GET", f"{base}/{RESOURCE_ID}", None),
        ])

    def test_node_address_change_is_idempotent_and_refuses_drift(self):
        panel = self.panel(self.resource(NEW_IP), self.resource("192.0.2.12"))
        self.assertFalse(panel.replace_node_address(RESOURCE_ID, OLD_IP, NEW_IP))
        with self.assertRaises(RemnawaveDriftError):
            panel.replace_node_address(RESOURCE_ID, OLD_IP, NEW_IP)
        self.assertEqual([method for method, _, _ in panel._opener.requests], ["GET", "GET"])

    def test_host_disable_requires_expected_address_and_checks_write(self):
        panel = self.panel(self.resource(NEW_IP), self.resource(), self.resource(disabled=True), self.resource())
        with self.assertRaises(RemnawaveDriftError):
            panel.set_host_disabled(RESOURCE_ID, False, True, expected_address=OLD_IP)
        with self.assertRaises(RemnawaveDriftError):
            panel.set_host_disabled(RESOURCE_ID, False, True, expected_address=OLD_IP)
        self.assertEqual(panel._opener.requests[2], (
            "PATCH", "https://panel.example.test/api/hosts", {"uuid": RESOURCE_ID, "isDisabled": True},
        ))

    def test_redirect_error_does_not_expose_response_body(self):
        error = HTTPError("https://panel.example.test/api/nodes", 302, "redirect", {}, io.BytesIO(b"private body"))
        panel = self.panel(error)
        with self.assertRaisesRegex(RemnawaveError, r"^Remnawave GET returned HTTP 302$"):
            panel.read_node(RESOURCE_ID)


if __name__ == "__main__":
    unittest.main()
