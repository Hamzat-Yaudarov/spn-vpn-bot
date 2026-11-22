"""Offline regressions for multiple targets, endpoint aliases and reinstall."""
import contextlib
import copy
import io
import json
from pathlib import Path
import socket
from unittest import TestCase, main
from unittest.mock import patch

import deploy as d
from test_deploy import FakeAPI, plan, temporary_directory


class StateDirectoryTests(TestCase):
    def root(self, home):
        return Path(home) / '.spn-direct-deploy' / d.digest(plan()['panel'])[:12]

    def test_target_identity_is_stable_and_separates_ip_domain_and_ssh_port(self):
        with temporary_directory() as home:
            base = plan()['panel']
            target = d.default_state_directory(base, 'node.example.com', '8.8.8.8', 22, home=Path(home))
            self.assertEqual(target, self.root(home) / 'node.example.com' / 'targets' / '8.8.8.8-ssh22')
            self.assertEqual(target, d.default_state_directory(base, 'node.example.com', '8.8.8.8', 22, home=Path(home)))
            variants = {
                d.default_state_directory(base, 'node.example.com', '8.8.4.4', 22, home=Path(home)),
                d.default_state_directory(base, 'second.example.com', '8.8.8.8', 22, home=Path(home)),
                d.default_state_directory(base, 'node.example.com', '8.8.8.8', 22222, home=Path(home)),
                d.default_state_directory('https://other.example.com/api', 'node.example.com', '8.8.8.8', 22, home=Path(home)),
            }
            self.assertEqual(len(variants), 4)
            self.assertNotIn(target, variants)

    def test_matching_legacy_domain_state_is_reused_without_rewrite(self):
        with temporary_directory() as home:
            legacy = self.root(home) / 'node.example.com'
            store = d.Store(legacy)
            store.save('plan.json', plan())
            before = (legacy / 'plan.json').read_bytes()
            actual = d.default_state_directory(plan()['panel'], 'node.example.com', '8.8.8.8', 22, home=Path(home))
            self.assertEqual(actual, legacy)
            self.assertEqual((legacy / 'plan.json').read_bytes(), before)

    def test_changed_target_preserves_legacy_plan_and_key(self):
        for target_ip, port in [('8.8.4.4', 22), ('8.8.8.8', 22222)]:
            with self.subTest(ip=target_ip, port=port), temporary_directory() as home:
                legacy = self.root(home) / 'node.example.com'
                store = d.Store(legacy)
                store.save('plan.json', plan())
                store.save('node-secret.json', {'value': 'old-service-key'})
                before = {p.name: p.read_bytes() for p in legacy.iterdir()}
                actual = d.default_state_directory(plan()['panel'], 'node.example.com', target_ip, port, home=Path(home))
                self.assertEqual(actual, legacy / 'targets' / (target_ip + '-ssh' + str(port)))
                self.assertEqual({p.name: p.read_bytes() for p in legacy.iterdir() if p.is_file()}, before)


