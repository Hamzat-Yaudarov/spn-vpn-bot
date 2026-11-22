from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spn_ip_failover import monitor


def outbound(host):
    return {"tag": "fixture", "protocol": "vless",
            "settings": {"vnext": [{"address": host, "port": 443, "users": [{"id": "private-test-key"}]}]},
            "streamSettings": {"network": "ws", "security": "tls",
                               "tlsSettings": {"serverName": host}, "wsSettings": {"path": "/test"}}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.audit = monitor.load_audit_helper()
        self.first = outbound("first.example.com")
        self.second = outbound("second.example.com")
        self.config = {"xray": "/fixture/xray", "sha256": "a" * 64,
                       "routes": {"one": {"probe": self.first, "control": self.second},
                                  "two": {"probe": self.second, "control": self.first}}}

    def test_unique_leaves_are_tested_once_and_report_contains_no_credentials(self):
        duplicate = deepcopy(self.first)
        duplicate["tag"] = "another-name"
        self.config["routes"]["three"] = {"probe": duplicate, "control": self.second}

        def check(_engine, leaf, _directory, _number):
            host = leaf["settings"]["vnext"][0]["address"]
            return {"status": "passed"} if host.startswith("first") else {"status": "failed", "reason": "https_timeout"}

        with patch.object(self.audit, "pinned_engine") as pin, patch.object(self.audit, "test_one", side_effect=check) as test:
            result = monitor.collect(self.audit, self.config)
        pin.assert_called_once()
        self.assertEqual(test.call_count, 2)
        self.assertEqual(result["routes"]["one"], {"status": "pass", "control": "down"})
        self.assertEqual(result["routes"]["two"], {"status": "down", "control": "pass"})
        self.assertEqual(result["counts"], {"pass": 2, "down": 1, "unknown": 0})
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_same_endpoint_or_mismatched_control_remains_unknown(self):
        self.config["routes"] = {"one": {"probe": self.first, "control": deepcopy(self.first)}}
        with patch.object(self.audit, "pinned_engine"), patch.object(self.audit, "test_one", return_value={"status": "passed"}) as test:
            result = monitor.collect(self.audit, self.config)
        self.assertEqual(test.call_count, 1)
        self.assertEqual(result["routes"]["one"], {"status": "pass", "control": "unknown"})
        self.config["routes"]["one"]["control"] = {"protocol": "invalid"}
        with patch.object(self.audit, "pinned_engine"), patch.object(self.audit, "test_one", side_effect=RuntimeError("private-test-key")):
            result = monitor.collect(self.audit, self.config)
        self.assertEqual(result["routes"]["one"], {"status": "unknown", "control": "unknown"})

    def test_unusable_probe_is_not_sent_to_engine(self):
        self.config["routes"] = {"unsupported": {"probe": {"protocol": "invalid"}, "control": self.second}}
        with patch.object(self.audit, "pinned_engine"), patch.object(self.audit, "test_one") as test:
            result = monitor.collect(self.audit, self.config)
        test.assert_not_called()
        self.assertEqual(result["uniqueTests"], 0)
        self.assertEqual(result["routes"]["unsupported"], {"status": "unknown", "control": "unknown"})

    def test_direct_internet_control_is_shared_without_peer_controls(self):
        self.config["control_mode"] = "direct_https"
        duplicate = deepcopy(self.first)
        duplicate["tag"] = "same-leaf-other-name"
        self.config["routes"] = {"one": {"probe": self.first}, "two": {"probe": self.second},
                                 "duplicate": {"probe": duplicate},
                                 "unsupported": {"probe": {"protocol": "invalid"}}}

        def check(_engine, leaf, _directory, _number, **options):
            if leaf is None:
                self.assertEqual(options, {"direct_https": True})
                return {"status": "passed"}
            self.assertEqual(options, {})
            self.assertIn(leaf["settings"]["vnext"][0]["address"], ("first.example.com", "second.example.com"))
            return {"status": "failed", "reason": "https_timeout"}

        with patch.object(self.audit, "pinned_engine") as pin, \
             patch.object(self.audit, "test_one", side_effect=check) as test:
            result = monitor.collect(self.audit, self.config)
        pin.assert_called_once()
        self.assertEqual(test.call_count, 3)
        self.assertEqual(result["uniqueTests"], 3)
        self.assertEqual(sum(call.args[1] is None for call in test.call_args_list), 1)
        self.assertEqual(result["routes"], {
            "one": {"status": "down", "control": "pass"},
            "two": {"status": "down", "control": "pass"},
            "duplicate": {"status": "down", "control": "pass"},
            "unsupported": {"status": "unknown", "control": "pass"}})
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_direct_control_failure_is_shared_as_unknown(self):
        self.config["control_mode"] = "direct_https"

        def check(_engine, leaf, _directory, _number, **_options):
            if leaf is None:
                raise RuntimeError("private-test-key")
            return {"status": "passed"}

        with patch.object(self.audit, "pinned_engine"), patch.object(self.audit, "test_one", side_effect=check):
            result = monitor.collect(self.audit, self.config)
        self.assertTrue(all(row == {"status": "pass", "control": "unknown"} for row in result["routes"].values()))
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_unsupported_control_mode_fails_before_pin_or_network(self):
        self.config["control_mode"] = "unsupported"
        with patch.object(self.audit, "pinned_engine") as pin, patch.object(self.audit, "test_one") as test, \
             self.assertRaises(ValueError):
            monitor.collect(self.audit, self.config)
        pin.assert_not_called()
        test.assert_not_called()

    def test_snapshot_atomic_private_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            path = root / "latest.json"
            monitor.save_snapshot(path, {"format": 1, "routes": {}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            monitor.save_snapshot(path, {"format": 1, "routes": {"new": {"status": "pass"}}})
            self.assertEqual(json.loads(path.read_text())["routes"]["new"]["status"], "pass")
            alias = root / "alias.json"
            alias.symlink_to(path)
            with self.assertRaises(ValueError):
                monitor.save_snapshot(alias, {"bad": True})
            os.chmod(root, 0o755)
            with self.assertRaises(ValueError):
                monitor.save_snapshot(path, {"bad": True})


if __name__ == "__main__":
    unittest.main()
