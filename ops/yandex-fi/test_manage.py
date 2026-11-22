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


spec = importlib.util.spec_from_file_location("yandex_fi_test_subject", Path(__file__).with_name("manage.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
NEW = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HOST = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROFILE = "11111111-1111-4111-8111-111111111111"
NODE = "22222222-2222-4222-8222-222222222222"
INBOUNDS = ("33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444")
HOSTS = ("55555555-5555-4555-8555-555555555555", "66666666-6666-4666-8666-666666666666")
SCOPE = {"profile": PROFILE, "node": NODE, "inbounds": list(INBOUNDS), "hosts": list(HOSTS)}


def fixture():
    grpc, ws = sorted(set(INBOUNDS))
    old = [{"tag": "ordinary-grpc-reality-fi", "listen": "0.0.0.0", "port": 2083,
            "settings": {"clients": [], "decryption": "none"}, "streamSettings": {"network": "grpc", "realitySettings": {"privateKey": "PRIVATE-KEEP"}}},
           {"tag": "ordinary-ws-fi", "listen": "127.0.0.1", "port": 10088,
            "streamSettings": {"network": "ws", "wsSettings": {"path": "/direct/ws/"}}}]
    server = {"mode": "packet-up", "path": m.PATH, "uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
              "uplinkDataKey": "X-Transport-Data", "scMaxEachPostBytes": "2048", "serverMaxHeaderBytes": 8192}
    inbound = {"tag": m.SOURCE_TAG, "protocol": "vless", "listen": "127.0.0.1", "port": 10085,
               "settings": {"clients": [], "decryption": "none"}, "streamSettings": {"network": "xhttp", "security": "none", "xhttpSettings": server}}
    profile = {"uuid": PROFILE, "name": m.DIRECT_PROFILE_NAME, "config": {"inbounds": old,
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
    for uid, inboundid in zip(sorted(set(HOSTS)), (grpc, ws)):
        hosts.append({"uuid": uid, "address": m.IP if inboundid == grpc else m.ORIGIN, "port": 2083 if inboundid == grpc else 443, "sni": m.ORIGIN, "isDisabled": False,
                      "inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": inboundid}, "nodes": [NODE]})
    return {"profiles": [profile, source], "hosts": hosts,
            "nodes": [{"uuid": NODE, "name": m.DIRECT_NODE_NAME, "address": m.IP, "port": 2222,
                       "isConnected": True, "isDisabled": False, "lastStatusMessage": None,
                       "countryCode": "FI", "tags": ["FI_102_103", "DIRECT"],
                       "configProfile": {"activeConfigProfileUuid": PROFILE, "activeInbounds": [{"uuid": grpc}, {"uuid": ws}]}},
                      {"uuid": "source-node", "name": "source", "address": "192.0.2.2", "port": 2222,
                       "configProfile": {"activeConfigProfileUuid": m.SOURCE_PROFILE, "activeInbounds": [{"uuid": m.SOURCE_INBOUND}]}}],
            "squads": [{"uuid": "selected", "name": "Anti", "inbounds": [{"uuid": m.SOURCE_INBOUND}, {"uuid": grpc}]},
                       {"uuid": "untouched", "name": "Ordinary", "inbounds": [{"uuid": grpc}, {"uuid": ws}]}]}


def direct_records(before):
    profile = copy.deepcopy(m.one(before["profiles"], PROFILE))
    node = copy.deepcopy(m.one(before["nodes"], NODE))
    records = {"created-profile.json": profile, "created-node.json": node,
               "profile.create.json": {"name": profile["name"], "config": profile["config"]}}
    records["intent-profile.json"] = copy.deepcopy(records["profile.create.json"])
    records["intent-node.json"] = {key: copy.deepcopy(node[key]) for key in
                                  ("name", "address", "port", "countryCode", "tags", "configProfile")}
    for kind, uid in zip(("grpc", "ws"), HOSTS):
        host = copy.deepcopy(m.one(before["hosts"], uid))
        enabled = not host["isDisabled"]; host["isDisabled"] = True
        records["created-" + kind + ".json"] = host
        records["intent-" + kind + ".json"] = {k: v for k, v in host.items() if k != "uuid"}
        if enabled: records["enable-" + kind + ".json"] = {"uuid": uid, "isDisabled": False}
    records["plan.json"] = {"format": 1, "address": m.IP, "domain": m.ORIGIN,
         "profile": m.DIRECT_PROFILE_NAME, "node": m.DIRECT_NODE_NAME,
         "profileHash": m.direct_digest(profile["config"]), "sourceHostIds": m.DIRECT_SOURCE_HOSTS}
    return records


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


class YandexFinlandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.panel = FakePanel()
        self.direct = Path(self.temp.name) / "direct"
        self.direct.mkdir(mode=0o700)
        self.records = direct_records(self.panel.live)
        for name, value in self.records.items(): m.save(self.direct, name, value)

    def prepare(self): m.prepare(self.panel.live, self.state, self.direct)
    def apply(self, stage, **kwargs):
        with patch.object(m, "api", side_effect=self.panel.api): return m.apply_stage(self.state, stage, **kwargs)

    def test_default_plan_no_files_or_api(self):
        output = io.StringIO()
        with patch.object(m, "api") as api, patch.object(m, "read") as read, contextlib.redirect_stdout(output):
            self.assertEqual(m.main([]), 0)
        api.assert_not_called(); read.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertIn(m.IP, output.getvalue())
        self.assertNotIn("151.242.161.117", output.getvalue())

    def test_clone_exact_transport_preserve_direct_keys_settings_and_source(self):
        before = fixture(); original = copy.deepcopy(before); built = m.build(before, SCOPE)
        self.assertEqual(before, original)
        self.assertEqual(built["config"]["inbounds"][:2], before["profiles"][0]["config"]["inbounds"])
        self.assertEqual(built["config"]["routing"], before["profiles"][0]["config"]["routing"])
        expected = copy.deepcopy(before["profiles"][1]["config"]["inbounds"][0]); expected["tag"] = m.TAG
        self.assertEqual(built["config"]["inbounds"][2], expected)
        self.assertEqual(built["host"]["nodes"], [NODE]); self.assertTrue(built["host"]["isDisabled"])
        self.assertEqual(built["host"]["xhttpExtraParams"], before["hosts"][0]["xhttpExtraParams"])
        self.assertEqual(set(built["squads"]), {"selected"})

    def test_scope_loaded_from_direct_receipts_and_frozen_without_live_calls(self):
        before = fixture()
        self.assertEqual(m.direct_scope(self.records, before), SCOPE)
        with patch.object(m, "api") as api:
            self.prepare()
            frozen_before, candidate, scope = m.load(self.state)
        api.assert_not_called()
        self.assertEqual(scope, SCOPE); self.assertEqual(frozen_before, before)
        self.assertEqual(candidate, m.build(before, SCOPE))
        self.assertEqual(m.read(self.state / "direct-records.json"), self.records)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.state.iterdir()))

    def test_direct_receipt_mismatch_or_fresh_snapshot_drift_prevents_preparation(self):
        changes = [lambda r: r["plan.json"].update(address="144.31.168.231"),
                   lambda r: r["plan.json"].update(domain="other.wayspn.com"),
                   lambda r: r["created-profile.json"].update(uuid=m.SOURCE_PROFILE),
                   lambda r: r["intent-node.json"].update(address="192.0.2.7"),
                   lambda r: r["intent-grpc.json"].update(nodes=["another-node"]),
                   lambda r: r.pop("enable-grpc.json"),
                   lambda r: r["created-ws.json"].update(uuid=HOSTS[0])]
        for index, change in enumerate(changes):
            records = copy.deepcopy(self.records); change(records)
            with self.subTest(index=index), self.assertRaises((m.GuardError, ValueError, StopIteration)):
                m.direct_scope(records, fixture())
        before = fixture(); before["profiles"][0]["config"]["routing"]["rules"].append({"outboundTag": "changed"})
        with self.assertRaises(m.GuardError): m.direct_scope(self.records, before)
        self.assertFalse(self.state.exists())

    def test_scope_is_not_selected_by_matching_address_or_name(self):
        before = fixture()
        before["nodes"][0]["uuid"] = "77777777-7777-4777-8777-777777777777"
        with self.assertRaises(m.GuardError): m.direct_scope(self.records, before)

    def test_new_returned_uuid_set_is_supported_without_hardcoded_target_ids(self):
        mapping = {value: str(__import__("uuid").uuid4()) for value in (PROFILE, NODE, *INBOUNDS, *HOSTS)}
        def replace(value):
            text = json.dumps(value)
            for old, new in mapping.items(): text = text.replace(old, new)
            return json.loads(text)
        before, records = replace(fixture()), replace(self.records)
        scope = m.direct_scope(records, before)
        self.assertEqual(scope["profile"], mapping[PROFILE]); self.assertEqual(scope["node"], mapping[NODE])
        built = m.build(before, scope)
        self.assertEqual(built["host"]["nodes"], [mapping[NODE]])
        self.assertEqual(built["config"]["inbounds"][:2], before["profiles"][0]["config"]["inbounds"])

    def test_prepare_rejects_nonprivate_parent_before_creating_operation(self):
        exposed = Path(self.temp.name) / "exposed"; exposed.mkdir(mode=0o755)
        with self.assertRaises(m.GuardError): m.prepare(self.panel.live, exposed / "operation", self.direct)
        self.assertFalse((exposed / "operation").exists())

    def test_private_inputs_reject_public_modes_symlinks_hardlinks_and_duplicates(self):
        record = self.direct / "plan.json"
        record.chmod(0o644)
        with self.assertRaises(m.GuardError): m.read_direct(self.direct)
        record.chmod(0o600)
        link = self.direct / "alias.json"; link.symlink_to(record)
        with self.assertRaises(m.GuardError): m.read(link)
        link.unlink()
        import os
        os.link(record, link)
        with self.assertRaises(m.GuardError): m.read(record)
        with self.assertRaises(m.GuardError): m.parsed('{"same":1,"same":2}')

    def test_wrong_ip_bad_transport_overrides_or_nonempty_clients_fail_before_state(self):
        mutations = [lambda b: b["nodes"][0].update(address="151.242.161.117"),
                     lambda b: b["hosts"][0]["xhttpExtraParams"].update(uplinkHTTPMethod="POST"),
                     lambda b: b["hosts"][0]["xhttpExtraParams"].update(path="/override/"),
                     lambda b: b["profiles"][1]["config"]["inbounds"][0]["settings"]["clients"].append({"id": "NO"}),
                     lambda b: b["profiles"][1]["config"]["inbounds"][0]["streamSettings"]["xhttpSettings"].update(scMaxEachPostBytes="4096")]
        for mutate in mutations:
            before = fixture(); mutate(before)
            with self.subTest(mutate=mutate), self.assertRaises(m.GuardError): m.prepare(before, self.state, self.direct)
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
        self.assertEqual(m.one(self.panel.live["nodes"], NODE)["address"], m.IP)
        self.assertEqual(m.one(self.panel.live["profiles"], PROFILE)["config"]["inbounds"][:2], original["profiles"][0]["config"]["inbounds"])
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
               "body": {"uuid": PROFILE, "config": candidate["config"]}})
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

    def test_other_node_static_configuration_changes_block_all_writes(self):
        self.prepare(); self.panel.live["nodes"][1]["isDisabled"] = True
        with self.assertRaises(m.GuardError): self.apply("apply-profile")
        self.assertFalse(self.panel.writes)

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
