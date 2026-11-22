"""Offline tests for a private, resumable replacement certificate import."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('tls_import_under_test', HERE / 'tls_certificate_import.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
DOMAIN, DEPLOYMENT = 'fi.example.com', 'a' * 32


class CertificateImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name).resolve()
        self.node, self.root = self.path / 'node', self.path / 'letsencrypt'
        self.node.mkdir(mode=0o700)
        self.write(self.node / '.spn-deployment', DEPLOYMENT + '\n')
        self.bundle = self.path / 'bundle.json'
        self.value = {'format': 1, 'domain': DOMAIN, 'account_id': 'b' * 32, 'server': m.SERVER,
                      'files': {'cert.pem': 'certificate\n', 'chain.pem': 'chain\n',
                                'fullchain.pem': 'certificate\nchain\n', 'privkey.pem': 'secret-key\n',
                                'meta.json': '{}',
                                'regr.json': json.dumps({'body': {}, 'uri':
                                    'https://acme-v02.api.letsencrypt.org/acme/acct/123'}),
                                'private_key.json': json.dumps({'kty': 'EC', 'd': 'private_acme_key'})}}
        self.write(self.bundle, json.dumps(self.value))
        self.validations = []

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, value):
        path.write_text(value)
        path.chmod(0o600)

    def apply(self, **kwargs):
        validator = kwargs.pop('validator', lambda files, domain: self.validations.append(domain))
        return m.import_certificate(self.bundle, DEPLOYMENT, DOMAIN, root=self.root, node=self.node,
                                    uid=os.geteuid(), validator=validator, **kwargs)

    def test_creates_standard_renewable_lineage_without_manual_hooks_or_dns_credentials(self):
        result = self.apply()
        self.assertTrue(result['certificateImported'])
        renewal = (self.root / 'renewal' / (DOMAIN + '.conf')).read_text()
        self.assertIn('authenticator = standalone', renewal)
        self.assertIn('pref_challs = http-01,', renewal)
        self.assertNotIn('manual', renewal)
        self.assertNotIn('cloudflare', renewal.lower())
        self.assertEqual((self.root / 'live' / DOMAIN / 'privkey.pem').read_text(), 'secret-key\n')
        self.assertEqual((self.root / 'live' / DOMAIN / 'cert.pem').readlink(),
                         Path('../../archive/' + DOMAIN + '/cert1.pem'))
        account = self.root / 'accounts' / m.ACCOUNT_SERVER / ('b' * 32)
        self.assertEqual(json.loads((account / 'private_key.json').read_text())['d'], 'private_acme_key')
        for file in self.root.rglob('*'):
            if file.is_file() and not file.is_symlink():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.apply(), result)
        self.assertTrue((self.node / 'tls-import.complete.json').is_file())

    def test_validation_failure_happens_before_guest_mutation(self):
        def fail(files, domain):
            raise m.ImportError('TLS certificate validation failed')
        with self.assertRaises(m.ImportError):
            m.import_certificate(self.bundle, DEPLOYMENT, DOMAIN, root=self.root, node=self.node,
                                 uid=os.geteuid(), validator=fail)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.node / 'tls-domain').exists())
        self.assertFalse((self.node / 'tls-import.intent.json').exists())

    def test_yandex_mode_keeps_nginx_port80_and_uses_its_owned_webroot(self):
        result = self.apply(mode='yandex-cdn')
        self.assertEqual(result['renewalAuthenticator'], 'webroot')
        renewal = self.root / 'renewal' / (DOMAIN + '.conf')
        self.assertIn('authenticator = webroot', renewal.read_text())
        self.assertIn('webroot_path = /var/www/spn-yandex-acme,', renewal.read_text())
        self.assertEqual(self.apply(mode='yandex-cdn'), result)
        with self.assertRaisesRegex(m.ImportError, 'bundle changed'):
            self.apply()
        renewal.write_text(renewal.read_text().replace('/var/www/spn-yandex-acme,', '/tmp/foreign,'))
        with self.assertRaisesRegex(m.ImportError, 'configuration changed'):
            self.apply(mode='yandex-cdn')

    def test_foreign_existing_lineage_or_domain_claim_is_never_adopted(self):
        self.root.mkdir(mode=0o700)
        (self.root / 'live').mkdir(mode=0o700)
        (self.root / 'live' / DOMAIN).mkdir(mode=0o700)
        with self.assertRaisesRegex(m.ImportError, 'unrelated'):
            self.apply()
        self.assertFalse((self.node / 'tls-domain').exists())
        (self.root / 'live' / DOMAIN).rmdir()
        self.write(self.node / 'tls-domain', DOMAIN + '\n')
        with self.assertRaisesRegex(m.ImportError, 'unrelated'):
            self.apply()

    def test_bundle_symlink_hardlink_and_world_readable_file_are_rejected(self):
        self.bundle.chmod(0o644)
        with self.assertRaises(m.ImportError):
            self.apply()
        self.bundle.chmod(0o600)
        extra = self.path / 'extra'
        extra.hardlink_to(self.bundle)
        with self.assertRaises(m.ImportError):
            self.apply()
        extra.unlink()
        self.bundle.rename(extra)
        self.bundle.symlink_to(extra)
        with self.assertRaises(m.ImportError):
            self.apply()
        self.assertFalse(self.root.exists())

    def test_changed_bundle_and_guest_key_are_rejected_without_overwrite(self):
        self.apply()
        key = self.root / 'archive' / DOMAIN / 'privkey1.pem'
        self.write(key, 'administrator-key\n')
        def validate(files, domain):
            if files['privkey.pem'] != self.value['files']['privkey.pem']:
                raise m.ImportError('TLS private key differs from its certificate')
        with self.assertRaisesRegex(m.ImportError, 'differs'):
            self.apply(validator=validate)
        self.assertEqual(key.read_text(), 'administrator-key\n')
        self.value['files']['privkey.pem'] = 'replacement-key\n'
        self.write(self.bundle, json.dumps(self.value))
        with self.assertRaisesRegex(m.ImportError, 'bundle changed'):
            self.apply()

    def test_interrupted_account_write_resumes_exact_owned_import(self):
        real_write = m._write
        failed = []
        def fail_once(path, data, uid):
            if path.name == 'private_key.json' and not failed:
                failed.append(True)
                raise OSError('simulated interruption')
            return real_write(path, data, uid)
        with patch.object(m, '_write', side_effect=fail_once), self.assertRaises(OSError):
            self.apply()
        self.assertTrue((self.node / 'tls-import.intent.json').exists())
        self.assertFalse((self.node / 'tls-import.complete.json').exists())
        self.assertTrue(self.apply()['certificateImported'])

    def test_changed_link_or_symlinked_directory_is_rejected(self):
        self.apply()
        key = self.root / 'live' / DOMAIN / 'privkey.pem'
        key.unlink()
        key.symlink_to('/etc/passwd')
        with self.assertRaisesRegex(m.ImportError, 'link changed'):
            self.apply()
        self.assertEqual(os.readlink(key), '/etc/passwd')

    def test_completed_import_audits_current_renewed_certificate_without_restoring_old_bundle(self):
        self.apply()
        old_bundle = self.bundle.read_bytes()
        current = {**self.value['files'], 'cert.pem': 'renewed-certificate\n',
                   'chain.pem': 'renewed-chain\n', 'fullchain.pem': 'renewed-certificate\nrenewed-chain\n',
                   'privkey.pem': 'renewed-private-key\n'}
        for name in m.FILES[:4]:
            renewed = self.root / 'archive' / DOMAIN / (name.removesuffix('.pem') + '2.pem')
            self.write(renewed, current[name])
            if name != 'privkey.pem':
                renewed.chmod(0o644)
            link = self.root / 'live' / DOMAIN / name
            link.unlink()
            link.symlink_to('../../archive/' + DOMAIN + '/' + renewed.name)
        renewal = self.root / 'renewal' / (DOMAIN + '.conf')
        renewal.write_text(renewal.read_text() + 'reuse_key = False\n')
        renewal.chmod(0o644)
        account = self.root / 'accounts' / m.ACCOUNT_SERVER / ('b' * 32)
        self.write(account / 'meta.json', '{"creation_host":"renewed"}')
        current['meta.json'] = (account / 'meta.json').read_text()
        def validate(files, domain):
            self.assertEqual(files, current)
            self.assertEqual(domain, DOMAIN)
        self.assertTrue(self.apply(validator=validate)['certificateImported'])
        self.assertEqual(self.bundle.read_bytes(), old_bundle)
        self.assertTrue(os.readlink(self.root / 'live' / DOMAIN / 'privkey.pem').endswith('privkey2.pem'))
        renewal.write_text(renewal.read_text().replace('standalone', 'manual'))
        with self.assertRaisesRegex(m.ImportError, 'configuration changed'):
            self.apply(validator=validate)

    def test_unknown_fields_wrong_account_and_untrusted_server_rejected(self):
        for field, bad in (('server', 'https://evil.invalid/directory'), ('account_id', '../escape'),
                           ('domain', 'another.example.com'), ('unexpected', 'secret')):
            bad_value = {**self.value, field: bad}
            self.write(self.bundle, json.dumps(bad_value))
            with self.assertRaises(m.ImportError):
                self.apply()
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
