"""Archives fence obsolete installations without deleting their unfinished journals."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spn_reserve_fleet.cli import main
from spn_reserve_fleet.enrollment import discover
from spn_reserve_fleet.hardening import harden
from spn_reserve_fleet.runtime import Runtime
from spn_reserve_fleet.store import Store


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'home'
        self.journal = self.home / '.spn-reality-deploy' / 'namespace' / 'old'
        self.journal.mkdir(mode=0o700, parents=True)
        self.marker = self.journal / 'unfinished-operation.json'
        self.marker.write_bytes(b'{"pending":"unchanged"}\n')
        (self.journal / 'plan.json').write_text('{}')
        self.installation = {
            'domain': 'old.example.com', 'name': 'Legacy', 'country': 'DE',
            'transport': 'raw-reality-vision-safari-v1', 'current_ip': '8.8.8.8',
            'ssh_port': 22, 'state_dir': str(self.journal), 'installation_id': 'a' * 32,
            'panel_url': 'https://panel.example.com/api', 'ssh_fingerprint': 'SHA256:' + 'A' * 43,
            'pending_ip': '8.8.4.4', 'generation': 0,
        }
        self.probe_calls, self.execute_calls = [], []
        self.runtime = Runtime(self.root / 'fleet', snapshot=lambda path: dict(self.installation),
                               probe=lambda *args: self.probe_calls.append(args) or True,
                               executor=lambda *args: self.execute_calls.append(args))
        self.store = self.runtime.store
        self.server = self.installation['domain']
        self.store.register(self.installation)
        self.store.add_reserve(self.server, '8.8.4.4')
        self.store.update(self.server, last_error='Причина прежнего отказа',
                          last_error_code='topology_changed', last_error_log_id='f' * 32,
                          last_switch_target='8.8.4.4')
        replacement = {**self.installation, 'domain': 'new.example.com', 'name': 'Current TLS',
                       'state_dir': str(self.root / 'new-journal'), 'installation_id': 'b' * 32,
                       'current_ip': '1.1.1.1', 'pending_ip': None,
                       'transport': 'raw-tls-vision-safari-v1'}
        self.replacement = replacement['domain']
        self.store.register(replacement)

    def tearDown(self):
        self.temporary.cleanup()

    def archive(self):
        return self.store.archive(self.server, 'Заменён действующей TLS-установкой', self.replacement)

    def test_archive_preserves_pending_reserves_errors_and_original_journal(self):
        before = self.store.get_server(self.server)
        marker = self.marker.read_bytes()
        result = self.archive()
        self.assertTrue(result['archived'])
        self.assertFalse(result['auto_enabled'])
        self.assertEqual(result['replacement_id'], self.replacement)
        self.assertEqual(result['status'], 'archived')
        for key in ('pending_ip', 'current_ip', 'reserves', 'last_error', 'last_error_code',
                    'last_error_log_id', 'last_switch_target', 'state_dir', 'installation_id'):
            self.assertEqual(result[key], before[key])
        self.assertEqual(self.marker.read_bytes(), marker)
        self.assertEqual([r['id'] for r in self.store.list_servers()], [self.replacement])
        self.assertEqual(len(self.store.list_servers(include_archived=True)), 2)
        self.assertEqual(len(self.store.overview(include_archived=True)['servers']), 2)
        self.assertEqual(self.store.overview()['events'][0]['kind'], 'archived')

    def test_archive_survives_repeated_registration_and_store_restart(self):
        archived = self.archive()
        updated_snapshot = {**self.installation, 'current_ip': '1.0.0.1', 'pending_ip': None, 'generation': 1}
        self.assertEqual(self.store.register(updated_snapshot), archived)
        reopened = Store(self.store.root)
        self.assertTrue(reopened.get_server(self.server)['archived'])
        self.assertNotIn(self.server, [r['id'] for r in reopened.list_servers()])
        self.assertEqual(reopened.get_server(self.server)['pending_ip'], '8.8.4.4')

    def test_discovery_does_not_reimport_archived_path_from_namespace(self):
        self.archive()
        paths = []
        def sync(store, path):
            paths.append(Path(path))
            self.assertNotEqual(Path(path), self.journal)
            return store.get_server(self.replacement)
        with patch('spn_reserve_fleet.enrollment.synchronize', side_effect=sync):
            result = discover(self.store, self.home)
        self.assertEqual(result['errors'], [])
        self.assertNotIn(self.journal, paths)
        self.assertTrue(self.store.get_server(self.server)['archived'])

    def test_direct_operations_are_rejected_before_probe_or_executor(self):
        self.archive()
        actions = (
            lambda: self.runtime.check(self.server),
            lambda: self.runtime.switch(self.server, '8.8.4.4'),
            lambda: self.store.set_auto(self.server, True),
            lambda: self.store.set_auto(self.server, False),
            lambda: self.store.add_reserve(self.server, '1.0.0.1'),
            lambda: self.store.remove_reserve(self.server, '8.8.4.4'),
            lambda: self.store.reserve_result(self.server, '8.8.4.4', True),
            lambda: harden(self.runtime, self.server, env_file='/unused', apply=False),
            lambda: harden(self.runtime, self.server, env_file='/unused', apply=True),
        )
        for action in actions:
            with self.assertRaisesRegex(ValueError, 'архив'):
                action()
        self.assertEqual(self.probe_calls, [])
        self.assertEqual(self.execute_calls, [])

    def test_tick_skips_archived_row(self):
        self.archive()
        seen = []
        with patch.object(self.runtime, 'scan', return_value={}), \
                patch.object(self.runtime, 'check', side_effect=lambda sid: seen.append(sid) or self.store.get_server(sid)):
            self.runtime.tick()
        self.assertEqual(seen, [self.replacement])

    def test_archive_and_restore_use_operational_server_lock(self):
        with self.store.server_lock(self.server):
            with self.assertRaisesRegex(ValueError, 'уже проверяется'):
                self.archive()
        self.archive()
        with self.store.server_lock(self.server):
            with self.assertRaisesRegex(ValueError, 'уже проверяется'):
                self.store.restore(self.server)
        self.assertTrue(self.store.get_server(self.server)['archived'])

    def test_restore_preserves_pending_and_never_enables_automatic_switching(self):
        self.archive()
        restored = self.store.restore(self.server)
        self.assertFalse(restored['archived'])
        self.assertFalse(restored['auto_enabled'])
        self.assertEqual(restored['pending_ip'], '8.8.4.4')
        self.assertEqual(restored['last_error_code'], 'topology_changed')
        self.assertEqual(restored['status'], 'pending')
        with self.assertRaises(ValueError):
            self.store.set_auto(self.server, True)

    def test_archive_replacement_and_reason_are_validated_without_changes(self):
        for replacement in ('absent.example.com', self.server):
            with self.assertRaises(ValueError):
                self.store.archive(self.server, 'reason', replacement)
        for reason in ('', 'x' * 501, 'unsafe\nreason'):
            with self.assertRaises(ValueError):
                self.store.archive(self.server, reason)
        self.assertFalse(self.store.get_server(self.server)['archived'])

    def test_cli_archive_and_restore_keep_same_pending_state(self):
        args = ['--data-dir', str(self.store.root)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args + ['archive', '--server', self.server, '--reason', 'obsolete',
                                          '--replacement', self.replacement]), 0)
            self.assertTrue(self.store.get_server(self.server)['archived'])
            self.assertEqual(main(args + ['restore', '--server', self.server]), 0)
        self.assertEqual(self.store.get_server(self.server)['pending_ip'], '8.8.4.4')
        self.assertFalse(self.store.get_server(self.server)['auto_enabled'])


if __name__ == '__main__':
    unittest.main()
