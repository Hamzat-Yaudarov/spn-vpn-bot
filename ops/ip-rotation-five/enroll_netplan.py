#!/usr/bin/env python3
"""Explicit five-VPS Netplan handoff; no netplan apply, network or VPN restart."""
from __future__ import annotations

import argparse
import fcntl
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import stat
import sys
import types


SERVICES = {
    "f5c0287a-636e-4981-95c7-80b24c9559c0": None,
    "21e508f0-2ef2-47da-aee7-15dc8c66eb15": "6a7fa82e4c8740dc9b46b79b1829d5e2",
    "9b0316d5-fa83-4bf8-b6dc-8202b66622c9": "2f5435abe9eb45dc895adcdd759f79f9",
    "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d": "e75882a2c9684ee7b5d3b8457059a320",
    "4d6f358a-e75f-4dd8-a635-c6ad320f2732": "fddb54bd54424364a036afab3f79a919",
}
YAML = Path("/etc/netplan/50-cloud-init.yaml")
CONFIG = Path("/etc/spn-ip-failover-guest.json")
STATE = Path("/var/lib/spn-ip-failover-guest")
ENROLLMENT = Path("/var/lib/spn-ip-failover-netplan-enrollment")
SOURCE_SHA = "cf988e389588da27975a69ce20785f46ee9f0f4bc3c4f1f3b386eb54743f22b0"


def need(condition, message):
    if not condition:
        raise ValueError(message)


def load_modules(package_root):
    """Load root-controlled source only; never a package initializer/pycache."""
    script = Path(__file__).with_name("guest_enrollment.py")
    # Read the utility with a small self-contained no-follow check first.
    for parent in script.parents:
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
             "Utility parents are not root-controlled")
    descriptor = os.open(script, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
             and not info.st_mode & 0o022, "Utility file is not root-controlled")
        raw = stream.read(262145)
        need(len(raw) <= 262144, "Utility source exceeds limit")
    helper = types.ModuleType("_spn_netplan_enrollment_util")
    helper.__file__ = str(script)
    sys.modules[helper.__name__] = helper
    exec(compile(raw, str(script), "exec"), helper.__dict__)
    package_name = "_spn_netplan_enrollment_runtime"
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
    modules = []
    for name in ("managed_guest", "netplan_guest"):
        path = package_root / "spn_ip_failover" / (name + ".py")
        source = helper.body(helper.record(path))
        module = types.ModuleType(package_name + "." + name)
        module.__package__ = package_name
        module.__file__ = str(path)
        sys.modules[module.__name__] = module
        setattr(package, name, module)
        exec(compile(source, str(path), "exec"), module.__dict__)
        modules.append(module)
    return helper, modules[0], modules[1]


def public_ip(value):
    need(isinstance(value, str) and str(IPv4Address(value)) == value
         and IPv4Address(value).is_global and not IPv4Address(value).is_multicast
         and not IPv4Address(value).is_reserved, "Expected a canonical public IPv4")
    return value


def capture(service, ip, helper, netplan):
    base = helper.capture(service, ip)
    need(base["service_id"] == service and base["ip"] == ip, "Diagnostic identifies another VPS")
    interface = base["interface"]
    need(re.fullmatch(r"[A-Za-z0-9_.-]{1,12}", interface), "Invalid pinned interface")
    generated = Path("/run/systemd/network") / ("10-netplan-" + interface + ".network")
    # The marker itself may be absent, but all of its root-controlled parents
    # must exist before a plan can authorize disabling any boot-time writer.
    helper.safe_parents(netplan.CLOUD_CONFIG)
    return {"format": 1, "service_id": service, "ip": ip, "diagnostic": base,
            "netplan": helper.record(YAML), "generated": helper.record(generated),
            "cloud_init": helper.record(netplan.CLOUD_CONFIG, absent=True),
            "netplan_guard": netplan.capture_guard(interface, YAML, enrollment=True)}


