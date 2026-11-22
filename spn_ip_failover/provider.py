"""Run trusted, provider-specific commands using a small JSON protocol."""

import ipaddress
import json
import math
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class ProviderError(RuntimeError):
    """The provider command did not confirm completion."""


class ProviderNotSubmitted(ProviderError):
    """A trusted adapter definitively refused before dispatching a paid order."""


def _identifier(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ValueError(f"{name} must be a nonempty identifier")
    if not value.isprintable() or any(character.isspace() for character in value):
        raise ValueError(f"{name} must be a nonempty identifier")
    return value


@dataclass(frozen=True)
class Allocation:
    ref: str
    ip: str

    def __post_init__(self):
        _identifier(self.ref, "allocation ID")
        if not isinstance(self.ip, str):
            raise ValueError("allocation IP must be a public IPv4 address")
        address = ipaddress.IPv4Address(self.ip)
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError("allocation IP must be a public IPv4 address")

    def as_payload(self):
        return {"id": self.ref, "ip": self.ip}

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or payload.keys() != {"id", "ip"}:
            raise ValueError("invalid provider allocation")
        return cls(payload["id"], payload["ip"])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class CommandProvider:
    """Bridge provider allocation and persistent guest network configuration."""

    operations = frozenset({"check", "allocate", "prepare", "activate", "release"})
    optional_operations = frozenset({"inventory", "retire"})

    def __init__(self, commands: Mapping[str, Sequence[str]], timeout=120):
        if not isinstance(commands, Mapping) or not self.operations <= commands.keys():
            raise ValueError("provider commands must define check, allocate, prepare, activate and release")
        if commands.keys() - self.operations - self.optional_operations:
            raise ValueError("unknown provider command")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("provider command timeout must be finite and positive")
        self.commands = {}
        for operation, argv in commands.items():
            if not isinstance(argv, (list, tuple)) or not argv:
                raise ValueError(f"provider {operation} must be a nonempty argv")
            if any(not isinstance(arg, str) or "\0" in arg for arg in argv):
                raise ValueError(f"provider {operation} argv must contain strings")
            if not os.path.isabs(argv[0]):
                raise ValueError(f"provider {operation} executable must be an absolute path")
            self.commands[operation] = tuple(argv)
        self.timeout = timeout

    def _call(self, operation, server_id, **fields):
        request = {"operation": operation, "server_id": _identifier(server_id, "server ID"), **fields}
        try:
            completed = subprocess.run(
                self.commands[operation],
                input=json.dumps(request).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
            )
        except OSError:
            raise ProviderError(f"could not start provider {operation} command") from None
        except subprocess.TimeoutExpired:
            raise ProviderError(f"provider {operation} timed out; outcome unconfirmed") from None
        if completed.returncode != 0:
            raise ProviderError(f"provider {operation} failed; outcome unconfirmed")
        try:
            if len(completed.stdout) > 16_384:
                raise ValueError("response too long")
            response = json.loads(completed.stdout, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise ProviderError(f"invalid response from provider {operation}; outcome unconfirmed") from None
        if not isinstance(response, dict):
            raise ProviderError(f"invalid response from provider {operation}; outcome unconfirmed")
        return response

    def _require_ok(self, operation, server_id, **fields):
        response = self._call(operation, server_id, **fields)
        if response.keys() != {"ok"} or response["ok"] is not True:
            raise ProviderError(f"provider {operation} did not confirm success")

    def check(self, server_id) -> None:
        for operation, argv in self.commands.items():
            if not os.path.isfile(argv[0]) or not os.access(argv[0], os.X_OK):
                raise ProviderError(f"provider {operation} executable is missing or cannot be run")
        self._require_ok("check", server_id)

    def allocate(self, server_id, request_id) -> Allocation:
        response = self._call("allocate", server_id, request_id=_identifier(request_id, "request ID"))
        if response == {"error": "not_submitted", "reason": "budget_limit"}:
            raise ProviderNotSubmitted("provider budget refused the order before submission")
        if response.keys() != {"id", "ip"}:
            raise ProviderError("provider allocate did not return an allocation; outcome unconfirmed")
        try:
            return Allocation.from_payload(response)
        except ValueError:
            raise ProviderError("provider allocate returned an invalid allocation; outcome unconfirmed") from None

    def inventory(self, server_id) -> tuple[Allocation, ...]:
        if "inventory" not in self.commands:
            raise ProviderError("provider inventory command is missing")
        response = self._call("inventory", server_id)
        try:
            if response.keys() != {"allocations"} or not isinstance(response["allocations"], list):
                raise ValueError("invalid inventory")
            allocations = tuple(Allocation.from_payload(entry) for entry in response["allocations"])
            if len({a.ref for a in allocations}) != len(allocations) or len({a.ip for a in allocations}) != len(allocations):
                raise ValueError("duplicate inventory entry")
        except ValueError:
            raise ProviderError("provider inventory did not return unique IPv4 allocations") from None
        return allocations

    def prepare(self, server_id, allocation: Allocation) -> None:
        self._require_ok("prepare", server_id, **allocation.as_payload())

    def activate(self, server_id, old: Allocation, new: Allocation) -> None:
        if old.ref == new.ref or old.ip == new.ip:
            raise ValueError("provider activate needs two distinct allocations")
        self._require_ok("activate", server_id, old=old.as_payload(), new=new.as_payload())

    def release(self, server_id, allocation: Allocation) -> None:
        self._require_ok("release", server_id, **allocation.as_payload())

    def retire(self, server_id, old: Allocation, current: Allocation) -> None:
        if old.ref == current.ref or old.ip == current.ip:
            raise ValueError("provider retire needs distinct old and current allocations")
        if "retire" in self.commands:
            self._require_ok("retire", server_id, old=old.as_payload(), current=current.as_payload())
        else:
            self.release(server_id, old)
