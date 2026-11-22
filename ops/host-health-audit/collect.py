"""Explicit GET-only host inventory. Default is a pure, no-I/O plan.

Full infrastructure snapshots contain server secrets and remain owner-private.
The separate inventory uses an explicit public-field projection. No user or
subscription endpoint, external reachability probe or mutation is implemented.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
STATE = Path("/private/tmp/wayspn-host-health-20261024")
HOST_FIELDS = ("uuid", "address", "port", "path", "sni", "host", "alpn", "fingerprint", "securityLayer", "isDisabled")


class AuditError(ValueError): pass


def require(value):
    if not value: raise AuditError("Infrastructure inventory prerequisites failed; details suppressed.")


class Parser(argparse.ArgumentParser):
    def error(self, message): raise AuditError("Only the fixed read-only collection option is accepted.")


def api(path):
    require(path in {"/hosts", "/nodes", "/config-profiles", "/internal-squads"})
    spec = importlib.util.spec_from_file_location("_host_health_readonly_adapter", ROOT / "ops/direct-pol/manage.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    return adapter.api("GET", path)


def snapshot():
    return {"hosts": api("/hosts"), "profiles": api("/config-profiles")["configProfiles"],
            "nodes": api("/nodes"), "squads": api("/internal-squads")["internalSquads"]}


def index(rows):
    require(isinstance(rows, list) and len(rows) <= 20_000)
    result = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("uuid"), str) and row["uuid"]
                and row["uuid"] not in result)
        result[row["uuid"]] = row
    return result


def text(value):
    return value if isinstance(value, str) else None


def identifier(value):
    return value.get("uuid") if isinstance(value, dict) else value if isinstance(value, str) else None


def public_node(node, profile_id, inbound_id, *, visual=False):
    binding = node.get("configProfile") or {}
    active = [identifier(item) for item in binding.get("activeInbounds", [])]
    active = [value for value in active if isinstance(value, str)]
    matches = binding.get("activeConfigProfileUuid") == profile_id and inbound_id in active
    return {"uuid": text(node.get("uuid")), "name": text(node.get("name")), "address": text(node.get("address")),
            "isConnected": node.get("isConnected") if type(node.get("isConnected")) is bool else None,
            "isDisabled": node.get("isDisabled") if type(node.get("isDisabled")) is bool else None,
            "profile": text(binding.get("activeConfigProfileUuid")), "activeInboundIds": active,
            "visualHostNode": visual,
            "hasHostInbound": bool(matches),
            "eligibleConnected": bool(matches and node.get("isConnected") is True and node.get("isDisabled") is False)}


def inventory(raw):
    require(isinstance(raw, dict) and set(raw) == {"hosts", "profiles", "nodes", "squads"})
    hosts, profiles, nodes, _ = (index(raw[name]) for name in ("hosts", "profiles", "nodes", "squads"))
    result = []
    for host in hosts.values():
        row = {name: (host.get(name) if type(host.get(name)) is (bool if name == "isDisabled" else int)
                      else None) if name in {"port", "isDisabled"} else text(host.get(name)) for name in HOST_FIELDS}
        row["name"] = text(host.get("remark"))
        link = host.get("inbound") or {}
        profile_id, inbound_id = text(link.get("configProfileUuid")), text(link.get("configProfileInboundUuid"))
        errors, warnings, selected = [], [], None
        profile = profiles.get(profile_id)
        if profile is None:
            errors.append("missing_profile")
        else:
            metadata = [item for item in profile.get("inbounds", []) if item.get("uuid") == inbound_id]
            if len(metadata) != 1:
                errors.append("missing_or_ambiguous_inbound_metadata")
            else:
                selected = [item for item in (profile.get("config") or {}).get("inbounds", [])
                            if item.get("tag") == metadata[0].get("tag")]
                if len(selected) != 1:
                    errors.append("missing_or_ambiguous_inbound_config")
                    selected = None
                else: selected = selected[0]
        stream = (selected or {}).get("streamSettings") or {}
        row["inbound"] = {"profileUuid": profile_id, "uuid": inbound_id,
                          "network": text(stream.get("network")), "protocol": text((selected or {}).get("protocol")),
                          "security": text(stream.get("security"))}
        explicit = host.get("nodes") or []
        require(isinstance(explicit, list))
        explicit_ids = [identifier(item) for item in explicit]
        if any(uid not in nodes for uid in explicit_ids): warnings.append("missing_visual_host_node")
        # host.nodes is a visual association, not a serving-node restriction.
        # Keep matching profile/inbound nodes even with empty/stale visual links.
        candidates = [node for uid, node in nodes.items()
                      if uid in explicit_ids or ((node.get("configProfile") or {}).get("activeConfigProfileUuid") == profile_id
                      and inbound_id in [identifier(item) for item in (node.get("configProfile") or {}).get("activeInbounds", [])])]
        row["candidateNodes"] = [public_node(node, profile_id, inbound_id, visual=node["uuid"] in explicit_ids) for node in candidates]
        if not row["candidateNodes"] or not any(node["hasHostInbound"] for node in row["candidateNodes"]):
            warnings.append("no_matching_node_binding")
        if not any(node["eligibleConnected"] for node in row["candidateNodes"]):
            warnings.append("no_eligible_connected_node")
        row.update(structuralErrors=errors, statusWarnings=warnings, healthVerdict="not_probed", deleteRecommended=False)
        result.append(row)
    return {"format": 1, "collectedAt": datetime.now(timezone.utc).isoformat(), "hosts": result,
            "notice": "Structural/status evidence only. No reachability tests or deletion decisions."}


def private_directory():
    require(STATE.parent == Path("/private/tmp") and not STATE.parent.is_symlink())
    try:
        STATE.mkdir(mode=0o700)
        directory = STATE
    except FileExistsError:
        directory = Path(tempfile.mkdtemp(prefix=STATE.name + "-", dir=STATE.parent))
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
    return directory


def write(directory, name, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    require(len(raw) <= 64_000_000)
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def collect():
    raw = snapshot()
    public = inventory(raw)
    directory = private_directory()
    write(directory, "infrastructure.private.json", raw)
    write(directory, "inventory.json", public)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
    return {"collected": True, "inventory": str(directory / "inventory.json"),
            "counts": {name: len(raw[name]) for name in raw},
            "hosts_with_structural_findings": sum(bool(row["structuralErrors"]) for row in public["hosts"]),
            "hosts_without_eligible_connected_node": sum("no_eligible_connected_node" in row["statusWarnings"] for row in public["hosts"]),
            "mutations": 0, "deletions_recommended": 0}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--collect", action="store_true")
        args = parser.parse_args(argv)
        result = collect() if args.collect else {"plan_only": True, "network": False, "mutations": 0}
        print(json.dumps(result, sort_keys=True)); return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0: return 0
        print("Read-only collection stopped; credentials and raw responses suppressed.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__": raise SystemExit(main())
