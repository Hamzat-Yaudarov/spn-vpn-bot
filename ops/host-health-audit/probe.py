"""Read-only public endpoint evidence; never a VPN-success or deletion decision.

Accepts only the collector's sanitized inventory. The default prints a no-I/O
plan. --execute makes DNS/TCP/TLS checks against public addresses only, with
at most eight workers. UDP/Reality are never misclassified by TLS probing.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid


TIMEOUT = 5.0
MAX_WORKERS = 8
MAX_HOSTS = 500
MAX_ADDRESSES = 3
TCP_NETWORKS = {"tcp", "raw", "ws", "grpc", "xhttp", "http", "httpupgrade", "splithttp", "h2"}
UDP_NETWORKS = {"quic", "kcp", "mkcp", "hysteria", "hysteria2", "hy2", "tuic"}
DNS_CODE = (
    "import json,socket,sys; "
    "h=json.loads(sys.stdin.read()); "
    "a=socket.getaddrinfo(h,None,socket.AF_UNSPEC,socket.SOCK_STREAM); "
    "print(json.dumps(list(dict.fromkeys(x[4][0] for x in a))))"
)


class ProbeError(ValueError):
    pass


def require(value):
    if not value:
        raise ProbeError("Public endpoint probe prerequisites failed; details suppressed.")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProbeError("Only the documented public endpoint probe options are accepted.")


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def load_inventory(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 8_000_000)
        data = json.load(stream, object_pairs_hook=no_duplicate_keys)
    require(isinstance(data, dict) and data.get("format") == 1
            and set(data) <= {"format", "collectedAt", "hosts", "notice"})
    require(isinstance(data.get("hosts"), list) and len(data["hosts"]) <= MAX_HOSTS)
    seen = set()
    for host in data["hosts"]:
        require(isinstance(host, dict) and isinstance(host.get("uuid"), str)
                and str(uuid.UUID(host["uuid"])) == host["uuid"] and host["uuid"] not in seen)
        require(isinstance(host.get("inbound"), dict))
        seen.add(host["uuid"])
    return data


def clean_label(value):
    return "".join(char for char in value[:160] if char.isprintable()) if isinstance(value, str) else None


def public_ip(value):
    try:
        parsed = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return None
    # Reject scoped/mapped addresses as well as private, special and local IPs.
    if "%" in str(parsed) or getattr(parsed, "ipv4_mapped", None) is not None:
        return None
    return parsed if parsed.is_global and not parsed.is_multicast and not parsed.is_reserved else None


def hostname(value):
    if not isinstance(value, str) or not value or len(value) > 253 or value != value.strip():
        return None
    try:
        encoded = value.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    labels = encoded.split(".")
    if len(labels) < 2 or encoded.endswith((".local", ".localhost", ".internal", ".invalid", ".test")):
        return None
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        return None
    # All-numeric dotted strings cannot become DNS requests after IP rejection.
    if all(label.isdecimal() for label in labels):
        return None
    return encoded


def resolve_public(address):
    """Resolve once with a bounded child; connect only to the returned public IPs.

    getaddrinfo does not honour Python socket timeouts, hence the bounded child.
    No shell is involved and a hostname is supplied through stdin, not code.
    """
    parsed = public_ip(address)
    if parsed is not None:
        return "literal_public_ip", [str(parsed)]
    name = hostname(address)
    if name is None:
        return "unsafe_or_invalid_address", []
    try:
        process = subprocess.run(
            [sys.executable, "-I", "-B", "-c", DNS_CODE], input=json.dumps(name),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=TIMEOUT,
            check=False, env={"PATH": "/usr/bin:/bin"},
        )
    except subprocess.TimeoutExpired:
        return "dns_timeout", []
    except OSError:
        return "dns_failed", []
    if process.returncode != 0 or len(process.stdout) > 16_384:
        return "dns_failed", []
    try:
        values = json.loads(process.stdout)
        require(isinstance(values, list) and len(values) <= 128)
        valid = sorted({str(value) for value in (public_ip(item) for item in values) if value is not None})
    except (ValueError, TypeError):
        return "dns_failed", []
    # Mixed answers are filtered and connections are pinned; no second lookup.
    return ("resolved_public", valid[:MAX_ADDRESSES]) if valid else ("no_public_address", [])


def transport_mode(host):
    inbound = host.get("inbound") or {}
    network = str(inbound.get("network") or "").lower()
    protocol = str(inbound.get("protocol") or "").lower()
    security = str(inbound.get("security") or "").lower()
    layer = str(host.get("securityLayer") or "").lower()
    if network in UDP_NETWORKS or protocol in UDP_NETWORKS:
        return "unsupported_udp"
    if network not in TCP_NETWORKS:
        return "unsupported_transport"
    if security == "reality" or layer == "reality":
        return "reality_tcp_only"
    if layer == "tls" or (layer in {"", "default"} and security == "tls"):
        return "verified_tls"
    if layer not in {"", "default", "none"}:
        return "unsupported_security"
    return "tcp_only"


def tls_name(host, address):
    supplied = host.get("sni")
    if isinstance(supplied, str) and supplied:
        return hostname(supplied) or (str(public_ip(supplied)) if public_ip(supplied) else None)
    return hostname(address) or (str(public_ip(address)) if public_ip(address) else None)


def alpn_protocols(value):
    # Only known ALPN tokens; arbitrary host input never reaches the TLS API.
    if not isinstance(value, str):
        return []
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip() in {"h2", "http/1.1"}))


def tls_context(protocols):
    import certifi
    context = ssl.create_default_context(cafile=certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if protocols:
        context.set_alpn_protocols(protocols)
    return context


def probe_address(address, port, mode, name, protocols):
    started = time.monotonic()
    parsed = public_ip(address)
    require(parsed is not None and type(port) is int and 1 <= port <= 65535)
    result = {"ip": str(parsed), "tcp": "not_tested", "tls": "not_tested", "alpn": None}
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    endpoint = (str(parsed), port, 0, 0) if parsed.version == 6 else (str(parsed), port)
    try:
        # Numeric socket addresses avoid DNS rebinding and additional lookups.
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(TIMEOUT)
            connection.connect(endpoint)
            result["tcp"] = "connected"
            if mode == "verified_tls":
                if name is None:
                    result["tls"] = "unsupported_server_name"
                else:
                    try:
                        with tls_context(protocols).wrap_socket(connection, server_hostname=name) as encrypted:
                            result["tls"] = "verified"
                            selected = encrypted.selected_alpn_protocol()
                            result["alpn"] = selected if selected in {"h2", "http/1.1"} else None
                    except ssl.SSLCertVerificationError:
                        result["tls"] = "certificate_invalid"
                    except (socket.timeout, TimeoutError):
                        result["tls"] = "timeout"
                    except (ssl.SSLError, OSError):
                        result["tls"] = "handshake_failed"
    except (socket.timeout, TimeoutError):
        result["tcp"] = "timeout"
    except ConnectionRefusedError:
        result["tcp"] = "refused"
    except OSError:
        result["tcp"] = "failed"
    result["elapsedMs"] = round((time.monotonic() - started) * 1000)
    return result


def probe_host(host):
    started = time.monotonic()
    address, port = host.get("address"), host.get("port")
    mode = transport_mode(host)
    result = {"uuid": host["uuid"], "name": clean_label(host.get("name")),
              "address": clean_label(address), "port": port if type(port) is int else None,
              "mode": mode, "dns": "not_tested", "result": "not_tested", "attempts": [],
              "e2eVerified": False, "deleteRecommended": False}
    if mode.startswith("unsupported_"):
        result["result"] = mode
    elif type(port) is not int or not 1 <= port <= 65535:
        result["result"] = "unsafe_or_invalid_port"
    else:
        dns, addresses = resolve_public(address)
        result["dns"] = dns
        if not addresses:
            result["result"] = dns
        else:
            protocols = alpn_protocols(host.get("alpn"))
            name = tls_name(host, address)
            result["attempts"] = [probe_address(item, port, mode, name, protocols) for item in addresses]
            attempts = result["attempts"]
            if mode == "verified_tls" and any(item["tls"] == "verified" for item in attempts):
                result["result"] = "tls_reachable_not_vpn_verified"
            elif any(item["tcp"] == "connected" for item in attempts):
                result["result"] = "tcp_reachable_tls_unverified" if mode == "verified_tls" else "tcp_reachable_not_vpn_verified"
            else:
                result["result"] = "unreachable_from_audit_network"
    result["elapsedMs"] = round((time.monotonic() - started) * 1000)
    return result


def validate_output(path):
    path = Path(path)
    directory = path.parent
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700 and not directory.is_symlink())
    require(not path.exists() and not path.is_symlink())
    return path


def write_report(path, value):
    path = validate_output(path)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def execute(inventory_path, output_path, round_number):
    inventory = load_inventory(inventory_path)
    validate_output(output_path)
    # Build trust settings before any checks, not after successful TCP probes.
    tls_context([])
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(probe_host, inventory["hosts"]))
    counts = {}
    for result in results:
        counts[result["result"]] = counts.get(result["result"], 0) + 1
    report = {"format": 1, "round": round_number, "probedAt": datetime.now(timezone.utc).isoformat(),
              "inventoryCollectedAt": inventory.get("collectedAt"), "hosts": results, "counts": counts,
              "notice": "Public DNS/TCP/TLS evidence from this network only. No authenticated VPN tests. No deletion recommendations.",
              "mutations": 0, "deletionsRecommended": 0}
    write_report(output_path, report)
    return {"report": str(output_path), "round": round_number, "hosts": len(results), "counts": counts,
            "mutations": 0, "deletions_recommended": 0}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--inventory", type=Path)
        parser.add_argument("--output", type=Path)
        parser.add_argument("--round", type=int, choices=(1, 2), default=1)
        args = parser.parse_args(argv)
        if args.execute:
            require(args.inventory is not None and args.output is not None)
            result = execute(args.inventory, args.output, args.round)
        else:
            result = {"plan_only": True, "network": False, "mutations": 0}
        print(json.dumps(result, sort_keys=True))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        print("Read-only public endpoint probe stopped; raw errors and inputs suppressed.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
