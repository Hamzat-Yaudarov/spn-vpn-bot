"""Install and control the already provisioned, narrowly scoped RF controller.

No credentials, VPSes, SSH accounts or hosting orders are created here. Installation
is disabled by default. Enabling requires fresh RPC/probe results AND a correlated
paid-pilot receipt; copying a boolean readiness file can never authorize purchases.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile

from . import five_scope as scope
from .config import load_config
from .provider_gateway import Gateway
from .probes import ProbeRound
from .remna import Remnawave
from .worker import load_audit_helper

UNIT = "spn-ip-failover-five.service"
ETC = Path("/etc/spn-ip-failover")
STATE = Path(scope.STATE)
CONFIG = STATE / "rotation.json"
PROOF = STATE / "priced-pilot-proof.json"

UNIT_TEXT = """[Unit]
Description=Way SPN five ordinary VPS IPv4 replacement
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=spn-failover
Group=spn-failover
WorkingDirectory=/opt/wayspn
StateDirectory=spn-ip-failover
StateDirectoryMode=0700
UMask=0077
Environment=HOME=/var/lib/spn-ip-failover
Environment=PLAYWRIGHT_BROWSERS_PATH=/var/lib/spn-ip-failover/.cache/ms-playwright
LoadCredential=remnawave:/etc/spn-ip-failover/credentials/remnawave
LoadCredential=onecent_email:/etc/spn-ip-failover/credentials/onecent_email
LoadCredential=onecent_password:/etc/spn-ip-failover/credentials/onecent_password
ExecStartPre=/bin/sh /opt/wayspn/spn_ip_failover/deploy/run-five-controller.sh preflight
ExecStart=/bin/sh /opt/wayspn/spn_ip_failover/deploy/run-five-controller.sh run
Restart=on-failure
RestartSec=120s
TimeoutStartSec=900
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/spn-ip-failover

