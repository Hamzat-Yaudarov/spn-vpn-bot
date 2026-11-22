"""Forward-only REALITY SNI upgrade ownership and optional real HAProxy probes."""
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import guard
import reality_sni_guard as sni
import test_guard


HOST = 'www.cloudflare.com'
PROOF = {'validSniHandshakeVerified': True, 'unknownSniRejected': True, 'missingSniRejected': True}


class SniUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_guard.GuardTests()
        self.fixture.setUp()
        self.fixture.manager.install()
        self.manager = self.fixture.manager
        self.base = self.manager.rendered()
        self.original_intent = (self.manager.state / 'intent.json').read_bytes()
        self.original_backup = (self.manager.state / 'before.cfg').read_bytes()
        self.reload_failure = self.probe_failure = False
        self.probes = []
        self.manager.run = self.fake_run
        self.upgrade = sni.SniGuard(test_guard.IP, test_guard.ID, HOST, manager=self.manager, probe=self.probe)
        self.fixture.calls.clear()

    def tearDown(self):
        self.fixture.tearDown()

    def fake_run(self, *args, allowed=(0,)):
        if args[:2] in (('systemctl', 'reload'), ('systemctl', 'start')):
            self.fixture.calls.append(args)
            if self.reload_failure:
                raise guard.GuardError('injected reload failure')
            self.fixture.active = True
            return ''
        return self.fixture.fake_run(*args, allowed=allowed)

    def probe(self, hostname):
        self.probes.append(hostname)
        if self.probe_failure:
            raise guard.GuardError('injected SNI probe failure')
        return dict(PROOF)

    def write_json(self, path, value):
        path.write_bytes(sni.json_bytes(value))
        path.chmod(0o600)

    def test_upgrade_preserves_original_template_intent_and_backup(self):
        proof = self.upgrade.install()
        self.assertTrue(proof['sniFiltered'])
        self.assertFalse(proof['tlsTerminated'])
        self.assertEqual(proof['allowedSni'], HOST)
        self.assertEqual(self.manager.rendered(), self.base)
        self.assertEqual((self.manager.state / 'intent.json').read_bytes(), self.original_intent)
        self.assertEqual((self.manager.state / 'before.cfg').read_bytes(), self.original_backup)
        self.assertEqual((self.upgrade.directory / 'before.cfg').read_bytes(), self.base)
        self.assertEqual(self.manager.config.read_bytes(), sni.rendered(self.base, HOST))
        self.assertIn(('systemctl', 'reload', 'haproxy'), self.fixture.calls)
        self.assertNotIn(('systemctl', 'restart', 'haproxy'), self.fixture.calls)
        self.assertTrue(self.manager.verify()['guardVerified'])
        self.assertTrue(self.upgrade.verify()['unknownSniRejected'])

    def test_rerun_does_not_reload_or_duplicate_original_backup(self):
        self.upgrade.install()
        self.fixture.calls.clear()
        self.upgrade.install()
        self.assertNotIn(('systemctl', 'reload', 'haproxy'), self.fixture.calls)
        self.assertEqual((self.manager.state / 'intent.json').read_bytes(), self.original_intent)
        self.assertEqual(self.probes, [HOST, HOST])

    def test_failed_reload_preserves_exact_forward_only_intent_and_can_resume(self):
        self.reload_failure = True
        with self.assertRaisesRegex(guard.GuardError, 'reload failure'):
            self.upgrade.install()
        desired = sni.rendered(self.base, HOST)
        self.assertEqual(self.manager.config.read_bytes(), desired)
        self.assertTrue((self.upgrade.directory / 'intent.json').exists())
        self.assertFalse((self.upgrade.directory / 'ready.json').exists())
        with self.assertRaisesRegex(guard.GuardError, 'unfinished'):
            self.manager.verify()
        self.reload_failure = False
        self.assertTrue(self.upgrade.install()['sniFiltered'])
        self.assertEqual(self.manager.config.read_bytes(), desired)
        self.assertEqual((self.manager.state / 'intent.json').read_bytes(), self.original_intent)

    def test_failed_proof_does_not_mark_upgrade_ready_or_downgrade_policy(self):
        self.probe_failure = True
        with self.assertRaisesRegex(guard.GuardError, 'probe failure'):
            self.upgrade.install()
        self.assertEqual(self.manager.config.read_bytes(), sni.rendered(self.base, HOST))
        self.assertFalse((self.upgrade.directory / 'ready.json').exists())
        self.probe_failure = False
        self.upgrade.install()
        self.assertTrue(self.manager.verify()['guardVerified'])

    def test_base_installer_resumes_pending_upgrade_without_rewriting_its_legacy_hash(self):
        self.reload_failure = True
        with self.assertRaises(guard.GuardError):
            self.upgrade.install()
        self.reload_failure = False
        with patch.object(sni, 'probe_sni', side_effect=self.probe):
            self.manager.install()
        self.assertTrue((self.upgrade.directory / 'ready.json').exists())
        self.assertEqual((self.manager.state / 'intent.json').read_bytes(), self.original_intent)
        self.assertEqual(self.manager.config.read_bytes(), sni.rendered(self.base, HOST))

    def test_existing_upgrade_is_not_rolled_back_when_core_health_fails(self):
        self.upgrade.install()
        def failure(_deployment):
            raise guard.GuardError('injected unhealthy core')
        self.manager.fd = failure
        with self.assertRaisesRegex(guard.GuardError, 'unhealthy core'):
            self.manager.install()
        self.assertEqual(self.manager.config.read_bytes(), sni.rendered(self.base, HOST))

    def test_another_sni_is_rejected_without_modifying_current_policy(self):
        self.upgrade.install()
        desired = self.manager.config.read_bytes()
        other = sni.SniGuard(test_guard.IP, test_guard.ID, 'example.com', manager=self.manager, probe=self.probe)
        with self.assertRaises(guard.GuardError):
            other.install()
        self.assertEqual(self.manager.config.read_bytes(), desired)

    def test_foreign_configuration_is_rejected_before_upgrade_state_is_created(self):
        modified = self.base + b'\n# administrator change\n'
        self.manager.config.write_bytes(modified)
        with self.assertRaisesRegex(guard.GuardError, 'outside'):
            self.upgrade.install()
        self.assertFalse(self.upgrade.directory.exists())
        self.assertEqual(self.manager.config.read_bytes(), modified)

    def test_syntax_failure_precedes_upgrade_intent_and_live_change(self):
        self.fixture.fail_syntax = True
        with self.assertRaisesRegex(guard.GuardError, 'invalid'):
            self.upgrade.install()
        self.assertFalse(self.upgrade.directory.exists())
        self.assertEqual(self.manager.config.read_bytes(), self.base)

    def test_tampered_desired_or_ready_receipt_cannot_authorize_changed_config(self):
        self.upgrade.install()
        desired_path = self.upgrade.directory / 'desired.cfg'
        desired = desired_path.read_bytes()
        desired_path.write_bytes(desired + b'\n# changed\n')
        with self.assertRaisesRegex(guard.GuardError, 'proof differs'):
            self.manager.verify()
        desired_path.write_bytes(desired)
        ready_path = self.upgrade.directory / 'ready.json'
        receipt = json.loads(ready_path.read_bytes())
        receipt['sni'] = 'example.com'
        self.write_json(ready_path, receipt)
        with self.assertRaisesRegex(guard.GuardError, 'receipt differs'):
            self.manager.verify()

    def test_original_guard_marker_is_insufficient_without_readiness_and_hash_proof(self):
        (self.manager.state / 'ready.json').unlink()
        with self.assertRaisesRegex(guard.GuardError, 'missing'):
            self.upgrade.install()
        self.assertFalse(self.upgrade.directory.exists())

    def test_symlinked_or_public_upgrade_files_are_rejected(self):
        self.upgrade.install()
        path = self.upgrade.directory / 'desired.cfg'
        path.chmod(0o644)
        with self.assertRaisesRegex(guard.GuardError, 'Unsafe'):
            self.manager.verify()
        path.chmod(0o600)
        backup = path.with_name('other.cfg')
        path.rename(backup)
        path.symlink_to(backup)
        with self.assertRaises(OSError):
            self.manager.verify()

    def test_private_upgrade_permissions(self):
        self.upgrade.install()
        self.assertEqual(self.upgrade.directory.stat().st_mode & 0o777, 0o700)
        for path in self.upgrade.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sni_validation_rejects_injection_wildcards_addresses_and_empty_names(self):
        for value in ('', 'localhost', '*.example.com', '8.8.8.8', 'example.com\n', 'example.com;id',
                      'example.com acl evil', '-bad.example.com', 'a' * 64 + '.example.com'):
            with self.subTest(value=value), self.assertRaises(guard.GuardError):
                sni.hostname(value)
        self.assertEqual(sni.hostname('WWW.Cloudflare.COM'), HOST)


