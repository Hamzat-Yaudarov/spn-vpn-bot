"""Offline recovery of the exact route transition observed after APT/networkd."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import preferred_source as source
import source_recovery as recovery
from test_preferred_source import FakeHost


ID = '877bd442ab3a4c2ea7c7d35f4cc131b2'
IP = '169.40.47.125'
MACHINE = '1234567890abcdef' * 2
BEFORE = {'dst': 'default', 'gateway': '169.40.47.65', 'dev': 'enp1s0', 'flags': []}
OBSERVED = {**BEFORE, 'protocol': 'static'}


class Host(FakeHost):
    def __init__(self):
        super().__init__()
        self.route = copy.deepcopy(BEFORE)
        self.addresses = [{'ifname': 'enp1s0', 'flags': ['UP'], 'addr_info': [
            {'family': 'inet', 'local': IP, 'prefixlen': 26}]}]
        self.selected = IP

    def run(self, *args):
        if args == ('ip', '-j', '-4', 'route', 'get', '1.1.1.1'):
            self.commands.append(args)
            return json.dumps([{'dev': self.route['dev'], 'gateway': self.route['gateway'],
                                'prefsrc': self.route.get('prefsrc', self.selected)}])
        return super().run(*args)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.root = Path(self.temp.name)
        self.stage = self.root / ('spn-reality-managed-' + ID)
        self.stage.mkdir(mode=0o700)
        (self.root / 'etc/systemd/system').mkdir(parents=True)
        (self.root / 'etc/machine-id').write_text(MACHINE + '\n')
        (self.root / 'etc/machine-id').chmod(0o644)
        node = self.root / 'opt/spn-reality-node'
        node.mkdir(mode=0o700, parents=True)
        (node / '.spn-deployment').write_text(ID + '\n')
        (node / '.spn-deployment').chmod(0o600)
        self.helper = self.stage / 'preferred_source.py'
        self.helper.write_bytes(b'#!/usr/bin/python3\n# unchanged frozen helper fixture\n')
        self.helper.chmod(0o600)
        self.host = Host()
        self.manager = source.PreferredSource(IP, self.stage, root=self.root, helper=self.helper,
                                              run=self.host.run, probe=self.host.probe)
        self.manager.apply()
        self.original = (self.stage / source.PLAN).read_bytes()
        self.unit = self.manager.unit.read_bytes()
        self.host.route = copy.deepcopy(OBSERVED)
        self.host.commands.clear()
        self.host.probes.clear()
        self.recovery = recovery.SourceRecovery(IP, self.stage, MACHINE, recovery.route_digest(OBSERVED),
                                                root=self.root, helper=self.helper,
                                                run=self.host.run, probe=self.host.probe)

    def tearDown(self):
        self.temp.cleanup()

    def test_check_requires_exact_reviewed_case_and_makes_no_writes(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.recovery.check()
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertTrue(result['planOnly'])
        self.assertEqual(before, after)
        self.assertEqual(self.host.mutations(), [])
        self.assertEqual(self.host.probes, [])

    def test_rebases_only_protocol_preserving_original_snapshot_helper_and_unit(self):
        helper = self.helper.read_bytes()
        result = self.recovery.apply()
        plan = json.loads((self.stage / source.PLAN).read_bytes())
        snapshot = json.loads(self.recovery.snapshot_path.read_bytes())
        self.assertEqual(snapshot['oldPlan'].encode(), self.original)
        self.assertEqual(plan['before'], OBSERVED)
        self.assertEqual(plan['after'], {**OBSERVED, 'prefsrc': IP})
        self.assertEqual(self.host.route, plan['after'])
        self.assertEqual(self.manager.unit.read_bytes(), self.unit)
        self.assertEqual(self.helper.read_bytes(), helper)
        self.assertEqual((self.manager.directory / 'source.py').read_bytes(), helper)
        self.assertEqual((self.manager.directory / 'plan.json').read_bytes(), (self.stage / source.PLAN).read_bytes())
        self.assertTrue(result['preferredSourceVerified'])
        self.assertFalse(result['networkRestarted'])
        self.assertTrue(self.manager.verify()['bootPersistenceVerified'])

    def test_completed_retry_only_verifies_and_never_reapplies_route(self):
        first = self.recovery.apply()
        self.host.commands.clear()
        self.assertEqual(self.recovery.apply(), first)
        self.assertEqual(self.host.mutations(), [('systemctl', 'is-enabled', '--quiet', self.manager.unit_name)])

    def test_partial_two_file_swap_is_resumed_without_changing_saved_old_plan(self):
        actual = recovery.replace_owned
        count = 0
        def interrupted(path, old, new):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('simulated interrupted second plan write')
            actual(path, old, new)
        with patch('source_recovery.replace_owned', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.recovery.apply()
        self.assertNotEqual((self.stage / source.PLAN).read_bytes(), self.original)
        self.assertEqual((self.manager.directory / 'plan.json').read_bytes(), self.original)
        self.recovery.apply()
        self.assertTrue(self.manager.verify()['bootPersistenceVerified'])
        self.assertEqual(json.loads(self.recovery.snapshot_path.read_bytes())['oldPlan'].encode(), self.original)

    def test_apply_rollback_after_systemd_failure_can_resume_and_restore_owned_unit(self):
        self.host.fail_enable = True
        with self.assertRaisesRegex(source.SourceError, 'enable failure'):
            self.recovery.apply()
        self.assertFalse(self.manager.unit.exists())
        self.assertEqual(self.host.route, OBSERVED)
        self.host.fail_enable = False
        self.recovery.apply()
        self.assertEqual(self.manager.unit.read_bytes(), self.unit)
        self.assertTrue(self.manager.verify()['bootPersistenceVerified'])

    def test_refuses_unreviewed_route_digest_before_any_write(self):
        self.recovery.route_sha256 = '0' * 64
        with self.assertRaisesRegex(source.SourceError, 'reviewed'):
            self.recovery.apply()
        self.assertFalse(self.recovery.journal.exists())
        self.assertEqual(self.host.mutations(), [])

    def test_refuses_foreign_gateway_interface_metric_flags_protocol_and_source(self):
        changes = [{'gateway': '169.40.47.66'}, {'dev': 'eth1'}, {'metric': 200},
                   {'flags': ['onlink']}, {'protocol': 'dhcp'}, {'prefsrc': '169.40.47.124'}]
        for fields in changes:
            with self.subTest(fields=fields):
                self.host.route = {**OBSERVED, **fields}
                self.recovery.route_sha256 = recovery.route_digest(self.host.route)
                with self.assertRaises(source.SourceError):
                    self.recovery.apply()
                self.assertFalse(self.recovery.journal.exists())
        self.assertEqual(self.host.mutations(), [])

    def test_refuses_different_machine_node_helper_or_unit(self):
        cases = [(self.root / 'etc/machine-id', b'0' * 32 + b'\n'),
                 (self.root / 'opt/spn-reality-node/.spn-deployment', b'0' * 32 + b'\n'),
                 (self.manager.directory / 'source.py', b'foreign helper'),
                 (self.manager.unit, b'foreign unit')]
        for path, replacement in cases:
            with self.subTest(path=path):
                old = path.read_bytes()
                path.write_bytes(replacement)
                with self.assertRaises(source.SourceError):
                    self.recovery.apply()
                self.assertFalse(self.recovery.journal.exists())
                path.write_bytes(old)
        self.assertEqual(self.host.mutations(), [])

    def test_refuses_changes_after_pending_recovery(self):
        with patch('source_recovery.replace_owned', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.recovery.apply()
        self.host.route['metric'] = 10
        with self.assertRaisesRegex(source.SourceError, 'outside this recovery'):
            self.recovery.apply()
        self.assertEqual((self.stage / source.PLAN).read_bytes(), self.original)

    def test_refuses_unjournaled_half_plan_write(self):
        new = {**json.loads(self.original), 'before': OBSERVED, 'after': {**OBSERVED, 'prefsrc': IP}}
        (self.stage / source.PLAN).write_bytes(source.json_bytes(new))
        with self.assertRaisesRegex(source.SourceError, 'must match'):
            self.recovery.apply()
        self.assertFalse(self.recovery.journal.exists())

    def test_refuses_changed_original_source_or_protocol(self):
        original = json.loads(self.original)
        for changed in ({**original, 'beforeSource': '169.40.47.124'},
                        {**original, 'before': {**BEFORE, 'protocol': 'dhcp'},
                         'after': {**BEFORE, 'protocol': 'dhcp', 'prefsrc': IP}}):
            with self.subTest(changed=changed):
                for path in (self.stage / source.PLAN, self.manager.directory / 'plan.json'):
                    path.write_bytes(source.json_bytes(changed))
                with self.assertRaises(source.SourceError):
                    self.recovery.apply()
                self.assertFalse(self.recovery.journal.exists())

    def test_refuses_malformed_or_changed_snapshot(self):
        with patch('source_recovery.replace_owned', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.recovery.apply()
        snapshot = json.loads(self.recovery.snapshot_path.read_bytes())
        for changed in ({**snapshot, 'machineId': '0' * 32},
                        {**snapshot, 'observedRoute': []},
                        {**snapshot, 'newPlan': {**snapshot['newPlan'], 'target': '169.40.47.124'}}):
            with self.subTest(changed=changed):
                self.recovery.snapshot_path.write_bytes(source.json_bytes(changed))
                with self.assertRaises(source.SourceError):
                    self.recovery.apply()
        self.assertEqual((self.stage / source.PLAN).read_bytes(), self.original)

    def test_refuses_foreign_route_after_completed_recovery(self):
        self.recovery.apply()
        self.host.route['gateway'] = '169.40.47.66'
        self.host.commands.clear()
        with self.assertRaises(source.SourceError):
            self.recovery.apply()
        self.assertEqual(self.host.mutations(), [])

    def test_private_journal_and_plan_permissions(self):
        self.recovery.apply()
        self.assertEqual(self.recovery.journal.stat().st_mode & 0o777, 0o700)
        for file in self.recovery.journal.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.stage / source.PLAN).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.manager.directory / 'plan.json').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
