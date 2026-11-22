"""Offline DNS-01 ownership, uncertain writes, and controller secret boundary."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('tls_issuance_under_test', HERE / 'tls_replacement_certificate.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
DOMAIN = 'tls.example.com'
VALIDATION = 'a' * 43
TOKEN = 'private-controller-dns-token'
ZONE = {'id': 'zone123', 'name_servers': ['alice.ns.cloudflare.com']}


class FakeDNS:
    def __init__(self):
        self.rows, self.calls, self.lose = [], [], None

    def _list(self, path, query):
        return [dict(row) for row in self.rows]

    def _request(self, method, path, *, body=None):
        self.calls.append((method, path, body))
        if method == 'POST':
            if self.lose != 'absent':
                self.rows.append({**body, 'id': 'owned123'})
            if self.lose:
                raise c.dns.DNSOutcomeUnknown('Simulated unknown DNS outcome')
            return {'result': self.rows[-1]}
        if method == 'DELETE':
            self.rows = [row for row in self.rows if row['id'] != path.split('/')[-1]]
            return {'result': {'id': path.split('/')[-1]}}
        raise AssertionError('Unexpected DNS operation')


class TLSCertificateIssuanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.op = c.u.Store(Path(self.temp.name).resolve() / 'operation')
        self.op.save('intent.json', {'domain': DOMAIN, 'toIp': '9.9.9.9'})
        self.context = {'domain': DOMAIN, 'zone': ZONE, 'token': TOKEN,
                        'intentHash': c.u.digest(self.op.load('intent.json'))}
        self.op.save(c.CONTEXT, self.context)
        self.client = FakeDNS()

    def tearDown(self):
        self.temp.cleanup()

    def hook(self, action='auth', domain=DOMAIN):
        with patch.object(c.dns, 'CloudflareDNS', return_value=self.client), \
                patch.object(c, '_wait_txt'), \
                patch.dict(os.environ, CERTBOT_DOMAIN=domain, CERTBOT_VALIDATION=VALIDATION):
            c.hook(action, self.op.path)

    def test_create_resume_cleanup_preserves_foreign_txt(self):
        foreign = {'id': 'foreign123', 'name': '_acme-challenge.' + DOMAIN, 'type': 'TXT',
                   'content': VALIDATION, 'comment': 'another owner', 'ttl': 60}
        self.client.rows.append(foreign)
        self.hook()
        self.hook()
        self.assertEqual(len([x for x in self.client.calls if x[0] == 'POST']), 1)
        self.hook('cleanup')
        self.assertEqual(self.client.rows, [foreign])
        self.hook('cleanup')
        self.assertEqual(len([x for x in self.client.calls if x[0] == 'DELETE']), 1)

    def test_lost_post_response_reconciles_exact_marker_without_second_post(self):
        self.client.lose = 'created'
        self.hook()
        self.hook()
        self.assertEqual(len([x for x in self.client.calls if x[0] == 'POST']), 1)
        self.hook('cleanup')
        self.assertEqual(self.client.rows, [])

    def test_unknown_post_without_record_is_never_reposted(self):
        self.client.lose = 'absent'
        for attempt in range(2):
            with self.assertRaises(c.u.DeployError):
                self.hook()
        self.assertEqual(len([x for x in self.client.calls if x[0] == 'POST']), 1)

    def test_different_certbot_domain_rejected_before_cloudflare(self):
        with self.assertRaises(c.u.DeployError):
            self.hook(domain='other.example.com')
        self.assertEqual(self.client.calls, [])

    def test_cleanup_rejects_record_id_change(self):
        self.hook()
        self.client.rows[0]['id'] = 'changed123'
        with self.assertRaises(c.u.DeployError):
            self.hook('cleanup')
        self.assertFalse(any(x[0] == 'DELETE' for x in self.client.calls))

    def test_issue_keeps_token_local_and_removes_ephemeral_context(self):
        captured = []
        bundle = {'format': 1, 'domain': DOMAIN, 'files': {'privkey.pem': 'private TLS key'}}

        class Process:
            def __init__(self, command, **kwargs):
                captured.append((command, kwargs))
                self.pid = 99999
            def wait(self, **kwargs):
                return 0

        with patch.object(c, 'require_tools', return_value={'certbot': '/usr/bin/certbot'}), \
                patch.object(c.subprocess, 'Popen', Process), patch.object(c, '_collect', return_value=bundle), \
                patch.object(c, '_validate'), patch.object(c.dns, 'CloudflareDNS', return_value=self.client), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = c.issue(self.op, {'domain': DOMAIN}, TOKEN, ZONE)
        self.assertEqual(result, bundle)
        self.assertNotIn(TOKEN, repr(captured))
        self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(output.getvalue(), '')
        self.assertFalse((self.op.path / c.CONTEXT).exists())
        self.assertEqual(self.op.load(c.BUNDLE), bundle)
        self.assertEqual((self.op.path / c.BUNDLE).stat().st_mode & 0o777, 0o600)
        command = captured[0][0]
        self.assertIn('--non-interactive', command)
        self.assertIn('--manual-auth-hook', command)
        self.assertNotIn('--standalone', command)

    def test_failed_certbot_reconciles_own_txt_then_clears_secret_context(self):
        owner = self

        class Process:
            def __init__(self, command, **kwargs):
                self.pid = 99999
            def wait(self, **kwargs):
                owner.hook()
                return 1

        with patch.object(c, 'require_tools', return_value={'certbot': '/usr/bin/certbot'}), \
                patch.object(c.subprocess, 'Popen', Process), \
                patch.object(c.dns, 'CloudflareDNS', return_value=self.client), self.assertRaises(c.u.DeployError):
            c.issue(self.op, {'domain': DOMAIN}, TOKEN, ZONE)
        self.assertEqual(self.client.rows, [])
        self.assertFalse((self.op.path / c.CONTEXT).exists())
        self.assertIsNone(self.op.load(c.BUNDLE))

    def test_tools_missing_stops_before_issuance(self):
        with patch.object(c.shutil, 'which', return_value=None), self.assertRaises(c.u.DeployError):
            c.require_tools()
        self.assertEqual(self.client.calls, [])

    def test_interruption_stops_certbot_and_hooks_before_context_cleanup(self):
        actions = []
        class Process:
            pid = 99999
            def __init__(self, command, **kwargs):
                self.waits = 0
            def wait(self, **kwargs):
                self.waits += 1
                actions.append('wait')
                if self.waits == 1:
                    raise KeyboardInterrupt
                return 0
        def cleanup(*args):
            self.assertTrue((self.op.path / c.CONTEXT).is_file())
            actions.append('cleanup')
        with patch.object(c, 'require_tools', return_value={'certbot': '/usr/bin/certbot'}), \
                patch.object(c.subprocess, 'Popen', Process), \
                patch.object(c.os, 'killpg', side_effect=lambda *args: actions.append('terminate')), \
                patch.object(c, '_cleanup_owned', side_effect=cleanup), self.assertRaises(KeyboardInterrupt):
            c.issue(self.op, {'domain': DOMAIN}, TOKEN, ZONE)
        self.assertEqual(actions, ['wait', 'terminate', 'wait', 'cleanup'])
        self.assertFalse((self.op.path / c.CONTEXT).exists())


class CertificateCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / 'config'
        self.config.mkdir(mode=0o700)
        account = 'a' * 32
        self.account = self.config / 'accounts' / 'acme-v02.api.letsencrypt.org' / 'directory' / account
        self.account.mkdir(mode=0o700, parents=True)
        self.live = self.config / 'live' / DOMAIN
        self.live.mkdir(mode=0o700, parents=True)
        archive = self.config / 'archive' / DOMAIN
        archive.mkdir(mode=0o700, parents=True)
        for name in ('cert.pem', 'chain.pem', 'fullchain.pem', 'privkey.pem'):
            target = archive / (name.removesuffix('.pem') + '1.pem')
            target.write_text('fixture-' + name)
            target.chmod(0o600)
            (self.live / name).symlink_to('../../archive/' + DOMAIN + '/' + target.name)
        renewal = self.config / 'renewal'
        renewal.mkdir(mode=0o700)
        self.renewal = renewal / (DOMAIN + '.conf')
        self.renewal.write_text('[renewalparams]\naccount = ' + account + '\n')
        self.renewal.chmod(0o600)
        for name in ('meta.json', 'regr.json', 'private_key.json'):
            target = self.account / name
            target.write_text(json.dumps({'fixture': name}))
            target.chmod(0o400 if name == 'private_key.json' else 0o600)

    def test_collects_stock_certbot_read_only_private_account_key(self):
        bundle = c._collect(self.config, DOMAIN)
        self.assertEqual(bundle['account_id'], 'a' * 32)
        self.assertEqual(bundle['files']['private_key.json'], '{"fixture": "private_key.json"}')
        self.assertEqual(bundle['files']['privkey.pem'], 'fixture-privkey.pem')
        self.assertEqual((self.account / 'private_key.json').stat().st_mode & 0o777, 0o400)
        # Owner-writable private keys remain supported too.
        (self.account / 'private_key.json').chmod(0o600)
        self.assertEqual(c._collect(self.config, DOMAIN), bundle)

    def test_private_collector_still_rejects_group_world_and_executable_modes(self):
        key = self.account / 'private_key.json'
        for mode in (0o440, 0o640, 0o644, 0o700):
            with self.subTest(mode=oct(mode)):
                key.chmod(mode)
                with self.assertRaises(c.u.DeployError):
                    c._collect(self.config, DOMAIN)

    def test_private_account_hardlink_and_foreign_owner_still_rejected(self):
        key = self.account / 'private_key.json'
        extra = self.root / 'extra-key'
        extra.hardlink_to(key)
        with self.assertRaises(c.u.DeployError):
            c._collect(self.config, DOMAIN)
        extra.unlink()
        with patch.object(c.os, 'geteuid', return_value=os.geteuid() + 1), self.assertRaises(c.u.DeployError):
            c._collect(self.config, DOMAIN)

    def test_public_file_policy_is_not_widened_by_private_key_fix(self):
        self.renewal.chmod(0o400)
        with self.assertRaises(c.u.DeployError):
            c._collect(self.config, DOMAIN)


if __name__ == '__main__':
    unittest.main()
