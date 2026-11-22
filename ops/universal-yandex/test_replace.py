"""Offline full CDN replacement with real journals and fake panel/guest/DNS."""
import contextlib
import copy
import importlib.util
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


x = load('yandex_replacement_under_test', HERE / 'replace.py')
support = load('yandex_replacement_existing_fixtures', HERE / 'test_deploy.py')
d, r, u, c = x.d, x.r, x.u, x.c
OLD, NEW, NEXT = '8.8.4.4', '9.9.9.9', '8.8.8.8'
BASE, PANEL, SQUAD = support.BASE, support.PANEL, support.SQUAD
DOMAIN, CDN = 'origin.example.com', 'cdn.example.com'


class API(support.API):
    def __call__(self, method, path, body=None):
        result = super().__call__(method, path, body)
        if self.lose == 'PATCH' and method == 'PATCH':
            self.lose = None
            raise u.DeployError('Injected lost node PATCH response')
        return result

    def _action(self, uid, action):
        node = next(row for row in self.data['nodes'] if row['uuid'] == uid)
        self.calls.append(('POST', '/nodes/' + uid + '/actions/' + action, None))
        node['isDisabled'] = action == 'disable'
        if self.lose == action:
            self.lose = None
            raise u.DeployError('Injected lost node ' + action + ' response')
        return copy.deepcopy(node)

    def disable_node(self, uid):
        return self._action(uid, 'disable')

    def enable_node(self, uid):
        return self._action(uid, 'enable')


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.store = u.Store(self.home / 'installation')
        self.api = API()
        self.source = {'server': copy.deepcopy(c.SERVER), 'client': copy.deepcopy(c.CLIENT),
                       'fingerprint': 'firefox', 'extraEncoding': 'object', 'sourceHost': c.SOURCE_HOST}
        self.plan = {'format': 1, 'mode': 'yandex-cdn', 'id': 'a' * 32, 'code': 'a1b2c3',
                     'ip': OLD, 'sshPort': 22, 'panel': BASE, 'panelIp': PANEL,
                     'name': '🇫🇮 Финляндия обход', 'country': 'FI', 'domain': DOMAIN, 'cdnDomain': CDN,
                     'dns': {'provider': 'cloudflare', 'zone': 'example.com', 'ttl': 60},
                     'path': '/assets/edge-0123456789abcdef/', 'squads': [SQUAD]}
        c.validate_plan(self.plan)
        self.store.save('plan.json', self.plan)
        self.store.save('source.json', self.source)
        deployment = d.Deployment(self.api, self.store, self.plan, self.source)
        profile, node, inbound = deployment.provision()
        deployment.attach(inbound)
        host = deployment.host(profile, node, inbound)
        self.api.data['hosts'][0]['isDisabled'] = False
        self.store.save('origin.ready.json', {'id': self.plan['id'], 'node': node['uuid'], 'profile': profile['uuid'],
                                              'inbound': inbound, 'host': host['uuid']})
        self.store.save('activation.verified.json', {'host': host['uuid'], 'code': self.plan['code']})
        self.store.save('node-secret.json', {'value': 'preserved-private-node-service-key-123456789'})
        dns_intent = {'version': 1, 'zone_id': 'zone123', 'name': DOMAIN, 'type': 'A', 'content': OLD,
                      'proxied': False, 'ttl': 60, 'comment': 'SPN universal-reality:' + 'b' * 32}
        self.store.save('dns.intent.json', dns_intent)
        self.store.save('dns.receipt.json', {**{k: v for k, v in dns_intent.items() if k != 'version'}, 'id': 'a123'})
        for kind, name, target in (('cdn', CDN, 'e1a2b3.topology.gslb.yccdn.ru'),
                                   ('certificate', '_acme-challenge.' + CDN, 'fpqabc123.cm.yandexcloud.net')):
            value = {'version': 1, 'zone_id': 'zone123', 'name': name, 'content': target, 'type': 'CNAME',
                     'proxied': False, 'ttl': 60, 'comment': 'SPN universal-yandex:' + 'c' * 32}
            self.store.save('dns-' + kind + '.intent.json', value)
            self.store.save('dns-' + kind + '.receipt.json',
                            {**{k: v for k, v in value.items() if k != 'version'}, 'id': kind + '123'})
        machine = self.machine(OLD)
        self.store.save('machine-' + u.digest(machine) + '.json', machine)
        self.original = {path.name: path.read_bytes() for path in self.store.path.iterdir() if path.is_file()}
        self.before = copy.deepcopy(self.api.data)
        self.api.calls.clear()
        self.events, self.uploads, self.dns_calls, self.cname_checks = [], [], [], []
        self.dns_ip = OLD
        self.origin_ok, self.cdn_ok, self.export_ok = True, True, True
        self.origin_override = {}
        self.fail_action, self.on_origin, self.on_cdn = None, None, None
        self.lose_dns = False
        self.cname_conflict = None
        self.machine_overrides = {}

    @staticmethod
    def machine(ip):
        letter = {OLD: 'A', NEW: 'B', NEXT: 'C'}[ip]
        number = {OLD: '1', NEW: '2', NEXT: '3'}[ip]
        return {'machineId': number * 32, 'sshFingerprint': 'SHA256:' + letter * 43, 'ownedInstallation': True}

    def operation(self, number=1):
        return u.Store(self.store.path / 'replacements' / f'{number:06d}')

    def proof(self, runtime, *, origin=False):
        passed = self.origin_ok if origin else self.cdn_ok
        proof = {'passed': passed, 'tests': 9, 'transport': 'xhttp-get-header',
                 'domain': runtime['domain'] if origin else runtime['cdnDomain'],
                 'expectedExitIp': runtime['ip'], 'testRoute': 'system', 'directReachabilityVerified': False,
                 'results': [{'passed': passed, 'clientAlive': True} for _ in range(9)]}
        if origin:
            proof.update(scope='origin-direct', cdnVerified=False, originIp=runtime['ip'])
            proof.update(self.origin_override)
        return proof

    def args(self, ip=NEW, *extra):
        return x.parser().parse_args(['--domain', DOMAIN, '--ip', ip, '--state-dir', str(self.store.path),
                                      '--panel-url', BASE, '--test-route', 'system', '--wait', '60',
                                      '--host-key-sha256', self.machine(ip)['sshFingerprint'], '--apply', *extra])

    def apply(self, ip=NEW, *extra):
        owner = self
        class SSH:
            def __init__(self, runtime, known=None, control_socket=None):
                self.plan = runtime
                self.stage = '/root/test-stage'
            def __enter__(self):
                owner.events.append('connect'); return self
            def __exit__(self, *args):
                owner.events.append('close')
            def upload(self, files):
                owner.events.append('upload'); owner.uploads.append(copy.deepcopy(files))
            def source(self, action):
                owner.events.append('source-' + action)
            def run(self, command, data=None, capture=False, **kwargs):
                if 'preferred_source.py' in command and 'verify' in command:
                    owner.events.append('source-verify-captured')
                    return json.dumps({'ip': self.plan['ip'], 'preferredSourceVerified': True,
                                       'bootPersistenceVerified': True}).encode()
                raise AssertionError('Unexpected fake SSH command')
            def origin_health(self):
                owner.events.append('local-health')
                return {'passed': True, 'scope': 'origin-local-ssh', 'tlsVerified': True, 'cdnVerified': False,
                        'domain': DOMAIN, 'ip': self.plan['ip'], 'deployment': owner.plan['code']}
        class DNS:
            def __init__(self, token):
                pass
            def find_zone(self, name):
                return {'id': 'zone123', 'name': 'example.com', 'name_servers': ['alice.ns.cloudflare.com']}
            def check_update(self, op, *, receipt, old_ipv4, new_ipv4):
                owner.dns_calls.append(('check', old_ipv4, new_ipv4))
                u.need(receipt == owner.store.load('dns.receipt.json') and owner.dns_ip in (old_ipv4, new_ipv4),
                       'Owned DNS content changed')
            def update_record(self, op, *, receipt, old_ipv4, new_ipv4):
                owner.events.append('dns-update')
                owner.dns_calls.append(('update', old_ipv4, new_ipv4))
                desired = {'fromIp': old_ipv4, 'toIp': new_ipv4, 'record': receipt['id']}
                op.save('test-dns-update.intent.json', desired)
                owner.dns_ip = new_ipv4
                if owner.lose_dns:
                    owner.lose_dns = False
                    raise r.dns.DNSOutcomeUnknown('Injected lost DNS update response')
                op.save('test-dns-update.receipt.json', desired)
        def action(ssh, name, capture=False):
            owner.events.append(name)
            if name == owner.fail_action:
                raise u.DeployError('Injected guest failure')
            if name == 'identity':
                return json.dumps({**owner.machine(ssh.plan['ip']),
                                   **owner.machine_overrides.get(ssh.plan['ip'], {})}).encode()
        def cname_check(manager, store, name, content, kind):
            owner.cname_checks.append((kind, name, content))
            if owner.cname_conflict == kind:
                raise d.records.dns.DNSConflict('Owned CNAME changed')
            expected = owner.store.load('dns-' + kind + '.receipt.json')
            u.need(expected['name'] == name and expected['content'] == content, 'Owned CNAME changed')
            return expected
        def origin(runtime, source, subscription, **kwargs):
            owner.events.append('origin-proof')
            if owner.on_origin:
                owner.on_origin()
            return owner.proof(runtime, origin=True)
        def cdn(runtime, source, subscription, **kwargs):
            owner.events.append('cdn-proof')
            if owner.on_cdn:
                owner.on_cdn()
            return owner.proof(runtime)
        def exported(runtime, *args, **kwargs):
            owner.events.append('export-proof')
            return {'passed': owner.export_ok, 'domain': CDN}
        contexts = [
            patch.object(u, 'read_settings', return_value={'REMNAWAVE_BASE_URL': BASE,
                                   'REMNAWAVE_API_TOKEN': 'PRIVATE-PANEL-TOKEN'}), 
                patch.object(r, 'API', return_value=self.api), patch.object(r, 'panel_ipv4', return_value=PANEL), 
                patch.object(r, 'cloudflare_token', return_value='PRIVATE-CF-TOKEN'), 
                patch.object(r.dns, 'CloudflareDNS', DNS), patch.object(d.records.Records, 'check', new=cname_check), 
                patch.object(x.cert, 'require_tools'), 
                patch.object(x.cert, 'issue', return_value={'format': 1, 'domain': DOMAIN,
                                                          'files': {'privkey.pem': 'PRIVATE-CERTIFICATE-KEY'}}), 
                patch.object(r.trust, 'resolve', return_value=self.home / 'known-hosts'), 
                patch.object(d, 'SSH', SSH), patch.object(x, '_action', side_effect=action), 
                patch.object(r.probe, 'private_file', return_value=self.home / 'subscription'), 
                patch.object(r.probe, 'subscription_url', return_value='https://sub.example.com/private'), 
                patch.object(r.probe, 'select_engine', return_value=self.home / 'xray'), 
                patch.object(r.probe, 'validate_interface'), 
                patch.object(d.probe, 'run_origin', side_effect=origin), patch.object(d.probe, 'run', side_effect=cdn), 
                patch.object(d.probe, 'verify_export', side_effect=exported), patch.object(x, '_wait_dns'), 
                patch.object(x.time, 'monotonic', side_effect=itertools.count(step=1000)), 
                patch.object(x.time, 'sleep')
        ]
        with contextlib.ExitStack() as stack:
            for context in contexts:
                stack.enter_context(context)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                x.run(self.args(ip, *extra))
        return output.getvalue()

    def assert_original_unchanged(self):
        for name, data in self.original.items():
            self.assertEqual((self.store.path / name).read_bytes(), data, name)

    def test_full_replacement_preserves_panel_and_client_identities_and_original_journal(self):
        self.apply()
        expected = copy.deepcopy(self.before)
        expected['nodes'][0]['address'] = NEW
        self.assertEqual(self.api.data, expected)
        self.assert_original_unchanged()
        self.assertTrue(self.operation().load('commit.json')['trafficAndExportVerified'])
        self.assertTrue(self.operation().load('commit.json')['originTrafficVerified'])
        self.assertFalse(any(method == 'POST' and path in ('/nodes', '/hosts', '/config-profiles')
                             for method, path, _ in self.api.calls))
        self.assertTrue(all(path.startswith('/nodes') for _, path, _ in self.api.calls))
        self.assertEqual(self.events.count('origin-proof'), 1)
        self.assertEqual(self.events.count('cdn-proof'), 1)
        self.assertLess(self.events.index('packages'), self.events.index('source-check'))
        self.assertLess(self.events.index('source-check'), self.events.index('source-apply'))
        self.assertLess(self.events.index('origin-proof'), self.events.index('dns-update'))
        self.assertLess(self.events.index('dns-update'), self.events.index('cdn-proof'))
        self.assertLess(self.events.index('cdn-proof'), self.events.index('export-proof'))

    def test_only_origin_a_is_updated_cnames_are_read_only_and_tokens_never_uploaded(self):
        self.apply()
        self.assertEqual(self.dns_calls, [('check', OLD, NEW), ('update', OLD, NEW), ('check', OLD, NEW)])
        self.assertEqual([row[:2] for row in self.cname_checks],
                         [('cdn', CDN), ('certificate', '_acme-challenge.' + CDN)] * 2)
        for kind in ('cdn', 'certificate'):
            self.assertEqual(self.store.load('dns-' + kind + '.receipt.json')['type'], 'CNAME')
        payload = b'\n'.join(data for files in self.uploads for data in files.values())
        self.assertNotIn(b'PRIVATE-CF-TOKEN', payload)
        self.assertNotIn(b'PRIVATE-PANEL-TOKEN', payload)
        self.assertIn(b'preserved-private-node-service-key-123456789', payload)
        self.assertIn(b'PRIVATE-CERTIFICATE-KEY', payload)

    def test_failed_origin_never_changes_dns_or_commits_and_same_target_resumes(self):
        self.origin_ok = False
        with self.assertRaisesRegex(u.DeployError, '9/9'):
            self.apply()
        self.assertEqual(self.dns_ip, OLD)
        self.assertFalse(any(call[0] == 'update' for call in self.dns_calls))
        self.assertIsNone(self.operation().load('commit.json'))
        self.assertIsNone(self.operation().load('origin-traffic.verified.json'))
        self.assertEqual(x.installation_snapshot(self.store.path)['pending_ip'], NEW)
        self.origin_ok = True
        self.apply()
        self.assertEqual(x.installation_snapshot(self.store.path)['current_ip'], NEW)
        self.assert_original_unchanged()

    def test_pending_target_guard_rejects_other_vps_before_new_side_effects(self):
        self.origin_ok = False
        with self.assertRaises(u.DeployError):
            self.apply()
        before = (copy.deepcopy(self.api.data), list(self.events), list(self.dns_calls))
        with self.assertRaisesRegex(u.DeployError, 'незавершённая'):
            self.apply(NEXT)
        self.assertEqual((self.api.data, self.events, self.dns_calls), before)
        self.assertEqual(self.operation().load('intent.json')['toIp'], NEW)

    def test_failed_cdn_after_dns_keeps_pending_and_resumes_without_duplicate_objects(self):
        self.cdn_ok = False
        with self.assertRaisesRegex(u.DeployError, 'CDN ещё'):
            self.apply()
        self.assertEqual(self.dns_ip, NEW)
        self.assertIsNotNone(self.operation().load('dns.verified.json'))
        self.assertIsNone(self.operation().load('traffic.verified.json'))
        self.assertIsNone(self.operation().load('commit.json'))
        self.assertIsNone(self.operation().load('export.verified.json'))
        self.cdn_ok = True
        self.apply()
        self.assertEqual(len(x.chain(self.store, self.plan)[0]), 1)
        self.assertEqual([len(self.api.data[k]) for k in ('nodes', 'hosts', 'config-profiles')], [1, 1, 1])
        self.assert_original_unchanged()

    def test_export_failure_never_commits_and_resume_keeps_existing_client_endpoint(self):
        self.export_ok = False
        with self.assertRaisesRegex(u.DeployError, 'Выгрузка'):
            self.apply()
        self.assertIsNone(self.operation().load('commit.json'))
        self.assertEqual(self.api.data['hosts'][0], self.before['hosts'][0])
        self.export_ok = True
        self.apply()
        self.assertEqual(self.api.data['hosts'][0]['address'], CDN)
        self.assertIsNotNone(self.operation().load('commit.json'))

    def test_topology_drift_during_origin_proof_is_preserved_and_blocks_dns(self):
        self.on_origin = lambda: self.api.data['hosts'][0].update(sni='owner-change.example.com')
        with self.assertRaisesRegex(u.DeployError, 'изменил'):
            self.apply()
        self.assertEqual(self.dns_ip, OLD)
        self.assertEqual(self.api.data['hosts'][0]['sni'], 'owner-change.example.com')
        self.assertFalse(any(call[0] == 'update' for call in self.dns_calls))
        self.assertIsNone(self.operation().load('commit.json'))

    def test_lost_patch_response_recovers_same_journal_without_second_patch(self):
        self.api.lose = 'PATCH'
        with self.assertRaisesRegex(u.DeployError, 'lost node PATCH'):
            self.apply()
        self.assertEqual(self.api.data['nodes'][0]['address'], NEW)
        self.assertTrue(self.api.data['nodes'][0]['isDisabled'])
        self.assertEqual(self.dns_ip, OLD)
        self.apply()
        self.assertEqual(len([1 for method, path, _ in self.api.calls if (method, path) == ('PATCH', '/nodes')]), 1)
        self.assertIsNotNone(self.operation().load('commit.json'))

    def test_lost_disable_or_enable_response_recovers_without_new_identities(self):
        for action in ('disable', 'enable'):
            with self.subTest(action=action):
                fixture = ReplacementTests('test_full_replacement_preserves_panel_and_client_identities_and_original_journal')
                fixture.setUp()
                try:
                    fixture.api.lose = action
                    with self.assertRaisesRegex(u.DeployError, 'lost node ' + action):
                        fixture.apply()
                    self.assertIsNone(fixture.operation().load('commit.json'))
                    fixture.apply()
                    self.assertIsNotNone(fixture.operation().load('commit.json'))
                    expected = copy.deepcopy(fixture.before)
                    expected['nodes'][0]['address'] = NEW
                    self.assertEqual(fixture.api.data, expected)
                finally:
                    fixture.doCleanups()

    def test_lost_dns_response_reconciles_saved_target_and_does_not_recreate_cnames(self):
        self.lose_dns = True
        with self.assertRaises(r.dns.DNSOutcomeUnknown):
            self.apply()
        self.assertEqual(self.dns_ip, NEW)
        self.assertIsNone(self.operation().load('commit.json'))
        self.apply()
        self.assertIsNotNone(self.operation().load('commit.json'))
        self.assert_original_unchanged()

    def test_snapshot_and_next_generation_follow_committed_guest_identity_and_ip(self):
        self.apply()
        snapshot = x.installation_snapshot(self.store.path)
        self.assertEqual((snapshot['current_ip'], snapshot['pending_ip'], snapshot['generation']), (NEW, None, 1))
        self.assertEqual(snapshot['machine_id'], self.machine(NEW)['machineId'])
        self.assertEqual(snapshot['ssh_fingerprint'], self.machine(NEW)['sshFingerprint'])
        self.assertEqual(snapshot['status_proof'], 'traffic')
        self.apply(NEXT)
        intent = self.operation(2).load('intent.json')
        self.assertEqual((intent['fromIp'], intent['toIp']), (NEW, NEXT))
        self.assertEqual(intent['previousCommitHash'], u.digest(self.operation().load('commit.json')))
        latest = x.installation_snapshot(self.store.path)
        self.assertEqual((latest['current_ip'], latest['generation']), (NEXT, 2))
        self.assertEqual(latest['ssh_fingerprint'], self.machine(NEXT)['sshFingerprint'])
        self.assert_original_unchanged()

    def test_incomplete_or_misidentified_origin_proof_never_authorizes_dns_cutover(self):
        changes = ({'scope': 'cdn'}, {'cdnVerified': True}, {'originIp': OLD},
                   {'expectedExitIp': OLD}, {'domain': CDN}, {'results': []})
        for change in changes:
            with self.subTest(change=change):
                fixture = ReplacementTests('test_full_replacement_preserves_panel_and_client_identities_and_original_journal')
                fixture.setUp()
                try:
                    fixture.origin_override = change
                    with self.assertRaisesRegex(u.DeployError, '9/9'):
                        fixture.apply()
                    self.assertEqual(fixture.dns_ip, OLD)
                    self.assertIsNone(fixture.operation().load('commit.json'))
                    self.assertNotIn('dns-update', fixture.events)
                finally:
                    fixture.doCleanups()

    def test_repeating_committed_target_does_not_ssh_or_create_another_operation(self):
        self.apply()
        before = (copy.deepcopy(self.api.data), list(self.events), list(self.dns_calls))
        output = self.apply()
        self.assertIn('уже текущий', output)
        self.assertEqual((self.api.data, self.events, self.dns_calls), before)
        self.assertEqual(len(list((self.store.path / 'replacements').iterdir())), 1)
        self.assert_original_unchanged()

    def test_modified_original_profile_is_rejected_before_operation_or_guest_changes(self):
        self.api.data['config-profiles'][0]['config']['inbounds'][0]['port'] = 9999
        with self.assertRaisesRegex(u.DeployError, 'профиль CDN изменён'):
            self.apply()
        self.assertEqual(self.events, [])
        self.assertEqual(self.dns_calls, [])
        self.assertFalse((self.store.path / 'replacements').exists())

    def test_pending_operation_cannot_change_its_original_verification_route(self):
        self.origin_ok = False
        with self.assertRaises(u.DeployError):
            self.apply()
        before = (copy.deepcopy(self.api.data), list(self.events), list(self.dns_calls))
        with self.assertRaises(u.DeployError):
            self.apply(NEW, '--test-route', 'physical')
        self.assertEqual((self.api.data, self.events, self.dns_calls), before)
        self.assertIsNone(self.operation().load('commit.json'))

    def test_external_cname_change_after_cdn_proof_blocks_commit_without_overwrite(self):
        self.on_cdn = lambda: setattr(self, 'cname_conflict', 'cdn')
        with self.assertRaises(d.records.dns.DNSConflict):
            self.apply()
        self.assertEqual(self.dns_ip, NEW)
        self.assertIsNotNone(self.operation().load('traffic.verified.json'))
        self.assertIsNone(self.operation().load('commit.json'))
        self.assertEqual(self.api.data['hosts'][0], self.before['hosts'][0])

    def test_external_origin_a_change_after_cdn_proof_blocks_commit_without_reverting_owner(self):
        self.on_cdn = lambda: setattr(self, 'dns_ip', NEXT)
        with self.assertRaisesRegex(u.DeployError, 'Owned DNS content changed'):
            self.apply()
        self.assertEqual(self.dns_ip, NEXT)
        self.assertIsNone(self.operation().load('commit.json'))
        self.assertIsNotNone(self.operation().load('traffic.verified.json'))

    def test_initial_additional_origin_binding_is_predecessor_of_two_vps_generations(self):
        extra = '8.8.8.8'
        base = c._nginx(self.plan).encode('ascii')
        binding = {'version': 1, 'deployment': self.plan['id'], 'domain': DOMAIN, 'code': self.plan['code'],
                   'oldIp': OLD, 'extraIp': extra, 'baseSha256': d.bindings.digest(base),
                   'extraSha256': d.bindings.digest(d.bindings.extra_config(base, OLD, extra)),
                   'passed': True, 'baseConfigUnchanged': True}
        self.store.save('origin-bindings.receipt.json', binding)
        self.api.data['nodes'][0]['address'] = extra
        self.dns_ip = extra
        self.apply()
        self.assertEqual(self.operation().load('intent.json')['fromIp'], extra)
        self.assertEqual(x.installation_snapshot(self.store.path)['current_ip'], NEW)
        self.apply(NEXT)
        self.assertEqual(self.operation(2).load('intent.json')['fromIp'], NEW)
        self.assertEqual(x.installation_snapshot(self.store.path)['current_ip'], NEXT)
        self.assertEqual(self.store.load('origin-bindings.receipt.json'), binding)
        self.assert_original_unchanged()

    def test_additional_origin_dns_with_original_panel_address_recovers_lost_patch_and_next_generation(self):
        extra = NEXT
        base = c._nginx(self.plan).encode('ascii')
        binding = {'version': 1, 'deployment': self.plan['id'], 'domain': DOMAIN, 'code': self.plan['code'],
                   'oldIp': OLD, 'extraIp': extra, 'baseSha256': d.bindings.digest(base),
                   'extraSha256': d.bindings.digest(d.bindings.extra_config(base, OLD, extra)),
                   'passed': True, 'baseConfigUnchanged': True}
        self.store.save('origin-bindings.receipt.json', binding)
        self.dns_ip = extra
        self.assertEqual(self.api.data['nodes'][0]['address'], OLD)
        self.api.lose = 'PATCH'
        with self.assertRaisesRegex(u.DeployError, 'lost node PATCH'):
            self.apply()
        intent = self.operation().load('intent.json')
        self.assertEqual(intent['fromIp'], extra)
        self.assertEqual(intent['snapshot']['node']['address'], OLD)
        self.assertEqual(self.dns_ip, extra)
        self.apply()
        self.assertEqual(x.installation_snapshot(self.store.path)['current_ip'], NEW)
        self.assertEqual(len([1 for method, path, _ in self.api.calls if (method, path) == ('PATCH', '/nodes')]), 1)
        self.apply(NEXT)
        self.assertEqual(self.operation(2).load('intent.json')['fromIp'], NEW)
        self.assertEqual(x.installation_snapshot(self.store.path)['current_ip'], NEXT)
        self.assertEqual(self.store.load('origin-bindings.receipt.json'), binding)
        self.assert_original_unchanged()

    def test_pending_retry_rejects_changed_machine_id_even_when_ssh_fingerprint_is_unchanged(self):
        self.origin_ok = False
        with self.assertRaisesRegex(u.DeployError, '9/9'):
            self.apply()
        operation = self.operation()
        intent = operation.load('intent.json')
        machine_files = {path.name: path.read_bytes() for path in operation.path.glob('machine-*.json')}
        self.assertEqual(len(machine_files), 1)
        pinned_fingerprint = self.machine(NEW)['sshFingerprint']
        self.machine_overrides[NEW] = {'machineId': 'f' * 32, 'sshFingerprint': pinned_fingerprint}
        self.assertEqual(self.args().host_key_sha256, pinned_fingerprint)
        self.events.clear()
        self.api.calls.clear()
        self.dns_calls.clear()
        with self.assertRaisesRegex(u.DeployError, 'machine-id'):
            self.apply()
        self.assertIn('identity', self.events)
        for action in ('packages', 'source-check', 'source-apply', 'install', 'source-verify-captured',
                       'origin-proof', 'dns-update', 'cdn-proof'):
            self.assertNotIn(action, self.events)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(any(call[0] == 'update' for call in self.dns_calls))
        self.assertEqual({path.name: path.read_bytes() for path in operation.path.glob('machine-*.json')},
                         machine_files)
        self.assertEqual(operation.load('intent.json'), intent)
        self.assertIsNone(operation.load('commit.json'))
        self.assertEqual(x.installation_snapshot(self.store.path)['pending_ip'], NEW)
        self.assertEqual(self.dns_ip, OLD)
        self.assert_original_unchanged()

    def test_guest_failure_before_origin_proof_keeps_dns_and_pending_journal(self):
        self.fail_action = 'packages'
        with self.assertRaisesRegex(u.DeployError, 'guest failure'):
            self.apply()
        self.assertEqual(self.dns_ip, OLD)
        self.assertNotIn('source-apply', self.events)
        self.assertNotIn('origin-proof', self.events)
        self.assertIsNone(self.operation().load('commit.json'))

    def test_offline_plan_reads_no_state_credentials_or_network(self):
        args = x.parser().parse_args(['--domain', DOMAIN, '--ip', NEW, '--state-dir', '/not-read'])
        with patch.object(u, 'read_settings', side_effect=AssertionError('credential read')), \
                patch.object(u, 'Store', side_effect=AssertionError('state read')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            x.run(args)
        self.assertFalse(json.loads(output.getvalue())['changes'])
        self.assertFalse(json.loads(output.getvalue())['changesYandexResources'])


if __name__ == '__main__':
    unittest.main()
