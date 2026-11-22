"""Offline scoped DE4 gRPC/TLS deployment guards; no real API or keys."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("grpc_tls_de4_test_subject", Path(__file__).with_name("manage.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

PROFILE = "60714af0-bc08-44e5-bbb3-9aa0d2dedb53"
NODE = "f87b14cf-438a-4c61-acc1-7522491c05ae"
SOURCE_HOST = "421b6c60-88e2-4aca-980f-e5cf8114794d"
OLD = ("70fab348-62d3-4913-b484-25f755857ded", "fe91ea9f-554d-4d13-bf1b-0428e13f4f0d",
       "33333333-3333-4333-8333-333333333333")
HOSTS = (SOURCE_HOST, "7875fb42-cba6-4593-9f1c-ced29a9a48cc", "44444444-4444-4444-8444-444444444444")
NEW_INBOUND = "55555555-5555-4555-8555-555555555555"
NEW_HOST = "66666666-6666-4666-8666-666666666666"
SQUADS = ("77777777-7777-4777-8777-777777777777", "88888888-8888-4888-8888-888888888888",
          "99999999-9999-4999-8999-999999999999")
OTHER_PROFILE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_NODE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_HOST = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER_INBOUND = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
IP, DOMAIN = "194.48.250.102", "deeee1.wayspn.com"
TAG, SERVICE = "ordinary-grpc-tls-de4-test", "grpc-de4-tls-test"
OLD_TAGS = ("ordinary-grpc-reality-deeee1", "ordinary-ws-deeee1", "vless-xhttp-yandex-deeee1")
TARGET_PRIVATE = "SYNTHETIC-TARGET-REALITY-KEY-KEEP"
UNRELATED_PRIVATE = "UNRELATED-PROFILE-CONFIG-MUST-NOT-SAVE"
NODE_PRIVATE = "NODE-SERVICE-KEY-MUST-NOT-SAVE"
ERROR_SECRET = "ARBITRARY-PRIVATE-API-ERROR-NOT-FOR-OUTPUT"


def fixture():
    inbounds = [
        {"tag": OLD_TAGS[0], "protocol": "vless", "listen": "0.0.0.0", "port": 2083,
         "settings": {"clients": [], "decryption": "none"},
         "streamSettings": {"network": "grpc", "security": "reality",
             "grpcSettings": {"serviceName": "grpc"},
             "realitySettings": {"privateKey": TARGET_PRIVATE, "serverNames": [DOMAIN],
                                 "shortIds": ["0123456789abcdef"], "target": "127.0.0.1:443"}}},
        {"tag": OLD_TAGS[1], "protocol": "vless", "listen": "127.0.0.1", "port": 10088,
         "settings": {"clients": [], "decryption": "none"},
         "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": "/direct/ws/"}}},
        {"tag": OLD_TAGS[2], "protocol": "vless", "listen": "127.0.0.1", "port": 10085,
         "settings": {"clients": [], "decryption": "none"},
         "streamSettings": {"network": "xhttp", "security": "none", "xhttpSettings": {"mode": "packet-up",
             "path": "/assets/app/", "uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
             "uplinkDataKey": "X-Transport-Data", "scMaxEachPostBytes": "2048", "serverMaxHeaderBytes": 8192}}},
    ]
    metadata = [{"uuid": uid, "tag": tag} for uid, tag in zip(OLD, OLD_TAGS)]
    profile = {"uuid": PROFILE, "name": "DIRECT_DEEEE1_83_197", "inbounds": metadata,
               "config": {"inbounds": inbounds, "outbounds": [{"tag": "direct", "protocol": "freedom"},
                          {"tag": "block", "protocol": "blackhole"}],
                          "routing": {"domainStrategy": "AsIs", "rules": [{"type": "field",
                              "inboundTag": [OLD_TAGS[0]], "network": "udp", "outboundTag": "block"}]},
                          "dns": {"servers": ["1.1.1.1"]}}}
    node = {"uuid": NODE, "name": "current-de4-name-is-not-renamed", "address": IP, "port": 2222,
            "isConnected": True, "isDisabled": False, "lastStatusMessage": None,
            "countryCode": "DE", "tags": ["CURRENT", "KEEP"], "note": "Keep existing node metadata",
            "secretKey": NODE_PRIVATE,
            "configProfile": {"activeConfigProfileUuid": PROFILE, "activeInbounds": deepcopy(metadata)}}
    hosts = []
    for index, (uid, iid, kind) in enumerate(zip(HOSTS, OLD, ("grpc", "ws", "xhttp"))):
        hosts.append({"uuid": uid, "remark": "Existing DE4 " + kind,
            "address": IP if index == 0 else DOMAIN if index == 1 else "cdn-deeee1.wayspn.com",
            "port": 2083 if index == 0 else 443, "sni": DOMAIN if index != 2 else "cdn-deeee1.wayspn.com",
            "host": None if index == 0 else DOMAIN if index == 1 else "cdn-deeee1.wayspn.com",
            "path": ("grpc", "/direct/ws/", "/assets/app/")[index], "alpn": "h2" if index != 1 else "http/1.1",
            "fingerprint": "chrome", "securityLayer": "DEFAULT" if index == 0 else "TLS",
            "isDisabled": False, "isHidden": False, "viewPosition": index, "updatedAt": "2026-10-31T00:00:00Z",
            "internalSquads": {"mode": "EXCLUDE", "squads": []}, "nodes": [NODE],
            "inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": iid},
            "mapper": None, "xrayJsonTemplateUuid": None})
    unrelated_profile = {"uuid": OTHER_PROFILE, "name": "UNRELATED", "inbounds": [{"uuid": OTHER_INBOUND, "tag": "other"}],
                         "config": {"inbounds": [], "privateMarker": UNRELATED_PRIVATE}}
    unrelated_node = {"uuid": OTHER_NODE, "name": "Unrelated", "address": "192.0.2.20", "port": 2222,
                      "secretKey": NODE_PRIVATE, "configProfile": {"activeConfigProfileUuid": OTHER_PROFILE,
                          "activeInbounds": [{"uuid": OTHER_INBOUND}]}}
    hosts.append({"uuid": OTHER_HOST, "remark": "Unrelated host", "address": "other.example.com", "port": 443,
                  "isDisabled": False, "nodes": [OTHER_NODE], "viewPosition": 4, "updatedAt": "2026-10-31T00:00:00Z",
                  "inbound": {"configProfileUuid": OTHER_PROFILE, "configProfileInboundUuid": OTHER_INBOUND}})
    return {"profiles": [profile, unrelated_profile], "nodes": [node, unrelated_node], "hosts": hosts,
            "squads": [{"uuid": SQUADS[0], "name": "gRPC only", "inbounds": [{"uuid": OLD[0]}]},
                       {"uuid": SQUADS[1], "name": "All current", "inbounds": [{"uuid": uid} for uid in (*OLD, OTHER_INBOUND)]},
                       {"uuid": SQUADS[2], "name": "WS only", "inbounds": [{"uuid": OLD[1]}]}]}


class FakePanel:
    def __init__(self):
        self.live = fixture()
        self.calls = []
        self.writes = []
        self.fail_after = None
        self.fail_before = None
        self.before_write = None

    def api(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, deepcopy(body)))
        key = {"/config-profiles": "profiles", "/nodes": "nodes", "/hosts": "hosts", "/internal-squads": "squads"}[endpoint]
        if method == "GET":
            value = deepcopy(self.live[key])
            return {"configProfiles": value} if key == "profiles" else {"internalSquads": value} if key == "squads" else value
        if self.before_write is not None: self.before_write(method, endpoint, body)
        self.writes.append((method, endpoint, deepcopy(body)))
        if self.fail_before == (method, endpoint):
            self.fail_before = None
            raise RuntimeError(ERROR_SECRET)
        if method == "POST":
            assert endpoint == "/hosts"
            row = {"uuid": NEW_HOST, **deepcopy(body)}
            self.live[key].append(row)
        else:
            assert method == "PATCH"
            row = next(row for row in self.live[key] if row["uuid"] == body["uuid"])
            if endpoint == "/config-profiles":
                row["config"] = deepcopy(body["config"])
                by_tag = {metadata["tag"]: metadata for metadata in row["inbounds"]}
                by_tag[TAG] = {"uuid": NEW_INBOUND, "tag": TAG}
                row["inbounds"] = [deepcopy(by_tag[inbound["tag"]]) for inbound in row["config"]["inbounds"]]
            elif endpoint == "/internal-squads":
                row["inbounds"] = [{"uuid": uid} for uid in body["inbounds"]]
            elif endpoint == "/nodes":
                row.update(deepcopy(body))
                row["configProfile"]["activeInbounds"] = [{"uuid": uid} if isinstance(uid, str) else uid
                                                          for uid in body["configProfile"]["activeInbounds"]]
            else: row.update(deepcopy(body))
        if self.fail_after == (method, endpoint):
            self.fail_after = None
            raise RuntimeError(ERROR_SECRET)
        return deepcopy(row)


class BuildTests(unittest.TestCase):
    def setUp(self): self.live = fixture()

    def rejected(self, change):
        live = deepcopy(self.live); change(live)
        with self.assertRaises(ValueError) as error: m.build(live)
        self.assertNotIn(TARGET_PRIVATE, str(error.exception))
        self.assertNotIn(UNRELATED_PRIVATE, str(error.exception))
        self.assertNotIn(NODE_PRIVATE, str(error.exception))

    def test_fixed_scope_constants(self):
        for key, expected in (("IP", IP), ("DOMAIN", DOMAIN), ("PROFILE", PROFILE), ("NODE", NODE),
                              ("SOURCE_HOST", SOURCE_HOST), ("TAG", TAG), ("SERVICE", SERVICE), ("PORT", 10089)):
            with self.subTest(key=key): self.assertEqual(getattr(m, key), expected)

    def test_build_preserves_entire_current_config_and_does_not_mutate_inputs(self):
        original = deepcopy(self.live); built = m.build(self.live)
        self.assertEqual(self.live, original)
        config = deepcopy(built["config"]); added = config["inbounds"].pop()
        self.assertEqual(config, self.live["profiles"][0]["config"])
        self.assertEqual(added["tag"], TAG)
        self.assertEqual(len(built["config"]["inbounds"]), 4)

    def test_new_inbound_is_loopback_vless_plain_grpc_not_reality_or_tls(self):
        inbound = m.build(self.live)["config"]["inbounds"][-1]
        self.assertEqual((inbound["protocol"], inbound["listen"], inbound["port"]), ("vless", "127.0.0.1", 10089))
        self.assertEqual(inbound["settings"], {"clients": [], "decryption": "none"})
        stream = inbound["streamSettings"]
        self.assertEqual((stream["network"], stream["security"]), ("grpc", "none"))
        self.assertEqual(stream["grpcSettings"]["serviceName"], SERVICE)
        self.assertNotIn("realitySettings", stream); self.assertNotIn("tlsSettings", stream)
        self.assertNotIn(TARGET_PRIVATE, json.dumps(inbound))

    def test_new_host_is_disabled_exact_public_tls_h2_endpoint(self):
        host = m.build(self.live)["new_host"]
        self.assertEqual((host["address"], host["port"], host["sni"], host["host"]), (DOMAIN, 443, DOMAIN, DOMAIN))
        self.assertEqual((host["path"], host["alpn"], host["securityLayer"]), (SERVICE, "h2", "TLS"))
        self.assertIs(host["isDisabled"], True)
        self.assertEqual(host["nodes"], [NODE])
        self.assertNotIn("mapper", host); self.assertIsNone(host["xrayJsonTemplateUuid"])
        self.assertIs(host["mihomoX25519"], False)

    def test_new_host_does_not_copy_source_mapper_or_mihomo_x25519(self):
        self.live["hosts"][0].update(mapper={"foreign": True}, mihomoX25519=True)
        host = m.build(self.live)["new_host"]
        self.assertNotIn("mapper", host)
        self.assertIs(host["mihomoX25519"], False)

    def test_shuffle_host_copies_actual_booleans_only(self):
        for value in (False, True, None, 0, 1, "false", {}):
            with self.subTest(value=value):
                self.live["hosts"][0]["shuffleHost"] = value
                host = m.build(self.live)["new_host"]
                if type(value) is bool:
                    self.assertIs(host["shuffleHost"], value)
                else:
                    self.assertNotIn("shuffleHost", host)

    def test_mirrors_only_squads_containing_source_grpc_and_preserves_their_members(self):
        built = m.build(self.live)
        self.assertEqual(built["squads"], {SQUADS[0]: [OLD[0]], SQUADS[1]: [*OLD, OTHER_INBOUND]})
        self.assertNotIn(SQUADS[2], built["squads"])
        self.assertEqual(set(built["scope"]["old_inbounds"]), set(OLD))
        self.assertEqual(set(built["scope"]["old_hosts"]), set(HOSTS))

    def test_wrong_ip_rejected(self): self.rejected(lambda s: s["nodes"][0].update(address="192.0.2.99"))
    def test_wrong_node_port_rejected(self): self.rejected(lambda s: s["nodes"][0].update(port=3333))
    def test_wrong_node_profile_rejected(self): self.rejected(lambda s: s["nodes"][0]["configProfile"].update(activeConfigProfileUuid=OTHER_PROFILE))
    def test_missing_old_active_inbound_rejected(self): self.rejected(lambda s: s["nodes"][0]["configProfile"]["activeInbounds"].pop())
    def test_missing_old_config_inbound_rejected(self): self.rejected(lambda s: s["profiles"][0]["config"]["inbounds"].pop())
    def test_changed_old_tag_rejected(self): self.rejected(lambda s: s["profiles"][0]["config"]["inbounds"][0].update(tag="ordinary-foreign"))
    def test_duplicate_profile_identity_rejected(self): self.rejected(lambda s: s["profiles"].append(deepcopy(s["profiles"][0])))
    def test_duplicate_source_host_rejected(self): self.rejected(lambda s: s["hosts"].append(deepcopy(s["hosts"][0])))
    def test_wrong_source_host_inbound_rejected(self): self.rejected(lambda s: s["hosts"][0]["inbound"].update(configProfileInboundUuid=OLD[1]))
    def test_wrong_source_host_profile_rejected(self): self.rejected(lambda s: s["hosts"][0]["inbound"].update(configProfileUuid=OTHER_PROFILE))
    def test_wrong_source_host_node_rejected(self): self.rejected(lambda s: s["hosts"][0].update(nodes=[OTHER_NODE]))
    def test_wrong_source_grpc_path_rejected(self): self.rejected(lambda s: s["hosts"][0].update(path="foreign-service"))
    def test_wrong_source_grpc_security_rejected(self): self.rejected(lambda s: s["profiles"][0]["config"]["inbounds"][0]["streamSettings"].update(security="tls"))
    def test_wrong_source_grpc_port_rejected(self): self.rejected(lambda s: s["profiles"][0]["config"]["inbounds"][0].update(port=443))
    def test_wrong_source_grpc_service_rejected(self): self.rejected(lambda s: s["profiles"][0]["config"]["inbounds"][0]["streamSettings"]["grpcSettings"].update(serviceName="foreign"))
    def test_nonempty_source_clients_rejected(self): self.rejected(lambda s: s["profiles"][0]["config"]["inbounds"][0]["settings"].update(clients=[{"id": ERROR_SECRET}]))
    def test_existing_new_tag_rejected(self): self.rejected(lambda s: s["profiles"][0]["config"]["inbounds"].append({"tag": TAG}))


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(dir="/private/tmp", prefix="grpc-tls-de4-offline-"))
        self.directory = Path(self.temporary); self.directory.chmod(0o700)
        self.state = self.directory / "state"
        self.panel = FakePanel()
        self.api = self.enterContext(patch.object(m, "api", side_effect=self.panel.api))

    def prepare(self): return m.prepare(self.state)
    def stage(self, action):
        return m.apply_stage(self.state, action, **({"tls_verified": True} if action == "enable" else {}))
    def through(self, action):
        for name in ("apply-profile", "bind-node", "create-host", "bind-squads", "enable"):
            self.stage(name)
            if name == action: return
        raise AssertionError("unknown test stage")
    def audit(self): return m.audit(self.state, self.panel.live)

    def test_recorded_owned_host_identity_cannot_be_replaced(self):
        self.prepare(); self.through("create-host")
        before_ids = set(m.read(self.state / "before.json")["scope"]["baseline_ids"]["hosts"])
        owned = next(row for row in self.panel.live["hosts"] if row["uuid"] not in before_ids)
        owned["uuid"] = "00000000-0000-4000-8000-000000001337"
        with self.assertRaises(ValueError): self.audit()

    def test_default_plan_is_pure_no_files_or_api(self):
        with patch.object(m, "prepare") as prepare, patch.object(m, "audit") as audit, \
             patch.object(m, "apply_stage") as apply_stage, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(m.main([]), 0)
        for call in (self.api, prepare, audit, apply_stage): call.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertFalse(self.state.exists())

    def test_prepare_is_get_only_for_exact_four_infrastructure_endpoints(self):
        self.prepare()
        self.assertEqual({(method, endpoint) for method, endpoint, _ in self.panel.calls},
                         {("GET", endpoint) for endpoint in ("/config-profiles", "/nodes", "/hosts", "/internal-squads")})
        self.assertEqual(self.panel.writes, [])

    def test_prepare_saves_private_projection_not_unrelated_payloads_or_node_keys(self):
        self.prepare()
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertTrue({"before.json", "candidate.json", "manifest.json"} <= {p.name for p in self.state.iterdir()})
        combined = ""
        for path in self.state.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
            combined += path.read_text()
        self.assertNotIn(UNRELATED_PRIVATE, combined); self.assertNotIn(NODE_PRIVATE, combined)
        before = json.loads((self.state / "before.json").read_text())
        self.assertTrue({"profile", "targetnode", "sourcehost", "squads", "hashes", "scope"} <= set(before))
        self.assertEqual(before["profile"]["uuid"], PROFILE)
        self.assertEqual(before["targetnode"]["uuid"], NODE)

    def test_duplicate_prepare_cannot_overwrite_state(self):
        self.prepare(); original = {p.name: p.read_bytes() for p in self.state.iterdir()}
        with self.assertRaises((ValueError, FileExistsError)): self.prepare()
        self.assertEqual({p.name: p.read_bytes() for p in self.state.iterdir()}, original)
        self.assertEqual(self.panel.writes, [])

    def test_invalid_build_stops_before_state_creation(self):
        self.panel.live["nodes"][0]["address"] = "192.0.2.9"
        with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.state.exists()); self.assertEqual(self.panel.writes, [])

    def test_audit_after_prepare_is_read_only_and_reports_initial_state(self):
        self.prepare(); result = self.audit()
        self.assertFalse(result["profile_applied"]); self.assertFalse(result["node_bound"])
        self.assertFalse(result["enabled"]); self.assertFalse(result["squads_bound"])
        self.assertIsNone(result["host_uuid"])
        self.assertEqual(self.panel.writes, [])

    def test_apply_profile_changes_only_target_config_and_preserves_three_old_inbounds(self):
        self.prepare(); original = deepcopy(self.panel.live); self.stage("apply-profile")
        self.assertEqual(self.panel.writes[0][0:2], ("PATCH", "/config-profiles"))
        self.assertEqual(self.panel.writes[0][2]["uuid"], PROFILE)
        self.assertEqual(self.panel.live["profiles"][0]["config"]["inbounds"][:3], original["profiles"][0]["config"]["inbounds"])
        self.assertEqual(self.panel.live["profiles"][1:], original["profiles"][1:])
        self.assertEqual(self.panel.live["nodes"], original["nodes"])
        self.assertEqual(self.panel.live["hosts"], original["hosts"])
        self.assertEqual(self.panel.live["squads"], original["squads"])
        self.assertTrue(self.audit()["profile_applied"])

    def test_bind_node_appends_only_new_inbound_preserving_node_metadata(self):
        self.prepare(); original = deepcopy(self.panel.live["nodes"][0]); self.through("bind-node")
        node = self.panel.live["nodes"][0]
        self.assertEqual({k: v for k, v in node.items() if k != "configProfile"},
                         {k: v for k, v in original.items() if k != "configProfile"})
        self.assertEqual(node["configProfile"]["activeConfigProfileUuid"], PROFILE)
        self.assertEqual({i["uuid"] for i in node["configProfile"]["activeInbounds"]}, {*OLD, NEW_INBOUND})
        self.assertTrue(self.audit()["node_bound"])

    def test_create_host_creates_exactly_one_disabled_host_for_new_panel_inbound(self):
        self.prepare(); self.through("create-host")
        posts = [body for method, path, body in self.panel.writes if method == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["inbound"], {"configProfileUuid": PROFILE, "configProfileInboundUuid": NEW_INBOUND})
        self.assertIs(posts[0]["isDisabled"], True)
        self.assertIs(posts[0]["mihomoX25519"], False)
        self.assertNotIn("mapper", posts[0])
        self.assertEqual(self.audit()["host_uuid"], NEW_HOST)
        self.assertFalse(self.audit()["enabled"])

    def test_owned_host_raw_empty_mapper_is_allowed(self):
        self.prepare(); self.through("create-host")
        host = next(row for row in self.panel.live["hosts"] if row["uuid"] == NEW_HOST)
        for value in (None, {}):
            with self.subTest(value=value):
                host["mapper"] = value
                self.assertEqual(self.audit()["host_uuid"], NEW_HOST)

    def test_owned_host_raw_foreign_mapper_is_rejected(self):
        self.prepare(); self.through("create-host")
        host = next(row for row in self.panel.live["hosts"] if row["uuid"] == NEW_HOST)
        for value in ({"foreign": True}, "foreign", [], False):
            with self.subTest(value=value):
                host["mapper"] = value
                with self.assertRaises(ValueError): self.audit()

    def test_bind_squads_adds_new_inbound_only_where_source_grpc_exists(self):
        self.prepare(); original = deepcopy(self.panel.live["squads"]); self.through("bind-squads")
        for index in (0, 1):
            self.assertEqual([i["uuid"] for i in self.panel.live["squads"][index]["inbounds"]],
                             [i["uuid"] for i in original[index]["inbounds"]] + [NEW_INBOUND])
        self.assertEqual(self.panel.live["squads"][2], original[2])
        self.assertEqual(set(self.audit()["squads_bound"]), set(SQUADS[:2]))
        self.assertFalse(self.audit()["enabled"])

    def test_enable_is_last_and_patches_only_new_host_disable_flag(self):
        self.prepare(); self.through("enable")
        self.assertEqual(self.panel.writes[-1], ("PATCH", "/hosts", {"uuid": NEW_HOST, "isDisabled": False}))
        self.assertTrue(self.audit()["enabled"])
        self.assertTrue(all(host["isDisabled"] is False for host in self.panel.live["hosts"][:3]))

    def test_enable_requires_explicit_tls_verification(self):
        self.prepare(); self.through("bind-squads"); count = len(self.panel.writes)
        with self.assertRaises(ValueError): m.apply_stage(self.state, "enable")
        self.assertEqual(len(self.panel.writes), count)
        self.assertFalse(self.audit()["enabled"])

    def test_enable_rejects_disconnected_or_disabled_or_error_node(self):
        for field, value in (("isConnected", False), ("isDisabled", True), ("lastStatusMessage", ERROR_SECRET)):
            with self.subTest(field=field):
                if self.state.exists():
                    self.temporary_cleanup_state()
                self.panel.live = fixture(); self.panel.calls.clear(); self.panel.writes.clear()
                self.prepare(); self.through("bind-squads"); count = len(self.panel.writes)
                self.panel.live["nodes"][0][field] = value
                with self.assertRaises(ValueError): self.stage("enable")
                self.assertEqual(len(self.panel.writes), count)

    def temporary_cleanup_state(self):
        # Only files created inside this test's disposable private directory.
        for path in self.state.iterdir(): path.unlink()
        self.state.rmdir()

    def test_each_mutation_has_exact_exclusive_durable_intent_before_api_call(self):
        self.prepare()
        def check_intent(method, endpoint, body):
            name = ("intent-profile.json" if endpoint == "/config-profiles" else
                    "intent-node.json" if endpoint == "/nodes" else
                    "intent-squad-" + body["uuid"] + ".json" if endpoint == "/internal-squads" else
                    "intent-host.json" if method == "POST" else "intent-enable.json")
            path = self.state / name
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), {"method": method, "endpoint": endpoint, "body": body})
        self.panel.before_write = check_intent
        self.through("enable")
        self.assertEqual(len(self.panel.writes), 6)

    def test_lost_accepted_host_response_is_recovered_without_duplicate_post(self):
        self.prepare(); self.through("bind-node"); self.panel.fail_after = ("POST", "/hosts")
        with self.assertRaises(RuntimeError): self.stage("create-host")
        self.assertEqual(self.audit()["host_uuid"], NEW_HOST)
        self.stage("create-host")
        self.assertEqual(sum(method == "POST" for method, _, _ in self.panel.writes), 1)

    def test_rollback_disable_changes_only_owned_host(self):
        self.prepare(); self.through("enable"); before = deepcopy(self.panel.live["hosts"])
        self.stage("rollback-disable")
        self.assertEqual(self.panel.writes[-1], ("PATCH", "/hosts", {"uuid": NEW_HOST, "isDisabled": True}))
        self.assertEqual(self.panel.live["hosts"][:-1], before[:-1])
        self.assertFalse(self.audit()["enabled"])

    def test_rollback_squads_restores_only_mirrored_original_memberships(self):
        self.prepare(); self.through("enable"); self.stage("rollback-disable"); self.stage("rollback-squads")
        self.assertEqual(self.panel.live["squads"], fixture()["squads"])
        self.assertFalse(self.audit()["squads_bound"])

    def test_rollback_node_restores_original_binding_without_other_node_changes(self):
        self.prepare(); self.through("enable"); self.stage("rollback-disable"); self.stage("rollback-squads")
        self.stage("rollback-node")
        original = fixture()["nodes"]
        self.assertEqual(m.node_state(self.panel.live["nodes"][0]), m.node_state(original[0]))
        self.assertEqual({key: value for key, value in self.panel.live["nodes"][0].items() if key != "configProfile"},
                         {key: value for key, value in original[0].items() if key != "configProfile"})
        self.assertEqual(self.panel.live["nodes"][1:], original[1:])
        self.assertFalse(self.audit()["node_bound"])

    def test_rollback_profile_refuses_existing_owned_host_even_when_disabled(self):
        self.prepare(); self.through("enable"); self.stage("rollback-disable"); self.stage("rollback-squads")
        self.stage("rollback-node"); count = len(self.panel.writes)
        with self.assertRaises(ValueError): self.stage("rollback-profile")
        self.assertEqual(len(self.panel.writes), count)
        self.assertTrue(self.audit()["profile_applied"])

    def test_stages_are_idempotent_without_duplicate_writes(self):
        self.prepare()
        for action in ("apply-profile", "bind-node", "create-host", "bind-squads", "enable"):
            self.stage(action); count = len(self.panel.writes); self.stage(action)
            self.assertEqual(len(self.panel.writes), count)

    def test_enable_before_other_stages_fails_without_writes(self):
        self.prepare()
        with self.assertRaises(ValueError): self.stage("enable")
        self.assertEqual(self.panel.writes, [])

    def test_bind_node_before_profile_fails_without_writes(self):
        self.prepare()
        with self.assertRaises(ValueError): self.stage("bind-node")
        self.assertEqual(self.panel.writes, [])

    def test_unrelated_profile_drift_blocks_mutation(self):
        self.prepare(); self.panel.live["profiles"][1]["config"]["changed"] = True
        with self.assertRaises(ValueError): self.stage("apply-profile")
        self.assertEqual(self.panel.writes, [])

    def test_unrelated_node_drift_blocks_mutation(self):
        self.prepare(); self.panel.live["nodes"][1]["address"] = "192.0.2.55"
        with self.assertRaises(ValueError): self.stage("apply-profile")
        self.assertEqual(self.panel.writes, [])

    def test_unrelated_host_drift_blocks_mutation(self):
        self.prepare(); self.panel.live["hosts"][-1]["address"] = "changed.example.com"
        with self.assertRaises(ValueError): self.stage("apply-profile")
        self.assertEqual(self.panel.writes, [])

    def test_host_update_and_reordering_metadata_do_not_false_fail_audit(self):
        self.prepare()
        for host in self.panel.live["hosts"]:
            host.update(updatedAt="2999-01-01T00:00:00Z", viewPosition=999)
        self.panel.live["hosts"].reverse()
        self.assertFalse(self.audit()["profile_applied"])
        self.assertEqual(self.panel.writes, [])

    def test_target_old_private_key_change_blocks_mutation(self):
        self.prepare()
        self.panel.live["profiles"][0]["config"]["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] = "foreign"
        with self.assertRaises(ValueError): self.stage("apply-profile")
        self.assertEqual(self.panel.writes, [])

    def test_candidate_file_tampering_blocks_mutation(self):
        self.prepare(); path = self.state / "candidate.json"
        value = json.loads(path.read_text()); value["new_host"]["address"] = "foreign.example.com"
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): self.stage("apply-profile")
        self.assertEqual(self.panel.writes, [])

    def test_public_state_permissions_block_mutation(self):
        self.prepare(); (self.state / "candidate.json").chmod(0o644)
        with self.assertRaises(ValueError): self.stage("apply-profile")
        self.assertEqual(self.panel.writes, [])

    def test_lost_accepted_profile_response_recovers_by_audit_not_duplicate_write(self):
        self.prepare(); self.panel.fail_after = ("PATCH", "/config-profiles")
        with self.assertRaises(RuntimeError): self.stage("apply-profile")
        self.assertTrue(self.audit()["profile_applied"])
        self.stage("apply-profile")
        self.assertEqual(len(self.panel.writes), 1)

    def test_unaccepted_write_with_intent_refuses_blind_retry(self):
        self.prepare(); self.panel.fail_before = ("PATCH", "/config-profiles")
        with self.assertRaises(RuntimeError): self.stage("apply-profile")
        self.assertFalse(self.audit()["profile_applied"])
        with self.assertRaises(ValueError): self.stage("apply-profile")
        self.assertEqual(len(self.panel.writes), 1)

    def test_external_matching_profile_change_without_intent_is_rejected(self):
        self.prepare(); candidate = m.build(fixture())
        profile = self.panel.live["profiles"][0]
        profile["config"] = candidate["config"]
        profile["inbounds"].append({"uuid": NEW_INBOUND, "tag": TAG})
        with self.assertRaises(ValueError): self.stage("bind-node")
        self.assertEqual(self.panel.writes, [])

    def test_cli_write_requires_confirm_and_redacts_arbitrary_error(self):
        with patch.object(m, "apply_stage") as apply_stage, patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(m.main(["apply-profile", "--state", str(self.state)]), 2)
        apply_stage.assert_not_called(); self.api.assert_not_called()
        with patch.object(m, "apply_stage", side_effect=RuntimeError(ERROR_SECRET)), \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(m.main(["apply-profile", "--state", str(self.state), "--confirm"]), 2)
        self.assertNotIn(ERROR_SECRET, errors.getvalue())

    def test_projection_hashes_cover_unrelated_records_without_saving_their_payloads(self):
        self.prepare()
        before = json.loads((self.state / "before.json").read_text())
        self.assertEqual(set(before), {"profile", "targetnode", "sourcehost", "squads", "hashes", "scope"})
        self.assertEqual(set(before["hashes"]["profiles"]), {OTHER_PROFILE})
        self.assertEqual(set(before["hashes"]["nodes"]), {OTHER_NODE})
        self.assertEqual(set(before["hashes"]["hosts"]), {*HOSTS, OTHER_HOST})
        self.assertEqual(set(before["hashes"]["squads"]), {SQUADS[2]})
        for hashes in before["hashes"].values():
            for digest in hashes.values(): self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(set(before["squads"]), set(SQUADS[:2]))
        self.assertEqual(before["targetnode"], m.node_state(fixture()["nodes"][0]))
        self.assertNotIn("secretKey", before["targetnode"])
        self.assertNotIn("note", before["targetnode"])
        self.assertNotIn(UNRELATED_PRIVATE, json.dumps(before))

    def test_validation_profile_is_only_candidate_config_with_no_unrelated_records(self):
        self.prepare()
        candidate = json.loads((self.state / "candidate.json").read_text())
        validation = json.loads((self.state / "validation-profile.json").read_text())
        self.assertEqual(validation, candidate["config"])
        self.assertEqual(validation, m.build(fixture())["config"])
        for secret in (UNRELATED_PRIVATE, NODE_PRIVATE): self.assertNotIn(secret, json.dumps(validation))


class InfrastructureAdapterGuardTests(unittest.TestCase):
    def test_account_subscription_and_key_generation_routes_stop_before_adapter_loading(self):
        paths = ("/users", "/users/stream", "/users/by-telegram-id/6910097562", "/subscriptions",
                 "/subscriptions/by-short-uuid/private/raw", "/keygen", "/api/users", "/api/subscriptions")
        with patch.object(m.runpy, "run_path") as adapter_loader:
            for path in paths:
                for method in ("GET", "POST", "PATCH", "DELETE"):
                    with self.subTest(method=method, path=path), self.assertRaises(m.GuardError):
                        m.api(method, path, None if method == "GET" else {})
        adapter_loader.assert_not_called()

    def test_new_profile_node_and_squad_creation_or_any_delete_are_forbidden(self):
        with patch.object(m.runpy, "run_path") as adapter_loader:
            for path in ("/config-profiles", "/nodes", "/internal-squads"):
                with self.subTest(path=path), self.assertRaises(m.GuardError): m.api("POST", path, {})
            for path in ("/config-profiles", "/nodes", "/internal-squads", "/hosts"):
                with self.subTest(path=path), self.assertRaises(m.GuardError): m.api("DELETE", path, {})
        adapter_loader.assert_not_called()

    def test_wrong_methods_and_invalid_body_shapes_stop_before_adapter_loading(self):
        calls = (("GET", "/hosts", {}), ("POST", "/hosts", None), ("PATCH", "/hosts", None),
                 ("PATCH", "/nodes", []), ("PUT", "/config-profiles", {}),
                 ("get", "/hosts", None), ("GET", "/hosts?with-users=true", None))
        with patch.object(m.runpy, "run_path") as adapter_loader:
            for method, path, body in calls:
                with self.subTest(method=method, path=path), self.assertRaises(m.GuardError): m.api(method, path, body)
        adapter_loader.assert_not_called()

    def test_only_reviewed_infrastructure_calls_delegate_to_fixed_local_adapter(self):
        adapter = Mock(return_value={"synthetic": True})
        with patch.object(m.runpy, "run_path", return_value={"api": adapter}) as loader:
            for endpoint in ("/hosts", "/nodes", "/config-profiles", "/internal-squads"):
                self.assertEqual(m.api("GET", endpoint), {"synthetic": True})
                adapter.assert_called_with("GET", endpoint, None)
                self.assertEqual(m.api("PATCH", endpoint, {"uuid": NODE}), {"synthetic": True})
                adapter.assert_called_with("PATCH", endpoint, {"uuid": NODE})
            self.assertEqual(m.api("POST", "/hosts", {"isDisabled": True}), {"synthetic": True})
            adapter.assert_called_with("POST", "/hosts", {"isDisabled": True})
        self.assertEqual(loader.call_count, 9)
        for call in loader.call_args_list:
            self.assertEqual(call.args, (str(Path(__file__).resolve().parents[1] / "direct-fi" / "manage.py"),))


if __name__ == "__main__": unittest.main()
