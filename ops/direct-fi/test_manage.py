"""Offline activation guards. All state is temporary; API is always mocked."""
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("direct_fi_manage_tests", Path(__file__).with_name("manage.py"))
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)

OLD_PROFILE, NEW_PROFILE = "source-profile", "fi-profile"
OLD_GRPC, OLD_WS, OLD_HY2 = "old-grpc", "old-ws", "old-hy2"
NEW_GRPC, NEW_WS = "fi-grpc", "fi-ws"
TAGS = ("ordinary-grpc-reality-fi", "ordinary-ws-fi")


def inbound(tag, network, port, security):
    stream = {"network": network, "security": security}
    value = {"tag": tag, "port": port, "listen": "0.0.0.0", "protocol": "vless",
             "settings": {"clients": [], "decryption": "none"}, "streamSettings": stream}
    if network == "grpc":
        stream.update(grpcSettings={"serviceName": "grpc"}, realitySettings={"privateKey": "offline-source-placeholder"})
    elif network == "ws":
        value["listen"] = "127.0.0.1"
        stream["wsSettings"] = {"path": "/direct/ws/"}
    return value


def squad(uid, name, inbounds):
    return {"uuid": uid, "name": name, "inbounds": [{"uuid": value} for value in inbounds]}


def fixtures():
    metadata = [{"uuid": OLD_GRPC, "tag": "source-grpc"}, {"uuid": OLD_WS, "tag": "source-ws"}, {"uuid": OLD_HY2, "tag": "source-hy2"}]
    source = {"uuid": OLD_PROFILE, "name": "DIRECT_SOURCE", "inbounds": metadata,
              "config": {"inbounds": [inbound("source-grpc", "grpc", 2083, "reality"),
                                       inbound("source-ws", "ws", 10088, "none"),
                                       inbound("source-hy2", "hysteria", 8443, "tls")],
                         "outbounds": [{"tag": "direct", "protocol": "freedom"}]}}
    hosts = []
    for uid, iid, kind in zip(manage.SOURCE_HOSTS, (OLD_GRPC, OLD_WS), ("grpc", "ws")):
        hosts.append({"uuid": uid, "remark": "Existing " + kind, "address": "source.example.com", "port": 2083 if kind == "grpc" else 443,
                      "sni": "source.example.com", "host": None if kind == "grpc" else "source.example.com",
                      "path": "grpc" if kind == "grpc" else "/direct/ws/", "alpn": "h2" if kind == "grpc" else "http/1.1",
                      "isDisabled": False, "nodes": ["source-node"], "internalSquads": {"mode": "EXCLUDE", "squads": []},
                      "inbound": {"configProfileUuid": OLD_PROFILE, "configProfileInboundUuid": iid}})
    before = {"profiles": [source], "hosts": hosts,
              "nodes": [{"uuid": "source-node", "name": "existing-node", "address": "192.0.2.10", "port": 2222,
                         "configProfile": {"activeConfigProfileUuid": OLD_PROFILE, "activeInbounds": metadata}}],
              "squads": [squad("squad-de", "DE", [OLD_GRPC, OLD_WS]), squad("squad-grpc", "SOURCE_GRPC", [OLD_GRPC]), squad("squad-other", "UNRELATED", [OLD_HY2])]}
    config = {"inbounds": [inbound(TAGS[0], "grpc", 2083, "reality"), inbound(TAGS[1], "ws", 10088, "none")],
              "outbounds": [{"tag": "direct", "protocol": "freedom"}]}
    p = {"uuid": NEW_PROFILE, "name": manage.PROFILE, "config": config,
         "inbounds": [{"uuid": NEW_GRPC, "tag": TAGS[0]}, {"uuid": NEW_WS, "tag": TAGS[1]}]}
    node_intent = {"name": manage.NODE, "address": manage.ADDRESS, "port": 2222, "countryCode": "FI",
                   "tags": ["FI_102_103", "DIRECT"], "note": "offline deployment fixture",
                   "configProfile": {"activeConfigProfileUuid": NEW_PROFILE, "activeInbounds": [NEW_GRPC, NEW_WS]}}
    n = {**deepcopy(node_intent), "uuid": "fi-node", "isConnected": True, "isDisabled": False, "lastStatusMessage": None}
    n["configProfile"]["activeInbounds"] = deepcopy(p["inbounds"])
    new_hosts = []
    for iid, kind in ((NEW_GRPC, "grpc"), (NEW_WS, "ws")):
        new_hosts.append({"uuid": "fi-host-" + kind, "remark": "Finland " + kind,
                          "address": manage.ADDRESS if kind == "grpc" else manage.DOMAIN,
                          "port": 2083 if kind == "grpc" else 443, "sni": manage.DOMAIN,
                          "host": None if kind == "grpc" else manage.DOMAIN,
                          "path": "grpc" if kind == "grpc" else "/direct/ws/", "isDisabled": True,
                          "nodes": [n["uuid"]], "internalSquads": {"mode": "EXCLUDE", "squads": []},
                          "inbound": {"configProfileUuid": NEW_PROFILE, "configProfileInboundUuid": iid}})
    live = deepcopy(before)
    live["profiles"].append(p)
    live["nodes"].append(n)
    live["hosts"].extend(new_hosts)
    return before, live, p, n, new_hosts, node_intent


