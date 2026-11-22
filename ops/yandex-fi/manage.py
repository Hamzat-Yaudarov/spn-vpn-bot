"""Add ONE Yandex XHTTP transport to the reviewed Finland direct node.

Default prints a pure plan. prepare is local-only; audit GET-only. Every write
is a separate confirmed stage with an exclusive durable intent and fresh audit.
Never reads users/subscriptions, rotates keys, edits source CDN, DNS or Nginx.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
IP, ORIGIN, DOMAIN = "92.42.102.103", "fi103.wayspn.com", "cdn-fi103.wayspn.com"
DIRECT_PROFILE_NAME = "DIRECT_FI_102_103"
DIRECT_NODE_NAME = "node-direct-fi-102.103"
DIRECT_TAGS = {"ordinary-grpc-reality-fi", "ordinary-ws-fi"}
DIRECT_SOURCE_HOSTS = ["52edd62d-7e98-4b04-8cb4-67086a3660ea", "cd19762c-685e-4d91-8124-2ddfa33ee59a"]
DIRECT_STATE = Path("/private/tmp/wayspn-direct-fi-20261030")
SOURCE_PROFILE = "59aba40b-9ed3-4eec-b6af-b34a39da88d1"
SOURCE_HOST = "c1614c56-403d-4a67-b917-928a04600db8"
SOURCE_INBOUND = "3f4c7e56-8784-4783-a34a-27dd1123c7a8"
SOURCE_TAG, TAG, PATH = "vless-xhttp-yandex-de11", "vless-xhttp-yandex-fi", "/assets/app/"
REMARK = "🇫🇮 Финляндия • ОБХОД CDN"
DEFAULT_STATE = Path("/private/tmp/wayspn-yandex-fi-20261030/operation")
HOST_FIELDS = set("""address port path sni host alpn fingerprint securityLayer xhttpExtraParams muxParams
sockoptParams finalMask serverDescription pinnedPeerCertSha256 verifyPeerCertByName shuffleHost
mihomoX25519 mihomoIpVersion isHidden overrideSniFromAddress keepSniBlank vlessRouteId
xrayJsonTemplateUuid excludeFromSubscriptionTypes mapper internalSquads""".split())


class GuardError(ValueError): pass


class Parser(argparse.ArgumentParser):
    def error(self, message): raise GuardError("Only the fixed CDN stage options are accepted.")


def require(value):
    if not value: raise GuardError("Scoped CDN prerequisites changed; private details suppressed.")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def one(rows, uid):
    found = [row for row in rows if row.get("uuid") == uid]
    require(len(found) == 1)
    return found[0]


def ids(rows):
    result = [row["uuid"] if isinstance(row, dict) else row for row in rows]
    require(len(set(result)) == len(result))
    return result


def host_state(host):
    return {key: value for key, value in host.items() if key not in {"updatedAt", "viewPosition"}}


def parsed(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result); result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def api(method, path, body=None):
    require(path in {"/hosts", "/nodes", "/config-profiles", "/internal-squads"})
    # Reuse only the fixed HTTPS/NoRedirect infrastructure adapter; no request
    # or credential read occurs before an explicit snapshot/audit/write stage.
    source = ROOT / "ops/direct-pol/manage.py"
    spec = importlib.util.spec_from_file_location("_scoped_cdn_panel", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.api(method, path, body)


def snapshot():
    return {"profiles": api("GET", "/config-profiles")["configProfiles"], "hosts": api("GET", "/hosts"),
            "nodes": api("GET", "/nodes"), "squads": api("GET", "/internal-squads")["internalSquads"]}


def read(path):
    require(path.is_absolute() and path.resolve().is_relative_to(Path("/private/tmp"))
            and not any(p.is_symlink() for p in (path, *path.parents)))
    parent = path.parent.stat()
    require(parent.st_uid == os.geteuid() and stat.S_IMODE(parent.st_mode) == 0o700)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 8_000_000)
        with os.fdopen(fd, "rb", closefd=False) as stream: raw = stream.read(8_000_001)
        after = os.fstat(fd)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_mode, item.st_uid, item.st_nlink)
        require(len(raw) == info.st_size and signature(after) == signature(info))
        return parsed(raw)
    finally: os.close(fd)


def save(directory, name, value):
    require(directory.is_absolute() and directory.resolve().is_relative_to(Path("/private/tmp"))
            and not any(p.is_symlink() for p in (directory, *directory.parents)))
    info = directory.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def bound(value):
    parts = str(value).split("-")
    require(len(parts) in {1, 2} and all(p.isdecimal() for p in parts))
    numbers = list(map(int, parts)); require(0 < numbers[0] <= numbers[-1])
    return numbers[-1]


DIRECT_FILES = ("plan.json", "profile.create.json", "intent-profile.json", "intent-node.json",
                "intent-grpc.json", "intent-ws.json", "created-profile.json", "created-node.json",
                "created-grpc.json", "created-ws.json")


def direct_digest(value):
    # Match the direct helper's hash format without importing it or credentials.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical_uuid(value):
    require(isinstance(value, str) and str(uuid.UUID(value)) == value)
    return value


def record_check(actual, expected):
    for key, value in expected.items():
        if key == "configProfile":
            require(actual[key]["activeConfigProfileUuid"] == value["activeConfigProfileUuid"]
                    and set(ids(actual[key]["activeInbounds"])) == set(ids(value["activeInbounds"])))
        else: require(actual.get(key) == value)


def direct_scope(records, before=None):
    """Load only identities established by the separately reviewed direct stage.

    No target UUID is guessed from an address/name search. Creation intents,
    returned objects and the fresh snapshot must all agree before preparation.
    """
    require(set(records) >= set(DIRECT_FILES)
            and set(records) <= set(DIRECT_FILES) | {"enable-grpc.json", "enable-ws.json"})
    profile, node = records["created-profile.json"], records["created-node.json"]
    profile_id, node_id = canonical_uuid(profile["uuid"]), canonical_uuid(node["uuid"])
    config = records["profile.create.json"]
    require(config == records["intent-profile.json"] and config.get("name") == DIRECT_PROFILE_NAME)
    record_check(profile, config)
    require(records["plan.json"] == {"format": 1, "address": IP, "domain": ORIGIN,
            "profile": DIRECT_PROFILE_NAME, "node": DIRECT_NODE_NAME,
            "profileHash": direct_digest(config["config"]), "sourceHostIds": DIRECT_SOURCE_HOSTS})
    old_inbounds = {canonical_uuid(i["uuid"]) for i in profile["inbounds"]}
    require(len(profile["inbounds"]) == 2 and len(old_inbounds) == 2
            and {i["tag"] for i in profile["inbounds"]} == DIRECT_TAGS
            and len(config["config"]["inbounds"]) == 2
            and {i["tag"] for i in config["config"]["inbounds"]} == DIRECT_TAGS
            and {i["streamSettings"]["network"] for i in config["config"]["inbounds"]} == {"grpc", "ws"})
    expected_node = records["intent-node.json"]
    require(expected_node.get("name") == DIRECT_NODE_NAME and expected_node.get("address") == IP
            and expected_node.get("port") == 2222 and expected_node.get("countryCode") == "FI"
            and expected_node.get("tags") == ["FI_102_103", "DIRECT"]
            and expected_node.get("configProfile", {}).get("activeConfigProfileUuid") == profile_id
            and set(ids(expected_node["configProfile"]["activeInbounds"])) == old_inbounds)
    record_check(node, expected_node)
    old_hosts = set()
    for kind, tag in (("grpc", "ordinary-grpc-reality-fi"), ("ws", "ordinary-ws-fi")):
        host, intent = records["created-" + kind + ".json"], records["intent-" + kind + ".json"]
        host_id = canonical_uuid(host["uuid"]); old_hosts.add(host_id)
        require(intent.get("isDisabled") is True and intent.get("nodes") == [node_id]
                and intent.get("address") == (IP if kind == "grpc" else ORIGIN)
                and intent.get("sni") == ORIGIN and intent.get("port") == (2083 if kind == "grpc" else 443)
                and intent.get("inbound") == {"configProfileUuid": profile_id,
                    "configProfileInboundUuid": next(i["uuid"] for i in profile["inbounds"] if i["tag"] == tag)})
        record_check(host, intent)
        if before is not None:
            live_host = one(before["hosts"], host_id)
            require(type(live_host.get("isDisabled")) is bool)
            if not live_host["isDisabled"]:
                require(records.get("enable-" + kind + ".json") == {"uuid": host_id, "isDisabled": False})
            record_check(live_host, {**intent, "isDisabled": live_host["isDisabled"]})
    require(len(old_hosts) == 2 and not old_inbounds.intersection(old_hosts)
            and profile_id != node_id and not {profile_id, node_id}.intersection(old_inbounds | old_hosts))
    if before is not None:
        record_check(one(before["profiles"], profile_id), config)
        require(one(before["profiles"], profile_id)["inbounds"] == profile["inbounds"])
        record_check(one(before["nodes"], node_id), expected_node)
    return {"profile": profile_id, "node": node_id,
            "inbounds": sorted(old_inbounds), "hosts": sorted(old_hosts)}


def read_direct(directory):
    records = {name: read(directory / name) for name in DIRECT_FILES}
    for name in ("enable-grpc.json", "enable-ws.json"):
        if (directory / name).exists() or (directory / name).is_symlink(): records[name] = read(directory / name)
    return records


def build(before, scope):
    PROFILE, NODE = scope["profile"], scope["node"]
    OLD_INBOUNDS, OLD_HOSTS = set(scope["inbounds"]), set(scope["hosts"])
    require(set(before) == {"profiles", "hosts", "nodes", "squads"})
    for rows in before.values(): ids(rows)
    target, source = one(before["profiles"], PROFILE), one(before["profiles"], SOURCE_PROFILE)
    node, source_host = one(before["nodes"], NODE), one(before["hosts"], SOURCE_HOST)
    require(node["name"] == DIRECT_NODE_NAME and target["name"] == DIRECT_PROFILE_NAME
            and node["address"] == IP and node["port"] == 2222
            and node["configProfile"]["activeConfigProfileUuid"] == PROFILE
            and set(ids(node["configProfile"]["activeInbounds"])) == OLD_INBOUNDS)
    require(set(ids(target["inbounds"])) == OLD_INBOUNDS and len(target["config"]["inbounds"]) == 2
            and {i["streamSettings"]["network"] for i in target["config"]["inbounds"]} == {"grpc", "ws"}
            and not any(h.get("address") == DOMAIN for h in before["hosts"]))
    for uid in OLD_HOSTS:
        host = one(before["hosts"], uid)
        require(host["inbound"]["configProfileUuid"] == PROFILE
                and host["inbound"]["configProfileInboundUuid"] in OLD_INBOUNDS)
    require(source_host["inbound"] == {"configProfileUuid": SOURCE_PROFILE, "configProfileInboundUuid": SOURCE_INBOUND})
    require(one(source["inbounds"], SOURCE_INBOUND)["tag"] == SOURCE_TAG)
    selected = [i for i in source["config"]["inbounds"] if i.get("tag") == SOURCE_TAG]
    require(len(selected) == 1)
    inbound = copy.deepcopy(selected[0]); stream = inbound["streamSettings"]
    require(inbound.get("protocol") == "vless" and inbound.get("listen") == "127.0.0.1"
            and inbound.get("port") == 10085 and inbound.get("settings") == {"clients": [], "decryption": "none"}
            and stream.get("network") == "xhttp" and stream.get("security") == "none")
    server = stream["xhttpSettings"]
    extra = source_host.get("xhttpExtraParams")
    client = parsed(extra) if isinstance(extra, str) else extra
    require(isinstance(client, dict) and server.get("mode") == "packet-up"
            and server.get("path") == source_host.get("path") == PATH and server.get("serverMaxHeaderBytes") == 8192
            and server.get("host") in {None, ""}
            and not any(name in client for name in ("host", "path", "downloadSettings")))
    for settings in (server, client):
        require(settings.get("uplinkHTTPMethod") == "GET" and settings.get("uplinkDataPlacement") == "header"
                and settings.get("uplinkDataKey") == "X-Transport-Data" and bound(settings.get("scMaxEachPostBytes")) <= 2048)
    require(source_host.get("port") == 443 and source_host.get("securityLayer") == "TLS"
            and source_host.get("internalSquads") == {"mode": "EXCLUDE", "squads": []})
    inbound["tag"] = TAG
    config = copy.deepcopy(target["config"])
    require(not any(i.get("tag") == TAG or i.get("port") == 10085 for i in config["inbounds"]))
    config["inbounds"].append(inbound)
    host = {key: copy.deepcopy(value) for key, value in source_host.items() if key in HOST_FIELDS}
    host.update(remark=REMARK, address=DOMAIN, sni=DOMAIN, host=DOMAIN, port=443, path=PATH,
                isDisabled=True, nodes=[NODE], tags=["FI_YANDEX_CDN"])
    squads = {s["uuid"]: ids(s["inbounds"]) for s in before["squads"] if SOURCE_INBOUND in ids(s["inbounds"])}
    require(squads)
    return {"config": config, "host": host, "squads": squads}


def prepare(before, directory, direct_directory=DIRECT_STATE):
    records = read_direct(direct_directory)
    scope = direct_scope(records, before)
    candidate = build(before, scope)
    require(directory.is_absolute() and directory.resolve().is_relative_to(Path("/private/tmp"))
            and not any(p.is_symlink() for p in (directory, *directory.parents)))
    parent = directory.parent.stat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.geteuid()
            and stat.S_IMODE(parent.st_mode) == 0o700)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    save(directory, "before.json", before); save(directory, "candidate.json", candidate)
    save(directory, "direct-records.json", records)
    save(directory, "validation-profile.json", validation_config(candidate))
    save(directory, "manifest.json", {"format": 1, "ip": IP, "origin": ORIGIN, "scope": scope,
         "domain": DOMAIN, "before_hash": digest(before), "candidate_hash": digest(candidate),
         "direct_records_hash": digest(records)})
    return {"prepared": True, "state": str(directory), "network_writes": 0}


def validation_config(candidate):
    validation = copy.deepcopy(candidate["config"])
    for inbound in validation["inbounds"]:
        if isinstance(inbound.get("settings"), dict) and "clients" in inbound["settings"]:
            inbound["settings"]["clients"] = []
    return validation


def load(directory):
    manifest, before, candidate = (read(directory / name) for name in ("manifest.json", "before.json", "candidate.json"))
    records = read(directory / "direct-records.json")
    scope = direct_scope(records, before)
    require(manifest == {"format": 1, "ip": IP, "origin": ORIGIN, "scope": scope, "domain": DOMAIN,
                         "before_hash": digest(before), "candidate_hash": digest(candidate),
                         "direct_records_hash": digest(records)} and build(before, scope) == candidate)
    require(read(directory / "validation-profile.json") == validation_config(candidate))
    return before, candidate, scope


def intent_matches(directory, name, method, endpoint, body):
    require(read(directory / ("intent-" + name + ".json")) == {"method": method, "endpoint": endpoint, "body": body})


def audit(directory, live=None):
    before, wanted, scope = load(directory)
    PROFILE, NODE, OLD_INBOUNDS = scope["profile"], scope["node"], set(scope["inbounds"])
    live = snapshot() if live is None else live
    for key in live: ids(live[key])
    require(set(ids(live["profiles"])) == set(ids(before["profiles"]))
            and set(ids(live["nodes"])) == set(ids(before["nodes"]))
            and set(ids(live["squads"])) == set(ids(before["squads"])))
    target = one(live["profiles"], PROFILE)
    appended = target["config"] == wanted["config"]
    require(appended or target["config"] == one(before["profiles"], PROFILE)["config"])
    new = [i for i in target["inbounds"] if i.get("tag") == TAG]
    require((len(new) == 1 and set(ids(target["inbounds"])) == OLD_INBOUNDS | {new[0]["uuid"]}) if appended
            else (not new and set(ids(target["inbounds"])) == OLD_INBOUNDS))
    inbound = new[0]["uuid"] if new else None
    if appended:
        intent_matches(directory, "profile", "PATCH", "/config-profiles", {"uuid": PROFILE, "config": wanted["config"]})
    for profile in before["profiles"]:
        current = one(live["profiles"], profile["uuid"])
        require(current["name"] == profile["name"])
        if profile["uuid"] != PROFILE: require(current["config"] == profile["config"] and current["inbounds"] == profile["inbounds"])
        else:
            for old in profile["inbounds"]: require(one(current["inbounds"], old["uuid"]) == old)
    node_bound = False
    for old in before["nodes"]:
        current = one(live["nodes"], old["uuid"])
        require(all(current.get(k) == old.get(k) for k in (
            "name", "address", "port", "countryCode", "tags", "note", "isDisabled",
            "isTrafficTrackingActive", "trafficLimitBytes", "notifyPercent", "consumptionMultiplier")))
        require(current["configProfile"]["activeConfigProfileUuid"] == old["configProfile"]["activeConfigProfileUuid"])
        original = set(ids(old["configProfile"]["activeInbounds"]))
        active = set(ids(current["configProfile"]["activeInbounds"]))
        if old["uuid"] == NODE:
            node_bound = inbound is not None and active == original | {inbound}
            require(active == original or node_bound)
            if node_bound:
                intent_matches(directory, "node", "PATCH", "/nodes", {"uuid": NODE, "configProfile": {
                    "activeConfigProfileUuid": PROFILE, "activeInbounds": ids(old["configProfile"]["activeInbounds"]) + [inbound]}})
        else: require(active == original)
    for old in before["hosts"]: require(host_state(one(live["hosts"], old["uuid"])) == host_state(old))
    added = [h for h in live["hosts"] if h["uuid"] not in set(ids(before["hosts"]))]
    require(len(added) <= 1)
    host = added[0] if added else None
    if host:
        expected = {**wanted["host"], "inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": inbound}}
        require(node_bound and all(host.get(k) == v for k, v in expected.items() if k != "isDisabled")
                and type(host.get("isDisabled")) is bool)
        intent_matches(directory, "host", "POST", "/hosts", expected)
    bound = []
    for old in before["squads"]:
        current = one(live["squads"], old["uuid"])
        original, active = set(ids(old["inbounds"])), set(ids(current["inbounds"]))
        require(current["name"] == old["name"])
        if inbound and old["uuid"] in wanted["squads"] and active == original | {inbound}:
            require(host is not None); bound.append(old["uuid"])
            intent_matches(directory, "squad-" + old["uuid"], "PATCH", "/internal-squads",
                           {"uuid": old["uuid"], "inbounds": ids(old["inbounds"]) + [inbound]})
        else: require(active == original)
    require(not host or host["isDisabled"] or set(bound) == set(wanted["squads"]))
    if host and not host["isDisabled"]:
        intent_matches(directory, "enable", "PATCH", "/hosts", {"uuid": host["uuid"], "isDisabled": False})
    return {"profile_appended": appended, "node_bound": node_bound, "host_uuid": host["uuid"] if host else None,
            "inbound_uuid": inbound, "squads_bound": sorted(bound), "enabled": bool(host and not host["isDisabled"])}, live, wanted


def write_once(directory, name, method, endpoint, payload, done, verify):
    path = directory / ("intent-" + name + ".json")
    intent = {"method": method, "endpoint": endpoint, "body": payload}
    if path.exists():
        require(read(path) == intent and done)
        return
    require(not done)
    save(directory, path.name, intent)
    api(method, endpoint, payload)  # Never automatically retried, even on timeout.
    require(verify(audit(directory)[0]))


def apply_stage(directory, action, *, tls_verified=False):
    state, live, wanted = audit(directory)
    scope = load(directory)[2]
    PROFILE, NODE = scope["profile"], scope["node"]
    inbound, host = state["inbound_uuid"], state["host_uuid"]
    if action == "apply-profile":
        payload = {"uuid": PROFILE, "config": wanted["config"]}
        write_once(directory, "profile", "PATCH", "/config-profiles", payload, state["profile_appended"], lambda s: s["profile_appended"])
    elif action == "bind-node":
        require(state["profile_appended"])
        old = one(load(directory)[0]["nodes"], NODE)
        payload = {"uuid": NODE, "configProfile": {"activeConfigProfileUuid": PROFILE,
                   "activeInbounds": ids(old["configProfile"]["activeInbounds"]) + [inbound]}}
        write_once(directory, "node", "PATCH", "/nodes", payload, state["node_bound"], lambda s: s["node_bound"])
    elif action == "create-host":
        require(state["node_bound"])
        payload = {**wanted["host"], "inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": inbound}}
        write_once(directory, "host", "POST", "/hosts", payload, host is not None, lambda s: s["host_uuid"] is not None)
    elif action == "bind-squads":
        require(host is not None)
        for uid, original in sorted(wanted["squads"].items()):
            payload = {"uuid": uid, "inbounds": original + [inbound]}
            write_once(directory, "squad-" + uid, "PATCH", "/internal-squads", payload,
                       uid in state["squads_bound"], lambda s, uid=uid: uid in s["squads_bound"])
            state = audit(directory)[0]
    elif action == "enable":
        require(tls_verified and host and set(state["squads_bound"]) == set(wanted["squads"]))
        node = one(live["nodes"], NODE)
        require(node.get("isConnected") is True and node.get("isDisabled") is False and not node.get("lastStatusMessage"))
        write_once(directory, "enable", "PATCH", "/hosts", {"uuid": host, "isDisabled": False}, state["enabled"], lambda s: s["enabled"])
    else: raise GuardError("Unknown explicit stage.")
    return audit(directory)[0]


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("action", nargs="?", choices=("prepare", "audit", "apply-profile", "bind-node", "create-host", "bind-squads", "enable"))
        parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
        parser.add_argument("--snapshot", type=Path)
        parser.add_argument("--direct-state", type=Path, default=DIRECT_STATE)
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--tls-verified", action="store_true")
        args = parser.parse_args(argv)
        if args.action is None:
            result = {"plan_only": True, "ip": IP, "origin": ORIGIN, "cdn": DOMAIN,
                      "stages": ["prepare", "apply-profile", "bind-node", "create-host", "bind-squads", "enable --tls-verified"], "network": False}
        elif args.action == "prepare":
            require(args.snapshot is not None)
            result = prepare(read(args.snapshot), args.state, args.direct_state)
        elif args.action == "audit": result = audit(args.state)[0]
        else:
            require(args.confirm)
            result = apply_stage(args.state, args.action, tls_verified=args.tls_verified)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True)); return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0: return 0
        print("CDN stage stopped; raw responses and private configuration suppressed. Audit before any retry.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__": raise SystemExit(main())
