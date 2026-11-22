#!/usr/bin/env python3
"""Fixed-target keyless Poland checks; no API, SSH, config or subscriber access.

Connections use the fixed IP, while TLS SNI and HTTP Host use the fixed domain.
DNS is deliberately not a prerequisite. Infrastructure checks do not prove an
authenticated VPN connection or replace a server-side firewall rule audit.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import socket
import ssl
import time

import certifi

ADDRESS = "191.217.245.62"
DOMAIN = "pol.tekst.site"
TIMEOUT = 5.0
MAX_HEADERS = 16_384


class ProbeFailure(RuntimeError):
    """Only fixed, nonsecret diagnostics may be used as messages."""


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError()
    return value


def trusted_tls(port, alpn, deadline):
    context = ssl.create_default_context(cafile=certifi.where())
    context.set_alpn_protocols(alpn)
    raw = socket.create_connection((ADDRESS, port), timeout=remaining(deadline))
    try:
        raw.settimeout(remaining(deadline))
        return context.wrap_socket(raw, server_hostname=DOMAIN)
    except BaseException:
        raw.close()
        raise


def receive_headers(stream, deadline):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        stream.settimeout(remaining(deadline))
        chunk = stream.recv(min(4096, MAX_HEADERS + 1 - len(data)))
        if not chunk:
            raise ProbeFailure("headers_incomplete")
        data.extend(chunk)
        if len(data) > MAX_HEADERS:
            raise ProbeFailure("headers_too_large")
    head, body = bytes(data).split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ", 2)
    if len(parts) < 2 or parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"}:
        raise ProbeFailure("invalid_http_status")
    try:
        status = int(parts[1])
        headers = {}
        for line in lines[1:]:
            name, value = line.split(b":", 1)
            name = name.strip().lower()
            if not name or name in headers:
                raise ValueError()
            headers[name] = value.strip()
    except ValueError:
        raise ProbeFailure("invalid_http_headers") from None
    return status, headers, body


def health():
    deadline = time.monotonic() + TIMEOUT
    with trusted_tls(443, ["http/1.1"], deadline) as stream:
        stream.settimeout(remaining(deadline))
        stream.sendall((f"GET /health HTTP/1.1\r\nHost: {DOMAIN}\r\nConnection: close\r\n\r\n").encode("ascii"))
        status, headers, body = receive_headers(stream, deadline)
        if status != 200 or headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json":
            raise ProbeFailure("health_status_or_type")
        try:
            length = int(headers[b"content-length"])
        except (KeyError, ValueError):
            raise ProbeFailure("health_length_missing") from None
        if not 1 <= length <= 256 or b"transfer-encoding" in headers:
            raise ProbeFailure("health_length_invalid")
        while len(body) < length:
            stream.settimeout(remaining(deadline))
            chunk = stream.recv(length - len(body))
            if not chunk:
                raise ProbeFailure("health_body_incomplete")
            body += chunk
        if body.strip() != b'{"status":"ok"}':
            raise ProbeFailure("health_body_unexpected")
    return "trusted_tls_expected_health"


def websocket():
    deadline = time.monotonic() + TIMEOUT
    nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    with trusted_tls(443, ["http/1.1"], deadline) as stream:
        stream.settimeout(remaining(deadline))
        stream.sendall((f"GET /direct/ws/ HTTP/1.1\r\nHost: {DOMAIN}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                        f"Sec-WebSocket-Key: {nonce}\r\n\r\n").encode("ascii"))
        status, headers, _ = receive_headers(stream, deadline)
        # SHA1 here is the mandated WebSocket handshake, not password hashing.
        expected = base64.b64encode(hashlib.sha1((nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest())
        if (status != 101 or headers.get(b"sec-websocket-accept") != expected
                or headers.get(b"upgrade", b"").lower() != b"websocket"
                or b"upgrade" not in {part.strip() for part in headers.get(b"connection", b"").lower().split(b",")}):
            raise ProbeFailure("websocket_upgrade_invalid")
    return "trusted_tls_websocket_101_correct_accept"


def reality_fallback():
    deadline = time.monotonic() + TIMEOUT
    with trusted_tls(2083, ["h2"], deadline) as stream:
        if stream.version() != "TLSv1.3" or stream.selected_alpn_protocol() != "h2":
            raise ProbeFailure("reality_fallback_tls_or_alpn")
    return "trusted_fallback_tls13_h2_not_authenticated_vpn"


def node_api_not_tls():
    deadline = time.monotonic() + TIMEOUT
    # Detection only: trusting ANY certificate makes an exposed private/self-
    # signed API detectable. Never used for authenticated traffic or other probes.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection((ADDRESS, 2222), timeout=remaining(deadline))
    except (TimeoutError, ConnectionRefusedError, ConnectionResetError):
        return "no_external_tcp_response"
    try:
        raw.settimeout(remaining(deadline))
        with context.wrap_socket(raw, server_hostname=DOMAIN):
            raise ProbeFailure("node_api_tls_exposed")
    except (TimeoutError, ConnectionResetError, ssl.SSLError):
        return "tcp_connected_but_no_tls_response_firewall_audit_still_required"
    finally:
        raw.close()


def ws_upstream_closed():
    deadline = time.monotonic() + TIMEOUT
    try:
        with socket.create_connection((ADDRESS, 10088), timeout=remaining(deadline)):
            raise ProbeFailure("ws_loopback_upstream_exposed")
    except (TimeoutError, ConnectionRefusedError, ConnectionResetError):
        return "no_external_tcp_response"


def run():
    checks = []
    for name, probe in (("https_health", health), ("websocket_upgrade", websocket),
                        ("grpc_reality_fallback", reality_fallback),
                        ("node_api_2222", node_api_not_tls), ("ws_upstream_10088", ws_upstream_closed)):
        try:
            checks.append({"check": name, "ok": True, "detail": probe()})
        except Exception as error:
            # Never print remote response headers/bodies or arbitrary exception text.
            detail = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
            checks.append({"check": name, "ok": False, "detail": detail})
    return {"ok": all(item["ok"] for item in checks), "target": ADDRESS, "server_name": DOMAIN,
            "dns_checked": False, "subscriber_traffic_tested": False, "firewall_rules_audited": False,
            "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print only sanitized JSON")
    args = parser.parse_args(argv)
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        for item in result["checks"]:
            print(("PASS" if item["ok"] else "FAIL") + " " + item["check"] + ": " + item["detail"])
        print("Keyless checks only. DNS, firewall rules and real VPN traffic are not verified.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
