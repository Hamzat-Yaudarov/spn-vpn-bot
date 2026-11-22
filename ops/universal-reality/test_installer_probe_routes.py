"""Real ingress validation for the Linux controller's system/lo CLI pair."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


creation = load('probe_route_creation_fixture', HERE / 'test_deploy.py')
replacement = load('probe_route_replacement_fixture', HERE / 'test_tls_replace.py')


class CreationIngressRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = creation.InstallerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.d = creation.d

    def test_linux_system_and_ssh_tls_creation_accept_controller_lo(self):
        for route in ('system', 'ssh'):
            with self.subTest(route=route):
                args = self.fixture.args('--transport', 'tls', '--domain', 'tls.wayspn.com',
                                         '--interface', 'lo', '--test-route', route, '--apply')
                with patch.object(self.d.probe.platform, 'system', return_value='Linux'), \
                        patch.object(self.d.probe, 'validate_interface', wraps=self.d.probe.validate_interface) as validation:
                    self.fixture.install(args)
                validation.assert_called_with('lo', physical=False)
                self.assertEqual(self.fixture.probe_route_calls[-1], route)

    def test_mac_physical_and_auto_keep_physical_interface_requirement(self):
        for route in ('physical', 'auto'):
            with self.subTest(route=route):
                args = self.fixture.args('--interface', 'lo', '--test-route', route, '--apply')
                with patch.object(self.d.probe.platform, 'system', return_value='Darwin'), \
                        self.assertRaises(self.d.u.DeployError):
                    self.fixture.install(args)
                self.assertEqual(creation.FakeSSH.calls, [])
                self.assertEqual(self.fixture.api.calls, [])

    def test_mac_physical_and_auto_still_accept_en0(self):
        for route in ('physical', 'auto'):
            with self.subTest(route=route):
                args = self.fixture.args('--interface', 'en0', '--test-route', route, '--apply')
                with patch.object(self.d.probe.platform, 'system', return_value='Darwin'), \
                        patch.object(self.d.probe, 'validate_interface', wraps=self.d.probe.validate_interface) as validation:
                    self.fixture.install(args)
                validation.assert_called_with('en0', physical=True)
                self.assertEqual(self.fixture.probe_route_calls[-1], 'physical')


class ReplacementIngressRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = replacement.TLSReplacementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.d = replacement.d

    def test_linux_system_and_ssh_tls_replacement_accept_controller_lo(self):
        # Each fixture has a distinct original journal and target. This uses
        # the actual validator through the ordinary replacement test adapter.
        for route in ('system', 'ssh'):
            with self.subTest(route=route):
                fixture = replacement.TLSReplacementTests()
                fixture.setUp()
                try:
                    with patch.object(self.d.probe.platform, 'system', return_value='Linux'):
                        fixture.apply(route=route, extra=['--interface', 'lo'])
                    commits, pending = replacement.r.chain(fixture.store, fixture.plan)
                    self.assertIsNone(pending)
                    self.assertEqual(commits[-1]['testRoute'], route)
                finally:
                    fixture.tearDown()

    def test_mac_physical_and_auto_reject_lo_before_ssh_or_panel_mutations(self):
        for route in ('physical', 'auto'):
            with self.subTest(route=route):
                with patch.object(self.d.probe.platform, 'system', return_value='Darwin'), \
                        self.assertRaises(self.d.u.DeployError):
                    self.fixture.apply(route=route, extra=['--interface', 'lo'])
                self.assertEqual(replacement.h.FakeSSH.calls, [])
                self.assertEqual(self.fixture.api.calls, [])

    def test_system_route_still_rejects_invalid_interface_syntax(self):
        with self.assertRaises(self.d.u.DeployError):
            self.fixture.apply(route='system', extra=['--interface', 'lo;evil'])
        self.assertEqual(replacement.h.FakeSSH.calls, [])
        self.assertEqual(self.fixture.api.calls, [])


if __name__ == '__main__':
    unittest.main()
