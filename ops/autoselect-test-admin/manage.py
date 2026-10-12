"""An additive, Admin-only Remnawave auto-selection pilot.

Never fetches subscriber configurations or stores user authentication keys.
Existing hosts, templates, profiles, node bindings and squad inbounds are audited,
not modified. The new host is created disabled and gated by a private marker squad.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import certifi

if not __debug__:
    raise RuntimeError("Optimized Python execution is prohibited for this guarded deployment")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.prepare_direct_transports import api as _api

NAME = "SPN_AUTO_TEST_ADMIN_20261009"
REMARK = "🧪 Авто • тест"
ADMIN_ID = 2
SOURCE_HOST = "fdf653d2-d6dd-40c9-9bfb-d9c66aad847f"
SOURCE_TEMPLATE = "1c7a1e0a-9376-4bec-a569-633e73a2fbdb"
PREFIX = "auto-test-"
BALANCER = "AutoTest_Balancer"
CANDIDATES = [
    "52edd62d-7e98-4b04-8cb4-67086a3660ea",  # Estonia NODE29 gRPC
    "cd19762c-685e-4d91-8124-2ddfa33ee59a",  # Estonia NODE29 WS
    "ac8404f0-0cf6-4709-9fad-be2ceb40dddd",  # Finland CHISLO gRPC
    "b89c0f76-4497-4c06-b19f-9932433db3a4",  # Finland CHISLO WS
    "b9087851-e6e5-45ac-9415-9c3b94d84125",  # Finland TEKST gRPC
    "e82e5344-6d52-4c0e-98a6-3b9fa3696a2a",  # Finland TEKST WS
    "5159d30e-0f9f-453d-ab34-f8bef6463b7a",  # Estonia PRESTIZH gRPC
    "5652e383-5030-444e-a72a-7b03b0fa3f20",  # Estonia PRESTIZH WS
]
HOST_COPY_FIELDS = {
    "address", "port", "path", "sni", "host", "alpn", "fingerprint",
    "securityLayer", "xhttpExtraParams", "muxParams", "sockoptParams",
    "finalMask", "pinnedPeerCertSha256", "verifyPeerCertByName", "shuffleHost",
    "mihomoX25519", "mihomoIpVersion", "overrideSniFromAddress", "keepSniBlank",
    "vlessRouteId", "mapper", "inbound",
}
USER_FIELDS = ("id", "username", "status", "expireAt", "trafficLimitBytes",
               "trafficLimitStrategy", "hwidDeviceLimit", "externalSquadUuid")


def api(method, path, body=None):
    # GET is safe to retry. Writes are never retried automatically after an
    # ambiguous transport result; inspect the live object before continuing.
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            return _api(method, path, body)
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(1 + attempt)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def save(directory, name, value):
    path = Path(directory) / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def load(directory, name):
    return json.loads((Path(directory) / name).read_text())


def membership(user):
    return [s["uuid"] for s in user["activeInternalSquads"]]


def visible(host, member_ids, squads, include_hidden=True):
    """Mirror the server's per-squad/inbound EXISTS predicate, not node links."""
    if host.get("isDisabled") or (host.get("isHidden") and not include_hidden):
        return False
    iid = (host.get("inbound") or {}).get("configProfileInboundUuid")
    restriction = host.get("internalSquads") or {"mode": "EXCLUDE", "squads": []}
    allowed = set(restriction.get("squads", []))
    for sid in member_ids:
        s = squads.get(sid)
        if s and iid in {i["uuid"] for i in s.get("inbounds", [])}:
            linked = sid in allowed
            if linked == (restriction.get("mode") == "ALLOW_ONLY"):
                return True
    return False