def legacy_proof(base, service, ip, legacy_ip, source_sha, helper):
    need(service in SERVICES and base.get("format") == 1 and base.get("service_id") == service
         and base.get("ip") == ip and base.get("deployment") == SERVICES[service],
         "Diagnostic identifies another approved VPS")
    deployment = SERVICES[service]
    rows = [line.split() for line in base["units"].splitlines()]
    legacy = base["legacy"]
    if deployment is None:
        need(not rows and legacy is None, "Unexpected legacy source unit on manual VPS")
        return None
    public_ip(legacy_ip)
    unit = "spn-reality-source-" + deployment + ".service"
    need(len(rows) == 1 and len(rows[0]) in (2, 3) and rows[0][0] == unit
         and rows[0][1] in ("enabled", "disabled"), "Unexpected legacy source unit ownership/state")
    need(isinstance(legacy, dict) and set(legacy) == {"unit", "plan", "source", "stage_plan", "active_state"}
         and legacy["active_state"] in ("active", "inactive", "failed"), "Legacy source unit is transitioning")
    need(helper.body(legacy["unit"], mode=0o644) == helper.unit_bytes(deployment, legacy_ip),
         "Legacy unit differs from the explicitly pinned old deployment")
    raw_plan = helper.body(legacy["plan"], mode=0o600)
    plan = json.loads(raw_plan, object_pairs_hook=helper.unique)
    need(isinstance(plan, dict) and set(plan) == {"version", "deployment", "target", "before", "after", "beforeSource", "helperHash"}
         and plan["version"] == 1 and plan["deployment"] == deployment and plan["target"] == legacy_ip
         and plan["helperHash"] == source_sha, "Legacy plan is not the pinned deployment")
    source = legacy["source"]
    need(source_sha == SOURCE_SHA and isinstance(source, dict)
         and set(source) == {"uid", "gid", "mode", "size", "sha256"}
         and source["uid"] == 0 and source["mode"] == 0o600 and source["sha256"] == source_sha
         and type(source["size"]) is int and 0 < source["size"] <= helper.MAX_FILE,
         "Legacy source helper is not the trusted installer version")
    if legacy["stage_plan"] is not None:
        need(helper.body(legacy["stage_plan"], mode=0o600) == raw_plan, "Legacy stage plan differs")
    need(isinstance(plan["before"], dict) and plan["after"] == {**plan["before"], "prefsrc": legacy_ip},
         "Legacy source transition differs")
    route = base["network"]["default"]
    need(len(route) == 1 and {k: v for k, v in route[0].items() if k != "prefsrc"}
         == {k: v for k, v in plan["after"].items() if k != "prefsrc"},
         "Live default route differs beyond the authorized IP change")
    return unit


def network_proof(base, ip):
    interface, data, managers = base["interface"], base["network"], base["managers"]
    need(base["interfaces"] is None and managers["systemd-networkd"] == "active"
         and managers["NetworkManager"] in ("inactive", "failed", "unknown")
         and managers["networking"] in ("inactive", "failed", "unknown"),
         "Pinned interface is not exclusively managed by networkd/Netplan")
    fields = managers.get("networkctl", "").split()
    need(len(fields) == 5 and fields[1] == interface and fields[-1] == "configured",
         "networkd has not configured the pinned interface")
    rules = data["rules"]
    need(len(rules) == 3 and all(row.keys() <= {"priority", "src", "table", "protocol"}
         and row.get("src") == "all" for row in rules)
         and [(row.get("priority"), row.get("table")) for row in rules]
         == [(0, "local"), (32766, "main"), (32767, "default")], "Unexpected policy routing")
    links = [row for row in data["addresses"] if row.get("ifname") == interface]
    need(len(links) == 1 and "UP" in links[0].get("flags", []), "Pinned interface is not uniquely UP")
    addresses = [row for row in links[0].get("addr_info", []) if row.get("family") == "inet"]
    need(len(addresses) == 1 and addresses[0].get("local") == ip and addresses[0].get("prefixlen") == 32,
         "Expected exactly the currently billed /32 IPv4")
    need(not any(row.get("family") == "inet" and row.get("local") == ip
                 for link in data["addresses"] if link.get("ifname") != interface
                 for row in link.get("addr_info", [])), "Current IPv4 is duplicated on another interface")
    routes, chosen = data["default"], data["selected"]
    need(len(routes) == 1, "Expected one default route")
    route = routes[0]
    need(route.keys() <= {"dst", "dev", "gateway", "flags", "prefsrc", "protocol", "scope", "type", "table"}
         and route.get("dst") == "default" and route.get("dev") == interface
         and route.get("flags") == ["onlink"] and route.get("protocol") in ("static", 4)
         and route.get("table", "main") in ("main", 254) and route.get("type", "unicast") == "unicast"
         and route.get("scope", "global") in ("global", 0), "Unexpected default route")
    gateway = str(IPv4Address(route["gateway"]))
    need(len(chosen) == 1 and chosen[0].get("dev") == interface and chosen[0].get("gateway") == gateway
         and (chosen[0].get("prefsrc") or chosen[0].get("src")) == ip
         and route.get("prefsrc", ip) == ip, "Default route does not select the current billed IP")
    return gateway


