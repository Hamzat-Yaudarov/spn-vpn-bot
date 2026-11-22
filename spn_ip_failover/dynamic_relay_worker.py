"""Outside-RF VPN checks through pinned relays whose IPs follow Remnawave.

The private configuration pins node UUIDs, hosting service UUIDs, SSH users and
key files. Only a relay's current literal public IPv4 comes from the panel.
HostKeyAlias is derived from the immutable service UUID, never from that IP.
The two scoped relays may explicitly use their fixed reverse-management
loopback transports, without replacing the logical public address or alias.
No credentials, Xray, commands or files are sent to an EU relay: relay_worker
keeps client configuration and Xray execution on this controller.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
from uuid import UUID

from . import relay_worker, worker
from .remna import Remnawave


MAX_RELAYS = 16
MAX_ROUTE_KEYS = 512
MAX_RELAY_ATTEMPTS = 2
DEADLINE_SECONDS = 35  # Leaves time for both process-group cleanups before Probe's 55s timeout.


def canonical_uuid(value):
    try:
        normalized = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Expected a pinned resource UUID") from None
    if value != normalized:
        raise ValueError("Resource UUID must be canonical")
    return normalized


def public_ipv4(value):
    if not isinstance(value, str):
        raise ValueError("Relay needs a literal public IPv4")
    address = IPv4Address(value)
    if str(address) != value or not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError("Relay needs a literal public IPv4")
    return value


@dataclass(frozen=True)
class Relay:
    node_uuid: str
    service_id: str
    user: str
    identity: str
    known_hosts: str
    ssh_port: int
    reverse_service: str | None = None

    def connection(self, address):
        address = public_ipv4(address)
        result = {
            "ssh_command": ["/usr/bin/ssh", "-i", self.identity, "-o",
                            "UserKnownHostsFile=" + self.known_hosts, "-o",
                            "HostKeyAlias=spn-ip-rotation-relay-" + self.service_id,
                            "-p", str(self.ssh_port)],
            "host": self.user + "@" + address,
        }
        if self.reverse_service is not None:
            result["reverse_service"] = self.reverse_service
        return result


@dataclass(frozen=True)
class Settings:
    panel_url: str
    credential: str
    routes: dict
    relays: tuple[Relay, ...]

    @classmethod
    def from_value(cls, value):
        if (not isinstance(value, dict) or set(value) != {"version", "remnawave", "routes", "relays"}
                or type(value["version"]) is not int or value["version"] != 1):
            raise ValueError("Invalid dynamic relay configuration")
        panel, routes, relays = value["remnawave"], value["routes"], value["relays"]
        if (not isinstance(panel, dict) or set(panel) != {"url", "credential"}
                or panel["credential"] != "remnawave"):
            raise ValueError("Invalid dynamic relay panel configuration")
        # Validate the origin without issuing a request or reading credentials.
        Remnawave(panel["url"], "configuration-validation-only")
        if not isinstance(routes, dict) or not 1 <= len(routes) <= MAX_ROUTE_KEYS:
            raise ValueError("A bounded route ownership map is required")
        owned = {}
        for key, row in routes.items():
            if (not isinstance(key, str) or not worker.ROUTE_KEY.fullmatch(key)
                    or not isinstance(row, dict) or set(row) != {"node_uuid", "service_id"}):
                raise ValueError("Invalid route ownership")
            owned[key] = {name: canonical_uuid(row[name]) for name in ("node_uuid", "service_id")}
        if not isinstance(relays, list) or not 2 <= len(relays) <= MAX_RELAYS:
            raise ValueError("At least two separately owned relay nodes must be pinned")
        pinned, nodes, services = [], set(), set()
        for row in relays:
            required = {"node_uuid", "service_id", "user", "identity", "known_hosts", "ssh_port"}
            if not isinstance(row, dict) or not required <= set(row) or set(row) - required - {"reverse_service"}:
                raise ValueError("Invalid pinned relay")
            node, service = canonical_uuid(row["node_uuid"]), canonical_uuid(row["service_id"])
            if node in nodes or service in services:
                raise ValueError("Relays must use distinct nodes and hosting services")
            user, port = row["user"], row["ssh_port"]
            if (not isinstance(user, str) or not relay_worker.SSH_USER.fullmatch(user)
                    or user == "root" or type(port) is not int or not 1 <= port <= 65535):
                raise ValueError("Relay must use a bounded non-root SSH account")
            identity, known = relay_worker.private_file(row["identity"]), relay_worker.private_file(row["known_hosts"])
            if identity == known:
                raise ValueError("Relay key and known_hosts must be separate private files")
            reverse = row.get("reverse_service")
            if "reverse_service" in row and (reverse != service or reverse not in relay_worker.REVERSE_PORTS
                                               or user != "spn-ip-relay" or port != 22):
                raise ValueError("Reverse relay must use its reviewed service and account")
            pinned.append(Relay(node, service, user, identity, known, port, reverse))
            nodes.add(node)
            services.add(service)
        return cls(panel["url"], panel["credential"], owned, tuple(pinned))


def credential_token(environ=os.environ):
    """Read only the fixed systemd credential; never accept a token path/argv."""
    value = environ.get("CREDENTIALS_DIRECTORY")
    if not isinstance(value, str):
        raise ValueError("Systemd credentials are unavailable")
    directory = Path(value)
    if (not directory.is_absolute() or value != str(directory)
            or directory.parent != Path("/run/credentials")
            or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,200}", directory.name)):
        raise ValueError("Untrusted systemd credential directory")
    for parent in (*reversed(directory.parents), directory):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022):
            raise ValueError("Systemd credential parents are not controlled")
    info = directory.lstat()
    # Ubuntu systemd exposes LoadCredential to User= via an ACL on a
    # root:root 0550 directory / 0440 file. Root-group read is not public;
    # accept only this observed read-only systemd layout, never group write.
    root_group_directory = (stat.S_IMODE(info.st_mode) == 0o550
                            and info.st_uid == 0 and info.st_gid == 0)
    if stat.S_IMODE(info.st_mode) not in (0o500, 0o700) and not root_group_directory:
        raise ValueError("Systemd credential directory is not private")
    descriptor = os.open(directory / "remnawave", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        root_group_file = (stat.S_IMODE(info.st_mode) == 0o440
                           and info.st_uid == 0 and info.st_gid == 0)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600) and not root_group_file
                or not 0 < info.st_size <= 8192):
            raise ValueError("Systemd credential file is not private")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError("Systemd credential exceeds limit")
        token = raw.removesuffix(b"\n").decode("ascii")
        if not token or not token.isprintable() or any(c.isspace() for c in token):
            raise ValueError("Systemd credential is invalid")
        return token
    finally:
        os.close(descriptor)


def relay_address(panel, row):
    node = panel.read_node(row.node_uuid)
    if (node.get("uuid") != row.node_uuid or node.get("isDisabled") is not False
            or node.get("isConnected") is not True):
        raise ValueError("Pinned relay is not connected and enabled")
    return public_ipv4(node.get("address"))


def check(audit, config, settings, panel, request, *, run=relay_worker.check):
    # Validate the request/leaf before any panel reads or SSH startup. Outside
    # control is a direct HTTPS request through the same SSH relay transport.
    if config.get("control_mode") != "direct_https":
        raise ValueError("Dynamic outside probes require direct_https control")
    worker.select_outbound(audit, config["routes"], request, control_mode="direct_https")
    owner = settings.routes.get(request["key"])
    if owner is None:
        raise ValueError("Route is not in the pinned ownership map")
    audit.pinned_engine(Path(config["xray"]), config["sha256"])
    published, _error = audit.endpoint(config["routes"][request["key"]]["probe"])
    excluded_addresses = {published["address"], request.get("address")}
    control = {"action": "control", "key": request["key"]}
    attempted = 0
    for row in settings.relays:
        # Different UUIDs alone are insufficient: two panel nodes could share
        # one VPS. Exclude both the backend node and its hosting service.
        if row.node_uuid == owner["node_uuid"] or row.service_id == owner["service_id"]:
            continue
        if attempted >= MAX_RELAY_ATTEMPTS:
            break
        attempted += 1
        try:
            address = relay_address(panel, row)
            if address in excluded_addresses:
                # A manual panel edit must not make the outside relay be the
                # exact backend/candidate under test, even with other UUIDs.
                continue
            relay_config = row.connection(address)
            # A usable SSH port is not proof of working EU internet. Require a
            # fresh HTTPS control on this relay before interpreting its leaf.
            if run(audit, config, relay_config, control) != "pass":
                continue
            result = "pass" if request["action"] == "control" else run(audit, config, relay_config, request)
            # A concurrent manual/IP rotation invalidates this relay snapshot.
            if relay_address(panel, row) != address:
                continue
            if result == "pass":
                return "pass"
            if result == "down":
                return "down"
        except Exception:
            # SSH/panel/config failures are not evidence of an inaccessible VPN.
            continue
    return "unknown"


def main(argv=None):
    parser = worker.ProbeArguments(add_help=False, exit_on_error=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--relay-config", required=True, type=Path)
    status = "unknown"
    alarm_handler = None
    timer = None
    try:
        args = parser.parse_args(argv)
        raw = sys.stdin.buffer.read(worker.MAX_REQUEST_BYTES + 1)
        if not 0 < len(raw) <= worker.MAX_REQUEST_BYTES:
            raise ValueError("Probe request is missing or too large")
        request = json.loads(raw)
        audit = worker.load_audit_helper()
        config = audit.protected_json(args.config)
        settings = Settings.from_value(audit.protected_json(args.relay_config))
        panel = Remnawave(settings.panel_url, credential_token(), timeout=5)
        if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
            raise ValueError("Bounded process cleanup needs POSIX timers")
        alarm_handler = signal.signal(signal.SIGALRM, worker._interrupt)
        timer = signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
        status = check(audit, config, settings, panel, request)
    except (Exception, KeyboardInterrupt, SystemExit):
        pass
    finally:
        if timer is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if alarm_handler is not None:
            signal.signal(signal.SIGALRM, alarm_handler)
    print(json.dumps({"status": status}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    for stop in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop, worker._interrupt)
    raise SystemExit(main())