def build_template(source):
    config = copy.deepcopy(source)
    outbound_map = {o["tag"]: o["protocol"] for o in config["outbounds"]}
    if len(config["outbounds"]) != 2 or outbound_map != {"direct": "freedom", "block": "blackhole"}:
        raise ValueError("Source must contain only the direct and real blackhole outbounds")
    old_tags = {b["tag"] for b in config["routing"]["balancers"]}
    for rule in config["routing"]["rules"]:
        if rule.get("balancerTag") in old_tags:
            rule["balancerTag"] = BALANCER
    config["routing"]["balancers"] = [{
        "tag": BALANCER, "selector": [PREFIX],
        "strategy": {"type": "leastPing"}, "fallbackTag": "block",
    }]
    # Explicit direct-domain rules remain as in the current subscription. Unknown
    # or failed balanced routes cannot accidentally default to the direct handler.
    config["outbounds"] = sorted(config["outbounds"], key=lambda o: o["tag"] != "block")
    config.pop("observatory", None)
    config["burstObservatory"] = {
        "subjectSelector": [PREFIX],
        "pingConfig": {
            "destination": "https://www.gstatic.com/generate_204",
            "connectivity": "", "interval": "15s", "sampling": 1,
            "timeout": "3s", "httpMethod": "GET",
        },
    }
    config["remnawave"] = {
        "addVirtualHostAsOutbound": False,
        "injectHosts": [{"selector": {"type": "uuids", "values": list(CANDIDATES)},
                         "tagPrefix": PREFIX, "selectFrom": "ALL"}],
    }
    return config


def validation_fixture(template):
    """Compile the balancer with keyless placeholders; never use subscriber keys."""
    config = copy.deepcopy(template)
    config.pop("remnawave")
    config["inbounds"] = []
    config["outbounds"].extend(
        {"tag": PREFIX + str(i), "protocol": "freedom"} for i in range(len(CANDIDATES)))
    return config


def topology(nodes):
    return {n["uuid"]: {
        "address": n["address"], "port": n["port"],
        "profile": n["configProfile"]["activeConfigProfileUuid"],
        "inbounds": sorted(i["uuid"] for i in n["configProfile"]["activeInbounds"]),
    } for n in nodes}


def squad_snapshot(squads):
    return {s["uuid"]: {"name": s["name"], "inbounds": sorted(i["uuid"] for i in s["inbounds"])}
            for s in squads}


def profile_snapshot(profiles):
    # Nested DTO info/counts can change when the additive marker refers to an
    # inbound. The profile's actual name/configuration must remain identical.
    return {p["uuid"]: digest({"name": p["name"], "config": p["config"]}) for p in profiles}


def template_hashes():
    templates = api("GET", "/subscription-templates")["templates"]
    def one(t):
        full = api("GET", "/subscription-templates/" + t["uuid"])
        return t["uuid"], digest(full)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(one, templates))


