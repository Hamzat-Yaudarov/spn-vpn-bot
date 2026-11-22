"""Full TLS replacement keeps all client identities and cuts DNS over last."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('tls_replace_fixture', HERE / 'test_replace.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
r, d = h.r, h.d


class TLSReplacementTests(unittest.TestCase):
    transport = 'tls'
    setUp = h.ReplacementTests.setUp
    tearDown = h.ReplacementTests.tearDown
    start = h.ReplacementTests.start
    snapshot = h.ReplacementTests.snapshot

    def apply(self, *args, certificate_error=None, **kwargs):
        def issue(op, runtime, token, zone):
            h.FakeSSH.calls.append(('certificate', runtime['ip']))
            if certificate_error:
                raise certificate_error
            self.assertEqual(runtime['domain'], h.DOMAIN)
            self.assertEqual(self.dns_ip, h.IP)
            self.assertEqual(token, 'fake-cloudflare-token')
            return {'format': 1, 'domain': h.DOMAIN, 'files': {'privkey.pem': 'fake-private-key'}}
        with patch.object(r.tls_certificate, 'require_tools'), patch.object(r.tls_certificate, 'issue', side_effect=issue):
            return h.ReplacementTests.apply(self, *args, **kwargs)

    def test_full_tls_preserves_domain_credentials_objects_and_original_journal(self):
        (self.store.path / 'reality.private.json').unlink()
        before = copy.deepcopy(self.api.data)
        files = {p.name: p.read_bytes() for p in self.store.path.iterdir() if p.is_file()}
        output = self.apply()
        commits, pending = r.chain(self.store, self.plan)
        self.assertIsNone(pending)
        self.assertEqual(len(commits), 1)
        self.assertTrue(commits[0]['trafficAndExportVerified'])
        self.assertEqual(self.api.data, before)
        self.assertEqual(self.dns_ip, h.NEW)
        self.assertIn('Замена завершена', output)
        self.assertEqual(self.probe_options[0]['security'], 'tls')
        self.assertEqual(self.probe_options[0]['sni'], h.DOMAIN)
        self.assertEqual(self.export_options[0]['sni'], h.DOMAIN)
        for name, data in files.items():
            self.assertEqual((self.store.path / name).read_bytes(), data)
        self.assertFalse(any(method == 'POST' and path in ('/nodes', '/hosts', '/config-profiles')
                             for method, path, body in self.api.calls))

    def test_certificate_before_target_and_packages_before_source_snapshot(self):
        self.apply()
        calls = h.FakeSSH.calls
        self.assertLess(calls.index(('certificate', h.NEW)), calls.index(('connect', h.NEW)))
        self.assertLess(calls.index(('action', h.NEW, 'preflight')), calls.index(('action', h.NEW, 'packages')))
        self.assertLess(calls.index(('action', h.NEW, 'packages')), calls.index(('source', h.NEW, 'check')))
        self.assertLess(calls.index(('source', h.NEW, 'check')), calls.index(('source', h.NEW, 'apply')))
        uploads = [c[2] for c in calls if c[0] == 'upload']
        self.assertIn('tls-site-policy.sh', uploads[0])
        self.assertTrue(any({'tls_certificate_import.py', 'tls-certificate.bundle.json'} <= names for names in uploads))
        self.assertEqual(self.guard_calls, [(h.NEW, None, False), (h.NEW, None, True)])
        self.assertEqual(self.sni_calls, [])

    def test_certificate_failure_keeps_old_dns_and_creates_no_guest_or_panel_changes(self):
        with self.assertRaises(d.u.DeployError):
            self.apply(certificate_error=d.u.DeployError('DNS-01 not verified'))
        self.assertEqual(self.dns_ip, h.IP)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(any(c[0] == 'connect' for c in h.FakeSSH.calls))
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(commits, [])
        self.assertEqual(pending[1]['toIp'], h.NEW)

    def test_failed_vpn_cannot_cut_dns_over(self):
        self.proof_passed = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(self.dns_ip, h.IP)
        self.assertFalse(any(row[0] == 'update' for row in self.dns_calls))
        self.assertIsNotNone(r.chain(self.store, self.plan)[1])

    def test_failed_export_after_cutover_resumes_same_pending_generation(self):
        self.export_passed = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(self.dns_ip, h.NEW)
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(commits, [])
        operation = pending[0].path
        self.export_passed = True
        # Existing issuance is immutable and must remain reusable after DNS cutover.
        with patch.object(r.tls_certificate, 'require_tools'), \
                patch.object(r.tls_certificate, 'issue', return_value={'domain': h.DOMAIN}):
            h.ReplacementTests.apply(self)
        self.assertEqual(len(r.chain(self.store, self.plan)[0]), 1)
        self.assertEqual(list((self.store.path / 'replacements').iterdir()), [operation])

    def test_changed_ssh_port_is_rejected_before_api_and_certificate_work(self):
        with patch.object(r.tls_certificate, 'require_tools', side_effect=AssertionError('must stop early')):
            with self.assertRaises(d.u.DeployError):
                h.ReplacementTests.apply(self, extra=['--ssh-port', '222'])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(h.FakeSSH.calls, [])

    def test_pending_retry_rejects_reinstalled_machine_with_same_ssh_key_before_mutations(self):
        self.proof_passed = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        operation = r.chain(self.store, self.plan)[1][0]
        before = {path.name: path.read_bytes() for path in operation.path.glob('machine-*.json')}
        h.FakeSSH.calls = []
        self.api.calls.clear()
        original = h.FakeSSH.action
        def reinstalled(ssh, action, capture=False):
            result = original(ssh, action, capture=capture)
            if action == 'identity':
                guest = json.loads(result)
                guest['machineId'] = 'b' * 32
                return json.dumps(guest).encode()
            return result
        self.proof_passed = True
        with patch.object(h.FakeSSH, 'action', new=reinstalled), self.assertRaisesRegex(d.u.DeployError, 'VPS изменилась'):
            self.apply()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.dns_ip, h.IP)
        self.assertFalse(any(row[0] == 'source' or (row[0] == 'action' and row[2] in ('packages', 'install'))
                             for row in h.FakeSSH.calls))
        self.assertEqual({path.name: path.read_bytes() for path in operation.path.glob('machine-*.json')}, before)
        self.assertIsNone(operation.load('commit.json'))


if __name__ == '__main__':
    unittest.main()
