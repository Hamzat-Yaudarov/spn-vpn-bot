"""Check the same VPN leaf through a short-lived, private SSH TCP relay.

All client credentials and Xray execution remain on this machine. SSH only
forwards transport connections; it runs no remote commands. Input/output match
worker.py. A relay failure is unknown, never evidence that a VPN IP is down.

Private relay JSON: {"ssh_command": ["/usr/bin/ssh", "-i", "/ABS/key",
"-o", "UserKnownHostsFile=/ABS/known_hosts"], "host": "spnrelay@EU_IP"}.
Alternatively the destination may be the last ssh_command argument. Optional
-p, -l and -o HostKeyAlias=NAME are accepted. No other SSH options are accepted.
Key and known_hosts must be uid-owned 0600 in a uid-owned 0700 directory.
Optional reverse_service accepts only the two reviewed service UUIDs below;
it does not expose arbitrary HostName, loopback ports or SSH options to input.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

from . import worker


STARTUP_SECONDS = 8
SSH_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
HOST_ALIAS = re.compile(r"[A-Za-z0-9_.:-]{1,253}")
# Only the two already selected, separately owned outside-control VPSes.
# The logical destination remains its freshly validated public node address;
# HostName merely transports SSH over that service's private reverse listener.
REVERSE_PORTS = {
    "21e508f0-2ef2-47da-aee7-15dc8c66eb15": 23002,
    "4d6f358a-e75f-4dd8-a635-c6ad320f2732": 23005,
}


def private_file(value):
    """Check a fixed SSH input without reading or displaying its contents."""
    if not isinstance(value, str):
        raise ValueError("Invalid private relay input")
    path = Path(value)
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Invalid private relay input")
    parent = path.parent.stat()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not (parent.st_uid == os.geteuid() and stat.S_IMODE(parent.st_mode) == 0o700
                and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                and 0 < info.st_size <= 1_000_000):
            raise ValueError("Invalid private relay input")
    finally:
        os.close(descriptor)
    return str(path)


def ssh_arguments(audit, config, port):
    """Accept only key-based SSH inputs and add all behavioral options here."""
    if (not isinstance(config, dict) or config.keys() - {"ssh_command", "host", "reverse_service"}
            or type(port) is not int or not 1024 <= port <= 65535):
        raise ValueError("Invalid relay configuration")
    command = config.get("ssh_command")
    if (not isinstance(command, list) or not 1 <= len(command) <= 20
            or any(not isinstance(item, str) or not item or "\x00" in item for item in command)
            or command[0] != "/usr/bin/ssh"):
        raise ValueError("Invalid relay command")
    destination = config.get("host")
    options = command[1:]
    if destination is None:
        if not options or options[-1].startswith("-"):
            raise ValueError("Missing relay destination")
        destination, options = options[-1], options[:-1]
    if not isinstance(destination, str) or destination.startswith("-"):
        raise ValueError("Invalid relay destination")
    if "@" in destination:
        if destination.count("@") != 1:
            raise ValueError("Invalid relay destination")
        user, host = destination.split("@")
        if not SSH_USER.fullmatch(user):
            raise ValueError("Invalid relay user")
    else:
        user, host = None, destination
    if not audit.public_address(host):
        raise ValueError("Relay must use a public destination")

    accepted, seen, values = [], set(), {}
    for position in range(0, len(options), 2):
        if position + 1 >= len(options):
            raise ValueError("Invalid relay option")
        flag, value = options[position:position + 2]
        if flag == "-i":
            name = "identity"
            value = private_file(value)
        elif flag == "-p":
            name = "port"
            if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
                raise ValueError("Invalid relay port")
        elif flag == "-l":
            name = "user"
            if user is not None or not SSH_USER.fullmatch(value):
                raise ValueError("Invalid relay user")
        elif flag == "-o" and value.startswith("UserKnownHostsFile="):
            name = "known_hosts"
            value = "UserKnownHostsFile=" + private_file(value.split("=", 1)[1])
        elif flag == "-o" and value.startswith("HostKeyAlias="):
            name = "host_alias"
            if not HOST_ALIAS.fullmatch(value.split("=", 1)[1]):
                raise ValueError("Invalid relay alias")
        else:
            raise ValueError("Unsupported relay option")
        if name in seen:
            raise ValueError("Duplicate relay option")
        seen.add(name)
        values[name] = value
        accepted.extend((flag, value))
    if not {"identity", "known_hosts"} <= seen or (user is None and "user" not in seen):
        raise ValueError("Explicit relay key, known host and user are required")
    if "reverse_service" in config:
        service = config["reverse_service"]
        if (not isinstance(service, str) or service not in REVERSE_PORTS
                or user != "spn-ip-relay" or values.get("port") != "22"
                or values.get("host_alias") != "HostKeyAlias=spn-ip-rotation-relay-" + service):
            raise ValueError("Reverse relay must bind its exact service, account and host-key alias")
        accepted = [part for pair in zip(accepted[::2], accepted[1::2])
                    if pair[0] != "-p" for part in pair]
        accepted += ["-o", "HostName=127.0.0.1", "-p", str(REVERSE_PORTS[service])]
    return [command[0], "-F", "/dev/null", "-N", "-D", "127.0.0.1:" + str(port),
            "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UpdateHostKeys=no",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "ConnectTimeout=5",
            "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=1", *accepted, destination]


@contextmanager
def relay(audit, config):
    child = None
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command = ssh_arguments(audit, config, port)
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=audit.ENV, close_fds=True,
                                 start_new_session=True)
        deadline = time.monotonic() + STARTUP_SECONDS
        ready = False
        while time.monotonic() < deadline and child.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    pass
                ready = True
                break
            except OSError:
                time.sleep(0.05)
        if not ready or child.poll() is not None:
            raise RuntimeError("Relay startup failed")
        yield port, child
    finally:
        audit.stop(child)


def check(audit, config, relay_config, request):
    mode = config.get("control_mode", "peer_vpn")
    outbound = worker.select_outbound(audit, config["routes"], request, control_mode=mode)
    engine = Path(config["xray"])
    audit.pinned_engine(engine, config["sha256"])
    with relay(audit, relay_config) as (port, child):
        with tempfile.TemporaryDirectory(prefix="spn-relay-probe-") as directory:
            direct = request["action"] == "control"
            result = audit.test_one(engine, None if direct else outbound, Path(directory), 0,
                                    transport_proxy=port, direct_https=direct)
        if child.poll() is not None:
            return "unknown"
    return worker.result_status(result)


def main(argv=None):
    parser = worker.ProbeArguments(add_help=False, exit_on_error=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--relay-config", required=True, type=Path)
    status = "unknown"
    try:
        arguments = parser.parse_args(argv)
        raw = sys.stdin.buffer.read(worker.MAX_REQUEST_BYTES + 1)
        if not 0 < len(raw) <= worker.MAX_REQUEST_BYTES:
            raise ValueError("Probe request is missing or too large")
        request = json.loads(raw)
        audit = worker.load_audit_helper()
        config = audit.protected_json(arguments.config)
        relay_config = audit.protected_json(arguments.relay_config)
        status = check(audit, config, relay_config, request)
    except (Exception, KeyboardInterrupt, SystemExit):
        pass
    print(json.dumps({"status": status}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    for stop_signal in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop_signal, worker._interrupt)
    raise SystemExit(main())
