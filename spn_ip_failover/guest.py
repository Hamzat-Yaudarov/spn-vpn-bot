"""Verify and switch already assigned IPv4 addresses on a supported Linux VPS."""

import argparse
from contextlib import contextmanager, redirect_stdout
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import signal
import stat
import sys
from types import SimpleNamespace

from .provider import Allocation, _unique_object


MAX_REQUEST_BYTES = 4096
LOCK_PATH = "/run/spn-ip-failover-guest.lock"


class GuestArguments(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid guest arguments")


def load_ip_move():
    script = Path(__file__).resolve().parents[1] / "ops" / "ip-move" / "ip_move.py"
    for entry in (script, *script.parents):
        info = entry.lstat()
        expected_type = stat.S_ISREG if entry == script else stat.S_ISDIR
        if not expected_type(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("The installed IPv4 migration helper must be controlled by root")
    return SimpleNamespace(**runpy.run_path(str(script)))


@contextmanager
def exclusive_guest():
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("Unexpected guest migration lock permissions")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def require_assigned(move, interface, *addresses):
    links = move.ip_json("address", "show", "scope", "global")
    for address in addresses:
        matches = [link for link in links for item in link.get("addr_info", [])
                   if item.get("family") == "inet" and item.get("local") == address]
        if len(matches) != 1 or matches[0].get("ifname") != interface or "UP" not in matches[0].get("flags", []):
            raise ValueError("IPv4 is not uniquely assigned on the configured UP interface")


def selected_source(move, interface):
    move.check_rules()
    route = move.default_route()
    if route.get("dev") != interface:
        raise ValueError("Default route uses a different interface")
    return move.selected_source(route)


def run_request(move, server_id, interface, request):
    if not isinstance(request, dict) or request.get("server_id") != server_id:
        raise ValueError("The request does not identify this VPS")
    operation = request.get("operation")
    if operation == "prepare" and request.keys() == {"operation", "server_id", "id", "ip"}:
        target = Allocation(request["id"], request["ip"])
        require_assigned(move, interface, target.ip)
        selected_source(move, interface)
        move.migrate(target.ip, apply=False, ifupdown=True)
        return
    if operation != "activate" or request.keys() != {"operation", "server_id", "old", "new"}:
        raise ValueError("The guest accepts only prepare and activate")

    old = Allocation.from_payload(request["old"])
    new = Allocation.from_payload(request["new"])
    if old.ref == new.ref or old.ip == new.ip:
        raise ValueError("Activation requires two distinct allocations")
    require_assigned(move, interface, old.ip, new.ip)
    if selected_source(move, interface) not in (old.ip, new.ip):
        raise ValueError("Selected source has drifted from both allocations")

    # Reapply the persistent configuration if a previous run switched the live route.
    move.migrate(new.ip, apply=True, ifupdown=True)
    if selected_source(move, interface) != new.ip:
        raise ValueError("Activation did not select the requested source")
    require_assigned(move, interface, old.ip, new.ip)


def _interrupt(_number, _frame):
    raise InterruptedError("Guest migration interrupted")


def main(argv=None):
    parser = GuestArguments(add_help=False, exit_on_error=False)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--interface", required=True)
    try:
        args = parser.parse_args(argv)
        if (not sys.platform.startswith("linux") or os.geteuid() != 0
                or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", args.server_id)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", args.interface)):
            raise ValueError("A root-operated Linux guest with a configured VPS and interface is required")
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if not 0 < len(raw) <= MAX_REQUEST_BYTES:
            raise ValueError("Invalid guest request length")
        request = json.loads(raw, object_pairs_hook=_unique_object)
        with exclusive_guest(), redirect_stdout(sys.stderr):
            run_request(load_ip_move(), args.server_id, args.interface, request)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, argparse.ArgumentError, KeyboardInterrupt) as exc:
        print(f"Guest IPv4 change unconfirmed: {exc}", file=sys.stderr)
        print('{"ok":false}')
        return 1
    print('{"ok":true}')
    return 0


if __name__ == "__main__":
    for stop_signal in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(stop_signal, _interrupt)
    raise SystemExit(main())
