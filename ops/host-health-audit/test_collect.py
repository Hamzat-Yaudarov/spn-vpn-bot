"""Synthetic GET-only collection and public-projection checks; no API calls."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("host_health_collect_under_test", Path(__file__).with_name("collect.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def fixture():
    return {"hosts": [{"uuid": "host", "remark": "Public host", "address": "vpn.example", "port": 443,
                       "path": "/edge/", "sni": "vpn.example", "host": "vpn.example", "alpn": "h2",
                       "fingerprint": "firefox", "securityLayer": "TLS", "isDisabled": False, "nodes": [],
                       "inbound": {"configProfileUuid": "profile", "configProfileInboundUuid": "inbound"},
                       "serverDescription": "SECRET-MARKER"}],
            "profiles": [{"uuid": "profile", "name": "private profile", "inbounds": [{"uuid": "inbound", "tag": "tag"}],
                          "config": {"inbounds": [{"tag": "tag", "protocol": "vless", "settings": {"clients": []},
                                      "streamSettings": {"network": "xhttp", "security": "none",
                                                         "realitySettings": {"privateKey": "SECRET-MARKER"}}}]}}],
            "nodes": [{"uuid": "node", "name": "Public node", "address": "192.0.2.1", "isConnected": True,
                       "isDisabled": False, "lastStatusMessage": "SECRET-MARKER", "configProfile": {
                           "activeConfigProfileUuid": "profile", "activeInbounds": [{"uuid": "inbound",
                           "rawInbound": {"privateKey": "SECRET-MARKER"}}]}}],
            "squads": [{"uuid": "squad", "inbounds": [{"uuid": "inbound"}]}]}


class HostCollectionTests(unittest.TestCase):
    def test_default_plan_does_not_read_credentials_files_or_network(self):
        output = io.StringIO()
        with patch.object(m, "collect") as collect, patch.object(m, "api") as api, \
             patch.object(m, "private_directory") as directory, contextlib.redirect_stdout(output):
            self.assertEqual(m.main([]), 0)
        collect.assert_not_called(); api.assert_not_called(); directory.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"plan_only": True, "network": False, "mutations": 0})

    def test_inventory_never_emits_raw_config_keys_or_status_message(self):
        before = fixture(); unchanged = copy.deepcopy(before)
        public = m.inventory(before); host = public["hosts"][0]
        text = json.dumps(public)
        for secret in ("SECRET-MARKER", "rawInbound", "privateKey", "lastStatusMessage", "realitySettings", "clients"):
            self.assertNotIn(secret, text)
        self.assertEqual(before, unchanged)
        self.assertEqual(host["inbound"]["network"], "xhttp")
        self.assertTrue(host["candidateNodes"][0]["eligibleConnected"])
        self.assertFalse(host["candidateNodes"][0]["visualHostNode"])
        self.assertFalse(host["structuralErrors"]); self.assertFalse(host["deleteRecommended"])

    def test_visual_nodes_never_restrict_global_matching_node_or_imply_dead(self):
        before = fixture(); before["hosts"][0]["nodes"] = ["missing-visual-node"]
        host = m.inventory(before)["hosts"][0]
        self.assertFalse(host["structuralErrors"])
        self.assertIn("missing_visual_host_node", host["statusWarnings"])
        self.assertNotIn("no_eligible_connected_node", host["statusWarnings"])
        for name in ("isConnected", "isDisabled"):
            sample = fixture(); sample["nodes"][0][name] = name == "isDisabled"
            host = m.inventory(sample)["hosts"][0]
            self.assertIn("no_eligible_connected_node", host["statusWarnings"])
            self.assertEqual(host["healthVerdict"], "not_probed")
            self.assertFalse(host["deleteRecommended"])

    def test_dangling_profile_inbound_and_absent_binding_are_evidence_not_deletion(self):
        for mutate, code in (
            (lambda value: value["profiles"].clear(), "missing_profile"),
            (lambda value: value["profiles"][0]["inbounds"].clear(), "missing_or_ambiguous_inbound_metadata"),
            (lambda value: value["profiles"][0]["config"]["inbounds"].clear(), "missing_or_ambiguous_inbound_config"),
            (lambda value: value["nodes"][0]["configProfile"]["activeInbounds"].clear(), "no_matching_node_binding"),
        ):
            with self.subTest(code=code):
                sample = fixture(); mutate(sample); host = m.inventory(sample)["hosts"][0]
                field = "statusWarnings" if code == "no_matching_node_binding" else "structuralErrors"
                self.assertIn(code, host[field])
                if field == "statusWarnings": self.assertFalse(host["structuralErrors"])
                self.assertFalse(host["deleteRecommended"])
                self.assertEqual(host["healthVerdict"], "not_probed")

    def test_collect_uses_only_four_get_routes_and_exclusive_private_storage(self):
        raw = fixture(); calls = []
        def api(path):
            calls.append(path)
            return {"configProfiles": raw["profiles"]} if path == "/config-profiles" else \
                   {"internalSquads": raw["squads"]} if path == "/internal-squads" else raw[path[1:]]
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            # Match the production parent restriction, but use a unique base.
            base = Path(temp)
            fixed = Path("/private/tmp") / (base.name + "-collection")
            self.addCleanup(lambda: __import__("shutil").rmtree(fixed, ignore_errors=True))
            with patch.object(m, "STATE", fixed), patch.object(m, "api", side_effect=api):
                result = m.collect()
                first = Path(result["inventory"]).parent
                self.assertEqual(first, fixed)
                second = Path(m.collect()["inventory"]).parent
                self.addCleanup(lambda: __import__("shutil").rmtree(second, ignore_errors=True))
            self.assertNotEqual(first, second)
            for directory in (first, second):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                for file in directory.iterdir(): self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual(calls, ["/hosts", "/config-profiles", "/nodes", "/internal-squads"] * 2)
            self.assertIn("SECRET-MARKER", (first / "infrastructure.private.json").read_text())
            self.assertNotIn("SECRET-MARKER", (first / "inventory.json").read_text())
            self.assertNotIn("SECRET-MARKER", json.dumps(result))
            self.assertEqual(result["mutations"], result["deletions_recommended"], 0)


if __name__ == "__main__": unittest.main()