@unittest.skipUnless(shutil.which('haproxy') and shutil.which('openssl'),
                     'Real ClientHello gate test needs HAProxy and OpenSSL')
class RealSniGateTests(unittest.TestCase):
    """An actual HAProxy gates real TLS and preserves the PROXY-v2 backend link."""
    def test_matching_sni_completes_tls_and_other_sni_never_reaches_backend(self):
        with tempfile.TemporaryDirectory(prefix='spn-sni-live-test-') as temporary:
            root = Path(temporary)
            key, cert = root / 'key.pem', root / 'cert.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=' + HOST,
                            '-addext', 'subjectAltName=DNS:' + HOST], check=True, capture_output=True)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(cert), str(key))
            client_context = ssl.create_default_context(cafile=str(cert))
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(0.2)
            backend_port = listener.getsockname()[1]
            with socket.socket() as available:
                available.bind(('127.0.0.1', 0))
                front_port = available.getsockname()[1]
            stop, accepted, errors = threading.Event(), [], []

            def read_exact(connection, length):
                result = b''
                while len(result) < length:
                    part = connection.recv(length - len(result))
                    if not part:
                        raise ValueError('Truncated PROXY header')
                    result += part
                return result

            def backend():
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    accepted.append(True)
                    try:
                        with connection:
                            connection.settimeout(3)
                            header = read_exact(connection, 16)
                            if header[:12] != b'\r\n\r\n\0\r\nQUIT\n':
                                raise ValueError('Missing PROXY-v2 header')
                            read_exact(connection, int.from_bytes(header[14:16], 'big'))
                            with server_context.wrap_socket(connection, server_side=True) as secured:
                                secured.recv(1)
                    except Exception as error:
                        errors.append(str(error))

            worker = threading.Thread(target=backend, daemon=True)
            worker.start()
            base = Path(guard.__file__).with_name('haproxy-reality.cfg').read_bytes()
            candidate = sni.rendered(base, HOST).decode()
            candidate = candidate.replace('    user haproxy\n', '').replace('    group haproxy\n', '')
            candidate = candidate.replace('    ulimit-n 65536\n', '')
            candidate = candidate.replace('bind 0.0.0.0:443', 'bind 127.0.0.1:' + str(front_port))
            candidate = candidate.replace('127.0.0.1:2443', '127.0.0.1:' + str(backend_port))
            config = root / 'haproxy.cfg'
            config.write_text(candidate)
            process = subprocess.Popen(['haproxy', '-db', '-f', str(config)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with socket.create_connection(('127.0.0.1', front_port), timeout=0.2):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail('HAProxy did not start')
                        time.sleep(0.05)
                with patch.object(sni.ssl, 'create_default_context', return_value=client_context):
                    proof = sni.probe_sni(HOST, address='127.0.0.1', port=front_port, timeout=3)
                self.assertEqual(proof, PROOF)
                self.assertEqual(len(accepted), 1)
                self.assertEqual(errors, [])
            finally:
                process.terminate()
                process.communicate(timeout=5)
                stop.set()
                listener.close()
                worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