def prepare():
    paths = ["/hosts", "/nodes", "/internal-squads", "/users/by-username/Admin",
             "/config-profiles", "/subscription-templates/" + SOURCE_TEMPLATE]
    with ThreadPoolExecutor(max_workers=4) as pool:
        data = dict(zip(paths, pool.map(lambda p: api("GET", p), paths)))
    hosts, nodes = data["/hosts"], data["/nodes"]
    squads = data["/internal-squads"]["internalSquads"]
    user = data["/users/by-username/Admin"]
    assert user["id"] == ADMIN_ID and user["username"] == "Admin"
    assert not any(s["name"] == NAME for s in squads), "Pilot squad already exists"
    assert not any(h["remark"] == REMARK for h in hosts), "Pilot host already exists"
    ts = api("GET", "/subscription-templates")["templates"]
    assert not any(t["name"] == NAME for t in ts), "Pilot template already exists"
    by_host = {h["uuid"]: h for h in hosts}
    by_squad = {s["uuid"]: s for s in squads}
    assert visible(by_host[SOURCE_HOST], membership(user), by_squad)
    candidates = []
    for uid in CANDIDATES:
        host = by_host[uid]
        assert visible(host, membership(user), by_squad), "Admin cannot access a candidate"
        iid = host["inbound"]["configProfileInboundUuid"]
        assigned = [n for n in nodes if not n["isDisabled"] and n["isConnected"]
                    and any(i["uuid"] == iid for i in n["configProfile"]["activeInbounds"])]
        assert assigned, "Candidate has no connected enabled node"
        ins = [i for n in assigned for i in n["configProfile"]["activeInbounds"] if i["uuid"] == iid]
        assert all(i["type"] == "vless" and i["network"] in ("grpc", "ws") for i in ins)
        candidates.append({"uuid": uid, "remark": host["remark"],
                           "address": host["address"], "port": host["port"]})
    directory = Path(tempfile.mkdtemp(prefix="wayspn-auto-test-admin-"))
    os.chmod(directory, 0o700)
    source = data["/subscription-templates/" + SOURCE_TEMPLATE]["templateJson"]
    template = build_template(source)
    before = {
        "pilotTemplateHash": digest(template),
        "hostHashes": {h["uuid"]: digest(h) for h in hosts},
        "templates": template_hashes(), "nodes": topology(nodes),
        "profiles": profile_snapshot(data["/config-profiles"]["configProfiles"]),
        "squads": squad_snapshot(squads), "admin": {k: user.get(k) for k in USER_FIELDS},
        "adminSquads": membership(user),
        "adminVisibleHosts": sorted(h["uuid"] for h in hosts if visible(h, membership(user), by_squad)),
    }
    save(directory, "before.json", before)
    save(directory, "source-host.json", by_host[SOURCE_HOST])
    save(directory, "template.json", template)
    save(directory, "validation-keyless.json", validation_fixture(template))
    save(directory, "candidates.json", candidates)
    save(directory, "created.json", {})
    print(json.dumps({"stateDirectory": str(directory), "candidates": candidates,
                      "panelWrites": False}, ensure_ascii=False, indent=2))


