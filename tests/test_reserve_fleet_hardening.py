"""Owned-journal hardening order; only API/SSH/network boundaries are faked."""
import contextlib
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from spn_reserve_fleet import hardening
from spn_reserve_fleet.enrollment import synchronize
from spn_reserve_fleet.runtime import Runtime


OPS = Path(__file__).resolve().parents[1] / 'ops/universal-reality'
spec = importlib.util.spec_from_file_location('fleet_hardening_change_fixture', OPS / 'test_change_ip.py')
changes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changes)
r, d, h = changes.r, changes.d, changes.h


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = changes.TLSChangeIPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.original = self.fixture.store
        self.plan = self.fixture.plan
        # Recreate the already deployed TLS generation that lacked a website.
        # Fixture files are disposable; production never rewrites these records.
        self.plan.pop('tlsSiteVersion', None)
        path = self.original.path / 'plan.json'
        path.write_text(json.dumps(self.plan))
        profile = self.fixture.api.data['config-profiles'][0]
        old_profile = {'name': profile['name'], 'config': d.tls_profile_config(self.plan['code'], self.plan['domain'])}
        profile['config'] = old_profile['config']
        (self.original.path / 'profile.intent.json').write_text(json.dumps(old_profile))
        receipt = self.original.load('profile.receipt.json')
        receipt['config'] = old_profile['config']
        (self.original.path / 'profile.receipt.json').write_text(json.dumps(receipt))
        self.runtime = Runtime(self.fixture.home / 'fleet')
        self.server = synchronize(self.runtime.store, self.original.path)
        self.runtime.store.add_reserve(self.server['id'], h.NEW)
        self.env_file = self.fixture.home / 'panel.env'
        self.env_file.write_text('REMNAWAVE_BASE_URL=' + h.BASE + '\nREMNAWAVE_API_TOKEN=offline-panel-token\n')
        self.env_file.chmod(0o600)
        self.timeline, self.guest_plans = [], []
        self.identity = {**changes.IDENTITY, 'ownedInstallation': True}
        self.guard_error, self.site_error = None, None
        self.on_site_verify = None
        self.reachable = {h.IP}
        self.fixture.api.calls.clear()
        owner = self

        class SSH:
            def __init__(self, plan, known_hosts):
                self.plan = plan
                owner.guest_plans.append(dict(plan))

            def __enter__(self):
                owner.timeline.append(('ssh', self.plan['ip']))
                return self

            def __exit__(self, *args):
                pass

            def upload(self, files):
                owner.timeline.append(('upload', set(files)))

            def action(self, action, capture=False):
                owner.timeline.append(('action', action))
                if action == 'identity':
                    return json.dumps(owner.identity).encode()
                if action == 'site-install' and owner.site_error:
                    raise owner.site_error
                if action == 'site-verify' and owner.on_site_verify:
                    owner.on_site_verify()

            def assigned_ipv4(self):
                return {h.IP, h.NEW}

        class API:
            def rows(self, kind):
                return owner.fixture.api.rows(kind)

            def __call__(self, method, path, body=None):
                owner.timeline.append(('api', method, path))
                return owner.fixture.api(method, path, body)

        def guard(ssh, active, *, verify=False, **kwargs):
            self.timeline.append(('guard', active, verify))
            if self.guard_error:
                raise self.guard_error
            return {'deployment': self.plan['id'], 'activeIp': active, 'reserveVpnBlocked': True,
                    'scannerFilterEnabled': True, 'bootPersistenceVerified': True}

        def sni(ssh, *, verify=False):
            self.timeline.append(('sni', ssh.plan['ip'], verify))
            return {'sniFiltered': True, 'allowedSni': ssh.plan.get('sni', d.SNI), 'tlsTerminated': False,
                    'validSniHandshakeVerified': True, 'unknownSniRejected': True, 'missingSniRejected': True}

        self.security = types.SimpleNamespace(guard=guard, sni=sni, files=lambda **kwargs: {'node_guard.py': b'# test'})
        self.fake_ssh, self.fake_api = SSH, API()

    @contextlib.contextmanager
    def boundaries(self):
        real_topology = r.topology

        def topology(*args, **kwargs):
            self.timeline.append(('topology',))
            return real_topology(*args, **kwargs)

        with patch.object(hardening, 'replacement_module', return_value=r), \
                patch.object(hardening, 'check_ssh', side_effect=lambda ip, port: ip in self.reachable), \
                patch.object(d, 'local_module', return_value=self.security), \
                patch.object(d, 'API', return_value=self.fake_api) as api, \
                patch.object(d, 'SSH', self.fake_ssh) as ssh, \
                patch.object(d.trust, 'resolve', return_value=self.fixture.home / 'known-hosts') as trust, \
                patch.object(d, 'panel_ipv4', return_value=h.PANEL), \
                patch.object(r, 'topology', side_effect=topology) as audited:
            yield api, ssh, trust, audited

    def apply(self):
        return hardening.harden(self.runtime, self.server['id'], env_file=self.env_file, apply=True)

    def test_dry_run_does_not_load_credentials_take_locks_or_connect(self):
        with patch.object(hardening, 'replacement_module', side_effect=AssertionError('loaded mutation helpers')), \
                patch.object(self.runtime.store, 'server_lock', side_effect=AssertionError('took server lock')), \
                patch.object(d, 'API', side_effect=AssertionError('API')), \
                patch.object(d, 'SSH', side_effect=AssertionError('SSH')):
            value = hardening.harden(self.runtime, self.server['id'], env_file=None)
        self.assertTrue(value['planOnly'])
        self.assertFalse(value['changes'])
        self.assertEqual(value['activeIp'], h.IP)
        self.assertEqual(self.fixture.api.calls, [])

    def test_pending_ip_change_is_rejected_before_api_or_ssh(self):
        self.fixture.start()
        with self.boundaries() as (api, ssh, trust, audited), self.assertRaisesRegex(ValueError, 'незавершённую'):
            self.apply()
        self.assertEqual(api.call_count, 0)
        self.assertEqual(trust.call_count, 0)
        self.assertEqual(audited.call_count, 0)
        self.assertEqual(self.guest_plans, [])
        self.assertEqual(self.fixture.api.calls, [])

    def test_wrong_machine_is_rejected_before_guard_site_or_profile_changes(self):
        self.identity['machineId'] = 'f' * 32
        with self.boundaries(), self.assertRaisesRegex(d.u.DeployError, 'переустановлена'):
            self.apply()
        self.assertFalse(any(row[0] in ('guard', 'sni', 'api') for row in self.timeline))
        self.assertNotIn(('action', 'site-install'), self.timeline)
        self.assertEqual(self.fixture.api.calls, [])
        self.assertEqual(list(self.original.path.glob('security-v1-*.verified.json')), [])

    def test_tls_site_is_installed_before_profile_upgrade_and_verified_before_success(self):
        original_plan = (self.original.path / 'plan.json').read_bytes()
        with self.boundaries() as (_, _, _, audited):
            proof = self.apply()
        self.assertTrue(proof['httpsSiteVerified'])
        self.assertFalse(proof['sniFilterVerified'])
        self.assertEqual(audited.call_count, 2)
        installed = self.timeline.index(('action', 'site-install'))
        changed = self.timeline.index(('api', 'PATCH', '/config-profiles'))
        verified = self.timeline.index(('action', 'site-verify'))
        guard_verified = self.timeline.index(('guard', h.IP, True))
        self.assertLess(self.timeline.index(('guard', h.IP, False)), installed)
        self.assertLess(installed, changed)
        self.assertLess(changed, verified)
        self.assertLess(verified, guard_verified)
        self.assertEqual(self.timeline[-1], ('topology',))
        self.assertEqual((self.original.path / 'plan.json').read_bytes(), original_plan)
        self.assertIsNotNone(self.original.load('tls-site-v1.receipt.json'))
        self.assertEqual(len(list(self.original.path.glob('security-v1-*.verified.json'))), 1)
        self.assertEqual(self.runtime.store.overview()['events'][0]['kind'], 'security_enabled')

    def test_guard_failure_stops_before_site_or_api_profile_change(self):
        self.guard_error = ValueError('injected guard failure')
        with self.boundaries(), self.assertRaisesRegex(ValueError, 'guard failure'):
            self.apply()
        self.assertNotIn(('action', 'site-install'), self.timeline)
        self.assertFalse(any(row[0] == 'api' for row in self.timeline))
        self.assertIsNone(self.original.load('tls-site-v1.intent.json'))
        self.assertEqual(list(self.original.path.glob('security-v1-*.verified.json')), [])

    def test_interrupted_site_upgrade_skips_stale_initial_audit_but_finishes_with_real_audit(self):
        transition, before, after = d.tls_site_transition(self.plan, self.original)
        self.original.save('tls-site-v1.intent.json', transition)
        # An intent was saved before PATCH. The live old profile is deliberately
        # inconsistent with the derived upgraded profile until resume applies it.
        with self.assertRaises(d.u.DeployError):
            r.topology(self.fake_api, self.original, self.plan)
        with self.boundaries() as (_, _, _, audited):
            proof = self.apply()
        self.assertTrue(proof['httpsSiteVerified'])
        self.assertEqual(audited.call_count, 1)
        self.assertEqual(self.timeline[-1], ('topology',))
        self.assertEqual(self.fixture.api.data['config-profiles'][0]['config'], after['config'])

    def test_final_topology_audit_is_mandatory_after_resuming_site_upgrade(self):
        transition, _, _ = d.tls_site_transition(self.plan, self.original)
        self.original.save('tls-site-v1.intent.json', transition)
        self.on_site_verify = lambda: self.fixture.api.data['hosts'][0].update(port=8443)
        with self.boundaries() as (_, _, _, audited), self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(audited.call_count, 1)
        self.assertIn(('action', 'site-verify'), self.timeline)
        self.assertEqual(list(self.original.path.glob('security-v1-*.verified.json')), [])
        self.assertFalse(any(event['kind'] == 'security_enabled' for event in self.runtime.store.overview()['events']))

    def test_reserve_ssh_is_only_management_route_while_guard_keeps_original_active_ip(self):
        self.reachable = {h.NEW}
        with self.boundaries():
            proof = self.apply()
        self.assertEqual(self.guest_plans[0]['ip'], h.NEW)
        self.assertEqual(self.guest_plans[0]['nodeIp'], h.IP)
        self.assertEqual(proof['activeIp'], h.IP)
        self.assertIn(('guard', h.IP, False), self.timeline)
        self.assertIn(('guard', h.IP, True), self.timeline)
        self.assertEqual(self.runtime.store.get_server(self.server['id'])['current_ip'], h.IP)


if __name__ == '__main__':
    unittest.main()
