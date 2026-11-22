"""Safe switch diagnostics survive journal synchronization without leaking logs."""
from pathlib import Path
from types import SimpleNamespace
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from spn_reserve_fleet.runtime import (ChangeExecutor, Runtime, SwitchFailure,
                                       classify_switch_failure)


LOG_ID = 'a7' * 16
SECRET = 'PRIVATE-TEST-CREDENTIAL-DO-NOT-EXPOSE'
TOPOLOGY = ('Нода, хост, профиль или аудитория изменились во время замены; '
            'чужие правки не перезаписываю.')


class FailureClassificationTests(unittest.TestCase):
    def test_known_script_and_ssh_failures_have_fixed_public_categories(self):
        examples = {
            'topology_changed': TOPOLOGY,
            'ssh_identity_changed': '@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @',
            'ssh_unavailable': 'ssh: connect to host 8.8.4.4 port 22: Connection timed out',
            'ssh_auth_failed': 'root@8.8.4.4: Permission denied (publickey,password).',
            'dns_failed': 'DNS IPv4 differs from the committed predecessor and this replacement',
            'certificate_failed': 'Остановлено: Небезопасные права или размер TLS-сертификата.',
            'subscription_export_failed': 'Остановлено: Новый хост отсутствует в свежей подписке или найден неоднозначно.',
            'route_changed': 'Stopped: Default route changed outside this operation',
            'policy_routing': 'Stopped: Policy routing is not supported; no route was changed',
            'guard_failed': 'Защита резервных IP и её сохранение после перезагрузки не подтверждены',
            'api_failed': 'Ответ панели потерян или некорректен. POST автоматически не повторяется;',
            'verification_mode_changed': 'Режим проверки незавершённой операции отличается.',
        }
        for expected, marker in examples.items():
            with self.subTest(code=expected):
                result = classify_switch_failure(SECRET + '\n' + marker, LOG_ID)
                self.assertIsInstance(result, SwitchFailure)
                self.assertEqual(result.code, expected)
                self.assertEqual(result.log_id, LOG_ID)
                self.assertEqual(str(result), result.public_message)
                self.assertNotIn(SECRET, str(result))

    def test_unknown_or_forged_messages_never_become_public_error_text(self):
        result = classify_switch_failure(SECRET, LOG_ID)
        self.assertEqual(result.code, 'switch_failed')
        self.assertNotIn(SECRET, str(result))
        for code in ('topology_changed', 'unknown-code'):
            failure = SwitchFailure(code, SECRET, LOG_ID)
            self.assertNotIn(SECRET, str(failure))
            self.assertNotIn(SECRET, failure.public_message)

    def test_classification_reads_only_a_bounded_tail(self):
        prefix = 'ssh: connect to host 8.8.4.4 port 22: Connection timed out\n'
        self.assertEqual(classify_switch_failure(prefix + 'x' * 70000, LOG_ID).code,
                         'switch_failed')
        self.assertEqual(classify_switch_failure(b'x' * 70000 + TOPOLOGY.encode(), LOG_ID).code,
                         'topology_changed')

    def test_untrusted_log_identifier_is_not_accepted_as_a_path(self):
        for value in ('../secret', '/private/secret.log', 'x' * 32, LOG_ID + '.log', SECRET):
            with self.subTest(value=value):
                try:
                    result = classify_switch_failure(TOPOLOGY, value)
                except ValueError as error:
                    self.assertNotIn(SECRET, str(error))
                else:
                    self.assertIsNone(result.log_id)


class RuntimeFailurePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.installation = {
            'domain': 'failure.example.com', 'name': 'Германия', 'country': 'DE',
            'transport': 'raw-tls-vision-safari-v1', 'current_ip': '8.8.8.8',
            'ssh_port': 22, 'state_dir': str(self.root / 'owned-journal'),
            'installation_id': 'b' * 32, 'panel_url': 'https://panel.example.com/api',
            'ssh_fingerprint': 'SHA256:' + 'A' * 43, 'pending_ip': None, 'generation': 0,
        }
        self.runtime = Runtime(self.root / 'fleet', snapshot=lambda path: dict(self.installation),
                               probe=lambda ip, port: True, executor=self.interrupt)
        self.store = self.runtime.store
        self.server_id = self.installation['domain']
        self.store.register(self.installation)
        self.store.add_reserve(self.server_id, '8.8.4.4')

    def tearDown(self):
        self.temp.cleanup()

    def interrupt(self, server, ip):
        self.installation['pending_ip'] = ip
        raise classify_switch_failure(SECRET + '\n' + TOPOLOGY, LOG_ID)

    def test_detailed_failure_survives_pending_register_check_and_discovery(self):
        self.store.set_auto(self.server_id, True)
        with self.assertRaises(ValueError):
            self.runtime.switch(self.server_id, '8.8.4.4')
        failed = self.store.get_server(self.server_id)
        self.assertEqual(failed['last_error_code'], 'topology_changed')
        self.assertEqual(failed['last_error_log_id'], LOG_ID)
        self.assertEqual(failed['last_switch_target'], '8.8.4.4')
        self.assertEqual(failed['pending_ip'], '8.8.4.4')
        self.assertFalse(failed['auto_enabled'])
        self.assertNotIn(SECRET, str(failed))
        self.store.register(dict(self.installation))
        self.runtime.check(self.server_id)
        with patch('spn_reserve_fleet.enrollment.Path.home', return_value=self.root), \
                patch('spn_reserve_fleet.enrollment.synchronize',
                      side_effect=lambda store, path: store.register(dict(self.installation))):
            self.runtime.scan()
        synchronized = self.store.get_server(self.server_id)
        for field in ('last_error', 'last_error_code', 'last_error_log_id',
                      'pending_ip', 'last_switch_target'):
            self.assertEqual(synchronized[field], failed[field], field)
        self.assertFalse(synchronized['auto_enabled'])

    def test_unknown_executor_exception_is_scrubbed_from_state_and_events(self):
        def unknown(server, ip):
            self.installation['pending_ip'] = ip
            raise RuntimeError(SECRET)
        self.runtime.executor = unknown
        with self.assertRaises(ValueError) as caught:
            self.runtime.switch(self.server_id, '8.8.4.4')
        value = self.store.get_server(self.server_id)
        self.assertEqual(value['last_error_code'], 'switch_failed')
        self.assertIsNone(value['last_error_log_id'])
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, str(self.store.overview()))

    def test_successful_same_target_resume_clears_stale_failure_details(self):
        with self.assertRaises(ValueError):
            self.runtime.switch(self.server_id, '8.8.4.4')
        def complete(server, ip):
            self.installation.update(current_ip=ip, pending_ip=None, generation=1)
        self.runtime.executor = complete
        result = self.runtime.switch(self.server_id, '8.8.4.4')
        self.assertEqual(result['current_ip'], '8.8.4.4')
        for field in ('last_error', 'last_error_code', 'last_error_log_id'):
            self.assertIsNone(result[field], field)
        self.assertIsNone(result['pending_ip'])


class ExecutorFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.executor = ChangeExecutor(self.root, timeout=3)
        self.server = {'domain': 'failure.example.com', 'state_dir': '/private/owned',
                       'panel_url': 'https://panel.example.com/api'}

    def tearDown(self):
        self.temp.cleanup()

    def test_nonzero_child_is_classified_without_exposing_private_log(self):
        def child(*args, **kwargs):
            kwargs['stdout'].write((SECRET + '\n' + TOPOLOGY).encode())
            return SimpleNamespace(pid=55555, wait=Mock(return_value=2))
        with patch('spn_reserve_fleet.runtime.subprocess.Popen', side_effect=child):
            with self.assertRaises(SwitchFailure) as caught:
                self.executor(self.server, '8.8.4.4')
        failure = caught.exception
        self.assertEqual(failure.code, 'topology_changed')
        self.assertRegex(failure.log_id, r'^[0-9a-f]{32}$')
        self.assertNotIn(SECRET, str(failure))
        path = self.executor.logs / (failure.log_id + '.log')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn(SECRET, path.read_text())

    def test_launch_failure_has_safe_reason_and_log_reference(self):
        with patch('spn_reserve_fleet.runtime.subprocess.Popen', side_effect=OSError(SECRET)):
            with self.assertRaises(SwitchFailure) as caught:
                self.executor(self.server, '8.8.4.4')
        self.assertEqual(caught.exception.code, 'executor_failed')
        self.assertNotIn(SECRET, str(caught.exception))

    def test_timeout_terminates_only_child_group_and_has_safe_reason(self):
        process = SimpleNamespace(pid=55555,
                                  wait=Mock(side_effect=[subprocess.TimeoutExpired('private', 3), 0]))
        with patch('spn_reserve_fleet.runtime.subprocess.Popen', return_value=process), \
                patch('spn_reserve_fleet.runtime.os.killpg') as kill:
            with self.assertRaises(SwitchFailure) as caught:
                self.executor(self.server, '8.8.4.4')
        self.assertEqual(caught.exception.code, 'switch_timeout')
        kill.assert_called_once_with(55555, signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
