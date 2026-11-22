"""Offline replacement regressions: fake panel, guest, DNS and traffic only."""
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
spec = importlib.util.spec_from_file_location('reality_replace_under_test', HERE / 'replace.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
d = r.d
IP, NEW, NEXT = '8.8.4.4', '9.9.9.9', '8.8.8.8'
PANEL, BASE = '1.1.1.1', 'https://panel.example.com/api'
DOMAIN = 'n-a1b2c3.wayspn.com'
SQUAD = '11111111-1111-4111-8111-111111111111'


class FakeAPI:
    """Actions/PATCH can lose their response AFTER applying the exact write."""
    def __init__(self):
        self.data = {'config-profiles': [], 'nodes': [], 'hosts': [], 'internal-squads': [
            {'uuid': SQUAD, 'name': 'regular', 'inbounds': [{'uuid': 'other-inbound'}]}]}
        self.calls, self.sequence, self.lose = [], 0, None

    def rows(self, kind):
        return copy.deepcopy(self.data[kind])

    def action(self, uid, action):
        node = next(row for row in self.data['nodes'] if row['uuid'] == uid)
        self.calls.append(('POST', '/nodes/' + uid + '/actions/' + action, None))
        node['isDisabled'] = action == 'disable'
        if self.lose == action:
            self.lose = None
            raise d.u.DeployError('Simulated lost action response')
        return copy.deepcopy(node)

    def disable_node(self, uid):
        return self.action(uid, 'disable')

    def enable_node(self, uid):
        return self.action(uid, 'enable')

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        kind = path.removeprefix('/')
        if method == 'POST':
            self.sequence += 1
            row = {'uuid': f'00000000-0000-4000-8000-{self.sequence:012d}', **copy.deepcopy(body)}
            if kind == 'config-profiles':
                row['inbounds'] = [{'uuid': 'inbound-' + row['uuid'], 'tag': inbound['tag']}
                                   for inbound in row['config']['inbounds']]
            if kind == 'nodes':
                row.update(isConnected=True, isDisabled=False, lastStatusMessage=None)
            self.data[kind].append(row)
            return copy.deepcopy(row)
        if method == 'PATCH':
            row = next(item for item in self.data[kind] if item['uuid'] == body['uuid'])
            row.update(copy.deepcopy(body))
            if self.lose == 'PATCH':
                self.lose = None
                raise d.u.DeployError('Simulated lost PATCH response')
            return copy.deepcopy(row)
        raise AssertionError('Unexpected fake API operation: ' + method + ' ' + path)


class FakeSSH:
    calls = []

    def __init__(self, plan, known_hosts=None, control_socket=None):
        self.plan = plan

    def __enter__(self):
        self.calls.append(('connect', self.plan['ip']))
        return self

    def __exit__(self, *args):
        pass

    def upload(self, files):
        self.calls.append(('upload', self.plan['ip'], set(files)))
        if 'node.env' in files:
            assert b'REMNAWAVE_API_TOKEN' not in files['node.env']
            assert b'CLOUDFLARE_API_TOKEN' not in files['node.env']

    def run(self, command, data=None, capture=False, **kwargs):
        self.calls.append(('run', self.plan['ip']))
        return b''

    def action(self, action, capture=False):
        self.calls.append(('action', self.plan['ip'], action))
        if action == 'identity':
            return json.dumps({'machineId': 'a' * 32, 'sshFingerprint': 'SHA256:' + 'A' * 43,
                               'ownedInstallation': False}).encode()

    def source(self, action, capture=False):
        self.calls.append(('source', self.plan['ip'], action))
        if action == 'check':
            return json.dumps({'needsChange': False, 'currentSource': self.plan['ip']}).encode()

    def assigned_ipv4(self):
        return {self.plan['ip']}

    @contextlib.contextmanager
    def test_forward(self):
        yield 19987


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.store = d.u.Store(self.home / '.spn-reality-deploy' / d.u.digest(BASE)[:12] / (IP + '-ssh22'))
        self.api = FakeAPI()
        self.store.save('reality.private.json', {'private': 'a' * 43, 'public': 'b' * 43, 'short': '0123456789abcdef'})
        args = d.parser().parse_args(['--ip', IP, '--domain', DOMAIN, '--name', 'Финляндия', '--country', 'FI',
                                     '--squad', 'regular', '--transport', getattr(self, 'transport', 'reality')])
        self.plan = d.prepare(self.api, self.store, args, BASE, PANEL)
        deployment = d.Deployment(self.api, self.store, self.plan)
        self.profile, self.node, self.inbound = deployment.provision()
        deployment.attach(self.inbound)
        self.host = deployment.host(self.profile, self.node, self.inbound)
        deployment.publish(self.host)
        self.store.save('node-secret.json', {'value': 'preserved-node-service-secret-123456789'})
        self.store.save('activation.verified.json', {'host': self.host['uuid'], 'code': self.plan['code'],
                                                      'trafficAndExportVerified': True})
        dns_intent = {'version': 1, 'zone_id': 'zone123', 'name': DOMAIN, 'type': 'A', 'content': IP,
                      'proxied': False, 'ttl': 60, 'comment': 'SPN universal-reality:' + 'a' * 32}
        self.store.save('dns.intent.json', dns_intent)
        self.store.save('dns.receipt.json', {**{key: value for key, value in dns_intent.items() if key != 'version'},
                                             'id': 'record123'})
        self.api.calls.clear()
        self.dns_ip, self.dns_calls, self.probe_calls = IP, [], []
        self.probe_options, self.export_options = [], []
        self.proof_passed, self.export_passed, self.dns_error = True, True, None
        self.dns_wait_error = None
        self.guard_calls, self.sni_calls = [], []
        self.guard_error, self.guard_fail_phase, self.sni_error = None, None, None
        FakeSSH.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return r.topology(self.api, self.store, self.plan)

    def start(self, ip=NEW):
        return r.start_operation(self.store, self.plan, ip, self.snapshot())

    def apply(self, ip=NEW, route='system', extra=()):
        owner = self

        class FakeDNS:
            def __init__(self, token):
                pass

            def find_zone(self, name):
                return {'id': 'zone123', 'name': name, 'name_servers': ['alice.ns.cloudflare.com']}

            def check_update(self, operation, *, receipt, old_ipv4, new_ipv4):
                owner.dns_calls.append(('check', old_ipv4, new_ipv4))
                if owner.dns_error:
                    raise owner.dns_error
                if owner.dns_ip not in (old_ipv4, new_ipv4):
                    raise d.dns.DNSConflict('Fake record has foreign content')
                return {**receipt, 'content': owner.dns_ip}

            def update_record(self, operation, *, receipt, old_ipv4, new_ipv4):
                owner.dns_calls.append(('update', old_ipv4, new_ipv4))
                before, after = {**receipt, 'content': old_ipv4}, {**receipt, 'content': new_ipv4}
                intent = {'version': 1, 'operation_id': 'b' * 32, 'before': before, 'after': after}
                pending = {'version': 1, 'operation_id': 'b' * 32}
                operation.save(d.dns.UPDATE_INTENT, intent)
                operation.save(d.dns.UPDATE_PENDING, pending)
                owner.dns_ip = new_ipv4
                operation.save(d.dns.UPDATE_RECEIPT, {**pending, 'record': after})
                return after

        def traffic(*args, **kwargs):
            self.probe_calls.append((args[0], kwargs.get('route')))
            self.probe_options.append(kwargs)
            test_route = kwargs.get('route', 'physical')
            return {'passed': self.proof_passed, 'tests': 9, 'ip': args[0], 'testRoute': test_route,
                    'physicalSourceObserved': test_route == 'physical',
                    'directReachabilityVerified': test_route == 'physical',
                    'results': [{'passed': self.proof_passed, 'clientAlive': True} for _ in range(9)]}

        def verify_export(*args, **kwargs):
            self.export_options.append(kwargs)
            if no_vpn:
                raise no_vpn
            return {'passed': self.export_passed, 'endpointAddress': DOMAIN}

        def node_guard(ssh, active_ip, *, old_ip=None, verify=False, **kwargs):
            self.guard_calls.append((active_ip, old_ip, verify))
            FakeSSH.calls.append(('security-guard', active_ip, verify))
            phase = 'verify' if verify else 'install'
            if self.guard_error and self.guard_fail_phase in (None, phase):
                raise self.guard_error
            return {'activeIp': active_ip, 'deployment': ssh.plan['id'], 'reserveVpnBlocked': True,
                    'scannerFilterEnabled': True, 'bootPersistenceVerified': True}

        def sni_guard(ssh, *, verify=False):
            self.sni_calls.append((ssh.plan['ip'], verify))
            FakeSSH.calls.append(('security-sni', ssh.plan['ip'], verify))
            if self.sni_error:
                raise self.sni_error
            return {'sniFiltered': True, 'allowedSni': ssh.plan.get('sni', d.SNI), 'tlsTerminated': False,
                    'validSniHandshakeVerified': True, 'unknownSniRejected': True, 'missingSniRejected': True}

        args = r.parser().parse_args(['--domain', DOMAIN, '--ip', ip, '--state-dir', str(self.store.path),
                                      '--panel-url', BASE, '--test-route', route, '--apply', *extra])
        no_vpn = AssertionError('SSH-only must not invoke VPN preflight or export') if '--ssh-only' in extra else None
        with patch.object(d, 'API', return_value=self.api), \
                patch.object(d.u, 'read_settings', return_value={'REMNAWAVE_BASE_URL': BASE,
                              'REMNAWAVE_API_TOKEN': 'fake-panel-token'}), \
                patch.object(d, 'cloudflare_token', return_value='fake-cloudflare-token'), \
                patch.object(d.dns, 'CloudflareDNS', FakeDNS), \
                patch.object(d, 'panel_ipv4', return_value=PANEL), \
                patch.object(d, 'SSH', FakeSSH), \
                patch.object(d.trust, 'resolve', return_value=self.home / 'known-hosts'), \
                patch.object(d.probe, 'private_file', return_value=self.home / 'subscription', side_effect=no_vpn), \
                patch.object(d.probe, 'subscription_url', return_value='https://sub.example.com/private'), \
                patch.object(d.probe, 'select_engine', return_value=self.home / 'xray', side_effect=no_vpn), \
                patch.object(d.probe, 'validate_interface', side_effect=no_vpn or d.probe.validate_interface), \
                patch.object(d.probe, 'run', side_effect=traffic), \
                patch.object(d.probe, 'verify_export', side_effect=verify_export), \
                patch.object(d.security, 'guard', side_effect=node_guard), \
                patch.object(d.security, 'sni', side_effect=sni_guard), \
                patch.object(d, 'verify_authoritative_dns'), \
                patch.object(d, '_dig', side_effect=lambda server, domain, kind, **kw: [self.dns_ip] if kind == 'A' else []), \
                patch.object(r, '_wait_dns', side_effect=self.dns_wait_error), \
                patch.object(d.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            r.run(args)
        return output.getvalue()

    def test_plan_has_no_network_or_state_access(self):
        args = r.parser().parse_args(['--domain', DOMAIN, '--ip', NEW])
        with patch.object(r, 'find_original', side_effect=AssertionError('state access')), \
                patch.object(d, 'API', side_effect=AssertionError('network access')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            r.run(args)
        plan = json.loads(output.getvalue())
        self.assertFalse(plan['changes'])
        self.assertIn(DOMAIN, output.getvalue())
        self.assertIn(NEW, output.getvalue())

    def test_exact_domain_lookup_rejects_ambiguous_saved_plans(self):
        found = r.find_original(BASE, DOMAIN, home=self.home)
        self.assertEqual(found.path, self.store.path)
        duplicate = d.u.Store(self.store.path.parent / 'duplicate')
        duplicate.save('plan.json', self.plan)
        with self.assertRaises(d.u.DeployError):
            r.find_original(BASE, DOMAIN, home=self.home)

    def test_explicit_state_does_not_adopt_another_domain(self):
        with self.assertRaises(d.u.DeployError):
            r.find_original(BASE, 'foreign.wayspn.com', state_dir=self.store.path, home=self.home)

    def test_original_ownership_is_required_without_mutations(self):
        self.assertEqual(r.validate_original(self.store, DOMAIN, BASE), self.plan)
        (self.store.path / 'activation.verified.json').unlink()
        with self.assertRaises(d.u.DeployError):
            r.validate_original(self.store, DOMAIN, BASE)
        self.assertEqual(self.api.calls, [])

    def test_original_plan_keys_and_dns_receipt_remain_immutable(self):
        before = {path.name: path.read_bytes() for path in self.store.path.iterdir() if path.is_file()}
        operation, intent = self.start()
        self.assertEqual(intent['fromIp'], IP)
        self.assertEqual(intent['toIp'], NEW)
        self.assertEqual(operation.path.parent, self.store.path / 'replacements')
        for name, content in before.items():
            self.assertEqual((self.store.path / name).read_bytes(), content)
        self.assertIsNone(operation.load('dns.intent.json'))
        self.assertIsNone(operation.load('dns.receipt.json'))

    def test_pending_operation_resumes_only_same_destination(self):
        operation, intent = self.start()
        resumed, again = self.start()
        self.assertEqual(resumed.path, operation.path)
        self.assertEqual(again, intent)
        with self.assertRaises(d.u.DeployError):
            self.start(NEXT)

    def test_empty_last_operation_directory_is_recovered_after_local_crash(self):
        replacements = d.u.Store(self.store.path / 'replacements')
        empty = d.u.Store(replacements.path / '000001')
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(commits, [])
        self.assertEqual(pending[0].path, empty.path)
        self.assertIsNone(pending[1])
        operation, intent = self.start()
        self.assertEqual(operation.path, empty.path)
        self.assertEqual(intent['generation'], 1)

    def test_second_pending_operation_is_not_silently_selected(self):
        self.start()
        d.u.Store(self.store.path / 'replacements' / '000002')
        with self.assertRaises(d.u.DeployError):
            r.chain(self.store, self.plan)

    def test_forged_commit_cannot_advance_current_ip_without_proof_markers(self):
        operation, intent = self.start()
        commit = {key: intent[key] for key in ('format', 'generation', 'domain', 'fromIp', 'toIp', 'previousCommitHash')}
        commit.update(intentHash=d.u.digest(intent), trafficAndExportVerified=True,
                      testRoute='physical', directReachabilityVerified=True)
        operation.save('commit.json', commit)
        with self.assertRaises(d.u.DeployError):
            r.chain(self.store, self.plan)

    def test_finish_refuses_incomplete_proof_stages(self):
        operation, intent = self.start()
        with self.assertRaises(d.u.DeployError):
            r.finish_operation(operation, intent, {'testRoute': 'physical', 'directReachabilityVerified': True})
        self.assertIsNone(operation.load('commit.json'))

    def test_shared_profile_is_rejected_read_only(self):
        foreign = copy.deepcopy(self.api.data['nodes'][0])
        foreign.update(uuid='22222222-2222-4222-8222-222222222222', address='other.wayspn.com')
        self.api.data['nodes'].append(foreign)
        with self.assertRaises(d.u.DeployError):
            self.snapshot()
        self.assertEqual(self.api.calls, [])

    def test_profile_key_drift_is_rejected_read_only(self):
        self.api.data['config-profiles'][0]['config']['inbounds'][0]['streamSettings']['realitySettings']['privateKey'] = 'x' * 43
        with self.assertRaises(d.u.DeployError):
            self.snapshot()
        self.assertEqual(self.api.calls, [])

    def test_host_endpoint_drift_is_rejected_read_only(self):
        self.api.data['hosts'][0]['address'] = 'foreign.wayspn.com'
        with self.assertRaises(d.u.DeployError):
            self.snapshot()
        self.assertEqual(self.api.calls, [])

    def test_pending_accepts_unrelated_inbound_added_to_existing_audience_squad(self):
        operation, intent = self.start()
        squad = next(row for row in self.api.data['internal-squads']
                     if self.inbound in d.u.ids(row['inbounds']))
        squad['inbounds'].append({'uuid': 'unrelated-new-server-inbound'})
        before = copy.deepcopy(self.api.data['internal-squads'])
        snapshot = r.topology(self.api, self.store, self.plan, (operation, intent))
        self.assertEqual(snapshot['squads'], intent['snapshot']['squads'])
        self.assertEqual(self.api.data['internal-squads'], before)
        self.assertEqual(self.api.calls, [])

    def test_pending_rejects_removal_of_own_inbound_from_audience_squad(self):
        operation, intent = self.start()
        squad = next(row for row in self.api.data['internal-squads']
                     if self.inbound in d.u.ids(row['inbounds']))
        squad['inbounds'] = [row for row in squad['inbounds']
                             if (row['uuid'] if isinstance(row, dict) else row) != self.inbound]
        before = copy.deepcopy(self.api.data['internal-squads'])
        with self.assertRaises(d.u.DeployError):
            r.topology(self.api, self.store, self.plan, (operation, intent))
        self.assertEqual(self.api.data['internal-squads'], before)
        self.assertEqual(self.api.calls, [])

    def test_intentionally_disabled_objects_are_not_enabled(self):
        for kind in ('nodes', 'hosts'):
            with self.subTest(kind=kind):
                self.api.data[kind][0]['isDisabled'] = True
                with self.assertRaises(d.u.DeployError):
                    self.snapshot()
                self.api.data[kind][0]['isDisabled'] = False
        self.assertEqual(self.api.calls, [])

    def test_route_changes_only_owned_node_address_and_preserves_names(self):
        node = self.api.data['nodes'][0]
        host = self.api.data['hosts'][0]
        node.update(name='Переименованная нода', note='User note', tags=['CUSTOM', 'AUTO_REALITY'])
        host.update(remark='Мой прежний хост', tags=['CUSTOM_HOST'])
        before = copy.deepcopy(self.api.data)
        operation, intent = self.start()
        r.route_node(self.api, operation, intent, 'new', NEW)
        self.assertEqual(self.api.data['nodes'][0]['address'], NEW)
        r.route_node(self.api, operation, intent, 'domain', DOMAIN)
        self.assertEqual(self.api.data, before)
        writes = [call for call in self.api.calls if call[0] == 'PATCH']
        self.assertEqual(writes, [('PATCH', '/nodes', {'uuid': node['uuid'], 'address': NEW}),
                                  ('PATCH', '/nodes', {'uuid': node['uuid'], 'address': DOMAIN})])
        self.assertTrue(all(path.startswith('/nodes/') or path == '/nodes'
                            for _, path, _ in self.api.calls))

    def test_lost_disable_response_resumes_own_disabled_node(self):
        operation, intent = self.start()
        self.api.lose = 'disable'
        with self.assertRaises(d.u.DeployError):
            r.route_node(self.api, operation, intent, 'new', NEW)
        self.assertTrue(self.api.data['nodes'][0]['isDisabled'])
        self.assertIsNotNone(operation.load('node-new.intent.json'))
        r.topology(self.api, self.store, self.plan, (operation, intent))
        r.route_node(self.api, operation, intent, 'new', NEW)
        self.assertFalse(self.api.data['nodes'][0]['isDisabled'])
        self.assertEqual(self.api.data['nodes'][0]['address'], NEW)

    def test_lost_address_patch_response_does_not_repeat_applied_patch(self):
        operation, intent = self.start()
        self.api.lose = 'PATCH'
        with self.assertRaises(d.u.DeployError):
            r.route_node(self.api, operation, intent, 'new', NEW)
        self.assertEqual(self.api.data['nodes'][0]['address'], NEW)
        self.assertTrue(self.api.data['nodes'][0]['isDisabled'])
        r.route_node(self.api, operation, intent, 'new', NEW)
        patches = [call for call in self.api.calls if call[0] == 'PATCH']
        self.assertEqual(len(patches), 1)
        self.assertFalse(self.api.data['nodes'][0]['isDisabled'])

    def test_pending_does_not_authorize_foreign_numeric_address(self):
        operation, intent = self.start()
        foreign = copy.deepcopy(self.api.data['nodes'][0])
        foreign.update(uuid='22222222-2222-4222-8222-222222222222', address=NEW,
                       configProfile={'activeConfigProfileUuid': 'foreign-profile', 'activeInbounds': ['foreign-inbound']})
        self.api.data['nodes'].append(foreign)
        with self.assertRaises(d.u.DeployError):
            r.topology(self.api, self.store, self.plan, (operation, intent))
        self.assertEqual(self.api.calls, [])

    def test_two_replacements_keep_all_objects_and_follow_committed_ip(self):
        before = copy.deepcopy(self.api.data)
        original_files = {path.name: path.read_bytes() for path in self.store.path.iterdir() if path.is_file()}
        self.apply(NEW)
        self.apply(NEXT)
        self.assertEqual(self.api.data, before)
        self.assertEqual(self.probe_calls, [(NEW, 'system'), (NEXT, 'system')])
        self.assertEqual([call for call in self.dns_calls if call[0] == 'update'],
                         [('update', IP, NEW), ('update', NEW, NEXT)])
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(len(commits), 2)
        self.assertIsNone(pending)
        self.assertEqual(commits[-1]['toIp'], NEXT)
        for name, content in original_files.items():
            self.assertEqual((self.store.path / name).read_bytes(), content)
        self.assertTrue(all(path == '/nodes' or path.startswith('/nodes/')
                            for _, path, _ in self.api.calls))

    def test_failed_vpn_leaves_pending_without_dns_cutover_then_resumes(self):
        self.proof_passed = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(self.dns_ip, IP)
        self.assertFalse(any(call[0] == 'update' for call in self.dns_calls))
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(commits, [])
        self.assertIsNotNone(pending)
        operation_path = pending[0].path
        self.proof_passed = True
        self.apply()
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(len(commits), 1)
        self.assertIsNone(pending)
        self.assertEqual(list((self.store.path / 'replacements').iterdir()), [operation_path])

    def test_dns_conflict_stops_before_any_guest_or_panel_mutations(self):
        self.dns_error = d.dns.DNSConflict('Simulated foreign DNS record')
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(FakeSSH.calls, [])
        self.assertEqual(self.api.calls, [])
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(commits, [])
        self.assertIsNotNone(pending)

    def test_export_failure_does_not_commit_or_recreate_panel_objects(self):
        before = copy.deepcopy(self.api.data)
        self.export_passed = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(self.api.data, before)
        self.assertEqual(self.dns_ip, NEW)
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(commits, [])
        self.assertIsNotNone(pending)
        self.export_passed = True
        self.apply()
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(len(commits), 1)
        self.assertIsNone(pending)

    def test_dns_ttl_timeout_keeps_numeric_node_and_resumable_pending(self):
        self.dns_wait_error = d.u.DeployError('Old DNS TTL has not elapsed')
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(self.dns_ip, NEW)
        self.assertEqual(self.api.data['nodes'][0]['address'], NEW)
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(commits, [])
        self.assertIsNotNone(pending)
        self.dns_wait_error = None
        self.apply()
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(len(commits), 1)
        self.assertIsNone(pending)

    def test_dns_wait_honors_old_ttl_even_with_fresh_recursive_answers(self):
        now, sleeps = [100.0], []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        with patch.object(r.time, 'time', side_effect=lambda: now[0]), \
                patch.object(r.time, 'monotonic', side_effect=lambda: now[0] - 100), \
                patch.object(r.time, 'sleep', side_effect=sleep), \
                patch.object(d, 'verify_authoritative_dns'), \
                patch.object(d, '_dig', side_effect=lambda server, domain, kind, **kw: [NEW] if kind == 'A' else []):
            r._wait_dns({**self.plan, 'ip': NEW}, {'id': 'zone123'}, 10, not_before=106)
        self.assertGreaterEqual(now[0], 106)
        self.assertGreaterEqual(sum(sleeps), 6)

    def test_dns_wait_timeout_does_not_ignore_unexpired_old_ttl(self):
        now = [100.0]

        def sleep(seconds):
            now[0] += seconds

        with patch.object(r.time, 'time', side_effect=lambda: now[0]), \
                patch.object(r.time, 'monotonic', side_effect=lambda: now[0] - 100), \
                patch.object(r.time, 'sleep', side_effect=sleep), \
                patch.object(d, 'verify_authoritative_dns'), \
                patch.object(d, '_dig', side_effect=lambda server, domain, kind, **kw: [NEW] if kind == 'A' else []):
            with self.assertRaises(d.u.DeployError):
                r._wait_dns({**self.plan, 'ip': NEW}, {'id': 'zone123'}, 5, not_before=160)
        self.assertLess(now[0], 160)


if __name__ == '__main__':
    unittest.main()
