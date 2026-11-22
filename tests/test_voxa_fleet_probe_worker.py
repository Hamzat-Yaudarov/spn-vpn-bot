"""Offline independent worker tests: no SSH, Xray execution or network I/O."""

from contextlib import contextmanager
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from voxa_fleet.enrollment import Enrollment
from voxa_fleet.probe import ProbeRunner, load_audit_helper
from voxa_fleet import probe_worker as worker


TARGET = "8.8.8.8"
OBSERVER = "9.9.9.9"
CONTROLLER = "https://1.0.0.1:8443"
PRIVATE = "4773118c-1bca-4e38-9b90-5385226df0e8"
PATH = {"interface": "eth0", "source": "192.168.1.5", "backend": "Linux"}


def config(**changes):
    return {"engine": "/owner/pinned/xray", "sha256": "a" * 64, "interface": "eth0", **changes}


def outbound():
    return {"protocol": "vless", "settings": {"vnext": [{"address": TARGET, "port": 443,
        "users": [{"id": PRIVATE, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
        "streamSettings": {"network": "raw", "security": "reality", "realitySettings": {
            "serverName": "www.cloudflare.com", "fingerprint": "safari", "publicKey": "A" * 43,
            "shortId": "1234567890abcdef"}}}


def request(**changes):
    return {"action": "probe", "target_ip": TARGET, "expected_exit_ip": TARGET,
            "outbound": outbound(), **changes}


class LocalRunner:
    def __init__(self, engine, sha256, *, interface=None, outside_command=None):
        self.engine, self.sha256, self.interface = Path(engine), sha256, interface
        self.outside_command = outside_command
        self._audit = load_audit_helper()
        self._audit.pinned_engine = MagicMock()
        self._physical_path = MagicMock(return_value=PATH)
        self._vpn = MagicMock(return_value=True)
        self.check = MagicMock(side_effect=AssertionError("outside recursion is forbidden"))

    _leaf = ProbeRunner._leaf
    _trace_args = staticmethod(ProbeRunner._trace_args)
    _trace_ok = staticmethod(ProbeRunner._trace_ok)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.config_path = self.directory / "worker.json"
        self.config_path.write_text(json.dumps(config()))
        self.config_path.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def instance(self, **changes):
        return worker.ProbeWorker(config(**changes), runner_factory=LocalRunner)

    def execute(self, instance, data, *, run=None):
        with patch.object(worker.platform, "system", return_value="Linux"), \
             patch.object(worker, "_bounded_run", side_effect=run,
                          return_value=(0, ("ip=" + OBSERVER + "\n200").encode())):
            return instance.execute(data)

    def controller_material(self):
        enrollment = Enrollment(self.directory / "controller", controller_url=CONTROLLER)
        tls = enrollment.ensure_tls()
        pem = tls["ca_pem"]
        digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        return pem, digest

    def test_private_config_modes_and_exact_fields_are_enforced(self):
        self.assertEqual(worker.load_config(self.config_path), config())
        self.config_path.chmod(0o644)
        with self.assertRaises(Exception):
            worker.load_config(self.config_path)
        self.config_path.chmod(0o600)
        self.config_path.write_text(json.dumps(config(outside_command=["arbitrary-command"])))
        with self.assertRaises(Exception):
            worker.load_config(self.config_path)
        alias = self.directory / "alias.json"
        alias.symlink_to(self.config_path)
        with self.assertRaises(Exception):
            worker.load_config(alias)

    def test_duplicate_or_nonfinite_config_json_is_rejected(self):
        for raw in ('{"engine":"/owner/xray","engine":"/other/xray"}',
                    '{"engine":"/owner/xray","sha256":NaN,"interface":"eth0"}'):
            self.config_path.write_text(raw)
            with self.assertRaises(Exception):
                worker.load_config(self.config_path)

    def test_probe_protocol_uses_actual_direct_observer_and_does_not_recurse(self):
        instance = self.instance()
        result = self.execute(instance, request())
        self.assertEqual(set(result), {"target_ip", "expected_exit_ip", "vpn_ok", "controls_ok",
                                       "observed_at", "independent", "observer_ip"})
        self.assertEqual(result["target_ip"], TARGET)
        self.assertEqual(result["expected_exit_ip"], TARGET)
        self.assertEqual(result["observer_ip"], OBSERVER)
        self.assertTrue(result["vpn_ok"])
        self.assertTrue(result["controls_ok"])
        self.assertTrue(result["independent"])
        self.assertIsNone(instance.runner.outside_command)
        instance.runner.check.assert_not_called()
        instance.runner._audit.pinned_engine.assert_called_once_with(instance.runner.engine, "a" * 64)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_definitive_vpn_failure_keeps_good_independent_control(self):
        instance = self.instance()
        instance.runner._vpn.return_value = False
        result = self.execute(instance, request())
        self.assertIs(result["vpn_ok"], False)
        self.assertIs(result["controls_ok"], True)

    def test_local_engine_or_control_failures_are_unknown_not_service_failure(self):
        instance = self.instance()
        instance.runner._audit.pinned_engine.side_effect = RuntimeError(PRIVATE)
        result = self.execute(instance, request())
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["controls_ok"])
        self.assertNotIn(PRIVATE, json.dumps(result))
        instance = self.instance()
        for rc, raw in ((28, b""), (0, b"ip=127.0.0.1\n200"),
                        (0, b"ip=9.9.9.9\n302"), (0, b"ip=9.9.9.9\nip=9.9.9.9\n200")):
            result = self.execute(instance, request(), run=lambda *args, rc=rc, raw=raw, **kwargs: (rc, raw))
            self.assertEqual(result["status"], "unknown")
        instance.runner._vpn.assert_not_called()

    def test_same_target_observer_is_not_independent(self):
        instance = self.instance()
        result = self.execute(instance, request(), run=lambda *args, **kwargs: (0, b"ip=8.8.8.8\n200"))
        self.assertEqual(result["status"], "unknown")
        instance.runner._vpn.assert_not_called()

    def test_route_change_invalidates_probe_evidence(self):
        instance = self.instance()
        instance.runner._physical_path.side_effect = [PATH, PATH, PATH, {**PATH, "source": "192.168.1.6"}]
        result = self.execute(instance, request())
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("vpn_ok", result)

    def test_malformed_requests_private_target_and_untrusted_programs_do_not_execute(self):
        cases = [request(target_ip="127.0.0.1"), request(expected_exit_ip="10.0.0.1"),
                 request(observer_ip="caller-invented"), request(action="run-shell"),
                 request(command=["/bin/sh"]), {}, [], {"action": "control", "command": "secret"}]
        for data in cases:
            instance = self.instance()
            with patch.object(worker.platform, "system", return_value="Linux"), \
                 patch.object(worker, "_bounded_run") as run:
                result = instance.execute(data)
            self.assertEqual(result["status"], "unknown")
            run.assert_not_called()
            instance.runner._vpn.assert_not_called()

    def test_linux_only_worker_does_not_use_darwin_runtime(self):
        instance = self.instance()
        with patch.object(worker.platform, "system", return_value="Darwin"), \
             patch.object(worker, "_bounded_run") as run:
            result = instance.execute(request())
        self.assertEqual(result["status"], "unknown")
        run.assert_not_called()

    def test_control_action_proves_real_observer_without_vpn_or_credentials(self):
        instance = self.instance()
        result = self.execute(instance, {"action": "control"})
        self.assertEqual(result["observer_ip"], OBSERVER)
        self.assertTrue(result["controls_ok"])
        self.assertTrue(result["independent"])
        instance.runner._vpn.assert_not_called()

    def test_enrollment_health_checks_allowed_origin_ca_and_fresh_challenge(self):
        pem, digest = self.controller_material()
        instance = self.instance(controller_url=CONTROLLER, controller_ca_sha256=digest)
        ca_paths = []
        def run(argv, **kwargs):
            self.assertNotIn(PRIVATE, json.dumps(argv))
            self.assertNotIn("--insecure", argv)
            self.assertNotIn("--location", argv)
            if "--cacert" not in argv:
                return 0, ("ip=" + OBSERVER + "\n200").encode()
            path = Path(argv[argv.index("--cacert") + 1])
            ca_paths.append(path)
            self.assertEqual(path.read_text(), pem)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(argv[argv.index("--interface") + 1], "eth0")
            parsed = urlsplit(argv[-1])
            self.assertEqual(parsed.path, "/healthz")
            challenge = parse_qs(parsed.query)["challenge"][0]
            self.assertEqual(len(challenge), 32)
            data = {"ok": True, "component": worker.HEALTH_COMPONENT, "challenge": challenge}
            return 0, json.dumps(data).encode() + b"\n200"
        result = self.execute(instance, {"action": "enrollment-check", "url": CONTROLLER, "ca_pem": pem}, run=run)
        self.assertTrue(result["reachable"])
        self.assertEqual(result["controller_url"], CONTROLLER)
        self.assertEqual(result["observer_ip"], OBSERVER)
        self.assertTrue(result["independent"])
        self.assertTrue(ca_paths)
        self.assertFalse(ca_paths[0].exists())

    def test_enrollment_ssrf_redirect_or_wrong_ca_never_sends_request(self):
        pem, digest = self.controller_material()
        instance = self.instance(controller_url=CONTROLLER, controller_ca_sha256=digest)
        for origin, certificate in (("https://127.0.0.1:8443", pem), ("https://169.254.169.254", pem),
                                    ("https://8.8.4.4", pem), (CONTROLLER + "/private", pem),
                                    ("https://example.com", pem), (CONTROLLER, "bad PEM")):
            with patch.object(worker.platform, "system", return_value="Linux"), \
                 patch.object(worker, "_bounded_run") as run:
                result = instance.execute({"action": "enrollment-check", "url": origin, "ca_pem": certificate})
            self.assertEqual(result["status"], "unknown")
            run.assert_not_called()
        other = Enrollment(self.directory / "other", controller_url=CONTROLLER).ensure_tls()["ca_pem"]
        with patch.object(worker.platform, "system", return_value="Linux"), \
             patch.object(worker, "_bounded_run") as run:
            result = instance.execute({"action": "enrollment-check", "url": CONTROLLER, "ca_pem": other})
        self.assertEqual(result["status"], "unknown")
        run.assert_not_called()

    def test_public_health_without_matching_challenge_is_not_ingress_proof(self):
        pem, digest = self.controller_material()
        instance = self.instance(controller_url=CONTROLLER, controller_ca_sha256=digest)
        for body, status in (({"ok": True, "component": worker.HEALTH_COMPONENT}, b"200"),
                             ({"ok": True, "component": worker.HEALTH_COMPONENT, "challenge": "old"}, b"200"),
                             ({"ok": True, "component": "admin-ui", "challenge": "old"}, b"200"),
                             ({"ok": True, "component": worker.HEALTH_COMPONENT, "challenge": "old"}, b"302")):
            def run(argv, **kwargs):
                return ((0, ("ip=" + OBSERVER + "\n200").encode()) if "--cacert" not in argv
                        else (0, json.dumps(body).encode() + b"\n" + status))
            result = self.execute(instance, {"action": "enrollment-check", "url": CONTROLLER, "ca_pem": pem}, run=run)
            self.assertEqual(result["status"], "unknown")

    def test_cli_one_bounded_json_output_unknown_is_redacted(self):
        output = io.StringIO()
        code = worker.main(["--config", str(self.config_path)],
            stdin=io.BytesIO(json.dumps({"action": "shell", "password": PRIVATE}).encode()), stdout=output)
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_cli_rejects_oversized_duplicate_and_malformed_stdin_before_worker(self):
        for raw in (b"x" * (worker.MAX_STDIN + 1), b"bad-json", b'{"action":"control","action":"probe"}'):
            with patch.object(worker, "ProbeWorker") as construct:
                output = io.StringIO()
                code = worker.main(["--config", str(self.config_path)], stdin=io.BytesIO(raw), stdout=output)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "unknown")
            # Constructor can be called for malformed JSON, but execute never is.
            construct.return_value.execute.assert_not_called()

    def test_cli_control_success_has_zero_exit_and_no_remote_shell(self):
        output = io.StringIO()
        fake = MagicMock()
        fake.execute.return_value = {"controls_ok": True, "observer_ip": OBSERVER,
            "observed_at": time.time(), "independent": True}
        with patch.object(worker, "ProbeWorker", return_value=fake):
            code = worker.main(["--config", str(self.config_path)],
                stdin=io.BytesIO(b'{"action":"control"}'), stdout=output)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["observer_ip"], OBSERVER)
        fake.execute.assert_called_once_with({"action": "control"})

    def test_deadline_context_sets_and_restores_signal_timer(self):
        with patch.object(worker.signal, "getsignal", return_value="previous"), \
             patch.object(worker.signal, "signal") as handler, \
             patch.object(worker.signal, "setitimer", return_value=(0, 0)) as timer:
            with worker._deadline():
                pass
        self.assertEqual(timer.call_args_list[0].args, (worker.signal.ITIMER_REAL, worker.DEADLINE_SECONDS))
        self.assertEqual(timer.call_args_list[-1].args, (worker.signal.ITIMER_REAL, 0, 0))
        self.assertEqual(handler.call_args_list[-1].args, (worker.signal.SIGALRM, "previous"))


if __name__ == "__main__":
    unittest.main()
