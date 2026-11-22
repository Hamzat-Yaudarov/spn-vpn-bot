"""Local transport migrations preserve the controller identity and host pin."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spn_reserve_fleet.controller_transport import ssh_transport_options


class ControllerTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / 'transport.json'
        self.controller = {'host': '147.45.164.28', 'port': 22, 'user': 'root'}
        self.route = {'host': '147.45.164.28', 'port': 22, 'connect_host': '129.101.97.242'}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, routes=None, **changes):
        value = {'format': 1, 'controllers': [self.route] if routes is None else routes, **changes}
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def test_migration_changes_destination_and_retains_old_host_pin_and_identity(self):
        self.write()
        before = copy.deepcopy(self.controller)
        self.assertEqual(ssh_transport_options(self.controller, self.path),
                         ['-o', 'HostName=129.101.97.242', '-o', 'HostKeyAlias=147.45.164.28'])
        self.assertEqual(self.controller, before)

    def test_default_is_optional_but_explicit_missing_file_is_error(self):
        with patch('spn_reserve_fleet.controller_transport.DEFAULT_TRANSPORT_CONFIG', str(self.path)):
            self.assertEqual(ssh_transport_options(self.controller), [])
        with self.assertRaises(ValueError):
            ssh_transport_options(self.controller, self.path)

    def test_unmatched_controller_or_port_remains_unrouted(self):
        self.write()
        for changed in ({'host': 'other.example'}, {'port': 2222}):
            self.assertEqual(ssh_transport_options({**self.controller, **changed}, self.path), [])

    def test_nonstandard_port_retains_original_known_hosts_lookup_name(self):
        self.write([{**self.route, 'port': 2222}])
        options = ssh_transport_options({**self.controller, 'port': 2222}, self.path)
        self.assertEqual(options, ['-o', 'HostName=129.101.97.242',
                                   '-o', 'HostKeyAlias=[147.45.164.28]:2222'])

    def test_optional_bind_interface_applies_only_to_matching_route(self):
        self.write([{**self.route, 'bind_interface': 'en0'}])
        self.assertEqual(ssh_transport_options(self.controller, self.path),
                         ['-o', 'HostName=129.101.97.242', '-o', 'HostKeyAlias=147.45.164.28',
                          '-o', 'BindInterface=en0'])
        self.assertEqual(ssh_transport_options({**self.controller, 'host': 'other.example'}, self.path), [])

    def test_bind_interface_rejects_option_injection_or_invalid_names(self):
        for interface in ('en0 -o ProxyCommand=evil', '-oProxyCommand=evil', 'en0\nProxyCommand=evil',
                          '', 'x' * 33, 123, True, None):
            with self.subTest(interface=interface):
                self.write([{**self.route, 'bind_interface': interface}])
                with self.assertRaises(ValueError):
                    ssh_transport_options(self.controller, self.path)

    def test_rejects_unsafe_file_and_symlink_parent(self):
        self.write()
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            ssh_transport_options(self.controller, self.path)
        self.path.chmod(0o600)
        linked = self.root / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            ssh_transport_options(self.controller, linked / self.path.name)
        hardlink = self.root / 'hardlink'
        os.link(self.path, hardlink)
        with self.assertRaises(ValueError):
            ssh_transport_options(self.controller, self.path)

    def test_rejects_malformed_or_ambiguous_routes_and_options(self):
        for routes in ([self.route, self.route],
                       [{**self.route, 'connect_host': 'good -o ProxyCommand=evil'}],
                       [{**self.route, 'port': True}],
                       [{**self.route, 'host': '../host'}],
                       [{**self.route, 'option': 'StrictHostKeyChecking=no'}]):
            with self.subTest(routes=routes):
                self.write(routes)
                with self.assertRaises(ValueError):
                    ssh_transport_options(self.controller, self.path)
        self.write(format=True)
        with self.assertRaises(ValueError):
            ssh_transport_options(self.controller, self.path)


if __name__ == '__main__':
    unittest.main()
