#!/usr/bin/env python3
"""Capture network metadata from five approved VPSs. No remote writes."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from spn_ip_failover.onecent import Settings, browser
from spn_ip_failover.onecent_ui import OneCentPage, ORIGIN

spec = importlib.util.spec_from_file_location("five_scope", Path(__file__).with_name("install_ro_monitor.py"))
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)

# Pins belong to the original installation, not a newly scanned remote key.
LEGACY = {
    "sweden-gemini": (None, "177.3.250.75"),
    "estonia-gemini": ("185.252.177.234-ssh22", "185.252.177.234"),
    "finland-gemini": ("87.251.16.31-ssh22", "87.251.16.31"),
    "finland-gemini-2": ("83.219.97.238-ssh22", "83.219.97.238"),
    "germany-gemini": ("77.91.66.18-ssh22", "77.91.66.18"),
}


def private_write(path, data):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    if path.exists():
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o777 != 0o600:
            raise ValueError("Unsafe existing metadata file")
        if path.read_bytes() != data:
            raise ValueError("Existing metadata differs; preserve it for reconciliation")
        return
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def trust(row, output):
    directory, alias = LEGACY[row["key"]]
    if directory:
        base = Path.home() / ".spn-reality-deploy/2c986fdadb79" / directory
        record = json.loads((base / "ssh-trust.current.json").read_bytes())
        if record["host"] != alias or record["ip"] != alias or record["port"] != 22:
            raise ValueError("Installer SSH pin identifies another endpoint")
        # Public key in an already accepted installer receipt, never keyscan.
        key = record["key"]
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", key):
            raise ValueError("Invalid accepted SSH key")
        data = f"{alias} ssh-ed25519 {key}\n".encode()
    else:
        result = subprocess.run(["ssh-keygen", "-F", alias], capture_output=True, check=True)
        lines = [line for line in result.stdout.splitlines() if line and not line.startswith(b"#")]
        if not lines:
            raise ValueError("Original server has no previously trusted SSH key")
        data = b"\n".join(lines) + b"\n"
    known = output / (row["key"] + ".known_hosts")
    private_write(known, data)
    return known, alias


def ssh_command(row, known, alias, interface):
    command = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-o", "StrictHostKeyChecking=yes",
               "-o", "UserKnownHostsFile=" + str(known), "-o", "GlobalKnownHostsFile=/dev/null",
               "-o", "HostKeyAlias=" + alias, "-o", "HostKeyAlgorithms=ssh-ed25519",
               "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
               "-o", "PasswordAuthentication=yes", "-o", "NumberOfPasswordPrompts=1",
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15"]
    if interface == "controller":
        physical = shlex.join([sys.executable, str(Path(__file__).with_name("physical_connect.py")),
                               "--ip", "185.9.24.53", "--interface", "en0"])
        relay = ["/usr/bin/ssh", "-F", "/dev/null", "-T", *controller_socket_args(), "-i",
                 str(ROOT / ".local-state/ip-rotation-20261115/controller_setup_ed25519"),
                 "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                 "-o", "ConnectTimeout=15", "-o", "ProxyCommand=" + physical,
                 "-W", row["ip_at_enrollment"] + ":22", "root@185.9.24.53"]
        command += ["-o", "ProxyCommand=" + shlex.join(relay)]
    elif interface:
        proxy = shlex.join([sys.executable, str(Path(__file__).with_name("physical_connect.py")),
                            "--ip", row["ip_at_enrollment"], "--interface", interface])
        command += ["-o", "ProxyCommand=" + proxy]
    command += ["root@" + row["ip_at_enrollment"]]
    return command


def controller_socket_args():
    path = ROOT / ".local-state/ip-rotation-20261115/controller.sock"
    try:
        info, parent = path.lstat(), path.parent.lstat()
    except FileNotFoundError:
        return []
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError("Unsafe temporary controller socket")
    return ["-S", str(path)]


def reachable_transport(ip, requested):
    if requested != "auto":
        return requested
    for interface in (None, "en0"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                if interface:
                    connection.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex(interface))
                connection.settimeout(5)
                connection.connect((ip, 22))
                if connection.recv(256).startswith(b"SSH-"):
                    return interface
        except OSError:
            continue
    raise ValueError("No SSH banner through either scoped transport")


def credentials(context, row):
    response = context.request.get(
        ORIGIN + "/api/services/" + row["service_id"] + "/credentials",
        max_redirects=0, timeout=15000,
    )
    try:
        if response.status != 200:
            raise ValueError("Approved service credentials were not returned")
        value = response.json().get("password")
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(c in value for c in ("\x00", "\r", "\n")):
            raise ValueError("Approved service has no valid current root password")
        return value
    finally:
        response.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--billing-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ssh-interface")
    parser.add_argument("--service", choices=[row["service_id"] for row in scope.SELECTED])
    parser.add_argument("--network-details", action="store_true", help="Read only the pinned Netplan/networkd config files into a private artifact")
    args = parser.parse_args()
    output = args.output.resolve(strict=True)
    if output.stat().st_uid != os.getuid() or output.stat().st_mode & 0o777 != 0o700:
        raise ValueError("Output must be a private owned directory")
    helper = Path(__file__).with_name("guest_enrollment.py").read_bytes()
    askpass = Path(__file__).with_name("guest_password_askpass.py")
    if not os.access(askpass, os.X_OK):
        raise ValueError("Password bridge must be executable")
    rows = [row for row in scope.SELECTED if not args.service or row["service_id"] == args.service]
    settings = Settings.load(args.billing_config)
    with browser(settings) as page:
        # This run can read authenticated pages but cannot submit UI mutations.
        page.route("**/*", lambda route: route.continue_() if route.request.method in ("GET", "HEAD", "OPTIONS") else route.abort())
        for row in rows:
            phase = "open_service"
            try:
                ui = OneCentPage(page, row["service_id"], row["service_name"])
                ui.open()
                phase = "inventory"
                assigned = ui.inventory()
                if len(assigned) != 1 or assigned[0].ip != row["ip_at_enrollment"] or not assigned[0].primary:
                    raise ValueError("Hosting inventory changed since the pinned selection")
                known, alias = trust(row, output)
                phase = "credentials_get"
                secret = credentials(page.context, row)
                env = {**os.environ, "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
                       "DISPLAY": ":0", "SPN_GUEST_SETUP_PASSWORD": secret}
                remote = shlex.join(["python3", "-", "snapshot", "--service-id", row["service_id"],
                                     "--ip", row["ip_at_enrollment"]])
                source = helper
                if args.network_details:
                    source = b'''import os, stat, json, base64, hashlib
from pathlib import Path
rows=[]
for path in [Path('/etc/netplan/50-cloud-init.yaml'), Path('/run/systemd/network/10-netplan-eth0.network'), Path('/run/systemd/network/10-netplan-ens3.network')]:
 if not path.exists():continue
 for parent in path.parents:
  meta=parent.lstat()
  assert stat.S_ISDIR(meta.st_mode) and meta.st_uid==0 and not meta.st_mode & 0o022
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as stream:
  info=os.fstat(stream.fileno()); assert stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and not info.st_mode & 0o022
  raw=stream.read(65537); assert len(raw)<=65536
 rows.append({'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'mode':stat.S_IMODE(info.st_mode),'base64':base64.b64encode(raw).decode()})
print(json.dumps({'network_files':rows}))
'''
                phase = "ssh_snapshot"
                transport = reachable_transport(row["ip_at_enrollment"], args.ssh_interface)
                result = subprocess.run(ssh_command(row, known, alias, transport) + [remote],
                                        input=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=env, start_new_session=True, timeout=100, check=False)
                del secret
                env.pop("SPN_GUEST_SETUP_PASSWORD", None)
                if result.returncode:
                    reason = ("host_key_mismatch" if b"HOST IDENTIFICATION HAS CHANGED" in result.stderr
                              or b"Host key verification failed" in result.stderr else "ssh_or_snapshot_not_confirmed")
                    detail = None
                    for message, code in [(b"Permission denied", "authentication_denied"),
                                          (b"Connection timed out", "connection_timeout"),
                                          (b"Physical SSH transport did not connect", "physical_connection_failed"),
                                          (b"Could not resolve", "resolve_failed")]:
                        if message in result.stderr:
                            reason = code
                    for line in result.stderr.decode("utf-8", errors="replace").splitlines():
                        if line.startswith("Guest enrollment stopped: ") and len(line) < 300:
                            reason = "snapshot_validation_stopped"
                            detail = line.removeprefix("Guest enrollment stopped: ")
                    print(json.dumps({"server": row["key"], "status": reason, "detail": detail}), flush=True)
                    continue
                snapshot = json.loads(result.stdout)
                phase = "persist_snapshot"
                if args.network_details:
                    private_write(output / (row["key"] + ".network-details.json"), result.stdout)
                    print(json.dumps({"server": row["key"], "status": "network_details_captured"}), flush=True)
                    continue
                if snapshot["service_id"] != row["service_id"] or snapshot["ip"] != row["ip_at_enrollment"]:
                    raise ValueError("Snapshot belongs to another approved VPS")
                private_write(output / (row["key"] + ".snapshot.json"), result.stdout)
                print(json.dumps({"server": row["key"], "status": "captured", "ip": snapshot["ip"],
                                  "interface": snapshot["interface"]}), flush=True)
            except Exception as exc:
                # Exception text can include browser arguments. Never print it.
                print(json.dumps({"server": row["key"], "status": "not_confirmed",
                                  "phase": phase, "error_type": type(exc).__name__}), flush=True)


if __name__ == "__main__":
    main()
