"""Real private journals/SQLite, fake authenticated SSH transport, no network."""
import contextlib
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import struct
import unittest
from unittest.mock import patch

from spn_reserve_fleet.enrollment import installation_snapshot, synchronize
from spn_reserve_fleet.remote import (DELEGATED, INTENT, MAX_ARCHIVE, handoff_installation,
                                     append_node_key, canonical_node_key, install_controller_key,
                                     load_controller, make_archive, receive_archive,
                                     receiver_command, require_local_control, send_archive, validate_controller)
from spn_reserve_fleet.store import Store


TESTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('remote_fleet_journal_fixtures', TESTS / 'test_reserve_fleet_enrollment.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.transport_path = self.root / 'controller-transport.json'
        transport_default = patch('spn_reserve_fleet.controller_transport.DEFAULT_TRANSPORT_CONFIG',
                                  str(self.transport_path))
        transport_default.start()
        self.addCleanup(transport_default.stop)
        self.local_data = self.root / 'local-data'
        self.remote_data = self.root / 'remote-data'
        self.remote_journals = self.root / 'remote-journals'
        self.config = validate_controller({'host': 'controller.example.com',
                                           'data_dir': str(self.remote_data),
                                           'journal_root': str(self.remote_journals)})
        self.sent = []

    def tearDown(self):
        self.temporary.cleanup()

    @contextlib.contextmanager
    def fixture(self, transport='reality'):
        with h.EnrollmentTests().fixture(transport) as fixture:
            yield fixture

    def sender(self, config, request_id, archive):
        self.sent.append(archive)
        return receive_archive(archive, data_dir=self.remote_data,
                               journal_root=self.remote_journals, request_id=request_id)

    def handoff(self, fixture, sender=None):
        return handoff_installation(fixture.store.path, self.config, local_data_dir=self.local_data,
                                    sender=sender or self.sender)

    def test_real_tls_and_reality_journals_transfer_once_and_fence_local_controller(self):
        for transport in ('tls', 'reality'):
            with self.subTest(transport=transport), self.fixture(transport) as fixture:
                # The fixture uses a stable domain; isolate remote inventory per transport.
                self.remote_data = self.root / ('remote-' + transport)
                self.remote_journals = self.root / ('journals-' + transport)
                self.local_data = self.root / ('local-' + transport)
                self.config = validate_controller({'host': 'controller.example.com',
                                                   'data_dir': str(self.remote_data),
                                                   'journal_root': str(self.remote_journals)})
                local = Store(self.local_data)
                server = synchronize(local, fixture.store.path)
                local.add_reserve(server['id'], h.changes.h.NEW)
                local.set_auto(server['id'], True)
                response = self.handoff(fixture)
                self.assertTrue(response['ok'])
                self.assertEqual(response['current_ip'], h.changes.h.IP)
                self.assertEqual(response['generation'], 0)
                self.assertTrue(fixture.store.load(INTENT))
                self.assertTrue(fixture.store.load(DELEGATED))
                self.assertFalse(local.get_server(server['id'])['auto_enabled'])
                central = Store(self.remote_data).get_server(server['id'])
                self.assertFalse(central['auto_enabled'])
                self.assertEqual(central['state_dir'], response['state_dir'])
                self.assertEqual(installation_snapshot(response['state_dir'])['domain'], server['id'])
                with self.assertRaises(ValueError):
                    installation_snapshot(fixture.store.path)
                with self.assertRaises(h.changes.d.u.DeployError):
                    fixture.apply(extra=['--ssh-only'])
                # A retry may read the existing receiver receipt but never overwrite it.
                before = {path.relative_to(Path(response['state_dir'])).as_posix(): path.read_bytes()
                          for path in Path(response['state_dir']).rglob('*') if path.is_file()}
                again = self.handoff(fixture)
                self.assertEqual(response, again)
                self.assertEqual(self.sent[-1], self.sent[-2])
                after = {path.relative_to(Path(response['state_dir'])).as_posix(): path.read_bytes()
                         for path in Path(response['state_dir']).rglob('*') if path.is_file()}
                self.assertEqual(before, after)

    def test_lost_response_keeps_local_fenced_and_same_request_resumes(self):
        with self.fixture('tls') as fixture:
            accepted = []
            def lost(config, request_id, archive):
                accepted.append(self.sender(config, request_id, archive))
                raise ValueError('Lost response')
            with self.assertRaises(ValueError):
                self.handoff(fixture, sender=lost)
            request = fixture.store.load(INTENT)['request_id']
            self.assertIsNone(fixture.store.load(DELEGATED))
            with self.assertRaises(ValueError):
                require_local_control(fixture.store)
            result = self.handoff(fixture)
            self.assertEqual(result['request_id'], request)
            self.assertEqual(result, accepted[0])

    def test_transfer_existing_committed_ssh_only_chain_preserves_history(self):
        with self.fixture('tls') as fixture:
            fixture.apply(extra=['--ssh-only', '--non-interactive'])
            response = self.handoff(fixture)
            self.assertEqual(response['generation'], 1)
            self.assertEqual(response['current_ip'], h.changes.h.NEW)
            remote = Path(response['state_dir'])
            self.assertEqual((remote / 'replacements' / '000001').stat().st_mode & 0o777, 0o700)
            original = fixture.store.path / 'replacements' / '000001' / 'commit.json'
            self.assertEqual((remote / 'replacements' / '000001' / 'commit.json').read_bytes(), original.read_bytes())

    def test_retry_never_overwrites_a_newer_change_on_controller(self):
        with self.fixture('tls') as fixture:
            response = self.handoff(fixture)
            local_store = fixture.store
            fixture.store = h.changes.d.u.Store(response['state_dir'])
            fixture.apply(extra=['--ssh-only', '--non-interactive'])
            committed = fixture.store.load('replacements/000001/commit.json')
            fixture.store = local_store
            again = self.handoff(fixture)
            self.assertEqual(again['generation'], 1)
            self.assertEqual(again['current_ip'], h.changes.h.NEW)
            remote_store = h.changes.d.u.Store(response['state_dir'])
            self.assertEqual(remote_store.load('replacements/000001/commit.json'), committed)
            self.assertEqual(self.sent[-1], self.sent[-2])

    def test_pending_change_cannot_be_handed_off_or_fence_existing_controller(self):
        with self.fixture() as fixture:
            fixture.start()
            with self.assertRaises(ValueError):
                self.handoff(fixture)
            self.assertIsNone(fixture.store.load(INTENT))
            self.assertEqual(self.sent, [])

    def test_remote_new_request_cannot_replace_existing_journal(self):
        with self.fixture() as fixture:
            result = self.handoff(fixture)
            archive, _ = make_archive(fixture.store, 'b' * 32)
            with self.assertRaises(ValueError):
                receive_archive(archive, data_dir=self.remote_data, journal_root=self.remote_journals,
                                request_id='b' * 32)
            self.assertEqual(Store(self.remote_data).get_server(result['domain'])['current_ip'], result['current_ip'])

    def test_receiver_rejects_traversal_symlink_duplicates_and_oversize(self):
        for name, kind in (('../escape.json', tarfile.REGTYPE), ('/escape.json', tarfile.REGTYPE),
                           ('safe.json', tarfile.SYMTYPE), ('replacements/../escape.json', tarfile.REGTYPE)):
            with self.subTest(name=name, kind=kind):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode='w') as archive:
                    entry = tarfile.TarInfo(name)
                    entry.mode, entry.type = 0o600, kind
                    archive.addfile(entry)
                with self.assertRaises(ValueError):
                    receive_archive(stream.getvalue(), data_dir=self.remote_data,
                                    journal_root=self.remote_journals, request_id='a' * 32)
        with self.assertRaises(ValueError):
            receive_archive(b'a' * (MAX_ARCHIVE + 1), data_dir=self.remote_data,
                            journal_root=self.remote_journals, request_id='a' * 32)

    def test_config_is_private_and_commands_cannot_inject_shell(self):
        config_path = self.root / 'controller.json'
        config_path.write_text(json.dumps({'host': '147.45.164.28'}))
        config_path.chmod(0o600)
        config = load_controller(config_path)
        command = receiver_command(config, 'a' * 32)
        self.assertIn('runuser -u spn-reserve -- /opt/spn-reserve-fleet/venv/bin/python', command)
        self.assertTrue(command.startswith('cd /opt/spn-reserve-fleet/current && '))
        config_path.chmod(0o644)
        with self.assertRaises(ValueError):
            load_controller(config_path)
        for body in ({'host': '-oProxyCommand=touch /tmp/unsafe'},
                     {'host': 'good.example.com;touch'}, {'host': 'good.example.com', 'port': True},
                     {'host': 'good.example.com', 'repo_dir': '/tmp/../etc'}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_controller(body)

    def test_sender_uses_only_private_selected_identity(self):
        identity = self.root / 'controller-key'
        identity.write_text('offline-fake-key-not-for-real-ssh')
        identity.chmod(0o600)
        config = validate_controller({**self.config, 'identity_file': str(identity)})
        result = type('Result', (), {'returncode': 0, 'stdout': b'{"ok":true}'})()
        with patch('spn_reserve_fleet.remote.subprocess.run', return_value=result) as run:
            self.assertEqual(send_archive(config, 'a' * 32, b'offline-archive'), {'ok': True})
            args = run.call_args.args[0]
            self.assertIn('IdentitiesOnly=yes', args)
            self.assertEqual(args[args.index('-i') + 1], str(identity))
            self.assertEqual(run.call_args.kwargs['input'], b'offline-archive')
        identity.chmod(0o644)
        with patch('spn_reserve_fleet.remote.subprocess.run', side_effect=AssertionError('network')):
            with self.assertRaises(ValueError):
                send_archive(config, 'a' * 32, b'offline-archive')

    def test_sender_routes_changed_ip_without_changing_controller_or_receiver_identity(self):
        config = validate_controller({**self.config, 'host': '147.45.164.28'})
        original = dict(config)
        self.transport_path.write_text(json.dumps({'format': 1, 'controllers': [
            {'host': config['host'], 'port': config['port'], 'connect_host': '129.101.97.242'}]}))
        self.transport_path.chmod(0o600)
        request_id = 'b' * 32
        expected_receiver = receiver_command(config, request_id)
        result = type('Result', (), {'returncode': 0, 'stdout': b'{"ok":true}'})()
        with patch('spn_reserve_fleet.remote.subprocess.run', return_value=result) as run:
            self.assertEqual(send_archive(config, request_id, b'offline-archive'), {'ok': True})
        args = run.call_args.args[0]
        self.assertIn('HostName=129.101.97.242', args)
        self.assertIn('HostKeyAlias=147.45.164.28', args)
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertLess(args.index('HostName=129.101.97.242'), args.index('--'))
        self.assertEqual(args[-3:], ['--', 'root@147.45.164.28', expected_receiver])
        self.assertEqual(config, original)
        self.assertEqual(run.call_args.kwargs['input'], b'offline-archive')

    def key(self, seed=b'a'):
        raw = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + seed * 32
        return 'ssh-ed25519 ' + base64.b64encode(raw).decode() + ' spn-reserve-controller'

    def test_node_public_key_only_accepts_canonical_ed25519_public_wire(self):
        key = self.key()
        self.assertEqual(canonical_node_key(key), key)
        self.assertEqual(validate_controller({**self.config, 'node_public_key': key})['node_public_key'], key)
        for invalid in (key + '\n', 'command="dangerous" ' + key,
                        'ssh-ed25519 ' + base64.b64encode(b'not-an-ed25519-wire-key').decode(),
                        key + ' "unsafe-comment"', key.replace('ssh-ed25519', 'ssh-rsa', 1)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                canonical_node_key(invalid)

    def test_node_key_append_preserves_other_keys_and_is_idempotent(self):
        home = self.root / 'node-root'
        home.mkdir(mode=0o700)
        original = self.key(b'b').replace('spn-reserve-controller', 'existing-owner-key')
        ssh = home / '.ssh'
        ssh.mkdir(mode=0o700)
        authorized = ssh / 'authorized_keys'
        authorized.write_text(original)  # also exercise missing trailing newline
        authorized.chmod(0o600)
        self.assertEqual(append_node_key(home, self.key(), owner=os.geteuid()), {'ok': True, 'installed': True})
        self.assertEqual(authorized.read_text(), original + '\n' + self.key() + '\n')
        self.assertEqual(append_node_key(home, self.key(), owner=os.geteuid()), {'ok': True, 'installed': False})
        self.assertEqual(authorized.read_text(), original + '\n' + self.key() + '\n')

    def test_node_key_append_rejects_unsafe_paths_and_existing_restricted_key(self):
        for mode in ('symlink', 'writable', 'restricted'):
            home = self.root / mode
            home.mkdir(mode=0o700)
            ssh = home / '.ssh'
            ssh.mkdir(mode=0o700)
            authorized = ssh / 'authorized_keys'
            if mode == 'symlink':
                target = self.root / 'untouched'
                target.write_text('do-not-change')
                authorized.symlink_to(target)
            else:
                authorized.write_text(('from="147.45.164.28" ' if mode == 'restricted' else '') + self.key() + '\n')
                authorized.chmod(0o666 if mode == 'writable' else 0o600)
            before = authorized.read_bytes()
            with self.subTest(mode=mode), self.assertRaises((ValueError, OSError)):
                append_node_key(home, self.key(), owner=os.geteuid())
            self.assertEqual(authorized.read_bytes(), before)

    def test_node_key_install_sends_public_key_only_and_requires_proof(self):
        class SSH:
            def __init__(self):
                self.calls = []
            def run(self, command, **kwargs):
                self.calls.append((command, kwargs))
                compile(kwargs['data'], '<public-key-installer>', 'exec')
                return b'{"ok":true,"installed":true}'
        ssh = SSH()
        self.assertIsNone(install_controller_key(ssh, self.config))
        self.assertEqual(ssh.calls, [])
        self.assertEqual(install_controller_key(ssh, {**self.config, 'node_public_key': self.key()}),
                         {'ok': True, 'installed': True})
        self.assertEqual(len(ssh.calls), 1)
        self.assertIn('ssh-ed25519', ssh.calls[0][0])
        self.assertIn(b'authorized_keys', ssh.calls[0][1]['data'])
        self.assertNotIn(b'PRIVATE KEY', ssh.calls[0][1]['data'])
        with self.fixture('tls') as fixture, patch('spn_reserve_fleet.remote.load_controller',
                return_value={**self.config, 'node_public_key': self.key()}):
            args = h.changes.d.parser().parse_args(['--ip', fixture.plan['ip']])
            h.changes.d.enroll_controller_key(ssh, fixture.store, args)
            receipts = list(fixture.store.path.glob('fleet-controller-key-*.json'))
            self.assertEqual(len(receipts), 1)
            self.assertTrue(json.loads(receipts[0].read_text())['verified'])

    def test_deploy_hook_uses_configured_remote_and_local_default_stays_compatible(self):
        with self.fixture('tls') as fixture:
            args = h.changes.d.parser().parse_args(['--ip', fixture.plan['ip'], '--fleet-dir', str(self.local_data)])
            with patch('spn_reserve_fleet.remote.load_controller', return_value=self.config), \
                    patch('spn_reserve_fleet.remote.send_archive', side_effect=self.sender), fixture.store.lock():
                result = h.changes.d.register_fleet(fixture.store, args)
            self.assertTrue(result['ok'])
            self.assertTrue(fixture.store.load(DELEGATED))


if __name__ == '__main__':
    unittest.main()
