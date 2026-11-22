"""Run one configured end-to-end VPN check and return a machine-readable status."""

from __future__ import annotations

import argparse
import copy
import importlib.util
from ipaddress import IPv4Address, ip_address
import json
from pathlib import Path
import re
import signal
import socket
import sys
import tempfile


MAX_REQUEST_BYTES = 4096
ROUTE_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ProbeArguments(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid probe arguments")


def load_audit_helper():
    script = Path(__file__).resolve().parents[1] / "ops" / "host-health-audit" / "e2e.py"
    spec = importlib.util.spec_from_file_location("spn_ip_failover._xray_e2e", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("Xray audit helper is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _published_ipv4(host, port, expected):
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("DNS readback needs a hostname")
    if not isinstance(expected, str):
        raise ValueError("Expected DNS address must be IPv4")
    expected = str(IPv4Address(expected))
    resolved = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    if {row[4][0] for row in resolved} != {expected}:
        raise ValueError("Probe DNS does not resolve exclusively to the expected IPv4")
    return expected


def select_outbound(audit, routes, request, *, control_mode="peer_vpn"):
    if control_mode not in ("peer_vpn", "direct_https"):
        raise ValueError("Invalid control mode")
    if not isinstance(request, dict) or request.keys() - {"action", "key", "address", "port", "expected_ip"}:
        raise ValueError("Invalid probe request")
    action, key = request.get("action"), request.get("key")
    if action not in ("control", "probe") or not isinstance(key, str) or not ROUTE_KEY.fullmatch(key):
        raise ValueError("Invalid probe route")
    route = routes[key]
    probe = route["probe"]
    probe_info, probe_error = audit.endpoint(probe)
    if probe_error:
        raise ValueError("Probe must use a supported single outbound")
    control = None
    if control_mode == "peer_vpn":
        control = route["control"]
        control_info, control_error = audit.endpoint(control)
        if control_error:
            raise ValueError("Control must use a supported single outbound")
        fields = ("protocol", "network", "security")
        if any(probe_info[field] != control_info[field] for field in fields):
            raise ValueError("Probe and control must use the same method")
        if all(probe_info[field] == control_info[field] for field in ("address", "port")):
            raise ValueError("Probe and control must use independent endpoints")
    if action == "control":
        return control

    address, port = request.get("address"), request.get("port")
    if not isinstance(address, str) or not 1 <= len(address) <= 253:
        raise ValueError("Invalid probe address")
    if type(port) is not int or port != probe_info["port"]:
        raise ValueError("Probe port differs from the configured outbound")
    configured_address = audit.normalize(address) == probe_info["address"]
    if "expected_ip" in request:
        if not configured_address:
            raise ValueError("DNS readback must use the hostname configured for the probe")
        address = _published_ipv4(probe_info["address"], port, request["expected_ip"])
    elif configured_address:
        return probe
    address = IPv4Address(address)
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError("Candidate address must be a public unicast IPv4")

    security, network = probe_info["security"], probe_info["network"]
    if security in ("tls", "reality") and not probe_info["sni"]:
        raise ValueError("Candidate checks require a stable configured TLS or REALITY server name")
    if security == "none" and network not in ("raw", "kcp", "mkcp"):
        raise ValueError("Candidate transport must have a stable identity")

    candidate = copy.deepcopy(probe)
    settings = candidate["settings"]
    if "vnext" in settings:
        server = settings["vnext"][0]
    elif "servers" in settings:
        server = settings["servers"][0]
    else:
        server = settings
    server["address"] = str(address)
    return candidate


def check(audit, config, request):
    mode = config.get("control_mode", "peer_vpn")
    outbound = select_outbound(audit, config["routes"], request, control_mode=mode)
    engine = Path(config["xray"])
    audit.pinned_engine(engine, config["sha256"])
    with tempfile.TemporaryDirectory(prefix="spn-probe-") as directory:
        if mode == "direct_https" and request["action"] == "control":
            result = audit.test_one(engine, None, Path(directory), 0, direct_https=True)
        else:
            result = audit.test_one(engine, outbound, Path(directory), 0)
    return result_status(result)


def result_status(result):
    if result["status"] == "passed":
        return "pass"
    if result["status"] == "failed" and result["reason"] != "unexpected_https_status":
        return "down"
    return "unknown"


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = ProbeArguments(add_help=False, exit_on_error=False)
    parser.add_argument("--config", required=True, type=Path)
    status = "unknown"
    try:
        arguments = parser.parse_args(argv)
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if not 0 < len(raw) <= MAX_REQUEST_BYTES:
            raise ValueError("Probe request is missing or too large")
        request = json.loads(raw)
        audit = load_audit_helper()
        config = audit.protected_json(arguments.config)
        status = check(audit, config, request)
    except (Exception, KeyboardInterrupt, SystemExit):
        pass
    print(json.dumps({"status": status}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    for stop_signal in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop_signal, _interrupt)
    raise SystemExit(main())
