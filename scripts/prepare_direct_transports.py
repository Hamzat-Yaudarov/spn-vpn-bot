"""Prepare and, after validation, install direct transports on two Remnawave profiles.

The generated state contains REALITY private keys. Keep its directory private.
Usage: python3 scripts/prepare_direct_transports.py prepare
       python3 scripts/prepare_direct_transports.py apply-profile /private/tmp/wayspn-direct-.../state.json
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import secrets
import ssl
import tempfile
import urllib.request
from pathlib import Path

import certifi
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from dotenv import load_dotenv


TARGETS = {
    "yandex": {
        "profile_uuid": "59aba40b-9ed3-4eec-b6af-b34a39da88d1",
        "node_uuid": "f0df11a1-006a-4c5f-a263-82ba69161356",
        "domain": "de11.wayspn.com",
        "suffix": "de11",
    },
    "vk": {
        "profile_uuid": "4284c20d-a682-4737-99db-f90216394459",
        "node_uuid": "80436f72-eed5-4d0c-9064-ac60b9dfd002",
        "domain": "dryft.su",
        "suffix": "dryft",
    },
}


def api(method: str, path: str, body: dict | None = None):
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    base = os.environ["REMNAWAVE_BASE_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['REMNAWAVE_API_TOKEN']}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(
        req, timeout=30, context=ssl.create_default_context(cafile=certifi.where())
    ) as response:
        return json.load(response)["response"]


def private_write(path: Path, data: object):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def make_inbounds(info: dict):
    suffix = info["suffix"]
    key = X25519PrivateKey.generate()
    private = base64.urlsafe_b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode().rstrip("=")
    public = base64.urlsafe_b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode().rstrip("=")
    short_id = secrets.token_hex(8)
    common = {
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
    }
    inbounds = [
        {
            **copy.deepcopy(common),
            "tag": f"direct-xhttp-{suffix}",
            "listen": "127.0.0.1",
            "port": 10086,
            "streamSettings": {
                "network": "xhttp",
                "security": "none",
                "xhttpSettings": {"path": "/direct/x/", "mode": "packet-up"},
            },
        },
        {
            **copy.deepcopy(common),
            "tag": f"direct-grpc-{suffix}",
            "listen": "127.0.0.1",
            "port": 10087,
            "streamSettings": {
                "network": "grpc",
                "security": "none",
                "grpcSettings": {"serviceName": "direct-grpc"},
            },
        },
        {
            **copy.deepcopy(common),
            "tag": f"direct-tcp-reality-{suffix}",
            "listen": "0.0.0.0",
            "port": 8443,
            "streamSettings": {
                "network": "raw",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "target": f"{info['domain']}:443",
                    "xver": 0,
                    "serverNames": [info["domain"]],
                    "privateKey": private,
                    "shortIds": [short_id],
                },
            },
        },
    ]
    return inbounds, {"public_key": public, "short_id": short_id}


def prepare():
    directory = Path(tempfile.mkdtemp(prefix="wayspn-direct-", dir="/private/tmp"))
    os.chmod(directory, 0o700)
    profiles = api("GET", "/config-profiles")["configProfiles"]
    nodes = api("GET", "/nodes")
    hosts = api("GET", "/hosts")
    squads = api("GET", "/internal-squads")["internalSquads"]
    state = {"targets": {}}
    for name, info in TARGETS.items():
        profile = next(p for p in profiles if p["uuid"] == info["profile_uuid"])
        node = next(n for n in nodes if n["uuid"] == info["node_uuid"])
        existing = profile["config"]["inbounds"]
        assert len(existing) == 1, f"{name}: unexpected current inbound count"
        assert len(node["configProfile"]["activeInbounds"]) == 1
        assert node["configProfile"]["activeInbounds"][0]["tag"] == existing[0]["tag"]
        additions, public = make_inbounds(info)
        candidate = copy.deepcopy(profile["config"])
        candidate["inbounds"].extend(additions)
        private_write(directory / f"{name}-profile-before.json", profile)
        private_write(directory / f"{name}-node-before.json", node)
        private_write(directory / f"{name}-candidate.json", candidate)
        state["targets"][name] = {
            **info,
            **public,
            "original_inbound_tag": existing[0]["tag"],
            "candidate_file": f"{name}-candidate.json",
            "added_tags": [item["tag"] for item in additions],
        }
    private_write(directory / "hosts-before.json", hosts)
    private_write(directory / "squads-before.json", squads)
    private_write(directory / "state.json", state)
    print(f"Prepared: {directory}")
    for name, info in state["targets"].items():
        print(f"{name}: existing {info['original_inbound_tag']}; add {', '.join(info['added_tags'])}")
    print("No panel or VPS changes made. Candidate files contain private keys.")


def apply_profile(state_path: Path):
    state = json.loads(state_path.read_text())
    profiles = api("GET", "/config-profiles")["configProfiles"]
    for name, info in state["targets"].items():
        candidate = json.loads((state_path.parent / info["candidate_file"]).read_text())
        live = next(p for p in profiles if p["uuid"] == info["profile_uuid"])
        if [item["tag"] for item in live["config"]["inbounds"]] == [
            item["tag"] for item in candidate["inbounds"]
        ]:
            print(f"{name}: profile already contains prepared tags")
            continue
        before = json.loads((state_path.parent / f"{name}-profile-before.json").read_text())
        assert live["config"] == before["config"], f"{name}: profile changed since backup"
        updated = api("PATCH", "/config-profiles", {"uuid": info["profile_uuid"], "config": candidate})
        actual_tags = [item["tag"] for item in updated["config"]["inbounds"]]
        assert actual_tags == [item["tag"] for item in candidate["inbounds"]]
        print(f"{name}: profile updated, {len(actual_tags)} inbounds (new still inactive)")


def activate_node(state_path: Path, target: str):
    state = json.loads(state_path.read_text())
    info = state["targets"][target]
    profiles = api("GET", "/config-profiles")["configProfiles"]
    nodes = api("GET", "/nodes")
    profile = next(p for p in profiles if p["uuid"] == info["profile_uuid"])
    node = next(n for n in nodes if n["uuid"] == info["node_uuid"])
    candidate = json.loads((state_path.parent / info["candidate_file"]).read_text())
    assert profile["config"] == candidate, f"{target}: profile changed since preparation"
    by_tag = {item["tag"]: item["uuid"] for item in profile["inbounds"]}
    selected = [by_tag[tag] for tag in [info["original_inbound_tag"], *info["added_tags"]]]
    active = [item["uuid"] for item in node["configProfile"]["activeInbounds"]]
    if set(active) == set(selected):
        print(f"{target}: all four inbounds already active")
        return
    assert active == [by_tag[info["original_inbound_tag"]]], (
        f"{target}: node active inbounds changed unexpectedly"
    )
    assert node["isConnected"] and not node["isDisabled"]
    updated = api(
        "PATCH",
        "/nodes",
        {
            "uuid": info["node_uuid"],
            "configProfile": {
                "activeConfigProfileUuid": info["profile_uuid"],
                "activeInbounds": selected,
            },
        },
    )
    now_active = [item["uuid"] for item in updated["configProfile"]["activeInbounds"]]
    assert set(now_active) == set(selected)
    print(f"{target}: node activated {len(now_active)} inbounds")


def update_squads(state_path: Path):
    state = json.loads(state_path.read_text())
    profiles = api("GET", "/config-profiles")["configProfiles"]
    wanted = []
    for info in state["targets"].values():
        profile = next(p for p in profiles if p["uuid"] == info["profile_uuid"])
        by_tag = {item["tag"]: item["uuid"] for item in profile["inbounds"]}
        wanted.extend(by_tag[tag] for tag in info["added_tags"])
    squads = api("GET", "/internal-squads")["internalSquads"]
    # Keep these in the owner's test squad until direct connectivity is verified.
    for squad in squads:
        if squad["name"] != "DE":
            continue
        current = [item["uuid"] for item in squad["inbounds"]]
        additional = [inbound for inbound in wanted if inbound not in current]
        if not additional:
            print(f"{squad['name']}: already contains all new inbounds")
            continue
        updated = api(
            "PATCH", "/internal-squads", {"uuid": squad["uuid"], "inbounds": current + additional}
        )
        actual = [item["uuid"] for item in updated["inbounds"]]
        assert set(actual) == set(current + additional)
        print(f"{squad['name']}: added {len(additional)}, preserved {len(current)} existing")


def create_hosts(state_path: Path):
    state = json.loads(state_path.read_text())
    profiles = api("GET", "/config-profiles")["configProfiles"]
    hosts = api("GET", "/hosts")
    for target, info in state["targets"].items():
        profile = next(p for p in profiles if p["uuid"] == info["profile_uuid"])
        by_tag = {item["tag"]: item["uuid"] for item in profile["inbounds"]}
        country = "Германия" if target == "yandex" else "Нидерланды"
        flag = "🇩🇪" if target == "yandex" else "🇳🇱"
        variants = [
            ("XHTTP", info["added_tags"][0], 443, "/direct/x/", "TLS"),
            ("gRPC", info["added_tags"][1], 443, "direct-grpc", "TLS"),
            ("TCP", info["added_tags"][2], 8443, None, "DEFAULT"),
        ]
        for label, tag, port, path, security_layer in variants:
            remark = f"{flag} SPN | {country} • {label}"
            existing = [host for host in hosts if host["remark"] == remark]
            if existing:
                assert len(existing) == 1
                assert existing[0]["inbound"]["configProfileInboundUuid"] == by_tag[tag]
                print(f"{target} {label}: host already exists ({existing[0]['uuid']})")
                continue
            body = {
                "inbound": {
                    "configProfileUuid": info["profile_uuid"],
                    "configProfileInboundUuid": by_tag[tag],
                },
                "remark": remark,
                "address": info["domain"],
                "port": port,
                "path": path,
                "sni": info["domain"],
                "alpn": "h2",
                "fingerprint": "chrome",
                "securityLayer": security_layer,
                "isDisabled": True,
                "nodes": [info["node_uuid"]],
            }
            if label == "XHTTP":
                body["host"] = info["domain"]
            created = api("POST", "/hosts", body)
            assert created["remark"] == remark and created["isDisabled"]
            print(f"{target} {label}: disabled host created ({created['uuid']})")


def disable_hosts(state_path: Path):
    state = json.loads(state_path.read_text())
    hosts = api("GET", "/hosts")
    selected = [
        host
        for host in hosts
        if " • " in host.get("remark", "")
        and host["address"] in {item["domain"] for item in state["targets"].values()}
        and host["inbound"]["configProfileUuid"]
        in {item["profile_uuid"] for item in state["targets"].values()}
    ]
    assert len(selected) == 6, f"Expected exactly 6 direct hosts, got {len(selected)}"
    backup = state_path.parent / "hosts-enabled-before-disable.json"
    if not backup.exists():
        private_write(backup, selected)
    for host in selected:
        if host["isDisabled"]:
            print(f"Already hidden: {host['remark']}")
            continue
        updated = api("PATCH", "/hosts", {"uuid": host["uuid"], "isDisabled": True})
        assert updated["isDisabled"]
        print(f"Hidden while diagnosing: {host['remark']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "apply-profile", "activate-node", "update-squads", "create-hosts", "disable-hosts"])
    parser.add_argument("state", nargs="?", type=Path)
    parser.add_argument("--target", choices=TARGETS)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "apply-profile":
        if args.state is None:
            parser.error("apply-profile requires state.json")
        apply_profile(args.state)
    elif args.command == "update-squads":
        if args.state is None:
            parser.error("update-squads requires state.json")
        update_squads(args.state)
    elif args.command == "create-hosts":
        if args.state is None:
            parser.error("create-hosts requires state.json")
        create_hosts(args.state)
    elif args.command == "disable-hosts":
        if args.state is None:
            parser.error("disable-hosts requires state.json")
        disable_hosts(args.state)
    else:
        if args.state is None or args.target is None:
            parser.error("activate-node requires state.json and --target")
        activate_node(args.state, args.target)


if __name__ == "__main__":
    main()
