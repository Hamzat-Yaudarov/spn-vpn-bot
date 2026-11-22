"""Explicit, recoverable replacement of a legacy node: all operations are fake."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('replacement_test_support', HERE / 'test_deploy.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
d = support.d
IP, BASE, PANEL = support.IP, support.BASE, support.PANEL
OLD_NODE = '11111111-2222-4333-8444-555555555555'
OLD_PROFILE = '22222222-2222-4333-8444-555555555555'
OLD_INBOUND = '33333333-2222-4333-8444-555555555555'
OTHER_NODE = '44444444-2222-4333-8444-555555555555'


class ReplacementTests(unittest.TestCase):
    setUp = support.InstallerTests.setUp
    tearDown = support.InstallerTests.tearDown
    args = support.InstallerTests.args
    plan = support.InstallerTests.plan
    store = support.InstallerTests.store
    install = support.InstallerTests.install

    def legacy(self):
        profile = {
            'uuid': OLD_PROFILE, 'name': 'old-shared-profile',
            'config': {'inbounds': [{'tag': 'old-ws', 'protocol': 'vless'}]},
            'inbounds': [{'uuid': OLD_INBOUND, 'tag': 'old-ws'}],
        }
        node = {
            'uuid': OLD_NODE, 'name': 'node-direct-legacy', 'address': IP, 'port': 2222,
            'tags': ['LEGACY'], 'note': 'Original legacy description', 'countryCode': 'DE',
            'configProfile': {'activeConfigProfileUuid': OLD_PROFILE, 'activeInbounds': [OLD_INBOUND]},
            'isDisabled': False, 'isConnected': False, 'lastStatusMessage': None,
        }
        old_host = {
            'uuid': '55555555-2222-4333-8444-555555555555', 'remark': 'Old WS',
            'address': 'old.example.com', 'port': 443, 'nodes': [OLD_NODE],
            'inbound': {'configProfileUuid': OLD_PROFILE, 'configProfileInboundUuid': OLD_INBOUND},
            'isDisabled': False,
        }
        shared_host = {**copy.deepcopy(old_host),
                       'uuid': '66666666-2222-4333-8444-555555555555', 'nodes': [OLD_NODE, OTHER_NODE]}
        unrelated_host = {**copy.deepcopy(old_host),
                          'uuid': '77777777-2222-4333-8444-555555555555', 'nodes': [OTHER_NODE]}
        self.api.data['config-profiles'] = [copy.deepcopy(profile)]
        self.api.data['nodes'] = [copy.deepcopy(node)]
        self.api.data['hosts'] = [copy.deepcopy(old_host), copy.deepcopy(shared_host), copy.deepcopy(unrelated_host)]
        return copy.deepcopy(self.api.data)

    def replacement(self, store=None):
        store = store or self.store()
        plan = self.plan(store, self.args('--replace-node', OLD_NODE))
        return store, plan, d.Deployment(self.api, store, plan)

    def test_existing_ip_requires_explicit_permission(self):
        self.legacy()
        with patch.object(d.sys.stdin, 'isatty', return_value=False), self.assertRaises(d.u.DeployError) as caught:
            self.plan()
        self.assertIn('--replace-node', str(caught.exception))
        self.assertEqual(self.api.calls, [])
        self.assertIsNone(self.store().load('plan.json'))

    def test_replacement_uuid_must_match_existing_target(self):
        original = self.legacy()
        with self.assertRaises(d.u.DeployError):
            self.plan(args=self.args('--replace-node', OTHER_NODE))
        self.assertEqual(self.api.data, original)
        self.assertEqual(self.api.calls, [])

    def test_duplicate_target_addresses_are_not_automatically_resolved(self):
        self.legacy()
        self.api.data['nodes'].append({**copy.deepcopy(self.api.data['nodes'][0]), 'uuid': OTHER_NODE})
        with self.assertRaises(d.u.DeployError):
            self.plan(args=self.args('--replace-node', OLD_NODE))
        self.assertEqual(self.api.calls, [])

    def test_replacement_flag_without_target_node_is_rejected(self):
        with self.assertRaises(d.u.DeployError):
            self.plan(args=self.args('--replace-node', OLD_NODE))
        self.assertEqual(self.api.calls, [])

    def test_preparation_only_persists_consent_no_panel_changes(self):
        original = self.legacy()
        store, plan, deployment = self.replacement()
        self.assertRegex(plan['code'], r'^[a-z0-9]{6}$')
        self.assertIsNotNone(store.load('replacement.intent.json'))
        self.assertEqual(self.api.data, original)
        self.assertEqual(self.api.calls, [])

    def test_explicit_replacement_preserves_node_uuid_and_other_objects(self):
        original = self.legacy()
        store, plan, deployment = self.replacement()
        profile, node, inbound = deployment.provision()
        self.assertEqual(node['uuid'], OLD_NODE)
        self.assertEqual(len(self.api.data['nodes']), 1)
        self.assertEqual(node['address'], plan['domain'])
        self.assertTrue(node['name'].endswith(plan['code']))
        self.assertEqual(node['configProfile']['activeConfigProfileUuid'], profile['uuid'])
        self.assertEqual(d.u.ids(node['configProfile']['activeInbounds']), [inbound])
        self.assertFalse(node['isDisabled'])
        self.assertEqual(self.api.data['config-profiles'][0], original['config-profiles'][0])
        self.assertEqual(len(self.api.data['config-profiles']), 2)
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(self.api.data['hosts'][1:], original['hosts'][1:])
        self.assertEqual(self.api.data['internal-squads'], original['internal-squads'])
        self.assertEqual(store.load('node.receipt.json')['uuid'], OLD_NODE)
        self.assertFalse(any(method == 'POST' and path == '/nodes' for method, path, body in self.api.calls))

    def test_repeat_uses_same_replacement_without_second_patch(self):
        self.legacy()
        store, plan, deployment = self.replacement()
        profile, node, inbound = deployment.provision()
        writes_before = copy.deepcopy(self.api.calls)
        repeat_plan = self.plan(store, d.parser().parse_args(['--ip', IP]))
        repeat = d.Deployment(self.api, store, repeat_plan)
        after_profile, after_node, after_inbound = repeat.provision()
        self.assertEqual((profile['uuid'], node['uuid'], inbound),
                         (after_profile['uuid'], after_node['uuid'], after_inbound))
        self.assertEqual(self.api.calls, writes_before)

    def test_saved_plan_rejects_switch_to_different_replacement_uuid(self):
        self.legacy()
        store, plan, deployment = self.replacement()
        with self.assertRaises(d.u.DeployError):
            self.plan(store, self.args('--replace-node', OTHER_NODE))
        self.assertEqual(self.api.calls, [])

    def test_external_node_drift_before_patch_is_not_overwritten(self):
        self.legacy()
        store, plan, deployment = self.replacement()
        self.api.data['nodes'][0]['note'] = 'Changed after confirmation'
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        self.assertEqual(self.api.data['nodes'][0]['note'], 'Changed after confirmation')
        self.assertFalse(any(path == '/nodes' or '/nodes/' in path for method, path, body in self.api.calls))

    def test_external_host_drift_before_patch_is_not_disabled(self):
        self.legacy()
        store, plan, deployment = self.replacement()
        self.api.data['hosts'][0]['nodes'] = [OTHER_NODE]
        with self.assertRaises(d.u.DeployError):
            deployment.provision()
        self.assertFalse(self.api.data['hosts'][0]['isDisabled'])

    def test_lost_node_patch_response_resumes_without_duplicate(self):
        self.legacy()
        store, plan, deployment = self.replacement()
        original_call = type(self.api).__call__
        lost = [False]

        def call(api, method, path, body=None):
            result = original_call(api, method, path, body)
            if method == 'PATCH' and path == '/nodes' and not lost[0]:
                lost[0] = True
                raise d.u.DeployError('Lost node PATCH response')
            return result

        with patch.object(type(self.api), '__call__', call):
            with self.assertRaises(d.u.DeployError):
                deployment.provision()
            profile, node, inbound = deployment.provision()
        self.assertTrue(lost[0])
        self.assertEqual(node['uuid'], OLD_NODE)
        self.assertFalse(node['isDisabled'])
        self.assertEqual(len(self.api.data['nodes']), 1)
        self.assertEqual(len(self.api.data['config-profiles']), 2)
        self.assertEqual(sum(method == 'PATCH' and path == '/nodes' for method, path, body in self.api.calls), 1)
        self.assertEqual(store.load('node.receipt.json')['uuid'], OLD_NODE)

    def test_lost_disable_response_resumes_from_disabled_node(self):
        self.legacy()
        store, plan, deployment = self.replacement()
        original_disable = self.api.disable_node
        lost = [False]

        def disable(uid):
            result = original_disable(uid)
            if not lost[0]:
                lost[0] = True
                raise d.u.DeployError('Lost disable response')
            return result

        with patch.object(self.api, 'disable_node', side_effect=disable):
            with self.assertRaises(d.u.DeployError):
                deployment.provision()
            profile, node, inbound = deployment.provision()
        self.assertEqual(node['uuid'], OLD_NODE)
        self.assertFalse(node['isDisabled'])
        self.assertEqual(len(self.api.data['nodes']), 1)
        self.assertEqual(len(self.api.data['config-profiles']), 2)
        self.assertEqual(sum(path == '/nodes/' + OLD_NODE + '/actions/disable'
                             for method, path, body in self.api.calls), 1)

    def test_lost_old_host_disable_response_does_not_repeat_node_creation(self):
        self.legacy()
        store, plan, deployment = self.replacement()
        original_call = type(self.api).__call__
        lost = [False]

        def call(api, method, path, body=None):
            result = original_call(api, method, path, body)
            if method == 'PATCH' and path == '/hosts' and not lost[0]:
                lost[0] = True
                raise d.u.DeployError('Lost host disable response')
            return result

        with patch.object(type(self.api), '__call__', call):
            with self.assertRaises(d.u.DeployError):
                deployment.provision()
            profile, node, inbound = deployment.provision()
        self.assertTrue(lost[0])
        self.assertEqual(node['uuid'], OLD_NODE)
        self.assertFalse(node['isDisabled'])
        self.assertTrue(self.api.data['hosts'][0]['isDisabled'])
        self.assertEqual(len(self.api.data['nodes']), 1)
        self.assertEqual(len(self.api.data['config-profiles']), 2)
        self.assertEqual(sum(method == 'PATCH' and path == '/hosts' for method, path, body in self.api.calls), 1)

    def test_preflight_failure_does_not_change_panel(self):
        original = self.legacy()

        def fail(action):
            if action == 'preflight':
                raise d.u.DeployError('Not a clean or owned VPS')

        support.FakeSSH.on_action = fail
        with self.assertRaises(d.u.DeployError):
            self.install(self.args('--apply', '--replace-node', OLD_NODE, '--test-route', 'ssh'))
        self.assertEqual(self.api.data, original)
        self.assertEqual(self.api.calls, [])


if __name__ == '__main__':
    unittest.main()
