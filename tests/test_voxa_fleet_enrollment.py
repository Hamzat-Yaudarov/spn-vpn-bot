"""Offline enrollment security tests: no sockets, provider writes or real guests."""

import ast
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import ssl
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from voxa_fleet.enrollment import Enrollment, EnrollmentError, NONCE_LIFETIME


OPERATION = "cd72403c-c25b-4c6d-bbdc-c8e7bc98b751"
OTHER = "d62d4b0e-ad76-4a79-bab6-6194bb390d4d"
IP = "8.8.8.8"
MACHINE = "1234567890abcdef1234567890abcdef"


def host_key():
    return ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def script_config(script):
    line = next(line for line in script.splitlines() if line.startswith("CFG=json.loads("))
    literal = line[len("CFG=json.loads("):-1]
    return json.loads(ast.literal_eval(literal))


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.enrollment = Enrollment(self.directory, controller_url="https://9.9.9.9:8443")
        self.key = host_key()

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, operation=OPERATION):
        result = self.enrollment.prepare(operation)
        config = script_config(result["user_data"])
        return result, config

    def payload(self, config, **changes):
        return {"nonce": config["nonce"], "machine_id": MACHINE, "host_key": self.key,
                "assigned_ips": [IP], "observed_at": time.time(), **changes}

    def test_management_key_is_real_ed25519_private_and_stable_across_restarts(self):
        private = self.enrollment.ssh_private_key.read_bytes()
        loaded = serialization.load_ssh_private_key(private, password=None)
        self.assertIsInstance(loaded, ed25519.Ed25519PrivateKey)
        public = loaded.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        self.assertEqual(public, self.enrollment.ssh_public_key.read_bytes().strip())
        restarted = Enrollment(self.directory, controller_url="https://9.9.9.9:8443")
        self.assertEqual(private, restarted.ssh_private_key.read_bytes())
        for path in (self.enrollment.directory, self.enrollment.operations, self.enrollment.tls_directory):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.enrollment.ssh_private_key, self.enrollment.ssh_public_key):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_static_readiness_requires_https_tls_but_never_claims_live_ingress(self):
        not_ready = self.enrollment.ready()
        self.assertFalse(not_ready["ready"])
        self.assertTrue(not_ready["reasons"])
        self.enrollment.ensure_tls()
        ready = self.enrollment.ready()
        self.assertTrue(ready["ready"])
        self.assertTrue(ready["static_only"])
        self.assertFalse(ready["ingress_verified"])
        for url in ("http://9.9.9.9", "https://127.0.0.1", "https://10.1.1.1", "https://localhost",
                    "https://user:secret@9.9.9.9", "https://9.9.9.9/path", "https://9.9.9.9/?secret=yes"):
            self.enrollment.controller_url = url
            self.assertFalse(self.enrollment.ready()["ready"])
            with self.assertRaises(EnrollmentError):
                self.enrollment.prepare(OTHER)

    def test_self_signed_tls_key_matches_cert_and_san_is_real_controller_ip(self):
        tls = self.enrollment.ensure_tls()
        cert = x509.load_pem_x509_certificate(tls["ca_pem"].encode())
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual([str(value) for value in sans.get_values_for_type(x509.IPAddress)], ["9.9.9.9"])
        self.assertEqual(cert.issuer, cert.subject)
        self.assertNotIn("PRIVATE KEY", tls["ca_pem"])
        self.assertEqual(self.enrollment.ensure_tls()["ca_pem"], tls["ca_pem"])
        for path in (tls["key_path"], tls["cert_path"]):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_pinned_public_ca_completes_tls_without_network_or_os_roots(self):
        tls = self.enrollment.ensure_tls()
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(tls["cert_path"], tls["key_path"])
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.load_verify_locations(cadata=tls["ca_pem"])
        incoming_client, outgoing_client = ssl.MemoryBIO(), ssl.MemoryBIO()
        incoming_server, outgoing_server = ssl.MemoryBIO(), ssl.MemoryBIO()
        client_socket = client.wrap_bio(incoming_client, outgoing_client, server_hostname="9.9.9.9")
        server_socket = server.wrap_bio(incoming_server, outgoing_server, server_side=True)
        completed = set()
        for _ in range(20):
            for name, connection in (("client", client_socket), ("server", server_socket)):
                try:
                    connection.do_handshake()
                    completed.add(name)
                except ssl.SSLWantReadError:
                    pass
            if outgoing_client.pending:
                incoming_server.write(outgoing_client.read())
            if outgoing_server.pending:
                incoming_client.write(outgoing_server.read())
            if len(completed) == 2:
                break
        self.assertEqual(completed, {"client", "server"})

    def test_controller_certificate_identity_cannot_change_silently(self):
        self.enrollment.ensure_tls()
        self.enrollment.controller_url = "https://8.8.4.4:8443"
        with self.assertRaises(EnrollmentError):
            self.enrollment.ensure_tls()
        self.assertFalse(self.enrollment.ready()["ready"])

    def test_prepare_is_durable_exact_replay_and_nonce_bound_to_operation(self):
        prepared, config = self.prepare()
        restarted = Enrollment(self.directory, controller_url="https://9.9.9.9:8443")
        self.assertEqual(restarted.prepare(OPERATION), prepared)
        other = script_config(restarted.prepare(OTHER)["user_data"])
        self.assertNotEqual(config["nonce"], other["nonce"])
        self.assertEqual(config["url"], "https://9.9.9.9:8443/bootstrap/" + OPERATION)
        self.assertEqual(set(prepared), {"ssh_public_key", "user_data"})
        state_path = self.enrollment.operations / (OPERATION + ".json")
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        state = json.loads(state_path.read_bytes())
        self.assertEqual(state["expires_at"] - state["issued_at"], NONCE_LIFETIME)
        self.enrollment._clock = lambda: state["expires_at"] + 1
        self.assertEqual(self.enrollment.prepare(OPERATION), prepared)
        self.assertEqual(json.loads(state_path.read_bytes())["expires_at"], state["expires_at"])

    def test_script_contains_only_public_material_nonce_and_stdlib_pinned_https(self):
        prepared, config = self.prepare()
        script = prepared["user_data"]
        self.assertTrue(script.startswith("#!/bin/bash\n"))
        self.assertLess(len(script.encode()), 65_536)
        self.assertIn("ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)", script)
        self.assertIn("load_verify_locations(cadata=CFG['ca_pem'])", script)
        self.assertIn("urllib.request.ProxyHandler({})", script)
        self.assertIn("NoRedirect()", script)
        self.assertIn("/etc/ssh/ssh_host_ed25519_key.pub", script)
        self.assertIn("/etc/machine-id", script)
        self.assertNotIn("PRIVATE KEY", script)
        self.assertNotIn("cryptography", script)
        self.assertNotIn("Authorization", script)
        self.assertNotIn("verify=False", script)
        self.assertNotIn("_create_unverified_context", script)
        self.assertNotIn(self.enrollment.ssh_private_key.read_text(), script)
        python_body = script.split("<<'VOXA_ENROLLMENT_PY'\n", 1)[1].rsplit("\nVOXA_ENROLLMENT_PY", 1)[0]
        compile(python_body, "<private-guest-script>", "exec")

    def test_accept_stores_untrusted_receipt_then_verify_binds_api_ip_and_ssh_key(self):
        _, config = self.prepare()
        payload = self.payload(config)
        accepted = self.enrollment.accept(OPERATION, payload, IP)
        self.assertTrue(accepted["accepted"])
        self.assertFalse(accepted["verified"])
        self.assertNotIn("nonce", accepted)
        verified = self.enrollment.verify(OPERATION, IP)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["machine_id"], MACHINE)
        self.assertEqual(verified["host_key"], self.key)
        wire = base64.b64decode(self.key.split()[1])
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip("=")
        self.assertEqual(verified["host_key_sha256"], expected)
        self.assertNotIn(config["nonce"], json.dumps(verified))

    def test_callback_before_known_service_ip_does_not_establish_wrong_ownership(self):
        _, config = self.prepare()
        self.enrollment.accept(OPERATION, self.payload(config), IP)
        for wrong in ("8.8.4.4", "127.0.0.1", "10.0.0.1"):
            with self.assertRaises(EnrollmentError):
                self.enrollment.verify(OPERATION, wrong)
        _, other = self.prepare(OTHER)
        self.enrollment.accept(OTHER, self.payload(other, assigned_ips=["8.8.4.4"]), IP)
        with self.assertRaises(EnrollmentError):
            self.enrollment.verify(OTHER, IP)

    def test_nonce_constant_time_validation_rejects_cross_operation_and_wrong_secret(self):
        _, config = self.prepare()
        _, other = self.prepare(OTHER)
        for payload in (self.payload(other), self.payload(config, nonce="A" * 43)):
            with self.assertRaises(EnrollmentError) as caught:
                self.enrollment.accept(OPERATION, payload, IP)
            self.assertNotIn(config["nonce"], str(caught.exception))
            self.assertNotIn(other["nonce"], str(caught.exception))
        with patch("voxa_fleet.enrollment.hmac.compare_digest", wraps=__import__("hmac").compare_digest) as compare:
            self.enrollment.accept(OPERATION, self.payload(config), IP)
        self.assertTrue(any(call.args[0] == config["nonce"].encode() for call in compare.call_args_list))

    def test_expired_or_stale_first_callback_is_rejected_without_refreshing_nonce(self):
        _, config = self.prepare()
        self.enrollment._clock = lambda: config["expires_at"] + 1
        with self.assertRaises(EnrollmentError):
            self.enrollment.accept(OPERATION, self.payload(config), IP)
        self.enrollment._clock = time.time
        with self.assertRaises(EnrollmentError):
            self.enrollment.accept(OPERATION, self.payload(config, observed_at=time.time() - 1000), IP)
        with self.assertRaises(EnrollmentError):
            self.enrollment.accept(OPERATION, self.payload(config, observed_at=time.time() + 1000), IP)

    def test_exact_duplicate_is_accepted_after_expiry_but_first_receipt_is_immutable(self):
        _, config = self.prepare()
        payload = self.payload(config)
        first = self.enrollment.accept(OPERATION, payload, IP)
        self.enrollment._clock = lambda: config["expires_at"] + 1
        self.assertEqual(self.enrollment.accept(OPERATION, copy.deepcopy(payload), IP), first)
        for changes, source in (({"host_key": host_key()}, IP), ({"machine_id": "a" * 32}, IP),
                                ({"observed_at": payload["observed_at"] + 1}, IP), ({}, "8.8.4.4")):
            with self.assertRaises(EnrollmentError):
                self.enrollment.accept(OPERATION, {**payload, **changes}, source)
        self.assertEqual(self.enrollment.verify(OPERATION, IP)["host_key"], self.key)

    def test_concurrent_delivery_can_only_commit_one_host_identity(self):
        _, config = self.prepare()
        payloads = [self.payload(config), self.payload(config, host_key=host_key())]
        def deliver(payload):
            try:
                return self.enrollment.accept(OPERATION, payload, IP)["host_key"]
            except EnrollmentError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(deliver, payloads))
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        self.assertIn(self.enrollment.verify(OPERATION, IP)["host_key"], [value for value in outcomes if value])

    def test_actual_wire_key_not_just_prefix_is_required(self):
        _, config = self.prepare()
        non_ed = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        for key in ("ssh-ed25519 PUBLIC_TEST_FIXTURE", "ssh-ed25519 " + base64.b64encode(b"wrong-wire-key").decode(), non_ed):
            with self.assertRaises(EnrollmentError):
                self.enrollment.accept(OPERATION, self.payload(config, host_key=key), IP)

    def test_bounded_machine_id_assigned_ips_and_actual_source(self):
        _, config = self.prepare()
        invalid = [{"machine_id": "0" * 32}, {"machine_id": "not-an-id"}, {"assigned_ips": []},
                   {"assigned_ips": [IP] * 33}, {"assigned_ips": [[IP]]}, {"assigned_ips": ["127.0.0.1"]},
                   {"assigned_ips": ["10.0.0.1"]}, {"observed_at": float("nan")},
                   {"host_key": self.key + "A" * 17_000}, {"unexpected": "private-value"}]
        for changes in invalid:
            with self.assertRaises(EnrollmentError) as caught:
                self.enrollment.accept(OPERATION, self.payload(config, **changes), IP)
            self.assertNotIn(config["nonce"], str(caught.exception))
        for source in ("127.0.0.1", "10.0.0.1", "X-Forwarded-For:8.8.8.8"):
            with self.assertRaises(EnrollmentError):
                self.enrollment.accept(OPERATION, self.payload(config), source)
        accepted = self.enrollment.accept(OPERATION, self.payload(config), "::ffff:8.8.8.8")
        self.assertEqual(accepted["source_ip"], IP)

    def test_unknown_unprepared_uuid_and_missing_receipt_never_verify(self):
        for operation in ("../unsafe", "", "00000000-0000-0000-0000-000000000000"):
            with self.assertRaises(EnrollmentError):
                self.enrollment.prepare(operation)
        _, config = self.prepare()
        with self.assertRaises(EnrollmentError):
            self.enrollment.verify(OPERATION, IP)
        with self.assertRaises(EnrollmentError):
            self.enrollment.accept(OTHER, self.payload(config), IP)

    def test_unsafe_permissions_or_symlink_never_repair_or_expose_private_keys(self):
        self.enrollment.ssh_private_key.chmod(0o644)
        with self.assertRaises(EnrollmentError):
            Enrollment(self.directory, controller_url="https://9.9.9.9:8443")
        self.enrollment.ssh_private_key.chmod(0o600)
        target = self.directory / "aliased"
        target.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            Enrollment(target, controller_url="https://9.9.9.9:8443")


if __name__ == "__main__":
    unittest.main()
