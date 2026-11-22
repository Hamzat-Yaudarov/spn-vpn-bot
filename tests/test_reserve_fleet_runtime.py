"""No remote network: real SQLite + fake SSH and authoritative change journals."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spn_reserve_fleet.cli import main, monitor_lock
from spn_reserve_fleet.probe import check_ssh
from spn_reserve_fleet.runtime import Runtime
from spn_reserve_fleet.store import Store


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.installation = {'domain': 'vpn.example.com', 'name': 'Германия', 'country': 'FI',
                             'transport': 'raw-tls-vision-safari-v1', 'current_ip': '8.8.8.8',
                             'ssh_port': 22, 'state_dir': '/private/deployment', 'installation_id': 'a' * 32,
                             'panel_url': 'https://panel.example.com/api', 'ssh_fingerprint': 'SHA256:' + 'A' * 43,
                             'pending_ip': None, 'generation': 0}
        self.up = {'8.8.4.4': True}
        self.calls = []
        self.runtime = Runtime(Path(self.temp.name).resolve() / 'fleet', clock=lambda: self.now,
                               snapshot=lambda path: dict(self.installation),
                               probe=lambda ip, port: self.up.get(ip, False), executor=self.execute)
        self.store = self.runtime.store
        self.server_id = self.installation['domain']
        self.store.register(self.installation)
        self.store.add_reserve(self.server_id, '8.8.4.4')

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, server, ip):
        self.calls.append(ip)
        self.installation.update(current_ip=ip, pending_ip=None, generation=self.installation['generation'] + 1)

    def check(self):
        result = self.runtime.check(self.server_id)
        self.now += 30
        return result

    def test_three_failures_switch_only_to_responding_reserve(self):
        self.store.set_auto(self.server_id, True)
        self.assertEqual(self.check()['failures'], 1)
        self.assertEqual(self.check()['failures'], 2)
        result = self.check()
        self.assertEqual(result['current_ip'], '8.8.4.4')
        self.assertEqual(result['failures'], 0)
        self.assertTrue(result['auto_enabled'])
        self.assertEqual(self.calls, ['8.8.4.4'])
        self.assertEqual([item['ip'] for item in result['reserves']], ['8.8.8.8'])
        self.assertEqual(self.store.overview()['events'][0]['kind'], 'switch_completed')

    def test_monitor_off_still_observes_without_changes(self):
        for _ in range(4):
            self.check()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.get_server(self.server_id)['status'], 'offline')

    def test_no_internet_or_no_working_reserve_does_not_rotate(self):
        self.up.clear()
        self.store.set_auto(self.server_id, True)
        for _ in range(4):
            self.check()
        self.assertEqual(self.calls, [])
        self.assertIn('не отвечают', self.store.get_server(self.server_id)['last_error'])
        events = self.store.overview()['events']
        self.assertEqual(sum(event['kind'] == 'no_reserve' for event in events), 1)

    def test_successful_ssh_resets_failure_series(self):
        self.store.set_auto(self.server_id, True)
        self.check()
        self.up['8.8.8.8'] = True
        self.assertEqual(self.check()['failures'], 0)
        self.up['8.8.8.8'] = False
        self.assertEqual(self.check()['failures'], 1)
        self.assertEqual(self.calls, [])

    def test_old_failure_series_expires(self):
        self.check()
        self.check()
        self.now += 1000
        self.assertEqual(self.check()['failures'], 1)

    def test_crash_mid_switch_disables_auto_and_only_same_ip_can_resume(self):
        def interrupted(server, ip):
            self.installation['pending_ip'] = ip
            raise RuntimeError('hidden sensitive details')
        self.runtime.executor = interrupted
        self.store.set_auto(self.server_id, True)
        with self.assertRaises(ValueError):
            self.runtime.switch(self.server_id, '8.8.4.4')
        result = self.store.get_server(self.server_id)
        self.assertFalse(result['auto_enabled'])
        self.assertEqual(result['pending_ip'], '8.8.4.4')
        self.assertNotIn('sensitive', str(result))
        self.store.add_reserve(self.server_id, '1.1.1.1')
        with self.assertRaises(ValueError):
            self.runtime.switch(self.server_id, '1.1.1.1')
        self.runtime.executor = self.execute
        self.assertEqual(self.runtime.switch(self.server_id, '8.8.4.4')['current_ip'], '8.8.4.4')

    def test_committed_switch_with_lost_response_is_adopted(self):
        def committed_then_error(server, ip):
            self.execute(server, ip)
            raise RuntimeError('lost response')
        self.runtime.executor = committed_then_error
        result = self.runtime.switch(self.server_id, '8.8.4.4')
        self.assertEqual(result['current_ip'], '8.8.4.4')
        self.assertIsNone(result['pending_ip'])

    def test_manual_script_change_is_discovered_and_old_ip_is_kept(self):
        self.installation.update(current_ip='8.8.4.4', generation=1)
        self.up['8.8.4.4'] = True
        result = self.check()
        self.assertEqual(result['current_ip'], '8.8.4.4')
        self.assertEqual([item['ip'] for item in result['reserves']], ['8.8.8.8'])

    def test_cooldown_prevents_rapid_return_to_old_ip(self):
        self.store.set_auto(self.server_id, True)
        self.runtime.switch(self.server_id, '8.8.4.4')
        self.up = {'8.8.8.8': True}
        for _ in range(4):
            self.check()
        self.assertEqual(self.calls, ['8.8.4.4'])

    def test_cannot_switch_to_unregistered_or_private_ip(self):
        for ip in ('1.1.1.1', '127.0.0.1', '10.0.0.1'):
            with self.assertRaises(ValueError):
                self.runtime.switch(self.server_id, ip)
        self.assertEqual(self.calls, [])

    def test_server_and_monitor_locks_are_cross_instance(self):
        other = Store(self.store.root)
        with self.store.server_lock(self.server_id):
            with self.assertRaises(ValueError):
                with other.server_lock(self.server_id):
                    pass
        with monitor_lock(self.store.root):
            with self.assertRaises(ValueError):
                with monitor_lock(self.store.root):
                    pass

    def test_rescanning_preserves_auto_and_consecutive_failures(self):
        self.store.set_auto(self.server_id, True)
        self.store.update(self.server_id, failures=2)
        result = self.store.register(self.installation)
        self.assertTrue(result['auto_enabled'])
        self.assertEqual(result['failures'], 2)

    def test_dry_switch_does_not_create_state_or_contact_network(self):
        path = Path(self.temp.name).resolve() / 'unused'
        with contextlib.redirect_stdout(io.StringIO()), patch('socket.create_connection', side_effect=AssertionError):
            result = main(['--data-dir', str(path), 'switch', '--server', self.server_id, '--ip', '1.1.1.1'])
        self.assertEqual(result, 0)
        self.assertFalse(path.exists())


class BannerTests(unittest.TestCase):
    def banner(self, chunks):
        socket = unittest.mock.MagicMock()
        socket.__enter__.return_value = socket
        socket.recv.side_effect = chunks
        return socket

    def test_requires_real_ssh_banner_and_accepts_pre_banner_lines(self):
        connection = self.banner([b'Notice\r\nSSH-2.', b'0-OpenSSH_9.9\r\n'])
        with patch('socket.create_connection', return_value=connection):
            self.assertTrue(check_ssh('8.8.8.8'))
        with patch('socket.create_connection', return_value=self.banner([b'HTTP/1.1 200 OK\r\n', b''])):
            self.assertFalse(check_ssh('8.8.8.8'))

    def test_connection_failure_is_unavailable(self):
        with patch('socket.create_connection', side_effect=TimeoutError):
            self.assertFalse(check_ssh('8.8.8.8'))

    def test_banner_input_is_bounded(self):
        connection = self.banner([b'a' * 1023 + b'\n'] * 8)
        with patch('socket.create_connection', return_value=connection):
            self.assertFalse(check_ssh('8.8.8.8'))
        self.assertEqual(connection.recv.call_count, 8)
