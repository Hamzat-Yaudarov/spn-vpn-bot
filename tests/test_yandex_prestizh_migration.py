"""Offline, keyless contracts for the single-host CDN migration helper.

The operation script is imported with its real panel client replaced before
execution. Fixtures are synthetic; writes go only to owned temporary state.
No environment, subscriber endpoint, SSH or network operation is reachable.
"""

import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import ModuleType
from unittest.mock import patch

import pytest


def _panel_tripwire(*args, **kwargs):
    raise AssertionError("Offline migration tests must never contact the panel")


def _private_write(path, value):
    """Test-only equivalent of the exclusive, private state writer."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _load_helper():
    path = Path(__file__).resolve().parents[1] / "ops/yandex-prestizh-migration/manage.py"
    spec = importlib.util.spec_from_file_location("yandex_prestizh_migration_tests", path)
    module = importlib.util.module_from_spec(spec)
    transport = ModuleType("scripts.prepare_direct_transports")
    transport.api = _panel_tripwire
    transport.private_write = _private_write
    with patch.dict(sys.modules, {"scripts.prepare_direct_transports": transport}):
        spec.loader.exec_module(module)
    return module


migration = _load_helper()
SOURCE_INBOUND = "synthetic-source-inbound"
DIRECT_INBOUND = "synthetic-direct-inbound"
OTHER_PROFILE = "synthetic-unrelated-profile"
OTHER_HOST = "synthetic-direct-host"
OTHER_NODE = "synthetic-unrelated-node"


def _item(rows, identity):
    return next(row for row in rows if row["uuid"] == identity)


def _target(snapshot):
    profile = _item(snapshot["profiles"], migration.TARGET_PROFILE)
    return next(row for row in profile["config"]["inbounds"] if row["tag"] == migration.TARGET_TAG)


def _source(snapshot):
    profile = _item(snapshot["profiles"], migration.SOURCE_PROFILE)
    return next(row for row in profile["config"]["inbounds"] if row["tag"] == migration.SOURCE_TAG)


def _client(snapshot):
    return json.loads(_item(snapshot["hosts"], migration.SOURCE_HOST)["xhttpExtraParams"])


def _set_client(snapshot, field, value):
    extra = _client(snapshot)
    extra[field] = value
    _item(snapshot["hosts"], migration.SOURCE_HOST)["xhttpExtraParams"] = json.dumps(extra, indent=2)


@pytest.fixture
def snapshot():
    server = {
        "path": "/synthetic-source-path/", "mode": "packet-up",
        "uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
        "uplinkDataKey": "X-Transport-Data", "scMaxEachPostBytes": 2048,
        "serverMaxHeaderBytes": 8192, "scMaxBufferedPosts": 8,
    }
    client = {key: copy.deepcopy(value) for key, value in server.items()
              if key not in {"path", "mode", "serverMaxHeaderBytes", "scMaxBufferedPosts"}}
    client["scMaxEachPostBytes"] = "512-2048"
    client["xPaddingBytes"] = "0-64"
    target = {
        "tag": migration.TARGET_TAG, "listen": "127.0.0.1", "port": 10085,
        "protocol": "vless", "settings": {"clients": [], "decryption": "none"},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        "streamSettings": {"network": "xhttp", "security": "none", "xhttpSettings": {
            "path": migration.TARGET_PATH, "mode": "packet-up",
            "uplinkHTTPMethod": "GET", "uplinkDataPlacement": "body",
        }},
    }
    source = copy.deepcopy(target)
    source["tag"] = migration.SOURCE_TAG
    source["streamSettings"]["xhttpSettings"] = server
    direct = {"tag": "synthetic-direct-grpc", "port": 2083, "protocol": "vless",
              "settings": {"clients": []}, "streamSettings": {"network": "grpc", "security": "none"}}
    ws = {"tag": "synthetic-direct-ws", "port": 10088, "protocol": "vless",
          "settings": {"clients": []}, "streamSettings": {"network": "ws", "security": "none"}}
    hysteria = {"tag": "synthetic-direct-hysteria", "port": 8443, "protocol": "hysteria",
                "settings": {"clients": []}, "streamSettings": {"network": "hysteria", "security": "none"}}
    config = {"log": {"loglevel": "warning"}, "inbounds": [target, direct, ws, hysteria],
              "outbounds": [{"tag": "direct", "protocol": "freedom"}],
              "routing": {"rules": [{"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"}]}}
    profiles = [
        {"uuid": migration.TARGET_PROFILE, "name": "synthetic-target-profile", "config": config,
         "inbounds": [{"uuid": migration.TARGET_INBOUND, "tag": migration.TARGET_TAG},
                      {"uuid": DIRECT_INBOUND, "tag": direct["tag"]},
                      {"uuid": "synthetic-ws-inbound", "tag": ws["tag"]},
                      {"uuid": "synthetic-hysteria-inbound", "tag": hysteria["tag"]}]},
        {"uuid": migration.SOURCE_PROFILE, "name": "synthetic-source-profile",
         "config": {"inbounds": [source], "outbounds": [{"protocol": "freedom"}]},
         "inbounds": [{"uuid": SOURCE_INBOUND, "tag": migration.SOURCE_TAG}]},
        {"uuid": OTHER_PROFILE, "name": "synthetic-unrelated-profile",
         "config": {"inbounds": [], "log": {"loglevel": "error"}}, "inbounds": []},
    ]
    hosts = [
        {"uuid": migration.TARGET_HOST, "remark": "synthetic-target-cdn",
         "address": migration.TARGET_DOMAIN, "port": 443, "path": migration.TARGET_PATH,
         "securityLayer": "TLS", "sni": migration.TARGET_DOMAIN, "host": migration.TARGET_DOMAIN,
         "alpn": "h2", "fingerprint": "chrome", "isDisabled": False,
         "nodes": [migration.TARGET_NODE], "updatedAt": "synthetic-before",
         "inbound": {"configProfileUuid": migration.TARGET_PROFILE,
                     "configProfileInboundUuid": migration.TARGET_INBOUND},
         "xhttpExtraParams": "{\"uplinkHTTPMethod\":\"GET\",\"uplinkDataPlacement\":\"body\"}"},
        {"uuid": migration.SOURCE_HOST, "address": "source-cdn.example.test", "port": 443,
         "path": server["path"], "securityLayer": "TLS", "nodes": [],
         "inbound": {"configProfileUuid": migration.SOURCE_PROFILE,
                     "configProfileInboundUuid": SOURCE_INBOUND},
         "xhttpExtraParams": json.dumps(client, indent=2)},
        {"uuid": OTHER_HOST, "address": "direct.example.test", "port": 2083,
         "inbound": {"configProfileUuid": migration.TARGET_PROFILE,
                     "configProfileInboundUuid": DIRECT_INBOUND}, "nodes": [migration.TARGET_NODE]},
    ]
    nodes = [
        {"uuid": migration.TARGET_NODE, "address": migration.TARGET_NODE_ADDRESS, "port": 2222,
         "configProfile": {"activeConfigProfileUuid": migration.TARGET_PROFILE,
                           "activeInbounds": [{"uuid": row["uuid"]} for row in profiles[0]["inbounds"]]}},
        {"uuid": OTHER_NODE, "address": "unrelated.example.test", "port": 2222,
         "configProfile": {"activeConfigProfileUuid": OTHER_PROFILE, "activeInbounds": []}},
    ]
    return {"profiles": profiles, "hosts": hosts, "nodes": nodes}


class Panel:
    """Independent synthetic server with strict endpoint/body write tripwires."""
    def __init__(self, snapshot):
        self.live = copy.deepcopy(snapshot)
        self.calls = []
        self.snapshot_count = 0
        self.on_snapshot = None
        self.on_patch = None
        self.fail_once = None
        self.no_commit_once = None
        self.expected_config, self.expected_host, _ = migration.build_candidate(snapshot)
        self.before_config = copy.deepcopy(_item(snapshot["profiles"], migration.TARGET_PROFILE)["config"])
        self.before_extra = copy.deepcopy(_item(snapshot["hosts"], migration.TARGET_HOST)["xhttpExtraParams"])

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def api(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET":
            assert body is None
            assert path in {"/config-profiles", "/hosts", "/nodes"}, "Forbidden read endpoint"
            if path == "/config-profiles":
                self.snapshot_count += 1
                if self.on_snapshot:
                    self.on_snapshot(self.snapshot_count, self.live)
                return {"configProfiles": copy.deepcopy(self.live["profiles"])}
            return copy.deepcopy(self.live["hosts" if path == "/hosts" else "nodes"])
        assert method == "PATCH", "Forbidden write method"
        assert path in {"/config-profiles", "/hosts"}, "Forbidden write endpoint"
        if path == "/config-profiles":
            assert set(body) == {"uuid", "config"}
            assert body["uuid"] == migration.TARGET_PROFILE
            assert body["config"] in (self.expected_config, self.before_config)
        else:
            assert set(body) == {"uuid", "xhttpExtraParams"}
            assert body["uuid"] == migration.TARGET_HOST
            assert body["xhttpExtraParams"] in (self.expected_host["xhttpExtraParams"], self.before_extra)
        failure = self.fail_once if self.fail_once and self.fail_once[0] == path else None
        if failure:
            self.fail_once = None
            if failure[1] == "before":
                raise TimeoutError("synthetic lost write response")
        commit = self.no_commit_once != path
        if not commit:
            self.no_commit_once = None
        if commit and path == "/config-profiles":
            _item(self.live["profiles"], migration.TARGET_PROFILE)["config"] = copy.deepcopy(body["config"])
        elif commit:
            host = _item(self.live["hosts"], migration.TARGET_HOST)
            host["xhttpExtraParams"] = copy.deepcopy(body["xhttpExtraParams"])
            host["updatedAt"] = "synthetic-after"
        if self.on_patch:
            self.on_patch(path, self.live)
        if failure:
            raise TimeoutError("synthetic lost write response")
        return {}


@pytest.fixture
def panel(snapshot, monkeypatch):
    result = Panel(snapshot)
    monkeypatch.setattr(migration, "api", result.api)
    return result


@pytest.fixture
def state_dir(panel):
    with tempfile.TemporaryDirectory(prefix="yandex-prestizh-unit-", dir="/private/tmp") as root:
        directory = migration.prepare(Path(root) / "prepared")
        panel.calls.clear()
        yield directory


@pytest.fixture
def applied_dir(panel, state_dir):
    assert migration.apply(state_dir, confirm=True) == "complete"
    panel.calls.clear()
    return state_dir


def test_import_uses_tripwire_not_environment_client():
    assert migration.api is _panel_tripwire
    assert migration.private_write is _private_write
    with pytest.raises(AssertionError, match="never contact"):
        _panel_tripwire("GET", "/users")


def test_approved_target_and_source_identities_are_not_retargetable():
    assert migration.TARGET_HOST == "645c8877-c1ef-4224-993d-70a31b5cd4e5"
    assert migration.TARGET_PROFILE == "63c21878-f375-4383-af21-98c777c358c5"
    assert migration.TARGET_INBOUND == "b3ee690f-2420-4441-8cdc-0e63bea75e80"
    assert migration.TARGET_TAG == "vless-xhttp-vk-prestizh"
    assert migration.SOURCE_PROFILE == "59aba40b-9ed3-4eec-b6af-b34a39da88d1"
    assert migration.SOURCE_HOST == "c1614c56-403d-4a67-b917-928a04600db8"
    assert migration.SOURCE_TAG == "vless-xhttp-yandex-de11"


def test_candidate_only_replaces_xhttp_settings_and_exact_client_extra(snapshot):
    before = copy.deepcopy(snapshot)
    config, host, validation = migration.build_candidate(snapshot)
    expected_config = copy.deepcopy(_item(before["profiles"], migration.TARGET_PROFILE)["config"])
    expected_server = copy.deepcopy(_source(before)["streamSettings"]["xhttpSettings"])
    expected_server["path"] = migration.TARGET_PATH
    expected_config["inbounds"][0]["streamSettings"]["xhttpSettings"] = expected_server
    expected_host = copy.deepcopy(_item(before["hosts"], migration.TARGET_HOST))
    expected_host["xhttpExtraParams"] = _item(before["hosts"], migration.SOURCE_HOST)["xhttpExtraParams"]
    assert config == expected_config
    assert host == expected_host
    assert validation == config
    assert snapshot == before
    config["inbounds"][0]["streamSettings"]["xhttpSettings"]["mode"] = "synthetic-mutation"
    host["nodes"].append("synthetic-mutation")
    assert snapshot == before


def test_validation_clears_only_synthetic_client_arrays(snapshot):
    for inbound in _item(snapshot["profiles"], migration.TARGET_PROFILE)["config"]["inbounds"]:
        inbound["settings"]["clients"] = [{"syntheticFixture": True}]
    config, _, validation = migration.build_candidate(snapshot)
    expected = copy.deepcopy(config)
    for inbound in expected["inbounds"]:
        inbound["settings"]["clients"] = []
    assert validation == expected
    assert all(row["settings"]["clients"] == [{"syntheticFixture": True}] for row in config["inbounds"])


@pytest.mark.parametrize("as_object", [False, True])
def test_source_client_extra_retains_exact_representation(snapshot, as_object):
    if as_object:
        _item(snapshot["hosts"], migration.SOURCE_HOST)["xhttpExtraParams"] = _client(snapshot)
    expected = copy.deepcopy(_item(snapshot["hosts"], migration.SOURCE_HOST)["xhttpExtraParams"])
    assert migration.build_candidate(snapshot)[1]["xhttpExtraParams"] == expected


@pytest.mark.parametrize("side,field,value", [
    ("server", "mode", "auto"), ("server", "uplinkHTTPMethod", "POST"),
    ("client", "uplinkHTTPMethod", "POST"), ("server", "uplinkDataPlacement", "body"),
    ("client", "uplinkDataPlacement", "body"), ("server", "uplinkDataKey", "Other-Header"),
    ("client", "uplinkDataKey", "Other-Header"), ("server", "scMaxEachPostBytes", 2049),
    ("client", "scMaxEachPostBytes", "512-2049"), ("client", "scMaxEachPostBytes", "2048-512"),
    ("client", "scMaxEachPostBytes", 0), ("client", "scMaxEachPostBytes", "not-a-limit"),
    ("server", "serverMaxHeaderBytes", 4096),
    ("client", "host", "unexpected.example.test"), ("client", "path", "/unexpected/"),
    ("client", "downloadSettings", {}),
])
def test_unknown_source_shape_is_rejected(snapshot, side, field, value):
    if side == "server":
        _source(snapshot)["streamSettings"]["xhttpSettings"][field] = value
    else:
        _set_client(snapshot, field, value)
    with pytest.raises(migration.GuardError):
        migration.build_candidate(snapshot)


@pytest.mark.parametrize("extra", [None, [], "not-json", "[]", "null"])
def test_missing_or_nonobject_source_extra_is_rejected(snapshot, extra):
    _item(snapshot["hosts"], migration.SOURCE_HOST)["xhttpExtraParams"] = extra
    with pytest.raises(migration.GuardError):
        migration.build_candidate(snapshot)


@pytest.mark.parametrize("case", [
    "target_profile_binding", "target_inbound_binding", "target_address", "target_port",
    "target_path", "target_security", "target_metadata_tag", "target_listen", "target_network",
    "target_server_path", "target_node_address", "target_node_profile", "target_node_inbound",
    "target_host_node", "source_profile_binding", "source_inbound_binding", "source_network",
])
def test_wrong_selected_object_binding_is_rejected(snapshot, case):
    host = _item(snapshot["hosts"], migration.TARGET_HOST)
    source_host = _item(snapshot["hosts"], migration.SOURCE_HOST)
    node = _item(snapshot["nodes"], migration.TARGET_NODE)
    target_meta = _item(_item(snapshot["profiles"], migration.TARGET_PROFILE)["inbounds"], migration.TARGET_INBOUND)
    mutations = {
        "target_profile_binding": lambda: host["inbound"].update(configProfileUuid=OTHER_PROFILE),
        "target_inbound_binding": lambda: host["inbound"].update(configProfileInboundUuid=DIRECT_INBOUND),
        "target_address": lambda: host.update(address="unexpected.example.test"),
        "target_port": lambda: host.update(port=8443),
        "target_path": lambda: host.update(path="/unexpected/"),
        "target_security": lambda: host.update(securityLayer="DEFAULT"),
        "target_metadata_tag": lambda: target_meta.update(tag="unexpected-tag"),
        "target_listen": lambda: _target(snapshot).update(listen="0.0.0.0"),
        "target_network": lambda: _target(snapshot)["streamSettings"].update(network="ws"),
        "target_server_path": lambda: _target(snapshot)["streamSettings"]["xhttpSettings"].update(path="/unexpected/"),
        "target_node_address": lambda: node.update(address="unexpected.example.test"),
        "target_node_profile": lambda: node["configProfile"].update(activeConfigProfileUuid=OTHER_PROFILE),
        "target_node_inbound": lambda: node["configProfile"].update(activeInbounds=[]),
        "target_host_node": lambda: host.update(nodes=[OTHER_NODE]),
        "source_profile_binding": lambda: source_host["inbound"].update(configProfileUuid=OTHER_PROFILE),
        "source_inbound_binding": lambda: source_host["inbound"].update(configProfileInboundUuid=DIRECT_INBOUND),
        "source_network": lambda: _source(snapshot)["streamSettings"].update(network="ws"),
    }
    mutations[case]()
    with pytest.raises(migration.GuardError):
        migration.build_candidate(snapshot)


@pytest.mark.parametrize("collection,identity", [
    ("hosts", migration.TARGET_HOST), ("hosts", migration.SOURCE_HOST),
    ("profiles", migration.TARGET_PROFILE), ("profiles", migration.SOURCE_PROFILE),
    ("nodes", migration.TARGET_NODE),
])
def test_duplicate_selected_uuid_is_rejected(snapshot, collection, identity):
    snapshot[collection].append(copy.deepcopy(_item(snapshot[collection], identity)))
    with pytest.raises(migration.GuardError):
        migration.build_candidate(snapshot)


@pytest.mark.parametrize("disabled", [False, True])
def test_another_host_sharing_selected_inbound_is_rejected(snapshot, disabled):
    collision = copy.deepcopy(_item(snapshot["hosts"], migration.TARGET_HOST))
    collision.update(uuid="synthetic-colliding-host", isDisabled=disabled)
    snapshot["hosts"].append(collision)
    with pytest.raises(migration.GuardError):
        migration.build_candidate(snapshot)


@pytest.mark.parametrize("which,duplicate", [("target", "tag"), ("source", "uuid")])
def test_ambiguous_inbound_metadata_is_rejected(snapshot, which, duplicate):
    profile = _item(snapshot["profiles"], migration.TARGET_PROFILE if which == "target" else migration.SOURCE_PROFILE)
    row = copy.deepcopy(profile["inbounds"][0])
    row["uuid" if duplicate == "tag" else "tag"] = "synthetic-duplicate"
    profile["inbounds"].append(row)
    with pytest.raises(migration.GuardError):
        migration.build_candidate(snapshot)


@pytest.mark.parametrize("case", ["duplicate_node", "shared_node", "extra_host_node"])
def test_shared_or_ambiguous_node_scope_is_rejected(snapshot, case):
    if case == "duplicate_node":
        snapshot["nodes"].append(copy.deepcopy(_item(snapshot["nodes"], OTHER_NODE)))
    elif case == "shared_node":
        _item(snapshot["nodes"], OTHER_NODE)["configProfile"] = {
            "activeConfigProfileUuid": migration.TARGET_PROFILE,
            "activeInbounds": [{"uuid": migration.TARGET_INBOUND}],
        }
    else:
        _item(snapshot["hosts"], migration.TARGET_HOST)["nodes"].append(OTHER_NODE)
    with pytest.raises(migration.GuardError):
        migration.build_candidate(snapshot)


@pytest.mark.parametrize("node_binding", [[], [{"uuid": migration.TARGET_NODE}]])
def test_unique_inferred_or_object_node_binding_is_supported(snapshot, node_binding):
    _item(snapshot["hosts"], migration.TARGET_HOST)["nodes"] = node_binding
    assert migration.build_candidate(snapshot)[1]["nodes"] == node_binding


def test_source_host_and_server_paths_must_agree(snapshot):
    _item(snapshot["hosts"], migration.SOURCE_HOST)["path"] = "/inconsistent-source/"
    with pytest.raises(migration.GuardError):
        migration.build_candidate(snapshot)


def test_prepare_is_three_allowed_gets_and_private_exclusive_files(panel, state_dir):
    assert panel.writes == []
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    expected_files = {"before-profiles.json", "before-hosts.json", "before-node-bindings.json",
                      "candidate-profile.json", "candidate-host-extra.json", "validation-profile.json", "manifest.json"}
    assert {path.name for path in state_dir.iterdir()} == expected_files
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in state_dir.iterdir())
    assert migration.load_state(state_dir)["candidate-profile.json"] == panel.expected_config
    with pytest.raises(FileExistsError):
        migration.prepare(state_dir)
    assert panel.writes == []


def test_prepare_reads_only_three_allowlisted_endpoints(panel):
    with tempfile.TemporaryDirectory(prefix="yandex-prestizh-unit-", dir="/private/tmp") as root:
        migration.prepare(Path(root) / "prepared")
    assert panel.calls == [("GET", "/config-profiles", None), ("GET", "/hosts", None), ("GET", "/nodes", None)]


def test_apply_requires_literal_confirmation_before_any_api(panel, state_dir):
    for confirmation in (False, None, 1, "yes"):
        with pytest.raises(migration.GuardError, match="confirmation"):
            migration.apply(state_dir, confirm=confirmation)
    assert panel.calls == []


def test_apply_exact_two_target_writes_preserves_all_other_state(panel, state_dir, snapshot):
    before = copy.deepcopy(panel.live)
    assert migration.apply(state_dir, confirm=True) == "complete"
    assert [(method, path) for method, path, _ in panel.calls] == (
        [("GET", path) for path in ("/config-profiles", "/hosts", "/nodes")]
        + [("PATCH", "/config-profiles")]
        + [("GET", path) for path in ("/config-profiles", "/hosts", "/nodes")]
        + [("PATCH", "/hosts")]
        + [("GET", path) for path in ("/config-profiles", "/hosts", "/nodes")]
    )
    expected = copy.deepcopy(before)
    _item(expected["profiles"], migration.TARGET_PROFILE)["config"] = panel.expected_config
    _item(expected["hosts"], migration.TARGET_HOST).update(
        xhttpExtraParams=panel.expected_host["xhttpExtraParams"], updatedAt="synthetic-after")
    assert panel.live == expected
    count = len(panel.writes)
    assert migration.audit(state_dir) == "complete"
    assert migration.apply(state_dir, confirm=True) == "complete"
    assert len(panel.writes) == count
    assert snapshot == before


@pytest.mark.parametrize("case", [
    "unrelated_profile", "direct_inbound", "unrelated_host", "source_extra", "target_sni",
    "target_metadata", "source_metadata", "node_binding", "node_duplicate", "host_added", "profile_removed",
])
def test_concurrent_drift_before_apply_blocks_every_write(panel, state_dir, case):
    live = panel.live
    if case == "unrelated_profile":
        _item(live["profiles"], OTHER_PROFILE)["config"]["log"]["loglevel"] = "debug"
    elif case == "direct_inbound":
        _item(live["profiles"], migration.TARGET_PROFILE)["config"]["inbounds"][1]["port"] = 2084
    elif case == "unrelated_host":
        _item(live["hosts"], OTHER_HOST)["address"] = "changed.example.test"
    elif case == "source_extra":
        _set_client(live, "xPaddingBytes", "0-128")
    elif case == "target_sni":
        _item(live["hosts"], migration.TARGET_HOST)["sni"] = "changed.example.test"
    elif case in {"target_metadata", "source_metadata"}:
        identity = migration.TARGET_PROFILE if case == "target_metadata" else migration.SOURCE_PROFILE
        _item(live["profiles"], identity)["inbounds"][0]["uuid"] = "changed-metadata"
    elif case == "node_binding":
        _item(live["nodes"], OTHER_NODE)["configProfile"]["activeInbounds"] = [{"uuid": "changed-binding"}]
    elif case == "node_duplicate":
        live["nodes"].append(copy.deepcopy(_item(live["nodes"], OTHER_NODE)))
    elif case == "host_added":
        live["hosts"].append({"uuid": "synthetic-added-host"})
    elif case == "profile_removed":
        live["profiles"] = [row for row in live["profiles"] if row["uuid"] != OTHER_PROFILE]
    with pytest.raises(migration.GuardError):
        migration.apply(state_dir, confirm=True)
    assert panel.writes == []


def test_between_phase_drift_stops_before_host_patch(panel, state_dir):
    def drift(path, live):
        if path == "/config-profiles":
            _item(live["hosts"], OTHER_HOST)["address"] = "changed.example.test"
    panel.on_patch = drift
    with pytest.raises(migration.GuardError, match="unrelated host"):
        migration.apply(state_dir, confirm=True)
    assert [path for _, path, _ in panel.writes] == ["/config-profiles"]


def test_final_audit_catches_drift_after_host_patch(panel, state_dir):
    def drift(path, live):
        if path == "/hosts":
            _item(live["nodes"], OTHER_NODE)["port"] = 2223
    panel.on_patch = drift
    with pytest.raises(migration.GuardError, match="Node bindings"):
        migration.apply(state_dir, confirm=True)
    assert len(panel.writes) == 2


def test_source_server_settings_drift_blocks_every_write(panel, state_dir):
    _source(panel.live)["streamSettings"]["xhttpSettings"]["scMaxBufferedPosts"] = 9
    with pytest.raises(migration.GuardError, match="unrelated profile"):
        migration.apply(state_dir, confirm=True)
    assert panel.writes == []


@pytest.mark.parametrize("path,stage,retry_paths", [
    ("/config-profiles", "prepared", ["/config-profiles", "/hosts"]),
    ("/hosts", "profile_applied", ["/hosts"]),
])
def test_successful_patch_response_without_commit_is_not_trusted(panel, state_dir, path, stage, retry_paths):
    panel.no_commit_once = path
    with pytest.raises(migration.GuardError):
        migration.apply(state_dir, confirm=True)
    if path == "/config-profiles":
        assert [endpoint for _, endpoint, _ in panel.writes] == ["/config-profiles"]
    assert migration.audit(state_dir) == stage
    panel.calls.clear()
    assert migration.apply(state_dir, confirm=True) == "complete"
    assert panel.calls[:3] == [("GET", endpoint, None) for endpoint in ("/config-profiles", "/hosts", "/nodes")]
    assert [endpoint for _, endpoint, _ in panel.writes] == retry_paths


@pytest.mark.parametrize("path,when,stage,retry_paths", [
    ("/config-profiles", "before", "prepared", ["/config-profiles", "/hosts"]),
    ("/config-profiles", "after", "profile_applied", ["/hosts"]),
    ("/hosts", "before", "profile_applied", ["/hosts"]),
    ("/hosts", "after", "complete", []),
])
def test_lost_response_never_blindly_retries_and_resumes_from_fresh_get(panel, state_dir, path, when, stage, retry_paths):
    panel.fail_once = (path, when)
    with pytest.raises(TimeoutError):
        migration.apply(state_dir, confirm=True)
    assert sum(call[1] == path for call in panel.writes) == 1
    assert migration.audit(state_dir) == stage
    panel.calls.clear()
    assert migration.apply(state_dir, confirm=True) == "complete"
    assert panel.calls[:3] == [("GET", endpoint, None) for endpoint in ("/config-profiles", "/hosts", "/nodes")]
    assert [endpoint for _, endpoint, _ in panel.writes] == retry_paths


def test_ambiguous_third_profile_state_cannot_be_retried(panel, state_dir):
    panel.fail_once = ("/config-profiles", "after")
    def drift(path, live):
        _target(live)["streamSettings"]["xhttpSettings"]["scMaxEachPostBytes"] = 1024
    panel.on_patch = drift
    with pytest.raises(TimeoutError):
        migration.apply(state_dir, confirm=True)
    panel.on_patch = None
    panel.calls.clear()
    with pytest.raises(migration.GuardError):
        migration.apply(state_dir, confirm=True)
    assert panel.writes == []


def test_ambiguous_third_host_state_cannot_be_retried(panel, state_dir):
    panel.fail_once = ("/hosts", "after")
    def drift(path, live):
        if path == "/hosts":
            _item(live["hosts"], migration.TARGET_HOST)["sni"] = "changed.example.test"
    panel.on_patch = drift
    with pytest.raises(TimeoutError):
        migration.apply(state_dir, confirm=True)
    panel.on_patch = None
    panel.calls.clear()
    with pytest.raises(migration.GuardError):
        migration.apply(state_dir, confirm=True)
    assert panel.writes == []


def test_host_only_partial_state_requires_manual_review(panel, state_dir):
    _item(panel.live["hosts"], migration.TARGET_HOST)["xhttpExtraParams"] = panel.expected_host["xhttpExtraParams"]
    with pytest.raises(migration.GuardError, match="Host-only"):
        migration.apply(state_dir, confirm=True)
    assert panel.writes == []


def test_already_correct_client_extra_needs_only_profile_patch(snapshot, monkeypatch):
    _item(snapshot["hosts"], migration.TARGET_HOST)["xhttpExtraParams"] = _item(snapshot["hosts"], migration.SOURCE_HOST)["xhttpExtraParams"]
    panel = Panel(snapshot)
    monkeypatch.setattr(migration, "api", panel.api)
    with tempfile.TemporaryDirectory(prefix="yandex-prestizh-unit-", dir="/private/tmp") as root:
        directory = migration.prepare(Path(root) / "prepared")
        panel.calls.clear()
        assert migration.audit(directory) == "prepared"
        assert migration.apply(directory, confirm=True) == "complete"
    assert [path for _, path, _ in panel.writes] == ["/config-profiles"]


@pytest.mark.parametrize("client_already_correct", [False, True])
def test_already_correct_server_skips_profile_write(snapshot, monkeypatch, client_already_correct):
    config, host, _ = migration.build_candidate(snapshot)
    _item(snapshot["profiles"], migration.TARGET_PROFILE)["config"] = config
    if client_already_correct:
        _item(snapshot["hosts"], migration.TARGET_HOST)["xhttpExtraParams"] = host["xhttpExtraParams"]
    panel = Panel(snapshot)
    monkeypatch.setattr(migration, "api", panel.api)
    with tempfile.TemporaryDirectory(prefix="yandex-prestizh-unit-", dir="/private/tmp") as root:
        directory = migration.prepare(Path(root) / "prepared")
        panel.calls.clear()
        assert migration.apply(directory, confirm=True) == "complete"
    assert [path for _, path, _ in panel.writes] == ([] if client_already_correct else ["/hosts"])


@pytest.mark.parametrize("entry", ["directory", "manifest.json", "candidate-profile.json"])
def test_state_permissions_must_remain_private(panel, state_dir, entry):
    path = state_dir if entry == "directory" else state_dir / entry
    path.chmod(0o755 if entry == "directory" else 0o644)
    with pytest.raises(migration.GuardError, match="mode"):
        migration.load_state(state_dir)
    assert panel.writes == []


def test_manifest_unknown_filename_is_rejected_before_any_external_read(state_dir):
    path = state_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["hashes"]["../synthetic-not-allowed.json"] = "synthetic-hash"
    path.write_text(json.dumps(manifest))
    visited = []
    original = Path.read_text
    def track(path, *args, **kwargs):
        visited.append(path.name)
        return original(path, *args, **kwargs)
    with patch.object(Path, "read_text", track), pytest.raises(migration.GuardError, match="state files"):
        migration.load_state(state_dir)
    assert visited == ["manifest.json"]


def test_tampered_candidate_and_rehashed_manifest_still_rejected(state_dir):
    candidate_path = state_dir / "candidate-profile.json"
    candidate = json.loads(candidate_path.read_text())
    candidate["inbounds"][1]["port"] = 2084
    candidate_path.write_text(json.dumps(candidate))
    manifest_path = state_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["hashes"][candidate_path.name] = migration.digest(candidate)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(migration.GuardError, match="scope"):
        migration.load_state(state_dir)


def test_symlinked_state_file_is_rejected(state_dir):
    target = state_dir / "candidate-host-extra.json"
    backup = state_dir / "synthetic-extra-copy.json"
    target.rename(backup)
    target.symlink_to(backup.name)
    with pytest.raises(migration.GuardError, match="0600"):
        migration.load_state(state_dir)


def test_prepare_refuses_paths_outside_private_tmp(panel, tmp_path):
    with pytest.raises(migration.GuardError, match="/private/tmp"):
        migration.prepare(tmp_path / "outside")
    assert not (tmp_path / "outside").exists()
    assert panel.writes == []


def test_rollback_requires_literal_confirmation_before_any_api(panel, state_dir):
    for confirmation in (False, None, 1, "yes"):
        with pytest.raises(migration.GuardError, match="confirmation"):
            migration.rollback(state_dir, confirm=confirmation)
    assert panel.calls == []


def test_rollback_exact_reverse_target_writes_preserves_all_other_state(panel, applied_dir, snapshot):
    assert migration.rollback(applied_dir, confirm=True) == "restored"
    assert [(method, path) for method, path, _ in panel.calls] == (
        [("GET", path) for path in ("/config-profiles", "/hosts", "/nodes")]
        + [("PATCH", "/hosts")]
        + [("GET", path) for path in ("/config-profiles", "/hosts", "/nodes")]
        + [("PATCH", "/config-profiles")]
        + [("GET", path) for path in ("/config-profiles", "/hosts", "/nodes")]
    )
    assert panel.writes == [
        ("PATCH", "/hosts", {"uuid": migration.TARGET_HOST, "xhttpExtraParams": panel.before_extra}),
        ("PATCH", "/config-profiles", {"uuid": migration.TARGET_PROFILE, "config": panel.before_config}),
    ]
    restored = copy.deepcopy(panel.live)
    _item(restored["hosts"], migration.TARGET_HOST)["updatedAt"] = "synthetic-before"
    assert restored == snapshot
    assert migration.audit(applied_dir) == "prepared"
    panel.calls.clear()
    assert migration.rollback(applied_dir, confirm=True) == "restored"
    assert panel.writes == []


def test_rollback_prepared_state_is_a_readonly_noop(panel, state_dir):
    assert migration.rollback(state_dir, confirm=True) == "restored"
    assert panel.writes == []
    assert all(method == "GET" for method, _, _ in panel.calls)


def test_rollback_profile_only_partial_state_writes_profile_only(panel, state_dir):
    _item(panel.live["profiles"], migration.TARGET_PROFILE)["config"] = copy.deepcopy(panel.expected_config)
    assert migration.rollback(state_dir, confirm=True) == "restored"
    assert panel.writes == [("PATCH", "/config-profiles", {
        "uuid": migration.TARGET_PROFILE, "config": panel.before_config,
    })]


@pytest.mark.parametrize("path,when,stage,retry_paths", [
    ("/hosts", "before", "complete", ["/hosts", "/config-profiles"]),
    ("/hosts", "after", "profile_applied", ["/config-profiles"]),
    ("/config-profiles", "before", "profile_applied", ["/config-profiles"]),
    ("/config-profiles", "after", "prepared", []),
])
def test_rollback_lost_response_requires_fresh_get_before_explicit_resume(panel, applied_dir, path, when, stage, retry_paths):
    panel.fail_once = (path, when)
    with pytest.raises(TimeoutError):
        migration.rollback(applied_dir, confirm=True)
    assert sum(call[1] == path for call in panel.writes) == 1
    assert migration.audit(applied_dir) == stage
    panel.calls.clear()
    assert migration.rollback(applied_dir, confirm=True) == "restored"
    assert panel.calls[:3] == [("GET", endpoint, None) for endpoint in ("/config-profiles", "/hosts", "/nodes")]
    assert [endpoint for _, endpoint, _ in panel.writes] == retry_paths


@pytest.mark.parametrize("path,retry_paths", [
    ("/hosts", ["/hosts", "/config-profiles"]),
    ("/config-profiles", ["/config-profiles"]),
])
def test_rollback_successful_response_without_commit_is_not_trusted(panel, applied_dir, path, retry_paths):
    panel.no_commit_once = path
    with pytest.raises(migration.GuardError):
        migration.rollback(applied_dir, confirm=True)
    if path == "/hosts":
        assert [endpoint for _, endpoint, _ in panel.writes] == ["/hosts"]
    panel.calls.clear()
    assert migration.rollback(applied_dir, confirm=True) == "restored"
    assert [endpoint for _, endpoint, _ in panel.writes] == retry_paths


@pytest.mark.parametrize("case", ["unrelated_host", "direct_inbound", "node_binding", "target_sni", "metadata"])
def test_rollback_concurrent_drift_blocks_every_write(panel, applied_dir, case):
    if case == "unrelated_host":
        _item(panel.live["hosts"], OTHER_HOST)["address"] = "changed.example.test"
    elif case == "direct_inbound":
        _item(panel.live["profiles"], migration.TARGET_PROFILE)["config"]["inbounds"][1]["port"] = 2084
    elif case == "node_binding":
        _item(panel.live["nodes"], OTHER_NODE)["port"] = 2223
    elif case == "target_sni":
        _item(panel.live["hosts"], migration.TARGET_HOST)["sni"] = "changed.example.test"
    else:
        _item(panel.live["profiles"], migration.TARGET_PROFILE)["inbounds"][0]["tag"] = "changed-metadata"
    with pytest.raises(migration.GuardError):
        migration.rollback(applied_dir, confirm=True)
    assert panel.writes == []


def test_rollback_between_phase_drift_stops_before_profile_patch(panel, applied_dir):
    def drift(path, live):
        if path == "/hosts":
            _item(live["hosts"], OTHER_HOST)["address"] = "changed.example.test"
    panel.on_patch = drift
    with pytest.raises(migration.GuardError, match="unrelated host"):
        migration.rollback(applied_dir, confirm=True)
    assert [endpoint for _, endpoint, _ in panel.writes] == ["/hosts"]


def test_rollback_final_audit_detects_unrelated_drift(panel, applied_dir):
    def drift(path, live):
        if path == "/config-profiles":
            _item(live["nodes"], OTHER_NODE)["port"] = 2223
    panel.on_patch = drift
    with pytest.raises(migration.GuardError, match="Node bindings"):
        migration.rollback(applied_dir, confirm=True)
    assert len(panel.writes) == 2


def test_rollback_ambiguous_third_state_cannot_be_retried(panel, applied_dir):
    panel.fail_once = ("/hosts", "after")
    def drift(path, live):
        if path == "/hosts":
            _item(live["hosts"], migration.TARGET_HOST)["xhttpExtraParams"] = "{\"syntheticThirdState\":true}"
    panel.on_patch = drift
    with pytest.raises(TimeoutError):
        migration.rollback(applied_dir, confirm=True)
    panel.on_patch = None
    panel.calls.clear()
    with pytest.raises(migration.GuardError):
        migration.rollback(applied_dir, confirm=True)
    assert panel.writes == []


@pytest.mark.parametrize("server_correct,client_correct", [(False, True), (True, False), (True, True)])
def test_rollback_noop_baseline_components_need_no_redundant_writes(snapshot, monkeypatch, server_correct, client_correct):
    config, host, _ = migration.build_candidate(snapshot)
    if server_correct:
        _item(snapshot["profiles"], migration.TARGET_PROFILE)["config"] = config
    if client_correct:
        _item(snapshot["hosts"], migration.TARGET_HOST)["xhttpExtraParams"] = host["xhttpExtraParams"]
    panel = Panel(snapshot)
    monkeypatch.setattr(migration, "api", panel.api)
    with tempfile.TemporaryDirectory(prefix="yandex-prestizh-unit-", dir="/private/tmp") as root:
        directory = migration.prepare(Path(root) / "prepared")
        assert migration.apply(directory, confirm=True) == "complete"
        panel.calls.clear()
        assert migration.rollback(directory, confirm=True) == "restored"
        assert [endpoint for _, endpoint, _ in panel.writes] == (
            ([] if client_correct else ["/hosts"]) + ([] if server_correct else ["/config-profiles"])
        )
        assert _item(panel.live["profiles"], migration.TARGET_PROFILE)["config"] == panel.before_config
        assert _item(panel.live["hosts"], migration.TARGET_HOST)["xhttpExtraParams"] == panel.before_extra