class ActivationGuardTests(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.old_umask)
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix="fi-manage-offline-"))
        self.enterContext(patch.object(manage, "STATE", Path(self.directory) / "state"))
        self.api = self.enterContext(patch.object(manage, "api", side_effect=AssertionError("unexpected API call")))
        self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.before, self.live, self.profile, self.node, self.hosts, node_intent = fixtures()
        self.enterContext(patch.object(manage, "snapshot", side_effect=lambda: deepcopy(self.live)))
        for name, value in {
            "baseline.json": self.before, "signatures.before.json": manage.signatures(self.before),
            "created-profile.json": self.profile, "intent-profile.json": {"name": manage.PROFILE, "config": self.profile["config"]},
            "profile.create.json": {"name": manage.PROFILE, "config": self.profile["config"]},
            "created-node.json": self.node, "intent-node.json": node_intent,
            "hosts.plan.json": [{"tag": TAGS[0], "sourceInbound": OLD_GRPC}, {"tag": TAGS[1], "sourceInbound": OLD_WS}],
        }.items():
            manage.save(name, value)
        for kind, h in zip(("grpc", "ws"), self.hosts):
            manage.save("created-" + kind + ".json", h)
            manage.save("intent-" + kind + ".json", {key: value for key, value in h.items() if key != "uuid"})

    def change_squad(self, uid, ids):
        entry = next(s for s in self.live["squads"] if s["uuid"] == uid)
        entry["inbounds"] = [{"uuid": iid} for iid in ids]

    def test_unchanged_baseline_and_disabled_owned_objects_are_read_only(self):
        live, profile, node = manage.audit_live()
        self.assertEqual(profile["uuid"], NEW_PROFILE)
        self.assertEqual(node["uuid"], "fi-node")
        self.assertEqual(live["squads"], self.before["squads"])
        self.api.assert_not_called()

    def test_only_host_order_and_update_metadata_are_ignored(self):
        for host in self.live['hosts'][:2]:
            host['viewPosition'] = 400
            host['updatedAt'] = '2026-10-30T01:02:03Z'
        manage.audit_live()
        self.live['hosts'][0]['createdAt'] = 'foreign-change'
        with self.assertRaisesRegex(ValueError, 'Existing hosts changed'):
            manage.audit_live()
        self.api.assert_not_called()

    def test_appending_third_cdn_inbound_requires_separate_stage_audit(self):
        self.profile['config']['inbounds'].append(inbound('vless-xhttp-yandex-fi', 'xhttp', 10085, 'none'))
        self.profile['inbounds'].append({'uuid': 'fi-cdn', 'tag': 'vless-xhttp-yandex-fi'})
        with self.assertRaises(ValueError):
            manage.audit_live()
        self.api.assert_not_called()

    def test_only_exact_two_new_inbounds_are_accepted_with_matching_durable_intent(self):
        ids = [OLD_GRPC, OLD_WS, NEW_GRPC, NEW_WS]
        self.change_squad("squad-de", ids)
        with self.assertRaises(ValueError):
            manage.audit_live()
        manage.save("squad-intent-squad-de.json", {"uuid": "squad-de", "inbounds": ids})
        manage.audit_live()
        self.change_squad("squad-de", ids + ["unauthorized-hy2"])
        with self.assertRaises(ValueError):
            manage.audit_live()
        self.api.assert_not_called()

    def test_foreign_existing_host_edit_is_rejected_before_any_mutation(self):
        self.live["hosts"][0]["sni"] = "foreign.example.com"
        with self.assertRaisesRegex(ValueError, "Existing hosts changed"):
            manage.bind_squads()
        self.api.assert_not_called()

    def test_owned_node_and_host_bindings_cannot_drift(self):
        for target, field, value in ((self.node, "address", "192.0.2.99"), (self.node, "port", 3333),
                                     (self.hosts[0], "nodes", ["foreign-node"]), (self.hosts[1], "sni", "wrong.example.com")):
            original = target[field]
            target[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                manage.audit_live()
            target[field] = original
        self.api.assert_not_called()

    def test_connection_enabled_and_error_free_guards_precede_squad_writes(self):
        for field, value in (("isConnected", False), ("isDisabled", True), ("lastStatusMessage", "offline failure")):
            original = self.node[field]
            self.node[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "New node not ready"):
                manage.bind_squads()
            self.node[field] = original
        self.api.assert_not_called()

    def test_bind_test_changes_only_de_and_preserves_existing_inbounds(self):
        calls = []
        def api(method, path, payload=None):
            self.assertEqual((method, path), ("PATCH", "/internal-squads"))
            self.assertEqual(payload, {"uuid": "squad-de", "inbounds": [OLD_GRPC, OLD_WS, NEW_GRPC, NEW_WS]})
            self.assertTrue((manage.STATE / "squad-intent-squad-de.json").exists())
            self.change_squad(payload["uuid"], payload["inbounds"])
            calls.append(deepcopy(payload))
            return deepcopy(next(s for s in self.live["squads"] if s["uuid"] == payload["uuid"]))
        self.api.side_effect = api
        manage.bind_squads(only_test=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.live["squads"][1:], self.before["squads"][1:])

    def test_bind_all_mirrors_each_source_inbound_not_a_permission_union(self):
        def api(method, path, payload=None):
            self.assertEqual((method, path), ("PATCH", "/internal-squads"))
            self.change_squad(payload["uuid"], payload["inbounds"])
            return deepcopy(next(s for s in self.live["squads"] if s["uuid"] == payload["uuid"]))
        self.api.side_effect = api
        manage.bind_squads()
        self.assertEqual(self.live["squads"][0]["inbounds"], squad("unused", "unused", [OLD_GRPC, OLD_WS, NEW_GRPC, NEW_WS])["inbounds"])
        self.assertEqual(self.live["squads"][1]["inbounds"], squad("unused", "unused", [OLD_GRPC, NEW_GRPC])["inbounds"])
        self.assertEqual(self.live["squads"][2], self.before["squads"][2])

    def test_activation_patches_only_owned_hosts_after_intent_and_is_idempotent(self):
        writes = []
        def api(method, path, payload=None):
            self.assertEqual(method, "PATCH")
            writes.append((path, deepcopy(payload)))
            if path == "/internal-squads":
                self.assertIn(payload["uuid"], {"squad-de", "squad-grpc"})
                self.assertEqual(manage.load("squad-intent-" + payload["uuid"] + ".json"), payload)
                self.change_squad(payload["uuid"], payload["inbounds"])
                return deepcopy(next(s for s in self.live["squads"] if s["uuid"] == payload["uuid"]))
            self.assertEqual(path, "/hosts")
            self.assertEqual(set(payload), {"uuid", "isDisabled"})
            host = next(h for h in self.hosts if h["uuid"] == payload["uuid"])
            kind = "grpc" if host["uuid"].endswith("grpc") else "ws"
            self.assertEqual(manage.load("enable-" + kind + ".json"), payload)
            host["isDisabled"] = payload["isDisabled"]
            return deepcopy(host)
        self.api.side_effect = api
        manage.activate()
        self.assertEqual(len(writes), 4)
        self.assertTrue(all(h["isDisabled"] is False for h in self.hosts))
        self.assertEqual(self.live["hosts"][:2], self.before["hosts"])
        self.assertEqual(self.live["nodes"][0], self.before["nodes"][0])
        manage.activate()
        self.assertEqual(len(writes), 4)

    def test_activation_requires_persisted_enabled_state_not_only_patch_response(self):
        def api(method, path, payload=None):
            self.assertEqual(method, "PATCH")
            if path == "/internal-squads":
                self.change_squad(payload["uuid"], payload["inbounds"])
                return deepcopy(next(s for s in self.live["squads"] if s["uuid"] == payload["uuid"]))
            self.assertEqual(path, "/hosts")
            # Simulate a successful-looking response without durable live state.
            host = next(h for h in self.hosts if h["uuid"] == payload["uuid"])
            return {**deepcopy(host), "isDisabled": False}
        self.api.side_effect = api
        with self.assertRaises(ValueError):
            manage.activate()

    def test_host_enabled_without_exact_durable_intent_is_rejected(self):
        self.hosts[0]["isDisabled"] = False
        with self.assertRaises(ValueError):
            manage.bind_squads()
        self.api.assert_not_called()

    def test_exact_enabled_host_intent_is_accepted(self):
        self.hosts[0]["isDisabled"] = False
        manage.save("enable-grpc.json", {"uuid": self.hosts[0]["uuid"], "isDisabled": False})
        manage.audit_live()
        self.api.assert_not_called()

    def test_wrong_or_unsafe_squad_intent_cannot_authorize_membership(self):
        ids = [OLD_GRPC, OLD_WS, NEW_GRPC, NEW_WS]
        self.change_squad("squad-de", ids)
        manage.save("squad-intent-squad-de.json", {"uuid": "foreign-squad", "inbounds": ids})
        with self.assertRaises(ValueError):
            manage.audit_live()
        path = manage.STATE / "squad-intent-squad-de.json"
        path.unlink()
        manage.save(path.name, {"uuid": "squad-de", "inbounds": ids})
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            manage.audit_live()
        self.api.assert_not_called()

    def test_extra_enabled_host_or_squad_with_owned_inbound_is_rejected(self):
        extra = {**deepcopy(self.hosts[0]), "uuid": "unexpected-host", "isDisabled": False}
        self.live["hosts"].append(extra)
        with self.assertRaises(ValueError):
            manage.audit_live()
        self.live["hosts"].pop()
        self.live["squads"].append(squad("unexpected-squad", "UNAPPROVED", [NEW_GRPC]))
        with self.assertRaises(ValueError):
            manage.audit_live()
        self.api.assert_not_called()

    def test_duplicate_infrastructure_uuid_fails_closed(self):
        self.live["hosts"].append(deepcopy(self.live["hosts"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate infrastructure UUIDs"):
            manage.audit_live()
        self.api.assert_not_called()

    def test_lost_post_is_never_blindly_retried_if_target_still_absent(self):
        payload = {"name": "offline-new-object"}
        calls = []
        def api(method, path, body=None):
            calls.append((method, path))
            if method == "GET": return []
            raise TimeoutError("offline simulated lost POST")
        self.api.side_effect = api
        with self.assertRaises(TimeoutError):
            manage.obtain("grpc", "retry-host", lambda h: h["name"] == payload["name"], payload)
        with self.assertRaisesRegex(RuntimeError, "prior create may have timed out"):
            manage.obtain("grpc", "retry-host", lambda h: h["name"] == payload["name"], payload)
        self.assertEqual(calls, [("GET", "/hosts"), ("POST", "/hosts"), ("GET", "/hosts")])

    def test_lost_post_can_recover_exact_created_object_by_fresh_get_without_post(self):
        payload = {"name": "offline-new-object"}
        found = []
        def api(method, path, body=None):
            if method == "GET": return deepcopy(found)
            found.append({"uuid": "offline-recovered", **payload})
            raise TimeoutError()
        self.api.side_effect = api
        with self.assertRaises(TimeoutError):
            manage.obtain("grpc", "recovered-host", lambda h: h["name"] == payload["name"], payload)
        result = manage.obtain("grpc", "recovered-host", lambda h: h["name"] == payload["name"], payload)
        self.assertEqual(result["uuid"], "offline-recovered")
        self.assertEqual(sum(call.args[0] == "POST" for call in self.api.call_args_list), 1)

    def test_prepare_uses_only_selected_grpc_ws_not_source_hy2(self):
        # Remove fixture-owned plans to test actual source filtering, never live I/O.
        for path in tuple(manage.STATE.iterdir()):
            if path.name != "baseline.json": path.unlink()
        self.api.side_effect = lambda method, path: {"secretKey": "offline-node-key-placeholder-long-enough"} if (method, path) == ("GET", "/keygen") else self.fail("unexpected API")
        manage.prepare()
        config = manage.load("profile.create.json")["config"]
        self.assertEqual({item["streamSettings"]["network"] for item in config["inbounds"]}, {"grpc", "ws"})
        self.assertEqual({item["port"] for item in config["inbounds"]}, {2083, 10088})
        self.assertEqual(len(config["inbounds"]), 2)
        self.assertTrue(all(item["settings"]["clients"] == [] for item in config["inbounds"]))
        self.assertTrue(all(item["body"]["isDisabled"] for item in manage.load("hosts.plan.json")))
        self.assertEqual(self.api.call_args_list[0].args, ("GET", "/keygen"))


class FreshCreationAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.old_umask)
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix='fi-create-offline-'))
        self.enterContext(patch.object(manage, 'STATE', Path(self.directory) / 'state'))
        self.api = self.enterContext(patch.object(manage, 'api', side_effect=AssertionError('unexpected API call')))
        self.stdout = self.enterContext(patch('sys.stdout', new_callable=io.StringIO))
        self.before = fixtures()[0]

    def prepare(self):
        manage.save('baseline.json', self.before)
        def keygen(method, endpoint, body=None):
            self.assertEqual((method, endpoint, body), ('GET', '/keygen', None))
            return {'secretKey': 'offline-node-secret-placeholder-long-enough'}
        self.api.side_effect = keygen
        manage.prepare()

    def panel_adapter(self, method, endpoint, body=None):
        buckets = {'/config-profiles': 'profiles', '/nodes': 'nodes', '/hosts': 'hosts'}
        self.assertIn(endpoint, buckets)
        rows = self.live[buckets[endpoint]]
        if method == 'GET':
            return {'configProfiles': deepcopy(rows)} if endpoint == '/config-profiles' else deepcopy(rows)
        self.assertEqual(method, 'POST')
        if endpoint == '/config-profiles':
            intent, uid = 'profile', NEW_PROFILE
            extra = {'inbounds': [{'uuid': NEW_GRPC, 'tag': TAGS[0]}, {'uuid': NEW_WS, 'tag': TAGS[1]}]}
        elif endpoint == '/nodes':
            intent, uid = 'node', 'fi-created-node'
            extra = {'isConnected': True, 'isDisabled': False, 'lastStatusMessage': None}
        else:
            intent = 'grpc' if body['port'] == 2083 else 'ws'
            uid, extra = 'fi-created-host-' + intent, {}
        # The fsynced intent must already exist when any POST is issued.
        self.assertEqual(manage.load('intent-' + intent + '.json'), body)
        self.assertEqual((manage.STATE / ('intent-' + intent + '.json')).stat().st_mode & 0o777, 0o600)
        result = {'uuid': uid, **deepcopy(body), **extra}
        rows.append(result)
        self.writes.append((method, endpoint, deepcopy(body)))
        return deepcopy(result)

    def test_default_cli_is_pure_plan_without_state_credentials_or_network(self):
        with (patch('sys.argv', ['manage.py']),
              patch.object(manage, 'snapshot', side_effect=AssertionError('snapshot')),
              patch.object(manage, 'load', side_effect=AssertionError('state read')),
              patch.object(manage, 'private_dir', side_effect=AssertionError('state write'))):
            manage.main()
        result = json.loads(self.stdout.getvalue())
        self.assertTrue(result['plan_only'])
        self.assertFalse(result['network'])
        self.assertFalse(result['filesystem_writes'])
        self.assertFalse(result['existing_node_reuse'])
        self.assertEqual((result['address'], result['domain']), ('92.42.102.103', 'fi103.wayspn.com'))
        self.assertFalse(manage.STATE.exists())
        self.api.assert_not_called()

    def test_prepare_has_fresh_keys_finland_metadata_and_preserves_source(self):
        original = deepcopy(self.before)
        self.prepare()
        config = manage.load('profile.create.json')['config']
        grpc = next(i for i in config['inbounds'] if i['streamSettings']['network'] == 'grpc')
        reality = grpc['streamSettings']['realitySettings']
        self.assertNotEqual(reality['privateKey'], 'offline-source-placeholder')
        self.assertEqual(reality['serverNames'], ['fi103.wayspn.com'])
        self.assertEqual(reality['target'], '127.0.0.1:443')
        self.assertEqual(len(reality['shortIds'][0]), 16)
        for item in manage.load('hosts.plan.json'):
            self.assertTrue(item['body']['remark'].startswith('🇫🇮 Финляндия • '))
            self.assertEqual(item['body']['tags'], ['FI_102_103', 'DIRECT'])
            self.assertTrue(item['body']['isDisabled'])
        self.assertEqual(manage.load('baseline.json'), original)
        self.assertEqual((manage.STATE / 'node.env').stat().st_mode & 0o777, 0o600)
        self.assertEqual(manage.STATE.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(reality['privateKey'], self.stdout.getvalue())

    def test_repeated_prepare_never_rotates_keys_or_repeats_keygen(self):
        self.prepare()
        before = {p.name: p.read_bytes() for p in manage.STATE.iterdir()}
        self.api.reset_mock()
        self.api.side_effect = AssertionError('second keygen forbidden')
        manage.prepare()
        self.assertEqual({p.name: p.read_bytes() for p in manage.STATE.iterdir()}, before)
        self.api.assert_not_called()

    def test_create_only_new_profile_node_two_disabled_hosts_and_resume_read_only(self):
        self.prepare()
        self.live, self.writes = deepcopy(self.before), []
        self.api.side_effect = self.panel_adapter
        manage.create()
        self.assertEqual([path for _, path, _ in self.writes], ['/config-profiles', '/nodes', '/hosts', '/hosts'])
        self.assertEqual(self.live['nodes'][1]['countryCode'], 'FI')
        self.assertEqual(self.live['nodes'][1]['address'], '92.42.102.103')
        self.assertEqual(self.live['nodes'][1]['name'], 'node-direct-fi-102.103')
        self.assertTrue(all(h['isDisabled'] for h in self.live['hosts'][2:]))
        self.assertEqual(self.live['hosts'][:2], self.before['hosts'])
        self.assertEqual(self.live['profiles'][:1], self.before['profiles'])
        self.assertEqual(self.live['nodes'][:1], self.before['nodes'])
        self.assertEqual(self.live['squads'], self.before['squads'])
        with patch.object(manage, 'snapshot', return_value=deepcopy(self.live)):
            manage.audit_live()
        manage.create()
        self.assertEqual(len(self.writes), 4)

    def test_existing_fi_address_or_name_is_never_reused_by_prepare(self):
        for field, value in (('address', manage.ADDRESS), ('name', manage.NODE)):
            with self.subTest(field=field):
                baseline = deepcopy(self.before)
                baseline['nodes'][0][field] = value
                with patch.object(manage, 'load', return_value=baseline):
                    with self.assertRaisesRegex(ValueError, 'Target node already exists'):
                        manage.prepare()
        self.api.assert_not_called()

    def test_wrong_domain_plan_is_rejected_before_network(self):
        self.prepare()
        plan = manage.load('plan.json')
        plan['domain'] = 'not-fi.example.com'
        original_load = manage.load
        with patch.object(manage, 'load', side_effect=lambda name: plan if name == 'plan.json' else original_load(name)):
            self.api.reset_mock()
            with self.assertRaisesRegex(ValueError, 'Wrong Finland plan'):
                manage.create()
        self.api.assert_not_called()

    def test_existing_node_without_our_intent_is_never_rebound_or_adopted(self):
        self.prepare()
        self.live, self.writes = deepcopy(self.before), []
        self.live['nodes'].append({'uuid': 'foreign-fi-node', 'address': manage.ADDRESS, 'name': 'foreign'})
        self.api.side_effect = self.panel_adapter
        with self.assertRaisesRegex(ValueError, 'without our durable intent'):
            manage.create()
        self.assertEqual([path for _, path, _ in self.writes], ['/config-profiles'])
        self.assertFalse((manage.STATE / 'intent-node.json').exists())
        self.assertEqual(self.live['nodes'][-1], {'uuid': 'foreign-fi-node', 'address': manage.ADDRESS, 'name': 'foreign'})

    def test_scope_rejects_noninfrastructure_paths_before_reading_credentials(self):
        # The unmocked adapter is exercised separately; there is no request.
        spec = importlib.util.spec_from_file_location('fi_scope_only', Path(__file__).with_name('manage.py'))
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        with patch.object(Path, 'read_text', side_effect=AssertionError('credential read')):
            for path in ('/users', '/subscriptions', '/hosts/../users', '/hosts?users=true'):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'outside infrastructure scope'):
                    adapter.api('GET', path)


if __name__ == '__main__':
    unittest.main()
