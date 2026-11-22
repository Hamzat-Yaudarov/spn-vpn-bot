"""Offline guest integration: real executor logic, fake panel/SSH/DNS/probes.

No provider client, real SSH process, DNS request, subscription fetch or paid
operation is used by these tests. Canonical identifiers match production APIs.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import time
import types
import uuid

import pytest

from voxa_fleet import executor as e
from voxa_fleet.store import Store

REAL_FLEET_SSH = e.FleetSSH


BASE = "https://panel.example.com/api"
DOMAIN = "n-a1b2c3.wayspn.com"
IP, NEW = "8.8.4.4", "9.9.9.9"
PANEL = "1.1.1.1"
SQUAD = "11111111-1111-4111-8111-111111111111"
IMAGE = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"
RECORD, ZONE = "a" * 32, "b" * 32
FINGERPRINT = "SHA256:" + "A" * 43
MACHINE = "a" * 32
KEYS = {"private": "a" * 43, "public": "b" * 43, "short": "0123456789abcdef"}


class FakeAPI:
    def __init__(self):
        self.data = {"config-profiles": [], "nodes": [], "hosts": [], "internal-squads": [
            {"uuid": SQUAD, "name": "regular", "inbounds": []}]}
        self.calls, self.sequence, self.lose = [], 0, None

    def _uuid(self):
        self.sequence += 1
        return f"00000000-0000-4000-8000-{self.sequence:012d}"

    def rows(self, kind):
        return copy.deepcopy(self.data[kind])

    def action(self, uid, action):
        row = next(item for item in self.data["nodes"] if item["uuid"] == uid)
        self.calls.append(("POST", "/nodes/" + uid + "/actions/" + action, None))
        row["isDisabled"] = action == "disable"
        if self.lose == action:
            self.lose = None
            raise e.d.u.DeployError("fake lost node action response")
        return copy.deepcopy(row)

    def disable_node(self, uid):
        return self.action(uid, "disable")

    def enable_node(self, uid):
        return self.action(uid, "enable")

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path == "/keygen":
            return {"secretKey": "preserved-service-node-secret-1234567890"}
        kind = path.removeprefix("/")
        if method == "POST":
            row = {"uuid": self._uuid(), **copy.deepcopy(body)}
            if kind == "config-profiles":
                row["inbounds"] = [{"uuid": self._uuid(), "tag": item["tag"]} for item in row["config"]["inbounds"]]
            if kind == "nodes":
                row.update(isConnected=True, isDisabled=False, lastStatusMessage=None)
            self.data[kind].append(row)
            return copy.deepcopy(row)
        if method == "PATCH":
            row = next(item for item in self.data[kind] if item["uuid"] == body["uuid"])
            row.update(copy.deepcopy(body))
            if self.lose == "PATCH":
                self.lose = None
                raise e.d.u.DeployError("fake lost applied PATCH response")
            return copy.deepcopy(row)
        raise AssertionError("unexpected panel call " + method + " " + path)


class FakeSSH:
    calls = []
    fail_action = None
    uploaded = []
    installed = set()

    def __init__(self, plan, *args, **kwargs):
        self.plan = plan
        self.common = []

    def __enter__(self):
        self.calls.append(("connect", self.plan["ip"]))
        return self

    def __exit__(self, *args):
        return None

    def upload(self, files):
        self.calls.append(("upload", self.plan["ip"], set(files)))
        self.uploaded.append(copy.deepcopy(files))
        for value in files.values():
            assert b"fake-provider-token" not in value
            assert b"fake-cloudflare-token" not in value
            assert b"fake-panel-token" not in value

    def action(self, name, capture=False):
        self.calls.append(("action", self.plan["ip"], name))
        if self.fail_action == name:
            raise e.d.u.DeployError("fake guest action failed")
        if name == "install":
            self.installed.add(self.plan["ip"])
        if name == "identity":
            return json.dumps({"machineId": MACHINE, "sshFingerprint": FINGERPRINT,
                               "ownedInstallation": self.plan["ip"] in self.installed}).encode()
        return b""

    def source(self, name, capture=False):
        self.calls.append(("source", self.plan["ip"], name))
        if name == "check":
            return json.dumps({"needsChange": False, "currentSource": self.plan["ip"]}).encode()
        return b""

    def assigned_ipv4(self):
        return {self.plan["ip"]}

    def run(self, command, data=None, capture=False, **kwargs):
        self.calls.append(("run", self.plan["ip"]))
        return b""


class FakeEnrollment:
    def __init__(self, private_key_path):
        self.private_key_path = private_key_path
        self.ssh_private_key = private_key_path
        self.calls, self.enrolled = [], True

    def ready(self):
        return {"ready": True, "reasons": []}

    def prepare(self, operation_id):
        self.calls.append(("prepare", operation_id))
        return {"ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA fixture",
                "user_data": "#cloud-config\n# one-use fixture"}

    def verify(self, operation_id, expected_ip):
        self.calls.append(("verify", operation_id, expected_ip))
        if not self.enrolled:
            raise ValueError("enrollment not confirmed")
        return {"host_key_sha256": FINGERPRINT, "machine_id": MACHINE, "ip": expected_ip}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.rf, self.outside, self.controls = True, True, True

    def readiness(self):
        return {"configured": True, "engine_verified": True, "outside_configured": True,
                "interface_configured": True, "linux_backend": True, "backend": "Linux"}

    def check(self, server, ip, *, outbound, expected_exit_ip):
        self.calls.append((server["id"], ip, copy.deepcopy(outbound), expected_exit_ip))
        assert expected_exit_ip == ip
        assert outbound["settings"]["vnext"][0]["address"] == ip
        now = time.time()
        return {"observed_at": now, "controls_ok": self.controls,
                "rf": [{"vpn_ok": self.rf, "controls_ok": self.controls, "observed_at": now}],
                "outside": [{"vpn_ok": self.outside, "controls_ok": self.controls, "observed_at": now}]}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    store = Store(tmp_path / "fleet" / "fleet.sqlite3")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture private key\n")
    key.chmod(0o600)
    credentials = types.SimpleNamespace(read=lambda: {
        "provider_token": "fake-provider-token", "cloudflare_token": "fake-cloudflare-token",
        "remnawave_token": "fake-panel-token", "remnawave_url": BASE,
        "probe_subscription_url": "https://sub.wayspn.online/fixture-secret"})
    config = {"engine": str(tmp_path / "xray"), "engine_sha256": "c" * 64,
              "interface": "eth0", "outside_command": ["/usr/local/bin/probe-worker"],
              "ssh_private_key": str(key), "os_image_id": IMAGE, "dns_zone": "wayspn.com", "wait": 120}
    enrollment, runner, api = FakeEnrollment(key), FakeRunner(), FakeAPI()
    owner = types.SimpleNamespace(store=store, data_dir=data_dir, credentials=credentials, config=config,
                                  enrollment=enrollment, runner=runner, api=api, dns_ip=None, dns_calls=[],
                                  dns_crash_after_apply=False, dns_error=None)

    class FakeDNS:
        def __init__(self, token):
            assert token == "fake-cloudflare-token"

        def find_zone(self, name):
            return {"id": ZONE, "name": name, "name_servers": ["alice.ns.cloudflare.com"]}

        def check_record(self, original, *, zone_id, name, ipv4, ttl=60):
            owner.dns_calls.append(("check", ipv4))
            if owner.dns_error:
                raise owner.dns_error
            if owner.dns_ip is not None:
                assert original.load("dns.receipt.json") is not None
            return None

        def ensure_record(self, original, *, zone_id, name, ipv4, ttl=60):
            intent = {"version": 1, "zone_id": zone_id, "name": name, "type": "A", "content": ipv4,
                      "proxied": False, "ttl": ttl, "comment": "SPN universal-reality:" + "d" * 32}
            original.save("dns.intent.json", intent)
            receipt = {**{k: v for k, v in intent.items() if k != "version"}, "id": RECORD}
            original.save("dns.receipt.json", receipt)
            owner.dns_calls.append(("create", ipv4))
            owner.dns_ip = ipv4
            return receipt

        def check_update(self, journal, *, receipt, old_ipv4, new_ipv4):
            owner.dns_calls.append(("check-update", old_ipv4, new_ipv4))
            if owner.dns_error:
                raise owner.dns_error
            assert receipt["id"] == RECORD
            assert owner.dns_ip in (old_ipv4, new_ipv4)
            return {**receipt, "content": owner.dns_ip}

        def update_record(self, journal, *, receipt, old_ipv4, new_ipv4):
            assert receipt["id"] == RECORD
            before, after = {**receipt, "content": old_ipv4}, {**receipt, "content": new_ipv4}
            intent = {"version": 1, "operation_id": "e" * 32, "before": before, "after": after}
            pending = {"version": 1, "operation_id": "e" * 32}
            journal.save(e.d.dns.UPDATE_INTENT, intent)
            journal.save(e.d.dns.UPDATE_PENDING, pending)
            if owner.dns_ip == old_ipv4:
                owner.dns_ip = new_ipv4
                owner.dns_calls.append(("update", old_ipv4, new_ipv4))
                if owner.dns_crash_after_apply:
                    owner.dns_crash_after_apply = False
                    raise SystemExit("fake process death after applied DNS PATCH")
            journal.save(e.d.dns.UPDATE_RECEIPT, {**pending, "record": after})
            return after

    def reality_keys(original):
        original.save("reality.private.json", KEYS)
        return copy.deepcopy(KEYS)

    FakeSSH.calls, FakeSSH.uploaded, FakeSSH.fail_action, FakeSSH.installed = [], [], None, set()
    monkeypatch.setattr(e.d, "API", lambda *_: api)
    monkeypatch.setattr(e, "FleetSSH", FakeSSH)
    monkeypatch.setattr(e.d, "panel_ipv4", lambda _: PANEL)
    monkeypatch.setattr(e.d, "reality_keys", reality_keys)
    monkeypatch.setattr(e.d.dns, "CloudflareDNS", FakeDNS)
    monkeypatch.setattr(e.d.trust, "resolve", lambda *args, **kwargs: tmp_path / "known-hosts")
    monkeypatch.setattr(e.d, "verify_authoritative_dns", lambda *_: None)
    monkeypatch.setattr(e.r, "_wait_dns", lambda *args, **kwargs: None)
    monkeypatch.setattr(e.d, "_dig", lambda resolver, domain, kind, **kwargs: [owner.dns_ip] if kind == "A" else [])
    monkeypatch.setattr(e.d.probe, "fetch_documents", lambda *_: {"outbounds": [{
        "tag": "sub-fixture", "protocol": "vless", "settings": {"vnext": [{"address": DOMAIN, "port": 443,
        "users": [{"id": USER, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
        "streamSettings": {"network": "raw", "security": "reality", "realitySettings": {
        "serverName": e.d.SNI, "fingerprint": "safari", "publicKey": KEYS["public"], "shortId": KEYS["short"]}}}]})
    monkeypatch.setattr(e.d.probe, "verify_export", lambda *args, **kwargs: {"passed": True,
                          "ip": args[0], "endpointAddress": kwargs["endpoint_address"]})
    owner.executor = e.GuestExecutor(store, credentials, data_dir, config, enrollment, probe_runner=runner)
    return owner


def make_candidate(fleet, *, action="create", server=None, ip=IP):
    server = server or fleet.store.create_server({"name": "Финляндия", "domain": DOMAIN, "country": "FI",
        "product_id": "44444444-4444-4444-8444-444444444444", "location": "fi", "currency": "EUR",
        "metadata": {"squads": [SQUAD]}})
    operation = fleet.store.enqueue_operation(action, server["id"], {"approved": True}, idempotency_key=str(uuid.uuid4()))
    operation["data"].update(previous_ip=server.get("current_ip"), previous_vps_id=server.get("current_vps_id"),
                             quote={"os_image_id": IMAGE})
    generation = {"vps_id": str(uuid.uuid4()), "operation_id": operation["id"], "server_id": server["id"],
                  "order_id": str(uuid.uuid4()), "ip": ip, "status": "bootstrap", "hourly_price": "0.01", "currency": "EUR"}
    return server, operation, generation


def registered_server(server, evidence):
    registration = evidence["registration"]
    return {**server, "original_state_path": registration["original_state_path"],
            "metadata": {**server["metadata"], **registration["metadata"]}}


def mark_terminal(fleet, operation):
    claimed = fleet.store.claim_operation("fixture", operation_id=operation["id"])
    fleet.store.update_operation(claimed["id"], expected_version=claimed["version"], worker_id="fixture", phase="complete")


def install_initial(fleet):
    server, operation, generation = make_candidate(fleet)
    evidence = fleet.executor.bootstrap(server, operation, generation)
    server = registered_server(server, evidence)
    server = fleet.store.update_server(server["id"], {"original_state_path": server["original_state_path"],
                                                     "metadata": server["metadata"]})
    fleet.executor.probe(server, generation)
    fleet.executor.cutover(server, operation, generation)
    evidence = fleet.executor.verify(server, operation, generation)
    assert evidence["ok"] is True
    server = fleet.store.update_server(server["id"], {"current_ip": IP})
    mark_terminal(fleet, operation)
    return server, operation, generation


def test_ready_requires_independent_runner_and_enrollment(fleet):
    assert fleet.executor.ready()["ready"] is True
    fleet.runner.readiness = lambda: {"configured": False}
    assert fleet.executor.ready()["ready"] is False


def test_ready_rejects_unconfirmed_enrollment(fleet):
    fleet.enrollment.ready = lambda: {"ready": False, "reasons": ["callback_unconfigured"]}
    assert fleet.executor.ready()["ready"] is False


@pytest.mark.parametrize("url", ["http://sub.wayspn.online/private", "https://sub.wayspn.online/",
                                  "https://other.example/private", "https://sub.wayspn.online/private?token=secret"])
def test_malformed_private_subscription_url_makes_executor_unready(fleet, url):
    prior = fleet.credentials.read()
    fleet.credentials.read = lambda: {**prior, "probe_subscription_url": url}
    evidence = fleet.executor.ready()
    assert evidence["ready"] is False
    assert "private_credentials_or_ssh_key_invalid" in evidence["reasons"]


def test_unenrolled_guest_never_gets_ssh_installation(fleet):
    server, operation, generation = make_candidate(fleet)
    fleet.enrollment.enrolled = False
    with pytest.raises(Exception):
        fleet.executor.bootstrap(server, operation, generation)
    assert FakeSSH.calls == []


def test_fleet_ssh_is_pinned_noninteractive_and_drops_control_plane_environment(fleet, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "do-not-forward")
    monkeypatch.setenv("REMNAWAVE_API_TOKEN", "do-not-forward")
    monkeypatch.setenv("VOXA_TOKEN", "do-not-forward")
    ssh = REAL_FLEET_SSH({"ip": NEW, "sshPort": 22}, fleet.data_dir / "known-hosts",
                         identity_file=fleet.config["ssh_private_key"])
    try:
        assert "StrictHostKeyChecking=yes" in ssh.common
        assert "IdentitiesOnly=yes" in ssh.common and "BatchMode=yes" in ssh.common
        assert "PasswordAuthentication=no" in ssh.common and "KbdInteractiveAuthentication=no" in ssh.common
        assert "ForwardAgent=no" in ssh.common and "ClearAllForwardings=yes" in ssh.common
        index = ssh.common.index("-i")
        assert ssh.common[index + 1] == fleet.config["ssh_private_key"]
        assert "CLOUDFLARE_API_TOKEN" not in ssh.environment
        assert "REMNAWAVE_API_TOKEN" not in ssh.environment
        assert "VOXA_TOKEN" not in ssh.environment
    finally:
        ssh.tmp.cleanup()


def test_prepare_order_has_public_key_and_one_use_userdata_only(fleet):
    server, operation, _ = make_candidate(fleet)
    options = fleet.executor.prepare_order(server, operation)
    assert options["os_image_id"] == IMAGE
    assert options["ssh_public_key"].startswith("ssh-ed25519 ")
    assert options["user_data"].startswith("#cloud-config")
    encoded = json.dumps(options)
    assert "private key" not in encoded
    assert "fake-provider-token" not in encoded
    assert "fake-panel-token" not in encoded
    assert "fake-cloudflare-token" not in encoded


def test_initial_bootstrap_keeps_host_disabled_and_preserves_registration(fleet):
    server, operation, generation = make_candidate(fleet)
    evidence = fleet.executor.bootstrap(server, operation, generation)
    assert evidence["ok"] is True and evidence["ids_preserved"] is True
    refs = evidence["registration"]["metadata"]
    assert set(refs) == {"node_id", "host_id", "profile_id", "inbound_id", "cloudflare_record_id", "installation_code"}
    assert refs["cloudflare_record_id"] == RECORD
    assert len(fleet.api.data["nodes"]) == len(fleet.api.data["hosts"]) == len(fleet.api.data["config-profiles"]) == 1
    assert fleet.api.data["hosts"][0]["isDisabled"] is True
    before = copy.deepcopy(fleet.api.data)
    fleet.executor.bootstrap(server, operation, generation)
    assert fleet.api.data == before
    assert not any(method == "PATCH" and path == "/hosts" and body.get("isDisabled") is False
                   for method, path, body in fleet.api.calls)
    assert (fleet.executor._journal(server, operation, generation).path / "bootstrap.receipt.json").exists()


def test_recover_bootstrap_replays_registration_after_controller_adoption(fleet):
    server, operation, generation = make_candidate(fleet)
    first = fleet.executor.bootstrap(server, operation, generation)
    adopted = registered_server(server, first)
    before = copy.deepcopy(fleet.api.data)
    recovered = fleet.executor.recover("bootstrap", adopted, operation, generation)
    assert recovered == first
    assert recovered["registration"] == first["registration"]
    assert fleet.api.data == before


def test_initial_partial_preflight_failure_can_close_exact_unpublished_candidate(fleet):
    server, operation, generation = make_candidate(fleet)
    FakeSSH.fail_action = "preflight"
    with pytest.raises(Exception):
        fleet.executor.bootstrap(server, operation, generation)
    assert fleet.api.data["nodes"] == [] and fleet.api.data["hosts"] == []
    FakeSSH.fail_action = None
    evidence = fleet.executor.restore(server, operation, generation)
    assert evidence["ok"] is True and evidence["ids_preserved"] is True
    journal = fleet.executor._journal(server, operation, generation)
    assert journal.load("cancelled.json")["vps_id"] == generation["vps_id"]
    assert journal.load("restore.receipt.json") == evidence
    assert fleet.dns_ip == IP


def test_initial_host_is_published_only_after_real_rf_and_outside_probe(fleet):
    server, operation, generation = make_candidate(fleet)
    evidence = fleet.executor.bootstrap(server, operation, generation)
    server = registered_server(server, evidence)
    fleet.executor.probe(server, generation)
    cutover = fleet.executor.cutover(server, operation, generation)
    assert cutover == {"ok": True, "ids_preserved": True, "dns_record_preserved": True}
    assert fleet.api.data["hosts"][0]["isDisabled"] is False
    verified = fleet.executor.verify(server, operation, generation)
    assert verified["ok"] is True and verified["dns_converged"] is True and verified["ids_preserved"] is True
    assert verified["round"]["rf"][0]["vpn_ok"] is True
    assert verified["round"]["outside"][0]["vpn_ok"] is True
    assert all(call[1] == IP and call[3] == IP for call in fleet.runner.calls)


def test_initial_cutover_refuses_bad_rf_despite_good_ssh_and_outside(fleet):
    server, operation, generation = make_candidate(fleet)
    evidence = fleet.executor.bootstrap(server, operation, generation)
    server = registered_server(server, evidence)
    fleet.runner.rf = False
    fleet.executor.probe(server, generation)
    with pytest.raises(Exception):
        fleet.executor.cutover(server, operation, generation)
    assert fleet.api.data["hosts"][0]["isDisabled"] is True


def test_initial_cutover_refuses_missing_outside_evidence(fleet):
    server, operation, generation = make_candidate(fleet)
    evidence = fleet.executor.bootstrap(server, operation, generation)
    server = registered_server(server, evidence)
    fleet.runner.check = lambda *args, **kwargs: {"observed_at": time.time(), "controls_ok": True,
                                                "rf": [{"vpn_ok": True}], "outside": []}
    fleet.executor.probe(server, generation)
    with pytest.raises(Exception):
        fleet.executor.cutover(server, operation, generation)
    assert fleet.api.data["hosts"][0]["isDisabled"] is True


def test_replacement_bootstrap_does_not_change_dns_or_host_and_keeps_all_ids(fleet):
    server, _, _ = install_initial(fleet)
    fleet.api.data["nodes"][0].update(name="Собственное имя ноды", note="Owner note", tags=["CUSTOM_NODE"])
    fleet.api.data["hosts"][0].update(remark="Собственный хост", tags=["CUSTOM_HOST"])
    fleet.api.data["config-profiles"][0]["name"] = "Owner profile label"
    server, operation, generation = make_candidate(fleet, action="replace", server=server, ip=NEW)
    original = e.d.u.Store(Path(server["original_state_path"]))
    original_bytes = {p.name: p.read_bytes() for p in original.path.iterdir() if p.is_file()}
    panel_before = copy.deepcopy(fleet.api.data)
    fleet.api.calls.clear()
    evidence = fleet.executor.bootstrap(server, operation, generation)
    assert evidence["ok"] is True and evidence["ids_preserved"] is True
    assert fleet.api.data["nodes"][0]["address"] == NEW
    assert fleet.dns_ip == IP
    assert fleet.api.data["hosts"] == panel_before["hosts"]
    assert fleet.api.data["config-profiles"] == panel_before["config-profiles"]
    assert fleet.api.data["internal-squads"] == panel_before["internal-squads"]
    assert not any(method == "POST" and path in ("/nodes", "/hosts", "/config-profiles")
                   for method, path, _ in fleet.api.calls)
    for name, content in original_bytes.items():
        assert (original.path / name).read_bytes() == content


def test_existing_profile_drift_is_refused_before_any_guest_actions_or_source_state(fleet):
    server, _, _ = install_initial(fleet)
    server, operation, generation = make_candidate(fleet, action="replace", server=server, ip=NEW)
    fleet.api.data["config-profiles"][0]["config"]["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] = "x" * 43
    FakeSSH.calls.clear()
    fleet.api.calls.clear()
    probe_count = len(fleet.runner.calls)
    with pytest.raises(e.ExecutorError, match="profile technical configuration"):
        fleet.executor.bootstrap(server, operation, generation)
    assert FakeSSH.calls == []
    assert fleet.api.calls == []
    assert len(fleet.runner.calls) == probe_count
    assert fleet.dns_ip == IP


def test_replacement_cutover_changes_only_existing_dns_record_and_node_route(fleet):
    server, _, _ = install_initial(fleet)
    server, operation, generation = make_candidate(fleet, action="replace", server=server, ip=NEW)
    before = copy.deepcopy(fleet.api.data)
    fleet.executor.bootstrap(server, operation, generation)
    fleet.executor.probe(server, generation)
    evidence = fleet.executor.cutover(server, operation, generation)
    assert evidence["ok"] is True and evidence["dns_record_preserved"] is True
    assert fleet.dns_ip == NEW
    assert fleet.api.data == before
    assert ("update", IP, NEW) in fleet.dns_calls
    verified = fleet.executor.verify(server, operation, generation)
    assert verified["ok"] is True and verified["dns_converged"] is True
    assert fleet.runner.calls[-1][1] == NEW


def test_rejected_candidate_restores_only_owned_node_before_cleanup(fleet):
    server, _, _ = install_initial(fleet)
    server, operation, generation = make_candidate(fleet, action="replace", server=server, ip=NEW)
    before = copy.deepcopy(fleet.api.data)
    fleet.executor.bootstrap(server, operation, generation)
    fleet.runner.rf = False
    fleet.executor.probe(server, generation)
    evidence = fleet.executor.restore(server, operation, generation)
    assert evidence["ok"] is True and evidence["ids_preserved"] is True
    assert fleet.api.data == before
    assert fleet.dns_ip == IP
    journal = fleet.executor._journal(server, operation, generation)
    assert journal.load("cancelled.json") is not None
    assert journal.load("restore.receipt.json") is not None


def test_cleanup_refuses_to_delete_candidate_after_dns_cutover(fleet):
    server, _, _ = install_initial(fleet)
    server, operation, generation = make_candidate(fleet, action="replace", server=server, ip=NEW)
    fleet.executor.bootstrap(server, operation, generation)
    fleet.executor.probe(server, generation)
    fleet.executor.cutover(server, operation, generation)
    with pytest.raises(Exception):
        fleet.executor.restore(server, operation, generation)
    assert fleet.dns_ip == NEW
    assert fleet.executor._journal(server, operation, generation).load("cancelled.json") is None


def test_observe_runs_actual_probe_not_ssh_diagnostic(fleet):
    server, _, _ = install_initial(fleet)
    FakeSSH.calls.clear()
    count = len(fleet.runner.calls)
    evidence = fleet.executor.observe(server)
    assert evidence["rf"][0]["vpn_ok"] is True and evidence["outside"][0]["vpn_ok"] is True
    assert len(fleet.runner.calls) == count + 1
    assert FakeSSH.calls == []


def test_monitor_refuses_owner_disabled_published_host_before_probe_runner(fleet):
    server, _, _ = install_initial(fleet)
    fleet.api.data["hosts"][0]["isDisabled"] = True
    FakeSSH.calls.clear()
    fleet.api.calls.clear()
    before = len(fleet.runner.calls)
    with pytest.raises(e.ExecutorError, match="disabled published host"):
        fleet.executor.observe(server)
    assert len(fleet.runner.calls) == before
    assert FakeSSH.calls == [] and fleet.api.calls == []


def test_recover_resumes_same_owned_bootstrap_after_lost_address_patch(fleet):
    server, _, _ = install_initial(fleet)
    server, operation, generation = make_candidate(fleet, action="replace", server=server, ip=NEW)
    fleet.api.lose = "PATCH"
    with pytest.raises(Exception):
        fleet.executor.bootstrap(server, operation, generation)
    assert fleet.api.data["nodes"][0]["address"] == NEW
    evidence = fleet.executor.recover("bootstrap", server, operation, generation)
    assert evidence["ok"] is True and evidence["ids_preserved"] is True
    assert fleet.api.data["nodes"][0]["isDisabled"] is False
    assert len(fleet.api.data["nodes"]) == len(fleet.api.data["hosts"]) == 1


def test_recover_cutover_reconciles_applied_dns_without_second_patch(fleet):
    server, _, _ = install_initial(fleet)
    server, operation, generation = make_candidate(fleet, action="replace", server=server, ip=NEW)
    fleet.executor.bootstrap(server, operation, generation)
    fleet.executor.probe(server, generation)
    fleet.dns_crash_after_apply = True
    with pytest.raises(SystemExit):
        fleet.executor.cutover(server, operation, generation)
    assert fleet.dns_ip == NEW
    journal = fleet.executor._journal(server, operation, generation)
    assert journal.load(e.d.dns.UPDATE_PENDING) is not None
    assert journal.load("cutover.receipt.json") is None
    recovered = fleet.executor.recover("cutover", server, operation, generation)
    assert recovered["ok"] is True and recovered["dns_record_preserved"] is True
    assert [call for call in fleet.dns_calls if call[0] == "update"] == [("update", IP, NEW)]
    assert fleet.api.data["nodes"][0]["address"] == DOMAIN


def test_recover_cutover_does_not_reenable_owner_disabled_confirmed_publication(fleet):
    server, operation, generation = make_candidate(fleet)
    evidence = fleet.executor.bootstrap(server, operation, generation)
    server = registered_server(server, evidence)
    fleet.executor.probe(server, generation)
    fleet.executor.cutover(server, operation, generation)
    journal = fleet.executor._journal(server, operation, generation)
    assert journal.load("host-publish.receipt.json") is not None
    fleet.api.data["hosts"][0]["isDisabled"] = True
    fleet.api.calls.clear()
    with pytest.raises(e.ExecutorError, match="disabled after this operation"):
        fleet.executor.recover("cutover", server, operation, generation)
    assert fleet.api.data["hosts"][0]["isDisabled"] is True
    assert fleet.api.calls == []


def test_recover_restore_rechecks_actual_dns_instead_of_replaying_cached_success(fleet):
    server, _, _ = install_initial(fleet)
    server, operation, generation = make_candidate(fleet, action="replace", server=server, ip=NEW)
    fleet.executor.bootstrap(server, operation, generation)
    fleet.runner.rf = False
    fleet.executor.probe(server, generation)
    first = fleet.executor.restore(server, operation, generation)
    assert first["ok"] is True
    fleet.dns_error = e.d.dns.DNSConflict("fake externally changed DNS identity")
    before = len(fleet.dns_calls)
    with pytest.raises(e.d.dns.DNSConflict):
        fleet.executor.recover("restore", server, operation, generation)
    assert len(fleet.dns_calls) > before
    assert fleet.api.data["nodes"][0]["address"] == DOMAIN


def test_provisioning_failed_generation_without_ip_can_restore_no_guest_effects(fleet):
    server, operation, generation = make_candidate(fleet)
    generation.update(ip=None, status="rejected")
    before = copy.deepcopy(fleet.api.data)
    evidence = fleet.executor.restore(server, operation, generation)
    assert evidence == {"ok": True, "ids_preserved": True}
    journal = fleet.executor._journal(server, operation, generation)
    assert journal.load("intent.json") is None
    assert journal.load("cancelled.json") == {"version": 1, "vps_id": generation["vps_id"], "reason": "no_guest_effects"}
    assert fleet.api.data == before
    assert fleet.dns_calls == []
    assert FakeSSH.calls == []


def test_rejected_initial_candidate_retry_reuses_first_registered_identity(fleet):
    server, operation, generation = make_candidate(fleet)
    evidence = fleet.executor.bootstrap(server, operation, generation)
    server = registered_server(server, evidence)
    server = fleet.store.update_server(server["id"], {"original_state_path": server["original_state_path"],
                                                     "metadata": server["metadata"]})
    original = e.d.u.Store(Path(server["original_state_path"]))
    keys, service_secret = original.load("reality.private.json"), original.load("node-secret.json")
    ids = {kind: [row["uuid"] for row in fleet.api.data[kind]] for kind in ("nodes", "hosts", "config-profiles")}
    fleet.runner.rf = False
    fleet.executor.probe(server, generation)
    assert fleet.executor.restore(server, operation, generation)["ok"] is True
    mark_terminal(fleet, operation)
    server, retry, new_generation = make_candidate(fleet, server=server, ip=NEW)
    fleet.api.calls.clear()
    fleet.runner.rf = True
    evidence = fleet.executor.bootstrap(server, retry, new_generation)
    assert evidence["ok"] is True and evidence["ids_preserved"] is True
    assert {kind: [row["uuid"] for row in fleet.api.data[kind]] for kind in ids} == ids
    assert original.load("reality.private.json") == keys
    assert original.load("node-secret.json") == service_secret
    assert fleet.api.data["hosts"][0]["isDisabled"] is True
    assert not any(method == "POST" and path in ("/nodes", "/hosts", "/config-profiles")
                   for method, path, _ in fleet.api.calls)
    fleet.executor.probe(server, new_generation)
    assert fleet.executor.cutover(server, retry, new_generation)["ok"] is True
    assert fleet.dns_ip == NEW
    assert fleet.api.data["hosts"][0]["isDisabled"] is False


def test_linux_executor_never_selects_legacy_macos_probe_engine(fleet, monkeypatch):
    monkeypatch.setattr(e.d.probe, "select_engine", lambda *_: pytest.fail("legacy Mac-only probe selector used"))
    server, operation, generation = make_candidate(fleet)
    evidence = fleet.executor.bootstrap(server, operation, generation)
    server = registered_server(server, evidence)
    round_ = fleet.executor.probe(server, generation)
    assert round_["controls_ok"] is True
    assert fleet.runner.calls[-1][2]["streamSettings"]["realitySettings"]["fingerprint"] == "safari"
