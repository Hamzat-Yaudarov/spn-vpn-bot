"""Local-only guards for the one approved manual Sweden epoch handoff."""
import base64
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


epoch = load("sweden_epoch_review", "ops/ip-rotation-five/reconcile_sweden_epoch.py")
enroll = load("sweden_epoch_enroll_review", "ops/ip-rotation-five/enroll_netplan.py")
util = load("sweden_epoch_util_review", "ops/ip-rotation-five/guest_enrollment.py")
OLD, NEW = "177.3.250.133", "177.3.250.179"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
FINGERPRINT = "SHA256:" + "A" * 43


def proof():
    return {
        "format": 1, "service_id": epoch.SERVICE, "old_ip": OLD, "new_ip": NEW,
        "observed_at": NOW.isoformat(), "user_confirmed_manual_change": True,
        "provider": {"service_id": epoch.SERVICE, "name": epoch.SERVICE_NAME,
                     "primary_ip": NEW, "assigned_ipv4": [NEW]},
        "remnawave": {"host_uuid": epoch.HOST, "node_uuid": epoch.NODE,
                      "host_address": NEW, "node_address": NEW, "host_node_uuids": [epoch.NODE],
                      "node_connected": True, "host_disabled": False, "node_disabled": False},
        "ssh": {"trusted_alias": epoch.SSH_ALIAS, "fingerprint": FINGERPRINT},
        "financial": {"rotation_running": False, "scope_service_id": epoch.SERVICE,
                      "controller_orders": 0, "pending_orders": 0, "global_pending_orders": 0,
                      "sweden_attempts": 0, "orders_journal_exists": False,
                      "orders_journal_sha256": None, "budget_journal_exists": False,
                      "budget_journal_sha256": None, "pilot_fence_exists": False},
    }


def validate(value):
    return epoch.validate_agreement(value, OLD, NEW, enroll, now=NOW)


def test_agreement_refresh_keeps_same_identity_but_revalidates_financial_proof():
    first = proof()
    second = deepcopy(first)
    second["observed_at"] = (NOW - timedelta(seconds=300)).isoformat()
    second["financial"].update(orders_journal_exists=True, orders_journal_sha256="a" * 64)
    assert validate(first) == validate(second)
    assert "financial" not in validate(first) and "observed_at" not in validate(first)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(service_id="21e508f0-2ef2-47da-aee7-15dc8c66eb15"),
    lambda p: p.update(user_confirmed_manual_change=False),
    lambda p: p["provider"].update(assigned_ipv4=[NEW, OLD]),
    lambda p: p["provider"].update(primary_ip=OLD),
    lambda p: p["remnawave"].update(host_node_uuids=[epoch.NODE, epoch.HOST]),
    lambda p: p["remnawave"].update(node_address=OLD),
    lambda p: p["ssh"].update(trusted_alias=NEW),
    lambda p: p["ssh"].update(fingerprint="SHA256:not-pinned"),
    lambda p: p.update(observed_at=(NOW - timedelta(seconds=601)).isoformat()),
    lambda p: p.update(observed_at=(NOW + timedelta(seconds=31)).isoformat()),
    lambda p: p.update(observed_at="2026-11-16T12:00:00"),
    lambda p: p.update(extra="not permitted"),
])
def test_identity_and_freshness_fail_closed(mutate):
    value = proof()
    mutate(value)
    with pytest.raises(ValueError):
        validate(value)


@pytest.mark.parametrize("field,value", [
    ("rotation_running", True), ("scope_service_id", epoch.NODE), ("controller_orders", 1),
    ("pending_orders", 1), ("global_pending_orders", 1), ("sweden_attempts", 1),
    ("controller_orders", False), ("controller_orders", "0"), ("pilot_fence_exists", True),
    ("orders_journal_exists", 1), ("orders_journal_sha256", "a" * 64),
    ("budget_journal_sha256", "not a hash"),
])
def test_financial_zero_state_cannot_be_inferred(field, value):
    agreement = proof()
    agreement["financial"][field] = value
    with pytest.raises(ValueError):
        validate(agreement)


