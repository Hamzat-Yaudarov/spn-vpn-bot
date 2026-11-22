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
DEFAULT_STAGE = Path("/private/tmp/wayspn-de1-xhttp-cdn")
DOMAIN = "de1.chislo.site"
ORIGIN_IP = "64.188.83.197"
PANEL_IP = "144.31.170.147"
EDGE_PATH = re.compile(r"/edge/[0-9a-f]{48}/\Z")
SERVER_XHTTP = {
    "host": "",
    "mode": "packet-up",
    "xmux": {
        "cMaxReuseTimes": "0",
        "maxConnections": "1",
        "hKeepAlivePeriod": 0,
        "hMaxRequestTimes": "300-600",
        "hMaxReusableSecs": "900-1800"
    },
    "noSSEHeader": False,
    "xPaddingKey": "_token",
    "uplinkDataKey": "X-Transport-Data",
    "xPaddingBytes": "16-64",
    "xPaddingHeader": "X-Signature",
    "xPaddingMethod": "tokenish",
    "uplinkChunkSize": "2048",
    "uplinkHTTPMethod": "GET",
    "xPaddingObfsMode": True,
    "xPaddingPlacement": "query",
    "scMaxBufferedPosts": 50,
    "scMaxEachPostBytes": "2048",
    "uplinkDataPlacement": "header",
    "scMinPostsIntervalMs": "0-1",
    "scStreamUpServerSecs": "60-180",
    "serverMaxHeaderBytes": 8192
}
CLIENT_XHTTP = {k: v for k, v in SERVER_XHTTP.items() if k not in {"host", "mode"}}
CLIENT_XHTTP["scMaxEachPostBytes"] = "1024-2048"



def xhttp_settings(edge_path: str) -> dict:
    return {"path": edge_path, **SERVER_XHTTP}


def validate_state(state: dict) -> None:
    expected = {"domain": DOMAIN, "originIp": ORIGIN_IP, "panelIp": PANEL_IP}
    if not isinstance(state, dict) or any(state.get(k) != v for k, v in expected.items()):
        raise ValueError("This bundle is restricted to the approved DE1 origin")
    if not isinstance(state.get("edgePath"), str) or not EDGE_PATH.fullmatch(state["edgePath"]):
        raise ValueError("Invalid private XHTTP path")


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
    if (inbound.get("listen") != "127.0.0.1" or inbound.get("port") != 10085
            or inbound.get("protocol") != "vless"
            or inbound.get("settings") != {"clients": [], "decryption": "none"}
            or inbound.get("streamSettings") != {
                "network": "xhttp", "security": "none", "xhttpSettings": xhttp_settings(state["edgePath"])
            }):
        raise ValueError("Probe must stay loopback-only, XHTTP-only and without user credentials")
    nginx = (stage / "nginx.conf").read_text()
    if json.loads((stage / "xhttp-extra.json").read_text()) != CLIENT_XHTTP:
        raise ValueError("Client extras differ from the verified DE11 baseline")
    if "__EDGE_PATH__" in nginx or f"location ^~ {state['edgePath']} {{" not in nginx:
        raise ValueError("Nginx and profile XHTTP paths do not match")
    return state


def prepare(stage: Path = DEFAULT_STAGE) -> Path:
    # Exclusive creation is intentional: never rotate an existing deployed path.
    stage.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(stage, 0o700)
    state = {
        "format": 1, "domain": DOMAIN, "originIp": ORIGIN_IP, "panelIp": PANEL_IP,
        "edgePath": f"/edge/{secrets.token_hex(24)}/",
        "scope": "TLS origin only; no panel node, subscriber or RU cascade",
    }
    validate_state(state)
    for name in ("prepare.py", "install.sh", "finish.sh", "bootstrap.conf", "renew-nginx.sh"):
        shutil.copyfile(SOURCE / name, stage / name)
    (stage / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    (stage / "nginx.conf").write_text(
        (SOURCE / "nginx.conf.template").read_text().replace("__EDGE_PATH__", state["edgePath"])
    )
    (stage / "configprofile.json").write_text(
        (SOURCE / "configprofile.json.template").read_text().replace("__EDGE_PATH__", state["edgePath"])
    )
    (stage / "xhttp-extra.json").write_text(
        (SOURCE / "xhttp-extra.json.template").read_text()
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
        print("Private DE1 stage validated; XHTTP path and configuration are not printed.")
    else:
        print(f"Prepared private bundle: {prepare(args.stage)}")
        print("No server/panel changes. Do not publish the state or rendered configuration.")


if __name__ == "__main__":
    main()
