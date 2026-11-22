"""Offline trust-state tests: no real SSH, DNS, VPS or known_hosts mutation."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('reality_ssh_trust_tested', HERE / 'ssh_trust.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
REAL_GLOBAL_KEYS = t._global_keys
IP = '177.3.250.75'


def key(number):
    raw = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + bytes([number]) * 32
    return base64.b64encode(raw).decode(), 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')


class Store:
    def __init__(self, path):
        self.path = path

    def load(self, name):
        path = self.path / name
        if not path.exists() and not path.is_symlink():
            return None
        t._private_file(path)
        return json.loads(path.read_text())

    def save(self, name, value):
        old = self.load(name)
        if old is not None:
            if old != value:
                raise RuntimeError('immutable receipt changed')
            return
        fd = os.open(self.path / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out)


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name).resolve())
        self.key1, self.fp1 = key(1)
        self.key2, self.fp2 = key(2)
        self.scanned = self.key1
        self.runner = self.enterContext(mock.patch.object(t.subprocess, 'run', side_effect=self.scan))
        self.global_keys = self.enterContext(mock.patch.object(t, '_global_keys', return_value=(set(), set())))

    def scan(self, argv, **kwargs):
        self.assertEqual(argv[0], '/usr/bin/ssh-keyscan')
        self.assertEqual(argv[-1], IP)
        host = IP if argv[4] == '22' else f'[{IP}]:{argv[4]}'
        return subprocess.CompletedProcess(argv, 0, (host + ' ssh-ed25519 ' + self.scanned + '\n').encode(), b'')

    def resolve(self, **kwargs):
        return t.resolve(IP, 22, self.store, **kwargs)

    def establish(self):
        return self.resolve(expected=self.fp1, interactive=False)

    def test_explicit_verified_key_is_saved_private(self):
        path = self.establish()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_text(), IP + ' ssh-ed25519 ' + self.key1 + '\n')
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)
        self.assertTrue((self.store.path / (path.name + '.json')).exists())

    def test_unchanged_key_needs_no_flag_or_prompt(self):
        path = self.establish()
        with mock.patch('builtins.input', side_effect=AssertionError('must not prompt')):
            self.assertEqual(self.resolve(interactive=False), path)

    def test_changed_key_noninteractive_stops_and_preserves_old_pin(self):
        self.establish()
        before = (self.store.path / t.CURRENT).read_bytes()
        self.scanned = self.key2
        with self.assertRaises(t.TrustError):
            self.resolve(interactive=False)
        self.assertEqual((self.store.path / t.CURRENT).read_bytes(), before)

    def test_changed_key_explicit_verification_rotates_only_private_pointer(self):
        old = self.establish()
        self.scanned = self.key2
        new = self.resolve(expected=self.fp2, interactive=False)
        self.assertNotEqual(old, new)
        self.assertTrue(old.exists())
        self.assertTrue((self.store.path / (old.name + '.json')).exists())
        self.assertEqual(self.resolve(interactive=False), new)

    def test_wrong_explicit_fingerprint_never_rotates(self):
        self.establish()
        self.scanned = self.key2
        with self.assertRaises(t.TrustError):
            self.resolve(expected=self.fp1)
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_first_unknown_requires_tty_and_explicit_yes(self):
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value='yes'):
            self.resolve()
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_first_unknown_without_tty_refuses(self):
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=False), self.assertRaises(t.TrustError):
            self.resolve()
        self.assertIsNone(self.store.load(t.CURRENT))

    def test_first_unknown_no_does_not_save(self):
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value='no'):
            with self.assertRaises(t.TrustError):
                self.resolve()
        self.assertIsNone(self.store.load(t.CURRENT))

    def test_changed_key_yes_is_not_a_valid_verification(self):
        self.establish()
        self.scanned = self.key2
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value='yes'):
            with self.assertRaises(t.TrustError):
                self.resolve()
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_changed_key_interactive_external_fingerprint_accepted_once(self):
        self.establish()
        self.scanned = self.key2
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value=self.fp2):
            new = self.resolve()
        with mock.patch('builtins.input', side_effect=AssertionError('must not prompt again')):
            self.assertEqual(self.resolve(interactive=False), new)

    def test_existing_global_matching_key_reused_without_prompt(self):
        self.global_keys.return_value = ({self.fp1}, set())
        with mock.patch('builtins.input', side_effect=AssertionError('must not prompt')):
            self.resolve(interactive=False)
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_global_old_key_does_not_turn_change_into_first_tofu(self):
        self.global_keys.return_value = ({self.fp2}, set())
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value='yes'):
            with self.assertRaises(t.TrustError):
                self.resolve()
        self.assertIsNone(self.store.load(t.CURRENT))

    def test_revoked_key_refused_even_with_expected(self):
        self.global_keys.return_value = (set(), {self.fp1})
        with self.assertRaises(t.TrustError):
            self.resolve(expected=self.fp1)

    def test_keyscan_is_not_trust_and_must_be_unique(self):
        self.runner.side_effect = None
        data = f'{IP} ssh-ed25519 {self.key1}\n{IP} ssh-ed25519 {self.key2}\n'.encode()
        self.runner.return_value = subprocess.CompletedProcess([], 0, data, b'')
        with self.assertRaises(t.TrustError):
            self.resolve(expected=self.fp1)
        self.assertIsNone(self.store.load(t.CURRENT))

    def test_invalid_key_blob_refused(self):
        self.scanned = base64.b64encode(b'not an ED25519 SSH key').decode()
        with self.assertRaises(t.TrustError):
            self.resolve(expected=self.fp1)

    def test_scan_failure_does_not_erase_pin(self):
        self.establish()
        self.runner.side_effect = subprocess.TimeoutExpired('ssh-keyscan', 30)
        with self.assertRaises(t.TrustError):
            self.resolve()
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_private_pointer_symlink_refused(self):
        outside = self.store.path / 'outside'
        outside.write_text('{}')
        os.chmod(outside, 0o600)
        (self.store.path / t.CURRENT).symlink_to(outside)
        with self.assertRaises(t.TrustError):
            self.resolve(expected=self.fp1)

    def test_private_pin_modified_or_missing_refused(self):
        path = self.establish()
        path.unlink()
        with self.assertRaises(t.TrustError):
            self.resolve(expected=self.fp1)

    def test_nondefault_port_uses_bracket_known_host_name(self):
        path = t.resolve(IP, 22022, self.store, expected=self.fp1)
        self.assertTrue(path.read_text().startswith(f'[{IP}]:22022 ssh-ed25519 '))

    def test_global_hashed_lookup_and_revoked_parser_do_not_write(self):
        home = self.store.path / 'home'
        directory = home / '.ssh'
        directory.mkdir(parents=True, mode=0o700)
        known = directory / 'known_hosts'
        known.write_text('test marker unchanged\n')
        os.chmod(known, 0o600)
        before = known.read_bytes()
        output = f'# Host {IP} found: line 1\n|1|hashed|host ssh-ed25519 {self.key1}\n@revoked {IP} ssh-ed25519 {self.key2}\n'.encode()
        with mock.patch.object(t.Path, 'home', return_value=home), mock.patch.object(t.subprocess, 'run') as runner:
            runner.return_value = subprocess.CompletedProcess([], 0, output, b'')
            self.assertEqual(REAL_GLOBAL_KEYS(IP), ({self.fp1}, {self.fp2}))
            self.assertEqual(runner.call_args.args[0], ['/usr/bin/ssh-keygen', '-F', IP, '-f', str(known)])
        self.assertEqual(known.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
