"""Cascade orchestration regression tests. All remote effects are fake."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


d = load('cascade_deploy_tests', HERE / 'deploy.py')
support = load('cascade_api_fixture', HERE.parent / 'universal-reality' / 'test_deploy.py')
RU, EU, PANEL = '8.8.4.4', '9.9.9.9', '1.1.1.1'
BASE = 'https://panel.example.com/api'


class FakeSSH:
    calls = []
    uploaded = []
    fail_preflight = None
    same_machine = False
    same_hostkey = False
    inventory = []

    def __init__(self, plan, role, known_hosts=None):
        self.cascade, self.role = plan, role

    def __enter__(self):
        self.calls.append(('connect', self.role))
        return self

    def __exit__(self, *args):
        self.calls.append(('close', self.role))

    def upload(self, files):
        self.calls.append(('upload', self.role))
        self.uploaded.append((self.role, copy.deepcopy(files)))

    def action(self, action, capture=False):
        self.calls.append((action, self.role))
        if action == 'inventory':
            return json.dumps(self.inventory).encode()
        if action == 'install' and self.role == 'ru':
            p = self.cascade
            row = {'id': p['id'], 'entryPort': d.c.entry_port(p), 'ruWGPort': d.c.ru_wg_port(p),
                   'localWG': p['link']['ru'], 'peerWG': p['link']['eu'], 'interface': p['link']['interface'],
                   'ruIP': p['ru']['ip'], 'euIP': p['eu']['ip']}
            if not any(item['id'] == p['id'] for item in self.inventory):
                self.inventory.append(row)
        if action == 'preflight' and self.role == self.fail_preflight:
            raise d.u.DeployError('Not a clean guest')
        if action == 'identity':
            number = 'a' if self.role == 'ru' or self.same_machine else 'b'
            key = 'A' if self.role == 'ru' or self.same_hostkey else 'B'
            return json.dumps({'machineId': number * 32, 'sshFingerprint': 'SHA256:' + key * 43,
                               'ownedInstallation': False}).encode()

    def source(self, action, capture=False):
        self.calls.append(('source-' + action, self.role))

    def assigned_ipv4(self):
        return {self.cascade[self.role]['ip']}


class CascadeDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.api = support.FakeAPI()
        self.dns_calls = []
        self.proofs = []
        self.passed, self.exit_ip = True, EU
        self.export_passed = True
        FakeSSH.calls, FakeSSH.uploaded = [], []
        FakeSSH.fail_preflight, FakeSSH.same_machine, FakeSSH.same_hostkey = None, False, False
        FakeSSH.inventory = []

    def args(self, *extra):
        return d.parser().parse_args(['--ru-ip', RU, '--eu-ip', EU, '--name', 'Каскад', '--country', 'NL',
                                     '--squad', 'regular', '--state-dir', str(self.root / 'state'), *extra])

    def store(self):
        return d.u.Store(self.root / 'state')

    def proof(self, ip, public, short, subscription, **kw):
        self.proofs.append((ip, kw))
        return {'passed': self.passed, 'ip': ip, 'port': kw.get('public_port', 443),
                'expectedExitIp': kw.get('expected_exit_ip') if self.exit_ip == EU else self.exit_ip,
                'relayPathVerified': self.passed,
                'testRoute': kw['route'], 'physicalSourceObserved': kw['route'] == 'physical',
                'directReachabilityVerified': self.passed and kw['route'] == 'physical', 'tests': 9,
                'results': [{'passed': self.passed, 'clientAlive': True} for _ in range(9)]}

    def install(self, *extra):
        def preflight(store, plan, args, settings):
            self.dns_calls.append(('check', plan['domain'], plan['ru']['ip']))
            return ('client', 'zone')

        def ensure(store, plan, context, wait):
            self.dns_calls.append(('ensure', plan['domain'], plan['ip']))
            self.assertEqual(plan['ip'], RU)

        def exported(*args, **kw):
            self.assertEqual(args[0], RU)
            self.assertNotEqual(kw['endpoint_address'], EU)
            if not self.export_passed:
                raise d.r.probe.ProbeError('Export rejected')
            return {'passed': True, 'endpointAddress': kw['endpoint_address']}

        with patch.object(d.u, 'read_settings', return_value={'REMNAWAVE_BASE_URL': BASE, 'REMNAWAVE_API_TOKEN': 'panel-test-token'}), \
                patch.object(d.r, 'API', return_value=self.api), patch.object(d.r, 'panel_ipv4', return_value=PANEL), \
                patch.object(d, 'SSH', FakeSSH), patch.object(d.r.trust, 'resolve', return_value=self.root / 'known'), \
                patch.object(d.r.probe, 'private_file', return_value=self.root / 'subscription'), \
                patch.object(d.r.probe, 'subscription_url', return_value='https://sub.example.com/private'), \
                patch.object(d.r.probe, 'select_engine', return_value=self.root / 'engine'), \
                patch.object(d.r.probe, 'run', side_effect=self.proof), \
                patch.object(d.r.probe, 'verify_export', side_effect=exported), \
                patch.object(d, 'dns_preflight', side_effect=preflight), patch.object(d.r, 'ensure_dns', side_effect=ensure), \
                patch.object(d.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            d.run(self.args('--apply', *extra))
        return output.getvalue()

    def test_no_apply_does_not_read_or_mutate_anything(self):
        with patch.object(d.u, 'Store', side_effect=AssertionError('state accessed')), \
                patch.object(d.r, 'API', side_effect=AssertionError('network')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            d.run(self.args())
        self.assertFalse(json.loads(out.getvalue())['changes'])
        self.assertFalse((self.root / 'state').exists())

    def test_validates_two_different_public_guests_and_reserved_ports(self):
        for extra in (['--eu-ip', RU], ['--ru-ip', '127.0.0.1'], ['--eu-ssh-port', '2443'], ['--ru-ssh-port', '443']):
            with self.subTest(extra=extra), self.assertRaises(d.u.DeployError):
                d.validate(self.args(*extra))

    def test_remote_profiles_are_private_but_host_and_dns_only_ru(self):
        self.install()
        profile, node, host = (self.api.data[k][0] for k in ('config-profiles', 'nodes', 'hosts'))
        plan = self.store().load('plan.json')
        self.assertEqual(profile['config']['inbounds'][0]['listen'], plan['link']['eu'])
        self.assertEqual(profile['config']['inbounds'][0]['port'], 2443)
        self.assertEqual(node['address'], EU)
        self.assertEqual(host['address'], plan['domain'])
        self.assertEqual(host['port'], 443)
        self.assertNotIn(EU, json.dumps(host))
        self.assertFalse(host['isDisabled'])
        self.assertTrue(all(row[2] == RU for row in self.dns_calls))
        self.assertTrue(self.store().load('activation.verified.json')['trafficAndExportVerified'])

    def test_both_guests_preflight_and_distinct_identity_before_source_or_panel_mutation(self):
        self.install()
        calls = FakeSSH.calls
        first_source = min(i for i, call in enumerate(calls) if call[0] == 'source-apply')
        self.assertTrue(all(calls.index((action, role)) < first_source
                            for role in ('ru', 'eu') for action in ('preflight', 'identity')))
        for role in ('ru', 'eu'):
            first_upload = next(files for target, files in FakeSSH.uploaded if target == role)
            self.assertIn('host_checks.py', first_upload)

    def test_one_bad_guest_never_changes_the_other_or_creates_panel_objects(self):
        FakeSSH.fail_preflight = 'eu'
        with self.assertRaises(d.u.DeployError):
            self.install()
        self.assertEqual(self.api.calls, [])
        self.assertFalse(any(call[0] in ('install', 'source-apply') for call in FakeSSH.calls))

    def test_same_machine_or_ssh_key_rejected_before_mutations(self):
        for attribute in ('same_machine', 'same_hostkey'):
            with self.subTest(attribute=attribute):
                setattr(FakeSSH, attribute, True)
                with self.assertRaises(d.u.DeployError):
                    self.install()
                self.assertEqual(self.api.calls, [])
                self.assertFalse(any(call[0] == 'source-apply' for call in FakeSSH.calls))
                setattr(FakeSSH, attribute, False)

    def test_ru_never_receives_real_node_secret_or_eu_wireguard_private_key(self):
        self.install()
        state = self.store()
        keys, wgkeys = state.load('reality.private.json'), state.load('wireguard.private.json')
        node_secret = state.load('node-secret.json')['value']
        payloads = [files for role, files in FakeSSH.uploaded if role == 'ru']
        for files in payloads:
            combined = b'\n'.join(files.values())
            for private in (keys['private'], wgkeys['eu']['private'], node_secret, 'panel-test-token'):
                self.assertNotIn(private.encode(), combined)
            self.assertNotIn('node.env', files)
            self.assertNotIn('docker-compose.yml', files)

    def test_real_traffic_proof_requires_ru_ingress_and_eu_exit_not_ru_exit(self):
        self.exit_ip = RU
        with self.assertRaises(d.u.DeployError):
            self.install()
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertIsNone(self.store().load('activation.verified.json'))
        self.assertEqual(self.proofs[0][0], RU)
        self.assertEqual(self.proofs[0][1]['expected_exit_ip'], EU)

    def test_failed_real_vpn_never_publishes_and_rerun_resumes_without_new_objects(self):
        self.passed = False
        with self.assertRaises(d.u.DeployError):
            self.install()
        before_ids = {kind: [row['uuid'] for row in self.api.data[kind]] for kind in ('config-profiles', 'nodes', 'hosts')}
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.passed = True
        self.install()
        self.assertEqual(before_ids, {kind: [row['uuid'] for row in self.api.data[kind]] for kind in before_ids})

    def test_export_failure_retracts_just_enabled_host_and_resume_keeps_keys(self):
        self.export_passed = False
        with self.assertRaises(d.u.DeployError):
            self.install()
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        old_keys = self.store().load('reality.private.json')
        self.export_passed = True
        self.install()
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(old_keys, self.store().load('reality.private.json'))

    def test_owner_disabled_host_is_not_reenabled_on_successful_rerun(self):
        self.install()
        self.api.data['hosts'][0]['isDisabled'] = True
        output = self.install()
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertIn('оставлен выключенным', output)

    def test_explicit_system_route_is_not_mislabelled_direct(self):
        output = self.install('--test-route', 'system')
        self.assertIn('прямой доступ из сети клиента НЕ подтверждён', output)

    def test_dns_conflict_leaves_no_wrong_domain_plan_and_no_guest_or_panel_writes(self):
        with patch.object(d.u, 'read_settings', return_value={'REMNAWAVE_BASE_URL': BASE, 'REMNAWAVE_API_TOKEN': 'test-token'}), \
                patch.object(d.r, 'API', return_value=self.api), patch.object(d.r, 'panel_ipv4', return_value=PANEL), \
                patch.object(d.r.probe, 'private_file', return_value=self.root / 'subscription'), \
                patch.object(d.r.probe, 'subscription_url', return_value='https://sub.example.com/private'), \
                patch.object(d.r.probe, 'select_engine', return_value=self.root / 'engine'), \
                patch.object(d, 'dns_preflight', side_effect=d.u.DeployError('Unowned DNS record')), \
                patch.object(d, 'SSH', side_effect=AssertionError('SSH called')), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(d.u.DeployError):
                d.run(self.args('--apply', '--domain', 'taken.wayspn.com'))
        self.assertIsNone(self.store().load('plan.json'))
        self.assertEqual(self.api.calls, [])

    def test_existing_node_at_eu_is_not_adopted_or_overwritten(self):
        self.api.data['nodes'].append({'uuid': 'foreign', 'address': EU})
        with self.assertRaises(d.u.DeployError):
            d.prepare(self.api, self.store(), self.args(), BASE, PANEL)
        self.assertEqual(self.api.calls, [])

    def test_source_helper_state_basename_uses_supported_deployment_uuid(self):
        self.install()
        plan = self.store().load('plan.json')
        ssh = d.SSH(plan, 'ru', self.root / 'known')
        commands = []
        with patch.object(ssh, 'run', side_effect=lambda command, *a, **kw: commands.append(command)):
            ssh.source('apply')
        ssh.tmp.cleanup()
        command = shlex.split(commands[-1])
        state = Path(command[command.index('--state-dir') + 1])
        self.assertEqual(state.name, plan['id'])
        self.assertEqual(state.parent.name, 'spn-cascade-managed-' + plan['id'])

    def test_broad_local_ssh_config_cannot_forward_control_plane_secrets_or_agent(self):
        self.install()
        plan = self.store().load('plan.json')
        with patch.dict(os.environ, {'REMNAWAVE_API_TOKEN': 'private-panel-env',
                                     'CLOUDFLARE_API_TOKEN': 'private-dns-env', 'OTHER_SECRET': 'private-other-env'}):
            ssh = d.SSH(plan, 'ru', self.root / 'known')
        try:
            self.assertNotIn('REMNAWAVE_API_TOKEN', ssh.environment)
            self.assertNotIn('CLOUDFLARE_API_TOKEN', ssh.environment)
            self.assertNotIn('OTHER_SECRET', ssh.environment)
            self.assertIn('ForwardAgent=no', ssh.common)
            self.assertIn('SendEnv=-*', ssh.common)
        finally:
            ssh.tmp.cleanup()

    def test_second_eu_uses_same_ru_and_free_port_without_recreating_first_objects(self):
        self.install()
        first = copy.deepcopy(self.api.data)
        first_plan = self.store().load('plan.json')
        next_dir = self.root / 'second'
        self.install('--eu-ip', '8.8.8.8', '--state-dir', str(next_dir))
        second = d.u.Store(next_dir).load('plan.json')
        self.assertEqual(first_plan['entryPort'], 443)
        self.assertEqual(second['entryPort'], 8443)
        self.assertEqual(second['link']['ruPort'], 8443)
        self.assertEqual(first_plan['ru'], second['ru'])
        self.assertNotEqual(first_plan['link']['interface'], second['link']['interface'])
        for kind in ('config-profiles', 'nodes', 'hosts'):
            self.assertEqual(self.api.data[kind][0], first[kind][0])
            self.assertEqual(len(self.api.data[kind]), 2)
        self.assertEqual(self.api.data['hosts'][1]['port'], 8443)
        self.assertEqual(self.proofs[-1][1]['public_port'], 8443)

    def test_explicit_occupied_port_stops_before_plan_save_or_new_panel_objects(self):
        self.install()
        before = copy.deepcopy(self.api.data)
        next_dir = self.root / 'second'
        with self.assertRaises(d.u.DeployError):
            self.install('--eu-ip', '8.8.8.8', '--entry-port', '443', '--state-dir', str(next_dir))
        self.assertIsNone(d.u.Store(next_dir).load('plan.json'))
        self.assertEqual(self.api.data, before)
        # Correcting a premature port choice must not require deleting state.
        self.install('--eu-ip', '8.8.8.8', '--entry-port', '2053', '--state-dir', str(next_dir))
        self.assertEqual(d.u.Store(next_dir).load('plan.json')['entryPort'], 2053)

    def test_legacy_plan_keeps_443_and_51820_when_resumed(self):
        args = self.args()
        store = self.store()
        plan = d.prepare(self.api, store, args, BASE, PANEL)
        plan['format'] = 1
        plan['link']['port'] = 51820
        del plan['entryPort']
        del plan['link']['ruPort']
        store.save('plan.json', plan)
        FakeSSH.inventory = [{'id': plan['id'], 'entryPort': 443, 'ruWGPort': 51820,
                              'localWG': plan['link']['ru'], 'peerWG': plan['link']['eu']}]
        self.install()
        self.assertEqual(store.load('plan.json'), plan)
        self.assertEqual(self.api.data['hosts'][0]['port'], 443)

    def test_allocator_avoids_shared_ru_private_pair_collisions(self):
        plan = d.prepare(self.api, self.store(), self.args(), BASE, PANEL)
        old_link = copy.deepcopy(plan['link'])
        inventory = [{'id': 'f' * 32, 'entryPort': 443, 'ruWGPort': 443,
                      'localWG': old_link['ru'], 'peerWG': old_link['eu']}]
        d.allocate_ports(plan, inventory, self.args(), saved=False)
        self.assertNotEqual(plan['link']['ru'], old_link['ru'])
        self.assertNotEqual(plan['link']['eu'], old_link['eu'])

    def test_same_domain_is_allowed_on_different_port_but_not_duplicate_endpoint(self):
        self.install('--domain', 'shared.wayspn.com')
        next_dir = self.root / 'second'
        self.install('--eu-ip', '8.8.8.8', '--domain', 'shared.wayspn.com', '--state-dir', str(next_dir))
        self.assertEqual([row['address'] for row in self.api.data['hosts']], ['shared.wayspn.com'] * 2)
        self.assertEqual([row['port'] for row in self.api.data['hosts']], [443, 8443])

    def test_dns_shared_receipt_only_from_same_ru_panel_name_and_owned_state(self):
        self.install('--domain', 'shared.wayspn.com')
        source = self.store()
        plan = source.load('plan.json')
        intent = {'version': 1, 'zone_id': 'a' * 32, 'name': plan['domain'], 'type': 'A',
                  'content': RU, 'proxied': False, 'ttl': 60, 'comment': 'SPN universal-reality:' + 'b' * 32}
        source.save('dns.intent.json', intent)
        source.save('dns.receipt.json', {**{k: v for k, v in intent.items() if k != 'version'}, 'id': 'c' * 32})
        next_store = d.u.Store(self.root / 'second')
        next_plan = d.prepare(self.api, next_store,
                             self.args('--eu-ip', '8.8.8.8', '--state-dir', str(next_store.path)), BASE, PANEL)
        self.assertEqual(next_plan['domain'], plan['domain'])
        shared = d.shared_dns(next_plan, d.local_branches(next_store, BASE, plan['ru']))
        self.assertEqual(shared['dns.receipt.json']['id'], 'c' * 32)
        self.assertEqual(d.shared_dns({**next_plan, 'domain': 'foreign.wayspn.com'}, [(source, plan)]), {})

    def test_explicit_existing_port_policy_reaches_only_selected_role_and_is_saved(self):
        self.install('--ru-keep-tcp-port', '10050')
        saved = self.store().load('host-policy.json')
        self.assertEqual(saved['ru']['keepTcpPorts'], [10050])
        self.assertEqual(saved['eu']['keepTcpPorts'], [])
        for role in ('ru', 'eu'):
            files = next(files for target, files in FakeSSH.uploaded if target == role)
            self.assertEqual(json.loads(files['host-policy.json']), saved[role])
        self.install()  # Resume inherits without requiring the flag again.
        self.assertEqual(self.store().load('host-policy.json'), saved)

    def test_new_eu_inherits_existing_shared_ru_port_exception(self):
        self.install('--ru-keep-tcp-port', '10050')
        second = self.root / 'second'
        self.install('--eu-ip', '8.8.8.8', '--state-dir', str(second))
        self.assertEqual(d.u.Store(second).load('host-policy.json')['ru']['keepTcpPorts'], [10050])

    def test_kept_tcp_port_cannot_be_used_as_cascade_entry_before_any_mutation(self):
        with self.assertRaisesRegex(d.u.DeployError, 'сохраняемой службой'):
            self.install('--ru-keep-tcp-port', '10050', '--entry-port', '10050')
        self.assertEqual(self.api.calls, [])
        self.assertEqual(FakeSSH.calls, [])
        self.assertIsNone(self.store().load('plan.json'))

    def test_saved_port_policy_cannot_be_silently_broadened(self):
        self.install()
        before = copy.deepcopy(self.api.data)
        with self.assertRaisesRegex(d.u.DeployError, 'Исключения портов отличаются'):
            self.install('--ru-keep-tcp-port', '10050')
        self.assertEqual(self.api.data, before)

    def test_explicit_keep_flag_is_narrow_and_rejects_arbitrary_ports_or_duplicates(self):
        for values in (['--ru-keep-tcp-port', '443'], ['--eu-keep-tcp-port', '2222'],
                       ['--ru-keep-tcp-port', '10050', '--ru-keep-tcp-port', '10050']):
            with self.subTest(values=values), self.assertRaises(d.u.DeployError):
                d.validate(self.args(*values))

    def test_new_cascade_defaults_eu_udp_to_443_and_preserves_saved_port(self):
        self.install()
        self.assertEqual(self.store().load('plan.json')['link']['port'], 443)
        self.install()
        self.assertEqual(self.store().load('plan.json')['link']['port'], 443)
        second = self.root / 'second'
        self.install('--eu-ip', '8.8.8.8', '--state-dir', str(second), '--eu-wg-port', '51820')
        self.assertEqual(d.u.Store(second).load('plan.json')['link']['port'], 51820)
        with self.assertRaisesRegex(d.u.DeployError, 'repair_udp.py'):
            self.install('--eu-wg-port', '51820')


if __name__ == '__main__':
    unittest.main()
