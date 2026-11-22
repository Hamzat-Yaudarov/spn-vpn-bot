#!/usr/bin/env python3
"""Prepare a private, single-origin bundle; never contacts a server or panel."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat


SOURCE = Path(__file__).resolve().parent
DEFAULT_STAGE = Path("/private/tmp/wayspn-de1-ws-cdn")
DOMAIN = "de1.chislo.site"
ORIGIN_IP = "64.188.83.197"
PANEL_IP = "144.31.170.147"
WS_PATH = re.compile(r"/ws/[0-9a-f]{48}/\Z")


def validate_state(state: dict) -> None:
    expected = {"domain": DOMAIN, "originIp": ORIGIN_IP, "panelIp": PANEL_IP}
    if not isinstance(state, dict) or any(state.get(k) != v for k, v in expected.items()):
        raise ValueError("This bundle is restricted to the approved DE1 origin")
    if not isinstance(state.get("wsPath"), str) or not WS_PATH.fullmatch(state["wsPath"]):
        raise ValueError("Invalid private WS path")


def check_stage(stage: Path) -> dict:
    if stage.is_symlink() or not stage.is_dir():
        raise ValueError("Stage must be a real private directory")
    if stat.S_IMODE(stage.stat().st_mode) != 0o700:
        raise ValueError("Stage directory must have mode 0700")
    state = json.loads((stage / "state.json").read_text())
    validate_state(state)
    for item in stage.rglob("*"):
        if item.is_symlink():
            raise ValueError("Stage must not contain symlinks")
        expected = 0o700 if item.is_dir() else 0o600
        if stat.S_IMODE(item.stat().st_mode) != expected:
            raise ValueError("All stage directories/files must have mode 0700/0600")
    profile = json.loads((stage / "configprofile.json").read_text())
    inbound = profile["inbounds"]
    if len(inbound) != 1:
        raise ValueError("Probe profile must contain exactly one inbound")
    inbound = inbound[0]
    if (inbound.get("listen") != "127.0.0.1" or inbound.get("port") != 10088
            or inbound.get("protocol") != "vless"
            or inbound.get("settings") != {"clients": [], "decryption": "none"}
            or inbound.get("streamSettings") != {
                "network": "ws", "security": "none", "wsSettings": {"path": state["wsPath"]}
            }):
        raise ValueError("Probe must stay loopback-only, WS-only and without user credentials")
    nginx = (stage / "nginx.conf").read_text()
    if "__WS_PATH__" in nginx or f"location = {state['wsPath']} {{" not in nginx:
        raise ValueError("Nginx and profile WS paths do not match")
    return state


def prepare(stage: Path = DEFAULT_STAGE) -> Path:
    # Exclusive creation is intentional: never rotate an existing deployed path.
    stage.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(stage, 0o700)
    state = {
        "format": 1, "domain": DOMAIN, "originIp": ORIGIN_IP, "panelIp": PANEL_IP,
        "wsPath": f"/ws/{secrets.token_hex(24)}/",
        "scope": "TLS origin only; no panel node, subscriber or RU cascade",
    }
    validate_state(state)
    for name in ("prepare.py", "install.sh", "finish.sh", "bootstrap.conf", "renew-nginx.sh"):
        shutil.copyfile(SOURCE / name, stage / name)
    (stage / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    (stage / "nginx.conf").write_text(
        (SOURCE / "nginx.conf.template").read_text().replace("__WS_PATH__", state["wsPath"])
    )
    (stage / "configprofile.json").write_text(
        (SOURCE / "configprofile.json.template").read_text().replace("__WS_PATH__", state["wsPath"])
    )
    shutil.copytree(SOURCE.parent / "chislo-setup" / "site", stage / "site")
    for item in stage.rglob("*"):
        os.chmod(item, 0o700 if item.is_dir() else 0o600)
    check_stage(stage)
    return stage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check-stage"))
    parser.add_argument("stage", nargs="?", type=Path, default=DEFAULT_STAGE)
    args = parser.parse_args()
    if args.action == "check-stage":
        check_stage(args.stage)
        print("Private DE1 stage validated; WS path and configuration are not printed.")
    else:
        print(f"Prepared private bundle: {prepare(args.stage)}")
        print("No server/panel changes. Do not publish the state or rendered configuration.")


if __name__ == "__main__":
    main()
