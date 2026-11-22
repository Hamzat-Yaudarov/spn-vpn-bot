import io
import json
import unittest
from urllib.error import HTTPError

from spn_ip_failover.dns import CloudflareDNS, DNSConflict, DNSRecord, DNSUpdateError


OLD_ADDRESS = "192.0.2.1"
NEW_ADDRESS = "192.0.2.2"


class CloudflareDNSUpdateTests(unittest.TestCase):
    def setUp(self):
        self.record = DNSRecord("1" * 32, "2" * 32, "test.example.com")
        self.api_record = {
            "id": self.record.record_id,
            "name": "TEST.example.com.",
            "type": "A",
            "content": OLD_ADDRESS,
            "proxied": False,
        }
        self.requests = []
        self.persist_patch = True
        self.client = CloudflareDNS("test-token", opener=self.open)

    def open(self, request, *, timeout):
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        self.requests.append((method, body))
        self.assertTrue(request.full_url.endswith(f"/zones/{self.record.zone_id}/dns_records/{self.record.record_id}"))
        if method == "PATCH" and self.persist_patch:
            self.api_record.update(body)
        return io.BytesIO(json.dumps({"success": True, "result": self.api_record}).encode())

    def test_patches_only_content_reads_back_and_retries_without_writing(self):
        self.assertTrue(self.client.replace_ipv4(self.record, OLD_ADDRESS, NEW_ADDRESS))
        self.assertFalse(self.client.replace_ipv4(self.record, OLD_ADDRESS, NEW_ADDRESS))
        self.assertEqual(self.requests, [
            ("GET", None), ("PATCH", {"content": NEW_ADDRESS}), ("GET", None), ("GET", None),
        ])

    def test_refuses_to_replace_a_changed_address(self):
        self.api_record["content"] = "192.0.2.3"
        with self.assertRaises(DNSConflict):
            self.client.replace_ipv4(self.record, OLD_ADDRESS, NEW_ADDRESS)
        self.assertEqual(self.requests, [("GET", None)])

    def test_refuses_to_modify_a_wrong_name_type_or_proxied_record(self):
        original_record = dict(self.api_record)
        for field, value in (("name", "other.example.com"), ("type", "AAAA"), ("proxied", True)):
            with self.subTest(field=field):
                self.api_record = {**original_record, field: value}
                self.requests.clear()
                with self.assertRaises(DNSConflict):
                    self.client.replace_ipv4(self.record, OLD_ADDRESS, NEW_ADDRESS)
                self.assertEqual(self.requests, [("GET", None)])

    def test_requires_dns_readback_even_when_patch_returns_success(self):
        self.persist_patch = False
        with self.assertRaises(DNSConflict):
            self.client.replace_ipv4(self.record, OLD_ADDRESS, NEW_ADDRESS)
        self.assertEqual([method for method, _ in self.requests], ["GET", "PATCH", "GET"])

    def test_api_error_body_is_not_in_exception(self):
        def reject(request, *, timeout):
            raise HTTPError(request.full_url, 403, "test-token", None, io.BytesIO(b"test-token"))

        client = CloudflareDNS("test-token", opener=reject)
        with self.assertRaisesRegex(DNSUpdateError, r"^Cloudflare DNS GET failed \(HTTP 403\)$"):
            client.read_ipv4(self.record)
