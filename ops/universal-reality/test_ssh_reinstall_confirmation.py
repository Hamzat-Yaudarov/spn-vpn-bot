"""Scoped operator confirmation of a changed SSH key, without real SSH calls."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('ssh_reinstall_test_support', HERE / 'test_ssh_trust.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
t, IP = support.t, support.IP


class ReinstallConfirmationTests(unittest.TestCase):
    setUp = support.TrustTests.setUp
    scan = support.TrustTests.scan
    resolve = support.TrustTests.resolve
    establish = support.TrustTests.establish

    def changed(self):
        old = self.establish()
        self.scanned = self.key2
        return old

    def confirm(self, answer=None, **kwargs):
        if answer is None:
            answer = 'ПЕРЕУСТАНОВЛЕН ' + IP
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), \
                mock.patch('builtins.input', return_value=answer), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            path = self.resolve(**kwargs)
        return path, output.getvalue()

    def test_scoped_reinstall_confirmation_accepts_changed_key_once(self):
        old = self.changed()
        old_pin = old.read_bytes()
        old_receipt = (self.store.path / (old.name + '.json')).read_bytes()
        new, output = self.confirm()
        self.assertNotEqual(old, new)
        self.assertEqual(old.read_bytes(), old_pin)
        self.assertEqual((self.store.path / (old.name + '.json')).read_bytes(), old_receipt)
        record = self.store.load(t.CURRENT)
        self.assertEqual(record['fingerprint'], self.fp2)
        self.assertEqual(record['acceptance'], 'operator-confirmed-reinstall')
        self.assertEqual(record['ip'], IP)
        self.assertEqual(record['port'], 22)
        key_receipt = self.store.load(new.name + '.json')
        self.assertNotIn('acceptance', key_receipt)
        self.assertEqual(key_receipt, {key: value for key, value in record.items() if key != 'acceptance'})
        journals = list(self.store.path.glob('ssh-reinstall-*.json'))
        self.assertEqual(len(journals), 1)
        journal = self.store.load(journals[0].name)
        self.assertEqual(journal['acceptance'], 'operator-confirmed-reinstall')
        self.assertIn(self.fp1, json.dumps(journal))
        self.assertIn(self.fp2, json.dumps(journal))
        self.assertIn(self.fp2, output)
        with mock.patch('builtins.input', side_effect=AssertionError('must not prompt twice')):
            self.assertEqual(self.resolve(interactive=False), new)

    def test_unscoped_or_wrong_ip_confirmation_does_not_rotate(self):
        self.changed()
        before = (self.store.path / t.CURRENT).read_bytes()
        for answer in ('yes', 'ПЕРЕУСТАНОВЛЕН', 'ПЕРЕУСТАНОВЛЕН 8.8.8.8',
                       'ПЕРЕУСТАНОВЛЕН ' + IP + ' другой', 'переустановлен ' + IP):
            with self.subTest(answer=answer), self.assertRaises(t.TrustError):
                self.confirm(answer)
            self.assertEqual((self.store.path / t.CURRENT).read_bytes(), before)

    def test_confirmation_is_not_available_without_tty(self):
        self.changed()
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=False), \
                mock.patch('builtins.input', return_value='ПЕРЕУСТАНОВЛЕН ' + IP) as prompt, \
                self.assertRaises(t.TrustError):
            self.resolve()
        prompt.assert_not_called()
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_noninteractive_mode_cannot_accept_reinstall_confirmation(self):
        self.changed()
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), \
                mock.patch('builtins.input', return_value='ПЕРЕУСТАНОВЛЕН ' + IP) as prompt, \
                self.assertRaises(t.TrustError):
            self.resolve(interactive=False)
        prompt.assert_not_called()
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_wrong_expected_fingerprint_cannot_be_bypassed_by_confirmation(self):
        self.changed()
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), \
                mock.patch('builtins.input', return_value='ПЕРЕУСТАНОВЛЕН ' + IP) as prompt, \
                self.assertRaises(t.TrustError):
            self.resolve(expected=self.fp1)
        prompt.assert_not_called()
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_revoked_offered_key_cannot_be_bypassed_by_confirmation(self):
        self.changed()
        self.global_keys.return_value = (set(), {self.fp2})
        with mock.patch.object(t.sys.stdin, 'isatty', return_value=True), \
                mock.patch('builtins.input', return_value='ПЕРЕУСТАНОВЛЕН ' + IP) as prompt, \
                self.assertRaises(t.TrustError):
            self.resolve()
        prompt.assert_not_called()
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_verified_fingerprint_remains_distinguishable_from_operator_override(self):
        self.changed()
        new, output = self.confirm(self.fp2)
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp2)
        self.assertNotEqual(self.store.load(t.CURRENT).get('acceptance'), 'operator-confirmed-reinstall')
        self.assertEqual(list(self.store.path.glob('ssh-reinstall-*.json')), [])

    def test_operator_rotation_back_to_previously_seen_key_preserves_both_receipts(self):
        old = self.changed()
        old_receipt = (self.store.path / (old.name + '.json')).read_bytes()
        second, output = self.confirm()
        second_receipt = (self.store.path / (second.name + '.json')).read_bytes()
        self.scanned = self.key1
        returned, output = self.confirm()
        self.assertEqual(returned, old)
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)
        self.assertEqual(self.store.load(t.CURRENT)['acceptance'], 'operator-confirmed-reinstall')
        self.assertEqual((self.store.path / (old.name + '.json')).read_bytes(), old_receipt)
        self.assertEqual((self.store.path / (second.name + '.json')).read_bytes(), second_receipt)
        self.assertEqual(len(list(self.store.path.glob('ssh-reinstall-*.json'))), 2)
        with mock.patch('builtins.input', side_effect=AssertionError('must reuse returned key')):
            self.assertEqual(self.resolve(interactive=False), old)

    def test_verified_rotation_then_operator_rotation_back_keeps_baseline_receipts(self):
        old = self.changed()
        old_receipt = (self.store.path / (old.name + '.json')).read_bytes()
        second = self.resolve(expected=self.fp2, interactive=False)
        second_receipt = (self.store.path / (second.name + '.json')).read_bytes()
        self.assertNotIn('acceptance', self.store.load(second.name + '.json'))
        self.scanned = self.key1
        returned, output = self.confirm()
        self.assertEqual(returned, old)
        self.assertEqual(self.store.load(t.CURRENT)['acceptance'], 'operator-confirmed-reinstall')
        self.assertEqual((self.store.path / (old.name + '.json')).read_bytes(), old_receipt)
        self.assertEqual((self.store.path / (second.name + '.json')).read_bytes(), second_receipt)
        self.assertEqual(len(list(self.store.path.glob('ssh-reinstall-*.json'))), 1)

    def test_legacy_receipt_with_acceptance_metadata_survives_rotation_back(self):
        old = self.changed()
        receipt_path = self.store.path / (old.name + '.json')
        legacy = self.store.load(receipt_path.name)
        legacy['acceptance'] = 'operator-confirmed-reinstall'
        receipt_path.write_text(json.dumps(legacy))
        before = receipt_path.read_bytes()
        self.resolve(expected=self.fp2, interactive=False)
        self.scanned = self.key1
        returned, output = self.confirm()
        self.assertEqual(returned, old)
        self.assertEqual(receipt_path.read_bytes(), before)
        self.assertEqual(self.store.load(t.CURRENT)['fingerprint'], self.fp1)

    def test_existing_global_old_trust_is_not_silently_erased(self):
        self.global_keys.return_value = ({self.fp1}, set())
        self.scanned = self.key2
        new, output = self.confirm()
        record = self.store.load(t.CURRENT)
        self.assertEqual(record['fingerprint'], self.fp2)
        self.assertEqual(record['acceptance'], 'operator-confirmed-reinstall')
        self.assertEqual(self.global_keys.return_value, ({self.fp1}, set()))

    def test_first_unknown_host_still_requires_first_trust_yes(self):
        with self.assertRaises(t.TrustError):
            self.confirm()
        self.assertIsNone(self.store.load(t.CURRENT))


if __name__ == '__main__':
    unittest.main()
