import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from spn_ip_failover.dns import DNSConflict, DNSUpdateError
from spn_ip_failover.regru_dns import ENDPOINT, RegRuDNS, RegRuRecord, RegRuRejected
from spn_ip_failover.state import Journal


OLD = "8.8.8.8"
NEW = "9.9.9.9"


class Backend:
    def __init__(self):
        self.rows = [{"subname": "pol", "rectype": "A", "content": OLD},
                     {"subname": "pol", "rectype": "TXT", "content": "preserve"},
                     {"subname": "other", "rectype": "AAAA", "content": "2001:4860:4860::8888"}]
        self.calls = []
        self.add_timeout = None
        self.remove_timeout = False
        self.reject_add = False
        self.drift_after_add = False
        self.wrong_zone = False
        self.expected_remove_content = OLD

    def __call__(self, request, *, timeout):
        function = request.full_url.removeprefix(ENDPOINT)
        fields = parse_qs(request.data.decode())
        payload = json.loads(fields["input_data"][0])
        assert request.method == "POST"
        assert request.full_url.startswith(ENDPOINT) and "password" not in request.full_url
        assert fields["input_format"] == ["json"] and fields["output_format"] == ["json"]
        assert payload["domains"] == [{"dname": "tekst.site"}]
        self.calls.append((function, payload))
        if function == "zone/add_alias":
            if self.reject_add:
                self.reject_add = False
                return io.BytesIO(b'{"result":"error","error_code":"ACCESS_DENIED_FROM_IP"}')
            fail, self.add_timeout = self.add_timeout, None
            if fail == "before":
                raise OSError("unconfirmed transport")
            self.rows.append({"subname": payload["subdomain"], "rectype": "A", "content": payload["ipaddr"]})
            if self.drift_after_add:
                self.rows.append({"subname": "pol", "rectype": "A", "content": "1.1.1.1"})
            if fail == "after":
                raise OSError("unconfirmed transport")
        elif function == "zone/remove_record":
            assert payload["record_type"] == "A" and payload["content"] == self.expected_remove_content
            self.rows = [row for row in self.rows if not (
                row["subname"] == payload["subdomain"] and row["rectype"] == "A" and row["content"] == payload["content"])]
            if self.remove_timeout:
                self.remove_timeout = False
                raise OSError("unconfirmed transport")
        elif function != "zone/get_resource_records":
            raise AssertionError("Unexpected operation")
        domain = {"dname": "wrong.site" if self.wrong_zone else "tekst.site", "result": "success"}
        if function == "zone/get_resource_records":
            domain["rrs"] = self.rows
        return io.BytesIO(json.dumps({"result": "success", "answer": {"domains": [domain]}}).encode())

    def count(self, function):
        return sum(name == function for name, _ in self.calls)


class RegRuDNSTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.backend = Backend()
        self.record = RegRuRecord("tekst.site", "pol", "pol.tekst.site")
        self.client = self.new_client()

    def new_client(self):
        return RegRuDNS("test-user", "test-password", journal_path=self.root / "journal", opener=self.backend)

    def test_exact_record_identity_and_generic_router_record(self):
        generic = SimpleNamespace(zone_id="tekst.site", record_id="pol", name="pol.tekst.site")
        self.assertEqual(self.client.read_ipv4(generic), OLD)
        self.assertEqual(RegRuRecord("tekst.site", "@", "tekst.site").subdomain, "@")
        self.assertEqual(RegRuRecord("tekst.site", "a.b", "a.b.tekst.site").subdomain, "a.b")
        for values in (("tekst.site", "wrong", "pol.tekst.site"),
                       ("tekst.site", "pol", "pol.tekst.site.evil"),
                       ("tekst.site", "*", "*.tekst.site")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RegRuRecord(*values)

    def test_add_first_then_remove_exact_old_preserves_other_records(self):
        preserved = self.backend.rows[1:]
        self.assertTrue(self.client.replace_ipv4(self.record, OLD, NEW))
        self.assertEqual([name for name, _ in self.backend.calls], [
            "zone/get_resource_records", "zone/add_alias", "zone/get_resource_records",
            "zone/remove_record", "zone/get_resource_records",
        ])
        self.assertTrue(all(row in self.backend.rows for row in preserved))
        self.assertEqual(self.client.read_ipv4(self.record), NEW)
        self.assertFalse(self.client.replace_ipv4(self.record, OLD, NEW))
        self.assertEqual(self.backend.count("zone/add_alias"), 1)
        self.assertEqual(self.backend.count("zone/remove_record"), 1)

    def test_rejects_ambiguous_a_aaaa_alias_or_wrong_zone_before_writes(self):
        original = self.backend.rows.copy()
        for extra in ({"subname": "pol", "rectype": "A", "content": "1.1.1.1"},
                      {"subname": "pol", "rectype": "A", "content": OLD},
                      {"subname": "pol", "rectype": "AAAA", "content": "2001:4860:4860::8888"},
                      {"subname": "pol", "rectype": "CNAME", "content": "example.com"}):
            self.backend.rows = [*original, extra]
            with self.subTest(extra=extra), self.assertRaises(DNSConflict):
                self.client.replace_ipv4(self.record, OLD, NEW)
        self.backend.rows = original
        self.backend.wrong_zone = True
        with self.assertRaises(DNSConflict):
            self.client.replace_ipv4(self.record, OLD, NEW)
        self.assertEqual(self.backend.count("zone/add_alias"), 0)
        self.assertEqual(self.backend.count("zone/remove_record"), 0)

    def test_restart_recovers_add_timeout_only_after_exact_readback(self):
        self.backend.add_timeout = "after"
        with self.assertRaises(DNSUpdateError):
            self.client.replace_ipv4(self.record, OLD, NEW)
        self.assertTrue(self.new_client().replace_ipv4(self.record, OLD, NEW))
        self.assertEqual(self.backend.count("zone/add_alias"), 1)
        self.assertEqual(self.backend.count("zone/remove_record"), 1)
        self.assertEqual(self.client.read_ipv4(self.record), NEW)

    def test_unconfirmed_add_is_not_repeated_and_another_request_is_blocked(self):
        self.backend.add_timeout = "before"
        with self.assertRaises(DNSUpdateError):
            self.client.replace_ipv4(self.record, OLD, NEW)
        with self.assertRaises(DNSUpdateError):
            self.new_client().replace_ipv4(self.record, OLD, NEW)
        with self.assertRaises(DNSConflict):
            self.new_client().replace_ipv4(self.record, OLD, "1.1.1.1")
        self.assertEqual(self.backend.count("zone/add_alias"), 1)
        self.assertEqual(self.backend.count("zone/remove_record"), 0)

    def test_completed_delete_after_timeout_is_confirmed_without_repeating_it(self):
        self.backend.remove_timeout = True
        with self.assertRaises(DNSUpdateError):
            self.client.replace_ipv4(self.record, OLD, NEW)
        self.assertFalse(self.new_client().replace_ipv4(self.record, OLD, NEW))
        self.assertEqual(self.backend.count("zone/remove_record"), 1)
        self.assertEqual(Journal(self.root / "journal").snapshot()["servers"][self.record.name]["phase"], "complete")

    def test_explicit_api_rejection_can_resume_when_access_is_restored(self):
        self.backend.reject_add = True
        with self.assertRaises(RegRuRejected):
            self.client.replace_ipv4(self.record, OLD, NEW)
        self.assertEqual(self.client.read_ipv4(self.record), OLD)
        self.assertTrue(self.new_client().replace_ipv4(self.record, OLD, NEW))

    def test_concurrent_drift_after_add_never_removes_the_old_record(self):
        self.backend.drift_after_add = True
        with self.assertRaises(DNSConflict):
            self.client.replace_ipv4(self.record, OLD, NEW)
        self.assertEqual(self.backend.count("zone/remove_record"), 0)
        self.assertIn({"subname": "pol", "rectype": "A", "content": OLD}, self.backend.rows)

    def test_no_mutation_without_private_journal(self):
        readonly = RegRuDNS("test-user", "test-password", opener=self.backend)
        self.assertEqual(readonly.read_ipv4(self.record), OLD)
        with self.assertRaises(DNSUpdateError):
            readonly.replace_ipv4(self.record, OLD, NEW)
        self.assertEqual(self.backend.count("zone/add_alias"), 0)

    def test_resume_without_pending_or_after_completion_does_no_api_calls(self):
        self.assertFalse(self.client.resume_ipv4(self.record, {OLD, NEW}))
        self.assertEqual(self.backend.calls, [])
        self.client.replace_ipv4(self.record, OLD, NEW)
        before = len(self.backend.calls)
        self.assertFalse(self.new_client().resume_ipv4(self.record, {OLD, NEW}))
        self.assertEqual(len(self.backend.calls), before)

    def test_resume_recovers_only_exact_owned_pending_pair_and_releases_lock(self):
        self.backend.add_timeout = "after"
        with self.assertRaises(DNSUpdateError):
            self.client.replace_ipv4(self.record, OLD, NEW)
        self.assertTrue(self.new_client().resume_ipv4(self.record, {OLD, NEW}))
        self.assertEqual(self.client.read_ipv4(self.record), NEW)
        self.assertEqual(self.backend.count("zone/add_alias"), 1)
        self.assertEqual(self.backend.count("zone/remove_record"), 1)

    def test_resume_cannot_republish_forward_after_a_writer_rolls_back_at_its_first_unlock(self):
        self.backend.add_timeout = "after"
        with self.assertRaises(DNSUpdateError):
            self.client.replace_ipv4(self.record, OLD, NEW)
        self.assertEqual(self.backend.count("zone/add_alias"), 1)
        parent = self
        ran = False

        class RollbackAtFirstUnlock(Journal):
            def __exit__(inner, *args):
                nonlocal ran
                super().__exit__(*args)
                if ran:
                    return
                ran = True
                # A competing owner gets the lock immediately when resume first
                # releases it, finishes the pending operation, then rolls back.
                # With the old unlock-then-replace implementation, resume would
                # subsequently create a third, unwanted forward transaction.
                other = parent.new_client()
                other.replace_ipv4(parent.record, OLD, NEW)
                parent.backend.expected_remove_content = NEW
                other.replace_ipv4(parent.record, NEW, OLD)
                parent.backend.expected_remove_content = OLD

        with patch("spn_ip_failover.regru_dns.Journal", RollbackAtFirstUnlock):
            self.assertTrue(self.new_client().resume_ipv4(self.record, {OLD, NEW}))
        self.assertTrue(ran)
        self.assertEqual(self.client.read_ipv4(self.record), OLD)
        self.assertEqual(self.backend.count("zone/add_alias"), 2)
        self.assertEqual(self.backend.count("zone/remove_record"), 2)
        saved = Journal(self.root / "journal").snapshot()["servers"][self.record.name]
        self.assertEqual((saved["expected"], saved["replacement"], saved["phase"]), (NEW, OLD, "complete"))

    def test_resume_rejects_a_foreign_pair_or_domain_without_api_calls(self):
        self.backend.add_timeout = "after"
        with self.assertRaises(DNSUpdateError):
            self.client.replace_ipv4(self.record, OLD, NEW)
        before = len(self.backend.calls)
        for allowed in ({OLD, "1.1.1.1"}, [OLD, OLD], [OLD, "not-an-ip"], None):
            with self.subTest(allowed=allowed), self.assertRaises(DNSConflict):
                self.new_client().resume_ipv4(self.record, allowed)
        with Journal(self.root / "journal") as journal:
            journal.data["servers"][self.record.name]["domain"] = "other.site"
            journal.save()
        with self.assertRaises(DNSConflict):
            self.new_client().resume_ipv4(self.record, {OLD, NEW})
        self.assertEqual(len(self.backend.calls), before)

    def test_resume_preserves_a_pending_rollback_direction(self):
        self.client.replace_ipv4(self.record, OLD, NEW)
        self.backend.expected_remove_content = NEW
        self.backend.add_timeout = "after"
        with self.assertRaises(DNSUpdateError):
            self.client.replace_ipv4(self.record, NEW, OLD)
        self.assertTrue(self.new_client().resume_ipv4(self.record, {OLD, NEW}))
        self.assertEqual(self.client.read_ipv4(self.record), OLD)
        self.assertEqual(self.backend.count("zone/add_alias"), 2)
        self.assertEqual(self.backend.count("zone/remove_record"), 2)

    def test_resume_does_not_repeat_an_unconfirmed_add_or_adopt_an_unknown_phase(self):
        self.backend.add_timeout = "before"
        with self.assertRaises(DNSUpdateError):
            self.client.replace_ipv4(self.record, OLD, NEW)
        with self.assertRaises(DNSUpdateError):
            self.new_client().resume_ipv4(self.record, {OLD, NEW})
        self.assertEqual(self.backend.count("zone/add_alias"), 1)
        self.assertEqual(self.backend.count("zone/remove_record"), 0)
        self.backend.rows[0]["content"] = NEW
        with Journal(self.root / "journal") as journal:
            journal.data["servers"][self.record.name]["phase"] = "unknown"
            journal.save()
        with self.assertRaises(DNSConflict):
            self.new_client().resume_ipv4(self.record, {OLD, NEW})


if __name__ == "__main__":
    unittest.main()
