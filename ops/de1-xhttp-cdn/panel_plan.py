#!/usr/bin/env python3
"""GET-only infrastructure snapshot and offline plan. There is no apply command."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

import prepare as origin

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "DE1_XHTTP_TIMEWEB"
NODE = "node-de1-xhttp-timeweb"
HOST = "DE1 • Timeweb XHTTP • TEST"
TAG = "vless-xhttp-de1-timeweb"
SOURCE_PROFILE = "59aba40b-9ed3-4eec-b6af-b34a39da88d1"
SOURCE_HOST = "c1614c56-403d-4a67-b917-928a04600db8"
TEST_SQUAD = "DE"
NEW_PROFILE = "__NEW_PROFILE_UUID__"
NEW_INBOUND = "__NEW_INBOUND_UUID__"
NEW_NODE = "__NEW_NODE_UUID__"
NEW_HOST = "__NEW_HOST_UUID__"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def private_directory(path):
    require(path.is_absolute() and path.resolve().is_relative_to(Path("/private/tmp")),
            "Output must be inside /private/tmp, outside the repository")
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(path, 0o700)


def write(path, value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def read(path):
    require(not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600,
            "Input must be a non-symlink private 0600 JSON file")
    return json.loads(path.read_text())


def metadata_snapshot(raw):
    """Explicit projections discard raw configs, keys, users and squad members."""
    snapshot = {"format": 1, "createdAt": datetime.now(timezone.utc).isoformat()}
    snapshot["profiles"] = [{
        "uuid": p["uuid"], "name": p["name"], "configSha256": digest(p["config"]),
        "inbounds": [{"uuid": i["uuid"], "tag": i["tag"]} for i in p["inbounds"]],
    } for p in raw["profiles"]]
    host_fields = ("uuid", "remark", "address", "port", "isDisabled", "isHidden", "tags",
                   "inbound", "nodes", "internalSquads", "alpn", "fingerprint", "securityLayer")
    snapshot["hosts"] = [{k: copy.deepcopy(h.get(k)) for k in host_fields} for h in raw["hosts"]]
    for saved, raw_host in zip(snapshot["hosts"], raw["hosts"]):
        extra = raw_host.get("xhttpExtraParams")
        saved["xhttpExtraParamsEncoding"] = ("json-string" if isinstance(extra, str)
                                             else "object" if isinstance(extra, dict) else None)
    snapshot["nodes"] = []
    for node in raw["nodes"]:
        item = {k: copy.deepcopy(node.get(k)) for k in (
            "uuid", "name", "address", "port", "isDisabled", "isConnected"
        )}
        binding = node.get("configProfile") or {}
        item["configProfile"] = {
            "activeConfigProfileUuid": binding.get("activeConfigProfileUuid"),
            "activeInbounds": [{"uuid": i["uuid"], "tag": i.get("tag")}
                               for i in binding.get("activeInbounds", [])],
        }
        snapshot["nodes"].append(item)
    snapshot["squads"] = [{"uuid": s["uuid"], "name": s["name"],
                            "inbounds": [{"uuid": i["uuid"]} for i in s["inbounds"]]}
                           for s in raw["squads"]]
    for key in ("profiles", "hosts", "nodes", "squads"):
        ids = [item["uuid"] for item in snapshot[key]]
        require(len(ids) == len(set(ids)), "Duplicated infrastructure UUIDs in snapshot")
    return snapshot


def collect_snapshot():
    # Lazy import: offline planning/tests never load .env or create API clients.
    sys.path.insert(0, str(ROOT))
    from scripts.prepare_direct_transports import api
    return metadata_snapshot({
        "profiles": api("GET", "/config-profiles")["configProfiles"],
        "hosts": api("GET", "/hosts"), "nodes": api("GET", "/nodes"),
        "squads": api("GET", "/internal-squads")["internalSquads"],
    })


def build_plan(snapshot, stage, cdn_domain):
    state = origin.check_stage(stage)
    require(bool(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+chislo\.site", cdn_domain)),
            "CDN hostname must be an explicit chislo.site subdomain")
    require(cdn_domain != origin.DOMAIN, "CDN delivery hostname must differ from origin hostname")
    profiles, hosts, nodes, squads = (snapshot[k] for k in ("profiles", "hosts", "nodes", "squads"))
    require(not any(p["name"] == PROFILE for p in profiles), "Dedicated profile already exists; do not duplicate")
    require(not any(n["name"] == NODE or n["address"] == origin.ORIGIN_IP for n in nodes),
            "Node name/address already exists; ownership/resume requires separate review")
    require(not any(h["remark"] == HOST or (h["address"] == cdn_domain and h["port"] == 443) for h in hosts),
            "Host name or CDN endpoint already exists; do not duplicate")
    source_profiles = [p for p in profiles if p["uuid"] == SOURCE_PROFILE]
    source_hosts = [h for h in hosts if h["uuid"] == SOURCE_HOST]
    selected_squads = [s for s in squads if s["name"] == TEST_SQUAD]
    require(len(source_profiles) == len(source_hosts) == len(selected_squads) == 1,
            "Expected one DE11 source and exactly one DE test squad")
    source = source_hosts[0]
    source_link = source.get("inbound") or {}
    require(source_link.get("configProfileUuid") == SOURCE_PROFILE
            and source_link.get("configProfileInboundUuid") in {
                i["uuid"] for i in source_profiles[0]["inbounds"]
            } and source.get("securityLayer") == "TLS", "DE11 source host binding/security changed")
    require(bool(source.get("alpn")) and bool(source.get("fingerprint")),
            "Review the DE11 source ALPN/fingerprint before creating a new host")
    require(source.get("xhttpExtraParamsEncoding") in {"json-string", "object"},
            "Review the working DE11 XHTTP extras representation before creating a new host")
    squad = selected_squads[0]
    config = read(stage / "configprofile.json")
    config["inbounds"][0]["tag"] = TAG
    profile = {"name": PROFILE, "config": config}
    node = {"name": NODE, "address": origin.ORIGIN_IP, "port": 2222, "countryCode": "DE",
            "tags": ["DE1_XHTTP_TIMEWEB_TEST"],
            "note": "Dedicated DE1 Timeweb XHTTP pilot; no RU cascade; DE test access only",
            "configProfile": {"activeConfigProfileUuid": NEW_PROFILE, "activeInbounds": [NEW_INBOUND]}}
    host = {"remark": HOST, "address": cdn_domain, "port": 443, "sni": cdn_domain,
            "host": cdn_domain, "path": state["edgePath"], "securityLayer": "TLS",
            "alpn": source["alpn"], "fingerprint": source["fingerprint"],
            "isDisabled": True, "isHidden": False, "shuffleHost": False,
            "tags": ["DE1_XHTTP_TIMEWEB_TEST"], "nodes": [NEW_NODE],
            "inbound": {"configProfileUuid": NEW_PROFILE, "configProfileInboundUuid": NEW_INBOUND},
            "internalSquads": {"mode": "ALLOW_ONLY", "squads": [squad["uuid"]]},
            "xrayJsonTemplateUuid": None}
    extra = read(stage / "xhttp-extra.json")
    host["xhttpExtraParams"] = (json.dumps(extra, separators=(",", ":"))
                               if source["xhttpExtraParamsEncoding"] == "json-string" else extra)
    # This is a later, explicit existing-object edit, NOT part of create-only work.
    squad_plan = {"status": "DO_NOT_APPLY_WITHOUT_SEPARATE_REVIEW", "method": "PATCH",
                  "path": "/internal-squads", "requiresExistingObjectMutation": True,
                  "body": {"uuid": squad["uuid"], "inbounds": [i["uuid"] for i in squad["inbounds"]] + [NEW_INBOUND]},
                  "guard": "Re-GET DE; preserve every old inbound; append only this new inbound; leave all other squads unchanged"}
    selector = {"selector": {"type": "uuids", "values": [NEW_HOST]},
                "selectFrom": "NOT_HIDDEN", "tagPrefix": "de1-timeweb-test"}
    return {"profile.create.json": profile, "node.create.template.json": node,
            "host.create.template.json": host, "de-squad.later-plan.json": squad_plan,
            "dedicated-selector.template.json": selector,
            "plan.json": {"format": 1, "snapshotSha256": digest(snapshot), "profileName": PROFILE,
                          "nodeName": NODE, "hostRemark": HOST, "cdnDomain": cdn_domain,
                          "originDomain": origin.DOMAIN, "originIp": origin.ORIGIN_IP,
                          "testSquadUuid": squad["uuid"], "existingObjectWritesAllowed": False,
                          "subscriberEndpointsRead": False, "applyImplemented": False}}


COMPOSE = """services:
  remnanode:
    image: remnawave/node:3.4.1
    container_name: remnanode
    hostname: remnanode
    restart: unless-stopped
    network_mode: host
    env_file:
      - node.env
    logging:
      driver: json-file
      options:
        max-size: 20m
        max-file: '3'
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--out", type=Path, required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--snapshot", type=Path, required=True)
    plan.add_argument("--origin-stage", type=Path, default=origin.DEFAULT_STAGE)
    plan.add_argument("--cdn-domain", required=True)
    plan.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "snapshot":
        snapshot = collect_snapshot()
        private_directory(args.out)
        write(args.out / "snapshot.json", snapshot)
        print("Private infrastructure metadata snapshot saved; no raw configs, users or keys exported.")
    else:
        artifacts = build_plan(read(args.snapshot), args.origin_stage, args.cdn_domain)
        private_directory(args.out)
        for name, content in artifacts.items():
            write(args.out / name, content)
        write(args.out / "docker-compose.yml", COMPOSE)
        write(args.out / "node.env.template", "NODE_PORT=2222\nSECRET_KEY=__OBTAIN_PRIVATELY_FROM_PANEL_KEYGEN_AFTER_APPROVAL__\n")
        print("Private create-only plan saved. UUID/env placeholders are NOT executable API payloads.")
        print("No API writes, keygen request, node launch, firewall or squad changes performed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Plan stopped ({type(exc).__name__}); no response/config/credential data printed.", file=sys.stderr)
        raise SystemExit(1)
