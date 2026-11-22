"""Offline preferred-source lifecycle tests: fake root, commands and HTTPS only."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

import preferred_source as source

_spec = importlib.util.spec_from_file_location('preferred_source_policy_tested',
    Path(__file__).with_name('preferred_source_policy.py'))
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


ID = 'abcd1234' * 4
IP = '87.251.16.31'
OLD = '87.251.16.30'
BEFORE = {'dst': 'default', 'gateway': '10.0.0.1', 'dev': 'ens3', 'protocol': 'dhcp',
          'metric': 100, 'flags': ['onlink']}


class FakeHost:
    def __init__(self):
        self.route = copy.deepcopy(BEFORE)
        self.rules = [{'priority': p, 'src': 'all', 'table': t}
                      for p, t in ((0, 'local'), (32766, 'main'), (32767, 'default'))]
        self.addresses = [{'ifname': 'ens3', 'flags': ['UP'], 'addr_info': [
            {'family': 'inet', 'local': IP, 'prefixlen': 32}]}]
        self.commands, self.probes = [], []
        self.fail_bound = self.fail_after = False
        self.fail_enable = self.concurrent_after_probe = False
        self.route_count = 1

    def run(self, *args):
        self.commands.append(args)
        if args[:3] == ('ip', '-j', '-4'):
            query = args[3:]
            if query == ('address', 'show', 'scope', 'global'):
                return json.dumps(self.addresses)
            if query == ('rule', 'show'):
                return json.dumps(self.rules)
            if query == ('route', 'show', 'table', 'main', 'default'):
                return json.dumps([copy.deepcopy(self.route)] * self.route_count)
            if query == ('route', 'get', '1.1.1.1'):
                return json.dumps([{'dev': self.route['dev'], 'gateway': self.route['gateway'],
                                    'prefsrc': self.route.get('prefsrc', OLD)}])
        if args[:4] == ('ip', '-4', 'route', 'change'):
            if 'src' in args:
                self.route['prefsrc'] = args[args.index('src') + 1]
            else:
                self.route.pop('prefsrc', None)
            return ''
        if args[0] == 'systemctl':
            if self.fail_enable and args[1] == 'enable':
                raise source.SourceError('fake enable failure')
            return ''
        raise AssertionError('Unexpected fake command ' + repr(args))

    def probe(self, expected, *, bind=False):
        self.probes.append((expected, bind))
        if bind and self.fail_bound:
            raise source.SourceError('fake bound HTTPS failure')
        if not bind and self.concurrent_after_probe:
            self.route['metric'] = 200
        if not bind and self.fail_after:
            raise source.SourceError('fake post HTTPS failure')

    def mutations(self):
        return [c for c in self.commands if c[:4] == ('ip', '-4', 'route', 'change') or c[0] == 'systemctl']


class PreferredSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.root = Path(self.temp.name)
        self.stage = self.root / ('spn-reality-managed-' + ID)
        self.stage.mkdir(mode=0o700)
        (self.root / 'etc/systemd/system').mkdir(parents=True)
        self.helper = self.stage / 'preferred_source.py'
        self.helper.write_bytes(b'#!/usr/bin/python3\n# fake owned helper\n')
        self.helper.chmod(0o600)
        self.host = FakeHost()
        self.manager = source.PreferredSource(IP, self.stage, root=self.root, helper=self.helper,
                                              run=self.host.run, probe=self.host.probe)

    def tearDown(self):
        self.temp.cleanup()

    def test_check_reports_mismatch_without_writes_or_https(self):
        files = sorted(p.name for p in self.stage.iterdir())
        result = self.manager.check()
        self.assertTrue(result['needsChange'])
        self.assertEqual(result['currentSource'], OLD)
        self.assertEqual(self.host.mutations(), [])
        self.assertEqual(self.host.probes, [])
        self.assertEqual(sorted(p.name for p in self.stage.iterdir()), files)
        self.assertFalse(self.manager.directory.exists())

    def test_check_already_selected_source_is_not_rejected(self):
        self.host.route['prefsrc'] = IP
        self.assertFalse(self.manager.check()['needsChange'])

    def test_apply_preserves_gateway_interface_metric_protocol_flags(self):
        result = self.manager.apply()
        self.assertTrue(result['preferredSourceVerified'])
        self.assertEqual(self.host.route, {**BEFORE, 'prefsrc': IP})
        mutations = [c for c in self.host.commands if c[:4] == ('ip', '-4', 'route', 'change')]
        self.assertEqual(mutations, [('ip', '-4', 'route', 'change', 'default', 'via', '10.0.0.1',
                                     'dev', 'ens3', 'proto', 'dhcp', 'metric', '100', 'src', IP, 'onlink')])
        self.assertEqual(self.host.probes, [(IP, True), (IP, False)])

    def test_durable_original_and_self_contained_boot_config(self):
        self.manager.apply()
        plan = json.loads((self.stage / source.PLAN).read_text())
        self.assertEqual(plan['before'], BEFORE)
        self.assertEqual((self.manager.directory / 'plan.json').read_bytes(), (self.stage / source.PLAN).read_bytes())
        self.assertEqual((self.manager.directory / 'source.py').read_bytes(), self.helper.read_bytes())
        unit = self.manager.unit.read_text()
        self.assertIn('/opt/spn-reality-network/' + ID + '/source.py apply', unit)
        self.assertNotIn(str(self.stage), unit)
        self.assertIn('After=network-online.target', unit)
        self.assertNotIn('--now', unit)

    def test_private_persistence_permissions(self):
        self.manager.apply()
        self.assertEqual(self.manager.directory.stat().st_mode & 0o777, 0o700)
        for file in (self.stage / source.PLAN, self.manager.directory / 'plan.json', self.manager.directory / 'source.py'):
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.unit.stat().st_mode & 0o777, 0o644)

    def test_bound_source_failure_changes_nothing(self):
        self.host.fail_bound = True
        with self.assertRaisesRegex(source.SourceError, 'bound HTTPS'):
            self.manager.apply()
        self.assertEqual(self.host.route, BEFORE)
        self.assertFalse((self.stage / source.PLAN).exists())
        self.assertEqual(self.host.mutations(), [])

    def test_postcheck_failure_rolls_back_exact_original_route(self):
        self.host.fail_after = True
        with self.assertRaisesRegex(source.SourceError, 'post HTTPS'):
            self.manager.apply()
        self.assertEqual(self.host.route, BEFORE)
        self.assertFalse(self.manager.unit.exists())
        self.assertEqual(json.loads((self.stage / source.PLAN).read_text())['before'], BEFORE)

    def test_unit_enable_failure_removes_owned_unit_and_rolls_back(self):
        self.host.fail_enable = True
        with self.assertRaisesRegex(source.SourceError, 'enable failure'):
            self.manager.apply()
        self.assertEqual(self.host.route, BEFORE)
        self.assertFalse(self.manager.unit.exists())
        self.assertIn(('systemctl', 'disable', self.manager.unit_name), self.host.commands)

    def test_retry_after_failure_reuses_backup_and_does_not_duplicate(self):
        self.host.fail_after = True
        with self.assertRaises(source.SourceError):
            self.manager.apply()
        original = (self.stage / source.PLAN).read_bytes()
        self.host.fail_after = False
        self.manager.apply()
        self.manager.apply()
        self.assertEqual((self.stage / source.PLAN).read_bytes(), original)
        self.assertEqual(len(list((self.root / 'etc/systemd/system').glob('*.service'))), 1)

    def test_boot_copy_can_reapply_without_staging_directory(self):
        self.manager.apply()
        self.host.route = copy.deepcopy(BEFORE)
        boot = source.PreferredSource(IP, self.manager.directory, root=self.root,
                                      helper=self.manager.directory / 'source.py', run=self.host.run, probe=self.host.probe)
        boot.apply()
        self.assertEqual(self.host.route, {**BEFORE, 'prefsrc': IP})
        self.assertTrue(boot.verify()['bootPersistenceVerified'])

    def test_verify_has_no_fs_or_route_mutations(self):
        self.manager.apply()
        before = list(self.host.mutations())
        self.manager.verify()
        # is-enabled is a read-only systemctl command, not a route/unit mutation.
        self.assertEqual(self.host.mutations()[len(before):], [('systemctl', 'is-enabled', '--quiet', self.manager.unit_name)])

    def test_explicit_rollback_is_idempotent_and_keeps_private_backup(self):
        self.manager.apply()
        self.assertTrue(self.manager.rollback()['rolledBack'])
        self.assertFalse(self.manager.unit.exists())
        self.assertEqual(self.host.route, BEFORE)
        self.assertFalse(self.manager.rollback()['changed'])
        self.assertTrue((self.stage / source.PLAN).exists())

    def test_concurrent_route_change_is_never_overwritten(self):
        self.manager.apply()
        self.host.route['metric'] = 200
        with self.assertRaisesRegex(source.SourceError, 'Concurrent route'):
            self.manager.rollback()
        self.assertEqual(self.host.route['metric'], 200)

    def test_postcheck_parallel_change_is_not_rolled_over(self):
        self.host.concurrent_after_probe = True
        with patch.object(source.sys, 'stderr', new=io.StringIO()):
            with self.assertRaisesRegex(source.SourceError, 'changed during HTTPS'):
                self.manager.apply()
        self.assertEqual(self.host.route['metric'], 200)

    def test_foreign_unit_is_not_replaced(self):
        self.manager.unit.write_text('foreign unit\n')
        self.manager.unit.chmod(0o644)
        with self.assertRaisesRegex(source.SourceError, 'unrelated persistence'):
            self.manager.apply()
        self.assertEqual(self.manager.unit.read_text(), 'foreign unit\n')
        self.assertEqual(self.host.route, BEFORE)

    def test_changed_owned_unit_is_not_removed(self):
        self.manager.apply()
        self.manager.unit.write_text('changed unit\n')
        with self.assertRaisesRegex(source.SourceError, 'unit changed'):
            self.manager.rollback()
        self.assertTrue(self.manager.unit.exists())

    def test_unassigned_down_or_ambiguous_target_is_rejected(self):
        original = copy.deepcopy(self.host.addresses)
        for addresses in ([], [{**original[0], 'flags': []}], original * 2):
            with self.subTest(addresses=addresses):
                self.host.addresses = addresses
                with self.assertRaisesRegex(source.SourceError, 'assigned once'):
                    self.manager.check()
        self.assertEqual(self.host.mutations(), [])

    def test_policy_routing_and_multiple_default_routes_are_rejected(self):
        self.host.rules.append({'priority': 100, 'src': IP, 'table': '100'})
        with self.assertRaisesRegex(source.SourceError, 'Policy routing'):
            self.manager.apply()
        self.host.rules.pop()
        self.host.route_count = 2
        with self.assertRaisesRegex(source.SourceError, 'exactly one'):
            self.manager.apply()
        self.assertEqual(self.host.mutations(), [])

    def test_wrong_interface_or_unknown_route_attributes_are_rejected(self):
        for route in ({**BEFORE, 'dev': 'eth1'}, {**BEFORE, 'nexthops': []}, {**BEFORE, 'flags': ['linkdown']}):
            with self.subTest(route=route):
                self.host.route = route
                with self.assertRaises(source.SourceError):
                    self.manager.check()

    def test_saved_plan_is_bound_to_target_and_deployment(self):
        self.manager.apply()
        other = source.PreferredSource('87.251.16.32', self.stage, root=self.root, helper=self.helper,
                                       run=self.host.run, probe=self.host.probe)
        with self.assertRaisesRegex(source.SourceError, 'another target'):
            other.plan()

    def test_state_and_plan_symlinks_are_rejected(self):
        link = self.root / ('spn-reality-managed-' + '1' * 32)
        link.symlink_to(self.stage, target_is_directory=True)
        with self.assertRaisesRegex(source.SourceError, 'Unsafe state'):
            source.PreferredSource(IP, link, root=self.root)
        (self.stage / source.PLAN).symlink_to(self.helper)
        with self.assertRaises(OSError):
            self.manager.plan()

    def test_world_readable_state_or_plan_is_rejected(self):
        self.stage.chmod(0o755)
        with self.assertRaisesRegex(source.SourceError, '0700'):
            source.PreferredSource(IP, self.stage, root=self.root)
        self.stage.chmod(0o700)
        self.manager.apply()
        (self.stage / source.PLAN).chmod(0o644)
        with self.assertRaisesRegex(source.SourceError, 'Unsafe owned file'):
            self.manager.plan()

    def test_static_layout_is_preserved_without_manager_config_edits(self):
        self.host.route = {'dst': 'default', 'gateway': '10.0.0.1', 'dev': 'ens3', 'flags': ['onlink'], 'prefsrc': OLD}
        before = copy.deepcopy(self.host.route)
        self.manager.apply()
        self.manager.rollback()
        self.assertEqual(self.host.route, before)
        self.assertFalse((self.root / 'etc/network').exists())
        self.assertFalse((self.root / 'etc/netplan').exists())

    def test_no_reboot_network_reload_or_address_deletion_commands(self):
        self.manager.apply()
        self.manager.verify()
        self.manager.rollback()
        for command in self.host.commands:
            self.assertNotIn('reboot', command)
            self.assertNotIn('restart', command)
            self.assertNotIn('netplan', command)
            self.assertNotIn('ifdown', command)
            self.assertNotIn('flush', command)
            self.assertNotIn('del', command)

    def test_https_pre_and_post_checks_force_ipv4_and_verified_tls(self):
        for bind, host, status, body in ((True, '1.1.1.1', 200, ('ip='+IP+'\n').encode()),
                                         (False, 'www.gstatic.com', 204, b'')):
            with self.subTest(bind=bind):
                raw = MagicMock()
                raw.getsockname.return_value = (IP, 12345)
                context = MagicMock()
                context.wrap_socket.return_value = raw
                response = MagicMock(status=status)
                response.read.return_value = body

                def request(connection, *args, **kwargs):
                    connection.connect()

                with patch.object(source.socket, 'getaddrinfo', return_value=[
                        (source.socket.AF_INET, source.socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]) as resolve, \
                        patch.object(source.socket, 'socket', return_value=raw) as create, \
                        patch.object(source.ssl, 'create_default_context', return_value=context) as tls, \
                        patch.object(source.http.client.HTTPSConnection, 'request', new=request), \
                        patch.object(source.http.client.HTTPSConnection, 'getresponse', return_value=response):
                    source.https_probe(IP, bind=bind)
                resolve.assert_called_once_with(host, 443, source.socket.AF_INET, source.socket.SOCK_STREAM)
                create.assert_called_once_with(source.socket.AF_INET, source.socket.SOCK_STREAM)
                tls.assert_called_once_with()
                context.wrap_socket.assert_called_once_with(raw, server_hostname=host)
                raw.settimeout.assert_called_once_with(5)
                if bind:
                    raw.bind.assert_called_once_with((IP, 0))
                else:
                    raw.bind.assert_not_called()
                raw.close.assert_called_once_with()

    def test_https_dns_failure_is_sanitized(self):
        def request(connection, *args, **kwargs):
            connection.connect()

        with patch.object(source.socket, 'getaddrinfo', side_effect=source.socket.gaierror('private DNS detail')), \
                patch.object(source.http.client.HTTPSConnection, 'request', new=request):
            with self.assertRaisesRegex(source.SourceError, 'Chosen IPv4 HTTPS check failed') as error:
                source.https_probe(IP)
        self.assertNotIn('private DNS detail', str(error.exception))


class PolicyHost(FakeHost):
    def __init__(self):
        super().__init__()
        self.tables, self.selected_overrides = {}, {}

    def run(self, *args):
        if args[:3] == ('ip', '-j', '-4'):
            query = args[3:]
            if len(query) == 4 and query[:3] == ('route', 'show', 'table'):
                self.commands.append(args)
                return json.dumps(self.tables.get(query[3], []))
            if len(query) == 3 and query[:2] == ('route', 'get'):
                self.commands.append(args)
                return json.dumps(self.selected_overrides.get(query[2], [
                    {'dev': self.route['dev'], 'gateway': self.route['gateway'],
                     'prefsrc': self.route.get('prefsrc', OLD)}]))
        try:
            return super().run(*args)
        except source.SourceError as error:
            raise policy.SourceError(str(error)) from None

    def probe(self, expected, *, bind=False):
        try:
            return super().probe(expected, bind=bind)
        except source.SourceError as error:
            raise policy.SourceError(str(error)) from None


class PreferredSourcePolicyTests(unittest.TestCase):
    def setUp(self):
        PreferredSourceTests.setUp(self)
        self.host = PolicyHost()
        self.manager = policy.PreferredSource(IP, self.stage, root=self.root, helper=self.helper,
                                              run=self.host.run, probe=self.host.probe)
        self.supplemental()

    tearDown = PreferredSourceTests.tearDown

    def supplemental(self):
        self.host.route = {**BEFORE, 'prefsrc': IP}
        self.host.addresses = [{'ifname': 'ens3', 'flags': ['UP'], 'addr_info': [
            {'family': 'inet', 'local': address, 'prefixlen': 32}
            for address in (IP, '87.251.16.32', '87.251.16.33')]}]
        self.host.rules = [
            {'priority': 0, 'src': 'all', 'table': 'local'},
            {'priority': 30001, 'src': '87.251.16.32', 'table': '30001', 'protocol': 'static'},
            {'priority': 30002, 'src': '87.251.16.33/32', 'table': 30002, 'protocol': 'static'},
            {'priority': 32766, 'src': 'all', 'table': 'main'},
            {'priority': 32767, 'src': 'all', 'table': 'default'}]
        self.host.tables = {str(table): [{'dst': 'default', 'gateway': '10.0.0.65', 'dev': 'ens3',
            'table': table, 'protocol': 'static', 'flags': ['onlink']}]
            for table in (30001, 30002)}
        self.host.selected_overrides = {}

    def assert_no_installation(self):
        self.assertEqual(self.host.mutations(), [])
        self.assertFalse((self.stage / policy.PLAN).exists())
        self.assertFalse(self.manager.directory.exists())

    def test_provider_extra_rules_are_read_only_and_main_target_is_proven_twice(self):
        rules, tables = copy.deepcopy(self.host.rules), copy.deepcopy(self.host.tables)
        self.assertFalse(self.manager.check()['needsChange'])
        self.assert_no_installation()
        self.assertEqual(self.host.probes, [])
        for destination in ('1.1.1.1', '8.8.8.8'):
            self.assertIn(('ip', '-j', '-4', 'route', 'get', destination), self.host.commands)
        self.assertEqual(self.host.rules, rules)
        self.assertEqual(self.host.tables, tables)

    def test_apply_verify_rollback_preserve_all_provider_rules_and_tables(self):
        rules, tables, addresses = (copy.deepcopy(value) for value in
                                   (self.host.rules, self.host.tables, self.host.addresses))
        before = copy.deepcopy(self.host.route)
        self.assertTrue(self.manager.apply()['preferredSourceVerified'])
        self.assertTrue(self.manager.verify()['bootPersistenceVerified'])
        self.manager.rollback()
        self.assertEqual(self.host.route, before)
        self.assertEqual(self.host.rules, rules)
        self.assertEqual(self.host.tables, tables)
        self.assertEqual(self.host.addresses, addresses)
        self.assertFalse(any(command[:4] == ('ip', '-4', 'rule', 'add')
                             or command[:4] == ('ip', '-4', 'rule', 'del') for command in self.host.commands))

    def test_already_selected_main_ip_with_implicit_source_can_gain_and_rollback_prefsrc(self):
        self.host.route.pop('prefsrc')
        self.host.selected_overrides = {destination: [{'dev': 'ens3', 'gateway': BEFORE['gateway'], 'prefsrc': IP}]
                                       for destination in ('1.1.1.1', '8.8.8.8')}
        before = copy.deepcopy(self.host.route)
        self.manager.apply()
        self.assertEqual(self.host.route['prefsrc'], IP)
        self.manager.rollback()
        self.assertEqual(self.host.route, before)
        self.assertEqual(json.loads((self.stage / policy.PLAN).read_text())['beforeSource'], IP)

    def test_three_standard_rules_keep_original_retargeting_behavior(self):
        self.host = PolicyHost()
        self.manager = policy.PreferredSource(IP, self.stage, root=self.root, helper=self.helper,
                                              run=self.host.run, probe=self.host.probe)
        self.manager.apply()
        self.assertEqual(self.host.route['prefsrc'], IP)
        self.manager.rollback()
        self.assertEqual(self.host.route, BEFORE)

    def test_forbidden_or_ambiguous_rule_attributes_are_rejected_without_mutations(self):
        replacements = [
            {'src': IP}, {'src': '87.251.16.32/24'}, {'src': '10.0.0.2'},
            {'src': '87.251.16.99'}, {'src': 'all'}, {'protocol': 'boot'},
            {'priority': 0}, {'priority': 32766}, {'priority': True},
            {'table': 'main'}, {'table': 'local'}, {'table': 'default'},
            {'table': '030001'}, {'table': 0}, {'table': 253}, {'table': 254}, {'table': 255},
            {'table': 4294967296}, {'fwmark': '0x1'}, {'dst': 'all'},
            {'suppress_prefixlength': 0}, {'action': 'blackhole'}, {'iif': 'ens3'}]
        for changes in replacements:
            with self.subTest(changes=changes):
                self.supplemental()
                self.host.rules[1].update(changes)
                with self.assertRaises(policy.SourceError):
                    self.manager.apply()
                self.assert_no_installation()

    def test_missing_extra_fields_and_duplicate_priorities_sources_tables_rejected(self):
        variants = []
        for missing in ('src', 'table', 'protocol', 'priority'):
            row = copy.deepcopy(self.host.rules[1]); row.pop(missing)
            variants.append(row)
        variants.extend([{**self.host.rules[1], 'priority': 30002},
                         {**self.host.rules[1], 'src': '87.251.16.33'},
                         {**self.host.rules[1], 'table': '30002'}])
        for row in variants:
            with self.subTest(rule=row):
                self.supplemental()
                self.host.rules[1] = row
                with self.assertRaises(policy.SourceError):
                    self.manager.apply()
                self.assert_no_installation()

    def test_down_other_interface_or_duplicate_assignment_of_extra_is_rejected(self):
        for case in ('down', 'other-interface', 'duplicate'):
            with self.subTest(case=case):
                self.supplemental()
                extra = self.host.addresses[0]['addr_info'].pop(1)
                row = {'ifname': 'eth1' if case == 'other-interface' else 'ens3',
                       'flags': [] if case == 'down' else ['UP'], 'addr_info': [extra]}
                self.host.addresses.append(row)
                if case == 'duplicate':
                    self.host.addresses.append(copy.deepcopy(row))
                with self.assertRaises(policy.SourceError):
                    self.manager.apply()
                self.assert_no_installation()

    def test_auxiliary_table_requires_one_simple_same_interface_onlink_default(self):
        original = copy.deepcopy(self.host.tables['30001'][0])
        variants = [[], [original, original], [{**original, 'dst': '0.0.0.0/1'}],
                    [{**original, 'dev': 'eth1'}], [{**original, 'flags': []}],
                    [{**original, 'flags': ['onlink', 'linkdown']}],
                    [{**original, 'table': 30002}], [{**original, 'table': 'main'}],
                    [{**original, 'type': 'blackhole'}], [{**original, 'nexthops': []}],
                    [{**original, 'prefsrc': IP}], [{**original, 'gateway': 'not-an-ip'}]]
        for routes in variants:
            with self.subTest(routes=routes):
                self.host.tables['30001'] = routes
                with self.assertRaises(policy.SourceError):
                    self.manager.apply()
                self.assert_no_installation()

    def test_both_destinations_must_select_target_source_and_main_gateway(self):
        for destination in ('1.1.1.1', '8.8.8.8'):
            for changes in ({'prefsrc': OLD}, {'dev': 'eth1'}, {'gateway': '10.0.0.65'}, {'table': 30001}):
                with self.subTest(destination=destination, changes=changes):
                    self.host.selected_overrides = {destination: [{
                        'dev': 'ens3', 'gateway': BEFORE['gateway'], 'prefsrc': IP, **changes}]}
                    with self.assertRaises(policy.SourceError):
                        self.manager.apply()
                    self.assert_no_installation()

    def test_provider_rules_cannot_be_used_to_rotate_a_different_main_source(self):
        self.host.route['prefsrc'] = OLD
        with self.assertRaisesRegex(policy.SourceError, 'must already use'):
            self.manager.apply()
        self.assert_no_installation()

    def test_postcheck_failure_rolls_back_only_owned_main_source_and_unit(self):
        self.host.route.pop('prefsrc')
        self.host.selected_overrides = {destination: [{'dev': 'ens3', 'gateway': BEFORE['gateway'], 'prefsrc': IP}]
                                       for destination in ('1.1.1.1', '8.8.8.8')}
        before, rules, tables = (copy.deepcopy(value) for value in
                                (self.host.route, self.host.rules, self.host.tables))
        self.host.fail_after = True
        with self.assertRaisesRegex(policy.SourceError, 'post HTTPS'):
            self.manager.apply()
        self.assertEqual(self.host.route, before)
        self.assertEqual(self.host.rules, rules)
        self.assertEqual(self.host.tables, tables)
        self.assertFalse(self.manager.unit.exists())

    def test_changed_provider_rule_stops_verify_before_https_or_rollback_write(self):
        self.manager.apply()
        mutations, probes = list(self.host.mutations()), list(self.host.probes)
        self.host.rules[1]['src'] = IP
        with self.assertRaises(policy.SourceError):
            self.manager.verify()
        with self.assertRaises(policy.SourceError):
            self.manager.rollback()
        self.assertEqual(self.host.mutations(), mutations)
        self.assertEqual(self.host.probes, probes)


if __name__ == '__main__':
    unittest.main()