class HostOwnershipTests(TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.store = d.Store(self.tmp.name)
        self.api = FakeAPI()
        self.operation = plan()
        self.profile = json.loads((d.HERE / 'profile.json').read_text())
        self.deployment = d.Deployment(self.api, self.store, self.operation, self.profile)

    def add_foreign_aliases(self):
        for kind, path in [('grpc', 'spngrpc'), ('ws', '/direct/ws/')]:
            for number in range(2):
                self.api.data['hosts'].append({
                    'uuid': f'foreign-{kind}-{number}', 'remark': f'Old {kind} alias {number}',
                    'address': self.operation['domain'], 'path': path, 'port': 443, 'isDisabled': False,
                    'inbound': {'configProfileUuid': f'foreign-profile-{number}',
                                'configProfileInboundUuid': f'foreign-inbound-{kind}-{number}'}})
        return copy.deepcopy(self.api.data['hosts'])

    def test_foreign_endpoint_aliases_do_not_block_owned_hosts_or_retry(self):
        foreign = self.add_foreign_aliases()
        profile, node, inbound = self.deployment.provision()
        hosts = self.deployment.hosts(profile, node, inbound)
        self.assertEqual(len(hosts), 2)
        self.assertEqual(self.api.data['hosts'][:len(foreign)], foreign)
        self.assertEqual(len(self.api.data['hosts']), len(foreign) + 2)
        count = self.api.post_count()
        retried = self.deployment.hosts(profile, node, inbound)
        self.assertEqual({k: r['uuid'] for k, r in retried.items()}, {k: r['uuid'] for k, r in hosts.items()})
        self.assertEqual(self.api.post_count(), count)

    def test_lost_own_host_response_adopts_by_inbound_not_endpoint(self):
        foreign = self.add_foreign_aliases()
        profile, node, inbound = self.deployment.provision()
        self.api.lose_after = ('POST', '/hosts')
        with self.assertRaises(d.DeployError):
            self.deployment.hosts(profile, node, inbound)
        hosts = self.deployment.hosts(profile, node, inbound)
        self.assertEqual(len(hosts), 2)
        self.assertEqual(self.api.post_count(), 4)
        self.assertEqual(len(self.api.data['hosts']), len(foreign) + 2)
        self.assertEqual(self.api.data['hosts'][:len(foreign)], foreign)

    def test_duplicate_own_inbound_without_receipt_still_stops(self):
        profile, node, inbound = self.deployment.provision()
        for number in range(2):
            self.api.data['hosts'].append({
                'uuid': 'own-inbound-alias-' + str(number), 'address': self.operation['domain'], 'path': 'spngrpc',
                'inbound': {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbound['direct-grpc']}})
        count = self.api.post_count()
        with self.assertRaises(d.DeployError):
            self.deployment.hosts(profile, node, inbound)
        self.assertEqual(self.api.post_count(), count)

    def test_receipt_uuid_wins_over_an_additional_same_inbound_alias(self):
        profile, node, inbound = self.deployment.provision()
        hosts = self.deployment.hosts(profile, node, inbound)
        alias = {**copy.deepcopy(hosts['grpc']), 'uuid': 'manual-same-inbound-alias'}
        self.api.data['hosts'].append(alias)
        count = self.api.post_count()
        retried = self.deployment.hosts(profile, node, inbound)
        self.assertEqual(retried['grpc']['uuid'], hosts['grpc']['uuid'])
        self.assertEqual(self.api.post_count(), count)
        self.assertEqual(self.api.data['hosts'][-1], alias)

    def test_missing_receipt_uuid_never_adopts_replacement_alias(self):
        profile, node, inbound = self.deployment.provision()
        hosts = self.deployment.hosts(profile, node, inbound)
        existing = next(r for r in self.api.data['hosts'] if r['uuid'] == hosts['grpc']['uuid'])
        existing['uuid'] = 'unrelated-replacement'
        count = self.api.post_count()
        with self.assertRaises(d.DeployError):
            self.deployment.hosts(profile, node, inbound)
        self.assertEqual(self.api.post_count(), count)


class ReusableWorkflowTests(TestCase):
    def make_ssh(self, events):
        class FakeSSH:
            def __init__(self, ip, port):
                self.ip = ip
            def __enter__(self):
                events.append(('enter', self.ip))
                return self
            def __exit__(self, *args):
                pass
            def upload(self, operation, files):
                events.append(('upload', self.ip))
            def action(self, operation, action):
                events.append((action, self.ip))
        return FakeSSH

    def fake_dns(self, target):
        def dns(host, port, family, kind):
            ip = target[0] if host == 'node.example.com' else '1.1.1.1'
            address = '::ffff:' + ip if family == socket.AF_INET6 else ip
            return [(family, kind, 6, '', (address, port, 0, 0) if family == socket.AF_INET6 else (address, port))]
        return dns

    def settings(self):
        return {'REMNAWAVE_BASE_URL': plan()['panel'], 'REMNAWAVE_API_TOKEN': 'test-token'}

    def test_reinstall_bypasses_completed_shortcut_and_reuses_panel_objects_and_key(self):
        api, events, target = FakeAPI(), [], ['8.8.8.8']
        with temporary_directory() as tmp, \
             patch.object(d, 'API', return_value=api), \
             patch.object(d, 'SSH', self.make_ssh(events)), \
             patch.object(d, 'read_settings', return_value=self.settings()), \
             patch.object(d.socket, 'getaddrinfo', side_effect=self.fake_dns(target)), \
             patch.object(d, 'verify_health'), \
             patch.object(d, 'default_state_directory', side_effect=AssertionError('Explicit state-dir ignored')), \
             contextlib.redirect_stdout(io.StringIO()):
            args = d.parser().parse_args(['--ip', target[0], '--domain', 'node.example.com', '--squad', 's1',
                                         '--state-dir', tmp, '--apply', '--agree-le-tos'])
            d.run(args)
            before = copy.deepcopy(api.data)
            stored_plan = d.Store(tmp).load('plan.json')
            completed = d.Store(tmp).load('complete.json')
            args.reinstall = True
            d.run(args)
            self.assertEqual(api.data, before)
            self.assertEqual(api.post_count(), 4)
            self.assertEqual(sum(path == '/keygen' for _, path, _ in api.calls), 1)
            self.assertEqual(events.count(('install', target[0])), 2)
            self.assertEqual(d.Store(tmp).load('plan.json'), stored_plan)
            self.assertEqual(d.Store(tmp).load('complete.json'), completed)
            count = len(events)
            args.reinstall = False
            d.run(args)
            self.assertEqual(len(events), count)

    def test_changed_ip_same_domain_creates_new_target_without_changing_old_objects(self):
        api, events, target = FakeAPI(), [], ['8.8.8.8']
        with temporary_directory() as home, \
             patch.object(d.Path, 'home', return_value=Path(home)), \
             patch.object(d, 'API', return_value=api), \
             patch.object(d, 'SSH', self.make_ssh(events)), \
             patch.object(d, 'read_settings', return_value=self.settings()), \
             patch.object(d.socket, 'getaddrinfo', side_effect=self.fake_dns(target)), \
             patch.object(d, 'verify_health'), contextlib.redirect_stdout(io.StringIO()):
            args = d.parser().parse_args(['--ip', target[0], '--domain', 'node.example.com', '--name', 'Germany',
                                         '--country', 'DE', '--squad', 's1', '--apply', '--agree-le-tos'])
            d.run(args)
            first_dir = d.default_state_directory(plan()['panel'], args.domain, args.ip, 22, home=Path(home))
            first_plan = d.Store(first_dir).load('plan.json')
            old_profiles = copy.deepcopy(api.data['config-profiles'])
            old_nodes = copy.deepcopy(api.data['nodes'])
            old_hosts = copy.deepcopy(api.data['hosts'])
            target[0] = args.ip = '8.8.4.4'
            args.name, args.country = 'Finland', 'FI'
            d.run(args)
            second_dir = d.default_state_directory(plan()['panel'], args.domain, args.ip, 22, home=Path(home))
            second_plan = d.Store(second_dir).load('plan.json')
            self.assertNotEqual(first_dir, second_dir)
            self.assertNotEqual(first_plan['id'], second_plan['id'])
            self.assertEqual(d.Store(first_dir).load('plan.json'), first_plan)
            self.assertEqual(api.data['config-profiles'][:len(old_profiles)], old_profiles)
            self.assertEqual(api.data['nodes'][:len(old_nodes)], old_nodes)
            self.assertEqual(api.data['hosts'][:len(old_hosts)], old_hosts)
            self.assertEqual((len(api.data['config-profiles']), len(api.data['nodes']), len(api.data['hosts'])), (2, 2, 4))
            self.assertEqual(api.post_count(), 8)
            count = len(events)
            d.run(args)
            self.assertEqual(api.post_count(), 8)
            self.assertEqual(len(events), count)


if __name__ == '__main__':
    main()
