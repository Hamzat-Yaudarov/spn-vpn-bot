"""Verify an explicit keyless fixture and isolated native-Xray failover.

Run this on the authorized Docker host, not through SSH. The only input file is
the explicitly supplied keyless fixture. No subscriber/live configurations or
authentication keys are discovered or fetched. Runtime configuration is generated
in memory and sent through stdin; existing container configuration is untouched.

All sockets bind/connect to 127.0.0.1. Mock proxies accept only special.invalid:80
and never contact that destination. This checks native leastPing/fallback behavior,
not real VPN credentials, remote transports, client support, or end-to-end service.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import queue
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

CONTAINER = "remnanode"
EXPECTED_VERSION = "26.7.28"
# An explicit local socket prevents an inherited Docker context/DOCKER_HOST from
# silently turning this host-local check into remote execution.
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
DESTINATION = "special.invalid"
PROBE_PATH = "/__auto_probe__"
ROUTE_PATH = "/__auto_route__"
MAX_FIXTURE_BYTES = 1024 * 1024
PHASE_TIMEOUT = 35.0


class VerificationError(Exception):
    """Only constant, non-sensitive diagnostic messages reach the report."""


def read_fixture(path: Path) -> bytes:
    """Read only the user-selected regular file and reject non-keyless content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FIXTURE_BYTES:
                raise VerificationError("Fixture must be a regular file under 1 MiB")
            payload = stream.read(MAX_FIXTURE_BYTES + 1)
    except OSError:
        raise VerificationError("Cannot safely read the explicitly supplied fixture") from None
    if len(payload) > MAX_FIXTURE_BYTES:
        raise VerificationError("Fixture exceeds the 1 MiB limit")
    try:
        config = json.loads(payload)
    except (ValueError, UnicodeError):
        raise VerificationError("Fixture is not valid JSON") from None
    if not isinstance(config, dict) or config.get("inbounds") or "remnawave" in config:
        raise VerificationError("Fixture must be keyless and have no inbounds or injection section")
    outbounds = config.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds or any(
        not isinstance(outbound, dict)
        or outbound.get("protocol") not in {"freedom", "blackhole"}
        for outbound in outbounds
    ):
        raise VerificationError("Fixture outbounds must contain only freedom/blackhole placeholders")
    forbidden = {
        "users", "clients", "accounts", "password", "passwords", "privatekey",
        "publickey", "secret", "secretkey", "token", "authorization", "uuid",
    }

    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.lower().replace("_", "").replace("-", "")
                if normalized in forbidden:
                    raise VerificationError("Fixture contains a prohibited authentication/key field")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(config)
    return payload


