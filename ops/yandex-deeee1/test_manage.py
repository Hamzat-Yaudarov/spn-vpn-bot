"""Isolated synthetic infrastructure fixtures; no real keys/API/CDN calls."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("yandex_deeee1_test_subject", Path(__file__).with_name("manage.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
NEW = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HOST = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def fixture():
    grpc, ws = sorted(m.OLD_INBOUNDS)
    old = [{"tag": "ordinary-grpc-reality-deeee1", "listen": "0.0.0.0", "port": 2083,
            "settings": {"clients": [], "decryption": "none"}, "streamSettings": {"network": "grpc", "realitySettings": {"privateKey": "PRIVATE-KEEP"}}},
           {"tag": "ordinary-ws-deeee1", "listen": "127.0.0.1", "port": 10088,
            "streamSettings": {"network": "ws", "wsSettings": {"path": "/direct/ws/"}}}]
    server = {"mode": "packet-up", "path": m.PATH, "uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
              "uplinkDataKey": "X-Transport-Data", "scMaxEachPostBytes": "2048", "serverMaxHeaderBytes": 8192}
    inbound = {"tag": m.SOURCE_TAG, "protocol": "vless", "listen": "127.0.0.1", "port": 10085,
               "settings": {"clients": [], "decryption": "none"}, "streamSettings": {"network": "xhttp", "security": "none", "xhttpSettings": server}}
    profile = {"uuid": m.PROFILE, "name": "DIRECT_DEEEE1_83_197", "config": {"inbounds": old,
               "outbounds": [{"protocol": "freedom"}], "routing": {"rules": [{"network": "udp"}]}},
               "inbounds": [{"uuid": grpc, "tag": old[0]["tag"]}, {"uuid": ws, "tag": old[1]["tag"]}]}
    source = {"uuid": m.SOURCE_PROFILE, "name": "Yandex", "config": {"inbounds": [inbound]},
              "inbounds": [{"uuid": m.SOURCE_INBOUND, "tag": m.SOURCE_TAG}]}
    sourcehost = {"uuid": m.SOURCE_HOST, "address": "de11cdn.wayspn.com", "port": 443, "path": m.PATH,
                  "sni": "de11cdn.wayspn.com", "host": "de11cdn.wayspn.com", "alpn": "h2", "fingerprint": "firefox",
                  "securityLayer": "TLS", "isDisabled": False, "isHidden": False, "viewPosition": 4,
                  "internalSquads": {"mode": "EXCLUDE", "squads": []}, "nodes": ["source-node"],
                  "xhttpExtraParams": {"uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
                     "uplinkDataKey": "X-Transport-Data", "scMaxEachPostBytes": "1024-2048"},
                  "inbound": {"configProfileUuid": m.SOURCE_PROFILE, "configProfileInboundUuid": m.SOURCE_INBOUND}}
    hosts = [sourcehost]
    for uid, inboundid in zip(sorted(m.OLD_HOSTS), (grpc, ws)):
        hosts.append({"uuid": uid, "address": m.IP, "port": 2083, "isDisabled": False,
                      "inbound": {"configProfileUuid": m.PROFILE, "configProfileInboundUuid": inboundid}, "nodes": [m.NODE]})
    return {"profiles": [profile, source], "hosts": hosts,
            "nodes": [{"uuid": m.NODE, "name": "German", "address": m.IP, "port": 2222,
                       "isConnected": True, "isDisabled": False, "lastStatusMessage": None,
                       "configProfile": {"activeConfigProfileUuid": m.PROFILE, "activeInbounds": [{"uuid": grpc}, {"uuid": ws}]}},
                      {"uuid": "source-node", "name": "source", "address": "192.0.2.2", "port": 2222,
                       "configProfile": {"activeConfigProfileUuid": m.SOURCE_PROFILE, "activeInbounds": [{"uuid": m.SOURCE_INBOUND}]}}],
            "squads": [{"uuid": "selected", "name": "Anti", "inbounds": [{"uuid": m.SOURCE_INBOUND}, {"uuid": grpc}]},
                       {"uuid": "untouched", "name": "Ordinary", "inbounds": [{"uuid": grpc}, {"uuid": ws}]}]}


class FakePanel:
    def __init__(self): self.live, self.writes, self.timeout = fixture(), [], None
    def api(self, method, path, body=None):
        key = {"/config-profiles": "profiles", "/hosts": "hosts", "/nodes": "nodes", "/internal-squads": "squads"}[path]
        if method == "GET":
            rows = copy.deepcopy(self.live[key])
            return {"configProfiles": rows} if key == "profiles" else {"internalSquads": rows} if key == "squads" else rows
        self.writes.append((method, path, copy.deepcopy(body)))
        if path == "/config-profiles":
            profile = m.one(self.live[key], body["uuid"]); profile["config"] = copy.deepcopy(body["config"])
            profile["inbounds"].append({"uuid": NEW, "tag": m.TAG})
        elif method == "POST": self.live[key].append({"uuid": HOST, **copy.deepcopy(body)})
        else:
            row = m.one(self.live[key], body["uuid"])
            row.update(copy.deepcopy(body))
            if path == "/nodes": row["configProfile"]["activeInbounds"] = [{"uuid": uid} for uid in body["configProfile"]["activeInbounds"]]
            if path == "/internal-squads": row["inbounds"] = [{"uuid": uid} for uid in body["inbounds"]]
        if self.timeout == path:
            self.timeout = None
            raise RuntimeError("SECRET-RESPONSE-LOST")
        return {}


class YandexDeeee1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.panel = FakePanel()

    def prepare(self): m.prepare(self.panel.live, self.state)
    def apply(self, stage, **kwargs):
        with patch.object(m, "api", side_effect=self.panel.api): return m.apply_stage(self.state, stage, **kwargs)

    def test_default_plan_no_files_or_api(self):
        output = io.StringIO()
        with patch.object(m, "api") as api, patch.object(m, "read") as read, contextlib.redirect_stdout(output):
            self.assertEqual(m.main([]), 0)
        api.assert_not_called(); read.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertIn("144.31.168.231", output.getvalue())
        self.assertNotIn("151.242.161.117", output.getvalue())

    def test_clone_exact_transport_preserve_direct_keys_settings_and_source(self):
        before = fixture(); original = copy.deepcopy(before); built = m.build(before)
        self.assertEqual(before, original)
        self.assertEqual(built["config"]["inbounds"][:2], before["profiles"][0]["config"]["inbounds"])
        self.assertEqual(built["config"]["routing"], before["profiles"][0]["config"]["routing"])
        expected = copy.deepcopy(before["profiles"][1]["config"]["inbounds"][0]); expected["tag"] = m.TAG
        self.assertEqual(built["config"]["inbounds"][2], expected)
        self.assertEqual(built["host"]["nodes"], [m.NODE]); self.assertTrue(built["host"]["isDisabled"])
        self.assertEqual(built["host"]["xhttpExtraParams"], before["hosts"][0]["xhttpExtraParams"])
        self.assertEqual(set(built["squads"]), {"selected"})

    def test_wrong_ip_bad_transport_overrides_or_nonempty_clients_fail_before_state(self):
        mutations = [lambda b: b["nodes"][0].update(address="151.242.161.117"),
                     lambda b: b["hosts"][0]["xhttpExtraParams"].update(uplinkHTTPMethod="POST"),
                     lambda b: b["hosts"][0]["xhttpExtraParams"].update(path="/override/"),
                     lambda b: b["profiles"][1]["config"]["inbounds"][0]["settings"]["clients"].append({"id": "NO"}),
                     lambda b: b["profiles"][1]["config"]["inbounds"][0]["streamSettings"]["xhttpSettings"].update(scMaxEachPostBytes="4096")]
        for mutate in mutations:
            before = fixture(); mutate(before)
            with self.subTest(mutate=mutate), self.assertRaises(m.GuardError): m.prepare(before, self.state)
            self.assertFalse(self.state.exists())

    def test_full_stages_disabled_until_explicit_tls_and_only_selected_squad(self):
        self.prepare(); original = copy.deepcopy(self.panel.live)
        for stage in ("apply-profile", "bind-node", "create-host", "bind-squads"):
            state = self.apply(stage); self.assertFalse(state["enabled"])
        with self.assertRaises(m.GuardError): self.apply("enable")
        state = self.apply("enable", tls_verified=True); self.assertTrue(state["enabled"])
        self.assertEqual(len(self.panel.writes), 5)
        self.assertEqual(m.one(self.panel.live["squads"], "untouched"), m.one(original["squads"], "untouched"))
        self.assertEqual(m.one(self.panel.live["hosts"], m.SOURCE_HOST), m.one(original["hosts"], m.SOURCE_HOST))
        self.assertEqual(m.one(self.panel.live["nodes"], m.NODE)["address"], m.IP)
        self.assertEqual(m.one(self.panel.live["profiles"], m.PROFILE)["config"]["inbounds"][:2], original["profiles"][0]["config"]["inbounds"])
        self.assertTrue(all(path != "/keygen" for _, path, _ in self.panel.writes))

    def test_lost_write_response_audit_recovers_without_duplicate_write(self):
        self.prepare(); self.panel.timeout = "/config-profiles"
        with self.assertRaises(RuntimeError): self.apply("apply-profile")
        self.assertTrue(self.apply("apply-profile")["profile_appended"])
        self.assertEqual(len(self.panel.writes), 1)
        self.apply("bind-node"); self.panel.timeout = "/hosts"
        with self.assertRaises(RuntimeError): self.apply("create-host")
        self.assertEqual(self.apply("create-host")["host_uuid"], HOST)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.panel.writes), 1)

    def test_intent_without_accepted_write_refuses_blind_retry(self):
        self.prepare()
        candidate = m.load(self.state)[1]
        m.save(self.state, "intent-profile.json", {"method": "PATCH", "endpoint": "/config-profiles",
               "body": {"uuid": m.PROFILE, "config": candidate["config"]}})
        with self.assertRaises(m.GuardError): self.apply("apply-profile")
        self.assertFalse(self.panel.writes)

    def test_drift_or_external_same_candidate_without_intent_cannot_advance(self):
        self.prepare()
        self.panel.live["profiles"][0]["config"]["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] = "CHANGED"
        with self.assertRaises(m.GuardError): self.apply("apply-profile")
        self.panel.live = fixture()
        self.panel.live["profiles"][0]["config"] = m.load(self.state)[1]["config"]
        self.panel.live["profiles"][0]["inbounds"].append({"uuid": NEW, "tag": m.TAG})
        with self.assertRaises((m.GuardError, FileNotFoundError)): self.apply("bind-node")

    def test_state_tampering_duplicate_prepare_and_secrets_redacted(self):
        self.prepare()
        with self.assertRaises(FileExistsError): self.prepare()
        path = self.state / "candidate.json"; value = m.read(path); value["host"]["nodes"] = ["OTHER"]
        path.write_text(json.dumps(value))
        with self.assertRaises(m.GuardError): m.load(self.state)
        output = io.StringIO()
        with patch.object(m, "apply_stage", side_effect=RuntimeError("TOKEN-SECRET")), contextlib.redirect_stderr(output):
            self.assertEqual(m.main(["apply-profile", "--confirm"]), 2)
            self.assertEqual(m.main(["--typo", "TOKEN-SECRET"]), 2)
        self.assertNotIn("TOKEN-SECRET", output.getvalue())


if __name__ == "__main__": unittest.main()
