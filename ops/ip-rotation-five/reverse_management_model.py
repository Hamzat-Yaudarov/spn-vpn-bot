"""Pure renderers for the five pinned, loopback-only reverse SSH channels.

This module performs no I/O, reads no credentials, and never changes a network
interface. The logical guest address remains in Gateway's command so its usual
inventory checks still apply; SSH connects to the service's pinned RF loopback
port and verifies the existing service-UUID host-key alias instead.
"""
from __future__ import annotations

import base64
import binascii
import re
import struct


CONTROLLER = "185.9.24.53"
RF_USER = "spn-ip-mgmt"
RF_HOME = "/var/lib/spn-ip-mgmt"
GUEST_USER = "spn-ip-mgmt-client"
GUEST_HOME = "/var/lib/spn-ip-mgmt-client"
GUEST_UNIT = "spn-ip-mgmt-client.service"
RF_DROPIN = "/etc/ssh/sshd_config.d/92-spn-reverse-management.conf"
SERVICE_PORTS = {
    "f5c0287a-636e-4981-95c7-80b24c9559c0": 23001,
    "21e508f0-2ef2-47da-aee7-15dc8c66eb15": 23002,
    "9b0316d5-fa83-4bf8-b6dc-8202b66622c9": 23003,
    "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d": 23004,
    "4d6f358a-e75f-4dd8-a635-c6ad320f2732": 23005,
}
SERVICES = frozenset(SERVICE_PORTS)
RF_EXPECTED = {
    "allowtcpforwarding": "remote",
    "allowstreamlocalforwarding": "no",
    "permitlisten": " ".join("127.0.0.1:" + str(port) for port in SERVICE_PORTS.values()),
    "permitopen": "none", "gatewayports": "no", "permittunnel": "no",
    "permittty": "no", "permituserrc": "no", "allowagentforwarding": "no",
    "x11forwarding": "no", "passwordauthentication": "no",
    "kbdinteractiveauthentication": "no", "pubkeyauthentication": "yes",
    "authenticationmethods": "publickey", "authorizedkeysfile": RF_HOME + "/.ssh/authorized_keys",
    "forcecommand": "/usr/bin/false", "maxsessions": "0",
    "clientaliveinterval": "15", "clientalivecountmax": "3",
}
RPC_OPTIONS_OLD = (b'restrict,from="185.9.24.53",command="/usr/bin/python3 -I '
                   b'/opt/spn-ip-failover-guest/guest_rpc.py"')
RPC_OPTIONS_NEW = (b'restrict,from="185.9.24.53,127.0.0.1",command="/usr/bin/python3 -I '
                   b'/opt/spn-ip-failover-guest/guest_rpc.py"')
MAX_AUTHORIZED_KEYS = 1024 * 1024
_PUBLIC_KEY = re.compile(r"ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [^\x00-\x1f\x7f]+)?")
_KEY_IN_LINE = re.compile(rb"(?<!\S)ssh-ed25519[ \t]+([A-Za-z0-9+/=]+)(?=[ \t]|$)")


def service_port(service: str) -> int:
    """Return only an explicitly pinned service's port; never derive a port."""
    if not isinstance(service, str) or service not in SERVICE_PORTS:
        raise ValueError("Service is outside the five-guest reverse management scope")
    return SERVICE_PORTS[service]


def public_key(value: str) -> str:
    """Validate the entire canonical Ed25519 wire blob and discard its comment."""
    if not isinstance(value, str) or not 0 < len(value) <= 2048:
        raise ValueError("Invalid plain Ed25519 public key")
    match = _PUBLIC_KEY.fullmatch(value)
    if match is None:
        raise ValueError("Invalid plain Ed25519 public key")
    encoded = match.group(1)
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid Ed25519 public key encoding") from None
    if (len(blob) != 51 or blob[:4] != struct.pack(">I", 11)
            or blob[4:15] != b"ssh-ed25519" or blob[15:19] != struct.pack(">I", 32)
            or base64.b64encode(blob).decode("ascii") != encoded):
        raise ValueError("Invalid canonical Ed25519 public key encoding")
    return "ssh-ed25519 " + encoded


def controller_policy() -> bytes:
    """Fence the unprivileged RF account at sshd as well as per public key."""
    listeners = " ".join("127.0.0.1:" + str(port) for port in SERVICE_PORTS.values())
    return (f"# Five pinned reverse SSH management listeners; no public listeners.\n"
            f"Match User {RF_USER}\n"
            "    AllowTcpForwarding remote\n"
            "    AllowStreamLocalForwarding no\n"
            f"    PermitListen {listeners}\n"
            "    PermitOpen none\n"
            "    GatewayPorts no\n"
            "    PermitTunnel no\n"
            "    PermitTTY no\n"
            "    PermitUserRC no\n"
            "    AllowAgentForwarding no\n"
            "    X11Forwarding no\n"
            "    PasswordAuthentication no\n"
            "    KbdInteractiveAuthentication no\n"
            "    PubkeyAuthentication yes\n"
            "    AuthenticationMethods publickey\n"
            f"    AuthorizedKeysFile {RF_HOME}/.ssh/authorized_keys\n"
            "    ForceCommand /usr/bin/false\n"
            "    MaxSessions 0\n"
            "    ClientAliveInterval 15\n"
            "    ClientAliveCountMax 3\n"
            "Match all\n").encode("ascii")