def docker_check(args: list[str], *, payload: bytes | None = None, timeout=15):
    try:
        return subprocess.run(
            DOCKER + args, input=payload, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise VerificationError("Local Docker command unavailable or timed out") from None


def check_container() -> str:
    result = docker_check([
        "inspect", "--format", "{{.State.Running}} {{.HostConfig.NetworkMode}}", CONTAINER,
    ])
    if result.returncode or result.stdout.strip() != b"true host":
        raise VerificationError("remnanode must be running with host networking")
    result = docker_check(["exec", CONTAINER, "xray", "version"])
    match = re.search(rb"\bXray\s+(\d+\.\d+\.\d+)\b", result.stdout)
    if result.returncode or not match or match.group(1).decode("ascii") != EXPECTED_VERSION:
        raise VerificationError("This verifier requires native Xray 26.7.28")
    return EXPECTED_VERSION


class MockProxy(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, label: str, delay: float):
        self.label, self.delay = label, delay
        self.available = threading.Event()
        self.available.set()
        self.lock = threading.Lock()
        self.connections = 0
        self.probes = 0
        self.routes = 0
        self.active: set[socket.socket] = set()
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        with self.lock:
            self.connections += 1
            self.active.add(connection)
        if not self.available.is_set():
            # An HTTP 503 would still count as a successful observatory probe.
            # Offline means transport failure: close without any HTTP response.
            self.close_request(connection)
            raise OSError("Mock proxy is offline")
        return connection, address

    def close_request(self, request):
        with self.lock:
            self.active.discard(request)
        super().close_request(request)

    def handle_error(self, request, client_address):
        # Offline transitions intentionally interrupt requests. Never dump raw
        # handler traces or HTTP request details to the JSON-report channel.
        pass

    def count_connections(self) -> int:
        with self.lock:
            return self.connections

    def offline(self):
        self.available.clear()
        with self.lock:
            connections = list(self.active)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def stop(self):
        self.offline()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def close_without_response(self):
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def response(self, status: int, body: bytes = b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def do_CONNECT(self):
        if not self.server.available.is_set():
            return self.close_without_response()
        if self.path != DESTINATION + ":80":
            return self.response(403)
        # Xray's HTTP outbound uses CONNECT even for plaintext HTTP destinations.
        # The handler's next iteration reads the GET inside this local tunnel.
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = False

    def do_GET(self):
        if not self.server.available.is_set():
            return self.close_without_response()
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme:
                allowed = (parsed.scheme == "http" and parsed.hostname == DESTINATION
                           and parsed.port in (None, 80) and not parsed.username
                           and not parsed.password)
            else:
                allowed = self.headers.get("Host", "") in {DESTINATION, DESTINATION + ":80"}
            allowed = allowed and parsed.path in {PROBE_PATH, ROUTE_PATH} and not parsed.query
        except ValueError:
            allowed = False
        if not allowed:
            return self.response(403)
        time.sleep(self.server.delay)
        if not self.server.available.is_set():
            return self.close_without_response()
        with self.server.lock:
            if parsed.path == PROBE_PATH:
                self.server.probes += 1
            else:
                self.server.routes += 1
        if parsed.path == PROBE_PATH:
            self.response(204)
        else:
            self.response(200, self.server.label.encode("ascii"))


def runtime_config(inbound_port: int, fast_port: int, slow_port: int) -> dict:
    return {
        "log": {"loglevel": "none"},
        "inbounds": [{"tag": "local-test", "listen": "127.0.0.1", "port": inbound_port,
                      "protocol": "http", "settings": {}}],
        "outbounds": [
            {"tag": "block", "protocol": "blackhole", "settings": {}},
            *[{"tag": tag, "protocol": "http", "settings": {
                "servers": [{"address": "127.0.0.1", "port": port}],
            }} for tag, port in [("mock-fast", fast_port), ("mock-slow", slow_port)]],
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [{"type": "field", "network": "tcp,udp", "balancerTag": "mock-balancer"}],
            "balancers": [{"tag": "mock-balancer", "selector": ["mock-"],
                           "strategy": {"type": "leastPing"}, "fallbackTag": "block"}],
        },
        "burstObservatory": {
            "subjectSelector": ["mock-"],
            "pingConfig": {"destination": "http://special.invalid" + PROBE_PATH,
                           "connectivity": "", "interval": "10s", "sampling": 1,
                           "timeout": "1s", "httpMethod": "GET"},
        },
    }


class OwnedXray:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.pid: int | None = None

    def start(self, config: dict):
        try:
            self.process = subprocess.Popen(
                DOCKER + ["exec", "-i", CONTAINER, "sh", "-c", "echo $$; exec xray run -config stdin:"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError:
            raise VerificationError("Cannot start the isolated native-Xray process") from None
        announced: queue.Queue[bytes] = queue.Queue(maxsize=1)

        def drain_stdout():
            announced.put(self.process.stdout.readline(64))
            while self.process.stdout.read(8192):
                pass

        def drain_stderr():
            while self.process.stderr.read(8192):
                pass

        threading.Thread(target=drain_stdout, daemon=True).start()
        threading.Thread(target=drain_stderr, daemon=True).start()
        try:
            line = announced.get(timeout=10).strip()
        except queue.Empty:
            raise VerificationError("Owned Xray PID was not announced; cleanup needs inspection") from None
        if not re.fullmatch(rb"[0-9]{1,10}", line) or int(line) <= 1:
            raise VerificationError("Refusing an invalid or PID-1 process announcement")
        self.pid = int(line)
        try:
            self.process.stdin.write(json.dumps(config, separators=(",", ":")).encode("ascii"))
            self.process.stdin.close()
        except (OSError, ValueError):
            raise VerificationError("Could not send the isolated runtime configuration") from None

    def require_running(self):
        if self.process is None or self.process.poll() is not None:
            raise VerificationError("The owned native-Xray process exited unexpectedly")

    def matches_owned_process(self) -> bool:
        if self.pid is None or self.pid <= 1:
            return False
        identity = docker_check(["exec", CONTAINER, "cat", f"/proc/{self.pid}/cmdline"], timeout=5)
        args = identity.stdout.rstrip(b"\0").split(b"\0")
        return (identity.returncode == 0 and len(args) == 4
                and args[0].rsplit(b"/", 1)[-1] == b"xray"
                and args[1:] == [b"run", b"-config", b"stdin:"])

    def signal_owned_process(self, signal: str):
        if signal not in {"TERM", "KILL"} or self.pid is None or self.pid <= 1:
            raise VerificationError("Refusing an unsafe process signal")
        # Alpine images may expose kill only as a shell builtin. The script is
        # constant; the strictly positive captured PID is a separate argument.
        return docker_check(["exec", CONTAINER, "sh", "-c", f'kill -{signal} "$1"',
                             "owned-cleanup", str(self.pid)], timeout=5)

    def cleanup(self) -> dict:
        result = {"ownedPid": self.pid, "terminated": False, "signal": None}
        if self.process is None:
            result["terminated"] = True
            return result
        if self.process.poll() is not None:
            result["terminated"] = True
            result["signal"] = "already-exited"
            return result
        if self.pid is None or self.pid <= 1:
            # No guessed PID, PID 1, pkill, killall, or container restart.
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
                result["terminated"] = True
            except subprocess.TimeoutExpired:
                pass
            result["error"] = "Owned PID unavailable; exact-process cleanup needs inspection"
            return result
        try:
            if not self.matches_owned_process():
                if self.process.poll() is not None:
                    result["terminated"] = True
                    result["signal"] = "already-exited"
                else:
                    result["error"] = "Owned stdin process identity could not be confirmed"
                return result
            terminated = self.signal_owned_process("TERM")
            result["signal"] = "TERM"
            result["termCommandReturnCode"] = terminated.returncode
            if terminated.returncode:
                raise VerificationError("Exact owned-process TERM command failed")
            self.process.wait(timeout=5)
            result["terminated"] = True
        except (VerificationError, subprocess.TimeoutExpired):
            # Escalation is permitted only while the original exec is alive and
            # /proc confirms precisely the keyless stdin command, never another
            # process's configuration or environment.
            try:
                if self.process.poll() is not None:
                    result["terminated"] = True
                    result["signal"] = "already-exited"
                elif self.matches_owned_process() and self.process.poll() is None:
                    killed = self.signal_owned_process("KILL")
                    result["signal"] = "KILL-after-identity-check"
                    result["killCommandReturnCode"] = killed.returncode
                    if killed.returncode:
                        raise VerificationError("Exact owned-process KILL command failed")
                    self.process.wait(timeout=5)
                    result["terminated"] = True
                else:
                    result["error"] = "Exact owned-process termination could not be confirmed"
            except (VerificationError, subprocess.TimeoutExpired):
                result["error"] = "Exact owned-process termination could not be confirmed"
        return result


def request_label(port: int, timeout=1.5) -> str | None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "http://special.invalid" + ROUTE_PATH,
                           headers={"Host": DESTINATION, "Connection": "close"})
        response = connection.getresponse()
        body = response.read(64)
        if response.status != 200:
            return None
        if body not in {b"mock-fast", b"mock-slow"}:
            raise VerificationError("Runtime returned an unexpected mock response")
        return body.decode("ascii")
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def wait_selection(runtime: OwnedXray, port: int, expected: str | None, proxies: list[MockProxy]) -> float:
    started = time.monotonic()
    deadline = started + PHASE_TIMEOUT
    consecutive = 0
    while time.monotonic() < deadline:
        runtime.require_running()
        before = sum(proxy.count_connections() for proxy in proxies)
        label = request_label(port, timeout=min(1.5, max(0.01, deadline - time.monotonic())))
        runtime.require_running()
        after = sum(proxy.count_connections() for proxy in proxies)
        # Mere failed connections to offline proxies are not fallback evidence.
        # A fallback-block attempt must fail without connecting to either mock.
        selected = label == expected and (expected is not None or before == after)
        consecutive = consecutive + 1 if selected else 0
        if consecutive >= 3:
            return round(time.monotonic() - started, 3)
        time.sleep(min(0.4, max(0, deadline - time.monotonic())))
    raise VerificationError("Native leastPing selection did not stabilize within 35 seconds")


def verify(fixture: Path) -> dict:
    report = {"ok": False, "scope": "keyless native-Xray localhost mocks",
              "realVpnEndToEnd": False, "existingConfigurationChanged": False,
              "subscriberConfigurationsRead": False, "compile": "not-run"}
    runtime = OwnedXray()
    proxies: list[MockProxy] = []
    started = time.monotonic()
    try:
        payload = read_fixture(fixture)
        report["fixtureSha256"] = hashlib.sha256(payload).hexdigest()
        report["xrayVersion"] = check_container()
        compiled = docker_check(["exec", "-i", CONTAINER, "xray", "run", "-test", "-config", "stdin:"],
                                payload=payload, timeout=30)
        if compiled.returncode:
            report["compile"] = "failed"
            raise VerificationError("Xray rejected the supplied keyless fixture")
        report["compile"] = "passed"
        proxies.append(MockProxy("mock-fast", 0.050))
        proxies.append(MockProxy("mock-slow", 0.250))
        # Reserve an ephemeral loopback port until just before Xray starts.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            inbound_port = reservation.getsockname()[1]
            config = runtime_config(inbound_port, proxies[0].server_port, proxies[1].server_port)
        runtime.start(config)
        timings = report["phaseSeconds"] = {}
        timings["initialFast"] = wait_selection(runtime, inbound_port, "mock-fast", proxies)
        report["initialSelection"] = "mock-fast"
        proxies[0].offline()
        timings["fastOfflineToSlow"] = wait_selection(runtime, inbound_port, "mock-slow", proxies)
        report["failoverSelection"] = "mock-slow"
        proxies[1].offline()
        timings["allOfflineToBlock"] = wait_selection(runtime, inbound_port, None, proxies)
        report["fallbackBlock"] = "verified: failed requests with no mock connection attempts"
        report["ok"] = True
    except VerificationError as error:
        report["error"] = str(error)
    except KeyboardInterrupt:
        report["error"] = "Verification interrupted"
    except Exception as error:
        report["error"] = "Unexpected verifier failure (" + type(error).__name__ + ")"
    finally:
        report["cleanup"] = runtime.cleanup()
        for proxy in proxies:
            proxy.stop()
        if not report["cleanup"]["terminated"]:
            report["ok"] = False
        report["totalSeconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, help="Explicitly provided keyless validation JSON")
    parser.add_argument("--report", type=Path, help="Optional private JSON report file; JSON always prints to stdout")
    args = parser.parse_args()
    if args.report and args.report.resolve() == args.fixture.resolve():
        parser.error("Report must not overwrite the fixture")
    report = verify(args.fixture)
    if args.report:
        try:
            fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(report, stream, indent=2)
                stream.write("\n")
        except OSError:
            report["ok"] = False
            report["reportError"] = "Could not safely write the requested report file"
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
