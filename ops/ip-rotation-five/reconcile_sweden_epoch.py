#!/usr/bin/env python3
"""Explicit manual-primary reconciliation into a new Sweden guest epoch.

This tool never buys/releases IPs, rewrites financial journals, changes panel
resources, restarts networking, or removes the previous guest state. A verified
operator agreement and a frozen private guest snapshot are required.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import types


SERVICE = "f5c0287a-636e-4981-95c7-80b24c9559c0"
SERVICE_NAME = "srv-mwfa434s"
HOST = "7737ba07-6d54-435c-8c01-aea4fe1369b9"
NODE = "5c472650-1f93-470a-abdb-0caadef059b7"
SSH_ALIAS = "177.3.250.75"
PREFIX = Path("/opt/spn-ip-failover-guest")
CONFIG = Path("/etc/spn-ip-failover-guest.json")
OLD_STATE = Path("/var/lib/spn-ip-failover-guest")
OLD_ENROLLMENT = Path("/var/lib/spn-ip-failover-netplan-enrollment") / SERVICE
EPOCHS = Path("/var/lib/spn-ip-failover-guest-epochs") / SERVICE
ARCHIVES = Path("/var/lib/spn-ip-failover-manual-reconcile") / SERVICE
ENROLLMENT_FILES = ("snapshot.json", "intent.json", "complete.json", "netplan-original.yaml",
                    "networkd-original.network", "cloud-init-before.json")
SHA = re.compile(r"[0-9a-f]{64}")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def root_module(path, name):
    for parent in path.parents:
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
             "Untrusted source parent")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
             and not info.st_mode & 0o022, "Untrusted source file")
        raw = stream.read(262145)
    need(0 < len(raw) <= 262144, "Source exceeds limit")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def load_runtime(package_root):
    enroll = root_module(Path(__file__).with_name("enroll_netplan.py"), "_spn_epoch_enroll")
    helper, managed, netplan = enroll.load_modules(package_root)
    return enroll, helper, managed, netplan


def parsed(record, helper):
    return json.loads(helper.body(record, mode=0o600), object_pairs_hook=helper.unique)


def agreement_identity(value):
    return {key: value[key] for key in ("format", "service_id", "old_ip", "new_ip", "provider", "remnawave", "ssh",
                                      "user_confirmed_manual_change")}


def validate_agreement(value, old_ip, new_ip, enroll, *, now=None):
    enroll.public_ip(old_ip)
    enroll.public_ip(new_ip)
    need(old_ip != new_ip, "Manual change requires two distinct public IPv4 addresses")
    need(isinstance(value, dict) and set(value) == {"format", "service_id", "old_ip", "new_ip", "observed_at",
                                                  "provider", "remnawave", "ssh", "financial",
                                                  "user_confirmed_manual_change"}
         and value["format"] == 1 and value["service_id"] == SERVICE
         and value["old_ip"] == old_ip and value["new_ip"] == new_ip
         and value["user_confirmed_manual_change"] is True, "Agreement does not authorize this manual Sweden change")
    observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    need(observed.tzinfo is not None and observed.utcoffset().total_seconds() == 0,
         "Agreement needs an explicit UTC observation time")
    age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    need(-30 <= age <= 600, "Agreement is stale; refresh read-only provider/panel/financial observations")
    need(value["provider"] == {"service_id": SERVICE, "name": SERVICE_NAME,
                               "primary_ip": new_ip, "assigned_ipv4": [new_ip]},
         "Provider does not show exactly the new primary and no extra IPs")
    need(value["remnawave"] == {"host_uuid": HOST, "node_uuid": NODE, "host_address": new_ip,
                                "node_address": new_ip, "host_node_uuids": [NODE], "node_connected": True,
                                "host_disabled": False, "node_disabled": False},
         "Panel does not uniquely agree with the selected provider service")
    ssh = value["ssh"]
    need(isinstance(ssh, dict) and set(ssh) == {"trusted_alias", "fingerprint"}
         and ssh["trusted_alias"] == SSH_ALIAS and isinstance(ssh["fingerprint"], str)
         and re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", ssh["fingerprint"]),
         "Use the previously trusted manual-75 SSH key, never a newly scanned key")
    financial = value["financial"]
    financial_fields = {
        "rotation_running", "scope_service_id", "controller_orders", "pending_orders", "global_pending_orders",
        "sweden_attempts", "orders_journal_exists", "orders_journal_sha256", "budget_journal_exists",
        "budget_journal_sha256", "pilot_fence_exists"}
    reviewed_fields = {"controller_journals", "settled_other_orders", "pilot_evidence"}
    need(isinstance(financial, dict) and set(financial) in (financial_fields, financial_fields | reviewed_fields),
         "Financial proof is incomplete")
    need(financial["rotation_running"] is False and financial["scope_service_id"] == SERVICE
         and all(type(financial[key]) is int and financial[key] == 0
                 for key in ("pending_orders", "global_pending_orders", "sweden_attempts")),
         "An active/unknown controller operation or prior Sweden attempt prevents a fresh guest epoch")
    need(type(financial["controller_orders"]) is int and financial["controller_orders"] in (0, 1)
         and type(financial["pilot_fence_exists"]) is bool, "Unknown global order/pilot state")
    for prefix in ("orders", "budget"):
        exists, digest = financial[prefix + "_journal_exists"], financial[prefix + "_journal_sha256"]
        need(type(exists) is bool and (isinstance(digest, str) and SHA.fullmatch(digest) if exists else digest is None),
             "Financial journal presence/hash is not confirmed")
    if set(financial) == financial_fields:
        need(financial["controller_orders"] == 0 and financial["pilot_fence_exists"] is False,
             "A settled other-service pilot requires its explicit reviewed evidence")
    else:
        controllers = financial["controller_journals"]
        paths = {"/var/lib/spn-ip-failover/state.json", "/var/lib/spn-ip-failover/rotation/state.json"}
        need(isinstance(controllers, list) and len(controllers) == 2
             and all(isinstance(row, dict) and set(row) == {"path", "exists", "sha256"}
                     and row["path"] in paths and type(row["exists"]) is bool
                     and (isinstance(row["sha256"], str) and SHA.fullmatch(row["sha256"]) if row["exists"]
                          else row["sha256"] is None) for row in controllers)
             and {row["path"] for row in controllers} == paths, "Controller journal evidence is incomplete")
        settled, pilot = financial["settled_other_orders"], financial["pilot_evidence"]
        need(isinstance(settled, list) and len(settled) == financial["controller_orders"],
             "Global order count differs from the reviewed settled pilot")
        if not settled:
            need(pilot is None and financial["pilot_fence_exists"] is False,
                 "Pilot fence without a settled matching order requires review")
        else:
            from decimal import Decimal
            row = settled[0]
            other_services = {"21e508f0-2ef2-47da-aee7-15dc8c66eb15", "4d6f358a-e75f-4dd8-a635-c6ad320f2732",
                              "9b0316d5-fa83-4bf8-b6dc-8202b66622c9", "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d"}
            need(isinstance(row, dict) and set(row) == {"service_id", "request_id", "ip", "phase", "release_phase",
                 "receipt_sha256", "response_body_sha256", "budget_key", "budget_phase", "amount_rub"}
                 and row["service_id"] in other_services and row["request_id"] == "five-vps-pilot-20261116-001"
                 and row["phase"] == row["release_phase"] == "released" and row["budget_phase"] == "confirmed"
                 and all(isinstance(row[key], str) and SHA.fullmatch(row[key])
                         for key in ("receipt_sha256", "response_body_sha256", "budget_key"))
                 and row["budget_key"] == hashlib.sha256((row["service_id"] + "\x00" + row["request_id"]).encode()).hexdigest()
                 and isinstance(row["amount_rub"], str) and re.fullmatch(r"(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?", row["amount_rub"])
                 and Decimal("0") <= Decimal(row["amount_rub"]) <= Decimal("100.00"),
                 "Other-service order lacks confirmed/released receipt-bound evidence")
            enroll.public_ip(row["ip"])
            need(isinstance(pilot, dict) and set(pilot) == {"service_id", "request_id", "ip", "intent_sha256",
                 "dispatch_sha256", "response_sha256", "proof_sha256"}
                 and all(pilot[key] == row[key] for key in ("service_id", "request_id", "ip"))
                 and all(isinstance(pilot[key], str) and SHA.fullmatch(pilot[key])
                         for key in ("intent_sha256", "dispatch_sha256", "response_sha256", "proof_sha256"))
                 and pilot["response_sha256"] == row["receipt_sha256"]
                 and financial["pilot_fence_exists"] is True
                 and financial["orders_journal_exists"] is True and financial["budget_journal_exists"] is True,
                 "Reviewed pilot/artifact/budget binding is incomplete")
    return agreement_identity(value)


def ssh_fingerprint(helper):
    raw = helper.body(helper.record(Path("/etc/ssh/ssh_host_ed25519_key.pub")))
    fields = raw.decode("ascii").split()
    need(len(fields) in (2, 3) and fields[0] == "ssh-ed25519", "Expected the same ED25519 SSH host key")
    key = base64.b64decode(fields[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")


def egress(new_ip, helper):
    result = subprocess.run(["/usr/bin/curl", "--ipv4", "--noproxy", "*", "--interface", new_ip,
                             "--max-time", "15", "--silent", "--show-error", "--fail", "https://api.ipify.org"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=helper.ENV, timeout=20, check=False)
    need(result.returncode == 0 and len(result.stdout) <= 64
         and result.stdout.decode("ascii").strip() == new_ip, "Current public egress does not confirm the new primary")
    return new_ip


def capture(old_ip, new_ip, enroll, helper, netplan):
    config = helper.record(CONFIG)
    value = parsed(config, helper)
    settings = netplan.Settings.from_value(value)
    need(settings.server_id == SERVICE and settings.state_directory == OLD_STATE,
         "Only the original Sweden guest epoch can be reconciled by this tool")
    need(helper.body(helper.record(PREFIX / ".service-id"), mode=0o600).decode().strip() == SERVICE,
         "Package belongs to another provider service")
    return {"format": 1, "service_id": SERVICE, "old_ip": old_ip, "new_ip": new_ip,
            "old": {"config": config, "state": helper.record(OLD_STATE / "state.json"),
                    "history": helper.record(OLD_STATE / "netplan-generated-history.json", absent=True),
                    "bootstrap_backup": helper.record(OLD_STATE / "bootstrap-original.interfaces"),
                    "enrollment": {name: helper.record(OLD_ENROLLMENT / name) for name in ENROLLMENT_FILES}},
            "current": enroll.capture(SERVICE, new_ip, helper, netplan),
            "ssh_fingerprint": ssh_fingerprint(helper), "egress_ipv4": egress(new_ip, helper)}


def identity_without_v4(value, interface, old_ip, new_ip):
    """Erase only the two explicitly authorized IPv4 fields for strict comparison."""
    value = deepcopy(value)
    eth = value["network"]["ethernets"][interface]
    v4 = [item for item in eth["addresses"] if ":" not in item]
    need(v4 in ([old_ip + "/32"], [new_ip + "/32"]), "Persistent YAML has an unapproved IPv4 set")
    eth["addresses"] = [item for item in eth["addresses"] if ":" in item]
    defaults = [route for route in eth["routes"] if ":" not in route["via"]]
    need(len(defaults) == 1 and defaults[0].get("from") in (None, old_ip, new_ip),
         "Persistent YAML has an unapproved preferred source")
    defaults[0].pop("from", None)
    return value


def validate_snapshot(snapshot, agreement, enroll, helper, managed, netplan):
    old_ip, new_ip = snapshot["old_ip"], snapshot["new_ip"]
    identity = validate_agreement(agreement, old_ip, new_ip, enroll)
    need(set(snapshot) == {"format", "service_id", "old_ip", "new_ip", "old", "current", "ssh_fingerprint", "egress_ipv4"}
         and snapshot["format"] == 1 and snapshot["service_id"] == SERVICE
         and snapshot["ssh_fingerprint"] == agreement["ssh"]["fingerprint"] and snapshot["egress_ipv4"] == new_ip,
         "Guest snapshot does not match the trusted manual change")
    old = snapshot["old"]
    need(set(old) == {"config", "state", "history", "bootstrap_backup", "enrollment"}
         and set(old["enrollment"]) == set(ENROLLMENT_FILES), "Old guest epoch snapshot is incomplete")
    old_value, old_state = parsed(old["config"], helper), parsed(old["state"], helper)
    old_settings = netplan.Settings.from_value(old_value)
    owned = {"id": old_ip, "ip": old_ip}
    need(old_settings.server_id == SERVICE and old_settings.state_directory == OLD_STATE
         and old_settings.bootstrap["active"] == owned and old_settings.bootstrap["allocations"] == [owned]
         and old_settings.bootstrap["expected_live_ipv4"] == [old_ip], "Old configuration is not the original one-IP epoch")
    netplan.ManagedGuest(old_settings, None, None).validate_state(old_state)
    need(old_state["active"] == owned and old_state["allocations"] == [owned]
         and old_state["pending"] is None and old_state["retired"] == [],
         "Old guest has pending, retired or additional allocations; do not reset its history")
    old_enrollment = parsed(old["enrollment"]["snapshot.json"], helper)
    old_intent, old_complete = (parsed(old["enrollment"][name], helper) for name in ("intent.json", "complete.json"))
    need(set(old_intent) == {"format", "service_id", "ip", "legacy_ip", "snapshot_sha256", "config_sha256"}
         and old_intent["format"] == 1 and old_intent["service_id"] == SERVICE and old_intent["ip"] == old_ip
         and old_intent["snapshot_sha256"] == helper.digest(helper.encoded(old_enrollment))
         and old_intent["config_sha256"] == helper.digest(helper.body(old["config"], mode=0o600))
         and old_complete == {**old_intent, "ready": True}, "Original enrollment receipts do not bind the old configuration")
    need(helper.body(old["config"], mode=0o600) == helper.encoded(old_value), "Old configuration encoding/receipt differs")
    _settings, old_render, reconstructed_value, _unit = enroll.validate_snapshot(
        old_enrollment, SERVICE, old_ip, old_intent["legacy_ip"], helper, managed, netplan)
    need(reconstructed_value == old_value and helper.digest(old_render) == old_state["config_sha256"],
         "Old journal is not an untouched successful bootstrap")
    old_original = helper.body(old_enrollment["netplan"], mode=0o600)
    need(helper.body(old["bootstrap_backup"], mode=0o600) == old_original
         and helper.body(old["enrollment"]["netplan-original.yaml"], mode=0o600) == old_original
         and helper.body(old["enrollment"]["networkd-original.network"], mode=0o600)
         == helper.body(old_enrollment["generated"]), "Old persistent backups do not match the original enrollment")
    need(parsed(old["enrollment"]["cloud-init-before.json"], helper) == old_enrollment["cloud_init"],
         "Old cloud-init backup differs from its enrollment snapshot")
    original_route = old_enrollment["diagnostic"]["network"]["default"]
    need(len(original_route) == 1 and old_state["route"] in (
        original_route[0], {**original_route[0], "prefsrc": old_ip}),
        "Old guest route is not an untouched bootstrap route")
    current = snapshot["current"]
    need(current["service_id"] == SERVICE and current["ip"] == new_ip, "Current snapshot belongs to another VPS")
    enroll.legacy_proof(current["diagnostic"], SERVICE, new_ip, new_ip, enroll.SOURCE_SHA, helper)
    gateway = enroll.network_proof(current["diagnostic"], new_ip)
    need(current["diagnostic"]["interface"] == old_settings.interface and gateway == old_settings.gateway,
         "Manual change altered interface or gateway")
    raw = helper.body(current["netplan"], mode=0o600)
    need(current["cloud_init"] is not None and helper.body(current["cloud_init"], mode=0o600) == netplan.CLOUD_DISABLED,
         "Existing cloud-init handoff changed")
    guard = current["netplan_guard"]
    need(all(guard[key] == old_settings.netplan_guard[key]
             for key in ("macaddress", "networkd_inputs", "cloud_init_file_sha256")),
         "Manual change altered physical identity or unrelated network inputs")
    known_generations = {helper.digest(helper.body(old_enrollment["generated"])),
                         helper.digest(netplan.generate_candidate(old_render, old_settings))}
    if old["history"] is not None:
        history = parsed(old["history"], helper)
        need(set(history) == {"format", "service_id", "sha256s"} and history["format"] == 1
             and history["service_id"] == SERVICE and isinstance(history["sha256s"], list)
             and 1 <= len(history["sha256s"]) <= 8 and len(set(history["sha256s"])) == len(history["sha256s"])
             and all(isinstance(item, str) and SHA.fullmatch(item) for item in history["sha256s"])
             and set(history["sha256s"]) <= known_generations, "Old generation history contains an unexplained operation")
    epoch = helper.digest(helper.encoded(snapshot))
    fresh = {**old_value, "state_directory": str(EPOCHS / epoch), "netplan_guard": guard,
             "bootstrap": {"expected_config_sha256": helper.digest(raw), "expected_live_ipv4": [new_ip],
                           "active": {"id": new_ip, "ip": new_ip}, "allocations": [{"id": new_ip, "ip": new_ip}]}}
    settings = netplan.Settings.from_value(fresh)
    # Both trees must remain acceptable native Netplan. A stale preferred
    # source referring to a removed address is not silently guessed/repaired.
    netplan.definition(raw, settings)
    need(identity_without_v4(netplan.parse_yaml(raw), settings.interface, old_ip, new_ip)
         == identity_without_v4(netplan.parse_yaml(old_render), settings.interface, old_ip, new_ip),
         "Current YAML differs beyond the explicitly authorized IPv4 change")
    generated = helper.body(current["generated"])
    need(guard["generated_network_sha256"] == helper.digest(generated)
         and helper.digest(generated) in known_generations | {helper.digest(netplan.generate_candidate(raw, settings))},
         "Running networkd generation is neither current nor previously owned")
    rendered = netplan.canonical_netplan(raw, settings, settings.bootstrap["allocations"], settings.bootstrap["active"])
    generated_after = netplan.generate_candidate(rendered, settings)
    return {"epoch": epoch, "identity": identity, "old_settings": old_settings, "settings": settings,
            "fresh_config": fresh, "rendered": rendered, "generated_after": generated_after}


def directories(helper, path):
    for directory in (path.parent.parent, path.parent, path):
        helper.create_directory(directory)


def apply(snapshot, agreement, plan, enroll, helper, managed, netplan):
    epoch = plan["epoch"]
    archive, settings = ARCHIVES / epoch, plan["settings"]
    directories(helper, archive)
    lock = os.open(archive.parent / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(lock)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, "Unsafe manual migration lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old_raw = helper.body(snapshot["old"]["config"], mode=0o600)
        fresh_raw = helper.encoded(plan["fresh_config"])
        intent = {"format": 1, "service_id": SERVICE, "epoch": epoch, "old_ip": snapshot["old_ip"],
                  "new_ip": snapshot["new_ip"], "snapshot_sha256": helper.digest(helper.encoded(snapshot)),
                  "old_config_sha256": helper.digest(old_raw), "old_state_sha256": snapshot["old"]["state"]["sha256"],
                  "new_config_sha256": helper.digest(fresh_raw), "new_state_directory": str(settings.state_directory),
                  "agreement_identity_sha256": helper.digest(helper.encoded(plan["identity"]))}
        old_store = netplan.FileStore(plan["old_settings"])
        with old_store.locked():
            need(helper.body(helper.record(PREFIX / ".service-id"), mode=0o600).decode().strip() == SERVICE,
                 "Guest package service ownership changed")
            need(helper.body(helper.record(CONFIG), mode=0o600) in (old_raw, fresh_raw), "Guest config pointer changed outside migration")
            need(helper.record(OLD_STATE / "state.json")["sha256"] == snapshot["old"]["state"]["sha256"]
                 and helper.record(OLD_STATE / "bootstrap-original.interfaces")["sha256"]
                 == snapshot["old"]["bootstrap_backup"]["sha256"], "Old guest state/backup changed before migration")
            for name, expected in snapshot["old"]["enrollment"].items():
                need(helper.record(OLD_ENROLLMENT / name)["sha256"] == expected["sha256"], "Old enrollment receipt changed")
            actual_history = helper.record(OLD_STATE / "netplan-generated-history.json", absent=True)
            need(actual_history == snapshot["old"]["history"], "Old generation history changed")
            validate_agreement(agreement, snapshot["old_ip"], snapshot["new_ip"], enroll)
            current = enroll.capture(SERVICE, snapshot["new_ip"], helper, netplan)
            enroll.current_matches(current, snapshot["current"], plan["rendered"], plan["generated_after"], netplan, helper)
            need(ssh_fingerprint(helper) == snapshot["ssh_fingerprint"] and egress(snapshot["new_ip"], helper) == snapshot["new_ip"],
                 "Current SSH identity/egress changed")
            # Every old file is preserved before bootstrap can replace YAML.
            helper.write_once(archive / "snapshot.json", helper.encoded(snapshot), managed)
            helper.write_once(archive / "intent.json", helper.encoded(intent), managed)
            proof = helper.encoded(agreement)
            helper.write_once(archive / ("agreement-" + helper.digest(proof) + ".json"), proof, managed)
            for name in ("config", "state", "history", "bootstrap_backup"):
                if snapshot["old"][name] is not None:
                    helper.write_once(archive / ("old-" + name), helper.body(snapshot["old"][name], mode=0o600), managed)
            for name, record in snapshot["old"]["enrollment"].items():
                helper.write_once(archive / ("old-enrollment-" + name), helper.body(record, mode=0o600), managed)
            helper.write_once(archive / "current-netplan-before.yaml", helper.body(snapshot["current"]["netplan"], mode=0o600), managed)
            helper.write_once(archive / "current-networkd-before.network", helper.body(snapshot["current"]["generated"]), managed)
            directories(helper, settings.state_directory)
            store = netplan.FileStore(settings)
            guest = netplan.ManagedGuest(settings, store, netplan.SystemKernel(settings))
            with store.locked():
                guest.run({"operation": "bootstrap", "server_id": SERVICE, "apply": True})
                need(guest.run({"operation": "check", "server_id": SERVICE}) == {"ok": True}, "Fresh epoch failed readiness")
                validate_agreement(agreement, snapshot["old_ip"], snapshot["new_ip"], enroll)

                def pointer_unchanged():
                    need(helper.body(helper.record(CONFIG), mode=0o600) == old_raw, "Config pointer changed before commit")

                actual = helper.body(helper.record(CONFIG), mode=0o600)
                if actual == old_raw:
                    managed.atomic_write(CONFIG, fresh_raw, 0o600, before_replace=pointer_unchanged)
                else:
                    need(actual == fresh_raw, "Unknown config pointer at commit")
                need(helper.body(helper.record(CONFIG), mode=0o600) == fresh_raw
                     and guest.run({"operation": "check", "server_id": SERVICE}) == {"ok": True},
                     "Published fresh epoch failed readback")
            complete = {**intent, "ready": True, "financial_counters_changed": False,
                        "network_restarted": False, "vpn_restarted": False, "paid_orders_submitted": 0}
            helper.write_once(archive / "complete.json", helper.encoded(complete), managed)
            return {"ok": True, "service_id": SERVICE, "epoch": epoch, "ip": snapshot["new_ip"],
                    "new_state_directory": str(settings.state_directory), "archive": str(archive),
                    "ready": True, "financial_counters_changed": False, "network_restarted": False,
                    "vpn_restarted": False, "paid_orders_submitted": 0}
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "plan", "apply"))
    parser.add_argument("--old-ip", required=True)
    parser.add_argument("--new-ip", required=True)
    parser.add_argument("--agreement", required=True, type=Path)
    parser.add_argument("--agreement-sha256", required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-sha256")
    parser.add_argument("--package-root", type=Path, default=PREFIX)
    args = parser.parse_args()
    need(sys.platform.startswith("linux") and os.geteuid() == 0, "Run only as root on the approved Sweden guest")
    enroll, helper, managed, netplan = load_runtime(args.package_root)
    proof_record = helper.record(args.agreement)
    need(SHA.fullmatch(args.agreement_sha256) and proof_record["sha256"] == args.agreement_sha256, "Agreement hash differs")
    agreement = parsed(proof_record, helper)
    validate_agreement(agreement, args.old_ip, args.new_ip, enroll)
    if args.action == "capture":
        snapshot = capture(args.old_ip, args.new_ip, enroll, helper, netplan)
        validate_snapshot(snapshot, agreement, enroll, helper, managed, netplan)
        print(json.dumps(snapshot, sort_keys=True))
        return
    need(args.snapshot is not None and isinstance(args.snapshot_sha256, str) and SHA.fullmatch(args.snapshot_sha256),
         "Freeze the private guest snapshot hash before plan/apply")
    snapshot_record = helper.record(args.snapshot)
    need(snapshot_record["sha256"] == args.snapshot_sha256, "Frozen guest snapshot changed")
    snapshot = parsed(snapshot_record, helper)
    need(snapshot["old_ip"] == args.old_ip and snapshot["new_ip"] == args.new_ip, "Snapshot identifies another change")
    plan = validate_snapshot(snapshot, agreement, enroll, helper, managed, netplan)
    if args.action == "plan":
        print(json.dumps({"plan_only": True, "service_id": SERVICE, "epoch": plan["epoch"],
                          "old_ip": args.old_ip, "new_ip": args.new_ip,
                          "new_state_directory": str(plan["settings"].state_directory),
                          "old_config_sha256": snapshot["old"]["config"]["sha256"],
                          "old_state_sha256": snapshot["old"]["state"]["sha256"],
                          "new_config_sha256": helper.digest(helper.encoded(plan["fresh_config"])),
                          "financial_counters_changed": False, "network_restarted": False, "vpn_restarted": False}))
    else:
        print(json.dumps(apply(snapshot, agreement, plan, enroll, helper, managed, netplan)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Manual Sweden reconciliation stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
