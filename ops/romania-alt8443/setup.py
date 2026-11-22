#!/usr/bin/env python3
"""Add one independent TCP passthrough listener; never rewrite the main proxy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time

IP = "82.152.132.35"
DEPLOYMENT = "96f1989a670c46178ab81b24da941aa7"
UNIT = "spn-reality-alt8443.service"
CONFIG = Path("/etc/haproxy/spn-reality-alt8443.cfg")
UNIT_FILE = Path("/etc/systemd/system") / UNIT
MAIN = Path("/etc/haproxy/haproxy.cfg")
CFG = f"""# SPN alternate 8443; deployment {DEPLOYMENT}
global
    user haproxy
    group haproxy
    maxconn 8192
    ulimit-n 65536

defaults
    mode tcp
    timeout connect 3s
    timeout queue 3s
    timeout client 120s
    timeout server 120s
    timeout client-fin 5s
    timeout server-fin 5s
    timeout tunnel 120s

frontend spn_reality_alt8443
    bind 0.0.0.0:8443
    maxconn 8192
    tcp-request inspect-delay 5s
    acl complete_client_hello req.ssl_hello_type 1
    tcp-request content accept if complete_client_hello
    tcp-request content reject if WAIT_END
    default_backend spn_reality_alt_core

backend spn_reality_alt_core
    server xray 127.0.0.1:2443 send-proxy-v2 maxconn 8192
""".encode()
SERVICE = f"""[Unit]
Description=SPN isolated REALITY listener 8443 ({DEPLOYMENT})
After=network-online.target haproxy.service
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/usr/sbin/haproxy -c -f {CONFIG}
ExecStart=/usr/sbin/haproxy -db -f {CONFIG}
Restart=on-failure
RestartSec=3
LimitNOFILE=65536
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
""".encode()


def run(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if check and result.returncode:
        raise RuntimeError("Scoped service command failed")
    return result


def active():
    return run("systemctl", "is-active", "--quiet", UNIT, check=False).returncode == 0


def owned_file(path, expected=None):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Symlink in a managed path")
    if path.exists():
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
            raise ValueError("Unsafe managed file")
        if expected is not None and path.read_bytes() != expected:
            raise ValueError("Existing alternate listener is not this deployment")


def create(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"plan_only": True, "port": 8443, "backend": "127.0.0.1:2443",
                          "main_proxy_changed": False}))
        return
    if os.geteuid() != 0:
        raise ValueError("Run as root on the confirmed VPS")
    marker = Path("/opt/spn-reality-node/.spn-deployment")
    owned_file(marker)
    if marker.read_text().strip() != DEPLOYMENT:
        raise ValueError("Different VPS deployment")
    addresses = json.loads(run("ip", "-j", "-4", "addr").stdout)
    if IP not in {a["local"] for link in addresses for a in link.get("addr_info", [])}:
        raise ValueError("Different assigned VPS address")
    owned_file(MAIN)
    before = hashlib.sha256(MAIN.read_bytes()).hexdigest()
    owned_file(CONFIG, CFG); owned_file(UNIT_FILE, SERVICE)
    existing = run("ss", "-H", "-lnt", "sport = :8443").stdout.strip()
    if existing and (not active() or not CONFIG.exists() or not UNIT_FILE.exists()):
        raise ValueError("Port 8443 belongs to another service")
    # The owner opened UFW already. Keep their firewall rule unchanged.
    rules = run("ufw", "status").stdout
    if not any(line.split()[:3] == ["8443/tcp", "ALLOW", "Anywhere"] for line in rules.splitlines()):
        raise ValueError("Owner's IPv4 UFW 8443 rule is missing")
    with tempfile.TemporaryDirectory(prefix="spn-alt8443-", dir="/run") as tmp:
        trial = Path(tmp) / "haproxy.cfg"; create(trial, CFG)
        run("/usr/sbin/haproxy", "-c", "-f", str(trial))
    was_active = active()
    was_enabled = run("systemctl", "is-enabled", "--quiet", UNIT, check=False).returncode == 0
    created = []
    try:
        for path, data in ((CONFIG, CFG), (UNIT_FILE, SERVICE)):
            if not path.exists():
                create(path, data); created.append(path)
        run("systemctl", "daemon-reload")
        run("systemctl", "start", UNIT)
        for _ in range(30):
            try:
                with socket.create_connection(("127.0.0.1", 8443), timeout=.5):
                    break
            except OSError:
                time.sleep(.2)
        else:
            raise RuntimeError("Alternate listener did not start")
        if hashlib.sha256(MAIN.read_bytes()).hexdigest() != before:
            raise RuntimeError("Main proxy changed concurrently")
        run("systemctl", "enable", UNIT)
        print(json.dumps({"port": 8443, "listener_ready": True, "enabled": True,
                          "main_443_config_unchanged": True, "backend": "127.0.0.1:2443"}))
    except Exception:
        if not was_active:
            run("systemctl", "stop", UNIT, check=False)
            if not was_enabled:
                run("systemctl", "disable", UNIT, check=False)
            for path in reversed(created):
                path.unlink()
            run("systemctl", "daemon-reload", check=False)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"stopped": True, "error_type": type(exc).__name__, "main_proxy_restarted": False}))
        raise SystemExit(1)