def rf_key_line(service: str, pub: str) -> bytes:
    """Grant only one loopback listener, without pinning the guest's changing IP."""
    port = service_port(service)
    key = public_key(pub)
    return (f'restrict,port-forwarding,permitlisten="127.0.0.1:{port}",'
            f'command="/usr/bin/false" {key} spn-reverse-management:{service}\n').encode("ascii")


def guest_unit(service: str) -> bytes:
    """Render a persistent unprivileged tunnel; never restart guest networking."""
    port = service_port(service)
    command = [
        "/usr/bin/ssh", "-4", "-NT", "-F", "/dev/null",
        "-i", GUEST_HOME + "/.ssh/tunnel_ed25519",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "HostKeyAlias=spn-ip-rotation-controller",
        "-o", "UserKnownHostsFile=" + GUEST_HOME + "/.ssh/controller_known_hosts",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
        "-o", "PermitLocalCommand=no", "-o", "UpdateHostKeys=no",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=15",
        "-R", f"127.0.0.1:{port}:127.0.0.1:22", RF_USER + "@" + CONTROLLER,
    ]
    return (f"[Unit]\nDescription=Pinned reverse SSH management for {service}\n"
            "Wants=network-online.target\nAfter=network-online.target\nStartLimitIntervalSec=0\n\n"
            f"[Service]\nType=simple\nUser={GUEST_USER}\nGroup={GUEST_USER}\n"
            f"WorkingDirectory={GUEST_HOME}\nUMask=0077\n"
            "ExecStart=" + " ".join(command) + "\n"
            "Restart=always\nRestartSec=3\nTimeoutStopSec=10\n"
            "NoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n"
            "ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n"
            "RestrictSUIDSGID=yes\nLockPersonality=yes\nRestrictRealtime=yes\n"
            "RestrictAddressFamilies=AF_INET AF_UNIX\n\n"
            "[Install]\nWantedBy=multi-user.target\n").encode("ascii")


def gateway_ssh(service: str) -> list[str]:
    """Keep Gateway's logical IP argument while transporting RPC over loopback."""
    port = service_port(service)
    private = "/var/lib/spn-ip-failover/.ssh"
    return [
        "/usr/bin/ssh", "-4", "-F", "/dev/null", "-T",
        "-o", "HostName=127.0.0.1", "-p", str(port), "-l", "root",
        "-o", "HostKeyAlias=" + service,
        "-i", private + "/guest_" + service + "_ed25519",
        "-o", "UserKnownHostsFile=" + private + "/guest_known_hosts",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no",
        "{address}",
    ]


def rpc_key_upgrade(before: bytes, pub: str, service: str) -> tuple[bytes, bool]:
    """Add loopback to only the existing fixed restricted RPC key, in place.

    No key is appended. Foreign uses of the same public key (even in a comment)
    cause a closed failure. All unrelated bytes, comments, and line endings are
    retained; already-upgraded lines are accepted without rewriting.
    """
    service_port(service)
    key = public_key(pub).encode("ascii")
    if not isinstance(before, bytes) or len(before) > MAX_AUTHORIZED_KEYS or b"\x00" in before:
        raise ValueError("Invalid pinned authorized_keys input")
    encoded = key.split()[1]
    wire = base64.b64decode(encoded, validate=True)
    old = RPC_OPTIONS_OLD + b" " + key
    new = RPC_OPTIONS_NEW + b" " + key
    result = []
    found = 0
    changed = False
    for raw_line in before.splitlines(keepends=True):
        line = raw_line.rstrip(b"\r\n")
        matches = []
        for match in _KEY_IN_LINE.finditer(line):
            try:
                token = match.group(1)
                # Excess padding is not canonical, but may still denote this
                # key to an SSH parser. It must not hide a foreign duplicate.
                same = token.rstrip(b"=") == encoded or base64.b64decode(token, validate=True) == wire
            except (ValueError, binascii.Error):
                same = False
            if same:
                matches.append(match)
        if matches:
            valid_old = line == old or line.startswith(old + b" ") or line.startswith(old + b"\t")
            valid_new = line == new or line.startswith(new + b" ") or line.startswith(new + b"\t")
            if len(matches) != 1 or not (valid_old or valid_new):
                raise ValueError("The same RPC public key has foreign or different options")
            found += 1
            if valid_old:
                raw_line = RPC_OPTIONS_NEW + raw_line[len(RPC_OPTIONS_OLD):]
                changed = True
        result.append(raw_line)
    if not found:
        raise ValueError("Existing restricted RPC public key is missing; no key was appended")
    return b"".join(result), changed
