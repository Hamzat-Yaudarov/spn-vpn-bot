"""Offline token storage checks; no real credentials or network."""
import contextlib
import getpass
import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings


spec = importlib.util.spec_from_file_location('cloudflare_setup_under_test', Path(__file__).with_name('setup_cloudflare.py'))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class TokenSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'private' / 'cloudflare-token'

    def test_private_file_and_directory(self):
        self.assertEqual(d.save_token(self.path, 'offline-example-token'), self.path)
        self.assertEqual(self.path.read_text(), 'offline-example-token\n')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_existing_file_is_not_overwritten(self):
        d.save_token(self.path, 'first-test-token')
        with self.assertRaises(d.SetupError):
            d.save_token(self.path, 'replacement-test-token')
        self.assertEqual(self.path.read_text(), 'first-test-token\n')

    def test_target_symlink_is_not_followed(self):
        target = self.root / 'untouched'
        target.write_text('unchanged')
        self.path.parent.mkdir(mode=0o700)
        self.path.symlink_to(target)
        with self.assertRaises(d.SetupError):
            d.save_token(self.path, 'offline-example-token')
        self.assertEqual(target.read_text(), 'unchanged')

    def test_parent_symlink_is_rejected(self):
        target = self.root / 'real'
        target.mkdir(mode=0o700)
        self.path.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(d.SetupError):
            d.save_token(self.path, 'offline-example-token')
        self.assertEqual(list(target.iterdir()), [])

    def test_shared_parent_is_rejected(self):
        self.path.parent.mkdir(mode=0o755)
        self.path.parent.chmod(0o755)
        with self.assertRaises(d.SetupError):
            d.save_token(self.path, 'offline-example-token')
        self.assertFalse(self.path.exists())

    def test_bad_value_never_creates_file(self):
        for token in ('', 'with spaces', 'new\nline', 'x' * 4096):
            with self.subTest(length=len(token)), self.assertRaises(d.SetupError):
                d.save_token(self.path, token)
        self.assertFalse(self.path.exists())

    def test_main_hides_token_and_reports_permissions(self):
        output = io.StringIO()
        with patch.object(d.getpass, 'getpass', return_value='offline-example-token'), contextlib.redirect_stdout(output):
            self.assertEqual(d.main(['--file', str(self.path)]), 0)
        self.assertNotIn('offline-example-token', output.getvalue())
        self.assertIn('0600', output.getvalue())

    def test_non_terminal_input_never_falls_back_to_echo(self):
        def warn(*_):
            warnings.warn('hidden input unavailable', getpass.GetPassWarning)
            return 'must-not-save'
        with patch.object(d.getpass, 'getpass', side_effect=warn), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.main(['--file', str(self.path)]), 2)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
