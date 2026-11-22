"""Real owned deployment journals + SQLite; panel, SSH, DNS and VPN are fake."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spn_reserve_fleet.enrollment import discover, installation_snapshot, synchronize
from spn_reserve_fleet.store import Store


OPS = Path(__file__).resolve().parents[1] / 'ops/universal-reality'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


deployment = module('reserve_enrollment_deployment_fakes', 'test_deploy.py')
changes = module('reserve_enrollment_change_fakes', 'test_change_ip.py')


class EnrollmentTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, transport='reality'):
        fixture = changes.TLSChangeIPTests() if transport == 'tls' else changes.ChangeIPTests()
        fixture.setUp()
        try:
            yield fixture
        finally:
            fixture.tearDown()

    def test_completed_deploy_calls_real_registration_for_tls_and_legacy_reality(self):
        d = deployment.d
        for transport in ('tls', 'reality'):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                state, fleet = root / 'deployment', root / 'fleet'
                api = deployment.FakeAPI()
                deployment.FakeSSH.calls = []
                deployment.FakeSSH.fresh = True
                deployment.FakeSSH.extra_addresses = set()
                deployment.FakeSSH.on_action = None

                class FakeDNS:
                    def __init__(self, token):
                        pass

                    def find_zone(self, name):
                        return {'id': 'zone123', 'name': name, 'name_servers': ['alice.ns.cloudflare.com']}

                    def check_record(self, *args, **kwargs):
                        pass

                    def ensure_record(self, store, *, zone_id, name, ipv4, ttl):
                        intent = {'version': 1, 'zone_id': zone_id, 'name': name, 'content': ipv4,
                                  'type': 'A', 'proxied': False, 'ttl': ttl,
                                  'comment': 'SPN universal-reality:' + 'a' * 32}
                        store.save('dns.intent.json', intent)
                        store.save('dns.receipt.json', {'id': 'record123', **{
                            key: value for key, value in intent.items() if key != 'version'}})

                def traffic(*args, **kwargs):
                    return {'passed': True, 'tests': 9, 'ip': args[0], 'testRoute': 'system',
                            'directReachabilityVerified': False,
                            'results': [{'passed': True, 'clientAlive': True} for _ in range(9)]}

                def guard(ssh, active_ip, **kwargs):
                    return {'deployment': ssh.plan['id'], 'activeIp': active_ip, 'reserveVpnBlocked': True,
                            'scannerFilterEnabled': True, 'bootPersistenceVerified': True}

                def sni(ssh, **kwargs):
                    return {'sniFiltered': True, 'allowedSni': ssh.plan.get('sni', d.SNI), 'tlsTerminated': False,
                            'validSniHandshakeVerified': True, 'unknownSniRejected': True, 'missingSniRejected': True}

                args = d.parser().parse_args([
                    '--ip', deployment.IP, '--domain', 'de-enrollment.wayspn.com',
                    '--name', 'Германия', '--squad', 'regular', '--transport', transport,
                    '--state-dir', str(state), '--fleet-dir', str(fleet),
                    '--test-route', 'system', '--apply'])
                with patch.object(d, 'API', return_value=api), \
                        patch('spn_reserve_fleet.remote.load_controller', return_value=None), \
                        patch.object(d.u, 'read_settings', return_value={
                            'REMNAWAVE_BASE_URL': deployment.BASE, 'REMNAWAVE_API_TOKEN': 'offline-token'}), \
                        patch.object(d, 'cloudflare_token', return_value='offline-cf-token'), \
                        patch.object(d.dns, 'CloudflareDNS', FakeDNS), \
                        patch.object(d, 'verify_authoritative_dns'), \
                        patch.object(d, 'panel_ipv4', return_value=deployment.PANEL), \
                        patch.object(d, 'SSH', deployment.FakeSSH), \
                        patch.object(d.trust, 'resolve', return_value=root / 'known-hosts'), \
                        patch.object(d.probe, 'private_file', return_value=root / 'subscription'), \
                        patch.object(d.probe, 'subscription_url', return_value='https://sub.example.com/private'), \
                        patch.object(d.probe, 'select_engine', return_value=root / 'xray'), \
                        patch.object(d.probe, 'validate_interface'), \
                        patch.object(d.probe, 'run', side_effect=traffic), \
                        patch.object(d.probe, 'verify_export', return_value={'passed': True}), \
                        patch.object(d.security, 'guard', side_effect=guard), \
                        patch.object(d.security, 'sni', side_effect=sni), \
                        contextlib.redirect_stdout(io.StringIO()):
                    d.run(args)

                server = Store(fleet).get_server(args.domain)
                self.assertIsNotNone(server)
                self.assertEqual(server['current_ip'], deployment.IP)
                self.assertEqual(server['transport'], 'raw-' + transport + '-vision-safari-v1')
                self.assertFalse(server['auto_enabled'])
                self.assertEqual(server['generation'], 0)
                self.assertEqual(server['ssh_fingerprint'], 'SHA256:' + 'A' * 43)
                self.assertEqual(len(Store(fleet).overview()['events']), 1)
                self.assertEqual(server['reserves'], [])
                self.assertEqual((state / 'reality.private.json').exists(), transport == 'reality')

    def test_ssh_only_committed_address_is_adopted_without_changing_original_plan(self):
        for transport in ('tls', 'reality'):
            with self.subTest(transport=transport), self.fixture(transport) as fixture:
                store = Store(fixture.home / 'fleet')
                original = (fixture.store.path / 'plan.json').read_bytes()
                first = synchronize(store, fixture.store.path)
                store.add_reserve(first['id'], changes.h.NEW)
                store.set_auto(first['id'], True)
                fixture.apply(extra=['--ssh-only', '--non-interactive'])
                updated = synchronize(store, fixture.store.path)
                self.assertEqual(updated['current_ip'], changes.h.NEW)
                self.assertEqual(updated['generation'], 1)
                self.assertIsNone(updated['pending_ip'])
                self.assertTrue(updated['auto_enabled'])
                self.assertEqual([item['ip'] for item in updated['reserves']], [changes.h.IP])
                self.assertEqual((fixture.store.path / 'plan.json').read_bytes(), original)
                # A delayed initial snapshot cannot move inventory backwards.
                with self.assertRaises(ValueError):
                    store.register(first)
                self.assertEqual(store.get_server(first['id'])['current_ip'], changes.h.NEW)

    def test_pending_journal_disables_existing_automation_and_preserves_reserves(self):
        with self.fixture('tls') as fixture:
            store = Store(fixture.home / 'fleet')
            server = synchronize(store, fixture.store.path)
            store.add_reserve(server['id'], changes.h.NEW)
            store.set_auto(server['id'], True)
            fixture.start()
            result = discover(store, home=fixture.home)
            self.assertEqual(result, {'imported': 1, 'errors': []})
            current = store.get_server(server['id'])
            self.assertEqual(current['current_ip'], changes.h.IP)
            self.assertEqual(current['pending_ip'], changes.h.NEW)
            self.assertEqual(current['status'], 'pending')
            self.assertFalse(current['auto_enabled'])
            self.assertEqual([item['ip'] for item in current['reserves']], [changes.h.NEW])

    def test_synchronize_holds_original_lock_until_inventory_publication(self):
        with self.fixture() as fixture:
            store = Store(fixture.home / 'fleet')
            register = store.register
            observed = []

            def register_under_lock(installation):
                with self.assertRaises(changes.d.u.DeployError):
                    with fixture.store.lock():
                        self.fail('Original journal lock was released before inventory publication')
                observed.append(installation['current_ip'])
                return register(installation)

            with patch.object(store, 'register', side_effect=register_under_lock):
                synchronize(store, fixture.store.path)
            self.assertEqual(observed, [changes.h.IP])

    def test_incomplete_installation_is_not_imported(self):
        with self.fixture() as fixture:
            (fixture.store.path / 'activation.verified.json').unlink()
            store = Store(fixture.home / 'fleet')
            self.assertEqual(discover(store, home=fixture.home), {'imported': 0, 'errors': []})
            self.assertEqual(store.list_servers(), [])
            with self.assertRaises(ValueError):
                installation_snapshot(fixture.store.path)


if __name__ == '__main__':
    unittest.main()
