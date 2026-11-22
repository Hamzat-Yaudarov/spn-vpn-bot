"""Integration of owned CDN journals with the common fleet inventory."""
import importlib.util
from pathlib import Path
import unittest

from spn_reserve_fleet.enrollment import installation_snapshot, synchronize
from spn_reserve_fleet.store import Store


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fleet_cdn_fixtures', ROOT / 'ops/universal-yandex/test_replace.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class CDNEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReplacementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.inventory = Store(self.fixture.home / 'fleet')

    def test_verified_cdn_is_registered_with_client_and_origin_domains(self):
        row = synchronize(self.inventory, self.fixture.store.path)
        self.assertEqual(row['domain'], fixtures.DOMAIN)
        self.assertEqual(row['cdn_domain'], fixtures.CDN)
        self.assertEqual(row['transport'], 'yandex-xhttp-cdn-v1')
        self.assertFalse(row['auto_enabled'])
        self.assertEqual(row['current_ip'], fixtures.OLD)

    def test_pending_and_committed_migrations_use_cdn_chain_and_new_machine(self):
        synchronize(self.inventory, self.fixture.store.path)
        self.fixture.origin_ok = False
        with self.assertRaises(Exception):
            self.fixture.apply()
        pending = installation_snapshot(self.fixture.store.path)
        self.assertEqual(pending['pending_ip'], fixtures.NEW)
        self.assertEqual(pending['current_ip'], fixtures.OLD)
        self.fixture.origin_ok = True
        self.fixture.apply()
        row = synchronize(self.inventory, self.fixture.store.path)
        self.assertEqual(row['current_ip'], fixtures.NEW)
        self.assertIsNone(row['pending_ip'])
        self.assertEqual(row['generation'], 1)
        self.assertEqual(row['ssh_fingerprint'], self.fixture.machine(fixtures.NEW)['sshFingerprint'])

    def test_unfinished_creation_is_not_a_managed_operational_server(self):
        (self.fixture.store.path / 'activation.verified.json').unlink()
        with self.assertRaises(ValueError):
            synchronize(self.inventory, self.fixture.store.path)
        self.assertEqual(self.inventory.list_servers(), [])


if __name__ == '__main__':
    unittest.main()
