"""Bounded physical-path VLESS/REALITY/Safari/Vision probes, Linux and macOS.

Only owner-pinned local Xray binaries are run. Existing credentials travel in a
0700 temporary directory / 0600 config or the fixed outside worker's stdin,
never argv, environment, diagnostics or results. No subscriptions are fetched,
no TUN is created, and the isolated VPN config has no DIRECT fallback.

The trusted outside worker must execute the probe on an independent host, not
forward a tunnel to the target. Its JSON reply explicitly identifies that host.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import re
import selectors
import socket
import stat
import subprocess
import tempfile
import time
import uuid

from spn_ip_failover.worker import load_audit_helper


TRACE_URL = "https://1.1.1.1/cdn-cgi/trace"
CURL = "/usr/bin/curl"
ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
MAX_OUTPUT = 65_536
MAX_INPUT = 131_072
OUTSIDE_MAX_AGE = 90
_IFACE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,14}")
_VIRTUAL = ("tun", "tap", "utun", "wg", "lo", "ppp", "tailscale", "ipsec", "sit", "gre", "vti")


class ProbeError(RuntimeError):
    """A safe static diagnostic; private payloads/process output are suppressed."""


def _require(condition):
    if not condition:
        raise ProbeError("Probe prerequisites are invalid or incomplete")


def _public_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ProbeError("Probe requires a concrete public IPv4 address") from None
    _require(isinstance(value, str) and str(address) == value and address.is_global
             and not address.is_multicast and not address.is_reserved)
    return str(address)


def _interface(value):
    _require(isinstance(value, str) and _IFACE.fullmatch(value) is not None
             and not value.casefold().startswith(_VIRTUAL))
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json(raw):
    try:
        def invalid(value):
            raise ValueError()
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=invalid)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProbeError("Probe command returned invalid JSON") from None


def _bounded_run(argv, *, input=None, timeout=15, limit=MAX_OUTPUT):
    """Run fixed argv without a shell; cap output while the process is running."""
    _require(input is None or isinstance(input, bytes) and len(input) <= MAX_INPUT)
    child = None
    try:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV,
                                 close_fds=True, start_new_session=True)
        output = bytearray()
        pending = memoryview(input) if input is not None else None
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ, "stdout")
            if child.stdin is not None:
                os.set_blocking(child.stdin.fileno(), False)
                selector.register(child.stdin, selectors.EVENT_WRITE, "stdin")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                _require(remaining > 0)
                for key, _ in selector.select(min(remaining, 0.5)):
                    if key.data == "stdout":
                        data = os.read(key.fd, min(16_384, limit + 1 - len(output)))
                        if not data:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(data)
                            _require(len(output) <= limit)
                    else:
                        try:
                            written = os.write(key.fd, pending[:4096]) if pending else 0
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = None
                        if not pending:
                            selector.unregister(key.fileobj)
                            child.stdin.close()
            result = child.wait(timeout=max(.01, deadline - time.monotonic()))
        return result, bytes(output)
    except ProbeError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ProbeError("Probe command failed; process output suppressed") from None
    finally:
        if child is not None:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, 9)
                except OSError:
                    # Restricted macOS hosts may deny process-group signalling.
                    # Killing our own direct child still prevents a leaked probe.
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                child.wait(timeout=3)
            if child.stdout is not None:
                child.stdout.close()
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()


def _command(command):
    _require(isinstance(command, (list, tuple)) and 1 <= len(command) <= 32
             and all(isinstance(arg, str) and arg and len(arg) <= 1024 and arg.isprintable()
                     and not any(character.isspace() for character in arg) for arg in command))
    path = Path(command[0])
    _require(path.is_absolute() and not any(part.is_symlink() for part in (path, *path.parents)))
    # A fixed worker/wrapper is supported; shells, ad-hoc code and SSH tunnels are not.
    _require(path.name not in {"sh", "bash", "zsh", "fish", "dash", "env", "sudo", "su",
                               "python", "python3", "perl", "ruby", "node", "ssh", "socat", "nc", "curl"})
    info, parent = path.stat(), path.parent.stat()
    _require(stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.geteuid()}
             and info.st_mode & 0o111 and not info.st_mode & 0o022 and info.st_nlink == 1
             and parent.st_uid in {0, os.geteuid()} and not parent.st_mode & 0o022)
    return list(command)


class ProbeRunner:
    """Owner-configured local engine and optional fixed independent probe worker.

    ``check(server, ip, outbound=private_leaf, expected_exit_ip=ip)`` never treats
    SSH success or an empty set of independent checks as VPN health evidence.
    No I/O occurs during construction. readiness() hashes assets but sends no probes.
    """

    def __init__(self, engine, sha256, *, interface=None, outside_command=None):
        _require(isinstance(engine, (str, Path)) and Path(engine).is_absolute()
                 and isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256) is not None)
        if interface is not None:
            _interface(interface)
        self.engine = Path(engine)
        self.sha256 = sha256
        self.interface = interface
        self.outside_command = list(outside_command) if isinstance(outside_command, (list, tuple)) else outside_command
        self._audit = load_audit_helper()

    @property
    def configured(self):
        return self.interface is not None and self.outside_command is not None

    def readiness(self):
        backend = platform.system()
        result = {"backend": backend, "linux_backend": backend == "Linux", "configured": False,
                  "engine_verified": False, "interface_configured": self.interface is not None,
                  "outside_configured": False}
        try:
            _require(backend in {"Linux", "Darwin"})
            self._audit.pinned_engine(self.engine, self.sha256)
            result["engine_verified"] = True
            if self.outside_command is not None:
                _command(self.outside_command)
                result["outside_configured"] = True
            result["configured"] = (result["engine_verified"] and result["interface_configured"]
                                    and result["outside_configured"])
        except Exception:
            result["reason"] = "probe_prerequisites_unverified"
        return result

    def _physical_path(self, target):
        """Prove effective route+source and bind the isolated engine to that device.

        Linux uses iproute2, not Darwin IP_BOUND_IF/ipconfig/route APIs. A normal
        lookup through TUN/policy routing is refused even if an oif override works.
        """
        interface = _interface(self.interface)
        backend = platform.system()
        if backend == "Linux":
            ip_binary = next((path for path in ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip") if Path(path).is_file()), None)
            _require(ip_binary is not None)
            rc, raw = _bounded_run([ip_binary, "-j", "-4", "route", "get", target], timeout=4)
            rows = _json(raw)
            _require(rc == 0 and isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict))
            route = rows[0]
            _require(route.get("dev") == interface and route.get("type", "unicast") == "unicast"
                     and route.get("gateway") != target)
            source = route.get("prefsrc", route.get("src"))
            rc, raw = _bounded_run([ip_binary, "-j", "link", "show", "dev", interface], timeout=4)
            links = _json(raw)
            _require(rc == 0 and isinstance(links, list) and len(links) == 1 and isinstance(links[0], dict))
            link = links[0]
            _require(link.get("ifname") == interface and link.get("link_type") == "ether"
                     and "UP" in link.get("flags", []))
            kind = (link.get("linkinfo") or {}).get("info_kind")
            _require(kind in (None, "vlan", "bond", "bridge", "veth"))
        elif backend == "Darwin":
            rc, raw = _bounded_run(["/sbin/route", "-n", "get", target], timeout=4)
            found = re.findall(rb"^\s*interface:\s*(\S+)\s*$", raw, re.MULTILINE)
            _require(rc == 0 and found == [interface.encode()])
            rc, raw = _bounded_run(["/usr/sbin/ipconfig", "getifaddr", interface], timeout=4)
            _require(rc == 0)
            source = raw.decode("ascii").strip()
        else:
            raise ProbeError("Physical probe backend is unsupported")
        try:
            address = ipaddress.IPv4Address(source)
        except (ValueError, TypeError):
            raise ProbeError("Physical route source is unverified") from None
        _require(not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast))
        return {"interface": interface, "source": str(address), "backend": backend}

    def _leaf(self, outbound, target):
        _require(isinstance(outbound, dict))
        metadata, reason = self._audit.endpoint(outbound)
        _require(reason is None and metadata["protocol"] == "vless" and metadata["security"] == "reality"
                 and metadata["network"] == "raw" and metadata["port"] == 443)
        try:
            user = outbound["settings"]["vnext"][0]["users"][0]
            reality = outbound["streamSettings"]["realitySettings"]
            _require(uuid.UUID(user["id"]).int != 0 and user.get("encryption") == "none"
                     and user.get("flow") == "xtls-rprx-vision" and reality.get("fingerprint") == "safari"
                     and re.fullmatch(r"[A-Za-z0-9_-]{43}", reality.get("publicKey", "")) is not None
                     and re.fullmatch(r"(?:[a-fA-F0-9]{2}){1,8}", reality.get("shortId", "")) is not None
                     and isinstance(reality.get("serverName"), str) and "." in reality["serverName"])
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise ProbeError("Private probe leaf is invalid; details suppressed") from None
        leaf = copy.deepcopy(outbound)
        leaf["settings"]["vnext"][0]["address"] = target
        return leaf

    @staticmethod
    def _trace_args(*, port=None, interface=None):
        args = [CURL, "--disable", "--silent", "--show-error", "--retry", "0", "--connect-timeout", "5",
                "--max-time", "12", "--max-filesize", "4096", "--proto", "=https", "--tlsv1.2", "--ipv4",
                "--write-out", "\n%{http_code}"]
        if port is None:
            args += ["--noproxy", "*", "--proxy", "", "--interface", interface]
        else:
            args += ["--noproxy", "", "--socks5-hostname", "127.0.0.1:" + str(port)]
        return args + [TRACE_URL]

    @staticmethod
    def _trace_ok(rc, raw, expected=None):
        if rc != 0 or len(raw) > 4096:
            return False
        try:
            body, status = raw.decode("ascii").rsplit("\n", 1)
            exits = [line[3:] for line in body.splitlines() if line.startswith("ip=")]
            if status.strip() != "200" or len(exits) != 1:
                return False
            return _public_ip(exits[0]) == expected if expected is not None else bool(_public_ip(exits[0]))
        except (ValueError, UnicodeError, ProbeError):
            return False

    def _control(self):
        rc, raw = _bounded_run(self._trace_args(interface=self.interface), timeout=15, limit=4096)
        return self._trace_ok(rc, raw)

    def _vpn(self, leaf, path, expected_exit_ip):
        child = None
        try:
            with tempfile.TemporaryDirectory(prefix="voxa-private-probe-") as temporary:
                directory = Path(temporary).resolve()
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                config = self._audit.single_config(leaf, port)
                selected = config["outbounds"][0]
                selected["sendThrough"] = path["source"]
                selected["streamSettings"]["sockopt"] = {"interface": path["interface"]}
                config["log"] = {"loglevel": "none", "access": "none"}
                config_path = directory / "private.json"
                self._audit.write_private(config_path, config)
                # Re-hash immediately before execution; never acquire binaries here.
                self._audit.pinned_engine(self.engine, self.sha256)
                rc, _ = _bounded_run([str(self.engine), "run", "-test", "-config", str(config_path)], timeout=12)
                _require(rc == 0)
                child = subprocess.Popen([str(self.engine), "run", "-config", str(config_path)], cwd=directory,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=ENV, close_fds=True, start_new_session=True)
                deadline = time.monotonic() + 6
                ready = False
                while time.monotonic() < deadline and child.poll() is None:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.2):
                            ready = True
                            break
                    except OSError:
                        time.sleep(.05)
                _require(ready and child.poll() is None)
                rc, raw = _bounded_run(self._trace_args(port=port), timeout=15, limit=4096)
                return self._trace_ok(rc, raw, expected_exit_ip) and child.poll() is None
        finally:
            try:
                self._audit.stop(child)
            except OSError:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)

    def _ssh_banner(self, target, path):
        """Diagnostic only: no SSH login, password, host-key trust or remote command."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                if path["backend"] == "Linux":
                    connection.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25),
                                          path["interface"].encode() + b"\0")
                elif path["backend"] == "Darwin":
                    connection.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex(path["interface"]))
                connection.bind((path["source"], 0))
                connection.connect((target, 22))
                return connection.recv(256).startswith(b"SSH-2.0-")
        except OSError:
            return False

    def _outside(self, target, expected, leaf):
        if self.outside_command is None:
            return []
        command = _command(self.outside_command)
        request = {"action": "probe", "target_ip": target, "expected_exit_ip": expected, "outbound": leaf}
        rc, raw = _bounded_run(command, input=json.dumps(request, allow_nan=False).encode(), timeout=45)
        data = _json(raw)
        now = time.time()
        _require(rc == 0 and isinstance(data, dict) and data.get("target_ip") == target
                 and data.get("expected_exit_ip") == expected and data.get("independent") is True
                 and _public_ip(data.get("observer_ip")) != target
                 and type(data.get("vpn_ok")) is bool and type(data.get("controls_ok")) is bool
                 and type(data.get("observed_at")) in (float, int) and math.isfinite(data["observed_at"])
                 and -5 <= now - data["observed_at"] <= OUTSIDE_MAX_AGE)
        return [{"vpn_ok": data["vpn_ok"], "controls_ok": data["controls_ok"],
                 "observed_at": data["observed_at"]}]

    def check(self, server, ip=None, *, outbound=None, expected_exit_ip=None):
        now = time.time()
        result = {"observed_at": now, "controls_ok": False, "rf": [], "outside": [],
                  "ssh_ok": None, "details": {"reason": "probe_prerequisites_unverified"}}
        try:
            _require(isinstance(server, dict))
            target = _public_ip(ip if ip is not None else server.get("current_ip"))
            expected = _public_ip(expected_exit_ip if expected_exit_ip is not None else target)
            leaf = self._leaf(outbound, target)
            self._audit.pinned_engine(self.engine, self.sha256)
            before = self._physical_path(target)
            controls_ok = self._control()
            vpn_ok = self._vpn(leaf, before, expected) if controls_ok else None
            # Path changes invalidate evidence; no TUN-route result is accepted.
            _require(self._physical_path(target) == before)
            result["ssh_ok"] = self._ssh_banner(target, before)
            result["controls_ok"] = controls_ok
            if vpn_ok is not None:
                result["rf"] = [{"vpn_ok": vpn_ok, "controls_ok": controls_ok, "observed_at": time.time()}]
            result["details"] = {"reason": "authenticated_expected_exit_checked", "physical_path_verified": True,
                                  "outside": "unconfigured" if self.outside_command is None else "unverified"}
            try:
                result["outside"] = self._outside(target, expected, leaf)
                if result["outside"]:
                    result["details"]["outside"] = "independent_checked"
            except Exception:
                result["details"]["outside"] = "probe_unavailable"
            result["observed_at"] = time.time()
        except Exception:
            # A local tool/configuration failure is unknown, not VPN unreachable.
            result.update(controls_ok=False, rf=[], outside=[])
        return result
