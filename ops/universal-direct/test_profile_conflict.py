"""Offline regressions for panel-global tags and legacy profile recovery."""
import copy
import json
from unittest import TestCase, main

import deploy as d
from test_deploy import FakeAPI, plan, temporary_directory


class UniqueProfileAPI(FakeAPI):
    """Model the panel constraints the original fake omitted."""

    def __call__(self, method, path, body=None):
        if method == 'POST' and path == '/config-profiles':
            profiles = self.data['config-profiles']
            tags = [row['tag'] for row in body['config']['inbounds']]
            occupied = {row['tag'] for profile in profiles for row in profile['inbounds']}
            if (any(profile['name'] == body['name'] for profile in profiles)
                    or len(tags) != len(set(tags)) or occupied.intersection(tags)):
                raise d.DeployError('Fake HTTP 409: profile name or inbound tag already exists')
        result = super().__call__(method, path, body)
        if method == 'POST' and path == '/config-profiles':
            # UUID identity is unique to the profile, not just its logical tag.
            for index, inbound in enumerate(self.data['config-profiles'][-1]['inbounds']):
                inbound['uuid'] = result['uuid'] + '-inbound-' + str(index)
            result = copy.deepcopy(self.data['config-profiles'][-1])
        return result


class ProfileConflictTests(TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.store = d.Store(self.tmp.name)
        self.api = UniqueProfileAPI()
        self.plan = plan()
        self.template = json.loads((d.HERE / 'profile.json').read_text())

    def deployment(self, recover=False, store=None, api=None, operation=None):
        return d.Deployment(api or self.api, store or self.store, operation or self.plan,
                            self.template, recover_profile_conflict=recover)

    def legacy_payload(self, operation=None):
        operation = operation or self.plan
        name = 'AUTO_' + operation['domain'].replace('.', '_')[:16] + '_' + operation['id'][:8]
        return {'name': name, 'config': copy.deepcopy(self.template)}

    def add_pending_legacy(self, store=None):
        store = store or self.store
        payload = self.legacy_payload()
        store.save('profile.intent.json', payload)
        return payload

    def occupy_legacy_tags(self, api=None, only=None):
        api = api or self.api
        config = copy.deepcopy(self.template)
        if only:
            config['inbounds'] = [row for row in config['inbounds'] if row['tag'] == only]
        return api('POST', '/config-profiles', {'name': 'unrelated-profile', 'config': config})

    def create_legacy(self, receipt=False):
        payload = self.add_pending_legacy()
        profile = self.api('POST', '/config-profiles', payload)
        if receipt:
            self.store.save('profile.receipt.json', {'uuid': profile['uuid']})
        return profile

    def test_two_servers_have_disjoint_tags_and_independent_objects(self):
        first_profile, first_node, first_inbound = self.deployment().provision()
        second = {**self.plan, 'id': 'b' * 32, 'domain': 'second.example.com', 'ip': '8.8.4.4'}
        with temporary_directory() as tmp:
            store = d.Store(tmp)
            deployment = self.deployment(store=store, operation=second)
            second_profile, second_node, second_inbound = deployment.provision()
            count = self.api.post_count()
            deployment.provision()
            self.assertEqual(count, self.api.post_count())
        self.assertNotEqual(first_profile['uuid'], second_profile['uuid'])
        self.assertNotEqual(first_node['uuid'], second_node['uuid'])
        self.assertTrue(set(first_inbound.values()).isdisjoint(second_inbound.values()))
        first_tags = {row['tag'] for row in first_profile['inbounds']}
        second_tags = {row['tag'] for row in second_profile['inbounds']}
        self.assertEqual(first_tags, {'direct-grpc-' + 'a' * 16, 'direct-ws-' + 'a' * 16})
        self.assertTrue(first_tags.isdisjoint(second_tags))
        self.assertEqual(set(first_inbound), {'direct-grpc', 'direct-ws'})
        self.assertEqual(set(second_inbound), {'direct-grpc', 'direct-ws'})
        self.assertEqual(self.template, json.loads((d.HERE / 'profile.json').read_text()))

    def test_profile_request_is_stable_without_mutating_template(self):
        deployment = self.deployment()
        key, payload, mapping = deployment._profile_request()
        self.assertEqual((key, payload, mapping), deployment._profile_request())
        self.assertEqual(mapping, {kind: kind + '-' + self.plan['id'][:16]
                                   for kind in ('direct-grpc', 'direct-ws')})
        self.assertEqual(payload['name'], self.legacy_payload()['name'])
        self.assertEqual([row['tag'] for row in self.template['inbounds']], ['direct-grpc', 'direct-ws'])

    def test_created_legacy_profile_without_receipt_is_adopted_unchanged(self):
        legacy = self.create_legacy(receipt=False)
        old_intent = self.store.load('profile.intent.json')
        profile, _, inbound = self.deployment().provision()
        self.assertEqual(profile['uuid'], legacy['uuid'])
        self.assertEqual(profile['config'], self.template)
        self.assertEqual(set(inbound), {'direct-grpc', 'direct-ws'})
        self.assertEqual(self.store.load('profile.intent.json'), old_intent)
        self.assertIsNone(self.store.load('profile.recovery.json'))
        self.assertEqual(len(self.api.data['config-profiles']), 1)

    def test_completed_legacy_deployment_still_resumes_without_new_post(self):
        self.create_legacy(receipt=True)
        deployment = self.deployment()
        profile, node, inbound = deployment.provision()
        deployment.attach(inbound)
        deployment.publish(deployment.hosts(profile, node, inbound))
        old_intent = self.store.load('profile.intent.json')
        count = self.api.post_count()
        profile, node, inbound = self.deployment().provision()
        hosts = self.deployment().hosts(profile, node, inbound)
        self.assertTrue(all(not row['isDisabled'] for row in hosts.values()))
        self.assertEqual(self.api.post_count(), count)
        self.assertEqual(self.store.load('profile.intent.json'), old_intent)
        self.assertIsNone(self.store.load('profile.recovery.json'))

    def test_absent_legacy_profile_needs_explicit_recovery(self):
        self.add_pending_legacy()
        self.occupy_legacy_tags()
        count = self.api.post_count()
        with self.assertRaises(d.DeployError):
            self.deployment().provision()
        self.assertEqual(self.api.post_count(), count)
        self.assertIsNone(self.store.load('profile.recovery.json'))

    def test_recovery_keeps_old_intent_and_uses_new_namespaced_request(self):
        original = self.add_pending_legacy()
        unrelated = self.occupy_legacy_tags()
        before = copy.deepcopy(unrelated)
        deployment = self.deployment(recover=True)
        profile, _, inbound = deployment.provision()
        self.assertEqual(self.store.load('profile.intent.json'), original)
        self.assertIsNotNone(self.store.load('profile-v2.intent.json'))
        self.assertIsNotNone(self.store.load('profile-v2.receipt.json'))
        marker = self.store.load('profile.recovery.json')
        self.assertEqual(marker['version'], 2)
        self.assertEqual(marker['planId'], self.plan['id'])
        self.assertEqual(marker['legacyIntentHash'], d.digest(original))
        self.assertEqual(marker['newPayloadHash'], d.digest(self.store.load('profile-v2.intent.json')))
        self.assertEqual(marker['name'], original['name'])
        self.assertEqual(profile['name'], original['name'])
        self.assertEqual(set(inbound), {'direct-grpc', 'direct-ws'})
        self.assertEqual(self.api.data['config-profiles'][0], before)
        count = self.api.post_count()
        self.deployment().provision()  # recovery flag is not required on resume
        self.assertEqual(self.api.post_count(), count)

    def test_crash_after_recovery_marker_resumes_v2_without_flag(self):
        self.add_pending_legacy()
        self.occupy_legacy_tags()
        key, payload, mapping = self.deployment(recover=True)._profile_request()
        self.assertEqual(key, 'profile-v2')
        self.assertIsNotNone(self.store.load('profile.recovery.json'))
        self.assertIsNone(self.store.load('profile-v2.intent.json'))
        self.assertEqual(self.deployment()._profile_request(), (key, payload, mapping))
        profile, _, _ = self.deployment().provision()
        self.assertEqual(profile['config'], payload['config'])
        self.assertEqual(len(self.api.data['config-profiles']), 2)

    def test_lost_v2_creation_response_is_adopted_without_repeating_post(self):
        original = self.add_pending_legacy()
        self.occupy_legacy_tags()
        self.api.lose_after = ('POST', '/config-profiles')
        with self.assertRaises(d.DeployError):
            self.deployment(recover=True).provision()
        self.assertIsNotNone(self.store.load('profile.recovery.json'))
        self.assertIsNotNone(self.store.load('profile-v2.intent.json'))
        self.assertIsNone(self.store.load('profile-v2.receipt.json'))
        creates_before = sum(method == 'POST' and path == '/config-profiles'
                             for method, path, _ in self.api.calls)
        profile, _, _ = self.deployment().provision()
        self.deployment().provision()
        creates_after = sum(method == 'POST' and path == '/config-profiles'
                            for method, path, _ in self.api.calls)
        self.assertEqual(creates_before, creates_after)
        self.assertEqual(profile['name'], original['name'])
        self.assertEqual(self.store.load('profile.intent.json'), original)
        self.assertEqual(len(self.api.data['config-profiles']), 2)

    def test_late_legacy_profile_after_marker_is_never_overwritten(self):
        original = self.add_pending_legacy()
        self.occupy_legacy_tags()
        self.deployment(recover=True)._profile_request()
        # Simulate a delayed legacy result/independent conflicting observation,
        # bypassing fake creation constraints deliberately. Recovery must stop.
        late = {**copy.deepcopy(original), 'uuid': 'late-profile', 'inbounds': [
            {'tag': row['tag'], 'uuid': 'late-' + row['tag']} for row in original['config']['inbounds']]}
        self.api.data['config-profiles'].append(late)
        before = copy.deepcopy(self.api.data)
        count = self.api.post_count()
        with self.assertRaises(d.DeployError):
            self.deployment().provision()
        self.assertEqual(self.api.post_count(), count)
        self.assertEqual(self.api.data, before)

    def test_no_automatic_recovery_without_confirming_both_occupied_tags(self):
        for occupied in (None, 'direct-grpc', 'direct-ws'):
            with self.subTest(occupied=occupied), temporary_directory() as tmp:
                store, api = d.Store(tmp), UniqueProfileAPI()
                self.add_pending_legacy(store=store)
                if occupied:
                    self.occupy_legacy_tags(api=api, only=occupied)
                count = api.post_count()
                with self.assertRaises(d.DeployError):
                    self.deployment(recover=True, store=store, api=api).provision()
                self.assertEqual(api.post_count(), count)
                self.assertIsNone(store.load('profile.recovery.json'))

    def test_downstream_state_blocks_legacy_recovery(self):
        for filename in ('node.intent.json', 'node.receipt.json', 'grpc.intent.json',
                         'ws.receipt.json', 'node-secret.json', 'complete.json'):
            with self.subTest(filename=filename), temporary_directory() as tmp:
                store, api = d.Store(tmp), UniqueProfileAPI()
                self.add_pending_legacy(store=store)
                self.occupy_legacy_tags(api=api)
                store.save(filename, {'uuid': 'already-started'})
                count = api.post_count()
                with self.assertRaises(d.DeployError):
                    self.deployment(recover=True, store=store, api=api).provision()
                self.assertEqual(api.post_count(), count)
                self.assertIsNone(store.load('profile.recovery.json'))

    def test_existing_target_ip_node_blocks_legacy_recovery(self):
        self.add_pending_legacy()
        self.occupy_legacy_tags()
        self.api.data['nodes'].append({'uuid': 'unrelated-node', 'name': 'another', 'address': self.plan['ip']})
        count = self.api.post_count()
        with self.assertRaises(d.DeployError):
            self.deployment(recover=True).provision()
        self.assertEqual(self.api.post_count(), count)
        self.assertIsNone(self.store.load('profile.recovery.json'))

    def test_changed_recovery_marker_is_rejected_without_any_post(self):
        self.add_pending_legacy()
        self.occupy_legacy_tags()
        self.deployment(recover=True)._profile_request()
        marker = self.store.load('profile.recovery.json')
        marker['planId'] = 'f' * 32
        # Simulate externally edited local state while preserving its mode.
        with (self.store.path / 'profile.recovery.json').open('w') as stream:
            json.dump(marker, stream)
        count = self.api.post_count()
        with self.assertRaises(d.DeployError):
            self.deployment().provision()
        self.assertEqual(self.api.post_count(), count)


if __name__ == '__main__':
    main()
