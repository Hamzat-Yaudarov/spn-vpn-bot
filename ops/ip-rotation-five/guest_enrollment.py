#!/usr/bin/env python3
"""Pinned five-VPS network handoff. Snapshot/plan are read-only; apply never restarts services."""
from __future__ import annotations

import argparse
import base64
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
import types


# No caller-supplied unit name, interface path, shell command, or extra VPS.
SERVICES = {
    "f5c0287a-636e-4981-95c7-80b24c9559c0": None,
    "21e508f0-2ef2-47da-aee7-15dc8c66eb15": "6a7fa82e4c8740dc9b46b79b1829d5e2",
    "9b0316d5-fa83-4bf8-b6dc-8202b66622c9": "2f5435abe9eb45dc895adcdd759f79f9",
    "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d": "e75882a2c9684ee7b5d3b8457059a320",
    "4d6f358a-e75f-4dd8-a635-c6ad320f2732": "fddb54bd54424364a036afab3f79a919",
}
CONFIG = Path("/etc/spn-ip-failover-guest.json")
STATE = Path("/var/lib/spn-ip-failover-guest")
ENROLLMENT = Path("/var/lib/spn-ip-failover-enrollment")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"}
MAX_FILE = 256 * 1024


def need(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "Duplicate JSON field")
        result[key] = value
    return result


def run(*args, allowed=(0,)):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=ENV, timeout=20, check=False)
    need(result.returncode in allowed and len(result.stdout) <= MAX_FILE, "A fixed readback/command failed")
    return result.stdout.decode("utf-8").strip()


def safe_parents(path, *, allow_missing=False):
    for parent in reversed(path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            need(allow_missing, "An expected root-controlled directory is missing")
            return
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
             "Path parents are not root controlled")


def record(path, *, content=True, absent=False):
    safe_parents(path, allow_missing=absent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        need(absent, "An expected owned file is missing")
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
             and not info.st_mode & 0o022, "Unexpected root file ownership or mode")
        body = stream.read(MAX_FILE + 1)
        need(len(body) <= MAX_FILE, "Owned file exceeds limit")
    value = {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
             "size": len(body), "sha256": digest(body)}
    if content:
        value["base64"] = base64.b64encode(body).decode("ascii")
    return value


def body(value, *, mode=None):
    need(isinstance(value, dict) and set(value) == {"uid", "gid", "mode", "size", "sha256", "base64"}
         and value["uid"] == 0 and type(value["gid"]) is int and type(value["mode"]) is int
         and not value["mode"] & 0o022 and (mode is None or value["mode"] == mode),
         "Invalid pinned file record")
    raw = base64.b64decode(value["base64"], validate=True)
    need(len(raw) <= MAX_FILE and len(raw) == value["size"] and digest(raw) == value["sha256"],
         "Pinned file digest differs")
    return raw


def unit_bytes(deployment, ip):
    base = "/opt/spn-reality-network/" + deployment
    return ("[Unit]\nDescription=SPN owned preferred IPv4 " + deployment + "\n"
            "Wants=network-online.target\nAfter=network-online.target\n\n"
            "[Service]\nType=oneshot\nUser=root\nRemainAfterExit=yes\nTimeoutStartSec=45\n"
            "ExecStart=/usr/bin/python3 " + base + "/source.py apply --ip " + ip
            + " --state-dir " + base + "\n\n[Install]\nWantedBy=multi-user.target\n").encode()


def optional_probe(*args):
    """Diagnostics must not hide the network manager merely because a tool is absent."""
    if not Path(args[0]).is_file():
        return "missing"
    try:
        return run(*args)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "unavailable"


def network_file_metadata():
    """Never emit YAML/netplan contents: they may contain authentication secrets."""
    result = []
    for directory, patterns in ((Path("/etc/netplan"), ("*.yaml", "*.yml")),
                                 (Path("/etc/systemd/network"), ("*.network",)),
                                 (Path("/run/systemd/network"), ("*.network",))):
        for path in sorted({entry for pattern in patterns for entry in directory.glob(pattern)}):
            need(len(result) < 128, "Too many network files for a bounded diagnostic snapshot")
            try:
                metadata = record(path, content=False)
                result.append({"path": str(path), **metadata})
            except (OSError, ValueError):
                # A symlink or untrusted file is diagnostic evidence, never an
                # excuse to read its target/content or choose another adapter.
                result.append({"path": str(path), "untrusted_or_unreadable": True})
    return result


