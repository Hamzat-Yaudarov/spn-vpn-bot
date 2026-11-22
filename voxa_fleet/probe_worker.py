"""Forced-command independent Linux probe worker; one bounded JSON stdin request.

Run ``python -m voxa_fleet.probe_worker --config /ABS/private-0600.json``
from a separately installed, restricted SSH forced-command wrapper. Never use
a target-VPS SSH tunnel as the outside vantage. The observer address comes
from the worker's own physically bound HTTPS control, not the caller.

Config: engine, sha256, interface; optional controller_url (public IPv4 HTTPS
origin) and controller_ca_sha256 (SHA256 of the certificate's DER bytes).
No caller-provided programs, shell fragments, URL paths or proxy settings run.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import ssl
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

from .probe import (CURL, ProbeRunner, _bounded_run, _interface, _json,
                    _public_ip, _require)


MAX_STDIN = 65_536
MAX_CONFIG = 16_384
DEADLINE_SECONDS = 40
HEALTH_COMPONENT = "voxa-enrollment"


class WorkerError(RuntimeError):
    """Safe static failure; private requests and engine output are never exposed."""


def _unknown():
    return {"status": "unknown", "reason": "independent_probe_unavailable",
            "controls_ok": False, "independent": False, "observed_at": time.time()}


def _origin(value):
    try:
        _require(isinstance(value, str) and value.isascii() and len(value) <= 512
                 and not any(character.isspace() for character in value))
        parsed = urlsplit(value)
        _require(parsed.scheme == "https" and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment and parsed.path in ("", "/")
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
        host = _public_ip(parsed.hostname)
        authority = host + (":" + str(parsed.port) if parsed.port not in (None, 443) else "")
        return urlunsplit(("https", authority, "", "", ""))
    except Exception:
        raise WorkerError("Only the configured public IPv4 HTTPS controller origin is accepted") from None


def validate_config(config):
    _require(isinstance(config, dict) and set(config) <= {
        "engine", "sha256", "interface", "controller_url", "controller_ca_sha256"}
        and {"engine", "sha256", "interface"} <= set(config))
    _require(isinstance(config["engine"], str) and Path(config["engine"]).is_absolute()
             and isinstance(config["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", config["sha256"]) is not None)
    _interface(config["interface"])
    result = dict(config)
    if "controller_url" in result or "controller_ca_sha256" in result:
        _require({"controller_url", "controller_ca_sha256"} <= set(result))
        result["controller_url"] = _origin(result["controller_url"])
        _require(isinstance(result["controller_ca_sha256"], str)
                 and re.fullmatch(r"[0-9a-f]{64}", result["controller_ca_sha256"]) is not None)
    return result


def load_config(path):
    """Read only a uid-owned 0600 regular file in a uid-owned 0700 directory."""
    path = Path(path)
    _require(path.is_absolute() and not any(part.is_symlink() for part in (path, *path.parents)))
    parent = path.parent.stat()
    _require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.geteuid()
             and stat.S_IMODE(parent.st_mode) == 0o700)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                 and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
                 and 0 < before.st_size <= MAX_CONFIG)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_CONFIG + 1)
        after, named = os.fstat(descriptor), path.stat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                                  value.st_mode, value.st_nlink)
        _require(len(raw) == before.st_size and identity(before) == identity(after) == identity(named))
    finally:
        os.close(descriptor)
    return validate_config(_json(raw))


class ProbeWorker:
    """Use ProbeRunner's bounded local primitives; never call recursive check()."""

    def __init__(self, config, *, runner_factory=ProbeRunner):
        self.config = validate_config(config)
        self.runner = runner_factory(self.config["engine"], self.config["sha256"],
                                     interface=self.config["interface"], outside_command=None)

    def _observer(self):
        """Derive the actual direct public egress IP with verified TLS, no proxy."""
        before = self.runner._physical_path("1.1.1.1")
        rc, raw = _bounded_run(self.runner._trace_args(interface=self.config["interface"]), timeout=15, limit=4096)
        _require(self.runner._trace_ok(rc, raw) and self.runner._physical_path("1.1.1.1") == before)
        body, _ = raw.decode("ascii").rsplit("\n", 1)
        addresses = [line[3:] for line in body.splitlines() if line.startswith("ip=")]
        _require(len(addresses) == 1)
        return _public_ip(addresses[0]), before

    def _enrollment_request(self, request):
        _require(set(request) == {"action", "url", "ca_pem"}
                 and "controller_url" in self.config and "controller_ca_sha256" in self.config)
        origin = _origin(request["url"])
        _require(origin == self.config["controller_url"])
        pem = request["ca_pem"]
        _require(isinstance(pem, str) and pem.isascii() and 1 <= len(pem) <= 16_384
                 and pem.count("-----BEGIN CERTIFICATE-----") == 1
                 and pem.count("-----END CERTIFICATE-----") == 1 and "PRIVATE KEY" not in pem)
        try:
            der = ssl.PEM_cert_to_DER_cert(pem)
            actual = hashlib.sha256(der).hexdigest()
            _require(hmac.compare_digest(actual, self.config["controller_ca_sha256"]))
            # Parse/validate the PEM using an otherwise empty trust store, not OS CAs.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=pem)
        except Exception:
            raise WorkerError("Controller certificate pin was rejected") from None
        return origin, pem

    def _enrollment_health(self, origin, pem):
        target = urlsplit(origin).hostname
        before = self.runner._physical_path(target)
        challenge = secrets.token_hex(16)
        with tempfile.TemporaryDirectory(prefix="voxa-worker-ca-") as temporary:
            path = Path(temporary) / "public-ca.pem"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(pem)
            args = [CURL, "--disable", "--silent", "--show-error", "--retry", "0", "--connect-timeout", "4",
                "--max-time", "8", "--max-filesize", "4096", "--proto", "=https", "--tlsv1.2", "--ipv4",
                "--noproxy", "*", "--proxy", "", "--interface", self.config["interface"],
                "--cacert", str(path), "--write-out", "\n%{http_code}",
                origin + "/healthz?challenge=" + challenge]
            rc, raw = _bounded_run(args, timeout=11, limit=4096)
        _require(rc == 0 and len(raw) <= 4096 and self.runner._physical_path(target) == before)
        body, status = raw.rsplit(b"\n", 1)
        _require(status.strip() == b"200")
        data = _json(body)
        _require(isinstance(data, dict) and data.get("ok") is True and data.get("component") == HEALTH_COMPONENT
                 and isinstance(data.get("challenge"), str) and hmac.compare_digest(data["challenge"], challenge))

    def execute(self, request):
        """Return safe evidence or explicit unknown; never echo private request data."""
        try:
            _require(platform.system() == "Linux" and isinstance(request, dict)
                     and len(json.dumps(request, allow_nan=False).encode()) <= MAX_STDIN)
            action = request.get("action")
            if action == "probe":
                _require(set(request) == {"action", "target_ip", "expected_exit_ip", "outbound"})
                target = _public_ip(request["target_ip"])
                expected = _public_ip(request["expected_exit_ip"])
                leaf = self.runner._leaf(request["outbound"], target)
            elif action == "control":
                _require(set(request) == {"action"})
            elif action == "enrollment-check":
                origin, pem = self._enrollment_request(request)
            else:
                raise WorkerError("Unsupported probe action")
            self.runner._audit.pinned_engine(self.runner.engine, self.runner.sha256)
            observer, control_path = self._observer()
            base = {"controls_ok": True, "observed_at": time.time(), "observer_ip": observer, "independent": True}
            if action == "control":
                return base
            if action == "enrollment-check":
                _require(observer != urlsplit(origin).hostname)
                self._enrollment_health(origin, pem)
                _require(self.runner._physical_path("1.1.1.1") == control_path)
                return {**base, "reachable": True, "controller_url": origin, "observed_at": time.time()}
            _require(observer != target)
            path = self.runner._physical_path(target)
            vpn_ok = self.runner._vpn(leaf, path, expected)
            _require(type(vpn_ok) is bool and self.runner._physical_path(target) == path
                     and self.runner._physical_path("1.1.1.1") == control_path)
            return {**base, "target_ip": target, "expected_exit_ip": expected,
                    "vpn_ok": vpn_ok, "observed_at": time.time()}
        except Exception:
            return _unknown()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise WorkerError("Only fixed worker arguments are accepted")


@contextmanager
def _deadline():
    def expired(signum, frame):
        raise WorkerError("Independent probe deadline exceeded")
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, previous)


def main(argv=None, *, stdin=None, stdout=None):
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("--config", required=True)
        args = parser.parse_args(argv)
        with _deadline():
            config = load_config(args.config)
            raw = stdin.read(MAX_STDIN + 1)
            _require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_STDIN)
            worker = ProbeWorker(config)
            response = worker.execute(_json(raw))
    except Exception:
        response = _unknown()
    stdout.write(json.dumps(response, allow_nan=False, separators=(",", ":")) + "\n")
    return 2 if response.get("status") == "unknown" else 0


if __name__ == "__main__":
    raise SystemExit(main())
