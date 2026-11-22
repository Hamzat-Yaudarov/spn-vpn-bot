"""Offline VK-rental lifecycle regressions; no live panel, SSH or payments."""
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


support = load('vk_reality_test_support', HERE.parent / 'universal-reality/test_deploy.py')
d = load('vk_deploy_under_test', HERE / 'deploy.py')
IP, BASE, PANEL = support.IP, support.BASE, support.PANEL
RELAY, OTHER_RELAY = '185.86.147.77', '185.86.147.78'
FIRST = {'address': RELAY, 'port': 10016}
NEXT = {'address': OTHER_RELAY, 'port': 10038}
THIRD = {'address': '185.86.147.79', 'port': 10039}


class VKInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.api = support.FakeAPI()
        support.FakeSSH.calls, support.FakeSSH.connections = [], 0
        support.FakeSSH.on_action, support.FakeSSH.extra_addresses = None, set()
        support.FakeSSH.fresh = True
        self.probe_calls = []
        self.proof_transform = None
        self.export_called = 0

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, *extra):
        return d.parser().parse_args(['--ip', IP, '--relay', RELAY + ':10016',
                                     '--name', 'Германия', '--country', 'DE', '--squad', 'regular', *extra])

    def store(self, name='state'):
        return d.r.u.Store(self.root / name)

    def plan(self, store=None, args=None):
        return d.prepare(self.api, store or self.store(), args or self.args(), BASE, PANEL)

    def stage(self):
        store = self.store()
        plan = self.plan(store)
        deployment = d.Deployment(self.api, store, plan)
        profile, node, inbound = deployment.provision()
        host = deployment.host(profile, node, inbound)
        return store, plan, deployment, profile, node, inbound, host

    def interrupted_move(self, store, deployment, profile, node, inbound, target=NEXT):
        """Simulate process death after PATCH, before verification/commit/cleanup."""
        host = deployment.host(profile, node, inbound)
        pending = {'host': host['uuid'],
                   'before': {key: host[key] for key in ('address', 'port', 'isDisabled')},
                   'after': copy.deepcopy(target), 'committedBefore': store.load('relay.current.json')}
        store.save('relay-move-' + d.u.digest(pending) + '.intent.json', pending)
        d.write_pointer(store, 'relay.pending.json', pending, None)
        self.api('PATCH', '/hosts', {'uuid': host['uuid'], **target, 'isDisabled': False})
        return pending

    def installed(self):
        self.install(self.args('--apply', '--test-route', 'physical'))
        store = d.u.Store(self.root / (IP + '-22'))
        plan = store.load('plan.json')
        deployment = d.Deployment(self.api, store, plan)
        profile, node = self.api.rows('config-profiles')[0], self.api.rows('nodes')[0]
        inbound = profile['inbounds'][0]['uuid']
        return store, plan, deployment, profile, node, inbound

    @staticmethod
    def verified(*args, **kwargs):
        return {'passed': True}

    def install(self, args):
        def traffic(ip, *positional, **kwargs):
            route = kwargs.get('route', 'physical')
            result = {'passed': True, 'tests': 9, 'ip': ip, 'port': kwargs.get('public_port', 443),
                      'expectedExitIp': kwargs.get('expected_exit_ip', ip), 'testRoute': route,
                      'results': [{'passed': True, 'clientAlive': True} for _ in range(9)],
                      'physicalSourceObserved': route == 'physical',
                      'directReachabilityVerified': route == 'physical',
                      'relayPathVerified': route in ('physical', 'system') and 'expected_exit_ip' in kwargs}
            self.probe_calls.append({'ip': ip, **copy.deepcopy(kwargs)})
            if self.proof_transform:
                result = self.proof_transform(result)
            return result

        def exported(*positional, **kwargs):
            self.export_called += 1
            return {'passed': True}

        with patch.object(d.r, 'API', return_value=self.api), \
                patch.object(d.u, 'read_settings', return_value={
                    'REMNAWAVE_BASE_URL': BASE, 'REMNAWAVE_API_TOKEN': 'test-api-token-not-uploaded'}), \
                patch.object(d.r, 'panel_ipv4', return_value=PANEL), \
                patch.object(d.r, 'SSH', support.FakeSSH), \
                patch.object(d.r.trust, 'resolve', return_value=self.root / 'known-hosts'), \
                patch.object(d.r.probe, 'private_file', return_value=self.root / 'subscription'), \
                patch.object(d.r.probe, 'subscription_url', return_value='https://sub.wayspn.online/private-test'), \
                patch.object(d.r.probe, 'select_engine', return_value=self.root / 'xray'), \
                patch.object(d.r.probe, 'run', side_effect=traffic), \
                patch.object(d.r.probe, 'verify_export', side_effect=exported), \
                patch.object(d.r, 'state_directory', side_effect=lambda base, ip, port, home=None: self.root / (ip + '-' + str(port))), \
                patch.object(d.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            d.run(args)
        return output.getvalue()

    def test_mode_and_vk_sni_saved_without_network_mutations(self):
        store = self.store()
        plan = self.plan(store)
        self.assertEqual(plan['mode'], 'vk-relay')
        self.assertEqual(plan['ip'], IP)
        self.assertEqual(plan['sni'], 'login.vk.com')
        self.assertRegex(plan['code'], r'^[a-z0-9]{6}$')
        self.assertEqual(store.load('relay.initial.json'), FIRST)
        self.assertEqual(self.api.calls, [])

    def test_dry_plan_never_opens_panel_or_ssh(self):
        with patch.object(d.r, 'API', side_effect=AssertionError('panel access')), \
                patch.object(d.r, 'SSH', side_effect=AssertionError('SSH access')), \
                patch.object(d.r, 'state_directory', side_effect=AssertionError('state mutation')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            d.run(self.args())
        plan = json.loads(output.getvalue())
        self.assertFalse(plan['changes'])
        self.assertEqual(plan['backendDestination'], IP + ':443')
        self.assertEqual(plan['relay'], FIRST)

    def test_invalid_relay_input_is_rejected_before_network(self):
        for value in ('', RELAY, RELAY + ':0', RELAY + ':65536', RELAY + ':-1', RELAY + ':443/x',
                      '127.0.0.1:443', '10.0.0.1:10016', 'relay.example.com:443', '[::1]:443',
                      RELAY + ':10016;echo unsafe', RELAY + ':10016:1', IP + ':443'):
            with self.subTest(relay=value), self.assertRaises((d.r.u.DeployError, ValueError)):
                d.validate(self.args('--relay', value))
        self.assertEqual(self.api.calls, [])

    def test_assigned_rental_ports_are_not_fixed_to_443(self):
        for port in (1, 10016, 10038, 65535):
            with self.subTest(port=port):
                args = self.args('--relay', RELAY + ':' + str(port))
                d.validate(args)
                plan = self.plan(self.store('port-' + str(port)), args)
                self.assertEqual(plan['ip'], IP)

    def test_first_run_requires_rental_endpoint(self):
        args = d.parser().parse_args(['--ip', IP, '--squad', 'regular'])
        with self.assertRaises(d.r.u.DeployError):
            self.plan(args=args)
        self.assertEqual(self.api.calls, [])

    def test_resume_can_omit_optional_values_and_reuses_keys(self):
        store = self.store()
        before = self.plan(store)
        keys = copy.deepcopy(d.r.reality_keys(store))
        after = self.plan(store, d.parser().parse_args(['--ip', IP]))
        self.assertEqual(before, after)
        self.assertEqual(keys, d.r.reality_keys(store))
        self.assertEqual(self.api.calls, [])

    def test_new_rental_does_not_rewrite_immutable_backend_plan(self):
        store = self.store()
        original = self.plan(store)
        encoded = (store.path / 'plan.json').read_bytes()
        self.plan(store, self.args('--relay', OTHER_RELAY + ':10038'))
        self.assertEqual((store.path / 'plan.json').read_bytes(), encoded)
        self.assertEqual(store.load('relay.initial.json'), FIRST)
        self.assertEqual(store.load('plan.json')['id'], original['id'])

    def test_saved_direct_install_is_not_silently_converted(self):
        store = self.store()
        args = d.r.parser().parse_args(['--ip', IP, '--name', 'Обычный', '--squad', 'regular'])
        d.r.prepare(self.api, store, args, BASE, PANEL)
        before = (store.path / 'plan.json').read_bytes()
        with self.assertRaises(d.r.u.DeployError):
            self.plan(store)
        self.assertEqual((store.path / 'plan.json').read_bytes(), before)
        self.assertEqual(self.api.calls, [])

    def test_profile_and_node_use_backend_host_uses_rental_endpoint(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        self.assertEqual(node['address'], IP)
        self.assertEqual(node['port'], 2222)
        reality = profile['config']['inbounds'][0]['streamSettings']['realitySettings']
        self.assertEqual(reality['target'], 'login.vk.com:443')
        self.assertEqual(reality['serverNames'], ['login.vk.com'])
        self.assertEqual(host['address'], RELAY)
        self.assertEqual(host['port'], FIRST['port'])
        self.assertEqual(host['fingerprint'], 'safari')
        self.assertEqual(host['sni'], 'login.vk.com')
        self.assertTrue(host['isDisabled'])
        self.assertEqual(host['nodes'], [node['uuid']])
        for name in (profile['name'], node['name'], host['remark']):
            self.assertTrue(name.endswith(plan['code']))

    def test_verified_first_publication_enables_only_owned_host(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        other = {**copy.deepcopy(host), 'uuid': 'unrelated-host', 'nodes': ['another-node']}
        self.api.data['hosts'].append(other)
        result = deployment.publish_verified(host, FIRST, self.verified)
        self.assertFalse(result['isDisabled'])
        self.assertEqual(result['uuid'], host['uuid'])
        self.assertEqual(self.api.data['hosts'][1], other)
        self.assertEqual(store.load('relay.current.json'), FIRST)

    def test_new_lease_updates_same_host_without_recreating_backend(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)
        keys = copy.deepcopy(d.r.reality_keys(store))
        before_profiles, before_nodes = copy.deepcopy(self.api.data['config-profiles']), copy.deepcopy(self.api.data['nodes'])
        current = deployment.host(profile, node, inbound)
        moved = deployment.publish_verified(current, NEXT, self.verified)
        self.assertEqual(moved['uuid'], host['uuid'])
        self.assertEqual((moved['address'], moved['port']), (OTHER_RELAY, 10038))
        self.assertEqual(store.load('relay.current.json'), NEXT)
        self.assertEqual(self.api.data['config-profiles'], before_profiles)
        self.assertEqual(self.api.data['nodes'], before_nodes)
        self.assertEqual(len(self.api.data['hosts']), 1)
        self.assertEqual(d.r.reality_keys(store), keys)
        self.assertEqual(deployment.host(profile, node, inbound)['uuid'], host['uuid'])

    def test_failed_export_restores_previous_endpoint_and_availability(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        enabled = deployment.publish_verified(host, FIRST, self.verified)
        before = copy.deepcopy(enabled)

        def fail(*args, **kwargs):
            raise d.r.probe.ProbeError('Failed actual export check')

        with self.assertRaises((d.r.probe.ProbeError, d.r.u.DeployError)):
            deployment.publish_verified(enabled, NEXT, fail)
        current = self.api.data['hosts'][0]
        self.assertEqual((current['address'], current['port'], current['isDisabled']),
                         (before['address'], before['port'], before['isDisabled']))
        self.assertEqual(store.load('relay.current.json'), FIRST)
        resumed = deployment.publish_verified(deployment.host(profile, node, inbound), NEXT, self.verified)
        self.assertEqual((resumed['address'], resumed['port']), (OTHER_RELAY, 10038))
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_failed_first_export_keeps_new_host_disabled(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()

        def fail(*args, **kwargs):
            raise d.r.probe.ProbeError('Missing subscription export')

        with self.assertRaises((d.r.probe.ProbeError, d.r.u.DeployError)):
            deployment.publish_verified(host, FIRST, fail)
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])

    def test_false_export_proof_is_not_accepted(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        with self.assertRaises((d.r.probe.ProbeError, d.r.u.DeployError)):
            deployment.publish_verified(host, FIRST, lambda *args, **kwargs: {'passed': False})
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])

    def test_external_technical_host_change_is_not_overwritten(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        self.api.data['hosts'][0]['sni'] = 'changed.example.com'
        writes = copy.deepcopy(self.api.calls)
        with self.assertRaises(d.r.u.DeployError):
            deployment.publish_verified(host, NEXT, self.verified)
        self.assertEqual(self.api.data['hosts'][0]['sni'], 'changed.example.com')
        self.assertEqual(self.api.calls, writes)

    def test_missing_receipt_uuid_does_not_adopt_lookalike(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        self.api.data['hosts'][0]['uuid'] = 'different-persons-host'
        with self.assertRaises(d.r.u.DeployError):
            deployment.host(profile, node, inbound)
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_display_rename_is_preserved_when_rental_changes(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        self.api.data['hosts'][0]['remark'] = 'Название, установленное владельцем'
        actual = deployment.host(profile, node, inbound)
        moved = deployment.publish_verified(actual, NEXT, self.verified)
        self.assertEqual(moved['remark'], 'Название, установленное владельцем')

    def test_lost_host_patch_response_retries_same_operation(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)
        original_call = type(self.api).__call__
        lost = [False]

        def call(api, method, path, body=None):
            result = original_call(api, method, path, body)
            if method == 'PATCH' and path == '/hosts' and body.get('address') == NEXT['address'] and not lost[0]:
                lost[0] = True
                raise d.r.u.DeployError('Lost host PATCH response')
            return result

        with patch.object(type(self.api), '__call__', call):
            with self.assertRaises(d.r.u.DeployError):
                deployment.publish_verified(deployment.host(profile, node, inbound), NEXT, self.verified)
            result = deployment.publish_verified(deployment.host(profile, node, inbound), NEXT, self.verified)
        self.assertTrue(lost[0])
        self.assertEqual(result['uuid'], host['uuid'])
        self.assertEqual((result['address'], result['port']), (OTHER_RELAY, 10038))
        self.assertEqual(len(self.api.data['nodes']), 1)
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_ssh_only_verifies_backend_but_never_publishes_rental(self):
        output = self.install(self.args('--apply', '--test-route', 'ssh'))
        self.assertEqual(len(self.api.data['hosts']), 1)
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual([row['route'] for row in self.probe_calls], ['ssh'])
        self.assertEqual(self.export_called, 0)
        self.assertIn('VK-реле НЕ проверено', output)
        self.assertNotIn('Настроено:', output)

    def test_public_success_publishes_rental_not_backend_and_checks_exit(self):
        output = self.install(self.args('--apply', '--test-route', 'physical'))
        self.assertEqual([row['ip'] for row in self.probe_calls], [IP, RELAY])
        public = self.probe_calls[1]
        self.assertEqual(public['public_port'], 10016)
        self.assertEqual(public['expected_exit_ip'], IP)
        self.assertEqual(public['sni'], 'login.vk.com')
        self.assertEqual(self.export_called, 1)
        self.assertEqual(self.api.data['nodes'][0]['address'], IP)
        self.assertEqual(self.api.data['hosts'][0]['address'], RELAY)
        self.assertEqual(self.api.data['hosts'][0]['port'], 10016)
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])
        self.assertIn(IP + ':443', output)

    def test_wrong_exit_ip_cannot_publish_even_when_https_claims_success(self):
        def wrong(proof):
            if proof['testRoute'] != 'ssh':
                proof['expectedExitIp'] = '8.8.8.8'
            return proof

        self.proof_transform = wrong
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(self.export_called, 0)

    def test_wrong_rental_port_in_proof_cannot_publish(self):
        def wrong(proof):
            if proof['testRoute'] != 'ssh':
                proof['port'] = 443
            return proof

        self.proof_transform = wrong
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(self.export_called, 0)

    def test_ssh_proof_mislabeled_as_relay_is_not_accepted(self):
        def wrong(proof):
            if proof['testRoute'] != 'ssh':
                proof['testRoute'] = 'ssh'
                proof['relayPathVerified'] = True
            return proof

        self.proof_transform = wrong
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(self.export_called, 0)

    def test_backend_success_does_not_cover_failed_relay_tests(self):
        def failed(proof):
            if proof['testRoute'] != 'ssh':
                proof['passed'] = False
                proof['relayPathVerified'] = False
                for row in proof['results']:
                    row['passed'] = False
            return proof

        self.proof_transform = failed
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertEqual([row['route'] for row in self.probe_calls], ['ssh', 'physical', 'system'])
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(self.export_called, 0)

    def test_system_route_success_is_not_claimed_as_whitelist_proof(self):
        output = self.install(self.args('--apply', '--test-route', 'system'))
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])
        self.assertIn('текущее подключение Mac', output)
        self.assertIn('не подтверждён', output)

    def test_complete_rerun_with_new_lease_preserves_backend_ids(self):
        self.install(self.args('--apply', '--test-route', 'physical'))
        initial = copy.deepcopy(self.api.data)
        self.install(self.args('--apply', '--test-route', 'physical', '--relay', OTHER_RELAY + ':10038'))
        self.assertEqual(self.api.data['nodes'], initial['nodes'])
        self.assertEqual(self.api.data['config-profiles'], initial['config-profiles'])
        self.assertEqual(len(self.api.data['hosts']), 1)
        self.assertEqual(self.api.data['hosts'][0]['uuid'], initial['hosts'][0]['uuid'])
        self.assertEqual((self.api.data['hosts'][0]['address'], self.api.data['hosts'][0]['port']),
                         (OTHER_RELAY, 10038))
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])

    def test_failed_new_public_lease_preserves_previously_working_host(self):
        self.install(self.args('--apply', '--test-route', 'physical'))
        before = copy.deepcopy(self.api.data)

        def fail_relay(proof):
            if proof['testRoute'] != 'ssh':
                proof['passed'] = False
                proof['relayPathVerified'] = False
                for row in proof['results']:
                    row['passed'] = False
            return proof

        self.proof_transform = fail_relay
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply', '--relay', OTHER_RELAY + ':10038'))
        self.assertEqual(self.api.data, before)
        self.assertEqual(self.export_called, 1)

    def test_preflight_failure_does_not_mutate_panel(self):
        def fail(action):
            if action == 'preflight':
                raise d.u.DeployError('Unrelated existing installation')

        support.FakeSSH.on_action = fail
        original = copy.deepcopy(self.api.data)
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply'))
        self.assertEqual(self.api.data, original)
        self.assertEqual(self.api.calls, [])

    def test_resume_preserves_manually_disabled_completed_host(self):
        self.install(self.args('--apply', '--test-route', 'physical'))
        self.api.data['hosts'][0]['isDisabled'] = True
        output = self.install(self.args('--apply', '--test-route', 'physical'))
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(self.export_called, 1)
        self.assertIn('оставлен выключенным', output)

    def test_pending_lease_requires_explicit_cancellation_before_direct_publication(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)
        self.interrupted_move(store, deployment, profile, node, inbound)
        before = copy.deepcopy(self.api.calls)
        with self.assertRaises(d.u.DeployError):
            deployment.publish_verified(deployment.host(profile, node, inbound), THIRD, self.verified)
        self.assertEqual(self.api.calls, before)

    def test_preparation_can_select_new_lease_without_mutating_unfinished_one(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)
        pending = self.interrupted_move(store, deployment, profile, node, inbound)
        before = copy.deepcopy(self.api.calls)
        args = self.args('--relay', THIRD['address'] + ':' + str(THIRD['port']))
        selected = self.plan(store, args)
        self.assertEqual(selected, plan)
        self.assertEqual(d.endpoint(store, args), THIRD)
        self.assertEqual(store.load('relay.pending.json'), pending)
        self.assertEqual(self.api.calls, before)
        self.assertEqual(self.api.data['hosts'][0]['address'], NEXT['address'])

    def test_cancel_uncommitted_lease_restores_last_verified_endpoint(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)
        self.interrupted_move(store, deployment, profile, node, inbound)
        deployment.cancel_pending()
        actual = deployment.host(profile, node, inbound)
        self.assertEqual((actual['address'], actual['port'], actual['isDisabled']), (RELAY, 10016, False))
        self.assertEqual(store.load('relay.current.json'), FIRST)
        self.assertIsNone(store.load('relay.pending.json'))

    def test_cancel_first_unverified_lease_does_not_invent_verified_endpoint(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        self.interrupted_move(store, deployment, profile, node, inbound)
        deployment.cancel_pending()
        actual = deployment.host(profile, node, inbound)
        self.assertEqual((actual['address'], actual['port'], actual['isDisabled']), (RELAY, 10016, True))
        self.assertIsNone(store.load('relay.current.json'))
        self.assertIsNone(store.load('activation.verified.json'))
        self.assertIsNone(store.load('relay.pending.json'))

    def test_cancel_already_committed_lease_finishes_cleanup_instead_of_reverting(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)
        self.interrupted_move(store, deployment, profile, node, inbound)
        d.write_pointer(store, 'relay.current.json', NEXT, FIRST)
        before = copy.deepcopy(self.api.calls)
        deployment.cancel_pending()
        actual = deployment.host(profile, node, inbound)
        self.assertEqual((actual['address'], actual['port'], actual['isDisabled']),
                         (NEXT['address'], NEXT['port'], False))
        self.assertEqual(store.load('relay.current.json'), NEXT)
        self.assertIsNone(store.load('relay.pending.json'))
        self.assertEqual(self.api.calls, before)

    def test_cancel_lost_rollback_reply_can_resume_from_saved_before_state(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)
        self.interrupted_move(store, deployment, profile, node, inbound)
        original = type(self.api).__call__
        lost = [False]

        def call(api, method, path, body=None):
            result = original(api, method, path, body)
            if (method == 'PATCH' and path == '/hosts' and body.get('address') == FIRST['address']
                    and not lost[0]):
                lost[0] = True
                raise d.u.DeployError('Lost cancel rollback reply')
            return result

        with patch.object(type(self.api), '__call__', call):
            with self.assertRaises(d.u.DeployError):
                deployment.cancel_pending()
            deployment.cancel_pending()
        self.assertTrue(lost[0])
        self.assertEqual(store.load('relay.current.json'), FIRST)
        self.assertIsNone(store.load('relay.pending.json'))
        actual = deployment.host(profile, node, inbound)
        self.assertEqual((actual['address'], actual['port']), (RELAY, 10016))

    def test_cancel_does_not_overwrite_external_technical_change(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)
        pending = self.interrupted_move(store, deployment, profile, node, inbound)
        self.api.data['hosts'][0]['fingerprint'] = 'chrome'
        writes = copy.deepcopy(self.api.calls)
        with self.assertRaises(d.u.DeployError):
            deployment.cancel_pending()
        self.assertEqual(self.api.calls, writes)
        self.assertEqual(self.api.data['hosts'][0]['fingerprint'], 'chrome')
        self.assertEqual(store.load('relay.pending.json'), pending)

    def test_expired_pending_lease_is_not_probed_before_replacement(self):
        store, plan, deployment, profile, node, inbound = self.installed()
        self.interrupted_move(store, deployment, profile, node, inbound)
        self.probe_calls = []
        self.install(self.args('--apply', '--test-route', 'physical', '--relay',
                               THIRD['address'] + ':' + str(THIRD['port'])))
        public = [row for row in self.probe_calls if row['route'] != 'ssh']
        self.assertEqual([row['ip'] for row in public], [THIRD['address']])
        self.assertEqual(store.load('relay.current.json'), THIRD)
        self.assertIsNone(store.load('relay.pending.json'))
        self.assertEqual(len(self.api.data['nodes']), 1)
        self.assertEqual(len(self.api.data['hosts']), 1)

    def test_failed_new_lease_after_cancelling_expired_one_preserves_last_verified_lease(self):
        store, plan, deployment, profile, node, inbound = self.installed()
        self.interrupted_move(store, deployment, profile, node, inbound)
        self.probe_calls = []

        def fail(proof):
            if proof['testRoute'] != 'ssh':
                proof['passed'] = False
                proof['relayPathVerified'] = False
                for row in proof['results']:
                    row['passed'] = False
            return proof

        self.proof_transform = fail
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply', '--relay', THIRD['address'] + ':' + str(THIRD['port'])))
        self.assertEqual([row['ip'] for row in self.probe_calls if row['route'] != 'ssh'],
                         [THIRD['address'], THIRD['address']])
        actual = deployment.host(profile, node, inbound)
        self.assertEqual((actual['address'], actual['port'], actual['isDisabled']), (RELAY, 10016, False))
        self.assertEqual(store.load('relay.current.json'), FIRST)
        self.assertIsNone(store.load('relay.pending.json'))

    def test_confirmed_export_rollback_clears_pending_operation(self):
        store, plan, deployment, profile, node, inbound, host = self.stage()
        deployment.publish_verified(host, FIRST, self.verified)

        def fail():
            raise d.r.probe.ProbeError('New lease export failed')

        with self.assertRaises((d.r.probe.ProbeError, d.u.DeployError)):
            deployment.publish_verified(deployment.host(profile, node, inbound), NEXT, fail)
        self.assertIsNone(store.load('relay.pending.json'))
        args = self.args('--relay', THIRD['address'] + ':' + str(THIRD['port']))
        self.plan(store, args)
        self.assertEqual(d.endpoint(store, args), THIRD)


if __name__ == '__main__':
    unittest.main()