def capture(service, ip):
    deployment = SERVICES[service]
    network = {}
    for name, args in (("addresses", ("address", "show", "scope", "global")),
                       ("rules", ("rule", "show")),
                       ("default", ("route", "show", "table", "main", "default")),
                       ("selected", ("route", "get", "1.1.1.1"))):
        network[name] = json.loads(run("/usr/sbin/ip", "-j", "-4", *args), object_pairs_hook=unique)
    matches = [row["ifname"] for row in network["addresses"] if "UP" in row.get("flags", [])
               for entry in row.get("addr_info", []) if entry.get("family") == "inet" and entry.get("local") == ip]
    need(len(matches) == 1 and re.fullmatch(r"[A-Za-z0-9_.-]{1,12}", matches[0]),
         "Current IP is not uniquely assigned to an UP interface")
    interface = matches[0]
    managers = {name: run("/usr/bin/systemctl", "is-active", name, allowed=(0, 3, 4))
                for name in ("networking", "NetworkManager", "systemd-networkd")}
    managers["ifquery"] = optional_probe("/usr/sbin/ifquery", "--state", interface)
    if managers["systemd-networkd"] == "active":
        managers["networkctl"] = optional_probe("/usr/bin/networkctl", "--no-pager", "--no-legend", "list", interface)
    units = run("/usr/bin/systemctl", "list-unit-files", "--no-pager", "--no-legend",
                "spn-reality-source-*.service", allowed=(0, 1))
    legacy = None
    if deployment:
        directory = Path("/opt/spn-reality-network") / deployment
        unit = "spn-reality-source-" + deployment + ".service"
        legacy = {"unit": record(Path("/etc/systemd/system") / unit, absent=True),
                  "plan": record(directory / "plan.json", absent=True),
                  "source": record(directory / "source.py", content=False, absent=True),
                  "stage_plan": record(Path("/root/spn-reality-managed-" + deployment)
                                       / "preferred-source-plan.json", absent=True),
                  "active_state": run("/usr/bin/systemctl", "is-active", unit, allowed=(0, 3, 4))}
    return {"format": 1, "service_id": service, "ip": ip, "deployment": deployment,
            "interface": interface, "network": network, "managers": managers,
            "units": units, "interfaces": record(Path("/etc/network/interfaces"), absent=True),
            "network_files": network_file_metadata(), "legacy": legacy}