[Install]
WantedBy=multi-user.target
"""


def require(ok, message):
    if not ok:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def private_json(path):
    return load_audit_helper().protected_json(Path(path))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_installed_unit():
    path = Path("/etc/systemd/system") / UNIT
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and not info.st_mode & 0o022 and not path.is_symlink() and path.read_text() == UNIT_TEXT,
            "The exact root-controlled five-VPS unit is not installed")


def validate_scope(panel_url=scope.PANEL):
    """Compare entire installed program inputs, not only selected UUID counts."""
    expected = scope.configs(panel_url)
    for name, value in expected.items():
        require(private_json(STATE / name) == value, "Installed five-VPS scope/configuration changed")
    billing = private_json(STATE / "billing-policy.json")
    require(billing == {"deployment": scope.DEPLOYMENT, "max_daily_gross_rub": "3000.00",
                        "max_monthly_per_ip_rub": "300.00", "maximum_attempts_per_server": 10,
                        "refunds_replenish_budget": False}, "Financial caps changed")
    # The browser profile/config is root-owned and readable by the runtime group;
    # unlike private runtime JSON it intentionally is not uid-owned mode0600.
    onecent_path = ETC / "onecent.json"
    info = onecent_path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) in (0o600, 0o640) and not onecent_path.is_symlink(),
            "Billing configuration is not root-controlled")
    onecent = json.loads(onecent_path.read_bytes())
    require(set(onecent["servers"]) == {r["service_id"] for r in scope.SERVERS}
            and onecent["budget"] == {"journal": scope.STATE + "/onecent-daily-budget",
                                      "max_daily_rub": "3000.00", "timezone": "Europe/Saratov"}
            and onecent["journal"] == scope.STATE + "/onecent-orders"
            and onecent["profile"] == scope.STATE + "/onecent-browser", "Billing scope or gross cap changed")
    for row in scope.SERVERS:
        entry = onecent["servers"][row["service_id"]]
        require(entry == {"name": row["service_name"], "max_upfront_rub": "3000.00",
                          "max_monthly_rub": "300.00", "removable_existing_additional_ipv4": []},
                "Billing service/name or per-IP cap changed")
    return expected


def financial_gate():
    """Cross-check a reviewed private receipt with durable billing/budget state.

    The allocator itself must also declare receipt-correlated ownership support;
    older inventory-delta-only versions intentionally cannot pass this gate.
    The proof contains references/hashes, not credentials or a readiness boolean.
    """
    from .onecent import OneCentBilling
    require(getattr(OneCentBilling, "RECEIPT_CORRELATED_OWNERSHIP", False) is True,
            "Paid enablement blocked: receipt-correlated billing adapter is not installed")
    proof = private_json(PROOF)
    fields = {"version", "deployment", "service_id", "request_id", "ip", "response_sha256",
              "intent_sha256", "dispatch_sha256"}
    require(isinstance(proof, dict) and set(proof) == fields and type(proof["version"]) is int and proof["version"] == 1
            and proof["deployment"] == scope.DEPLOYMENT
            and proof["service_id"] in {r["service_id"] for r in scope.SERVERS}
            and proof["request_id"] == "five-vps-pilot-20261116-001", "Missing correlated paid-pilot proof")
    pilot = STATE / "onecent-pilot-20261116"
    response = pilot / "response.json"
    receipt = private_json(response)
    require(digest(response) == proof["response_sha256"], "Paid-pilot capture changed")
    for name in ("intent", "dispatch"):
        path = pilot / (name + ".json")
        item = private_json(path)
        require(digest(path) == proof[name + "_sha256"] and item.get("service_id") == proof["service_id"]
                and item.get("request_key") == proof["request_id"], "Pilot request/dispatch evidence changed")
    require(receipt.get("service_id") == proof["service_id"] and receipt.get("request_key") == proof["request_id"]
            and type(receipt.get("http_status")) is int and 200 <= receipt["http_status"] <= 299
            and (receipt.get("request") or {}).get("method") == "POST"
            and (receipt.get("request") or {}).get("path") == "/api/services/" + proof["service_id"] + "/ipv4/add",
            "Pilot response does not match the exact paid request")
    orders = private_json(STATE / "onecent-orders/state.json")
    row = orders["servers"][proof["service_id"]]["orders"][proof["request_id"]]
    require(row.get("phase") == "released" and row.get("ip") == proof["ip"]
            and row.get("request_id") == proof["request_id"]
            and row.get("receipt_sha256") == proof["response_sha256"],
            "Paid pilot was not receipt-correlated, settled and cleaned up")
    require(orders["servers"][proof["service_id"]]["releases"].get(proof["ip"]) == "released",
            "Pilot removal is not confirmed in the release journal")
    from .budget import AccountBudget
    budget_key = AccountBudget.key(proof["service_id"], proof["request_id"])
    ledger = private_json(STATE / "onecent-daily-budget/state.json")["budget"]
    payment = ledger["orders"][budget_key]
    require(payment["phase"] == "confirmed" and payment["server_id"] == proof["service_id"]
            and payment["request_id"] == proof["request_id"] and payment["amount_rub"] == row["upfront_rub"],
            "Pilot gross spend is not durably accounted")
    for server in orders["servers"].values():
        require(all(item.get("phase") != "submitting" for item in server.get("orders", {}).values()),
                "An unresolved billing submission requires operator review")
    require(all(item.get("phase") not in ("reserved", "submitting") for item in ledger["orders"].values()),
            "An unresolved gross-spend reservation requires operator review")
    return proof


def preflight(*, financial=True):
    expected = validate_scope()
    proof = financial_gate() if financial else None
    token = os.environ.get("REMNAWAVE_API_TOKEN")
    require(token, "Systemd Remnawave credential is missing")
    panel = Remnawave(scope.PANEL, token)
    gateway = Gateway(expected["provider-gateway.json"], panel)
    journal_path = STATE / "rotation/state.json"
    journal = private_json(journal_path) if journal_path.exists() else {"servers": {}}
    current = {}
    for row in scope.SERVERS:
        node, host = panel.read_node(row["node_uuid"]), panel.read_host(row["host_uuid"])
        address = node.get("address")
        from .config import ipv4
        ipv4(address)
        saved = journal["servers"].get(row["key"])
        accepted = {row["ip"]} if saved is None else {saved["current"]["ip"]}
        if saved and isinstance(saved.get("pending"), dict):
            candidate = saved["pending"].get("candidate")
            if isinstance(candidate, dict):
                accepted.add(candidate.get("ip"))
        require(node.get("isDisabled") is False and node.get("isConnected") is True
                and address in accepted
                and host.get("address") == address and host.get("port") == 443
                and host.get("isDisabled") is False
                and (host.get("inbound") or {}).get("configProfileInboundUuid") == row["inbound_uuid"],
                "Selected node/host binding is not ready")
        gateway.execute({"operation": "check", "server_id": row["service_id"]})
        current[row["host_uuid"]] = address
    if proof is not None:
        service = gateway.servers[proof["service_id"]]
        require(proof["ip"] not in {a.ip for a in gateway.inventory(service, proof["service_id"])},
                "Pilot test address is still assigned; no paid enablement")
    config = load_config(CONFIG)
    probes = ProbeRound(config.probes)
    def observe(server):
        host = server.hosts[0]
        status = probes.test(host, current[host.key])
        # RF reachability may already be blocked: outside pass + healthy RF
        # control is precisely the legitimate failover trigger. Unknown is not.
        require(status in ("healthy", "ru_down"), "Fresh RU/outside-control checks are incomplete")
        return {"server": server.key, "status": status}
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(observe, config.servers))
    return {"ok": True, "guest_rpc": 5, "outside_relays": 2, "paid_gate": financial, "servers": results}


def write_owned(path, data, uid, gid, mode):
    """Atomic idempotent installation; never follow or replace a foreign link."""
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlink in installation path")
    if path.exists():
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid in (0, uid),
                "Foreign installation file")
        if path.read_bytes() == data and info.st_uid == uid and info.st_gid == gid and stat.S_IMODE(info.st_mode) == mode:
            return False
    fd, name = tempfile.mkstemp(prefix=".five-install-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)
    return True


def install():
    require(os.geteuid() == 0, "Installation requires root on the existing RF controller")
    marker = Path("/opt/wayspn/.controller-deployment")
    metadata = marker.lstat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0 and not metadata.st_mode & 0o022
            and not marker.is_symlink() and marker.read_text().strip() == scope.DEPLOYMENT,
            "This is not the selected RF controller")
    require(subprocess.run(["/bin/systemctl", "is-active", "--quiet", UNIT]).returncode != 0,
            "Stop the five-VPS controller before changing its installation")
    account = pwd.getpwnam("spn-failover")
    info = STATE.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == account.pw_uid
            and stat.S_IMODE(info.st_mode) == 0o700 and not STATE.is_symlink(), "Private runtime directory is not ready")
    for name, value in scope.configs().items():
        path = STATE / name
        # A previously started controller owns its initial address/fingerprint;
        # reinstalling cannot reset purchase limits or adopt another plan.
        if (STATE / "rotation/state.json").exists() and path.exists():
            require(path.read_bytes() == encoded(value), "Runtime plan changed after starting; operator reconciliation required")
        write_owned(path, encoded(value), account.pw_uid, account.pw_gid, 0o600)
    policy = {"deployment": scope.DEPLOYMENT, "max_daily_gross_rub": "3000.00", "max_monthly_per_ip_rub": "300.00",
              "maximum_attempts_per_server": 10, "refunds_replenish_budget": False}
    write_owned(STATE / "billing-policy.json", encoded(policy), account.pw_uid, account.pw_gid, 0o600)
    unit_path = Path("/etc/systemd/system") / UNIT
    if unit_path.exists():
        require(unit_path.read_text() == UNIT_TEXT, "Foreign five-VPS service unit; refusing overwrite")
    write_owned(unit_path, UNIT_TEXT.encode(), 0, 0, 0o644)
    subprocess.run(["/bin/systemctl", "daemon-reload"], check=True)
    return {"installed": True, "enabled": False, "financial_operations": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "install", "preflight", "enable", "status", "stop"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--readiness-only", action="store_true", help="Read-only checks; can never enable purchases")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            require(args.output is not None, "render requires --output")
            args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = args.output.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o700, "Output directory must be private")
            for name, value in scope.configs().items():
                write_owned(args.output / name, encoded(value), os.geteuid(), os.getegid(), 0o600)
            result = {"rendered": 3, "secrets_included": False, "financial_operations": False}
        elif args.command == "install":
            result = install()
        elif args.command == "preflight":
            result = preflight(financial=not args.readiness_only)
        elif args.command == "enable":
            require(os.geteuid() == 0 and not args.readiness_only, "Enable requires root and a full financial gate")
            require_installed_unit()
            # Use the same sandbox/account/credentials and fresh ExecStartPre as
            # production, not an old readiness artifact or caller environment.
            subprocess.run(["/bin/systemctl", "start", UNIT], check=True)
            require(subprocess.run(["/bin/systemctl", "is-active", "--quiet", UNIT]).returncode == 0,
                    "Controller preflight did not pass; purchases remain disabled")
            subprocess.run(["/bin/systemctl", "enable", UNIT], check=True)
            result = {"enabled": True}
        elif args.command == "stop":
            require(os.geteuid() == 0, "Stop requires root")
            subprocess.run(["/bin/systemctl", "disable", "--now", UNIT], check=True)
            result = {"enabled": False, "stopped": True}
        else:
            result = {"active": subprocess.run(["/bin/systemctl", "is-active", "--quiet", UNIT]).returncode == 0,
                      "boot_enabled": subprocess.run(["/bin/systemctl", "is-enabled", "--quiet", UNIT]).returncode == 0}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        print("Five-VPS controller prerequisites did not pass; details/credentials suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
