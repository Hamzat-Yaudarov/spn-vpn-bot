"""Pinned static Netplan/networkd adapter; never applies or restarts networking.

Only one Ethernet definition, public /32 IPv4 assignments and one on-link
IPv4 default route are supported. IPv6 and all accepted non-IPv4 settings
are preserved. Candidate files are compiled under a private --root-dir before
the shared durable journal can replace the real YAML.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv6Interface, ip_address
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile

from . import managed_guest as base


CLOUD_CONFIG = Path("/etc/cloud/cloud.cfg.d/99-spn-ip-failover-network.cfg")
CLOUD_DISABLED = b"network:\n  config: disabled\ndisable_network_activation: true\n"
YAML_PATH = re.compile(r"/etc/netplan/[A-Za-z0-9_.-]+\.ya?ml")
MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


@dataclass(frozen=True)
class Settings:
    server_id: str
    interface: str
    gateway: str
    interfaces_file: Path
    state_directory: Path
    bootstrap: dict
    netplan_guard: dict

    @classmethod
    def from_value(cls, value):
        base.require(isinstance(value, dict) and value.get("method") == "netplan"
                     and set(value) == {"version", "server_id", "interface", "gateway", "method",
                                       "interfaces_file", "state_directory", "bootstrap", "netplan_guard"},
                     "Unsupported pinned Netplan configuration")
        common = {key: item for key, item in value.items() if key != "netplan_guard"}
        common["method"] = "ifupdown"
        parsed = base.Settings.from_value(common)
        base.require(YAML_PATH.fullmatch(str(parsed.interfaces_file)), "Netplan YAML must be explicitly pinned")
        guard = value["netplan_guard"]
        base.require(isinstance(guard, dict) and set(guard) == {
            "macaddress", "generated_network_sha256", "networkd_inputs", "cloud_init_file_sha256"},
            "Netplan ownership guard is incomplete")
        base.require(isinstance(guard["macaddress"], str) and MAC.fullmatch(guard["macaddress"]),
                     "Pin the physical interface MAC")
        for key in ("generated_network_sha256", "cloud_init_file_sha256"):
            base.require(isinstance(guard[key], str) and re.fullmatch(r"[0-9a-f]{64}", guard[key]),
                         "Invalid Netplan guard hash")
        base.require(guard["cloud_init_file_sha256"] == base.digest(CLOUD_DISABLED),
                     "Cloud-init network ownership must be explicitly disabled")
        inputs = guard["networkd_inputs"]
        base.require(isinstance(inputs, dict) and len(inputs) <= 128, "Invalid networkd input manifest")
        for path, digest in inputs.items():
            base.require(isinstance(path, str) and networkd_path(Path(path))
                         and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
                         "Invalid pinned networkd input")
        return cls(parsed.server_id, parsed.interface, parsed.gateway, parsed.interfaces_file,
                   parsed.state_directory, parsed.bootstrap, deepcopy(guard))


def yaml_module():
    try:
        import yaml
        return yaml
    except ImportError:
        raise base.ManagedGuestError("The installed Netplan Python YAML dependency is missing") from None


def parse_yaml(raw):
    base.require(isinstance(raw, bytes) and 0 < len(raw) <= base.MAX_CONFIG and b"\x00" not in raw,
                 "Invalid bounded Netplan YAML")
    yaml = yaml_module()

    class StrictLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                base.require(isinstance(key, str) and key not in result, "Duplicate/non-string YAML key")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        text = raw.decode("utf-8")
        base.require(all(not isinstance(event, (yaml.AliasEvent,)) and getattr(event, "anchor", None) is None
                         for event in yaml.parse(text)), "YAML anchors/aliases are unsupported")
        value = yaml.load(text, Loader=StrictLoader)
    except (UnicodeError, yaml.YAMLError):
        raise base.ManagedGuestError("Invalid safe Netplan YAML") from None
    return value


def definition(raw, settings):
    value = parse_yaml(raw)
    base.require(isinstance(value, dict) and set(value) == {"network"}, "Expected one Netplan network document")
    network = value["network"]
    base.require(isinstance(network, dict) and set(network) <= {"version", "renderer", "ethernets"}
                 and type(network.get("version")) is int and network["version"] == 2
                 and network.get("renderer", "networkd") == "networkd", "Only networkd Netplan version 2 is supported")
    ethernets = network.get("ethernets")
    base.require(isinstance(ethernets, dict) and set(ethernets) == {settings.interface},
                 "Only the explicitly pinned Ethernet definition is supported")
    eth = ethernets[settings.interface]
    allowed = {"addresses", "dhcp4", "dhcp6", "match", "set-name", "nameservers", "optional",
               "wakeonlan", "mtu", "accept-ra", "ipv6-privacy", "link-local", "routes", "renderer", "critical"}
    base.require(isinstance(eth, dict) and set(eth) <= allowed and eth.get("dhcp4") is False
                 and eth.get("renderer", "networkd") == "networkd"
                 and eth.get("set-name") == settings.interface
                 and eth.get("match") == {"macaddress": settings.netplan_guard["macaddress"]},
                 "Expected a static Ethernet definition matched to the pinned MAC")
    for name in ("dhcp6", "optional", "wakeonlan", "accept-ra", "ipv6-privacy", "critical"):
        base.require(name not in eth or type(eth[name]) is bool, "Invalid Ethernet boolean")
    base.require("mtu" not in eth or type(eth["mtu"]) is int and 576 <= eth["mtu"] <= 65535,
                 "Invalid Ethernet MTU")
    base.require("link-local" not in eth or isinstance(eth["link-local"], list)
                 and all(item in ("ipv4", "ipv6") for item in eth["link-local"]), "Invalid link-local configuration")
    addresses = eth.get("addresses")
    base.require(isinstance(addresses, list) and 1 <= len(addresses) <= 128
                 and all(isinstance(item, str) for item in addresses) and len(set(addresses)) == len(addresses),
                 "Expected a bounded unique address list")
    v4, v6 = [], []
    for item in addresses:
        try:
            if ":" in item:
                IPv6Interface(item)
                v6.append(item)
            else:
                parsed = IPv4Interface(item)
                base.require(str(parsed) == item and parsed.network.prefixlen == 32,
                             "Managed Netplan IPv4 assignments must be canonical /32")
                v4.append(base.ipv4(str(parsed.ip)))
        except ValueError:
            raise base.ManagedGuestError("Invalid Netplan interface address") from None
    base.require(1 <= len(v4) <= base.MAX_IPS, "Expected one to ten public Netplan IPv4 addresses")
    routes = eth.get("routes")
    base.require(isinstance(routes, list) and 1 <= len(routes) <= 32, "Expected bounded static routes")
    defaults = []
    for index, route in enumerate(routes):
        base.require(isinstance(route, dict) and isinstance(route.get("via"), str), "Invalid static route")
        gateway = ip_address(route["via"])
        if isinstance(gateway, IPv4Address):
            base.require(set(route) <= {"to", "via", "on-link", "from"}
                         and route.get("to") in ("default", "0.0.0.0/0")
                         and route["via"] == settings.gateway and route.get("on-link") is True,
                         "Only one pinned on-link IPv4 default route is supported")
            base.require("from" not in route or route["from"] in v4, "Unexpected preferred IPv4 source")
            defaults.append(index)
        else:
            # The native generator validates IPv6 route options. Never replace
            # or reinterpret them during IPv4 rendering.
            base.require(all(isinstance(key, str) for key in route), "Invalid IPv6 route field")
    base.require(len(defaults) == 1, "Expected exactly one IPv4 default route")
    nameservers = eth.get("nameservers", {})
    base.require(isinstance(nameservers, dict) and set(nameservers) <= {"addresses", "search"},
                 "Unexpected resolver configuration")
    return value, eth, defaults[0], v4, v6


def canonical_netplan(raw, settings, owned, active):
    owned, active = base.allocations(owned), base.allocation(active)
    base.require(active in owned, "Active Netplan IPv4 is not owned")
    value, eth, route_index, _v4, v6 = definition(raw, settings)
    eth["addresses"] = [active["ip"] + "/32"] + [row["ip"] + "/32" for row in sorted(
        (row for row in owned if row != active), key=lambda row: int(IPv4Address(row["ip"])))] + v6
    eth["routes"][route_index]["from"] = active["ip"]
    result = yaml_module().safe_dump(value, sort_keys=False, allow_unicode=True).encode()
    base.require(len(result) <= base.MAX_CONFIG, "Rendered Netplan YAML exceeds size limit")
    definition(result, settings)
    return result


def networkd_path(path):
    bases = (Path("/etc/systemd/network"), Path("/run/systemd/network"), Path("/usr/lib/systemd/network"))
    return (path.is_absolute() and ".." not in path.parts
            and (path.parent in bases and path.suffix == ".network"
                 or path.parent.parent in bases and path.suffix == ".conf"
                 and path.parent.name.endswith(".network.d")))


def networkd_manifest(interface):
    expected = Path("/run/systemd/network/10-netplan-" + interface + ".network")
    result = {}
    for directory in (Path("/etc/systemd/network"), Path("/run/systemd/network"), Path("/usr/lib/systemd/network")):
        for path in sorted([*directory.glob("*.network"), *directory.glob("*.network.d/*.conf")]):
            base.require(len(result) < 128 and networkd_path(path), "Unbounded networkd input inventory")
            if path == expected:
                continue
            result[str(path)] = base.digest(base.read_root_file(path, base.MAX_CONFIG, private=False)[0])
    return result


def yaml_inventory():
    result = []
    for directory in (Path("/lib/netplan"), Path("/etc/netplan"), Path("/run/netplan")):
        for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
            base.require(len(result) < 32, "Unexpected Netplan input inventory")
            base.read_root_file(path, base.MAX_CONFIG, private=False)
            result.append(path)
    return result


def cloud_guard():
    cloud_kernel_guard()
    raw, _ = base.read_root_file(CLOUD_CONFIG, base.MAX_CONFIG, private=True)
    base.require(raw == CLOUD_DISABLED, "Cloud-init network renderer/activation is not explicitly handed off")
    cloud_order()
    return base.digest(raw)


def cloud_order():
    """A plan must check future precedence even before the marker exists."""
    configs = sorted({*CLOUD_CONFIG.parent.glob("*.cfg"), CLOUD_CONFIG})
    base.require(configs and configs[-1] == CLOUD_CONFIG, "A later cloud-init configuration may override network ownership")


def cloud_kernel_guard():
    # Cloud-init documents kernel networking as higher precedence than system
    # cloud.cfg files. Never claim ownership if boot arguments can override it.
    raw = Path("/proc/cmdline").read_text()
    base.require(len(raw) <= base.MAX_CONFIG and not any(
        word.startswith("ip=") or word.startswith("network-config=") and word != "network-config=disabled"
        for word in raw.split()), "Kernel boot arguments override cloud-init network ownership")


def capture_guard(interface, yaml_path, *, enrollment=False):
    """Read-only guard inventory after the explicit cloud-init ownership step."""
    base.require(re.fullmatch(r"[A-Za-z0-9_.-]{1,12}", interface), "Invalid interface")
    base.require(yaml_inventory() == [Path(yaml_path)], "Netplan requires one pinned YAML input")
    raw, _ = base.read_root_file(Path(yaml_path), base.MAX_CONFIG, private=True)
    value = parse_yaml(raw)
    mac = value["network"]["ethernets"][interface]["match"]["macaddress"]
    base.require(isinstance(mac, str) and MAC.fullmatch(mac), "Missing exact interface MAC")
    generated = Path("/run/systemd/network/10-netplan-" + interface + ".network")
    if enrollment:
        cloud_kernel_guard()
        cloud_order()
        try:
            cloud_hash = cloud_guard()
        except FileNotFoundError:
            cloud_hash = base.digest(CLOUD_DISABLED)
    else:
        cloud_hash = cloud_guard()
    return {"macaddress": mac, "generated_network_sha256": base.digest(base.read_root_file(
        generated, base.MAX_CONFIG, private=False)[0]), "networkd_inputs": networkd_manifest(interface),
            "cloud_init_file_sha256": cloud_hash}


def sections(raw):
    result, current = [], None
    try:
        lines = raw.decode().splitlines()
    except UnicodeError:
        raise base.ManagedGuestError("Invalid generated networkd text") from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if re.fullmatch(r"\[[A-Za-z0-9]+\]", line):
            current = (line[1:-1], [])
            result.append(current)
        else:
            base.require(current is not None and "=" in line, "Unknown generated networkd syntax")
            current[1].append(tuple(line.split("=", 1)))
    return result


def verify_generated(raw, yaml_raw, settings):
    _value, eth, route_index, v4, v6 = definition(yaml_raw, settings)
    groups = sections(raw)
    matches = [dict(fields) for name, fields in groups if name == "Match"]
    base.require(len(matches) == 1 and matches[0].get("Name") == settings.interface
                 and (matches[0].get("PermanentMACAddress") or matches[0].get("MACAddress"))
                 == settings.netplan_guard["macaddress"], "Generated match differs from the pinned interface")
    addresses = [value for name, fields in groups if name == "Network" for key, value in fields if key == "Address"]
    base.require(set(addresses) == {ip + "/32" for ip in v4} | set(v6) and len(addresses) == len(v4) + len(v6),
                 "Generated address set differs from Netplan")
    routes = [dict(fields) for name, fields in groups if name == "Route"
              and any(key == "Gateway" and ":" not in value for key, value in fields)]
    source = eth["routes"][route_index].get("from")
    base.require(len(routes) == 1 and routes[0].get("Destination") in ("0.0.0.0/0", "default")
                 and routes[0].get("Gateway") == settings.gateway and routes[0].get("GatewayOnLink") == "true"
                 and routes[0].get("PreferredSource") == source,
                 "Generated IPv4 default/preferred source differs from Netplan")
    base.require(not any(name in ("RoutingPolicyRule", "Bridge", "Bond") for name, _fields in groups),
                 "Unexpected generated policy or virtual interface")


def generate_candidate(raw, settings):
    """Compile only inside a private alternate root. No live generate/apply."""
    definition(raw, settings)
    # /tmp is intentionally world-writable and cannot satisfy read_root_file's
    # ancestor checks. /run is root-controlled; the child still has mode 0700.
    base.secure_parents(Path("/run/spn-netplan-validation"))
    with tempfile.TemporaryDirectory(prefix="spn-netplan-validation-", dir="/run") as directory:
        root = Path(directory)
        target = root / "etc/netplan" / settings.interfaces_file.name
        target.parent.mkdir(parents=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        try:
            result = subprocess.run(["/usr/sbin/netplan", "generate", "--root-dir", str(root)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=base.ENV, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise base.ManagedGuestError("Isolated Netplan validation did not finish") from None
        base.require(result.returncode == 0, "Isolated Netplan validation rejected the candidate")
        network = root / ("run/systemd/network/10-netplan-" + settings.interface + ".network")
        outputs = list((root / "run/systemd/network").glob("*.network"))
        base.require(outputs == [network], "Netplan generated unexpected interface definitions")
        generated, _ = base.read_root_file(network, base.MAX_CONFIG, private=False)
        verify_generated(generated, raw, settings)
        return generated


def generation_history(settings):
    path = settings.state_directory / "netplan-generated-history.json"
    try:
        raw, _ = base.read_root_file(path, base.MAX_CONFIG, private=True)
    except FileNotFoundError:
        return None, []
    value = json.loads(raw, object_pairs_hook=base.unique_object)
    base.require(isinstance(value, dict) and set(value) == {"format", "service_id", "sha256s"}
                 and value["format"] == 1 and value["service_id"] == settings.server_id
                 and isinstance(value["sha256s"], list) and 1 <= len(value["sha256s"]) <= 8
                 and len(set(value["sha256s"])) == len(value["sha256s"])
                 and all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
                         for item in value["sha256s"]), "Invalid owned Netplan generation history")
    return raw, value["sha256s"]


class FileStore(base.FileStore):
    def write_config(self, expected, raw):
        current = self.config()
        base.require(base.digest(current) == expected, "Netplan YAML changed before validation")
        next_hash = base.digest(generate_candidate(raw, self.settings))
        current_hash = base.digest(generate_candidate(current, self.settings))
        live_path = Path("/run/systemd/network/10-netplan-" + self.settings.interface + ".network")
        live_hash = base.digest(base.read_root_file(live_path, base.MAX_CONFIG, private=False)[0])
        previous, history = generation_history(self.settings)
        base.require(live_hash in {*history, current_hash, self.settings.netplan_guard["generated_network_sha256"]},
                     "Running networkd generation is not owned")
        # A second rotation may occur after a reboot, or many may occur without
        # reloading networkd. Keep the exact currently generated file plus the
        # committed and next YAML renders. Older, non-running generations can
        # be discarded; a crash before/after YAML replacement remains resumable.
        hashes = sorted({live_hash, current_hash, next_hash})
        value = {"format": 1, "service_id": self.settings.server_id, "sha256s": hashes}
        path = self.settings.state_directory / "netplan-generated-history.json"

        def unchanged():
            latest, _hashes = generation_history(self.settings)
            base.require(latest == previous, "Netplan generation history changed concurrently")

        base.atomic_write(path, base.encoded(value), 0o600, before_replace=unchanged)
        super().write_config(expected, raw)


class SystemKernel(base.SystemKernel):
    route_protocols = ("static", 4)

    def manager(self):
        for name in ("networking", "NetworkManager"):
            base.require(self.run("/usr/bin/systemctl", "is-active", name, allowed=(0, 3, 4))
                         in ("inactive", "failed", "unknown"), "Another network manager may own the interface")
        base.require(self.run("/usr/bin/systemctl", "is-active", "systemd-networkd", allowed=(0, 3, 4)) == "active",
                     "The pinned Netplan networkd renderer is not active")
        rows = self.run("/usr/bin/networkctl", "--no-pager", "--no-legend", "list", self.settings.interface).splitlines()
        fields = rows[0].split() if len(rows) == 1 else []
        base.require(len(fields) == 5 and fields[1] == self.settings.interface and fields[-1] == "configured",
                     "networkd did not confirm the configured interface")
        status_text = self.run("/usr/bin/networkctl", "--no-pager", "--no-legend", "status", self.settings.interface)
        network_path = "/run/systemd/network/10-netplan-" + self.settings.interface + ".network"
        found = re.findall(r"(?m)^\s*Network File:\s*(\S+)\s*$", status_text)
        base.require(found == [network_path], "The interface uses an unpinned networkd definition")
        base.require(yaml_inventory() == [self.settings.interfaces_file]
                     and networkd_manifest(self.settings.interface) == self.settings.netplan_guard["networkd_inputs"],
                     "Netplan/networkd inputs changed outside the journal")
        base.require(cloud_guard() == self.settings.netplan_guard["cloud_init_file_sha256"],
                     "Cloud-init network ownership changed")
        mac = Path("/sys/class/net") / self.settings.interface / "address"
        base.require(mac.read_text().strip() == self.settings.netplan_guard["macaddress"], "Physical MAC changed")
        units = self.run("/usr/bin/systemctl", "list-unit-files", "--no-pager", "--no-legend",
                         "spn-reality-source-*.service", allowed=(0, 1))
        for line in units.splitlines():
            fields = line.split()
            base.require(len(fields) in (2, 3)
                         and re.fullmatch(r"spn-reality-source-[0-9a-f]{32}\.service", fields[0])
                         and fields[1] in ("disabled", "masked"), "Legacy preferred source still owns boot persistence")
        raw, _ = base.read_root_file(self.settings.interfaces_file, base.MAX_CONFIG, private=True)
        generated = generate_candidate(raw, self.settings)
        live_generated, _ = base.read_root_file(Path(network_path), base.MAX_CONFIG, private=False)
        _history_raw, history = generation_history(self.settings)
        base.require(base.digest(live_generated) in {*history, self.settings.netplan_guard["generated_network_sha256"],
                                                     base.digest(generated)},
                     "Generated networkd configuration changed outside the journal")


class ManagedGuest(base.ManagedGuest):
    def render(self, raw, owned, active):
        return canonical_netplan(raw, self.settings, owned, active)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        base.require(sys.platform.startswith("linux") and os.geteuid() == 0, "Managed guest requires Linux root")
        raw, _ = base.read_root_file(args.config, base.MAX_CONFIG, private=True)
        settings = Settings.from_value(json.loads(raw, object_pairs_hook=base.unique_object))
        request_raw = sys.stdin.buffer.read(base.MAX_REQUEST + 1)
        base.require(0 < len(request_raw) <= base.MAX_REQUEST, "Invalid guest request size")
        request = json.loads(request_raw, object_pairs_hook=base.unique_object)
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
    except (Exception, KeyboardInterrupt):
        print("Pinned Netplan operation unconfirmed; private details suppressed.", file=sys.stderr)
        print('{"ok":false}')
        return 1


if __name__ == "__main__":
    for stop_signal in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(stop_signal, base.interrupted)
    raise SystemExit(main())