def validate_snapshot(snapshot, service, ip, source_sha, managed, *, legacy_ip=None):
    need(set(snapshot) == {"format", "service_id", "ip", "deployment", "interface", "network",
                           "managers", "units", "interfaces", "network_files", "legacy"}
         and snapshot["format"] == 1 and snapshot["service_id"] == service and snapshot["ip"] == ip
         and snapshot["deployment"] == SERVICES[service], "Snapshot identifies a different VPS")
    need(IPv4Address(ip).is_global and str(IPv4Address(ip)) == ip, "Expected a canonical public IPv4")
    need(snapshot["interfaces"] is not None, "ifupdown interfaces file is missing; diagnostic snapshot cannot be enrolled")
    need(isinstance(snapshot["managers"], dict) and snapshot["managers"].get("ifquery") not in (None, "missing", "unavailable"),
         "ifquery is missing/unavailable; this network is not authorized for the ifupdown adapter")
    raw = body(snapshot["interfaces"])
    deployment = SERVICES[service]
    legacy_ip = ip if legacy_ip is None else legacy_ip
    need(IPv4Address(legacy_ip).is_global and str(IPv4Address(legacy_ip)) == legacy_ip,
         "Invalid explicitly pinned legacy IPv4")
    unit_rows = [line.split() for line in snapshot["units"].splitlines()]
    legacy = snapshot["legacy"]
    if deployment:
        unit = "spn-reality-source-" + deployment + ".service"
        need(len(unit_rows) == 1 and len(unit_rows[0]) in (2, 3)
             and unit_rows[0][0] == unit and unit_rows[0][1] in ("enabled", "disabled"),
             "Unexpected legacy source unit ownership/state")
        need(isinstance(legacy, dict) and set(legacy) == {"unit", "plan", "source", "stage_plan", "active_state"}
             and legacy["active_state"] in ("active", "inactive", "failed"), "Legacy source unit is still transitioning")
        need(body(legacy["unit"], mode=0o644) == unit_bytes(deployment, legacy_ip),
             "Legacy unit differs from the explicitly pinned previous deployment IP")
        plan_raw = body(legacy["plan"], mode=0o600)
        plan = json.loads(plan_raw, object_pairs_hook=unique)
        need(isinstance(plan, dict) and set(plan) == {"version", "deployment", "target", "before", "after", "beforeSource", "helperHash"}
             and plan["version"] == 1 and plan["deployment"] == deployment and plan["target"] == legacy_ip
             and plan["helperHash"] == source_sha, "Legacy plan/helper is not the explicitly pinned deployment")
        need(re.fullmatch(r"[0-9a-f]{64}", source_sha or "")
             and isinstance(legacy["source"], dict)
             and legacy["source"] == {"uid": 0, "gid": 0, "mode": 0o600,
                                      "size": legacy["source"].get("size"), "sha256": source_sha}
             and type(legacy["source"]["size"]) is int and 0 < legacy["source"]["size"] <= MAX_FILE,
             "Legacy helper hash/ownership is not trusted")
        if legacy["stage_plan"] is not None:
            need(body(legacy["stage_plan"], mode=0o600) == plan_raw, "Stage and persistent legacy plans differ")
        need(isinstance(plan["before"], dict) and plan["after"] == {**plan["before"], "prefsrc": legacy_ip},
             "Legacy source transition differs")
    else:
        need(not unit_rows and legacy is None, "Unexpected preferred-source unit on the manual VPS")
    network = snapshot["network"]
    need(isinstance(network, dict) and set(network) == {"addresses", "rules", "default", "selected"}
         and len(network["default"]) == 1, "Invalid network snapshot")
    gateway = network["default"][0].get("gateway")
    settings = managed.Settings.from_value({"version": 1, "server_id": service,
        "interface": snapshot["interface"], "gateway": gateway, "method": "ifupdown",
        "interfaces_file": "/etc/network/interfaces", "state_directory": str(STATE),
        "bootstrap": {"expected_config_sha256": digest(raw), "expected_live_ipv4": [ip],
                      "active": {"id": ip, "ip": ip}, "allocations": [{"id": ip, "ip": ip}]}})

    class CapturedKernel(managed.SystemKernel):
        def run(self, *argv, **_kwargs):
            if argv[:3] == ("/usr/bin/systemctl", "is-active", "networking"):
                return snapshot["managers"]["networking"]
            if argv[:2] == ("/usr/bin/systemctl", "is-active"):
                return snapshot["managers"][argv[2]]
            if argv[:1] == ("/usr/sbin/ifquery",):
                return snapshot["managers"]["ifquery"]
            if argv[:1] == ("/usr/bin/networkctl",):
                return snapshot["managers"]["networkctl"]
            if argv[:2] == ("/usr/bin/systemctl", "list-unit-files"):
                # Dry plan validates the intended state after the separately
                # proven source unit is disabled; this performs no command.
                return "" if not deployment else unit + " disabled enabled"
            raise ValueError("Unrecognized fixed snapshot command")

        def ip(self, *args):
            names = {("address", "show", "scope", "global"): "addresses", ("rule", "show"): "rules",
                     ("route", "show", "table", "main", "default"): "default",
                     ("route", "get", "1.1.1.1"): "selected"}
            return network[names[args]]

    live = CapturedKernel(settings).snapshot()
    need(live["ips"] == [ip] and live["source"] == ip, "Expected exactly the currently billed primary IP and source")
    if deployment:
        need({k: v for k, v in live["route"].items() if k != "prefsrc"}
             == {k: v for k, v in plan["after"].items() if k != "prefsrc"},
             "Live route differs from the pinned legacy route apart from the authorized source IP change")
    rendered = managed.canonical_interfaces(raw, settings, settings.bootstrap["allocations"], settings.bootstrap["active"])
    return settings, rendered


def create_directory(path):
    safe_parents(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
         "Enrollment directory must be root-owned mode 0700")


def write_once(path, raw, managed):
    existing = record(path, absent=True)
    if existing is not None:
        need(body(existing, mode=0o600) == raw, "Existing enrollment/config differs")
        return
    managed.atomic_write(path, raw, 0o600)


