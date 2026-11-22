"""Offline only: synthetic keys, no network, real credentials or SSH startup."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover import dynamic_relay_worker as dynamic
from spn_ip_failover import probes, worker


NODE_A, NODE_B, NODE_C = [str(n) * 8 + '-1111-4111-8111-' + str(n) * 12 for n in (1, 2, 3)]
SERVICE_A, SERVICE_B, SERVICE_C = [str(n) * 8 + '-2222-4222-8222-' + str(n) * 12 for n in (1, 2, 3)]


def outbound():
    return {'protocol': 'vless', 'settings': {'vnext': [
        {'address': '8.8.8.8', 'port': 443, 'users': [{'id': 'synthetic-client-secret'}]}]},
        'streamSettings': {'network': 'raw', 'security': 'reality',
                           'realitySettings': {'serverName': 'example.com', 'publicKey': 'synthetic'}}}


def node(uuid, address='8.8.4.4', **changes):
    return {'uuid': uuid, 'address': address, 'isConnected': True, 'isDisabled': False, **changes}


class DynamicRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.directory.chmod(0o700)
        self.key, self.known = self.directory / 'key', self.directory / 'known_hosts'
        for path in (self.key, self.known):
            path.write_text('synthetic-public-or-private-data-never-used\n')
            path.chmod(0o600)
        self.audit = worker.load_audit_helper()
        self.config = {'control_mode': 'direct_https', 'xray': '/private/example/xray',
                       'sha256': 'a' * 64, 'routes': {'selected': {'probe': outbound()}}}
        self.request = {'action': 'probe', 'key': 'selected', 'address': '8.8.8.8', 'port': 443}
        self.settings = dynamic.Settings.from_value(self.value())

    def tearDown(self):
        self.temp.cleanup()

    def value(self):
        return {'version': 1, 'remnawave': {'url': 'https://panel.example.invalid', 'credential': 'remnawave'},
                'routes': {'selected': {'node_uuid': NODE_A, 'service_id': SERVICE_A}},
                'relays': [self.row(NODE_A, SERVICE_A), self.row(NODE_B, SERVICE_B), self.row(NODE_C, SERVICE_C)]}

    def row(self, uuid, service):
        return {'node_uuid': uuid, 'service_id': service, 'user': 'spnrelay', 'identity': str(self.key),
                'known_hosts': str(self.known), 'ssh_port': 22}

    def check(self, panel, run, *, request=None, settings=None):
        with patch.object(self.audit, 'pinned_engine'):
            return dynamic.check(self.audit, self.config, settings or self.settings, panel,
                                 request or self.request, run=run)

    def test_service_derived_host_alias_is_constant_across_ip_changes(self):
        relay = self.settings.relays[1]
        first, second = relay.connection('8.8.4.4'), relay.connection('1.1.1.1')
        self.assertEqual(first['ssh_command'], second['ssh_command'])
        self.assertIn('HostKeyAlias=spn-ip-rotation-relay-' + SERVICE_B, first['ssh_command'])
        self.assertEqual(second['host'], 'spnrelay@1.1.1.1')
        self.assertNotIn('synthetic-client-secret', json.dumps(first))

    def test_reverse_transport_is_explicit_service_bound_and_nonroot_only(self):
        value = self.value()
        service = '21e508f0-2ef2-47da-aee7-15dc8c66eb15'
        value['relays'][1].update(service_id=service, reverse_service=service, user='spn-ip-relay')
        selected = dynamic.Settings.from_value(value).relays[1]
        self.assertEqual(selected.connection('1.1.1.1')['reverse_service'], service)
        for changes in ({'reverse_service': SERVICE_B}, {'user': 'root'}, {'user': 'other'},
                        {'ssh_port': 23002}, {'reverse_service': None}):
            invalid = copy.deepcopy(value)
            invalid['relays'][1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dynamic.Settings.from_value(invalid)

    def test_excludes_backend_node_and_service_and_uses_fresh_panel_ip(self):
        panel = Mock()
        panel.read_node.return_value = node(NODE_B, '1.1.1.1')
        run = Mock(return_value='pass')
        self.assertEqual(self.check(panel, run), 'pass')
        self.assertEqual([call.args[0] for call in panel.read_node.call_args_list], [NODE_B, NODE_B])
        self.assertEqual(run.call_args_list[0].args[-1], {'action': 'control', 'key': 'selected'})
        self.assertEqual(run.call_args_list[1].args[-1], self.request)
        self.assertEqual(run.call_args_list[1].args[2]['host'], 'spnrelay@1.1.1.1')
        changed = copy.deepcopy(self.value())
        changed['routes']['selected']['service_id'] = SERVICE_B
        settings = dynamic.Settings.from_value(changed)
        panel.read_node.reset_mock()
        panel.read_node.return_value = node(NODE_C)
        self.assertEqual(self.check(panel, run, settings=settings), 'pass')
        self.assertTrue(all(call.args[0] == NODE_C for call in panel.read_node.call_args_list))

    def test_healthy_control_required_and_transport_unknown_can_use_second_relay(self):
        panel = Mock()
        panel.read_node.side_effect = [node(NODE_B), node(NODE_C, '1.1.1.1'), node(NODE_C, '1.1.1.1')]
        run = Mock(side_effect=['unknown', 'pass', 'pass'])
        self.assertEqual(self.check(panel, run), 'pass')
        self.assertEqual([call.args[-1]['action'] for call in run.call_args_list], ['control', 'control', 'probe'])

    def test_relay_pointing_at_same_backend_or_candidate_ip_is_not_independent(self):
        panel = Mock()
        panel.read_node.side_effect = [node(NODE_B, '8.8.8.8'), node(NODE_C, '1.1.1.1'), node(NODE_C, '1.1.1.1')]
        run = Mock(return_value='pass')
        self.assertEqual(self.check(panel, run), 'pass')
        self.assertTrue(all(call.args[2]['host'] == 'spnrelay@1.1.1.1' for call in run.call_args_list))

    def test_backend_failure_after_working_control_does_not_fanout_all_relays(self):
        panel = Mock()
        panel.read_node.return_value = node(NODE_B)
        run = Mock(side_effect=['pass', 'down'])
        self.assertEqual(self.check(panel, run), 'down')
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[0] == NODE_B for call in panel.read_node.call_args_list))

    def test_control_action_checks_only_eu_https(self):
        panel = Mock()
        panel.read_node.return_value = node(NODE_B)
        run = Mock(return_value='pass')
        self.assertEqual(self.check(panel, run, request={'action': 'control', 'key': 'selected'}), 'pass')
        run.assert_called_once()
        self.assertEqual(run.call_args.args[-1], {'action': 'control', 'key': 'selected'})

    def test_disabled_disconnected_private_dns_or_drifted_relay_is_unknown(self):
        for invalid in (node(NODE_B, isDisabled=True), node(NODE_B, isConnected=False),
                        node(NODE_B, '127.0.0.1'), node(NODE_B, 'relay.example.com'), node(NODE_A)):
            with self.subTest(node=invalid):
                panel = Mock()
                panel.read_node.return_value = invalid
                run = Mock(return_value='pass')
                self.assertEqual(self.check(panel, run), 'unknown')
                run.assert_not_called()
        panel = Mock()
        panel.read_node.side_effect = [node(NODE_B), node(NODE_B, '1.1.1.1'),
                                      RuntimeError('synthetic-api-secret')]
        self.assertEqual(self.check(panel, Mock(return_value='pass')), 'unknown')

    def test_unknown_and_exception_never_turn_into_down(self):
        for output in ('unknown', RuntimeError('synthetic-secret')):
            panel = Mock()
            panel.read_node.side_effect = lambda uuid: node(uuid)
            run = Mock(side_effect=output if isinstance(output, Exception) else None, return_value=output)
            self.assertEqual(self.check(panel, run), 'unknown')
            self.assertLessEqual(run.call_count, 2)

    def test_bad_leaf_or_unowned_key_does_not_read_panel_or_start_relay(self):
        panel, run = Mock(), Mock()
        for request in ({**self.request, 'port': 22}, {**self.request, 'address': '127.0.0.1'},
                        {**self.request, 'key': 'other'}, {**self.request, 'command': 'bad'}):
            with self.subTest(request=request), self.assertRaises((ValueError, KeyError)):
                self.check(panel, run, request=request)
        panel.read_node.assert_not_called()
        run.assert_not_called()

    def test_strict_config_rejects_free_commands_weak_files_root_or_duplicate_ownership(self):
        changes = [lambda v: v.update(command=['bad']),
                   lambda v: v['relays'][0].update(user='root'),
                   lambda v: v['relays'][0].update(ssh_port=True),
                   lambda v: v['relays'][1].update(node_uuid=NODE_A),
                   lambda v: v['relays'][1].update(service_id=SERVICE_A),
                   lambda v: v['relays'][0].update(ProxyCommand='bad'),
                   lambda v: v['remnawave'].update(token_env='SECRET'),
                   lambda v: v['remnawave'].update(credential='../other'),
                   lambda v: v.update(relays=v['relays'][:1])]
        for change in changes:
            value = self.value()
            change(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                dynamic.Settings.from_value(value)
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            dynamic.Settings.from_value(self.value())

    def test_cli_is_bounded_and_redacts_failures(self):
        for raw in (b'x' * 4097, json.dumps(self.request).encode()):
            with patch.object(dynamic.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw))), \
                    patch.object(worker, 'load_audit_helper', side_effect=RuntimeError('synthetic-secret')), \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(dynamic.main(['--config', '/private/a', '--relay-config', '/private/b']), 0)
            self.assertEqual(json.loads(output.getvalue()), {'status': 'unknown'})
            self.assertEqual(errors.getvalue(), '')

    def test_cli_posix_deadline_leaves_cleanup_time_and_hides_interruption(self):
        protected = Mock(side_effect=[self.config, self.value()])
        with patch.object(dynamic.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(self.request).encode()))), \
                patch.object(worker, 'load_audit_helper', return_value=self.audit), \
                patch.object(self.audit, 'protected_json', protected), \
                patch.object(dynamic, 'credential_token', return_value='synthetic-panel-token'), \
                patch.object(dynamic, 'Remnawave') as remna, \
                patch.object(dynamic, 'check', side_effect=KeyboardInterrupt), \
                patch.object(dynamic.signal, 'signal', return_value='old') as signals, \
                patch.object(dynamic.signal, 'setitimer', return_value=(0, 0)) as timers, \
                redirect_stdout(io.StringIO()) as output:
            dynamic.main(['--config', '/private/a', '--relay-config', '/private/b'])
        self.assertEqual(json.loads(output.getvalue()), {'status': 'unknown'})
        self.assertEqual(timers.call_args_list[0].args[1], 35)
        self.assertEqual(timers.call_args_list[-1].args[1], 0)
        self.assertEqual(remna.call_args.kwargs, {'timeout': 5})
        self.assertEqual(signals.call_args_list[-1].args[-1], 'old')


class CredentialPathTests(unittest.TestCase):
    def test_only_direct_systemd_credential_directory_is_accepted(self):
        for path in ('/tmp/stolen', '/run/credentials/../elsewhere', '/run/credentials/unit/extra',
                     'relative', '/run//credentials/unit', '/run/credentials/unit/'):
            with self.subTest(path=path), patch.object(dynamic.os, 'open') as opened, self.assertRaises(ValueError):
                dynamic.credential_token({'CREDENTIALS_DIRECTORY': path})
            opened.assert_not_called()

    def read(self, *, mode=0o400, token=b'synthetic-token\n', symlink=False, systemd_acl=False):
        def lstat(path):
            leaf = str(path) == '/run/credentials/spn-test.service'
            return SimpleNamespace(st_mode=(stat.S_IFLNK if symlink and leaf else stat.S_IFDIR)
                                   | ((0o550 if systemd_acl else 0o700) if leaf else 0o755),
                                   st_uid=0, st_gid=0)
        file = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=0 if systemd_acl else os.geteuid(),
                               st_gid=0, st_nlink=1, st_size=len(token))
        with patch.object(Path, 'lstat', lstat), patch.object(dynamic.os, 'open', return_value=99) as opened, \
                patch.object(dynamic.os, 'fstat', return_value=file), \
                patch.object(dynamic.os, 'fdopen', return_value=io.BytesIO(token)), \
                patch.object(dynamic.os, 'close') as close:
            try:
                result = dynamic.credential_token({'CREDENTIALS_DIRECTORY': '/run/credentials/spn-test.service'})
            finally:
                if opened.called:
                    close.assert_called_once_with(99)
        return result, opened

    def test_private_readable_modes_work_and_other_modes_or_symlinks_fail(self):
        for mode in (0o400, 0o600):
            result, opened = self.read(mode=mode)
            self.assertEqual(result, 'synthetic-token')
            self.assertEqual(opened.call_args.args[0], Path('/run/credentials/spn-test.service/remnawave'))
        for mode in (0o444, 0o640, 0o644, 0o777):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.read(mode=mode)
        result, _ = self.read(mode=0o440, systemd_acl=True)
        self.assertEqual(result, 'synthetic-token')
        with self.assertRaises(ValueError):
            self.read(symlink=True)

    def test_bad_empty_multiline_or_nonascii_token_is_not_accepted(self):
        for raw in (b'', b'a\nb', b' spaces ', b'\xff', b'x' * 8193):
            with self.subTest(raw_length=len(raw)), self.assertRaises((ValueError, UnicodeDecodeError)):
                self.read(token=raw)

    def test_probe_passes_only_credential_directory_not_sensitive_environment(self):
        response = SimpleNamespace(returncode=0, stdout=b'{"status":"pass"}')
        with patch.dict(os.environ, {'PATH': '/usr/bin', 'CREDENTIALS_DIRECTORY': '/run/credentials/example.service',
                                    'REMNAWAVE_API_TOKEN': 'synthetic-secret', 'ONECENT_PASSWORD': 'another-secret'}, clear=True), \
                patch.object(probes.subprocess, 'run', return_value=response) as run:
            self.assertEqual(probes.Probe('outside', 'outside', ('/bin/example',)).run({'action': 'control'}), 'pass')
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/usr/bin', 'CREDENTIALS_DIRECTORY': '/run/credentials/example.service'})


if __name__ == '__main__':
    unittest.main()