def validate_snapshot(snapshot, service, ip, legacy_ip, helper, managed, netplan):
    need(isinstance(snapshot, dict) and set(snapshot) == {"format", "service_id", "ip", "diagnostic", "netplan",
                                                       "generated", "cloud_init", "netplan_guard"}
         and snapshot["format"] == 1 and snapshot["service_id"] == service and snapshot["ip"] == public_ip(ip),
         "Snapshot identifies another VPS")
    base = snapshot["diagnostic"]
    unit = legacy_proof(base, service, ip, legacy_ip, SOURCE_SHA, helper)
    gateway = network_proof(base, ip)
    raw = helper.body(snapshot["netplan"], mode=0o600)
    generated = helper.body(snapshot["generated"])
    cloud = snapshot["cloud_init"]
    if cloud is not None:
        need(helper.body(cloud, mode=0o600) == netplan.CLOUD_DISABLED, "Existing cloud-init marker is not owned/identical")
    guard = snapshot["netplan_guard"]
    need(guard["generated_network_sha256"] == helper.digest(generated)
         and guard["cloud_init_file_sha256"] == helper.digest(netplan.CLOUD_DISABLED),
         "Netplan guard does not pin the captured generation/marker")
    value = {"version": 1, "server_id": service, "interface": base["interface"], "gateway": gateway,
             "method": "netplan", "interfaces_file": str(YAML), "state_directory": str(STATE),
             "bootstrap": {"expected_config_sha256": helper.digest(raw), "expected_live_ipv4": [ip],
                           "active": {"id": ip, "ip": ip}, "allocations": [{"id": ip, "ip": ip}]},
             "netplan_guard": guard}
    settings = netplan.Settings.from_value(value)
    need(netplan.generate_candidate(raw, settings) == generated,
         "Running networkd generation differs from the pinned original YAML")
    rendered = netplan.canonical_netplan(raw, settings, settings.bootstrap["allocations"], settings.bootstrap["active"])
    candidate = netplan.generate_candidate(rendered, settings)
    need(isinstance(candidate, bytes) and 0 < len(candidate) <= helper.MAX_FILE,
         "Candidate Netplan did not generate a bounded networkd file")
    return settings, rendered, value, unit


def normalize_live(snapshot):
    """Ignore only IP-route lookup's incidental uid/cache; keep routing itself pinned."""
    import copy
    value = copy.deepcopy(snapshot)
    for row in value["diagnostic"]["network"]["selected"]:
        row.pop("uid", None)
        row.pop("cache", None)
    return value