def test_existing_financial_journal_requires_exact_hash_without_erasing_it():
    value = proof()
    value["financial"].update(orders_journal_exists=True)
    with pytest.raises(ValueError):
        validate(value)
    value["financial"]["orders_journal_sha256"] = "b" * 64
    assert validate(value)["service_id"] == epoch.SERVICE


def test_yaml_identity_erases_only_the_two_authorized_ipv4_fields():
    def tree(ip):
        return {"network": {"version": 2, "ethernets": {"eth0": {
            "addresses": [ip + "/32", "2001:db8::1/64"],
            "routes": [{"to": "default", "via": "10.0.0.1", "from": ip}],
            "nameservers": {"addresses": ["1.1.1.1"]}}}}}
    old, new = tree(OLD), tree(NEW)
    assert epoch.identity_without_v4(old, "eth0", OLD, NEW) == epoch.identity_without_v4(new, "eth0", OLD, NEW)
    assert old["network"]["ethernets"]["eth0"]["addresses"][0] == OLD + "/32"
    new["network"]["ethernets"]["eth0"]["nameservers"]["addresses"] = ["8.8.8.8"]
    assert epoch.identity_without_v4(old, "eth0", OLD, NEW) != epoch.identity_without_v4(new, "eth0", OLD, NEW)
    new["network"]["ethernets"]["eth0"]["addresses"].append(OLD + "/32")
    with pytest.raises(ValueError):
        epoch.identity_without_v4(new, "eth0", OLD, NEW)


def record(raw):
    return {"uid": 0, "gid": 0, "mode": 0o600, "size": len(raw),
            "sha256": util.digest(raw), "base64": base64.b64encode(raw).decode()}


