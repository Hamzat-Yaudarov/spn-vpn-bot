"""End-to-end orchestration against fake panel/DNS/SSH; no external writes."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


d = load('universal_yandex_deploy_tests', HERE / 'deploy.py')
IP, PANEL, BASE = '8.8.4.4', '1.1.1.1', 'https://panel.example.com/api'
SQUAD = '11111111-1111-4111-8111-111111111111'


class API:
    def __init__(self):
        self.data = {'config-profiles': [], 'nodes': [], 'hosts': [],
                     'internal-squads': [{'uuid': SQUAD, 'name': 'bypass', 'inbounds': [{'uuid': 'old-inbound'}]}]}
        self.calls, self.number, self.lose = [], 0, None

    def rows(self, kind):
        return copy.deepcopy(self.data[kind])

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if (method, path) == ('GET', '/keygen'):
            return {'secretKey': 'PRIVATE-NODE-SERVICE-KEY-123456789'}
        kind = path[1:]
        if method == 'POST':
            self.number += 1
            row = {'uuid': '00000000-0000-4000-8000-' + format(self.number, '012d'), **copy.deepcopy(body)}
            if kind == 'config-profiles':
                row['inbounds'] = [{'uuid': '10000000-0000-4000-8000-' + format(self.number, '012d'), 'tag': x['tag']}
                                   for x in row['config']['inbounds']]
            if kind == 'nodes':
                row.update(isConnected=True, isDisabled=False, lastStatusMessage=None)
            self.data[kind].append(row)
            if self.lose == kind:
                self.lose = None
                raise d.u.DeployError('Injected lost POST reply')
            return copy.deepcopy(row)
        if method == 'PATCH':
            row = next(x for x in self.data[kind] if x['uuid'] == body['uuid'])
            row.update(copy.deepcopy(body))
            return copy.deepcopy(row)
        raise AssertionError('Forbidden API action')


class SSH:
    calls, payloads, fail = [], [], None

    def __init__(self, plan, known=None, control_socket=None):
        self.plan = plan

    def __enter__(self):
        self.calls.append('connect'); return self

    def __exit__(self, *args):
        self.calls.append('close')

    def upload(self, files):
        self.calls.append('upload'); self.payloads.append(copy.deepcopy(files))

    def action(self, action, capture=False):
        self.calls.append(action)
        if self.fail == action:
            raise d.u.DeployError('Injected owned VPS failure')
        if action == 'identity':
            return json.dumps({'machineId': 'a' * 32, 'sshFingerprint': 'SHA256:' + 'A' * 43,
                               'ownedInstallation': True}).encode()

    def source(self, action):
        self.calls.append('source-' + action)

    def origin_health(self):
        self.calls.append('origin-local-health')
        return {'passed': True, 'scope': 'origin-local-ssh', 'tlsVerified': True, 'cdnVerified': False,
                'domain': self.plan['domain'], 'ip': self.plan['ip'], 'deployment': self.plan['code']}


class DNS:
    def __init__(self, store):
        self.store = store

    def ensure(self, store, name, target, kind):
        intent = {'name': name, 'content': target}
        store.save('dns-' + kind + '.intent.json', intent)
        store.save('dns-' + kind + '.receipt.json', {**intent, 'id': kind + '-record'})


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.api = API()
        self.source = {'server': copy.deepcopy(d.c.SERVER), 'client': copy.deepcopy(d.c.CLIENT),
                       'fingerprint': 'firefox', 'extraEncoding': 'object', 'sourceHost': d.c.SOURCE_HOST}
        SSH.calls, SSH.payloads, SSH.fail = [], [], None
        self.health_calls, self.origin_dns = [], []
        self.proof_passed, self.export_passed, self.dns_conflict = True, True, False
        self.on_proof = None

    def args(self, *values):
        return d.parser().parse_args(['--ip', IP, '--name', '🇩🇪 Германия', '--country', 'DE',
                                     '--squad', 'bypass', '--state-dir', str(self.root / 'state'), *values])

    def state(self):
        return d.u.Store(self.root / 'state')

    def install(self, *values):
        def dns_context(store, plan, args, settings):
            if self.dns_conflict:
                raise d.records.dns.DNSConflict('Unowned DNS name')
            return ('cf-client', 'zone', DNS(store))

        def proof(plan, source, subscription, **kw):
            if self.on_proof:
                self.on_proof()
            return {'passed': self.proof_passed, 'tests': 9, 'domain': plan['cdnDomain'], 'expectedExitIp': plan['ip'],
                    'transport': 'xhttp-get-header', 'directReachabilityVerified': kw['route'] == 'physical'}

        def exported(*args, **kw):
            if not self.export_passed:
                raise d.probe.ProbeError('Injected export mismatch')
            return {'passed': True}

        with patch.object(d.u, 'read_settings', return_value={'REMNAWAVE_BASE_URL': BASE, 'REMNAWAVE_API_TOKEN': 'PANEL-TOKEN-NEVER-UPLOAD'}), \
                patch.object(d.r, 'API', return_value=self.api), patch.object(d.r, 'panel_ipv4', return_value=PANEL), \
                patch.object(d.c, 'extract_source', return_value=copy.deepcopy(self.source)), \
                patch.object(d, 'dns_context', side_effect=dns_context), \
                patch.object(d.r, 'ensure_dns', side_effect=lambda store, plan, context, wait: self.origin_dns.append(plan['domain'])), \
                patch.object(d.r.trust, 'resolve', return_value=self.root / 'known'), patch.object(d, 'SSH', SSH), \
                patch.object(d.probe, 'health', side_effect=lambda *a, **kw: self.health_calls.append(a) or {'passed': True}), \
                patch.object(d.probe, 'run', side_effect=proof), patch.object(d.probe, 'verify_export', side_effect=exported), \
                patch.object(d.r.probe, 'private_file', return_value=self.root / 'subscription'), \
                patch.object(d.r.probe, 'subscription_url', return_value='https://sub.example.com/private'), \
                patch.object(d.r.probe, 'select_engine', return_value=self.root / 'engine'), \
                patch.object(d.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            d.run(self.args('--apply', *values))
        return output.getvalue()

    def dns_steps(self):
        self.install('--certificate-target', 'fpqabc123.cm.yandexcloud.net')
        self.install('--cdn-target', 'e1a2b3.topology.gslb.yccdn.ru')

    def test_offline_default_reads_no_credentials_state_or_network(self):
        with patch.object(d.u, 'read_settings', side_effect=AssertionError('credentials read')), \
                patch.object(d.u, 'Store', side_effect=AssertionError('state changed')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            d.run(self.args())
        self.assertFalse(json.loads(output.getvalue())['changes'])
        self.assertFalse(json.loads(output.getvalue())['buysCDN'])

    def test_initial_apply_requires_explicit_origin_certificate_terms(self):
        with self.assertRaisesRegex(d.u.DeployError, 'agree-le-tos'):
            self.install()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(SSH.calls, [])
        self.assertEqual(self.origin_dns, [])

    def test_fresh_origin_creates_only_one_disabled_cdn_host_and_personalized_guide(self):
        output = self.install('--agree-le-tos', '--keep-tcp-port', '10050')
        state = self.state(); plan = state.load('plan.json')
        self.assertLess(SSH.calls.index('packages'), SSH.calls.index('source-apply'))
        self.assertEqual(len(list(state.path.glob('machine-*.json'))), 1)
        self.assertEqual(plan['ip'], IP)
        for kind in ('config-profiles', 'nodes', 'hosts'):
            self.assertEqual(len(self.api.data[kind]), 1)
        host = self.api.data['hosts'][0]
        self.assertTrue(host['isDisabled'])
        self.assertEqual(host['address'], plan['cdnDomain'])
        self.assertNotEqual(plan['domain'], plan['cdnDomain'])
        self.assertEqual(self.api.data['nodes'][0]['address'], IP)
        self.assertIn('old-inbound', d.u.ids(self.api.data['internal-squads'][0]['inbounds']))
        self.assertIsNotNone(state.load('origin.ready.json'))
        self.assertIsNone(state.load('activation.verified.json'))
        self.assertTrue(any(self.root.joinpath('state').glob('CDN-SETUP-*.md')))
        for value in (plan['domain'], plan['cdnDomain'], '--certificate-target', '--cdn-target', '--finish'):
            self.assertIn(value, output)
        uploaded = b'\n'.join(data for files in SSH.payloads for data in files.values())
        self.assertNotIn(b'PANEL-TOKEN-NEVER-UPLOAD', uploaded)
        self.assertNotIn(b'CLOUDFLARE', uploaded)

    def test_bad_vps_or_dns_never_creates_panel_objects_or_pins_wrong_plan(self):
        for condition in ('dns', 'preflight'):
            self.dns_conflict = condition == 'dns'; SSH.fail = 'preflight' if condition == 'preflight' else None
            with self.subTest(condition=condition), self.assertRaises(d.u.DeployError if condition == 'preflight' else d.records.dns.DNSConflict):
                self.install('--agree-le-tos')
            self.assertEqual(self.api.calls, [])
            self.assertIsNone(self.state().load('plan.json'))
            self.assertEqual(self.origin_dns, [])

    def test_repeated_apply_shows_guide_without_new_ssh_or_duplicate_objects(self):
        self.install('--agree-le-tos')
        before = copy.deepcopy(self.api.data); SSH.calls.clear()
        self.install()
        self.assertEqual(self.api.data, before)
        self.assertEqual(SSH.calls, [])

    def test_origin_preparation_does_not_require_direct_mac_access_to_origin(self):
        with patch.object(d.probe, 'health', side_effect=d.probe.ProbeError('Mac cannot reach origin')):
            self.install('--agree-le-tos')
        self.assertIn('origin-local-health', SSH.calls)
        self.assertIsNotNone(self.state().load('origin.ready.json'))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])

    def test_cert_and_cdn_dns_steps_need_no_vps_password_and_keep_host_disabled(self):
        self.install('--agree-le-tos'); SSH.calls.clear()
        self.dns_steps()
        self.assertEqual(SSH.calls, [])
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        state = self.state(); plan = state.load('plan.json')
        self.assertEqual(state.load('dns-certificate.receipt.json')['name'], '_acme-challenge.' + plan['cdnDomain'])
        self.assertEqual(state.load('dns-cdn.receipt.json')['name'], plan['cdnDomain'])

    def test_finish_requires_both_manual_cname_results(self):
        self.install('--agree-le-tos'); SSH.calls.clear()
        with self.assertRaisesRegex(d.u.DeployError, 'обе CNAME'):
            self.install('--finish')
        self.assertEqual(SSH.calls, [])

    def test_finish_uses_saved_targets_and_enables_only_after_real_vpn_and_export(self):
        self.install('--agree-le-tos'); self.dns_steps()
        self.on_proof = lambda: self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.install('--finish')
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])
        self.assertIsNotNone(self.state().load('activation.verified.json'))
        self.assertIsNone(self.state().load('activation.pending.json'))
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_failed_vpn_never_publishes_and_can_retry_without_recreating_objects(self):
        self.install('--agree-le-tos'); self.dns_steps()
        before = copy.deepcopy(self.api.data)
        self.proof_passed = False
        with self.assertRaisesRegex(d.u.DeployError, '9/9'):
            self.install('--finish')
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.proof_passed = True
        self.install('--finish')
        self.assertEqual([row['uuid'] for row in self.api.data['hosts']], [row['uuid'] for row in before['hosts']])

    def test_failed_export_retracts_new_host_and_resume_keeps_original_disabled_state(self):
        self.install('--agree-le-tos'); self.dns_steps()
        self.export_passed = False
        with self.assertRaises(d.probe.ProbeError):
            self.install('--finish')
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertTrue(self.state().load('activation.pending.json')['beforeDisabled'])
        self.export_passed = True
        self.install('--finish')
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])

    def test_unknown_enable_reply_is_reconciled_before_retry_traffic_test(self):
        self.install('--agree-le-tos'); self.dns_steps()
        host = self.api.data['hosts'][0]; plan = self.state().load('plan.json')
        self.state().save('activation.pending.json', {'host': host['uuid'], 'code': plan['code'], 'beforeDisabled': True})
        host['isDisabled'] = False  # enable happened before a lost reply / crash
        self.on_proof = lambda: self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.export_passed = False
        with self.assertRaises(d.probe.ProbeError):
            self.install('--finish')
        self.assertTrue(host['isDisabled'])

    def test_concurrent_technical_host_change_is_not_enabled_or_overwritten(self):
        self.install('--agree-le-tos'); self.dns_steps()
        self.on_proof = lambda: self.api.data['hosts'][0].update(sni='changed.example.com')
        with self.assertRaisesRegex(d.u.DeployError, 'изменены во время'):
            self.install('--finish')
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(self.api.data['hosts'][0]['sni'], 'changed.example.com')

    def test_recheck_origin_after_ready_reuses_every_panel_uuid(self):
        self.install('--agree-le-tos')
        before = copy.deepcopy(self.api.data); SSH.calls.clear()
        self.install('--recheck-origin')
        self.assertIn('install', SSH.calls)
        self.assertEqual(self.api.data, before)

    def test_existing_foreign_node_is_not_adopted_or_deleted(self):
        self.api.data['nodes'].append({'uuid': 'foreign', 'address': IP})
        with self.assertRaisesRegex(d.u.DeployError, 'уже есть нода'):
            self.install('--agree-le-tos')
        self.assertEqual(self.api.calls, [])
        self.assertEqual(SSH.calls, [])


if __name__ == '__main__':
    unittest.main()
