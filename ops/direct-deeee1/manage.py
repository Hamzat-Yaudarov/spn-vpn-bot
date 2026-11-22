"""Scoped DEEEE1 direct gRPC/WS deployment; reuse one approved offline node.

No subscriber endpoints, automatic mutation retries or global host-order audit.
The shared request/private-state helpers do nothing until an action is invoked.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys

_spec = importlib.util.spec_from_file_location(
    "_direct_deeee1_shared", Path(__file__).resolve().parents[1] / "direct-pol" / "manage.py"
)
shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shared)

ADDRESS = shared.ADDRESS = "64.188.83.197"
DOMAIN = shared.DOMAIN = "deeee1.wayspn.com"
PROFILE = shared.PROFILE = "DIRECT_DEEEE1_83_197"
NODE = shared.NODE = "node-direct-deeee1-83.197"
STATE = shared.STATE = Path("/private/tmp/wayspn-direct-deeee1-20261022")
EXISTING_NODE = "f87b14cf-438a-4c61-acc1-7522491c05ae"
OLD_NODE_NAME = "node-de1-xhttp-timeweb"
OLD_PROFILE = "6290e311-62cf-4a43-8d7d-e55561790a96"
SOURCE_HOSTS = shared.SOURCE_HOSTS
GRPC_TAG, WS_TAG = "ordinary-grpc-reality-deeee1", "ordinary-ws-deeee1"
require, api, save, load = shared.require, shared.api, shared.save, shared.load
snapshot, sources, check, obtain = shared.snapshot, shared.sources, shared.check, shared.obtain


def one(rows, uid, label):
    found = [row for row in rows if row.get("uuid") == uid]
    require(len(found) == 1, "Missing or ambiguous " + label)
    return found[0]


def original_node(live):
    node = one(live["nodes"], EXISTING_NODE, "approved existing node")
    require(len([n for n in live["nodes"] if n.get("address") == ADDRESS]) == 1,
            "Unexpected duplicate node address")
    require(node["name"] == OLD_NODE_NAME and node["address"] == ADDRESS and node["port"] == 2222
            and node["configProfile"]["activeConfigProfileUuid"] == OLD_PROFILE,
            "Existing node no longer matches the approved old binding")
    require(node.get("isConnected") is False and not node.get("isConnecting"), "Existing node must be offline")
    return node


def inspect_live():
    live = snapshot()
    hosts, profile, inbounds = sources(live)
    node = original_node(live)
    save("baseline.json", live)
    save("node-before.json", node)
    save("source-profile.json", profile)
    save("source-hosts.json", hosts)
    print(json.dumps({"existingNode": node["uuid"], "address": ADDRESS,
                      "oldProfile": OLD_PROFILE, "sourceHostIds": list(SOURCE_HOSTS),
                      "sourceNetworks": [i["streamSettings"]["network"] for i in inbounds]}))


def prepare():
    baseline = load("baseline.json")
    hosts, source, selected = sources(baseline)
    node = original_node(baseline)
    save("node-before.json", node)
    require(not any(p["name"] == PROFILE for p in baseline["profiles"]), "New profile already existed")
    require(not any(h["address"] in {ADDRESS, DOMAIN} for h in baseline["hosts"]), "New host address already existed")
    if (STATE / "plan.json").exists():
        require(load("plan.json")["existingNode"] == EXISTING_NODE, "Wrong saved plan")
        print("Existing private plan preserved; no new keys generated.")
        return
    config = copy.deepcopy(source["config"])
    require(all(o.get("protocol") in {"freedom", "blackhole", "dns"} for o in config.get("outbounds", [])),
            "Source must have direct egress only")
    key = shared.X25519PrivateKey.generate()
    b64 = lambda value: shared.base64.urlsafe_b64encode(value).decode().rstrip("=")
    private_key = b64(key.private_bytes(shared.serialization.Encoding.Raw,
                                      shared.serialization.PrivateFormat.Raw, shared.serialization.NoEncryption()))
    public_key = b64(key.public_key().public_bytes(shared.serialization.Encoding.Raw, shared.serialization.PublicFormat.Raw))
    short_id, mapping, inbounds = shared.secrets.token_hex(8), {}, []
    for old in selected:
        inbound = copy.deepcopy(old)
        stream = inbound["streamSettings"]
        kind = stream["network"]
        inbound["tag"] = GRPC_TAG if kind == "grpc" else WS_TAG
        mapping[old["tag"]] = inbound["tag"]
        if kind == "grpc":
            require(inbound["port"] == 2083 and stream["security"] == "reality"
                    and stream["grpcSettings"]["serviceName"] == "grpc", "Unexpected source gRPC")
            stream["realitySettings"].update(privateKey=private_key, shortIds=[short_id], serverNames=[DOMAIN], target="127.0.0.1:443")
        else:
            require(kind == "ws" and inbound["port"] == 10088 and inbound["listen"] == "127.0.0.1"
                    and stream["security"] == "none" and stream["wsSettings"]["path"] == "/direct/ws/", "Unexpected source WS")
        inbounds.append(inbound)
    config["inbounds"] = inbounds
    rules = []
    for rule in config.get("routing", {}).get("rules", []):
        rule = copy.deepcopy(rule)
        if rule.get("inboundTag"):
            rule["inboundTag"] = [mapping[tag] for tag in rule["inboundTag"] if tag in mapping]
            if not rule["inboundTag"]:
                continue
        rules.append(rule)
    if "routing" in config:
        config["routing"]["rules"] = rules
    save("profile.create.json", {"name": PROFILE, "config": config})
    save("public.json", {"domain": DOMAIN, "address": ADDRESS, "panelIp": "144.31.170.147",
                         "publicKey": public_key, "shortId": short_id})
    secret = api("GET", "/keygen")["secretKey"]
    require(isinstance(secret, str) and len(secret) > 20 and not any(c.isspace() for c in secret), "Invalid node key")
    save("node.env", "NODE_PORT=2222\nSECRET_KEY=" + secret + "\n")
    metadata = {i["uuid"]: i for i in source["inbounds"]}
    fields = {"address", "port", "path", "sni", "host", "alpn", "fingerprint", "securityLayer", "xhttpExtraParams",
              "muxParams", "sockoptParams", "finalMask", "serverDescription", "pinnedPeerCertSha256", "verifyPeerCertByName",
              "shuffleHost", "mihomoX25519", "mihomoIpVersion", "isHidden", "overrideSniFromAddress", "keepSniBlank",
              "vlessRouteId", "xrayJsonTemplateUuid", "excludeFromSubscriptionTypes", "mapper"}
    planned = []
    for old in hosts:
        tag = mapping[metadata[old["inbound"]["configProfileInboundUuid"]]["tag"]]
        grpc = tag == GRPC_TAG
        body = {k: copy.deepcopy(v) for k, v in old.items() if k in fields}
        body.update(remark="🇩🇪 Германия • " + ("gRPC" if grpc else "WS"),
                    address=ADDRESS if grpc else DOMAIN, sni=DOMAIN, host=None if grpc else DOMAIN,
                    isDisabled=True, tags=["DEEEE1_83_197", "DIRECT"], internalSquads=copy.deepcopy(old["internalSquads"]))
        planned.append({"tag": tag, "sourceInbound": old["inbound"]["configProfileInboundUuid"], "body": body})
    save("hosts.plan.json", planned)
    save("plan.json", {"format": 1, "address": ADDRESS, "domain": DOMAIN, "profile": PROFILE, "existingNode": EXISTING_NODE,
                       "profileHash": shared.digest(config), "sourceHostIds": list(SOURCE_HOSTS)})
    print("Prepared two direct transports and new keys; existing node preserved; no panel writes.")


def rebind_node(profile):
    nodes = api("GET", "/nodes")
    current = one(nodes, EXISTING_NODE, "approved existing node")
    require(len([n for n in nodes if n.get("address") == ADDRESS]) == 1, "Duplicate node address")
    payload = {"uuid": EXISTING_NODE, "name": NODE, "address": ADDRESS, "port": 2222, "countryCode": "DE",
               "tags": ["DEEEE1_83_197", "DIRECT"], "note": DOMAIN + ": direct gRPC REALITY + WS TLS",
               "configProfile": {"activeConfigProfileUuid": profile["uuid"],
                                 "activeInbounds": [i["uuid"] for i in profile["inbounds"]]}}
    intent = STATE / "intent-node.json"
    if current["configProfile"]["activeConfigProfileUuid"] == profile["uuid"]:
        require(intent.exists() and load(intent.name) == payload, "Rebound node lacks exact durable intent")
        check(current, payload)
    else:
        before = load("node-before.json")
        original_node({"nodes": nodes})
        check(current, {k: before[k] for k in ("uuid", "name", "address", "port")})
        require(current["configProfile"] == before["configProfile"], "Old node binding changed")
        require(not intent.exists(), "Previous node PATCH unresolved; inspect fresh state before retry")
        save(intent.name, payload)
        current = api("PATCH", "/nodes", payload)
        check(current, payload)
        current = one(api("GET", "/nodes"), EXISTING_NODE, "rebound node")
        check(current, payload)
    if (STATE / "created-node.json").exists():
        require(load("created-node.json")["uuid"] == EXISTING_NODE, "Saved node identity differs")
    else:
        save("created-node.json", current)
    return current


def create():
    plan = load("plan.json")
    require(plan["address"] == ADDRESS and plan["existingNode"] == EXISTING_NODE, "Wrong saved plan")
    profile = obtain("profile", "profile", lambda p: p["name"] == PROFILE, load("profile.create.json"))
    require(len(profile["inbounds"]) == 2 and {i["tag"] for i in profile["inbounds"]} == {GRPC_TAG, WS_TAG}, "Wrong new inbounds")
    node = rebind_node(profile)
    ids = {i["tag"]: i["uuid"] for i in profile["inbounds"]}
    for item in load("hosts.plan.json"):
        payload = copy.deepcopy(item["body"])
        payload.update(nodes=[node["uuid"]], inbound={"configProfileUuid": profile["uuid"], "configProfileInboundUuid": ids[item["tag"]]})
        kind = "grpc" if item["tag"] == GRPC_TAG else "ws"
        host = obtain(kind, kind, lambda h: h.get("inbound", {}).get("configProfileInboundUuid") == ids[item["tag"]], payload)
        print("Created disabled:", host["remark"], host["uuid"])
    print("Reused only the approved node; old Timeweb profile/host were not mutated.")


def owned(live, *, ready=False):
    profile = one(live["profiles"], load("created-profile.json")["uuid"], "new profile")
    node = one(live["nodes"], EXISTING_NODE, "reused node")
    check(profile, load("intent-profile.json"))
    check(node, load("intent-node.json"))
    require(len([n for n in live["nodes"] if n.get("address") == ADDRESS]) == 1, "Duplicate node address")
    require(len(profile["inbounds"]) == 2 and {i["tag"] for i in profile["inbounds"]} == {GRPC_TAG, WS_TAG}, "Wrong owned inbounds")
    require(sorted((i["uuid"], i["tag"]) for i in profile["inbounds"]) == sorted((i["uuid"], i["tag"]) for i in load("created-profile.json")["inbounds"]), "Inbound identities changed")
    host_ids = set()
    for kind in ("grpc", "ws"):
        host = one(live["hosts"], load("created-" + kind + ".json")["uuid"], "new host")
        host_ids.add(host["uuid"])
        require(type(host.get("isDisabled")) is bool, "Invalid host state")
        if host["isDisabled"] is False:
            require(load("enable-" + kind + ".json") == {"uuid": host["uuid"], "isDisabled": False}, "Missing exact enable intent")
        check(host, {**load("intent-" + kind + ".json"), "isDisabled": host["isDisabled"]})
    require({h["uuid"] for h in live["hosts"] if h.get("inbound", {}).get("configProfileUuid") == profile["uuid"]} == host_ids,
            "Unexpected host bound to the new profile")
    if ready:
        require(node.get("isConnected") is True and node.get("isDisabled") is False and not node.get("lastStatusMessage"), "Node not ready")
    return profile, node


def inbound_ids(profile):
    old = {item["sourceInbound"] for item in load("hosts.plan.json")}
    new = {i["uuid"] for i in profile["inbounds"]}
    require(len(old) == len(new) == 2 and not old.intersection(new), "Invalid source/target inbound pair")
    return old, new


def audit_live(live=None):
    live = snapshot() if live is None else live
    profile, node = owned(live)
    source_ids, new_ids = inbound_ids(profile)
    # Scoped checks only: global viewPosition and unrelated object edits are not
    # a deployment lock. The old Timeweb profile and host bindings stay intact.
    baseline = load("baseline.json")
    old_profile = one(baseline["profiles"], OLD_PROFILE, "old Timeweb profile")
    require(one(live["profiles"], OLD_PROFILE, "old Timeweb profile")["config"] == old_profile["config"], "Old Timeweb config changed")
    for old in baseline["hosts"]:
        if old.get("inbound", {}).get("configProfileUuid") == OLD_PROFILE:
            actual = one(live["hosts"], old["uuid"], "old Timeweb host")
            check(actual, {k: old[k] for k in ("inbound", "nodes", "address", "port")})
    for squad in live["squads"]:
        current = {i["uuid"] for i in squad["inbounds"]}
        if not current.intersection(new_ids):
            continue
        before = load("squad-before-" + squad["uuid"] + ".json")
        previous = [i["uuid"] for i in before["inbounds"]]
        require(before["uuid"] == squad["uuid"] and source_ids.issubset(previous), "Unapproved target squad")
        wanted = previous + sorted(new_ids - set(previous))
        require(load("squad-intent-" + squad["uuid"] + ".json") == {"uuid": squad["uuid"], "inbounds": wanted}
                and sorted(current) == sorted(wanted), "Squad differs from exact append intent")
    return live, profile, node


def bind_squads():
    live, profile, _ = audit_live()
    owned(live, ready=True)
    source_ids, new_ids = inbound_ids(profile)
    for squad in live["squads"]:
        current = [i["uuid"] for i in squad["inbounds"]]
        if not source_ids.issubset(current):
            continue
        name = "squad-intent-" + squad["uuid"] + ".json"
        if (STATE / name).exists():
            payload = load(name)
            before = load("squad-before-" + squad["uuid"] + ".json")
            previous = [i["uuid"] for i in before["inbounds"]]
            require(payload == {"uuid": squad["uuid"], "inbounds": previous + sorted(new_ids - set(previous))}
                    and sorted(current) in (sorted(previous), sorted(payload["inbounds"])), "Saved append intent changed")
        else:
            require(not new_ids.intersection(current), "Unrecorded new inbound membership")
            save("squad-before-" + squad["uuid"] + ".json", squad)
            payload = {"uuid": squad["uuid"], "inbounds": current + sorted(new_ids - set(current))}
            save(name, payload)
        if sorted(current) != sorted(payload["inbounds"]):
            result = api("PATCH", "/internal-squads", payload)
            require(result["uuid"] == payload["uuid"] and sorted(i["uuid"] for i in result["inbounds"]) == sorted(payload["inbounds"]), "Squad PATCH differs")
    final, _, _ = audit_live()
    require(all(new_ids.issubset(i["uuid"] for i in s["inbounds"]) for s in final["squads"]
                if source_ids.issubset(i["uuid"] for i in s["inbounds"])), "Appended membership did not persist")


def activate():
    bind_squads()
    live, profile, _ = audit_live()
    owned(live, ready=True)
    for kind in ("grpc", "ws"):
        uid = load("created-" + kind + ".json")["uuid"]
        host = one(live["hosts"], uid, "new host")
        if host["isDisabled"]:
            payload = {"uuid": uid, "isDisabled": False}
            save("enable-" + kind + ".json", payload)
            result = api("PATCH", "/hosts", payload)
            require(result["uuid"] == uid and result["isDisabled"] is False, "Host PATCH differs")
    final, _, _ = audit_live()
    require(all(one(final["hosts"], load("created-" + kind + ".json")["uuid"], "new host")["isDisabled"] is False
                for kind in ("grpc", "ws")), "Enabled host state did not persist")
    print("Enabled only two new hosts; new inbounds appended only to squads with BOTH source inbounds.")


def status():
    live, profile, node = audit_live()
    ids = {load("created-" + kind + ".json")["uuid"] for kind in ("grpc", "ws")}
    print(json.dumps({"node": {k: node.get(k) for k in ("uuid", "name", "address", "isConnected", "isDisabled", "lastStatusMessage", "versions")},
                      "hosts": [{k: h.get(k) for k in ("uuid", "remark", "address", "port", "isDisabled")} for h in live["hosts"] if h["uuid"] in ids],
                      "profile": profile["uuid"], "globalHostOrderAudited": False}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "prepare", "create", "status", "activate", "audit"])
    args = parser.parse_args()
    {"inspect": inspect_live, "prepare": prepare, "create": create,
     "status": status, "activate": activate, "audit": status}[args.action]()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"ok": False, "error": type(error).__name__,
                          "message": str(error) if isinstance(error, (ValueError, RuntimeError)) else "Scoped operation failed; details suppressed"}))
        sys.exit(1)
