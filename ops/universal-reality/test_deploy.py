"""Offline regression tests: no panel calls, SSH, DNS, VPS or real payments."""
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('reality_deploy_under_test', HERE / 'deploy.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
IP, PANEL, BASE = '8.8.4.4', '1.1.1.1', 'https://panel.example.com/api'
SQUAD = '11111111-1111-4111-8111-111111111111'


class FakeAPI:
    def __init__(self):
        self.data = {'config-profiles': [], 'nodes': [], 'hosts': [], 'internal-squads': [
            {'uuid': SQUAD, 'name': 'regular', 'inbounds': [{'uuid': 'old-inbound'}]}]}
        self.calls = []
        self.lose = None
        self.number = 0

    def rows(self, kind):
        return copy.deepcopy(self.data[kind])

    def disable_node(self, uid):
        node = next(row for row in self.data['nodes'] if row['uuid'] == uid)
        self.calls.append(('POST', '/nodes/' + uid + '/actions/disable', None))
        node['isDisabled'] = True
        return copy.deepcopy(node)

    def enable_node(self, uid):
        node = next(row for row in self.data['nodes'] if row['uuid'] == uid)
        self.calls.append(('POST', '/nodes/' + uid + '/actions/enable', None))
        node['isDisabled'] = False
        return copy.deepcopy(node)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == 'GET' and path == '/keygen':
            return {'secretKey': 'test-node-service-secret-123456789'}
        kind = path.lstrip('/')
        if method == 'POST':
            self.number += 1
            row = {'uuid': f'00000000-0000-4000-8000-{self.number:012d}', **copy.deepcopy(body)}
            if kind == 'config-profiles':
                row['inbounds'] = [{'uuid': 'inbound-' + row['uuid'], 'tag': inbound['tag']}
                                   for inbound in row['config']['inbounds']]
            if kind == 'nodes':
                row.update(isConnected=True, isDisabled=False, lastStatusMessage=None)
            self.data[kind].append(row)
            if self.lose == kind:
                self.lose = None
                raise d.u.DeployError('Simulated lost response after successful creation')
            return copy.deepcopy(row)
        if method == 'PATCH':
            row = next(row for row in self.data[kind] if row['uuid'] == body['uuid'])
            row.update(copy.deepcopy(body))
            return copy.deepcopy(row)
        raise AssertionError('Unexpected API operation')


class FakeSSH:
    calls = []
    connections = 0
    on_action = None
    extra_addresses = set()
    fresh = True

    def __init__(self, plan, known_hosts=None, control_socket=None):
        self.plan = plan

    def __enter__(self):
        FakeSSH.connections += 1
        return self

    def __exit__(self, *args):
        pass

    def upload(self, files):
        self.calls.append(('upload', set(files)))
        if 'node.env' in files:
            assert b'REMNAWAVE_API_TOKEN' not in files['node.env']

    def action(self, action, capture=False):
        self.calls.append(('action', action))
        if FakeSSH.on_action:
            FakeSSH.on_action(action)
        if action == 'identity':
            result = {'machineId': 'a' * 32, 'sshFingerprint': 'SHA256:' + 'A' * 43, 'ownedInstallation': not FakeSSH.fresh}
            FakeSSH.fresh = False
            return json.dumps(result).encode()

    def source(self, action, capture=False):
        self.calls.append(('source', action))
        if action == 'check':
            return json.dumps({'needsChange': False, 'currentSource': self.plan['ip']}).encode()

    def assigned_ipv4(self):
        return {self.plan['ip']} | FakeSSH.extra_addresses

    @contextlib.contextmanager
    def test_forward(self):
        self.calls.append(('forward', 'open'))
        try:
            yield 19987
        finally:
            self.calls.append(('forward', 'close'))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.api = FakeAPI()
        FakeSSH.calls, FakeSSH.connections, FakeSSH.on_action = [], 0, None
        FakeSSH.extra_addresses = set()
        FakeSSH.fresh = True
        self.probe_passed = True
        self.export_error = None
        self.physical_error = None
        self.probe_route_calls = []
        self.probe_ip_calls = []
        self.dns_token = 'cloudflare-private-test-token'
        self.dns_error = None
        self.dns_calls = []
        self.export_calls = []
        self.authoritative_error = None
        self.guard_calls, self.sni_calls = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, *extra):
        return d.parser().parse_args(['--ip', IP, '--transport', 'reality', '--name', 'Финляндия', '--country', 'FI', '--squad', 'regular', *extra])

    def store(self, name='state'):
        return d.u.Store(self.root / name)

    def plan(self, store=None, args=None):
        store = store or self.store()
        return d.prepare(self.api, store, args or self.args(), BASE, PANEL)

    def install(self, args):
        self.proof = {'passed': self.probe_passed, 'tests': 9, 'ip': args.ip,
                      'results': [{'passed': self.probe_passed, 'clientAlive': True} for _ in range(9)],
                      'physicalSourceObserved': True, 'fingerprint': 'safari'}
        def probe_result(*a, **kw):
            route = kw.get('route', 'physical')
            self.probe_route_calls.append(route)
            self.probe_ip_calls.append(a[0])
            if route == 'physical' and self.physical_error:
                raise self.physical_error
            result = copy.deepcopy(self.proof)
            result.update(testRoute=route, directReachabilityVerified=route == 'physical',
                          physicalSourceObserved=route == 'physical')
            return result
        owner = self
        class FakeDNS:
            def __init__(self, token):
                owner.dns_calls.append(('token', token))
            def find_zone(self, name):
                owner.dns_calls.append(('zone', name))
                return {'id': 'zone123', 'name': name, 'name_servers': ['alice.ns.cloudflare.com']}
            def check_record(self, store, **kw):
                owner.dns_calls.append(('check', kw))
                if owner.dns_error:
                    raise owner.dns_error
            def ensure_record(self, store, **kw):
                owner.dns_calls.append(('ensure', kw))
                FakeSSH.calls.append(('dns', 'ensure'))
                store.save('dns.receipt.json', kw)
        def exported(*a, **kw):
            self.export_calls.append((a, kw))
            if self.export_error:
                raise self.export_error
            return {'passed': True}
        def node_guard(ssh, active_ip, *, old_ip=None, verify=False, **kwargs):
            self.guard_calls.append((active_ip, old_ip, verify))
            return {'activeIp': active_ip, 'deployment': ssh.plan['id'], 'reserveVpnBlocked': True,
                    'scannerFilterEnabled': True, 'bootPersistenceVerified': True}
        def sni_guard(ssh, *, verify=False):
            self.sni_calls.append((ssh.plan['ip'], verify))
            return {'sniFiltered': True, 'allowedSni': ssh.plan.get('sni', d.SNI), 'tlsTerminated': False,
                    'validSniHandshakeVerified': True, 'unknownSniRejected': True, 'missingSniRejected': True}
        with patch.object(d, 'API', return_value=self.api), patch.object(d.u, 'read_settings', return_value={
                'REMNAWAVE_BASE_URL': BASE, 'REMNAWAVE_API_TOKEN': 'private-api-token-never-upload',
                'CLOUDFLARE_API_TOKEN': self.dns_token}), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(d.dns, 'CloudflareDNS', FakeDNS), \
                patch.object(d, 'verify_authoritative_dns', side_effect=self.authoritative_error), \
                patch.object(d, 'panel_ipv4', return_value=PANEL), patch.object(d, 'SSH', FakeSSH), \
                patch.object(d.trust, 'resolve', return_value=self.root / 'known-hosts'), \
                patch.object(d.probe, 'private_file', return_value=self.root / 'subscription'), \
                patch.object(d.probe, 'subscription_url', return_value='https://sub.wayspn.online/private-test'), \
                patch.object(d.probe, 'select_engine', return_value=self.root / 'xray'), \
                patch.object(d.probe, 'run', side_effect=probe_result), \
                patch.object(d.probe, 'verify_export', side_effect=exported), \
                patch.object(d.security, 'guard', side_effect=node_guard), \
                patch.object(d.security, 'sni', side_effect=sni_guard), \
                patch.object(d, 'register_fleet'), \
                patch('spn_reserve_fleet.remote.load_controller', return_value=None), \
                patch.object(d.time, 'sleep'), \
                patch.object(d, 'state_directory', side_effect=lambda base, ip, port, home=None: self.root / (ip + '-' + str(port))), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            d.run(args)
        return output.getvalue()

    def test_plan_only_has_no_network_or_private_state(self):
        with patch.object(d, 'API', side_effect=AssertionError('network')), \
                patch.object(d, 'state_directory', side_effect=AssertionError('disk')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            d.run(self.args())
        self.assertFalse(json.loads(output.getvalue())['changes'])

    def test_new_default_domain_reuses_code_and_dns_only_ttl(self):
        plan = self.plan()
        self.assertEqual(plan['domain'], 'n-' + plan['code'] + '.wayspn.com')
        self.assertEqual(plan['dns'], {'provider': 'cloudflare', 'zone': 'wayspn.com', 'ttl': 60})
        self.assertEqual(d.parser().parse_args(['--ip', IP]).dns_zone, 'wayspn.com')

    def test_initial_installer_never_reverts_started_or_completed_vps_replacement(self):
        store = self.store()
        self.plan(store)
        operation = d.u.Store(store.path / 'replacements' / '000001')
        operation.save('intent.json', {'domain': 'owned.wayspn.com', 'toIp': '8.8.8.8'})
        for completed in (False, True):
            with self.subTest(completed=completed):
                if completed:
                    operation.save('commit.json', {'trafficAndExportVerified': True})
                with self.assertRaises(d.u.DeployError) as caught:
                    self.plan(store)
                self.assertIn('replace.py', str(caught.exception))
                self.assertEqual(self.api.calls, [])

    def test_initial_installer_rejects_symlinked_replacement_journal(self):
        store = self.store()
        self.plan(store)
        (store.path / 'replacements').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(d.u.DeployError):
            self.plan(store)
        self.assertEqual(self.api.calls, [])

    def test_explicit_domain_and_zone_are_immutable_but_optional_on_resume(self):
        store = self.store()
        plan = self.plan(store, self.args('--dns-zone', 'example.com', '--domain', 'node.example.com'))
        self.assertEqual(self.plan(store, d.parser().parse_args(['--ip', IP])), plan)
        for extra in (['--domain', 'other.example.com', '--dns-zone', 'example.com'],
                      ['--dns-zone', 'wayspn.com']):
            with self.subTest(extra=extra), self.assertRaises(d.u.DeployError):
                self.plan(store, self.args(*extra))
        self.assertEqual(self.api.calls, [])

    def legacy_plan(self, store):
        plan = {'format': 1, 'id': 'a' * 32, 'code': 'old1ip', 'ip': IP, 'sshPort': 22,
                'panel': BASE, 'panelIp': PANEL, 'name': 'Финляндия', 'country': 'FI',
                'squads': [SQUAD], 'transport': 'raw-reality-vision-safari-v1'}
        store.save('plan.json', plan)
        return plan

    def test_saved_legacy_plan_stays_ip_without_cloudflare_credentials(self):
        store = self.store(IP + '-22')
        legacy = self.legacy_plan(store)
        self.dns_token = None
        self.install(self.args('--apply', '--env-file', str(self.root / 'missing.env'),
                               '--cloudflare-token-file', str(self.root / 'missing-token')))
        self.assertEqual(store.load('plan.json'), legacy)
        self.assertEqual(self.dns_calls, [])
        self.assertEqual(self.api.data['nodes'][0]['address'], IP)
        self.assertEqual(self.api.data['hosts'][0]['address'], IP)
        with self.assertRaises(d.u.DeployError):
            self.plan(store, self.args('--domain', 'new.wayspn.com'))

    def test_missing_cloudflare_token_stops_before_any_external_mutation(self):
        self.dns_token = None
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply', '--env-file', str(self.root / 'missing.env'),
                                   '--cloudflare-token-file', str(self.root / 'missing-token')))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(FakeSSH.calls, [])
        self.assertEqual(FakeSSH.connections, 0)

    def test_cloudflare_conflict_preflight_stops_before_vps_or_panel_writes(self):
        self.dns_error = d.dns.DNSConflict('Unowned A record; no overwrite')
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(FakeSSH.calls, [])
        self.assertFalse(any(kind == 'ensure' for kind, value in self.dns_calls))

    def test_domain_used_for_both_panel_addresses_but_probe_stays_physical_ip(self):
        self.install(self.args('--apply'))
        node, host = self.api.data['nodes'][0], self.api.data['hosts'][0]
        self.assertEqual(node['address'], host['address'])
        self.assertEqual(node['port'], 2222)
        self.assertRegex(host['address'], r'^n-[a-z0-9]{6}\.wayspn\.com$')
        self.assertEqual(host['sni'], 'www.cloudflare.com')
        self.assertEqual(self.probe_ip_calls, [IP])
        self.assertEqual(self.export_calls[0][0][0], IP)
        self.assertEqual(self.export_calls[0][1]['endpoint_address'], host['address'])
        self.assertLess(FakeSSH.calls.index(('action', 'preflight')), FakeSSH.calls.index(('dns', 'ensure')))
        self.assertLess(FakeSSH.calls.index(('source', 'apply')), FakeSSH.calls.index(('dns', 'ensure')))

    def test_dns_propagation_failure_never_creates_or_publishes_panel_node(self):
        self.authoritative_error = d.u.DeployError('Authoritative DNS not ready')
        with patch.object(d.time, 'monotonic', side_effect=[0, 6]), self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply', '--wait', '5'))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.data['nodes'], [])
        self.assertNotIn(('action', 'install'), FakeSSH.calls)

    def test_authoritative_a_and_aaaa_queries_bypass_os_endpoint_resolver(self):
        plan = self.plan()
        zone = {'name_servers': ['alice.ns.cloudflare.com', 'bob.ns.cloudflare.com']}
        with patch.object(d, '_dig', side_effect=[zone['name_servers'], ['1.1.1.1'], [IP], [], ['8.8.8.8'], [IP], []]) as query, \
                patch.object(d.socket, 'getaddrinfo', side_effect=AssertionError('OS FakeIP resolver')):
            d.verify_authoritative_dns(plan, zone)
        self.assertEqual(query.call_count, 7)
        self.assertEqual(query.call_args_list[0].args, ('1.1.1.1', 'wayspn.com', 'NS'))
        self.assertEqual(query.call_args_list[2].args, ('1.1.1.1', plan['domain'], 'A'))
        self.assertEqual(query.call_args_list[3].args, ('1.1.1.1', plan['domain'], 'AAAA'))
        with patch.object(d, '_dig', side_effect=[zone['name_servers'], ['1.1.1.1'], [IP], ['2606:4700::1111']]), \
                self.assertRaises(d.u.DeployError):
            d.verify_authoritative_dns(plan, zone)
        with patch.object(d, '_dig', return_value=['foreign.ns.example.com']), self.assertRaises(d.u.DeployError):
            d.verify_authoritative_dns(plan, zone)

    def test_dig_requires_authority_and_rejects_cname_or_wrong_answers(self):
        header = ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\n;; flags: qr aa; QUERY: 1\n'
        good = types.SimpleNamespace(returncode=0, stdout=(header + 'n.example.com. 60 IN A ' + IP + '\n').encode())
        with patch.object(d.subprocess, 'run', return_value=good):
            self.assertEqual(d._dig('1.1.1.1', 'n.example.com', 'A', authoritative=True), [IP])
        for output in (header.replace('qr aa', 'qr ra'),
                       header + 'n.example.com. 60 IN CNAME other.example.com.\n'):
            with patch.object(d.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=output.encode())), \
                    self.assertRaises(d.u.DeployError):
                d._dig('1.1.1.1', 'n.example.com', 'A', authoritative=True)

    def test_cloudflare_token_sources_are_local_and_private(self):
        args = self.args('--env-file', str(self.root / 'private.env'),
                         '--cloudflare-token-file', str(self.root / 'token'))
        with patch.dict(os.environ, {'CLOUDFLARE_API_TOKEN': 'env-token'}, clear=True):
            self.assertEqual(d.cloudflare_token(args, {'CLOUDFLARE_API_TOKEN': 'settings-token'}), 'env-token')
        # Test fixture files are private, and no secret is ever put in argv.
        Path(args.env_file).write_text('CLOUDFLARE_API_TOKEN="dotenv-token"\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(d.cloudflare_token(args, {}), 'dotenv-token')
            Path(args.env_file).unlink()
            token_path = Path(args.cloudflare_token_file)
            token_path.write_text('file-token\n')
            token_path.chmod(0o600)
            self.assertEqual(d.cloudflare_token(args, {}), 'file-token')
            token_path.chmod(0o644)
            with self.assertRaises(d.u.DeployError):
                d.cloudflare_token(args, {})

    def test_input_validation(self):
        for option in (['--ip', '127.0.0.1'], ['--ip', 'not-an-ip'], ['--ssh-port', '443'],
                       ['--country', 'finland'], ['--name', 'hello\nworld'], ['--wait', '0']):
            with self.subTest(option=option), self.assertRaises(d.u.DeployError):
                d.validate(d.parser().parse_args(['--ip', IP, *option]))

    def test_names_share_six_alphanumeric_suffix(self):
        store = self.store()
        plan = self.plan(store)
        deployment = d.Deployment(self.api, store, plan)
        profile, node, inbound = deployment.provision()
        host = deployment.host(profile, node, inbound)
        code = plan['code']
        self.assertRegex(code, r'^[a-z0-9]{6}$')
        self.assertTrue(any(c.isalpha() for c in code) and any(c.isdigit() for c in code))
        for name in (profile['name'], profile['inbounds'][0]['tag'], node['name'], host['remark']):
            self.assertTrue(name.endswith(code), name)

    def test_collision_is_regenerated_before_creation(self):
        rows = {'config-profiles': [{'name': 'REALITY_aaaaa1', 'inbounds': [{'tag': 'some_aaaaa1'}]}]}
        with patch.object(d.secrets, 'choice', side_effect=list('aaaaa1bbb2bb')):
            self.assertEqual(d.suffix(rows), 'bbb2bb')
        self.assertEqual(self.api.calls, [])

    def test_schema_matches_phone_verified_single_server(self):
        original_spec = importlib.util.spec_from_file_location('verified_node75', HERE.parent / 'node75-single/manage.py')
        verified = importlib.util.module_from_spec(original_spec)
        original_spec.loader.exec_module(verified)
        keys = {'private': 'private-key', 'short': '0123456789abcdef'}
        expected = verified.config(keys['private'], keys['short'])
        expected['inbounds'][0]['tag'] = 'reality-a1b2c3'
        expected['inbounds'][0]['streamSettings']['realitySettings'].update(
            target='www.cloudflare.com:443', serverNames=['www.cloudflare.com'])
        expected['inbounds'][0].update(listen='127.0.0.1', port=2443)
        expected['inbounds'][0]['streamSettings']['sockopt'] = {'acceptProxyProtocol': True}
        expected['policy'] = {'levels': {'0': {'handshake': 4, 'connIdle': 120, 'uplinkOnly': 5, 'downlinkOnly': 5}}}
        self.assertEqual(d.profile_config('a1b2c3', keys), expected)
        self.assertEqual(d.FINGERPRINT, 'safari')

    def test_legacy_listener_upgrade_preserves_profile_node_key_and_inbound(self):
        store = self.store()
        plan = self.plan(store)
        deployment = d.Deployment(self.api, store, plan)
        with patch.object(d, 'profile_config', side_effect=d.legacy_profile_config):
            before_profile, before_node, before_inbound = deployment.provision()
        after_profile, after_node, after_inbound = deployment.provision()
        self.assertEqual(before_profile['uuid'], after_profile['uuid'])
        self.assertEqual(before_node['uuid'], after_node['uuid'])
        self.assertEqual(before_inbound, after_inbound)
        self.assertEqual(after_profile['config']['inbounds'][0]['listen'], '127.0.0.1')
        self.assertEqual(after_profile['config']['inbounds'][0]['port'], 2443)
        self.assertTrue(store.load('profile-guard.receipt.json'))
        deployment.provision()
        self.assertEqual(len(self.api.data['config-profiles']), 1)

    def test_legacy_shared_profile_is_not_rewritten(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        with patch.object(d, 'profile_config', side_effect=d.legacy_profile_config):
            profile, node, inbound = deployment.provision()
        self.api.data['nodes'].append({'uuid': 'foreign-node', 'address': '8.8.8.8',
                                      'configProfile': copy.deepcopy(node['configProfile'])})
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        self.assertEqual(self.api.data['config-profiles'][0]['config'], profile['config'])

    def test_saved_key_reused_after_interruption_before_plan(self):
        store = self.store()
        keys = d.reality_keys(store)
        plan = self.plan(store)
        self.assertEqual(keys, d.reality_keys(store))
        self.assertEqual(d.profile_config(plan['code'], keys)['inbounds'][0]['streamSettings']['realitySettings']['privateKey'], keys['private'])

    def test_resume_without_optional_arguments_inherits_values(self):
        store = self.store()
        before = self.plan(store)
        args = d.parser().parse_args(['--ip', IP])
        self.assertEqual(self.plan(store, args), before)

    def test_wrong_existing_target_is_rejected(self):
        store = self.store()
        self.plan(store)
        args = d.parser().parse_args(['--ip', '8.8.8.8'])
        with self.assertRaises(d.u.DeployError):
            self.plan(store, args)
        self.assertEqual(self.api.calls, [])

    def test_foreign_same_ip_not_adopted(self):
        self.api.data['nodes'].append({'uuid': 'foreign', 'name': 'not-ours', 'address': IP})
        with self.assertRaises(d.u.DeployError):
            self.plan()
        self.assertEqual(self.api.calls, [])

    def test_reinstall_on_new_ip_creates_new_plan_without_legacy_requirement(self):
        plan = self.plan(args=self.args('--reinstall'))
        self.assertEqual(plan['ip'], IP)
        self.assertEqual(self.api.calls, [])

    def test_profile_lost_response_recovers_without_duplicate(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        self.api.lose = 'config-profiles'
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        deployment.provision()
        self.assertEqual(len(self.api.data['config-profiles']), 1)
        self.assertEqual(len(self.api.data['nodes']), 1)

    def test_node_lost_response_recovers_without_duplicate(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        self.api.lose = 'nodes'
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        deployment.provision()
        self.assertEqual(len(self.api.data['nodes']), 1)

    def test_host_lost_response_recovers_by_own_inbound(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        profile, node, inbound = deployment.provision()
        self.api.lose = 'hosts'
        with self.assertRaises(d.u.DeployError):
            deployment.host(profile, node, inbound)
        deployment.host(profile, node, inbound)
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_unknown_create_outcome_does_not_repeat_post(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        keys = d.reality_keys(store)
        store.save('profile.intent.json', {'name': 'REALITY_' + deployment.plan['code'],
                                         'config': d.profile_config(deployment.plan['code'], keys)})
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        self.assertEqual(self.api.calls, [])

    def test_removed_owned_uuid_not_replaced_with_lookalike(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        deployment.provision()
        self.api.data['nodes'][0]['uuid'] = 'foreign-replacement'
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        self.assertEqual(len(self.api.data['nodes']), 1)

    def test_receipted_node_country_correction_is_preserved_without_writes(self):
        store = self.store()
        plan = self.plan(store, self.args('--country', 'AU', '--name', 'Австрия'))
        deployment = d.Deployment(self.api, store, plan)
        deployment.provision()
        self.api.data['nodes'][0]['countryCode'] = 'AT'
        before_rows = copy.deepcopy(self.api.data)
        before_calls = list(self.api.calls)
        journal = {name: (store.path / name).read_bytes()
                   for name in ('plan.json', 'node.intent.json', 'node.receipt.json')}
        _, node, _ = deployment.provision()
        self.assertEqual(node['countryCode'], 'AT')
        self.assertEqual(self.api.data, before_rows)
        self.assertEqual(self.api.calls, before_calls)
        self.assertEqual({name: (store.path / name).read_bytes() for name in journal}, journal)

    def test_country_exception_still_rejects_receipted_node_technical_drift(self):
        changes = [
            {'name': 'manually-renamed-node'},
            {'address': '8.8.8.8'},
            {'port': 8443},
            {'configProfile': {'activeConfigProfileUuid': 'other-profile', 'activeInbounds': ['other-inbound']}},
            {'active_inbound': 'other-inbound'},
        ]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                store = self.store('technical-drift-' + str(index))
                deployment = d.Deployment(self.api, store, self.plan(store))
                _, created, _ = deployment.provision()
                actual = next(row for row in self.api.data['nodes'] if row['uuid'] == created['uuid'])
                actual['countryCode'] = 'AT'
                if 'active_inbound' in change:
                    actual['configProfile']['activeInbounds'] = [change['active_inbound']]
                else:
                    actual.update(copy.deepcopy(change))
                before_rows, before_calls = copy.deepcopy(self.api.data), list(self.api.calls)
                with self.assertRaises(d.u.DeployError):
                    deployment.provision()
                self.assertEqual(self.api.data, before_rows)
                self.assertEqual(self.api.calls, before_calls)

    def test_country_exception_does_not_adopt_unreceipted_node_after_lost_response(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        self.api.lose = 'nodes'
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        self.assertIsNone(store.load('node.receipt.json'))
        self.api.data['nodes'][0]['countryCode'] = 'AT'
        before_rows, before_calls = copy.deepcopy(self.api.data), list(self.api.calls)
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        self.assertIsNone(store.load('node.receipt.json'))
        self.assertEqual(self.api.data, before_rows)
        self.assertEqual(self.api.calls, before_calls)

    def test_country_exception_does_not_accept_a_changed_original_request(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        deployment.provision()
        self.api.data['nodes'][0]['countryCode'] = 'AT'
        changed_request = {**store.load('node.intent.json'), 'countryCode': 'AT'}
        before_calls = list(self.api.calls)
        with self.assertRaises(d.u.DeployError):
            deployment.ensure('node', 'nodes', changed_request, lambda row: True)
        self.assertEqual(self.api.calls, before_calls)
        self.assertEqual(store.load('node.intent.json')['countryCode'], 'FI')

    def test_squad_addition_preserves_previous_members(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        profile, node, inbound = deployment.provision()
        deployment.attach(inbound)
        deployment.attach(inbound)
        self.assertEqual(d.u.ids(self.api.data['internal-squads'][0]['inbounds']), ['old-inbound', inbound])
        self.assertEqual(len([c for c in self.api.calls if c[:2] == ('PATCH', '/internal-squads')]), 1)

    def test_publish_only_changes_owned_host_availability(self):
        store = self.store()
        deployment = d.Deployment(self.api, store, self.plan(store))
        profile, node, inbound = deployment.provision()
        host = deployment.host(profile, node, inbound)
        self.assertTrue(host['isDisabled'])
        deployment.publish(host)
        actual = self.api.data['hosts'][0]
        self.assertFalse(actual['isDisabled'])
        self.assertEqual(actual['fingerprint'], 'safari')
        self.assertEqual(actual['sni'], 'www.cloudflare.com')
        self.assertEqual(actual['nodes'], [node['uuid']])

    def test_full_install_resume_and_clean_reinstall_no_duplicates(self):
        self.install(self.args('--apply'))
        self.assertEqual(FakeSSH.connections, 1)
        original = copy.deepcopy(self.api.data)
        self.install(d.parser().parse_args(['--ip', IP, '--apply']))
        self.assertEqual(FakeSSH.connections, 2)
        self.install(d.parser().parse_args(['--ip', IP, '--apply', '--reinstall']))
        self.assertEqual(FakeSSH.connections, 3)
        self.assertEqual(self.api.data, original)
        self.assertEqual(len([c for c in self.api.calls if c[:2] == ('GET', '/keygen')]), 1)
        for kind in ('config-profiles', 'nodes', 'hosts'):
            self.assertEqual(len(self.api.data[kind]), 1)

    def test_new_ip_new_code_independent_state_old_objects_unchanged(self):
        self.install(self.args('--apply'))
        old = copy.deepcopy(self.api.data)
        self.install(d.parser().parse_args(['--ip', '8.8.8.8', '--name', 'Другой', '--squad', 'regular', '--apply']))
        for kind in ('config-profiles', 'nodes', 'hosts'):
            self.assertEqual(self.api.data[kind][0], old[kind][0])
            self.assertEqual(len(self.api.data[kind]), 2)
        codes = [row['name'][-6:] for row in self.api.data['config-profiles']]
        self.assertNotEqual(codes[0], codes[1])

    def test_new_ip_same_erased_guest_retires_only_our_old_controller(self):
        self.install(self.args('--apply'))
        old_node = self.api.data['nodes'][0]['uuid']
        FakeSSH.extra_addresses = {IP}
        self.install(d.parser().parse_args(['--ip', '8.8.8.8', '--squad', 'regular', '--apply', '--reinstall']))
        self.assertTrue(self.api.data['nodes'][0]['isDisabled'])
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertFalse(self.api.data['nodes'][1]['isDisabled'])
        self.assertFalse(self.api.data['hosts'][1]['isDisabled'])
        self.assertIn(('POST', '/nodes/' + old_node + '/actions/disable', None), self.api.calls)

    def test_busy_previous_owned_domain_installation_is_not_retired(self):
        self.install(self.args('--apply'))
        before = copy.deepcopy(self.api.data)
        FakeSSH.extra_addresses = {IP}
        previous = self.store(IP + '-22')
        with previous.lock(), self.assertRaises(d.u.DeployError):
            self.install(d.parser().parse_args(['--ip', '8.8.8.8', '--squad', 'regular', '--apply']))
        self.assertEqual(self.api.data, before)

    def test_foreign_alias_controller_not_disabled(self):
        self.api.data['nodes'].append({'uuid': 'not-ours', 'name': 'foreign', 'address': '8.8.8.8', 'isDisabled': False})
        FakeSSH.extra_addresses = {'8.8.8.8'}
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertFalse(self.api.data['nodes'][0]['isDisabled'])

    def test_old_owned_alias_can_be_retired_after_manual_profile_change(self):
        self.install(self.args('--apply'))
        self.api.data['nodes'][0]['configProfile'] = {'activeConfigProfileUuid': 'manually-selected', 'activeInbounds': ['manual-inbound']}
        FakeSSH.extra_addresses = {IP}
        self.install(d.parser().parse_args(['--ip', '8.8.8.8', '--squad', 'regular', '--apply']))
        self.assertTrue(self.api.data['nodes'][0]['isDisabled'])
        self.assertEqual(self.api.data['nodes'][0]['configProfile']['activeConfigProfileUuid'], 'manually-selected')

    def test_erased_guest_restores_own_node_binding_without_extra_flag(self):
        self.install(self.args('--apply'))
        expected = copy.deepcopy(self.api.data['nodes'][0]['configProfile'])
        self.api.data['nodes'][0]['configProfile'] = {'activeConfigProfileUuid': 'other-profile', 'activeInbounds': []}
        self.api.data['nodes'][0]['isDisabled'] = True
        FakeSSH.fresh = True
        self.install(d.parser().parse_args(['--ip', IP, '--apply']))
        self.assertEqual(self.api.data['nodes'][0]['configProfile'], expected)
        self.assertFalse(self.api.data['nodes'][0]['isDisabled'])
        self.assertEqual(len(self.api.data['nodes']), 1)

    def test_completed_rerun_does_not_undo_manual_host_disable(self):
        self.install(self.args('--apply'))
        self.api.data['hosts'][0]['isDisabled'] = True
        before = len(self.api.calls)
        self.install(d.parser().parse_args(['--ip', IP, '--apply']))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(len(self.api.calls), before)

    def test_manual_extra_squad_is_preserved_on_resume_and_reinstall(self):
        self.install(self.args('--apply'))
        inbound = self.api.data['config-profiles'][0]['inbounds'][0]['uuid']
        extra = {'uuid': 'manual-bypass-squad', 'name': 'bypass', 'inbounds': [{'uuid': inbound}]}
        self.api.data['internal-squads'].append(copy.deepcopy(extra))
        self.install(d.parser().parse_args(['--ip', IP, '--apply']))
        self.install(d.parser().parse_args(['--ip', IP, '--apply', '--reinstall']))
        self.assertEqual(self.api.data['internal-squads'][-1], extra)
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_squad_configuration_precedes_remote_listener_verification(self):
        seen = []
        def on_action(action):
            if action == 'verify':
                inbound = self.api.data['config-profiles'][0]['inbounds'][0]['uuid']
                self.assertIn(inbound, d.u.ids(self.api.data['internal-squads'][0]['inbounds']))
                seen.append(action)
        FakeSSH.on_action = on_action
        self.install(self.args('--apply'))
        self.assertEqual(seen, ['verify'])

    def test_real_traffic_failure_keeps_new_host_disabled_and_resumes(self):
        self.probe_passed = False
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertFalse((self.root / (IP+'-22') / 'activation.verified.json').exists())
        self.probe_passed = True
        self.install(d.parser().parse_args(['--ip', IP, '--apply']))
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_owner_can_enable_staged_host_before_resuming(self):
        self.probe_passed = False
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.api.data['hosts'][0]['isDisabled'] = False
        self.probe_passed = True
        self.install(d.parser().parse_args(['--ip', IP, '--apply']))
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_whitelists_auto_tests_configuration_via_ssh_but_not_claim_direct(self):
        self.physical_error = d.probe.ProbeError('Direct TCP blocked before TLS')
        output = self.install(self.args('--apply'))
        self.assertEqual(self.probe_route_calls, ['physical', 'ssh'])
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])
        self.assertNotIn('Готово:', output)
        self.assertIn('Прямой доступ пока не подтверждён', output)
        folder = self.root / (IP+'-22')
        proof = json.loads(next(folder.glob('vpn-test-*.json')).read_text())
        self.assertEqual(proof['testRoute'], 'ssh')
        self.assertFalse(proof['physicalSourceObserved'])

    def test_explicit_physical_failure_never_falls_back_to_system(self):
        self.physical_error = d.probe.ProbeError('Direct TCP blocked before TLS')
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply', '--test-route', 'physical'))
        self.assertEqual(self.probe_route_calls, ['physical'])
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])

    def test_explicit_system_mode_marks_direct_as_unverified(self):
        output = self.install(self.args('--apply', '--test-route', 'system'))
        self.assertEqual(self.probe_route_calls, ['system'])
        self.assertIn('через текущее подключение', output)
        self.assertNotIn('Готово:', output)

    def test_export_failure_retracts_new_host_and_retry_can_publish_again(self):
        self.export_error = d.probe.ProbeError('Simulated missing exported host')
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertTrue((self.root / (IP+'-22') / 'complete.json').exists())
        self.assertFalse((self.root / (IP+'-22') / 'activation.verified.json').exists())
        self.export_error = None
        self.install(d.parser().parse_args(['--ip', IP, '--apply']))
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])
        self.assertTrue((self.root / (IP+'-22') / 'activation.verified.json').exists())

    def test_old_success_then_reinstall_export_failure_retries_publication(self):
        self.install(self.args('--apply'))
        self.api.data['hosts'][0]['isDisabled'] = True
        self.export_error = d.probe.ProbeError('Simulated missing export after reinstall')
        with self.assertRaises(d.u.DeployError):
            self.install(d.parser().parse_args(['--ip', IP, '--apply', '--reinstall']))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.export_error = None
        self.install(d.parser().parse_args(['--ip', IP, '--apply']))
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])

    def test_only_fixed_https_checks_retry_not_ssh_or_ownership_errors(self):
        ssh = d.SSH(self.plan())
        try:
            bad = types.SimpleNamespace(returncode=2, stdout=b'', stderr=b'Stopped: Chosen IPv4 HTTPS check failed\n')
            good = types.SimpleNamespace(returncode=0, stdout=b'{}', stderr=b'')
            with patch.object(d.subprocess, 'run', side_effect=[bad, good]) as call, patch.object(d.time, 'sleep'), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ssh.run('fixed-source-helper', capture=True, network_retries=2), b'{}')
                self.assertEqual(call.call_count, 2)
            bad.stderr = b'Stopped: Owned file changed\n'
            with patch.object(d.subprocess, 'run', return_value=bad) as call, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(d.u.DeployError):
                    ssh.run('fixed-source-helper', capture=True, network_retries=2)
                self.assertEqual(call.call_count, 1)
        finally:
            ssh.tmp.cleanup()

    def test_render_has_only_node_secret_not_panel_token(self):
        plan = self.plan()
        files = d.render_files(plan, 'service-key-12345678901234567890')
        text = b'\n'.join(files.values()).decode()
        self.assertIn(d.IMAGE, text)
        self.assertIn('hard: 65536', text)
        self.assertNotIn('REMNAWAVE_API_TOKEN', text)
        self.assertNotIn('CLOUDFLARE_API_TOKEN', text)
        self.assertNotIn('nginx', text.lower())
        self.assertNotIn('certbot', text.lower())
        self.assertEqual(set(files), {'managed-node.sh', '.spn-deployment', 'docker-compose.yml', 'node.env',
                                     'guard.py', 'haproxy-reality.cfg', 'core_fd_health.py',
                                     'node_guard.py', 'scanner_guard.py', 'scanner_lists.json', 'reality_sni_guard.py',
                                     'node_guard_upgrade.py'})

    def test_ssh_cannot_forward_cloudflare_token_from_local_environment(self):
        with patch.dict(os.environ, {'CLOUDFLARE_API_TOKEN': 'private-cloudflare-token'}):
            ssh = d.SSH(self.plan())
        try:
            with patch.object(d.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=b'')) as run:
                ssh.run('true')
            self.assertNotIn('CLOUDFLARE_API_TOKEN', run.call_args.kwargs['env'])
            self.assertNotIn('private-cloudflare-token', str(run.call_args))
        finally:
            ssh.tmp.cleanup()

    def test_ssh_management_route_can_differ_from_active_node_ip(self):
        active, connected = IP, '9.9.9.9'
        plan = {**self.plan(), 'ip': connected, 'nodeIp': active}
        ssh = d.SSH(plan)
        try:
            with patch.object(d.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=b'{}')) as run:
                ssh.action('verify', capture=True)
            command = run.call_args.args[0]
            self.assertEqual(command[-2], 'root@' + connected)
            remote = shlex.split(command[-1])
            self.assertEqual(remote[:3], ['bash', '/root/spn-reality-managed-' + plan['id'] + '/managed-node.sh', 'verify'])
            self.assertEqual(remote[3], active)
            self.assertEqual(remote[4:7], [PANEL, str(plan['sshPort']), plan['id']])
            self.assertNotIn(connected, remote)
        finally:
            ssh.tmp.cleanup()

    def test_trust_rejects_keyscan_without_expected_match(self):
        raw = b'example-ed25519-wire-key'
        key = base64.b64encode(raw).decode()
        response = types.SimpleNamespace(returncode=0, stdout=(IP+' ssh-ed25519 '+key+'\n').encode())
        with patch.object(d.subprocess, 'run', return_value=response):
            with self.assertRaises(d.u.DeployError):
                d.pinned_known_hosts(IP, 22, 'SHA256:'+'A'*43, self.store())

    def test_verified_fingerprint_creates_private_scoped_known_hosts(self):
        raw = b'example-ed25519-wire-key'
        key = base64.b64encode(raw).decode()
        fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
        response = types.SimpleNamespace(returncode=0, stdout=(IP+' ssh-ed25519 '+key+'\n').encode())
        with patch.object(d.subprocess, 'run', return_value=response):
            path = d.pinned_known_hosts(IP, 2201, fingerprint, self.store())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_text(), '['+IP+']:2201 ssh-ed25519 '+key+'\n')

    def test_state_namespaces_panel_ip_and_ssh_port(self):
        first = d.state_directory(BASE, IP, 22, self.root)
        self.assertNotEqual(first, d.state_directory(BASE, '8.8.8.8', 22, self.root))
        self.assertNotEqual(first, d.state_directory(BASE, IP, 2201, self.root))
        self.assertNotEqual(first, d.state_directory('https://other.example/api', IP, 22, self.root))

    def test_ssh_commands_do_not_ignore_host_keys_or_fallback(self):
        plan = self.plan()
        ssh = d.SSH(plan)
        try:
            with patch.object(d.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=b'')) as run:
                ssh.run('true')
                args = run.call_args.args[0]
                self.assertIn('ProxyCommand=/usr/bin/false', args)
                self.assertIn('BatchMode=yes', args)
                self.assertNotIn('StrictHostKeyChecking=no', args)
        finally:
            ssh.tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