def add_admin_to_marker(sid):
    """The selected-user action is additive; HTTP 202 has no JSON body."""
    base = os.environ["REMNAWAVE_BASE_URL"].rstrip("/")
    req = urllib.request.Request(
        base + f"/internal-squads/{sid}/bulk-actions/add-many-users",
        data=json.dumps({"userIds": [ADMIN_ID]}).encode(), method="POST",
        headers={"Authorization": "Bearer " + os.environ["REMNAWAVE_API_TOKEN"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as response:
        assert response.status in (200, 201, 202, 204)
        response.read()


def require_validation(directory, template):
    assert digest(template) == load(directory, "before.json").get("pilotTemplateHash"), "Prepared template changed"
    assert template["remnawave"] == {
        "addVirtualHostAsOutbound": False,
        "injectHosts": [{"selector": {"type": "uuids", "values": list(CANDIDATES)},
                         "tagPrefix": PREFIX, "selectFrom": "ALL"}],
    }, "Unexpected pilot injection scope"
    report = load(directory, "validation-result.json")
    fixture_path = Path(directory) / "validation-keyless.json"
    assert load(directory, "validation-keyless.json") == validation_fixture(template), "Validation fixture is stale"
    assert report.get("fixtureSha256") == hashlib.sha256(fixture_path.read_bytes()).hexdigest(), "Validation report does not match fixture"
    assert report.get("ok") is True and report.get("compile") == "passed", "Validate Xray before panel writes"
    assert report.get("cleanup", {}).get("terminated") is True, "Validation process cleanup is incomplete"
    assert report.get("initialSelection") == "mock-fast" and report.get("failoverSelection") == "mock-slow", "Native selection/failover not verified"
    assert report.get("fallbackBlock") == "verified: failed requests with no mock connection attempts", "Fail-closed fallback not verified"
    assert report.get("realVpnEndToEnd") is False and report.get("subscriberConfigurationsRead") is False


def apply(directory):
    before, created = load(directory, "before.json"), load(directory, "created.json")
    template = load(directory, "template.json")
    require_validation(directory, template)
    hosts = api("GET", "/hosts")
    assert all(digest(h) == before["hostHashes"][h["uuid"]] for h in hosts if h["uuid"] in before["hostHashes"])
    user = api("GET", "/users/by-username/Admin")
    assert {k: user.get(k) for k in USER_FIELDS} == before["admin"]
    assert set(before["adminSquads"]) <= set(membership(user)), "Admin memberships changed"
    source = load(directory, "source-host.json")
    if "template" not in created:
        assert not any(t["name"] == NAME for t in api("GET", "/subscription-templates")["templates"])
        t = api("POST", "/subscription-templates", {"name": NAME, "templateType": "XRAY_JSON"})
        created["template"] = t["uuid"]
        save(directory, "created.json", created)
    tid = created["template"]
    assert tid not in before["templates"], "Cannot overwrite an existing template"
    owned_template = api("GET", "/subscription-templates/" + tid)
    assert owned_template["name"] == NAME and owned_template["templateType"] == "XRAY_JSON", "Template ownership mismatch"
    if owned_template["templateJson"] != template:
        api("PATCH", "/subscription-templates", {"uuid": tid, "templateJson": template})
    assert api("GET", "/subscription-templates/" + tid)["templateJson"] == template
    if "squad" not in created:
        squads = api("GET", "/internal-squads")["internalSquads"]
        assert not any(s["name"] == NAME for s in squads)
        s = api("POST", "/internal-squads", {
            "name": NAME, "inbounds": [source["inbound"]["configProfileInboundUuid"]],
        })
        created["squad"] = s["uuid"]
        save(directory, "created.json", created)
    sid = created["squad"]
    assert sid not in before["squads"], "Cannot use an existing squad"
    squads = api("GET", "/internal-squads")["internalSquads"]
    by_squad = {s["uuid"]: s for s in squads}
    marker = by_squad[sid]
    assert marker["name"] == NAME
    assert {i["uuid"] for i in marker["inbounds"]} == {source["inbound"]["configProfileInboundUuid"]}
    assert marker["info"]["membersCount"] == int(sid in membership(user)), "Marker has a non-Admin member"
    future_members = membership(user) + ([sid] if sid not in membership(user) else [])
    future_visible = sorted(h["uuid"] for h in hosts if h["uuid"] in before["hostHashes"]
                            and visible(h, future_members, by_squad))
    assert future_visible == before["adminVisibleHosts"], "Marker would change existing host visibility"
    if "host" not in created:
        wanted = {k: copy.deepcopy(v) for k, v in source.items() if k in HOST_COPY_FIELDS}
        wanted.update(remark=REMARK, isDisabled=True, isHidden=False, nodes=[],
                      tags=["AUTO_TEST"], xrayJsonTemplateUuid=tid,
                      excludeFromSubscriptionTypes=["MIHOMO", "STASH", "CLASH", "SINGBOX"],
                      internalSquads={"mode": "ALLOW_ONLY", "squads": [sid]})
        assert not any(h["remark"] == REMARK for h in hosts)
        h = api("POST", "/hosts", wanted)
        created["host"] = h["uuid"]
        save(directory, "created.json", created)
    if sid not in membership(user):
        add_admin_to_marker(sid)
    for _ in range(30):
        user = api("GET", "/users/by-username/Admin")
        if sid in membership(user):
            break
        time.sleep(1)
    assert sid in membership(user), "Additive membership action did not finish"
    assert set(membership(user)) == set(before["adminSquads"]) | {sid}
    audit(directory, allow_disabled=True)
    api("PATCH", "/hosts", {"uuid": created["host"], "isDisabled": False})
    audit(directory)


def audit(directory, allow_disabled=False):
    before, created = load(directory, "before.json"), load(directory, "created.json")
    hosts = api("GET", "/hosts")
    by_host = {h["uuid"]: h for h in hosts}
    for uid, old_hash in before["hostHashes"].items():
        assert uid in by_host and digest(by_host[uid]) == old_hash, "Existing host changed"
    assert set(by_host) == set(before["hostHashes"]) | {created["host"]}
    template = api("GET", "/subscription-templates/" + created["template"])
    assert template["templateJson"] == load(directory, "template.json")
    now_templates = template_hashes()
    assert all(now_templates.get(uid) == d for uid, d in before["templates"].items()), "Existing template changed"
    assert set(now_templates) == set(before["templates"]) | {created["template"]}
    profiles = api("GET", "/config-profiles")["configProfiles"]
    assert profile_snapshot(profiles) == before["profiles"], "Profile changed"
    assert topology(api("GET", "/nodes")) == before["nodes"], "Node binding changed"
    squads = api("GET", "/internal-squads")["internalSquads"]
    current_squads = squad_snapshot(squads)
    assert all(current_squads.get(uid) == v for uid, v in before["squads"].items()), "Existing squad changed"
    assert set(current_squads) == set(before["squads"]) | {created["squad"]}
    marker = next(s for s in squads if s["uuid"] == created["squad"])
    assert marker["info"]["membersCount"] == 1, "Private squad must contain only Admin"
    user = api("GET", "/users/by-username/Admin")
    assert user["id"] == ADMIN_ID and user["username"] == "Admin"
    assert {k: user.get(k) for k in USER_FIELDS} == before["admin"]
    assert set(membership(user)) == set(before["adminSquads"]) | {created["squad"]}
    host = by_host[created["host"]]
    source = load(directory, "source-host.json")
    assert host["remark"] == REMARK and host["xrayJsonTemplateUuid"] == created["template"]
    assert host["inbound"] == source["inbound"], "Pilot anchor inbound changed"
    assert host.get("nodes") == [] and host.get("isHidden") is False
    assert set(host.get("excludeFromSubscriptionTypes", [])) == {"MIHOMO", "STASH", "CLASH", "SINGBOX"}
    assert host["internalSquads"] == {"mode": "ALLOW_ONLY", "squads": [created["squad"]]}
    assert allow_disabled or not host["isDisabled"]
    by_squad = {s["uuid"]: s for s in squads}
    assert not visible(host, before["adminSquads"], by_squad), "Pilot leaks through existing public squads"
    assert visible({**host, "isDisabled": False}, membership(user), by_squad)
    assert sorted(h["uuid"] for h in hosts if h["uuid"] in before["hostHashes"]
                  and visible(h, membership(user), by_squad)) == before["adminVisibleHosts"]
    result = {"host": host["remark"], "hostUuid": host["uuid"], "enabled": not host["isDisabled"],
              "adminOnly": True, "markerMembers": 1, "candidateCount": len(CANDIDATES),
              "existingHostsTemplatesProfilesAndNodeBindingsUnchanged": True,
              "existingAdminHostVisibilityUnchanged": True,
              "authenticatedSubscriberConnectionsTested": False}
    save(directory, "audit-result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def disable(directory):
    """Safe rollback: hide the owned pilot; retain artifacts and memberships."""
    created = load(directory, "created.json")
    h = next(h for h in api("GET", "/hosts") if h["uuid"] == created["host"])
    assert h["remark"] == REMARK and h["xrayJsonTemplateUuid"] == created["template"]
    assert h["internalSquads"] == {"mode": "ALLOW_ONLY", "squads": [created["squad"]]}
    api("PATCH", "/hosts", {"uuid": h["uuid"], "isDisabled": True})
    print("Only the owned pilot host was disabled; existing objects untouched.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    # Populate dotenv references once before any concurrent GET operations.
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "apply", "audit", "disable"))
    parser.add_argument("directory", nargs="?")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    else:
        assert args.directory
        {"apply": apply, "audit": audit, "disable": disable}[args.action](args.directory)
