"""Manage a pinned, simple ifupdown IPv4 interface without restarting networking.

The root-owned configuration authorizes one interface, gateway and bootstrap
snapshot. JSON stdin accepts no paths or commands. Bootstrap defaults to a dry
plan; prepare, activate and deprovision are journalled, resumable operations.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import fcntl
import hashlib
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, ip_address
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile


MAX_REQUEST = 4096
MAX_CONFIG = 65536
MAX_STATE = 1024 * 1024
MAX_IPS = 10
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LC_ALL": "C"}
START = "# Managed by Way SPN IPv4 helper.\n"
END = "# End Way SPN IPv4 helper.\n"


class ManagedGuestError(ValueError):
    """The requested network transition could not be established safely."""


def require(condition, message):
    if not condition:
        raise ManagedGuestError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON field")
        result[key] = value
    return result


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def ipv4(value, *, public=True):
    require(isinstance(value, str), "Expected an IPv4 string")
    try:
        address = IPv4Address(value)
    except ValueError:
        raise ManagedGuestError("Expected an IPv4 address") from None
    require(str(address) == value and (not public or address.is_global)
            and not address.is_multicast and not address.is_unspecified,
            "Expected a canonical public unicast IPv4")
    return str(address)


def allocation(value):
    require(isinstance(value, dict) and set(value) == {"id", "ip"}, "Invalid allocation")
    reference = value["id"]
    require(isinstance(reference, str) and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", reference),
            "Invalid allocation ID")
    return {"id": reference, "ip": ipv4(value["ip"])}


def allocations(values):
    require(isinstance(values, list) and 1 <= len(values) <= MAX_IPS, "Expected one to ten owned IPv4 addresses")
    rows = [allocation(value) for value in values]
    require(len({row["id"] for row in rows}) == len(rows)
            and len({row["ip"] for row in rows}) == len(rows), "Allocations must have unique IDs and addresses")
    return rows


def ip_set(values):
    require(isinstance(values, list) and 1 <= len(values) <= MAX_IPS, "Invalid expected IPv4 set")
    result = [ipv4(value) for value in values]
    require(len(set(result)) == len(result), "Duplicate expected IPv4")
    return set(result)


@dataclass(frozen=True)
class Settings:
    server_id: str
    interface: str
    gateway: str
    interfaces_file: Path
    state_directory: Path
    bootstrap: dict

    @classmethod
    def from_value(cls, value):
        expected = {"version", "server_id", "interface", "gateway", "method", "interfaces_file",
                    "state_directory", "bootstrap"}
        require(isinstance(value, dict) and set(value) == expected and type(value["version"]) is int
                and value["version"] == 1 and value["method"] == "ifupdown", "Unsupported root guest configuration")
        server, interface = value["server_id"], value["interface"]
        require(isinstance(server, str) and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", server), "Invalid pinned VPS")
        require(isinstance(interface, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,12}", interface)
                and interface != "lo", "Invalid pinned interface")
        gateway = ipv4(value["gateway"], public=False)
        paths = [Path(value[name]) for name in ("interfaces_file", "state_directory")
                 if isinstance(value[name], str)]
        require(len(paths) == 2 and all(path.is_absolute() and ".." not in path.parts for path in paths),
                "Absolute pinned paths are required")
        require(paths[0] != paths[1] and paths[1] not in paths[0].parents, "Network configuration cannot be in state storage")
        boot = value["bootstrap"]
        require(isinstance(boot, dict) and set(boot) == {"expected_config_sha256", "expected_live_ipv4", "active", "allocations"},
                "Bootstrap must pin the exact network snapshot")
        require(isinstance(boot["expected_config_sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", boot["expected_config_sha256"]), "Invalid bootstrap SHA-256")
        owned, active = allocations(boot["allocations"]), allocation(boot["active"])
        live = ip_set(boot["expected_live_ipv4"])
        require(active in owned and live == {row["ip"] for row in owned}, "Bootstrap ownership differs from pinned live IPv4")
        return cls(server, interface, gateway, paths[0], paths[1], deepcopy(boot))


def canonical_interfaces(raw, settings, owned, active):
    """Replace only validated static IPv4 stanzas and their auto declarations."""
    require(isinstance(raw, bytes) and len(raw) <= MAX_CONFIG and b"\x00" not in raw,
            "Invalid interfaces file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ManagedGuestError("Interfaces file must be UTF-8") from None
    require(text.endswith("\n") and "\r" not in text, "Interfaces file must use complete LF lines")
    owned = allocations(owned)
    active = allocation(active)
    require(active in owned, "Active address is not owned")
    lines = text.splitlines(keepends=True)
    require(not any(re.match(r"\s*(?:source|source-directory|mapping)\b", line) for line in lines),
            "Interfaces includes and mappings are unsupported")
    dev = settings.interface
    target_name = re.compile(re.escape(dev) + r"(?::[0-9]+)?")
    family_headers = [match.groups() for line in lines
                      if (match := re.fullmatch(r"iface\s+(\S+)\s+(inet6?)\s+\S+\s*", line.strip()))
                      and not line[0].isspace()]
    ipv4_names = {name for name, family in family_headers if family == "inet" and target_name.fullmatch(name)}
    ipv6_names = {name for name, family in family_headers if family == "inet6" and target_name.fullmatch(name)}
    remove, changes, insertions, blocks = set(), {}, [], []
    fields = {"hwaddress": set(), "dns-nameservers": set()}
    stanza_names = set()
    markers = [index for index, line in enumerate(lines) if line in (START, END)]
    require((not markers) or (sum(line == START for line in lines) == 1
                             and sum(line == END for line in lines) == 1
                             and lines.index(START) < lines.index(END)), "Invalid managed block markers")
    remove.update(markers)
    insertions.extend(markers[:1])
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        require(not re.match(r"\s*(?:source|source-directory|mapping)\b", line),
                "Interfaces includes and mappings are unsupported")
        if not line[0].isspace() and stripped.startswith(("auto ", "allow-hotplug ")):
            tokens = stripped.split()
            require(len(tokens) >= 2 and all(re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", item) for item in tokens[1:]),
                    "Invalid interface activation declaration")
            # Preserve activation for a separate IPv6 alias, including an alias
            # whose stale IPv4 stanza is being removed. Base ens3 is re-added.
            selected = [name for name in tokens[1:] if name in ipv4_names
                        and (name == dev or name not in ipv6_names)]
            require(not any(name.startswith(dev + ":") and not target_name.fullmatch(name) for name in tokens[1:]),
                    "Unsupported managed IPv4 alias")
            if selected:
                insertions.append(index)
                remaining = [name for name in tokens[1:] if name not in selected]
                if remaining:
                    changes[index] = tokens[0] + " " + " ".join(remaining) + "\n"
                else:
                    remove.add(index)
            index += 1
            continue
        header = re.fullmatch(r"iface\s+(\S+)\s+(inet6?|can)\s+(\S+)\s*", stripped)
        if header and not line[0].isspace():
            name, family, method = header.groups()
            if name.startswith(dev + ":"):
                require(bool(target_name.fullmatch(name)), "Unsupported managed IPv4 alias")
            end = index + 1
            while end < len(lines) and (not lines[end].strip() or lines[end].lstrip().startswith("#")
                                        or lines[end][0].isspace()):
                # End marker belongs to this managed block, not a following IPv6 stanza.
                if lines[end] == END:
                    break
                end += 1
            if target_name.fullmatch(name) and family == "inet":
                require(method == "static" and name not in stanza_names, "Managed IPv4 stanzas must be unique and static")
                stanza_names.add(name)
                options = {}
                for option in lines[index + 1:end]:
                    value = option.strip()
                    if not value or value.startswith("#"):
                        continue
                    require(option[0].isspace(), "Invalid IPv4 option indentation")
                    parts = value.split(None, 1)
                    require(len(parts) == 2 and parts[0] in {"address", "netmask", "gateway", "hwaddress", "dns-nameservers", "post-up"}
                            and parts[0] not in options, "Unsupported or repeated IPv4 command/option")
                    options[parts[0]] = parts[1]
                require("address" in options, "A managed IPv4 stanza needs an address")
                address = options["address"]
                try:
                    parsed = IPv4Interface(address)
                except ValueError:
                    raise ManagedGuestError("Invalid static IPv4 address") from None
                ipv4(str(parsed.ip))
                require(("/" in address) != ("netmask" in options), "An IPv4 stanza needs exactly one prefix or netmask")
                if "netmask" in options:
                    try:
                        IPv4Network("0.0.0.0/" + options["netmask"])
                    except ValueError:
                        raise ManagedGuestError("Invalid IPv4 netmask") from None
                if "gateway" in options:
                    require(name == dev and options["gateway"] == settings.gateway, "Unexpected static gateway")
                if "hwaddress" in options:
                    require(re.fullmatch(r"ether (?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", options["hwaddress"]),
                            "Invalid hardware address")
                    fields["hwaddress"].add(options["hwaddress"].lower())
                if "dns-nameservers" in options:
                    dns = options["dns-nameservers"].split()
                    require(1 <= len(dns) <= 8, "Invalid DNS nameservers")
                    try:
                        dns = [str(ip_address(value)) for value in dns]
                    except ValueError:
                        raise ManagedGuestError("Invalid DNS nameservers") from None
                    fields["dns-nameservers"].add(" ".join(dns))
                if "post-up" in options:
                    expected = (f"/usr/sbin/ip -4 route replace default via {settings.gateway} "
                                f"dev {dev} onlink src {parsed.ip}")
                    require(name == dev and options["post-up"] == expected, "Unknown IPv4 post-up command")
                remove.update(range(index, end))
                insertions.append(index)
                blocks.append(name)
            index = end
            continue
        require(not (not line[0].isspace()), "Unknown top-level interfaces directive")
        raise ManagedGuestError("Orphaned interfaces option")
    require(dev in blocks and all(len(values) <= 1 for values in fields.values()),
            "Expected one base IPv4 stanza and consistent hardware/DNS options")
    canonical = [START, f"auto {dev}\n", f"iface {dev} inet static\n", f"    address {active['ip']}/32\n"]
    for name, values in fields.items():
        if values:
            canonical.append(f"    {name} {next(iter(values))}\n")
    canonical.append(f"    post-up /usr/sbin/ip -4 route replace default via {settings.gateway} dev {dev} onlink src {active['ip']}\n")
    remaining = sorted((row for row in owned if row != active), key=lambda row: int(IPv4Address(row["ip"])))
    number = 0
    for row in remaining:
        number += 1
        while f"{dev}:{number}" in ipv6_names:
            number += 1
        alias = f"{dev}:{number}"
        require(len(alias) <= 15, "Managed IPv4 alias exceeds interface name limit")
        canonical.extend([f"auto {alias}\n", f"iface {alias} inet static\n", f"    address {row['ip']}/32\n"])
    canonical.append(END)
    position = min(insertions)
    output = []
    for index, line in enumerate(lines):
        if index == position:
            output.extend(canonical)
        if index not in remove:
            output.append(changes.get(index, line))
    result = "".join(output).encode()
    require(len(result) <= MAX_CONFIG, "Rendered interfaces file exceeds size limit")
    return result


def secure_parents(path):
    require(path.is_absolute() and ".." not in path.parts, "Expected an absolute root-controlled path")
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                "Path parents must be root-owned directories without group/other write access")


def read_root_file(path, maximum, *, private):
    secure_parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and (stat.S_IMODE(info.st_mode) == 0o600 if private else not info.st_mode & 0o022),
                "Unexpected root file ownership or permissions")
        raw = stream.read(maximum + 1)
        require(0 < len(raw) <= maximum, "Root file exceeds the size limit")
    return raw, info


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, raw, mode, group=0, *, before_replace=None):
    descriptor, temporary = tempfile.mkstemp(prefix=".spn-ipv4-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchown(stream.fileno(), 0, group)
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class FileStore:
    def __init__(self, settings):
        self.settings = settings
        self.directory = settings.state_directory
        self.state_raw = None

    def config(self):
        return read_root_file(self.settings.interfaces_file, MAX_CONFIG, private=False)[0]

    def state(self):
        try:
            self.state_raw = read_root_file(self.directory / "state.json", MAX_STATE, private=True)[0]
        except FileNotFoundError:
            self.state_raw = None
            return None
        return json.loads(self.state_raw, object_pairs_hook=unique_object)

    def save(self, value):
        path = self.directory / "state.json"
        try:
            current = read_root_file(path, MAX_STATE, private=True)[0]
        except FileNotFoundError:
            current = None
        require(current == self.state_raw, "Guest journal changed concurrently")
        raw = encoded(value)
        require(len(raw) <= MAX_STATE, "Guest journal exceeds size limit")

        def confirm():
            try:
                latest = read_root_file(path, MAX_STATE, private=True)[0]
            except FileNotFoundError:
                latest = None
            require(latest == self.state_raw, "Guest journal changed concurrently before commit")

        atomic_write(path, raw, 0o600, before_replace=confirm)
        self.state_raw = raw

    def write_config(self, expected, raw):
        current, info = read_root_file(self.settings.interfaces_file, MAX_CONFIG, private=False)
        require(digest(current) == expected, "Interfaces changed concurrently")

        def confirm():
            latest, meta = read_root_file(self.settings.interfaces_file, MAX_CONFIG, private=False)
            require(digest(latest) == expected
                    and (meta.st_dev, meta.st_ino, meta.st_ctime_ns, meta.st_uid, meta.st_gid, meta.st_mode)
                    == (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode),
                    "Interfaces changed concurrently before commit")

        atomic_write(self.settings.interfaces_file, raw, stat.S_IMODE(info.st_mode), info.st_gid,
                     before_replace=confirm)

    def backup(self, raw):
        path = self.directory / "bootstrap-original.interfaces"
        try:
            existing = read_root_file(path, MAX_CONFIG, private=True)[0]
        except FileNotFoundError:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(self.directory)
            return
        require(existing == raw, "Bootstrap backup differs from pinned original")

    @contextmanager
    def locked(self):
        secure_parents(self.directory)
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = self.directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
                "State directory must be root-owned with mode 0700")
        fd = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o600, "Invalid guest lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


class SystemKernel:
    route_protocols = ("boot", 3)

    def __init__(self, settings):
        self.settings = settings

    def run(self, *argv, allowed=(0,)):
        try:
            result = subprocess.run(argv, env=ENV, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ManagedGuestError("A fixed network command could not complete") from None
        require(result.returncode in allowed and len(result.stdout.encode()) <= MAX_CONFIG,
                "A fixed network command failed")
        if argv[:2] == ("/usr/bin/systemctl", "list-unit-files") and result.returncode == 1:
            require(not result.stdout.strip() and not result.stderr.strip(),
                    "A source-unit inventory failure cannot be treated as an empty list")
        return result.stdout.strip()

    def ip(self, *arguments):
        value = json.loads(self.run("/usr/sbin/ip", "-j", "-4", *arguments), object_pairs_hook=unique_object)
        require(isinstance(value, list), "Unexpected iproute2 output")
        return value

    def manager(self):
        require(self.run("/usr/bin/systemctl", "is-active", "networking", allowed=(0, 3, 4)) == "active",
                "ifupdown networking service is not active")
        self.run("/usr/sbin/ifquery", "--state", self.settings.interface)
        require(self.run("/usr/bin/systemctl", "is-active", "NetworkManager", allowed=(0, 3, 4))
                in ("inactive", "failed", "unknown"), "NetworkManager may manage the pinned interface")
        networkd = self.run("/usr/bin/systemctl", "is-active", "systemd-networkd", allowed=(0, 3, 4))
        require(networkd in ("active", "inactive", "failed", "unknown"), "Unknown networkd state")
        if networkd == "active":
            rows = self.run("/usr/bin/networkctl", "--no-pager", "--no-legend", "list", self.settings.interface).splitlines()
            fields = rows[0].split() if len(rows) == 1 else []
            require(len(fields) == 5 and fields[1] == self.settings.interface and fields[-1] == "unmanaged",
                    "networkd is managing the pinned interface or returned an unknown state")
        # Universal REALITY installations own a separate boot-time route writer.
        # A new interfaces journal must not claim persistence while that old
        # writer can restore its immutable, previous IP on the next boot. Its
        # deliberate ownership-verified handoff is an enrollment step, not a
        # side effect of a runtime readiness check.
        units = self.run("/usr/bin/systemctl", "list-unit-files", "--no-pager", "--no-legend",
                         "spn-reality-source-*.service", allowed=(0, 1))
        for line in units.splitlines():
            fields = line.split()
            require(len(fields) in (2, 3)
                    and re.fullmatch(r"spn-reality-source-[0-9a-f]{32}\.service", fields[0])
                    and fields[1] in ("disabled", "masked"),
                    "An enabled or unknown legacy preferred-source unit prevents network handoff")

    def snapshot(self):
        self.manager()
        rules = self.ip("rule", "show")
        require(len(rules) == 3 and all(row.keys() <= {"priority", "src", "table", "protocol"}
                and row.get("src") == "all" for row in rules)
                and [(row.get("priority"), row.get("table")) for row in rules]
                == [(0, "local"), (32766, "main"), (32767, "default")], "Unexpected policy routing")
        links = self.ip("address", "show", "scope", "global")
        targets = [row for row in links if row.get("ifname") == self.settings.interface]
        require(len(targets) == 1 and "UP" in targets[0].get("flags", []), "Pinned interface is not uniquely UP")
        addresses = []
        for row in targets[0].get("addr_info", []):
            if row.get("family") != "inet":
                continue
            address = ipv4(row.get("local"))
            require(row.get("prefixlen") == 32, "Only public /32 IPv4 assignments are supported")
            addresses.append(address)
        require(1 <= len(addresses) <= MAX_IPS and len(set(addresses)) == len(addresses), "Unexpected interface IPv4 set")
        for link in links:
            if link.get("ifname") != self.settings.interface:
                require(not any(row.get("family") == "inet" and row.get("local") in addresses
                                for row in link.get("addr_info", [])), "Managed IPv4 is assigned on another interface")
        routes = self.ip("route", "show", "table", "main", "default")
        require(len(routes) == 1, "Expected one main default route")
        route = routes[0]
        require(route.keys() <= {"dst", "dev", "gateway", "flags", "prefsrc", "protocol", "scope", "type", "table"}
                and route.get("dst") == "default" and route.get("dev") == self.settings.interface
                and route.get("gateway") == self.settings.gateway and route.get("flags") == ["onlink"]
                and route.get("type", "unicast") == "unicast" and route.get("table", "main") in ("main", 254),
                "Default route differs from the pinned simple onlink route")
        for field in ("protocol", "scope"):
            require(field not in route or isinstance(route[field], (str, int)) and not isinstance(route[field], bool)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(route[field])), "Unexpected route attribute")
        require(route.get("protocol", "boot") in self.route_protocols
                and route.get("scope", "global") in ("global", 0),
                "Only the boot/global default route recreated by the managed post-up is supported")
        if "prefsrc" in route:
            ipv4(route["prefsrc"])
        selected = self.ip("route", "get", "1.1.1.1")
        require(len(selected) == 1 and selected[0].get("dev") == self.settings.interface
                and selected[0].get("gateway") == self.settings.gateway, "Route lookup escaped the pinned route")
        source = ipv4(selected[0].get("prefsrc") or selected[0].get("src"))
        require(source in addresses and ("prefsrc" not in route or route["prefsrc"] == source), "Unexpected selected source")
        return {"ips": sorted(addresses), "route": deepcopy(route), "source": source}

    def add(self, address):
        self.run("/usr/sbin/ip", "-4", "address", "add", ipv4(address) + "/32", "dev", self.settings.interface)

    def remove(self, address):
        self.run("/usr/sbin/ip", "-4", "address", "del", ipv4(address) + "/32", "dev", self.settings.interface)

    def route(self, before, source):
        latest = self.snapshot()
        require(latest["route"] == before and source in latest["ips"], "Default route changed before replacement")
        arguments = ["/usr/sbin/ip", "-4", "route", "replace", "default", "via", self.settings.gateway,
                     "dev", self.settings.interface]
        for field, option in (("protocol", "proto"), ("scope", "scope")):
            if field in before:
                arguments.extend([option, str(before[field])])
        arguments.extend(["onlink", "src", ipv4(source)])
        self.run(*arguments)


def with_source(route, source):
    return {**route, "prefsrc": source}


def same_allocation(rows, reference):
    matches = [row for row in rows if row["id"] == reference["id"] or row["ip"] == reference["ip"]]
    require(not matches or matches == [reference], "Allocation ID or address has changed ownership")
    return bool(matches)


class ManagedGuest:
    def __init__(self, settings, store, kernel):
        self.settings, self.store, self.kernel = settings, store, kernel

    def render(self, raw, owned, active):
        return canonical_interfaces(raw, self.settings, owned, active)

    def validate_request(self, request):
        require(isinstance(request, dict) and request.get("server_id") == self.settings.server_id,
                "Request does not identify the pinned VPS")
        operation = request.get("operation")
        fields = {
            "check": {"operation", "server_id"},
            "bootstrap": {"operation", "server_id", "apply"},
            "prepare": {"operation", "server_id", "id", "ip"},
            "activate": {"operation", "server_id", "old", "new"},
            "deprovision": {"operation", "server_id", "id", "ip", "billing_confirmed"},
        }
        require(operation in fields and set(request) == fields[operation], "Unsupported guest request")
        if operation == "check":
            pass
        elif operation == "bootstrap":
            require(type(request["apply"]) is bool, "Bootstrap apply must be boolean")
        elif operation in ("prepare", "deprovision"):
            allocation({"id": request["id"], "ip": request["ip"]})
            if operation == "deprovision":
                require(request["billing_confirmed"] is True, "Billing removal must be confirmed before deprovision")
        else:
            old, new = allocation(request["old"]), allocation(request["new"])
            require(old["id"] != new["id"] and old["ip"] != new["ip"], "Activation needs distinct old and new allocations")
        return operation

    def validate_state(self, state):
        expected = {"version", "server_id", "interface", "gateway", "active", "allocations", "retired",
                    "config_sha256", "route", "bootstrap_original_sha256", "pending"}
        require(isinstance(state, dict) and set(state) == expected and type(state["version"]) is int and state["version"] == 1
                and state["server_id"] == self.settings.server_id and state["interface"] == self.settings.interface
                and state["gateway"] == self.settings.gateway, "Guest journal does not match the pinned VPS")
        rows, active = allocations(state["allocations"]), allocation(state["active"])
        require(active in rows and isinstance(state["retired"], list) and len(state["retired"]) <= 256,
                "Invalid active or retired allocation state")
        for row in state["retired"]:
            allocation(row)
        require(isinstance(state["route"], dict) and all(isinstance(state[name], str)
                and re.fullmatch(r"[0-9a-f]{64}", state[name])
                for name in ("config_sha256", "bootstrap_original_sha256"))
                and state["bootstrap_original_sha256"] == self.settings.bootstrap["expected_config_sha256"],
                "Invalid guest snapshot state")

    def stable_snapshot(self, state, *, missing=None):
        current = self.kernel.snapshot()
        expected = {row["ip"] for row in state["allocations"]}
        permitted = [expected]
        if missing is not None:
            permitted.append(expected - {missing})
        require(set(current["ips"]) in permitted, "Live IPv4 set drifted from owned allocations")
        require(current["route"] in (state["route"], with_source(state["route"], state["active"]["ip"]))
                and current["source"] == state["active"]["ip"], "Default route or source changed outside this journal")
        require(digest(self.store.config()) == state["config_sha256"], "Interfaces changed outside this journal")
        return current

    def run(self, request):
        operation = self.validate_request(request)
        state = self.store.state()
        if state is not None:
            self.validate_state(state)
            if state["pending"] is not None:
                require(operation != "check", "A pending guest operation prevents readiness confirmation")
                require(request.get("apply", True) is not False, "A pending operation requires its exact apply request")
                return self.resume(state, request)
        if operation == "check":
            require(state is not None, "Bootstrap the pinned interface before readiness checks")
            self.stable_snapshot(state)
            return {"ok": True}
        if operation == "bootstrap":
            if state is not None:
                self.stable_snapshot(state)
                return {"ok": True, "operation": operation, "already_bootstrapped": True}
            raw = self.store.config()
            live = self.kernel.snapshot()
            boot = self.settings.bootstrap
            require(digest(raw) == boot["expected_config_sha256"]
                    and set(live["ips"]) == ip_set(boot["expected_live_ipv4"])
                    and live["source"] == boot["active"]["ip"], "Bootstrap no longer matches its exact pinned snapshot")
            state = {"version": 1, "server_id": self.settings.server_id, "interface": self.settings.interface,
                     "gateway": self.settings.gateway, "active": deepcopy(boot["active"]),
                     "allocations": deepcopy(boot["allocations"]), "retired": [], "config_sha256": digest(raw),
                     "route": deepcopy(live["route"]), "bootstrap_original_sha256": digest(raw), "pending": None}
            after = self.render(raw, state["allocations"], state["active"])
            result = {"ok": True, "operation": operation, "planned": not request["apply"],
                      "before_config_sha256": digest(raw), "after_config_sha256": digest(after),
                      "active_ip": state["active"]["ip"], "owned_ips": sorted(live["ips"])}
            if not request["apply"]:
                return result
            self.store.backup(raw)
        else:
            require(state is not None, "Bootstrap the pinned interface before runtime operations")
            missing = request["ip"] if operation == "deprovision" else None
            live = self.stable_snapshot(state, missing=missing)
            raw = self.store.config()
            if operation == "prepare":
                new = allocation({"id": request["id"], "ip": request["ip"]})
                if same_allocation(state["allocations"], new):
                    return {"ok": True, "operation": operation, "already_prepared": True}
                require(len(state["allocations"]) < MAX_IPS, "Ten-address guest limit reached")
            elif operation == "activate":
                old, new = allocation(request["old"]), allocation(request["new"])
                require(same_allocation(state["allocations"], new), "Replacement is not prepared")
                if state["active"] == new:
                    return {"ok": True, "operation": operation, "already_active": True}
                require(state["active"] == old and same_allocation(state["allocations"], old), "Active allocation changed")
            else:
                old = allocation({"id": request["id"], "ip": request["ip"]})
                require(state["active"] != old and state["active"]["ip"] != old["ip"], "Cannot deprovision the active address")
                if not same_allocation(state["allocations"], old):
                    require(old in state["retired"], "Address is not owned or previously retired")
                    return {"ok": True, "operation": operation, "already_deprovisioned": True}
            after = None
        destination = deepcopy(state)
        if operation == "prepare":
            destination["allocations"].append(new)
            destination["retired"] = [row for row in state["retired"] if row["id"] != new["id"] and row["ip"] != new["ip"]]
        elif operation == "activate":
            destination["active"] = new
        elif operation == "deprovision":
            destination["allocations"].remove(old)
            destination["retired"] = (destination["retired"] + [old])[-256:]
        if after is None:
            after = self.render(raw, destination["allocations"], destination["active"])
        destination["config_sha256"] = digest(after)
        destination["route"] = (with_source(live["route"], destination["active"]["ip"])
                                if operation == "activate" else deepcopy(live["route"]))
        destination["pending"] = None
        state["pending"] = {"request": deepcopy(request), "request_sha256": digest(encoded(request)),
                            "config_before": raw.decode(), "config_before_sha256": digest(raw),
                            "config_after": after.decode(), "config_after_sha256": digest(after),
                            "live_before": deepcopy(live), "destination": destination}
        self.store.save(state)  # Durable intent must precede every interfaces/live mutation.
        return self.resume(state, request)

    def resume(self, state, request):
        pending = state["pending"]
        expected = {"request", "request_sha256", "config_before", "config_before_sha256", "config_after",
                    "config_after_sha256", "live_before", "destination"}
        require(isinstance(pending, dict) and set(pending) == expected
                and pending["request"] == request and pending["request_sha256"] == digest(encoded(request)),
                "Another guest operation is pending; replay its exact request")
        operation = self.validate_request(request)
        destination = pending["destination"]
        self.validate_state(destination)
        require(destination["pending"] is None and destination["bootstrap_original_sha256"] == state["bootstrap_original_sha256"],
                "Invalid pending destination")
        before_raw, after_raw = pending["config_before"].encode(), pending["config_after"].encode()
        require(digest(before_raw) == pending["config_before_sha256"] == state["config_sha256"]
                and digest(after_raw) == pending["config_after_sha256"] == destination["config_sha256"],
                "Pending configuration hashes disagree")
        require(self.render(before_raw, destination["allocations"], destination["active"]) == after_raw,
                "Pending configuration is not the authorized render")
        before_route = pending["live_before"]["route"]
        after_route = destination["route"]
        before_ips = set(pending["live_before"]["ips"])
        after_ips = {row["ip"] for row in destination["allocations"]}
        owned_before = {row["ip"] for row in state["allocations"]}
        permitted_before = [owned_before]
        expected_owned = deepcopy(state["allocations"])
        expected_active = state["active"]
        if operation == "prepare":
            new = allocation({"id": request["id"], "ip": request["ip"]})
            require(not same_allocation(expected_owned, new), "Pending preparation already existed")
            expected_owned.append(new)
        elif operation == "activate":
            old, new = allocation(request["old"]), allocation(request["new"])
            require(state["active"] == old and same_allocation(expected_owned, old)
                    and same_allocation(expected_owned, new), "Pending activation allocation mismatch")
            expected_active = new
        elif operation == "deprovision":
            old = allocation({"id": request["id"], "ip": request["ip"]})
            require(old != state["active"] and same_allocation(expected_owned, old), "Pending deprovision allocation mismatch")
            expected_owned.remove(old)
            permitted_before.append(owned_before - {old["ip"]})
        require(before_ips in permitted_before and pending["live_before"]["source"] == state["active"]["ip"]
                and before_route in (state["route"], with_source(state["route"], state["active"]["ip"]))
                and destination["allocations"] == expected_owned and destination["active"] == expected_active
                and after_route == (with_source(before_route, expected_active["ip"])
                                    if operation == "activate" else before_route),
                "Pending transition does not match the owned snapshot and exact request")
        allowed_routes = (before_route, with_source(before_route, state["active"]["ip"]),
                          after_route, with_source(after_route, destination["active"]["ip"]))

        def observe():
            live = self.kernel.snapshot()
            current_hash = digest(self.store.config())
            require(current_hash in (pending["config_before_sha256"], pending["config_after_sha256"]),
                    "Interfaces changed concurrently during recovery")
            require(set(live["ips"]) in (before_ips, after_ips), "Live IPv4 set changed concurrently during recovery")
            require(live["route"] in allowed_routes, "Default route changed concurrently during recovery")
            expected_source = live["route"].get("prefsrc") or pending["live_before"]["source"]
            require(live["source"] == expected_source, "Selected source changed concurrently during recovery")
            return live, current_hash

        live, current_hash = observe()
        if current_hash != pending["config_after_sha256"]:
            self.store.write_config(pending["config_before_sha256"], after_raw)
        live, _ = observe()
        if operation == "prepare" and request["ip"] not in live["ips"]:
            self.kernel.add(request["ip"])
        elif operation == "activate" and live["route"] != after_route:
            self.kernel.route(live["route"], destination["active"]["ip"])
        elif operation == "deprovision" and request["ip"] in live["ips"]:
            self.kernel.remove(request["ip"])
        live, current_hash = observe()
        require(current_hash == destination["config_sha256"] and set(live["ips"]) == after_ips
                and live["source"] == destination["active"]["ip"], "Guest transition readback failed")
        if operation == "activate":
            require(live["route"] == after_route, "Activation route readback failed")
        else:
            require(live["route"] in (after_route, with_source(after_route, destination["active"]["ip"])),
                    "Operation unexpectedly changed the default route")
        destination["route"] = deepcopy(live["route"])
        self.store.save(destination)
        return {"ok": True, "operation": operation, "active_ip": destination["active"]["ip"], "owned_ips": sorted(after_ips)}


def interrupted(_number, _frame):
    raise InterruptedError("Managed guest operation interrupted")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        require(sys.platform.startswith("linux") and os.geteuid() == 0, "Managed guest requires Linux root")
        raw, _ = read_root_file(args.config, MAX_CONFIG, private=True)
        settings = Settings.from_value(json.loads(raw, object_pairs_hook=unique_object))
        request_raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        require(0 < len(request_raw) <= MAX_REQUEST, "Invalid guest request size")
        request = json.loads(request_raw, object_pairs_hook=unique_object)
        store = FileStore(settings)
        guest = ManagedGuest(settings, store, SystemKernel(settings))
        operation = guest.validate_request(request)
        if operation == "check" or operation == "bootstrap" and request["apply"] is False:
            result = guest.run(request)
        else:
            with store.locked():
                result = guest.run(request)
        print(json.dumps(result if operation == "bootstrap" else {"ok": True}, separators=(",", ":")))
        return 0
    except ManagedGuestError as exc:
        print("Managed guest transition unconfirmed: " + str(exc), file=sys.stderr)
        print('{"ok":false}')
        return 1
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, argparse.ArgumentError, KeyboardInterrupt):
        print("Managed guest transition unconfirmed; network/configuration details suppressed.", file=sys.stderr)
        print('{"ok":false}')
        return 1


if __name__ == "__main__":
    for stop_signal in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(stop_signal, interrupted)
    raise SystemExit(main())
