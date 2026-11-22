"""Offline tests: fake API/SSH only; never read real settings or contact VPSes."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import deploy as d


def temporary_directory():
    # macOS TMPDIR starts with /var -> /private/var, a system alias. Tests use
    # the canonical path because deployment state deliberately rejects links.
    return tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())


def plan():
    return {'id': 'a' * 32, 'format': 1, 'ip': '8.8.8.8', 'domain': 'node.example.com',
            'name': 'Германия', 'country': 'DE', 'sshPort': 22,
            'panel': 'https://panel.example.com/api', 'panelIp': '1.1.1.1', 'squads': ['s1']}


class FakeAPI:
    def __init__(self):
        self.data = {kind: [] for kind in ('config-profiles', 'nodes', 'hosts')}
        self.data['internal-squads'] = [
            {'uuid': 's1', 'name': 'Основной', 'inbounds': [{'uuid': 'old-inbound'}]},
            {'uuid': 's2', 'name': 'Другой', 'inbounds': []}]
        self.calls, self.lose_after, self.lose_before = [], None, None
        self.serial = 0

    def rows(self, kind):
        return copy.deepcopy(self.data[kind])

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.lose_before == (method, path):
            self.lose_before = None
            raise d.DeployError('Fake lost request')
        kind = path[1:]
        if method == 'GET' and path == '/keygen':
            return {'secretKey': 'N' * 64}
        if method == 'POST':
            self.serial += 1
            result = dict(copy.deepcopy(body), uuid='uuid-' + str(self.serial))
            if kind == 'config-profiles':
                result['inbounds'] = [{'uuid': 'in-' + i['tag'], 'tag': i['tag']}
                                      for i in body['config']['inbounds']]
            if kind == 'nodes':
                result['configProfile']['activeInbounds'] = [
                    {'uuid': uid} for uid in body['configProfile']['activeInbounds']]
                result.update(isConnected=True, lastStatusMessage=None, isDisabled=False)
            self.data[kind].append(result)
        elif method == 'PATCH':
            result = next(r for r in self.data[kind] if r['uuid'] == body['uuid'])
            result.update(copy.deepcopy(body))
            if kind == 'internal-squads':
                result['inbounds'] = [{'uuid': i} for i in body['inbounds']]
        else:
            raise AssertionError('Unexpected fake API operation')
        if self.lose_after == (method, path):
            self.lose_after = None
            raise d.DeployError('Fake lost response')
        return copy.deepcopy(result)

    def post_count(self):
        return sum(method == 'POST' for method, _, _ in self.calls)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.store = d.Store(self.tmp.name)
        self.api = FakeAPI()
        self.plan = plan()
        self.profile = json.loads((d.HERE / 'profile.json').read_text())
        self.deployment = d.Deployment(self.api, self.store, self.plan, self.profile)

    def setup_hosts(self):
        profile, node, inbound = self.deployment.provision()
        self.deployment.attach(inbound)
        hosts = self.deployment.hosts(profile, node, inbound)
        return hosts

    def test_stage_disabled_then_publish_and_repeat_without_duplicates(self):
        hosts = self.setup_hosts()
        self.assertTrue(all(h['isDisabled'] for h in hosts.values()))
        self.assertEqual(self.api.post_count(), 4)
        self.deployment.publish(hosts)
        self.assertTrue(all(not h['isDisabled'] for h in self.api.rows('hosts')))
        hosts = self.setup_hosts()
        self.deployment.publish(hosts)
        self.assertEqual(self.api.post_count(), 4)

    def test_additive_squads_and_no_other_audience(self):
        self.setup_hosts()
        s1, s2 = self.api.rows('internal-squads')
        suffix = self.plan['id'][:16]
        self.assertEqual(d.ids(s1['inbounds']), ['old-inbound', 'in-direct-grpc-' + suffix, 'in-direct-ws-' + suffix])
        self.assertEqual(s2['inbounds'], [])

    def test_lost_profile_response_adopt_on_resume(self):
        self.api.lose_after = ('POST', '/config-profiles')
        with self.assertRaises(d.DeployError):
            self.deployment.provision()
        self.deployment.provision()
        self.assertEqual(len(self.api.data['config-profiles']), 1)
        self.assertEqual(self.api.post_count(), 2)

    def test_unknown_outcome_and_absent_object_never_repeat_post(self):
        self.api.lose_before = ('POST', '/config-profiles')
        with self.assertRaises(d.DeployError):
            self.deployment.provision()
        with self.assertRaisesRegex(d.DeployError, 'Повторный POST запрещён'):
            self.deployment.provision()
        self.assertEqual(self.api.post_count(), 1)

    def test_lost_host_response_never_duplicate(self):
        profile, node, inbound = self.deployment.provision()
        self.api.lose_after = ('POST', '/hosts')
        with self.assertRaises(d.DeployError):
            self.deployment.hosts(profile, node, inbound)
        self.deployment.hosts(profile, node, inbound)
        self.assertEqual(len(self.api.data['hosts']), 2)
        self.assertEqual(self.api.post_count(), 4)

    def test_lost_enable_response_resumes_without_charging_or_recreation(self):
        hosts = self.setup_hosts()
        self.api.lose_after = ('PATCH', '/hosts')
        with self.assertRaises(d.DeployError):
            self.deployment.publish(hosts)
        hosts = self.setup_hosts()
        self.deployment.publish(hosts)
        self.assertEqual(self.api.post_count(), 4)
        self.assertTrue(self.store.load('complete.json'))

    def test_unowned_collision_is_never_adopted(self):
        profile_name = 'AUTO_' + self.plan['domain'].replace('.', '_')[:16] + '_' + self.plan['id'][:8]
        self.api.data['config-profiles'].append({'uuid': 'foreign', 'name': profile_name})
        with self.assertRaisesRegex(d.DeployError, 'не принадлежит'):
            self.deployment.provision()
        self.assertEqual(self.api.post_count(), 0)

    def test_manual_drift_is_never_overwritten(self):
        self.setup_hosts()
        self.api.data['nodes'][0]['address'] = '9.9.9.9'
        with self.assertRaisesRegex(d.DeployError, 'изменён вручную'):
            self.deployment.provision()
        self.assertEqual(self.api.post_count(), 4)

    def test_deleted_owned_object_is_not_recreated(self):
        self.deployment.provision()
        self.api.data['nodes'].clear()
        with self.assertRaisesRegex(d.DeployError, 'удалён'):
            self.deployment.provision()
        self.assertEqual(self.api.post_count(), 2)

    def test_reordered_inbound_response_does_not_change_intent(self):
        self.deployment.provision()
        self.api.data['config-profiles'][0]['inbounds'].reverse()
        self.deployment.provision()
        self.assertEqual(self.api.post_count(), 2)

    def test_extra_audience_is_reported(self):
        _, _, inbound = self.deployment.provision()
        self.api.data['internal-squads'][1]['inbounds'] = [{'uuid': inbound['direct-ws']}]
        with self.assertRaisesRegex(d.DeployError, 'Аудитория'):
            self.deployment.attach(inbound)

    def test_node_timeout_does_not_create_hosts(self):
        _, node, _ = self.deployment.provision()
        self.api.data['nodes'][0]['isConnected'] = False
        with patch.object(d.time, 'monotonic', side_effect=[0, 10]):
            with self.assertRaisesRegex(d.DeployError, 'не подключилась'):
                self.deployment.wait_ready(node, 5)
        self.assertFalse(self.api.data['hosts'])

    def test_http_rejection_records_only_status_and_payload_hash(self):
        def reject(api, method, path, body=None):
            raise d.PanelHTTPError(409, method, path)
        with patch.object(FakeAPI, '__call__', new=reject):
            with self.assertRaises(d.PanelHTTPError):
                self.deployment.provision()
        intent = self.store.load('profile.intent.json')
        self.assertEqual(self.store.load('profile.rejected.json'), {
            'status': 409, 'method': 'POST', 'path': '/config-profiles', 'payloadHash': d.digest(intent)})
        self.assertIsNone(self.store.load('profile.receipt.json'))


class SafetyTests(unittest.TestCase):
    def args(self, *extra):
        return d.parser().parse_args(['--ip', '8.8.8.8', '--domain', 'node.example.com', *extra])

    def test_plan_is_offline_and_never_reads_credentials(self):
        with patch.object(d, 'read_settings', side_effect=AssertionError('Settings read')), \
             patch.object(d.socket, 'getaddrinfo', side_effect=AssertionError('DNS used')), \
             patch.object(d, 'API', side_effect=AssertionError('API used')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            d.run(self.args())
        self.assertTrue(json.loads(output.getvalue())['planOnly'])

    def test_apply_requires_explicit_certificate_terms(self):
        with patch.object(d, 'read_settings', side_effect=AssertionError('Settings read')):
            with self.assertRaisesRegex(d.DeployError, 'agree-le-tos'):
                d.run(self.args('--apply'))

    def test_invalid_targets_and_ports(self):
        for ip, domain, port in [('127.0.0.1', 'node.example.com', 22),
                                 ('224.0.0.1', 'node.example.com', 22),
                                 ('8.8.8.8', 'x;bad.example.com', 22),
                                 ('8.8.8.8', 'a' * 64 + '.example.com', 22),
                                 ('8.8.8.8', 'node.example.com', 443)]:
            args = self.args()
            args.ip, args.domain, args.ssh_port = ip, domain, port
            with self.assertRaises(d.DeployError):
                d.validate_args(args)

    def test_panel_url_requires_tls_and_no_credentials_or_redirect(self):
        for value in ('http://panel.example.com/api', 'https://token@panel.example.com/api',
                      'https://panel.example.com:444/api', 'https://panel.example.com/api?token=x'):
            with self.assertRaises(d.DeployError):
                d.panel_url(value)
        with self.assertRaises(d.DeployError):
            d.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_api_cannot_access_users_or_delete(self):
        api = d.API('https://panel.example.com/api', 'fake-token')
        with patch.object(api.opener, 'open', side_effect=AssertionError('Network used')):
            for method, path in [('DELETE', '/hosts'), ('POST', '/users'), ('GET', '/hosts/foreign')]:
                with self.assertRaises(d.DeployError):
                    api(method, path)

    def test_api_http_error_preserves_endpoint_without_echoing_body(self):
        api = d.API('https://panel.example.com/api', 'private-api-token')
        error = urllib.error.HTTPError(api.base + '/config-profiles', 409, 'secret-body-message', {},
                                       io.BytesIO(b'{"message":"private-api-token secret-body-message"}'))
        with patch.object(api.opener, 'open', side_effect=error):
            with self.assertRaises(d.PanelHTTPError) as raised:
                api('POST', '/config-profiles', {'config': 'private-data'})
        self.assertEqual(raised.exception.status, 409)
        self.assertIn('POST /config-profiles', str(raised.exception))
        for secret in ('private-api-token', 'secret-body-message', 'private-data'):
            self.assertNotIn(secret, str(raised.exception))

    def test_store_private_immutable_and_locked(self):
        with temporary_directory() as tmp:
            store = d.Store(tmp)
            store.save('key.json', {'value': 'test-secret'})
            self.assertEqual(stat.S_IMODE((Path(tmp) / 'key.json').stat().st_mode), 0o600)
            with self.assertRaises(d.DeployError):
                store.save('key.json', {'value': 'new-secret'})
            with store.lock():
                with self.assertRaises(d.DeployError):
                    with d.Store(tmp).lock():
                        pass
            os.chmod(Path(tmp) / 'key.json', 0o644)
            with self.assertRaises(d.DeployError):
                store.load('key.json')

    def test_store_rejects_symlink(self):
        with temporary_directory() as tmp:
            target = Path(tmp) / 'target'
            target.mkdir(mode=0o700)
            link = Path(tmp) / 'link'
            link.symlink_to(target)
            with self.assertRaises(d.DeployError):
                d.Store(link)

    def test_squads_require_explicit_nonambiguous_selection(self):
        api = FakeAPI()
        with patch.object(d.sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(d.DeployError):
                d.choose_squads(api, [])
        self.assertEqual(d.choose_squads(api, ['Основной']), ['s1'])
        api.data['internal-squads'][1]['name'] = 'Основной'
        with self.assertRaises(d.DeployError):
            d.choose_squads(api, ['Основной'])

    def test_rendered_remote_files_and_only_node_key_uploaded(self):
        files = d.render_files(plan(), 'N' * 64)
        self.assertEqual(set(files), {'managed-node.sh', 'bootstrap.conf', 'nginx.conf',
                                     'docker-compose.yml', 'renew-nginx.sh', 'node.env'})
        nginx = files['nginx.conf'].decode()
        self.assertIn('server_name node.example.com;', nginx)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', nginx)
        self.assertNotIn('$DOMAIN', nginx)
        self.assertNotIn('\\$', nginx)
        self.assertIn(d.IMAGE, files['docker-compose.yml'].decode())
        self.assertIn('spn.deploy.id: "' + plan()['id'] + '"', files['docker-compose.yml'].decode())
        self.assertNotIn(b'REMNAWAVE_API_TOKEN', b'\n'.join(files.values()))
        self.assertEqual(files['node.env'], ('NODE_PORT=2222\nSECRET_KEY=' + 'N' * 64 + '\n').encode())

    def test_remote_script_syntax_and_ownership_guards(self):
        subprocess.run(['bash', '-n', str(d.HERE / 'managed-node.sh')], check=True)
        script = (d.HERE / 'managed-node.sh').read_text()
        for unsafe in ('ufw reset', 'docker system prune', 'StrictHostKeyChecking=no'):
            self.assertNotIn(unsafe, script)
        self.assertIn('Unrelated remnanode container exists', script)
        self.assertIn('Existing node installation is not ours', script)
        self.assertIn('Unowned Nginx site', script)


class IPv6ResolutionTests(unittest.TestCase):
    def rows(self, *addresses):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', (address, 443, 0, 0))
                for address in addresses]

    def test_target_ipv4_mapped_is_not_mistaken_for_aaaa(self):
        for address in ('::ffff:83.219.97.238', '::ffff:53db:61ee'):
            with self.subTest(address=address), \
                 patch.object(d.socket, 'getaddrinfo', return_value=self.rows(address)) as lookup:
                d.check_ipv6_resolution('fi103.wayspn.com', '83.219.97.238')
                lookup.assert_called_once_with('fi103.wayspn.com', 443, socket.AF_INET6, socket.SOCK_STREAM)

    def test_real_ipv6_is_still_blocked_even_alongside_mapped_target(self):
        with patch.object(d.socket, 'getaddrinfo', return_value=self.rows(
                '::ffff:83.219.97.238', '2001:db8::1234')):
            with self.assertRaisesRegex(d.DeployError, '2001:db8::1234'):
                d.check_ipv6_resolution('fi103.wayspn.com', '83.219.97.238')

    def test_mapped_different_ipv4_is_not_ignored(self):
        with patch.object(d.socket, 'getaddrinfo', return_value=self.rows('::ffff:83.219.97.239')):
            with self.assertRaises(d.DeployError):
                d.check_ipv6_resolution('fi103.wayspn.com', '83.219.97.238')

    def test_dns64_is_not_blindly_whitelisted(self):
        with patch.object(d.socket, 'getaddrinfo', return_value=self.rows('64:ff9b::53db:61ee')):
            with self.assertRaisesRegex(d.DeployError, 'синтез IPv6'):
                d.check_ipv6_resolution('fi103.wayspn.com', '83.219.97.238')

    def test_no_ipv6_address_is_allowed(self):
        for code in {socket.EAI_NONAME, getattr(socket, 'EAI_NODATA', socket.EAI_NONAME)}:
            with self.subTest(code=code), patch.object(d.socket, 'getaddrinfo', side_effect=socket.gaierror(code, 'no address')):
                d.check_ipv6_resolution('fi103.wayspn.com', '83.219.97.238')

    def test_transient_dns_error_is_not_treated_as_absent_ipv6(self):
        with patch.object(d.socket, 'getaddrinfo', side_effect=socket.gaierror(socket.EAI_AGAIN, 'retry')):
            with self.assertRaisesRegex(d.DeployError, 'временная ошибка DNS'):
                d.check_ipv6_resolution('fi103.wayspn.com', '83.219.97.238')


class VerificationUpgradeTests(unittest.TestCase):
    def signature(self, managed):
        return d.digest({'profile': json.loads((d.HERE / 'profile.json').read_text()),
                         'installer': (d.HERE / 'spn-direct-setup.sh').read_text(), 'managed': managed})

    def test_allowed_hash_transition_only_changes_read_only_ufw_parser(self):
        current = (d.HERE / 'managed-node.sh').read_text()
        original = re.sub(r'panel_rule_present\(\) \{\n.*?^\}\n\n', '', current, flags=re.M | re.S)
        original = original.replace('ufw status | panel_rule_present',
                                    'ufw status | grep -Eq "^2222/tcp[[:space:]]+ALLOW[[:space:]]+$PANEL_IP[[:space:]]*$"')
        self.assertEqual(d.READ_ONLY_VERIFIER_UPGRADES, {(self.signature(original), self.signature(current))})

    def test_save_plan_preserves_original_and_resumes_recorded_upgrade(self):
        old_hash, new_hash = next(iter(d.READ_ONLY_VERIFIER_UPGRADES))
        old = {**plan(), 'templateHash': old_hash}
        new = {**old, 'templateHash': new_hash}
        with temporary_directory() as tmp:
            store = d.Store(tmp)
            store.save('plan.json', old)
            self.assertEqual(d.save_plan(store, new), old)
            self.assertEqual(d.save_plan(store, new), old)
            self.assertEqual(store.load('plan.json'), old)
            self.assertEqual(store.load('verification-upgrade-' + new_hash + '.json'), {
                'from': old_hash, 'to': new_hash, 'kind': 'read-only-ufw-verification'})

    def test_unknown_template_and_other_plan_changes_are_blocked(self):
        old_hash, new_hash = next(iter(d.READ_ONLY_VERIFIER_UPGRADES))
        old = {**plan(), 'templateHash': old_hash}
        changes = ({'templateHash': 'unknown'}, {'templateHash': new_hash, 'ip': '8.8.4.4'},
                   {'templateHash': new_hash, 'squads': ['s2']}, {'templateHash': new_hash, 'name': 'Different'})
        for change in changes:
            with self.subTest(change=change), temporary_directory() as tmp:
                store = d.Store(tmp)
                store.save('plan.json', old)
                with self.assertRaises(d.DeployError):
                    d.save_plan(store, {**old, **change})
                self.assertEqual(store.load('plan.json'), old)
                self.assertEqual([p.name for p in store.path.iterdir()], ['plan.json'])


class WorkflowTests(unittest.TestCase):
    def test_full_workflow_and_recovery_after_failed_health(self):
        api = FakeAPI()
        events = []

        class FakeSSH:
            def __init__(self, *args):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def upload(self, p, files):
                events.append(('upload', set(files)))
            def action(self, p, action):
                events.append(action)

        def dns(host, port, family, kind):
            if family == socket.AF_INET6:
                # Reproduce the actual macOS false positive through the full
                # workflow, including recovery and the completed-run path.
                return [(family, kind, 6, '', ('::ffff:8.8.8.8', port, 0, 0))]
            ip = '8.8.8.8' if host == 'node.example.com' else '1.1.1.1'
            return [(family, kind, 6, '', (ip, port))]

        def health(p):
            self.assertEqual(len(api.data['hosts']), 2)
            self.assertTrue(all(h['isDisabled'] for h in api.data['hosts']))
            self.assertIn('verify', events)
            raise d.DeployError('Fake public health unavailable')

        with temporary_directory() as tmp, \
             patch.object(d, 'API', return_value=api), \
             patch.object(d, 'SSH', FakeSSH), \
             patch.object(d, 'read_settings', return_value={
                 'REMNAWAVE_BASE_URL': 'https://panel.example.com/api', 'REMNAWAVE_API_TOKEN': 'test'}), \
             patch.object(d.socket, 'getaddrinfo', side_effect=dns), \
             contextlib.redirect_stdout(io.StringIO()):
            args = d.parser().parse_args(['--ip', '8.8.8.8', '--domain', 'node.example.com',
                                         '--squad', 's1', '--state-dir', tmp, '--apply', '--agree-le-tos'])
            with patch.object(d, 'verify_health', side_effect=health):
                with self.assertRaises(d.DeployError):
                    d.run(args)
            self.assertEqual(api.post_count(), 4)
            with patch.object(d, 'verify_health'):
                d.run(args)
            self.assertTrue(all(not h['isDisabled'] for h in api.data['hosts']))
            count = len(events)
            with patch.object(d, 'verify_health', side_effect=AssertionError('Unexpected network test')):
                d.run(args)
            self.assertEqual(len(events), count)  # completed run never reinstalls
            self.assertEqual(api.post_count(), 4)
            self.assertEqual(sum(c[1] == '/keygen' for c in api.calls), 1)


if __name__ == '__main__':
    unittest.main()