class ApplyFixture:
    """In-memory files/guest; no subprocess, networking, real locks or root files."""
    def __init__(self, tmp_path):
        self.original = b'{"state_directory":"old","untouched":true}\n'
        self.fresh_config = {"state_directory": str(tmp_path / "epoch"), "untouched": True}
        self.files = {epoch.CONFIG: self.original, epoch.PREFIX / ".service-id": (epoch.SERVICE + "\n").encode(),
                      epoch.OLD_STATE / "state.json": b'{"untouched-old-journal":true}\n',
                      epoch.OLD_STATE / "bootstrap-original.interfaces": b"old yaml\n"}
        self.old_settings = SimpleNamespace(state_directory=epoch.OLD_STATE)
        self.settings = SimpleNamespace(state_directory=tmp_path / "epoch")
        self.snapshot = {"format": 1, "service_id": epoch.SERVICE, "old_ip": OLD, "new_ip": NEW,
                         "ssh_fingerprint": FINGERPRINT, "egress_ipv4": NEW,
                         "current": {"netplan": record(b"current yaml\n"), "generated": record(b"current generated\n")},
                         "old": {"config": record(self.original), "state": record(self.files[epoch.OLD_STATE / "state.json"]),
                                 "history": None, "bootstrap_backup": record(b"old yaml\n"), "enrollment": {}}}
        for name in epoch.ENROLLMENT_FILES:
            self.files[epoch.OLD_ENROLLMENT / name] = (name + " unchanged\n").encode()
            self.snapshot["old"]["enrollment"][name] = record(self.files[epoch.OLD_ENROLLMENT / name])
        self.plan = {"epoch": "a" * 64, "settings": self.settings, "old_settings": self.old_settings,
                     "fresh_config": self.fresh_config, "identity": epoch.agreement_identity(proof()),
                     "rendered": b"new canonical yaml\n", "generated_after": b"new generated\n"}
        self.helper = SimpleNamespace(body=util.body, encoded=util.encoded, digest=util.digest,
                                      record=self.record, create_directory=Mock(), write_once=self.write_once)
        self.guest = Mock()
        self.guest.run.return_value = {"ok": True}
        self.store = Mock(locked=lambda: nullcontext())
        self.netplan = SimpleNamespace(FileStore=Mock(return_value=self.store), ManagedGuest=Mock(return_value=self.guest),
                                       SystemKernel=Mock())
        self.enroll = SimpleNamespace(capture=Mock(return_value=self.snapshot["current"]), current_matches=Mock())
        self.managed = SimpleNamespace(atomic_write=Mock(side_effect=self.atomic_write))
        self.archives = tmp_path / "archives" / epoch.SERVICE
        self.changed_before_commit = False

    def record(self, path, *, absent=False):
        if absent and path not in self.files:
            return None
        return record(self.files[path])

    def write_once(self, path, raw, managed):
        if path in self.files:
            assert self.files[path] == raw, "immutable archive replaced"
        else:
            self.files[path] = raw

    def atomic_write(self, path, raw, mode, *, before_replace):
        if self.changed_before_commit:
            self.files[path] = b"external config pointer\n"
        before_replace()
        self.files[path] = raw

    def apply(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
        with patch.object(epoch, "ARCHIVES", self.archives), patch.object(epoch.os, "open", return_value=123), \
             patch.object(epoch.os, "fstat", return_value=info), patch.object(epoch.os, "close"), \
             patch.object(epoch.fcntl, "flock"), patch.object(epoch, "validate_agreement", return_value=self.plan["identity"]), \
             patch.object(epoch, "ssh_fingerprint", return_value=FINGERPRINT), patch.object(epoch, "egress", return_value=NEW):
            return epoch.apply(self.snapshot, proof(), self.plan, self.enroll, self.helper, self.managed, self.netplan)


def test_apply_preserves_old_journal_and_all_receipts_and_creates_fresh_epoch(tmp_path):
    fixture = ApplyFixture(tmp_path)
    before = dict(fixture.files)
    result = fixture.apply()
    assert result["ready"] and result["paid_orders_submitted"] == 0
    assert not result["financial_counters_changed"] and not result["network_restarted"] and not result["vpn_restarted"]
    assert result["new_state_directory"] != str(epoch.OLD_STATE)
    for path, raw in before.items():
        if path != epoch.CONFIG:
            assert fixture.files[path] == raw
    assert fixture.files[epoch.CONFIG] == util.encoded(fixture.fresh_config)
    assert json.loads(fixture.files[fixture.archives / fixture.plan["epoch"] / "complete.json"])["ready"]
    fixture.managed.atomic_write.assert_called_once()


def test_apply_retry_is_same_epoch_and_does_not_republish_pointer(tmp_path):
    fixture = ApplyFixture(tmp_path)
    first = fixture.apply()
    archive_before = {p: v for p, v in fixture.files.items() if p.is_relative_to(fixture.archives)}
    second = fixture.apply()
    assert second == first
    assert archive_before == {p: v for p, v in fixture.files.items() if p.is_relative_to(fixture.archives)}
    fixture.managed.atomic_write.assert_called_once()


def test_config_pointer_cas_refuses_drift_without_overwriting_external_config(tmp_path):
    fixture = ApplyFixture(tmp_path)
    fixture.changed_before_commit = True
    with pytest.raises(ValueError, match="pointer changed before commit"):
        fixture.apply()
    assert fixture.files[epoch.CONFIG] == b"external config pointer\n"
    assert fixture.archives / fixture.plan["epoch"] / "complete.json" not in fixture.files
    assert fixture.files[epoch.OLD_STATE / "state.json"] == b'{"untouched-old-journal":true}\n'


@pytest.mark.parametrize("changed", ["state", "receipt", "history", "pointer"])
def test_old_epoch_drift_blocks_bootstrap_before_any_pointer_write(tmp_path, changed):
    fixture = ApplyFixture(tmp_path)
    path = {"state": epoch.OLD_STATE / "state.json", "receipt": epoch.OLD_ENROLLMENT / "intent.json",
            "history": epoch.OLD_STATE / "netplan-generated-history.json", "pointer": epoch.CONFIG}[changed]
    fixture.files[path] = b"external edit\n"
    with pytest.raises(ValueError):
        fixture.apply()
    fixture.guest.run.assert_not_called()
    fixture.managed.atomic_write.assert_not_called()