def current_matches(current, pinned, rendered, generated_after, netplan, helper):
    left, right = normalize_live(current), normalize_live(pinned)
    need(left["service_id"] == right["service_id"] and left["ip"] == right["ip"], "Current VPS identity changed")
    # The applied YAML, exact cloud marker and own disabled legacy unit are
    # the only expected differences on a retry. Generation is not reloaded.
    live_yaml = helper.body(left["netplan"])
    need(live_yaml in (helper.body(right["netplan"]), rendered), "Netplan changed outside enrollment")
    left["netplan"] = right["netplan"]
    need(left["cloud_init"] is None or helper.body(left["cloud_init"], mode=0o600) == netplan.CLOUD_DISABLED,
         "Cloud-init marker changed outside enrollment")
    left["cloud_init"] = right["cloud_init"]
    units = left["diagnostic"]["units"].splitlines()
    expected = right["diagnostic"]["units"].splitlines()
    if expected:
        need(len(units) == len(expected) == 1, "Unexpected legacy unit appeared")
        a, b = units[0].split(), expected[0].split()
        need(len(a) == len(b) and a[0] == b[0] and a[1] in ("enabled", "disabled") and a[2:] == b[2:],
             "Legacy unit state changed outside enrollment")
        if a[1] == "disabled":
            need(left["diagnostic"]["legacy"]["active_state"] in ("active", "inactive", "failed"),
                 "Owned legacy unit is transitioning after handoff")
            left["diagnostic"]["legacy"]["active_state"] = right["diagnostic"]["legacy"]["active_state"]
        left["diagnostic"]["units"] = right["diagnostic"]["units"]
    else:
        need(not units, "Unexpected source unit appeared")
    # A reboot between YAML replacement and completion legitimately compiles
    # the exact authorized new definition and adds its preferred source. This
    # is not permission to accept any new generated file or another route.
    generated_before = helper.body(right["generated"])
    live_generated = helper.body(left["generated"])
    need(live_generated in (generated_before, generated_after)
         and left["netplan_guard"]["generated_network_sha256"] == helper.digest(live_generated),
         "Generated networkd file changed outside enrollment")
    if live_generated != generated_before:
        need(live_yaml == rendered and current["cloud_init"] is not None
             and (not units or units[0].split()[1] == "disabled"),
             "New boot generation has no completed ownership handoff")
    left["generated"] = right["generated"]
    left["netplan_guard"]["generated_network_sha256"] = right["netplan_guard"]["generated_network_sha256"]
    old_routes, live_routes = right["diagnostic"]["network"]["default"], left["diagnostic"]["network"]["default"]
    need(len(old_routes) == len(live_routes) == 1 and live_routes[0] in (
        old_routes[0], {**old_routes[0], "prefsrc": pinned["ip"]}),
        "Default route changed beyond the owned boot preferred source")
    if live_routes[0] != old_routes[0]:
        need(live_yaml == rendered and current["cloud_init"] is not None
             and (not units or units[0].split()[1] == "disabled"),
             "New preferred source has no completed ownership handoff")
    left["diagnostic"]["network"]["default"] = old_routes
    # diagnostic metadata includes the same changed YAML; compare its current
    # content above, then retain the original manifest for all other files.
    current_files = {row["path"]: row for row in left["diagnostic"]["network_files"]}
    pinned_files = {row["path"]: row for row in right["diagnostic"]["network_files"]}
    need(set(current_files) == set(pinned_files), "Network input inventory changed outside enrollment")
    if str(YAML) in current_files:
        need(current_files[str(YAML)]["sha256"] in (pinned_files[str(YAML)]["sha256"], helper.digest(rendered)),
             "Netplan manifest changed outside enrollment")
        current_files[str(YAML)] = pinned_files[str(YAML)]
    generated_path = "/run/systemd/network/10-netplan-" + pinned["diagnostic"]["interface"] + ".network"
    if generated_path in current_files:
        need(current_files[generated_path]["sha256"] == helper.digest(live_generated),
             "Generated-file manifest disagrees with the captured file")
        current_files[generated_path] = pinned_files[generated_path]
    left["diagnostic"]["network_files"] = [current_files[row["path"]] for row in right["diagnostic"]["network_files"]]
    need(left == right, "Live snapshot drifted before enrollment")


