"""Connect a scoped hosting adapter to its pinned SSH network helper."""

import argparse
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import subprocess
import sys

from .provider import Allocation, ProviderError, _unique_object
from .remna import Remnawave
from .worker import load_audit_helper


MAX_INPUT = 4096
MAX_OUTPUT = 1024 * 1024


def run_json(command, payload):
    try:
        result = subprocess.run(command, input=json.dumps(payload).encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=100, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ProviderError("The scoped provider command did not finish") from None
    if result.returncode or len(result.stdout) > MAX_OUTPUT:
        raise ProviderError("The scoped provider command did not confirm its result")
    try:
        value = json.loads(result.stdout, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, RecursionError):
        raise ProviderError("The scoped provider command returned invalid JSON") from None


def _argv(value, binary=None):
    if (not isinstance(value, list) or not value or len(value) > 48
            or not all(isinstance(part, str) and part and len(part) <= 2048
                       and not any(char in part for char in ("\x00", "\r", "\n")) for part in value)
            or not Path(value[0]).is_absolute() or (binary and Path(value[0]).name != binary)):
        raise ValueError("Invalid fixed adapter command")
    return list(value)


class Gateway:
    def __init__(self, settings, panel, *, run=run_json):
        if (not isinstance(settings, dict) or settings.keys() != {"version", "remnawave", "servers"}
                or settings["version"] != 1 or not isinstance(settings["servers"], dict)
                or not 1 <= len(settings["servers"]) <= 128):
            raise ValueError("Invalid provider gateway configuration")
        self.servers = {}
        self.panel, self.run = panel, run
        for server_id, row in settings["servers"].items():
            if (not isinstance(server_id, str) or not server_id or not isinstance(row, dict)
                    or row.keys() != {"billing_command", "guest_ssh", "node_uuid", "supports_retire"}
                    or type(row["supports_retire"]) is not bool):
                raise ValueError("Invalid pinned provider service")
            command = _argv(row["billing_command"])
            ssh = _argv(row["guest_ssh"], "ssh")
            if ssh.count("{address}") != 1 or any("{address}" in x and x != "{address}" for x in ssh):
                raise ValueError("SSH must have exactly one complete address placeholder")
            node = Remnawave._uuid(row["node_uuid"])
            self.servers[server_id] = {**row, "billing_command": command, "guest_ssh": ssh, "node_uuid": node}

    def billing(self, row, request):
        return self.run(row["billing_command"], request)

    def inventory(self, row, server_id):
        value = self.billing(row, {"operation": "inventory", "server_id": server_id})
        if value.keys() != {"allocations"} or not isinstance(value["allocations"], list):
            raise ProviderError("Hosting did not provide its assigned IPv4 inventory")
        entries = [Allocation.from_payload(item) for item in value["allocations"]]
        if (not 1 <= len(entries) <= 10 or len(set(entries)) != len(entries)
                or len({a.ref for a in entries}) != len(entries) or len({a.ip for a in entries}) != len(entries)):
            raise ProviderError("Hosting returned ambiguous IPv4 assignments")
        return entries

    def peer(self, row, assignments):
        node = self.panel.read_node(row["node_uuid"])
        try:
            peer = str(IPv4Address(node["address"]))
        except (ValueError, KeyError):
            raise ProviderError("The management node must use a pinned public IPv4") from None
        if peer not in {a.ip for a in assignments} or not IPv4Address(peer).is_global or node["isDisabled"]:
            raise ProviderError("The node address is not an assigned IPv4 of the pinned VPS")
        return peer

    def guest(self, row, peer, request):
        command = [peer if part == "{address}" else part for part in row["guest_ssh"]]
        if self.run(command, request) != {"ok": True}:
            raise ProviderError("The pinned guest network helper did not confirm the change")

    def execute(self, request):
        if not isinstance(request, dict) or request.get("server_id") not in self.servers:
            raise ValueError("The request does not identify an authorized hosting service")
        server_id, operation = request["server_id"], request.get("operation")
        row = self.servers[server_id]
        base = {"operation", "server_id"}
        if operation == "inventory" and request.keys() == base:
            return self.billing(row, request)
        if operation == "check" and request.keys() == base:
            assigned = self.inventory(row, server_id)
            self.guest(row, self.peer(row, assigned), request)
            return self.billing(row, request)
        if operation == "allocate" and request.keys() == base | {"request_id"}:
            # A billing quote/account check cannot prove that a purchased IPv4
            # can be installed. Confirm the existing pinned guest journal first,
            # on every order attempt (including retries), before any paid call.
            assigned = self.inventory(row, server_id)
            self.guest(row, self.peer(row, assigned), {"operation": "check", "server_id": server_id})
            return self.billing(row, request)
        if operation not in ("prepare", "activate", "release", "retire"):
            raise ValueError("Unsupported provider gateway operation")
        assigned = self.inventory(row, server_id)
        peer = self.peer(row, assigned)
        if operation in ("prepare", "release") and request.keys() == base | {"id", "ip"}:
            target = Allocation(request["id"], request["ip"])
            if operation == "prepare":
                if target not in assigned:
                    raise ProviderError("The candidate is not assigned to the pinned VPS")
                self.guest(row, peer, request)
                return {"ok": True}
            if target.ip == peer:
                raise ProviderError("The active published address cannot be released")
            if self.billing(row, request) != {"ok": True}:
                raise ProviderError("Hosting did not confirm removal of the unused address")
            if target in self.inventory(row, server_id):
                raise ProviderError("Hosting still lists the address being removed")
            self.guest(row, peer, {**request, "operation": "deprovision", "billing_confirmed": True})
            return {"ok": True}
        if operation == "activate" and request.keys() == base | {"old", "new"}:
            old, new = Allocation.from_payload(request["old"]), Allocation.from_payload(request["new"])
            if old == new or old.ip == new.ip or old.ref == new.ref or not {old, new} <= set(assigned):
                raise ProviderError("Activation requires two distinct assignments of the pinned VPS")
            self.guest(row, peer, request)
            return {"ok": True}
        if operation == "retire" and request.keys() == base | {"old", "current"}:
            old = Allocation.from_payload(request["old"])
            current = Allocation.from_payload(request["current"])
            if old.ip == current.ip or old.ref == current.ref or current not in assigned or peer != current.ip:
                raise ProviderError("Retirement requires the currently published replacement")
            removal = request if row["supports_retire"] else {
                "operation": "release", "server_id": server_id, **old.as_payload()}
            if self.billing(row, removal) != {"ok": True}:
                raise ProviderError("Hosting did not confirm retirement of the previous address")
            after = self.inventory(row, server_id)
            if old in after or current not in after:
                raise ProviderError("Hosting did not confirm the replacement after retirement")
            self.guest(row, peer, {"operation": "deprovision", "server_id": server_id,
                                   **old.as_payload(), "billing_confirmed": True})
            return {"ok": True}
        raise ValueError("Invalid provider gateway request")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        settings = load_audit_helper().protected_json(args.config)
        panel_config = settings["remnawave"]
        panel = Remnawave(panel_config["url"], os.environ[panel_config["token_env"]])
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if not 0 < len(raw) <= MAX_INPUT:
            raise ValueError("Invalid request length")
        request = json.loads(raw, object_pairs_hook=_unique_object)
        print(json.dumps(Gateway(settings, panel).execute(request)))
        return 0
    except (Exception, KeyboardInterrupt):
        print("The scoped provider operation was not confirmed; inspect its private journals.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
