"""Explicit authenticated single-outbound HTTPS tests; default is no-I/O plan.

Uses ONLY an operator-provided private subscription config and pinned local Xray.
No TUN, original routing/balancers, direct fallback, provider writes or deletion.
Credentials stay in 0600 temporary JSON, never argv/env/results. Unsupported or
ambiguous leaves remain uncovered; a failed check does not mean a host is dead.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time


URL = "https://www.gstatic.com/generate_204"
CURL = "/usr/bin/curl"
ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks", "socks", "http", "hysteria", "hysteria2"}
MAX_INPUT = 16_000_000


class E2EError(ValueError): pass


def require(value):
    if not value: raise E2EError("Explicit private E2E prerequisites failed; details suppressed.")


class Parser(argparse.ArgumentParser):
    def error(self, message): raise E2EError("Only fixed E2E options are accepted.")


def protected_json(path):
    require(path.is_absolute() and not any(item.is_symlink() for item in (path, *path.parents)))
    parent = path.parent.stat()
    require(parent.st_uid == os.geteuid() and stat.S_IMODE(parent.st_mode) == 0o700)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and 0 < info.st_size <= MAX_INPUT)
        with os.fdopen(fd, "rb", closefd=False) as stream: raw = stream.read(MAX_INPUT + 1)
        after = os.fstat(fd)
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_mode, row.st_nlink)
        require(len(raw) == info.st_size and identity(info) == identity(after))
        def unique(pairs):
            result = {}
            for key, value in pairs: require(key not in result); result[key] = value
            return result
        return json.loads(raw, object_pairs_hook=unique)
    finally: os.close(fd)


def write_private(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    require(len(raw) <= MAX_INPUT)
    with private_output(path) as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def private_output(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def normalize(value):
    return value.strip().rstrip(".").strip("[]").casefold() if isinstance(value, str) else ""


def public_address(value):
    name = normalize(value)
    if name == "localhost" or name.endswith((".localhost", ".local", ".internal")): return False
    try: return ipaddress.ip_address(name).is_global
    except ValueError: return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", name))


def chained(value):
    if isinstance(value, dict):
        if any(key in value and value[key] not in (None, "", {}, [])
               for key in ("dialerProxy", "proxyTag", "detour", "outboundTag", "balancerTag", "interface", "tproxy")):
            return True
        return any(chained(item) for item in value.values())
    return isinstance(value, list) and any(chained(item) for item in value)


def endpoint(outbound):
    """Conservative leaf extraction. Never guesses through chains/multi-servers."""
    if not isinstance(outbound, dict) or outbound.get("protocol") not in PROTOCOLS:
        return None, "unsupported_or_template_outbound"
    if outbound.get("proxySettings") not in (None, {}) or outbound.get("sendThrough") not in (None, "") or chained(outbound):
        return None, "dependent_or_bound_outbound"
    mux = outbound.get("mux")
    if mux is not None and (not isinstance(mux, dict) or mux.get("enabled") is not False):
        return None, "unsupported_mux"
    stream = outbound.get("streamSettings") or {}
    if not isinstance(stream, dict): return None, "invalid_transport"
    for key in ("xhttpSettings", "splithttpSettings"):
        if isinstance(stream.get(key), dict) and stream[key].get("downloadSettings") not in (None, {}):
            return None, "separate_download_endpoint"
    settings = outbound.get("settings") or {}
    if not isinstance(settings, dict): return None, "invalid_endpoint"
    servers = settings.get("vnext") if "vnext" in settings else settings.get("servers")
    if servers is not None:
        if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], dict):
            return None, "multiple_or_invalid_servers"
        server = servers[0]
        if isinstance(server.get("users"), list) and len(server["users"]) != 1:
            return None, "multiple_or_missing_credentials"
    else: server = settings
    address, port = server.get("address"), server.get("port")
    if not isinstance(address, str) or not address or any(c.isspace() for c in address) or type(port) is not int or not 1 <= port <= 65535:
        return None, "invalid_endpoint"
    if not public_address(address): return None, "non_public_endpoint"
    network = stream.get("network") or "raw"
    network = "raw" if network == "tcp" else network
    path = None
    if network in {"xhttp", "splithttp", "ws"}:
        options = stream.get({"ws": "wsSettings", "splithttp": "splithttpSettings"}.get(network, "xhttpSettings")) or {}
        path = options.get("path") if isinstance(options, dict) else None
    security = stream.get("security") or "none"
    tls = stream.get("tlsSettings") if security == "tls" else stream.get("realitySettings")
    if tls is not None and not isinstance(tls, dict): return None, "invalid_transport"
    if isinstance(tls, dict) and tls.get("allowInsecure") is True: return None, "insecure_transport"
    return {"address": normalize(address), "port": port, "network": network, "protocol": outbound["protocol"],
            "security": security, "path": path, "sni": normalize((tls or {}).get("serverName"))}, None


def matching(endpoint, outbound, hosts):
    matches = []
    tag = " ".join(str(outbound.get("tag", "")).casefold().split())
    for host in hosts:
        if host.get("port") != endpoint["port"]: continue
        name = " ".join(str(host.get("name", host.get("remark", ""))).casefold().split())
        address = normalize(host.get("address"))
        # SNI alone is not identity. It can only supplement an exact remark.
        if endpoint["address"] != address and not (name and name in tag and endpoint["sni"] and endpoint["sni"] in {address, normalize(host.get("sni"))}):
            continue
        inbound = host.get("inbound") or {}
        network = inbound.get("network") or host.get("network")
        if network == "tcp": network = "raw"
        if network and network != endpoint["network"]: continue
        if inbound.get("protocol") and inbound["protocol"] != endpoint["protocol"]: continue
        security = str(host.get("securityLayer") or "default").casefold()
        if security == "default": security = str(inbound.get("security") or "").casefold()
        if security and security != endpoint["security"]: continue
        if host.get("sni") and normalize(host["sni"]) != endpoint["sni"]: continue
        if host.get("path") and host["path"] != endpoint["path"]: continue
        matches.append(host)
    exact = [host for host in matches if " ".join(str(host.get("name", host.get("remark", ""))).casefold().split()) == tag]
    if len(exact) == 1: return exact
    return matches


def jobs(config, inventory, selected=()):
    require(isinstance(inventory, dict) and isinstance(inventory.get("hosts"), list))
    hosts = inventory["hosts"]
    require(all(isinstance(host, dict) and isinstance(host.get("uuid"), str) for host in hosts)
            and len({host["uuid"] for host in hosts}) == len(hosts) and set(selected) <= {host["uuid"] for host in hosts})
    configurations = config if isinstance(config, list) else [config]
    result = []
    for ci, document in enumerate(configurations):
        require(isinstance(document, dict) and isinstance(document.get("outbounds"), list))
        for oi, outbound in enumerate(document["outbounds"]):
            address, reason = endpoint(outbound)
            matches = matching(address, outbound, hosts) if address else []
            if reason is None and len(matches) != 1: reason = "ambiguous_or_missing_host_mapping"
            host_id = matches[0]["uuid"] if len(matches) == 1 else None
            if selected and host_id not in selected: continue
            result.append({"configIndex": ci, "outboundIndex": oi, "hostUuid": host_id,
                           "uncoveredReason": reason, "outbound": copy.deepcopy(outbound) if reason is None else None})
    require(len(result) <= 1000)
    return result, hosts


def single_config(outbound, port, *, transport_proxy=None, direct_https=False):
    """Build an isolated probe, optionally dialing its TCP transport through SSH.

    transport_proxy is a localhost SOCKS port, never an operator-supplied outbound.
    direct_https builds a fixed internet control instead of using client keys.
    Original VPN leaves are validated before adding the deliberate dialerProxy.
    """
    require(type(port) is int and 1024 <= port <= 65535 and type(direct_https) is bool)
    if transport_proxy is not None:
        require(type(transport_proxy) is int and 1024 <= transport_proxy <= 65535)
    relay = {"tag": "spn-eu-relay", "protocol": "socks", "settings": {"servers": [
        {"address": "127.0.0.1", "port": transport_proxy}]}}
    if direct_https:
        require(outbound is None)
        leaf = relay if transport_proxy is not None else {"protocol": "freedom", "settings": {}}
    else:
        metadata, reason = endpoint(outbound)
        require(reason is None)
        leaf = copy.deepcopy(outbound)
        leaf.pop("proxySettings", None); leaf.pop("mux", None)
        if transport_proxy is not None:
            # OpenSSH dynamic forwarding carries TCP, not QUIC/KCP/Hysteria.
            require(metadata["network"] in {"raw", "ws", "grpc", "http", "h2", "httpupgrade", "xhttp", "splithttp"})
            stream = leaf.setdefault("streamSettings", {})
            sockopt = stream.setdefault("sockopt", {})
            require(isinstance(sockopt, dict) and not sockopt.get("dialerProxy"))
            sockopt["dialerProxy"] = "spn-eu-relay"
    leaf["tag"] = "audit-selected"
    outbounds = [leaf]
    if transport_proxy is not None and not direct_https:
        outbounds.append(relay)
    return {"log": {"loglevel": "warning"},
            "inbounds": [{"tag": "audit-socks", "listen": "127.0.0.1", "port": port, "protocol": "socks",
                          "settings": {"auth": "noauth", "udp": False}}],
            "outbounds": outbounds, "routing": {"domainStrategy": "AsIs", "rules": [
                {"type": "field", "inboundTag": ["audit-socks"], "outboundTag": "audit-selected"}]}}


def pinned_engine(path, expected):
    require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected)
            and path.is_absolute() and not any(item.is_symlink() for item in (path, *path.parents)))
    parent = path.parent.stat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.geteuid()} and info.st_nlink == 1
                and not info.st_mode & 0o022 and info.st_mode & 0o111 and 0 < info.st_size <= 128_000_000
                and parent.st_uid in {0, os.geteuid()} and not parent.st_mode & 0o022)
        hasher = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(65_536), b""): hasher.update(chunk)
        after, named = os.fstat(fd), path.stat()
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_mode, row.st_nlink)
        require(hasher.hexdigest() == expected and identity(info) == identity(after) == identity(named))
    finally: os.close(fd)


def curl_args(port):
    return [CURL, "--disable", "--silent", "--show-error", "--retry", "0", "--connect-timeout", "6", "--max-time", "18",
            "--max-filesize", "4096", "--proto", "=https", "--tlsv1.2", "--noproxy", "", "--socks5-hostname",
            "127.0.0.1:" + str(port), "--output", "/dev/null", "--write-out", "%{http_code} %{time_total}", URL]


def stop(child):
    if child is None or child.poll() is not None: return
    try: os.killpg(child.pid, signal.SIGTERM); child.wait(timeout=3)
    except ProcessLookupError: pass
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=3)


def test_one(engine, outbound, directory, number, *, transport_proxy=None, direct_https=False):
    config = directory / ("config-" + str(number) + ".json")
    log = directory / ("engine-" + str(number) + ".log")
    curl_log = directory / ("curl-" + str(number) + ".log")
    child = None
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0)); port = reservation.getsockname()[1]
        write_private(config, single_config(outbound, port, transport_proxy=transport_proxy, direct_https=direct_https))
        with private_output(log) as output:
            checked = subprocess.run([str(engine), "run", "-test", "-config", str(config)], cwd=directory,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     env=ENV, timeout=12, check=False)
            if checked.returncode: return {"status": "uncovered", "reason": "engine_config_rejected"}
            child = subprocess.Popen([str(engine), "run", "-config", str(config)], cwd=directory,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     env=ENV, close_fds=True, start_new_session=True)
            ready, deadline = False, time.monotonic() + 6
            while time.monotonic() < deadline and child.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2): pass
                    ready = True; break
                except OSError: time.sleep(0.05)
            if not ready or child.poll() is not None: return {"status": "uncovered", "reason": "engine_listener_unavailable"}
            with private_output(curl_log) as errors:
                response = subprocess.run(curl_args(port), cwd=directory, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=errors, env=ENV, timeout=22, check=False)
            if len(response.stdout) > 128: return {"status": "uncovered", "reason": "unexpected_probe_output"}
            if child.poll() is not None: return {"status": "uncovered", "reason": "engine_exited_during_probe"}
            if response.returncode == 7: return {"status": "uncovered", "reason": "local_socks_unavailable"}
            if response.returncode:
                reason = {28: "https_timeout", 35: "https_tls_error", 60: "https_certificate_error"}.get(response.returncode, "https_transfer_failed")
                return {"status": "failed", "reason": reason}
            found = re.fullmatch(rb"([0-9]{3}) ([0-9]+(?:\.[0-9]+)?)", response.stdout.strip())
            if not found: return {"status": "uncovered", "reason": "unexpected_probe_output"}
            if child.poll() is not None: return {"status": "uncovered", "reason": "engine_exited_during_probe"}
            return {"status": "passed" if found[1] == b"204" else "failed",
                    "reason": "https_204" if found[1] == b"204" else "unexpected_https_status",
                    "elapsedMs": round(float(found[2]) * 1000)}
    except subprocess.TimeoutExpired: return {"status": "uncovered", "reason": "local_process_timeout"}
    except Exception: return {"status": "uncovered", "reason": "local_test_error"}
    finally:
        try: stop(child)
        finally:
            if config.exists(): config.unlink()


def report(plan, hosts, results, selected=()):
    covered = {row["hostUuid"] for row in results if row["status"] in {"passed", "failed"}}
    return {"format": 1, "createdAt": datetime.now(timezone.utc).isoformat(), "target": URL,
            "tests": results, "counts": {key: sum(row["status"] == key for row in results) for key in ("passed", "failed", "uncovered")},
            "uncoveredHosts": [host["uuid"] for host in hosts if host["uuid"] not in covered and (not selected or host["uuid"] in selected)],
            "notice": "One authenticated HTTPS request per isolated leaf. Failures are not deletion decisions; UDP payloads are not tested."}


def execute(config_path, inventory_path, engine, expected, selected=(), *, inspect=False):
    plan, hosts = jobs(protected_json(config_path), protected_json(inventory_path), selected)
    if inspect:
        return report(plan, hosts, [{key: value for key, value in job.items() if key != "outbound"} |
                      {"status": "uncovered", "reason": job["uncoveredReason"] or "not_run"} for job in plan], selected)
    pinned_engine(engine, expected)
    directory = Path(tempfile.mkdtemp(prefix="wayspn-host-e2e-20261024-", dir="/private/tmp"))
    results = []
    for number, job in enumerate(plan):
        outcome = ({"status": "uncovered", "reason": job["uncoveredReason"]} if job["uncoveredReason"] else
                   test_one(engine, job["outbound"], directory, number))
        results.append({"configIndex": job["configIndex"], "outboundIndex": job["outboundIndex"], "hostUuid": job["hostUuid"], **outcome})
    result = report(plan, hosts, results, selected)
    write_private(directory / "results.json", result)
    return {"completed": True, "results": str(directory / "results.json"), "counts": result["counts"],
            "uncovered_hosts": len(result["uncoveredHosts"]), "deletions": 0}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--run", action="store_true"); action.add_argument("--inspect", action="store_true")
        parser.add_argument("--config", type=Path); parser.add_argument("--inventory", type=Path)
        parser.add_argument("--xray", type=Path); parser.add_argument("--xray-sha256")
        parser.add_argument("--host", action="append", default=[])
        args = parser.parse_args(argv)
        if not (args.run or args.inspect): value = {"plan_only": True, "network": False, "deletions": 0}
        else:
            require(args.config is not None and args.inventory is not None and (args.inspect or args.xray is not None))
            value = execute(args.config, args.inventory, args.xray, args.xray_sha256, args.host, inspect=args.inspect)
        print(json.dumps(value, sort_keys=True)); return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0: return 0
        print("E2E operation stopped; credentials, configuration and process output suppressed.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__": raise SystemExit(main())