def apply(snapshot, settings, rendered, value, unit, legacy_ip, helper, managed, netplan):
    helper.create_directory(ENROLLMENT)
    directory = ENROLLMENT / settings.server_id
    helper.create_directory(directory)
    lock = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(lock)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, "Unsafe enrollment lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        intent = {"format": 1, "service_id": settings.server_id, "ip": snapshot["ip"],
                  "legacy_ip": legacy_ip, "snapshot_sha256": helper.digest(helper.encoded(snapshot)),
                  "config_sha256": helper.digest(helper.encoded(value))}
        complete_record = helper.record(directory / "complete.json", absent=True)
        if complete_record is not None:
            complete = json.loads(helper.body(complete_record, mode=0o600), object_pairs_hook=helper.unique)
            need(complete == {**intent, "ready": True}, "Completed enrollment belongs to another pinned intent")
            config_record = helper.record(CONFIG)
            need(helper.body(config_record, mode=0o600) == helper.encoded(value),
                 "Completed enrollment configuration changed")
            store = netplan.FileStore(settings)
            guest = netplan.ManagedGuest(settings, store, netplan.SystemKernel(settings))
            with store.locked():
                need(guest.run({"operation": "check", "server_id": settings.server_id}) == {"ok": True},
                     "Completed enrollment did not pass fresh read-only readiness")
                state = store.state()
                active = managed.allocation(state["active"])
            return {"ok": True, "service_id": settings.server_id, "ip": active["ip"], "ready": True,
                    "already_enrolled": True, "network_restarted": False, "vpn_restarted": False}
        helper.write_once(directory / "snapshot.json", helper.encoded(snapshot), managed)
        helper.write_once(directory / "intent.json", helper.encoded(intent), managed)
        current = capture(settings.server_id, snapshot["ip"], helper, netplan)
        current_matches(current, snapshot, rendered, netplan.generate_candidate(rendered, settings), netplan, helper)
        helper.write_once(directory / "netplan-original.yaml", helper.body(snapshot["netplan"]), managed)
        helper.write_once(directory / "networkd-original.network", helper.body(snapshot["generated"]), managed)
        helper.write_once(directory / "cloud-init-before.json", helper.encoded(snapshot["cloud_init"]), managed)
        if unit is not None:
            legacy = snapshot["diagnostic"]["legacy"]
            for name in ("unit", "plan", "stage_plan"):
                if legacy[name] is not None:
                    helper.write_once(directory / ("legacy-" + name), helper.body(legacy[name]), managed)
            source_path = Path("/opt/spn-reality-network") / SERVICES[settings.server_id] / "source.py"
            source = helper.record(source_path)
            need({key: val for key, val in source.items() if key != "base64"} == legacy["source"],
                 "Legacy source changed before backup")
            helper.write_once(directory / "legacy-source.py", helper.body(source, mode=0o600), managed)
            rows = current["diagnostic"]["units"].splitlines()
            if rows[0].split()[1] == "enabled":
                helper.run("/usr/bin/systemctl", "disable", unit)
            need(helper.run("/usr/bin/systemctl", "is-enabled", unit, allowed=(0, 1)) == "disabled",
                 "Owned legacy unit was not disabled")
        helper.write_once(netplan.CLOUD_CONFIG, netplan.CLOUD_DISABLED, managed)
        helper.write_once(CONFIG, helper.encoded(value), managed)
        store = netplan.FileStore(settings)
        guest = netplan.ManagedGuest(settings, store, netplan.SystemKernel(settings))
        with store.locked():
            guest.run({"operation": "bootstrap", "server_id": settings.server_id, "apply": True})
            need(guest.run({"operation": "check", "server_id": settings.server_id}) == {"ok": True},
                 "Read-only readiness was not confirmed")
        helper.write_once(directory / "complete.json", helper.encoded({**intent, "ready": True}), managed)
        return {"ok": True, "service_id": settings.server_id, "ip": snapshot["ip"], "ready": True,
                "network_restarted": False, "vpn_restarted": False}
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "plan", "apply"))
    parser.add_argument("--service-id", required=True, choices=SERVICES)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--legacy-ip")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--package-root", type=Path, default=Path("/opt/spn-ip-failover-guest"))
    args = parser.parse_args()
    need(sys.platform.startswith("linux") and os.geteuid() == 0, "Run on the pinned Linux VPS as root")
    ip = public_ip(args.ip)
    helper, managed, netplan = load_modules(args.package_root)
    if args.action == "snapshot":
        print(json.dumps(capture(args.service_id, ip, helper, netplan), sort_keys=True))
        return
    need(args.snapshot is not None, "A private pinned snapshot is required")
    raw = helper.body(helper.record(args.snapshot), mode=0o600)
    snapshot = json.loads(raw, object_pairs_hook=helper.unique)
    legacy_ip = args.legacy_ip or ip
    settings, rendered, value, unit = validate_snapshot(snapshot, args.service_id, ip, legacy_ip, helper, managed, netplan)
    if args.action == "plan":
        print(json.dumps({"plan_only": True, "service_id": args.service_id, "ip": ip,
                          "netplan_before_sha256": snapshot["netplan"]["sha256"],
                          "netplan_after_sha256": helper.digest(rendered), "source_unit_to_disable": unit,
                          "cloud_init_network_will_be_disabled": snapshot["cloud_init"] is None,
                          "network_restarted": False, "vpn_restarted": False}))
    else:
        print(json.dumps(apply(snapshot, settings, rendered, value, unit, legacy_ip, helper, managed, netplan)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Netplan enrollment stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