def apply(snapshot, settings, rendered, source_sha, managed, *, legacy_ip=None):
    create_directory(ENROLLMENT)
    directory = ENROLLMENT / settings.server_id
    create_directory(directory)
    lock = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        meta = os.fstat(lock)
        need(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0 and meta.st_nlink == 1
             and stat.S_IMODE(meta.st_mode) == 0o600, "Unsafe enrollment lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_once(directory / "snapshot.json", encoded(snapshot), managed)
        intent = {"format": 1, "snapshot_sha256": digest(encoded(snapshot)), "service_id": settings.server_id,
                  "ip": snapshot["ip"], "legacy_ip": legacy_ip or snapshot["ip"], "source_helper_sha256": source_sha}
        write_once(directory / "intent.json", encoded(intent), managed)
        current = capture(settings.server_id, snapshot["ip"])
        # On retry, only our own disabled unit and canonical network file may
        # differ. Every other live field and owned legacy file stays pinned.
        need(current["network"] == snapshot["network"] and current["managers"] == snapshot["managers"],
             "Live network/manager snapshot changed before handoff")
        original = body(snapshot["interfaces"])
        need(body(current["interfaces"]) in (original, rendered), "Interfaces changed before handoff")
        deployment = SERVICES[settings.server_id]
        if deployment:
            need(current["legacy"] == snapshot["legacy"], "Owned legacy files/state changed before handoff")
            unit = "spn-reality-source-" + deployment + ".service"
            rows = [line.split() for line in current["units"].splitlines()]
            need(len(rows) == 1 and len(rows[0]) in (2, 3) and rows[0][0] == unit
                 and rows[0][1] in ("enabled", "disabled"), "Unexpected source unit appeared")
            # Backups precede the only systemd mutation. Never stop/restart the
            # unit, networking, Xray, Docker, or the VPS.
            for name in ("unit", "plan", "stage_plan"):
                if snapshot["legacy"][name] is not None:
                    write_once(directory / ("legacy-" + name), body(snapshot["legacy"][name]), managed)
            write_once(directory / "legacy-source-metadata.json", encoded(snapshot["legacy"]["source"]), managed)
            source_path = Path("/opt/spn-reality-network") / deployment / "source.py"
            source_record = record(source_path)
            need({name: value for name, value in source_record.items() if name != "base64"}
                 == snapshot["legacy"]["source"], "Legacy source changed before backup")
            write_once(directory / "legacy-source.py", body(source_record, mode=0o600), managed)
            if rows[0][1] == "enabled":
                run("/usr/bin/systemctl", "disable", unit)
            need(run("/usr/bin/systemctl", "is-enabled", unit, allowed=(0, 1)) == "disabled",
                 "Legacy source boot writer was not disabled")
        else:
            need(not current["units"], "Unexpected source boot writer appeared")
        value = {"version": 1, "server_id": settings.server_id, "interface": settings.interface,
                 "gateway": settings.gateway, "method": "ifupdown", "interfaces_file": str(settings.interfaces_file),
                 "state_directory": str(settings.state_directory), "bootstrap": settings.bootstrap}
        write_once(CONFIG, encoded(value), managed)
        store = managed.FileStore(settings)
        guest = managed.ManagedGuest(settings, store, managed.SystemKernel(settings))
        with store.locked():
            guest.run({"operation": "bootstrap", "server_id": settings.server_id, "apply": True})
            need(guest.run({"operation": "check", "server_id": settings.server_id}) == {"ok": True},
                 "Read-only guest readiness was not confirmed")
        write_once(directory / "complete.json", encoded({**intent, "ready": True}), managed)
        return {"ok": True, "service_id": settings.server_id, "ip": snapshot["ip"],
                "network_restarted": False, "vpn_restarted": False, "ready": True}
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "plan", "apply"))
    parser.add_argument("--service-id", required=True, choices=SERVICES)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--source-sha256", default="")
    parser.add_argument("--legacy-ip", help="Explicit old installer IP, verified from local deployment state; defaults to current IP")
    parser.add_argument("--package-root", type=Path, default=Path("/opt/spn-ip-failover-guest"))
    args = parser.parse_args()
    need(sys.platform.startswith("linux") and os.geteuid() == 0, "Run on the pinned Linux guest as root")
    need(IPv4Address(args.ip).is_global and str(IPv4Address(args.ip)) == args.ip, "Invalid public IPv4")
    if args.action == "snapshot":
        print(json.dumps(capture(args.service_id, args.ip), sort_keys=True))
        return
    need(args.snapshot is not None, "A pinned snapshot is required")
    package = args.package_root / "spn_ip_failover/managed_guest.py"
    # Execute only the validated root-owned source, never a neighboring package
    # initializer or an unvalidated pre-existing bytecode cache.
    module_raw = body(record(package))
    managed = types.ModuleType("_spn_pinned_managed_guest")
    managed.__file__ = str(package)
    sys.modules[managed.__name__] = managed
    exec(compile(module_raw, str(package), "exec"), managed.__dict__)
    raw = body(record(args.snapshot), mode=0o600)
    snapshot = json.loads(raw, object_pairs_hook=unique)
    settings, rendered = validate_snapshot(snapshot, args.service_id, args.ip, args.source_sha256, managed,
                                           legacy_ip=args.legacy_ip)
    if args.action == "plan":
        deployment = SERVICES[args.service_id]
        print(json.dumps({"plan_only": True, "service_id": args.service_id, "ip": args.ip,
                          "legacy_ip": args.legacy_ip or args.ip,
                          "interfaces_before_sha256": snapshot["interfaces"]["sha256"],
                          "interfaces_after_sha256": digest(rendered),
                          "source_unit_to_disable": None if deployment is None else "spn-reality-source-" + deployment + ".service",
                          "network_restarted": False, "vpn_restarted": False}))
    else:
        print(json.dumps(apply(snapshot, settings, rendered, args.source_sha256, managed, legacy_ip=args.legacy_ip)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Guest enrollment stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
