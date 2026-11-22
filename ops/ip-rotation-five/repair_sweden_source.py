#!/usr/bin/env python3
"""One-service stale-source repair preparation. Default: offline plan only.

An actual apply requires separate permission for a potentially disruptive
networkctl reload/reconfigure. It never publishes a guest epoch, resets a
journal, changes a billed IP, or restarts a VPN/network service. Its independent
watchdog restores the known-working .179 IPv4 route, NOT the broken .133 source.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import hashlib
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import types

SERVICE = "f5c0287a-636e-4981-95c7-80b24c9559c0"
OLD, NEW = "177.3.250.133", "177.3.250.179"
INTERFACE, GATEWAY = "eth0", "10.0.0.1"
PREFIX = Path("/opt/spn-ip-failover-guest")
YAML = Path("/etc/netplan/50-cloud-init.yaml")
NETWORK = Path("/run/systemd/network/10-netplan-eth0.network")
ARCHIVES = Path("/var/lib/spn-sweden-source-repair")
SHA = re.compile("[0-9a-f]{64}")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LC_ALL": "C"}


def need(value, message):
    if not value:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def command(argv, *, timeout=30):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            env=ENV, timeout=timeout, check=False)
    need(result.returncode == 0 and len(result.stdout) < 262144, "Fixed repair command did not complete")
    return result.stdout.decode().strip()


def load_runtime():
    # Load only root-controlled sibling source, with no package initializer or
    # pycache execution. The shared enrollment loader checks every runtime file.
    path = PREFIX / "enroll_netplan.py"
    for parent in path.parents:
        m = parent.lstat()
        need(stat.S_ISDIR(m.st_mode) and m.st_uid == 0 and not m.st_mode & 0o022, "Untrusted runtime parent")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        m = os.fstat(stream.fileno())
        need(stat.S_ISREG(m.st_mode) and m.st_uid == 0 and m.st_nlink == 1 and not m.st_mode & 0o022, "Untrusted runtime")
        raw = stream.read(262145)
        need(0 < len(raw) <= 262144, "Runtime is oversized")
    enroll = types.ModuleType("_sweden_repair_enroll")
    enroll.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), enroll.__dict__)
    helper, managed, netplan = enroll.load_modules(PREFIX)
    path = PREFIX / "reconcile_sweden_epoch.py"
    raw = helper.body(helper.record(path))
    migration = types.ModuleType("_sweden_repair_migration")
    migration.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), migration.__dict__)
    return enroll, helper, managed, netplan, migration


def network_json(run=command):
    return json.loads(run(["/usr/bin/networkctl", "--json=short", "status", INTERFACE]))


def live_ipv4(diagnostic):
    need(diagnostic["interface"] == INTERFACE, "Unexpected interface")
    managers, data = diagnostic["managers"], diagnostic["network"]
    need(managers["systemd-networkd"] == "active"
         and managers["networking"] in ("inactive", "failed", "unknown")
         and managers["NetworkManager"] in ("inactive", "failed", "unknown")
         and diagnostic["interfaces"] is None, "Another network manager is present")
    rules = data["rules"]
    need(len(rules) == 3 and all(row.keys() <= {"priority", "src", "table", "protocol"}
         and row.get("src") == "all" for row in rules)
         and [(row.get("priority"), row.get("table")) for row in rules]
         == [(0, "local"), (32766, "main"), (32767, "default")], "Unexpected policy routing")
    links = [row for row in data["addresses"] if row.get("ifname") == INTERFACE]
    need(len(links) == 1 and "UP" in links[0].get("flags", []), "Interface is not uniquely UP")
    v4 = [row for row in links[0].get("addr_info", []) if row.get("family") == "inet"]
    need(len(v4) == 1 and v4[0].get("local") == NEW and v4[0].get("prefixlen") == 32,
         "Working IPv4 is not exactly the authorized .179/32")
    need(not any(row.get("local") == NEW for link in data["addresses"] if link.get("ifname") != INTERFACE
                 for row in link.get("addr_info", [])), "IPv4 is duplicated on another interface")
    routes, selected = data["default"], data["selected"]
    need(len(routes) == 1 and len(selected) == 1, "Expected a single working default/selected route")
    route = routes[0]
    need(route.keys() <= {"dst", "dev", "gateway", "flags", "prefsrc", "protocol", "scope", "type", "table"}
         and route.get("dst") == "default" and route.get("dev") == INTERFACE
         and route.get("gateway") == GATEWAY and route.get("flags") == ["onlink"]
         and route.get("prefsrc", NEW) == NEW and route.get("protocol", "boot") in ("boot", 3, "static", 4)
         and route.get("table", "main") in ("main", 254) and route.get("type", "unicast") == "unicast"
         and route.get("scope", "global") in ("global", 0), "Working default route drifted")
    need(selected[0].get("dev") == INTERFACE and selected[0].get("gateway") == GATEWAY
         and (selected[0].get("prefsrc") or selected[0].get("src")) == NEW, "Working egress route changed")


def networkd_identity(value, mac):
    need(isinstance(value, dict) and value.get("Name") == INTERFACE
         and type(value.get("Index")) is int and value["Index"] > 0
         and value.get("Type") == "ether" and value.get("OperationalState") == "routable"
         and value.get("IPv4AddressState") == "routable" and value.get("OnlineState") == "online"
         and value.get("NetworkFile") == str(NETWORK) and value.get("NetworkFileDropins") == [],
         "networkd identity/routability differs")
    need(value.get("HardwareAddress") == [int(part, 16) for part in mac.split(":")], "Physical MAC differs")
    addresses = [row for row in value.get("Addresses", []) if row.get("Family") == 2]
    need(len(addresses) == 1 and addresses[0].get("Address") == list(IPv4Address(NEW).packed)
         and addresses[0].get("PrefixLength") == 32 and addresses[0].get("ConfigSource") == "static"
         and addresses[0].get("ConfigState") == "configured" and addresses[0].get("Scope") == 0,
         "networkd has not configured the authorized static IPv4")


def pending_old_source(value, mac):
    networkd_identity(value, mac)
    need(value.get("AdministrativeState") == "configuring", "Expected only the observed stale-source state")
    routes = [row for row in value.get("Routes", []) if row.get("Family") == 2]
    pending = [row for row in routes if row.get("ConfigState") != "configured"]
    need(len(pending) == 1, "Unexpected pending IPv4 networkd requests")
    row = pending[0]
    need(row.get("ConfigState") == "requesting" and row.get("ConfigSource") == "static"
         and row.get("Destination") == [0, 0, 0, 0] and row.get("DestinationPrefixLength") == 0
         and row.get("Gateway") == [10, 0, 0, 1] and row.get("PreferredSource") == list(IPv4Address(OLD).packed)
         and row.get("Table") == 254 and row.get("Type") == 1, "Pending request is not precisely the old .133 source")


def render_source(raw, netplan):
    value = netplan.parse_yaml(raw)
    eth = value["network"]["ethernets"][INTERFACE]
    defaults = [r for r in eth["routes"] if ":" not in r["via"]]
    need(len(defaults) == 1 and defaults[0].get("from") == OLD, "Saved IPv4 default must contain the stale .133 source")
    need(eth.get("dhcp4") is False and [a for a in eth["addresses"] if ":" not in a] == [NEW + "/32"],
         "Saved current IPv4 is not the static authorized singleton")
    pattern = rb'(?m)^([ \t]*from:[ \t]*)([\x22\x27]?)177\.3\.250\.133\2([ \t]*(?:#[^\r\n]*)?)$'
    candidate, count = re.subn(pattern, lambda m: m[1] + m[2] + NEW.encode() + m[2] + m[3], raw)
    need(count == 1, "Expected exactly one standalone stale-source YAML field")
    expected = deepcopy(value)
    route = [r for r in expected["network"]["ethernets"][INTERFACE]["routes"] if ":" not in r["via"]][0]
    route["from"] = NEW
    need(netplan.parse_yaml(candidate) == expected, "Candidate alters fields beyond the one authorized source")
    return candidate


def validate(snapshot, agreement, runtime):
    enroll, helper, managed, netplan, migration = runtime
    migration.validate_agreement(agreement, OLD, NEW, enroll)
    need(set(snapshot) == {"format", "service_id", "snapshot", "networkctl"}
         and snapshot["format"] == 1 and snapshot["service_id"] == SERVICE, "Foreign repair snapshot")
    snap, old = snapshot["snapshot"], snapshot["snapshot"]["old"]
    need(snap["service_id"] == SERVICE and snap["old_ip"] == OLD and snap["new_ip"] == NEW
         and snap["egress_ipv4"] == NEW and snap["ssh_fingerprint"] == agreement["ssh"]["fingerprint"],
         "Repair snapshot has another service/IP/egress/host identity")
    old_config = json.loads(helper.body(old["config"], mode=0o600))
    old_settings = netplan.Settings.from_value(old_config)
    old_state = json.loads(helper.body(old["state"], mode=0o600))
    netplan.ManagedGuest(old_settings, None, None).validate_state(old_state)
    allocation = {"id": OLD, "ip": OLD}
    need(old_settings.server_id == SERVICE and old_settings.interface == INTERFACE and old_settings.gateway == GATEWAY
         and old_settings.state_directory == migration.OLD_STATE and old_state["active"] == allocation
         and old_state["allocations"] == [allocation] and old_state["pending"] is None and old_state["retired"] == [],
         "Original guest epoch has drift or pending operations")
    current = snap["current"]
    live_ipv4(current["diagnostic"])
    guard = current["netplan_guard"]
    need(all(guard[k] == old_settings.netplan_guard[k] for k in ("macaddress", "networkd_inputs", "cloud_init_file_sha256"))
         and helper.body(current["cloud_init"], mode=0o600) == netplan.CLOUD_DISABLED, "Network ownership inputs changed")
    pending_old_source(snapshot["networkctl"], guard["macaddress"])
    raw, generated = helper.body(current["netplan"], mode=0o600), helper.body(current["generated"])
    candidate = render_source(raw, netplan)
    value = {**old_config, "netplan_guard": guard,
             "bootstrap": {"expected_config_sha256": digest(candidate), "expected_live_ipv4": [NEW],
                           "active": {"id": NEW, "ip": NEW}, "allocations": [{"id": NEW, "ip": NEW}]}}
    settings = netplan.Settings.from_value(value)
    after = netplan.generate_candidate(candidate, settings)
    need(generated.count(("PreferredSource=" + OLD).encode()) == 1
         and generated.replace(("PreferredSource=" + OLD).encode(), ("PreferredSource=" + NEW).encode()) == after,
         "Generated networkd diff is not exactly PreferredSource .133 to .179")
    return {"epoch": digest(encoded(snapshot)), "settings": settings, "old_settings": old_settings,
            "yaml": candidate, "generated": after}


def capture(runtime):
    enroll, helper, _managed, netplan, migration = runtime
    return {"format": 1, "service_id": SERVICE,
            "snapshot": migration.capture(OLD, NEW, enroll, helper, netplan), "networkctl": network_json()}


def old_journals_unchanged(snapshot, runtime):
    _enroll, helper, _managed, _netplan, migration = runtime
    old = snapshot["snapshot"]["old"]
    for name, path in (("config", migration.CONFIG), ("state", migration.OLD_STATE / "state.json"),
                       ("bootstrap_backup", migration.OLD_STATE / "bootstrap-original.interfaces")):
        need(helper.record(path) == old[name], "Original guest journal/config/backup changed during repair")
    need(helper.record(migration.OLD_STATE / "netplan-generated-history.json", absent=True) == old["history"],
         "Original generation history changed")
    for name, record in old["enrollment"].items():
        need(helper.record(migration.OLD_ENROLLMENT / name) == record, "Original enrollment receipt changed")


def postcheck(plan, runtime):
    enroll, helper, _managed, netplan, migration = runtime
    settings = plan["settings"]
    need(helper.body(helper.record(YAML), mode=0o600) == plan["yaml"]
         and helper.body(helper.record(NETWORK)) == plan["generated"], "Repaired files differ")
    netplan.SystemKernel(settings).manager()  # Existing strict configured guard, unchanged.
    enroll.network_proof(helper.capture(SERVICE, NEW), NEW)
    value = network_json()
    networkd_identity(value, settings.netplan_guard["macaddress"])
    need(value.get("AdministrativeState") == "configured"
         and all(r.get("ConfigState") == "configured" for r in value.get("Routes", []) if r.get("Family") == 2),
         "An IPv4 networkd request is still pending")
    need(migration.egress(NEW, helper) == NEW, "Public .179 egress did not recover")


def watchdog(snapshot, archive, runtime, *, run=command):
    _enroll, helper, managed, netplan, _migration = runtime
    if (archive / "complete.json").exists():
        return {"skipped": "strict repair complete"}
    old_journals_unchanged(snapshot, runtime)
    current = snapshot["snapshot"]["current"]
    guard = current["netplan_guard"]
    live_guard = netplan.capture_guard(INTERFACE, YAML)
    need(all(live_guard[k] == guard[k] for k in ("macaddress", "networkd_inputs", "cloud_init_file_sha256"))
         and Path("/sys/class/net/eth0/address").read_text().strip() == guard["macaddress"],
         "Watchdog physical/network ownership identity changed")
    need(helper.body(helper.record(YAML), mode=0o600) in (helper.body(current["netplan"], mode=0o600),
         helper.body(helper.record(archive / "candidate.yaml"), mode=0o600))
         and helper.body(helper.record(NETWORK)) in (helper.body(current["generated"]),
         helper.body(helper.record(archive / "candidate.network"), mode=0o600)),
         "Watchdog refuses foreign persistent/generated file changes")
    links = json.loads(run(["/usr/sbin/ip", "-j", "-4", "address", "show", "dev", INTERFACE]))
    need(len(links) == 1 and "UP" in links[0].get("flags", [])
         and all(r.get("local") == NEW and r.get("prefixlen") == 32
                 for r in links[0].get("addr_info", []) if r.get("family") == "inet"),
         "Watchdog refuses foreign interface/IP changes")
    routes = json.loads(run(["/usr/sbin/ip", "-j", "-4", "route", "show", "table", "main", "default"]))
    need(len(routes) <= 1 and all(r.get("dev") == INTERFACE and r.get("gateway") == GATEWAY
         and r.get("prefsrc", NEW) == NEW and r.get("flags") == ["onlink"] for r in routes),
         "Watchdog refuses foreign default-route changes")
    # Corrected persistent files remain in place; never reintroduce .133.
    # Restore only the already-paid working .179 and its known gateway.
    run(["/usr/sbin/ip", "address", "replace", NEW + "/32", "dev", INTERFACE])
    run(["/usr/sbin/ip", "route", "replace", "default", "via", GATEWAY, "dev", INTERFACE,
         "proto", "static", "src", NEW, "onlink"])
    helper.write_once(archive / "watchdog-ran.json", encoded({"service_id": SERVICE, "ip": NEW,
                      "old_journals_preserved": True, "epoch_published": False}), managed)
    return {"working_ipv4_route_restored": NEW, "epoch_published": False}


def apply(snapshot, agreement, plan, runtime):
    enroll, helper, managed, netplan, migration = runtime
    archive = ARCHIVES / plan["epoch"]
    migration.directories(helper, archive)
    store = netplan.FileStore(plan["old_settings"])
    with store.locked():
        old_journals_unchanged(snapshot, runtime)
        migration.validate_agreement(agreement, OLD, NEW, enroll)
        current = capture(runtime)
        need(current == snapshot, "Frozen repair snapshot changed; no network change permitted")
        helper.write_once(archive / "snapshot.json", encoded(snapshot), managed)
        helper.write_once(archive / "agreement.json", encoded(agreement), managed)
        helper.write_once(archive / "candidate.yaml", plan["yaml"], managed)
        helper.write_once(archive / "candidate.network", plan["generated"], managed)
        intent = {"format": 1, "service_id": SERVICE, "epoch": plan["epoch"],
                  "snapshot_sha256": digest(encoded(snapshot)), "candidate_yaml_sha256": digest(plan["yaml"]),
                  "candidate_network_sha256": digest(plan["generated"]), "epoch_published": False}
        helper.write_once(archive / "intent.json", encoded(intent), managed)
        unit = "spn-sweden-source-" + plan["epoch"][:16] + "-rollback"
        script = PREFIX / "repair_sweden_source.py"
        helper.record(script)  # watchdog source must remain root controlled.
        command(["/usr/bin/systemd-run", "--quiet", "--unit=" + unit, "--on-active=120s", "--timer-property=AccuracySec=1s",
                 "--property=Type=oneshot", "/usr/bin/python3", "-I", str(script), "watchdog", "--epoch", plan["epoch"]])
        try:
            old = snapshot["snapshot"]["current"]
            def same(path, record):
                need(helper.record(path) == record, "Repair input changed before atomic write")
            managed.atomic_write(YAML, plan["yaml"], 0o600,
                                 before_replace=lambda: same(YAML, old["netplan"]))
            managed.atomic_write(NETWORK, plan["generated"], old["generated"]["mode"], group=old["generated"]["gid"],
                                 before_replace=lambda: same(NETWORK, old["generated"]))
            command(["/usr/bin/networkctl", "reload"])
            command(["/usr/bin/networkctl", "reconfigure", INTERFACE])
            deadline = time.monotonic() + 45
            while True:
                try:
                    postcheck(plan, runtime)
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(2)
            old_journals_unchanged(snapshot, runtime)
            helper.write_once(archive / "complete.json", encoded({**intent, "strict_ready": True,
                              "network_service_restarted": False, "vpn_restarted": False}), managed)
            command(["/usr/bin/systemctl", "stop", unit + ".timer"])
            return {"ok": True, "service_id": SERVICE, "archive": str(archive), "strict_ready": True,
                    "epoch_published": False, "next": "fresh strict epoch capture/plan/apply then RPC/reverse23001"}
        except Exception:
            # Keep the independent timer armed even if this SSH process dies.
            watchdog(snapshot, archive, runtime)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("capture", "plan", "apply", "watchdog"))
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-sha256")
    parser.add_argument("--agreement", type=Path)
    parser.add_argument("--agreement-sha256")
    parser.add_argument("--epoch")
    parser.add_argument("--allow-network-reconfigure", action="store_true")
    args = parser.parse_args()
    if args.action is None:
        print(json.dumps({"plan_only": True, "service_id": SERVICE, "network_used": False,
                          "apply_requires_separate_reconfigure_permission": True, "can_interrupt_network": True}))
        return
    need(sys.platform.startswith("linux") and os.geteuid() == 0, "Run only on the intended Sweden guest as Linux root")
    runtime = load_runtime()
    enroll, helper, _managed, _netplan, migration = runtime
    if args.action == "watchdog":
        need(args.epoch is not None and SHA.fullmatch(args.epoch), "Invalid frozen watchdog epoch")
        archive = ARCHIVES / args.epoch
        snapshot = json.loads(helper.body(helper.record(archive / "snapshot.json"), mode=0o600))
        need(digest(encoded(snapshot)) == args.epoch, "Watchdog snapshot differs")
        print(json.dumps(watchdog(snapshot, archive, runtime)))
        return
    need(args.agreement is not None and args.agreement_sha256 is not None and SHA.fullmatch(args.agreement_sha256),
         "Fresh pinned provider/panel/financial agreement is required")
    record = helper.record(args.agreement)
    need(record["sha256"] == args.agreement_sha256, "Agreement hash differs")
    agreement = json.loads(helper.body(record, mode=0o600))
    migration.validate_agreement(agreement, OLD, NEW, enroll)
    if args.action == "capture":
        snapshot = capture(runtime)
        validate(snapshot, agreement, runtime)
        print(json.dumps(snapshot, sort_keys=True))
        return
    need(args.snapshot is not None and args.snapshot_sha256 is not None and SHA.fullmatch(args.snapshot_sha256),
         "Frozen snapshot and hash are required")
    record = helper.record(args.snapshot)
    need(record["sha256"] == args.snapshot_sha256, "Frozen snapshot hash differs")
    snapshot = json.loads(helper.body(record, mode=0o600))
    plan = validate(snapshot, agreement, runtime)
    if args.action == "plan":
        print(json.dumps({"plan_only": True, "service_id": SERVICE, "epoch": plan["epoch"], "can_interrupt_network": True,
                          "yaml_sha256": digest(plan["yaml"]), "generated_sha256": digest(plan["generated"]),
                          "old_journals_preserved": True, "epoch_published": False}))
        return
    need(args.allow_network_reconfigure, "Apply requires explicit external permission for networkctl reload/reconfigure")
    print(json.dumps(apply(snapshot, agreement, plan, runtime)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Sweden source repair stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
