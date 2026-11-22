"""Offline guard ownership, timeout and rollback tests; no services or sockets."""
import hashlib
from pathlib import Path
import tempfile
import unittest

import guard


ID = 'a1b2c3d4' * 4
IP = '87.251.16.31'


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.root = Path(self.temp.name).resolve()
        self.node = self.root / 'opt/spn-reality-node'
        self.node.mkdir(mode=0o700, parents=True)
        (self.node / '.spn-deployment').write_text(ID + '\n')
        (self.node / '.spn-deployment').chmod(0o600)
        self.config = self.root / 'etc/haproxy/haproxy.cfg'
        self.config.parent.mkdir(parents=True)
        self.original = b'# pristine distro config\nglobal\n    user haproxy\n'
        self.config.write_bytes(self.original)
        self.config.chmod(0o644)
        self.template = self.root / 'haproxy-reality.cfg'
        self.template.write_bytes(Path(guard.__file__).with_name('haproxy-reality.cfg').read_bytes())
        self.template.chmod(0o600)
        self.active = self.enabled = False
        self.fail_start = self.fail_syntax = False
        self.backend_addresses = ['127.0.0.1:2443']
        self.calls = []
        self.fd_calls = []
        self.manager = guard.Guard(IP, ID, root=self.root, run=self.fake_run,
                                   ready=lambda ip, port: True, wait=lambda **kwargs: None,
                                   fd=self.fd, template=self.template,
                                   preread=lambda: {'partialHelloRejected': True, 'observedDeadlineMs': 5000})

    def tearDown(self):
        self.temp.cleanup()

    def fd(self, deployment):
        self.fd_calls.append(deployment)
        return {'fdInUse': 100, 'softLimit': 65536}

    def fake_run(self, *args, allowed=(0,)):
        self.calls.append(args)
        if args[0] == 'ss':
            addresses = self.backend_addresses if args[-1] == 'sport = :2443' else ['0.0.0.0:443']
            return '\n'.join('LISTEN 0 4096 ' + address + ' 0.0.0.0:*' for address in addresses)
        if args[0] == 'haproxy':
            if self.fail_syntax:
                raise guard.GuardError('fake invalid HAProxy configuration')
            return ''
        if args[0] == 'dpkg-query':
            return ' /etc/haproxy/haproxy.cfg ' + hashlib.md5(self.original).hexdigest()
        if args[0] == 'systemctl':
            action = args[1]
            if action == 'is-active':
                if '--quiet' in args:
                    if not self.active:
                        raise guard.GuardError('inactive')
                    return ''
                return 'active' if self.active else 'inactive'
            if action == 'is-enabled':
                return 'enabled' if self.enabled else 'disabled'
            if action == 'enable':
                self.enabled = True
            elif action == 'disable':
                self.enabled = False
            elif action == 'restart':
                if self.fail_start:
                    raise guard.GuardError('fake start failure')
                self.active = True
            elif action == 'stop':
                self.active = False
            else:
                self.fail('Unexpected service action ' + repr(args))
            return ''
        self.fail('Unexpected guard command ' + repr(args))

    def test_installs_guard_from_pristine_config_and_verifies_actual_core(self):
        value = self.manager.install()
        self.assertTrue(value['guardVerified'])
        self.assertFalse(value['tlsTerminated'])
        self.assertEqual(value['maxConnections'], 8192)
        self.assertEqual(self.fd_calls, [ID])
        self.assertEqual(self.config.read_bytes(), self.manager.rendered())
        self.assertTrue(self.active and self.enabled)
        self.assertEqual((self.node / 'guard/before.cfg').read_bytes(), self.original)

    def test_rerun_has_no_duplicate_backup_or_unnecessary_restart(self):
        self.manager.install()
        before = (self.node / 'guard/before.cfg').read_bytes()
        self.calls.clear()
        self.manager.install()
        self.assertEqual(before, (self.node / 'guard/before.cfg').read_bytes())
        self.assertNotIn(('systemctl', 'restart', 'haproxy'), self.calls)

    def test_modified_foreign_configuration_is_never_replaced(self):
        self.config.write_bytes(self.original + b'frontend foreign\n    bind :8443\n')
        with self.assertRaisesRegex(guard.GuardError, 'not pristine'):
            self.manager.install()
        self.assertFalse(any(c[:2] == ('systemctl', 'restart') for c in self.calls))

    def test_syntax_failure_happens_before_configuration_or_service_change(self):
        self.fail_syntax = True
        with self.assertRaisesRegex(guard.GuardError, 'invalid'):
            self.manager.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(any(c[0] == 'systemctl' for c in self.calls))

    def test_start_failure_restores_original_config_and_disabled_state(self):
        self.fail_start = True
        with self.assertRaisesRegex(guard.GuardError, 'start failure'):
            self.manager.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.active or self.enabled)

    def test_pristine_private_config_modes_are_accepted_and_rollback_preserves_mode(self):
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                self.config.chmod(mode)
                self.fail_start = True
                with self.assertRaisesRegex(guard.GuardError, 'start failure'):
                    self.manager.install()
                self.assertEqual(self.config.read_bytes(), self.original)
                # Both subtests reuse the immutable first backup, so explicitly
                # clear only this fake-root guard state between independent cases.
                self.assertEqual(self.config.stat().st_mode & 0o777, mode)
                for file in (self.node / 'guard').iterdir():
                    file.unlink()
                (self.node / 'guard').rmdir()

    def test_backend_must_be_loopback_only(self):
        for addresses in ([], ['0.0.0.0:2443'], ['127.0.0.1:2443', '[::]:2443']):
            with self.subTest(addresses=addresses):
                self.backend_addresses = addresses
                with self.assertRaisesRegex(guard.GuardError, 'only on 127'):
                    self.manager.install()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_other_deployment_marker_is_rejected(self):
        (self.node / '.spn-deployment').write_text('0' * 32 + '\n')
        with self.assertRaisesRegex(guard.GuardError, 'identity differs'):
            self.manager.install()
        self.assertEqual(self.calls, [])

    def test_fd_pressure_fails_guard_verification_and_restores_before_config(self):
        def exhausted(_deployment):
            raise guard.core_fd_health.HealthError('rw-core FD pressure')

        self.manager.fd = exhausted
        with self.assertRaisesRegex(guard.core_fd_health.HealthError, 'FD pressure'):
            self.manager.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.active or self.enabled)

    def test_template_has_no_tls_termination_and_retains_proxy_source(self):
        rendered = self.manager.rendered().decode()
        self.assertIn('bind 0.0.0.0:443\n', rendered)
        self.assertNotIn('bind 0.0.0.0:443 ssl', rendered)
        self.assertNotIn('crt ', rendered)
        self.assertIn('server xray 127.0.0.1:2443 send-proxy-v2 maxconn 8192', rendered)
        self.assertIn('tcp-request inspect-delay 5s', rendered)
        accept = rendered.index('tcp-request content accept if complete_client_hello')
        reject = rendered.index('tcp-request content reject if WAIT_END')
        self.assertLess(accept, reject)
        self.assertIn('acl complete_client_hello req.ssl_hello_type 1', rendered)
        for setting in ('timeout connect 3s', 'timeout client 120s', 'timeout server 120s',
                        'timeout client-fin 5s', 'timeout server-fin 5s', 'maxconn 8192'):
            self.assertIn(setting, rendered)

    def test_guard_files_are_private_and_original_preserved(self):
        self.manager.install()
        state = self.node / 'guard'
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        for path in state.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o644)


class BoundedWaitTests(unittest.TestCase):
    def test_backend_wait_is_bounded_without_sleeping(self):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with self.assertRaisesRegex(guard.GuardError, '45 seconds'):
            guard.wait_backend(ready=lambda: False, now=lambda: clock[0], sleep=sleep)
        self.assertEqual(clock[0], 45)


if __name__ == '__main__':
    unittest.main()
