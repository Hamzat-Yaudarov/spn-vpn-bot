"""Exact reviewed scanner fix; interruption recovery never edits firewall policy."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import node_guard_upgrade as upgrade


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('node_guard_upgrade_fixtures', ROOT / 'tests/test_node_guard.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
g = upgrade.g
OLD = b'# reviewed old scanner downloader\n'
NEW = b'# reviewed fixed scanner downloader\n'


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = helpers.NodeGuardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.manager.install(helpers.ACTIVE, helpers.PANEL, helpers.CONTROLLER)
        self.kernel = self.fixture.kernel
        self.source = self.fixture.root / 'stage'
        self.source.mkdir(mode=0o700)
        for name, raw in self.fixture.manager.source_files().items():
            path = self.source / name
            path.write_bytes(OLD if name == 'scanner_guard.py' else raw)
            path.chmod(0o600)
        self.manager = g.NodeGuard(helpers.ID, root=self.fixture.root, run=self.kernel.run, source_dir=self.source)
        state = self.manager.state()
        scanner = self.manager.library / 'scanner_guard.py'
        scanner.write_bytes(OLD)
        scanner.chmod(0o644)
        state['codeHashes']['scanner_guard.py'] = upgrade.digest(OLD)
        self.manager.config.write_bytes(g.encode(state))
        (self.source / 'scanner_guard.py').write_bytes(NEW)
        self.patches = [patch.object(upgrade, 'OLD_SCANNER_HASH', upgrade.digest(OLD)),
                        patch.object(upgrade, 'NEW_SCANNER_HASH', upgrade.digest(NEW))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.job = upgrade.Upgrade(helpers.ID, manager=self.manager)
        self.initial_state = copy.deepcopy(state)
        self.initial_kernel = copy.deepcopy(self.kernel.table)
        self.initial_batches = len(self.kernel.batches)
        self.initial_units = {path: path.read_bytes() for path in self.manager.persistence_files()}

    def assert_policy_unchanged(self):
        state = self.manager.state()
        self.assertEqual(state['current'], self.initial_state['current'])
        self.assertEqual(state['pending'], self.initial_state['pending'])
        self.assertEqual(self.kernel.table, self.initial_kernel)
        self.assertEqual(len(self.kernel.batches), self.initial_batches)
        self.assertEqual({path: path.read_bytes() for path in self.initial_units}, self.initial_units)

    def test_exact_upgrade_updates_only_scanner_code_hash_and_private_receipt(self):
        result = self.job.run()
        self.assertTrue(result['upgraded'])
        self.assertTrue(result['kernelPolicyUnchanged'])
        self.assertEqual((self.manager.library / 'scanner_guard.py').read_bytes(), NEW)
        self.assertEqual(self.manager.state()['codeHashes']['scanner_guard.py'], upgrade.digest(NEW))
        self.manager.verify_persistence(self.manager.state())
        self.assert_policy_unchanged()
        self.assertEqual(self.job.directory.stat().st_mode & 0o777, 0o700)
        for path in self.job.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.job.run()['upgraded'])

    def test_absent_guard_state_is_noop_without_creating_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = g.NodeGuard(helpers.ID, root=Path(temporary).resolve(), run=self.kernel.run, source_dir=self.source)
            result = upgrade.Upgrade(helpers.ID, manager=manager).run()
            self.assertEqual(result['reason'], 'not-installed')
            self.assertFalse(manager.directory.exists())

    def test_current_source_without_upgrade_journal_allows_missing_kernel_recovery(self):
        state = self.manager.state()
        state['codeHashes']['scanner_guard.py'] = upgrade.digest(NEW)
        self.manager.config.write_bytes(g.encode(state))
        (self.manager.library / 'scanner_guard.py').write_bytes(NEW)
        self.kernel.table = None
        result = self.job.run()
        self.assertEqual(result['reason'], 'already-current')
        self.assertFalse(result['upgraded'])
        self.assertIsNone(self.kernel.table)
        self.assertEqual(len(self.kernel.batches), self.initial_batches)
        self.manager.install(helpers.ACTIVE, helpers.PANEL, helpers.CONTROLLER)
        self.assertEqual(self.kernel.table, self.initial_kernel)

    def test_completed_upgrade_noop_allows_missing_kernel_recovery(self):
        self.job.run()
        self.kernel.table = None
        before_batches = len(self.kernel.batches)
        self.assertFalse(self.job.run()['upgraded'])
        self.assertIsNone(self.kernel.table)
        self.assertEqual(len(self.kernel.batches), before_batches)
        self.manager.install(helpers.ACTIVE, helpers.PANEL, helpers.CONTROLLER)
        self.assertEqual(self.kernel.table, self.initial_kernel)

    def test_current_source_pending_first_apply_noop_allows_empty_kernel(self):
        state = self.manager.state()
        state['codeHashes']['scanner_guard.py'] = upgrade.digest(NEW)
        state['pending'] = {'before': None, 'after': state['current']}
        state['current'] = None
        self.manager.config.write_bytes(g.encode(state))
        (self.manager.library / 'scanner_guard.py').write_bytes(NEW)
        self.kernel.table = None
        self.assertFalse(self.job.run()['upgraded'])
        self.assertIsNone(self.kernel.table)
        self.manager.install(helpers.ACTIVE, helpers.PANEL, helpers.CONTROLLER)
        self.assertEqual(self.kernel.table, self.initial_kernel)

    def test_old_source_cannot_be_upgraded_with_unconfirmed_missing_kernel(self):
        self.kernel.table = None
        with self.assertRaisesRegex(g.GuardError, 'kernel policy'):
            self.job.run()
        self.assertEqual((self.manager.library / 'scanner_guard.py').read_bytes(), OLD)
        self.assertEqual(self.manager.state()['codeHashes']['scanner_guard.py'], upgrade.digest(OLD))
        self.assertFalse(self.job.directory.exists())

    def test_interruption_after_source_copy_resumes_saved_pair(self):
        with patch.object(self.manager, 'save_state', side_effect=RuntimeError('lost connection')):
            with self.assertRaisesRegex(RuntimeError, 'lost connection'):
                self.job.run()
        self.assertEqual((self.manager.library / 'scanner_guard.py').read_bytes(), NEW)
        self.assertEqual(self.manager.state()['codeHashes']['scanner_guard.py'], upgrade.digest(OLD))
        self.assertFalse((self.job.directory / 'complete.json').exists())
        self.assertTrue(self.job.run()['upgraded'])
        self.assert_policy_unchanged()

    def test_pending_firewall_transition_is_preserved_without_being_applied(self):
        state = self.manager.state()
        state['pending'] = {'before': copy.deepcopy(state['current']),
                            'after': {**state['current'], 'activeIp': helpers.NEW}}
        self.manager.config.write_bytes(g.encode(state))
        self.initial_state = copy.deepcopy(state)
        self.assertTrue(self.job.run()['upgraded'])
        self.assert_policy_unchanged()

    def test_interruption_after_state_commit_resumes_without_recopying_code(self):
        real_write = g.atomic_write
        def interrupted(path, *args, **kwargs):
            if path.name == 'complete.json':
                raise RuntimeError('lost completion response')
            return real_write(path, *args, **kwargs)
        with patch.object(g, 'atomic_write', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'completion'):
                self.job.run()
        self.assertEqual(self.manager.state()['codeHashes']['scanner_guard.py'], upgrade.digest(NEW))
        self.assertFalse(self.job.run()['upgraded'])
        self.assertTrue((self.job.directory / 'complete.json').exists())
        self.assert_policy_unchanged()

    def test_unknown_source_saved_hash_or_other_code_cannot_be_adopted(self):
        for variant in ('staged-scanner', 'saved-scanner', 'installed-scanner', 'staged-node', 'installed-list'):
            with self.subTest(variant=variant):
                paths = [self.source / 'scanner_guard.py', self.manager.config,
                         self.manager.library / 'scanner_guard.py', self.source / 'node_guard.py',
                         self.manager.library / 'scanner_lists.json']
                before = {path: path.read_bytes() for path in paths}
                if variant == 'saved-scanner':
                    state = self.manager.state()
                    state['codeHashes']['scanner_guard.py'] = 'f' * 64
                    self.manager.config.write_bytes(g.encode(state))
                else:
                    target = paths[('staged-scanner', 'saved-scanner', 'installed-scanner', 'staged-node', 'installed-list').index(variant)]
                    target.write_bytes(b'# unknown source\n')
                with self.assertRaises(g.GuardError):
                    self.job.run()
                self.assertFalse(self.job.directory.exists())
                for path, raw in before.items():
                    path.write_bytes(raw)
        self.assert_policy_unchanged()

    def test_symlinked_source_is_rejected_and_kernel_never_changes(self):
        path = self.source / 'scanner_guard.py'
        path.unlink()
        other = self.source / 'other.py'
        other.write_bytes(NEW)
        other.chmod(0o600)
        path.symlink_to(other)
        with self.assertRaisesRegex(g.GuardError, 'Unsafe'):
            self.job.run()
        self.assert_policy_unchanged()

    def test_unit_or_kernel_changes_stop_before_code_mutation(self):
        self.kernel.table[-1]['rule']['comment'] = 'outside'
        with self.assertRaisesRegex(g.GuardError, 'kernel policy'):
            self.job.run()
        self.assertEqual((self.manager.library / 'scanner_guard.py').read_bytes(), OLD)
        self.kernel.table = copy.deepcopy(self.initial_kernel)
        path = next(iter(self.initial_units))
        path.write_bytes(b'# foreign unit\n')
        with self.assertRaisesRegex(g.GuardError, 'persistence'):
            self.job.run()
        self.assertEqual((self.manager.library / 'scanner_guard.py').read_bytes(), OLD)

    def test_tampered_upgrade_intent_or_receipt_never_authorizes_changes(self):
        self.job.run()
        path = self.job.directory / 'complete.json'
        value = json.loads(path.read_bytes())
        value['intentHash'] = 'e' * 64
        path.write_bytes(g.encode(value))
        with self.assertRaisesRegex(g.GuardError, 'receipt differs'):
            self.job.run()
        self.assert_policy_unchanged()

    def test_tampered_intent_refuses_resume_after_copy_interruption(self):
        with patch.object(self.manager, 'save_state', side_effect=RuntimeError('lost connection')):
            with self.assertRaises(RuntimeError):
                self.job.run()
        path = self.job.directory / 'intent.json'
        value = json.loads(path.read_bytes())
        value['oldScannerHash'] = 'd' * 64
        path.write_bytes(g.encode(value))
        with self.assertRaisesRegex(g.GuardError, 'intent differs'):
            self.job.run()
        self.assertEqual(self.manager.state()['codeHashes']['scanner_guard.py'], upgrade.digest(OLD))
        self.assert_policy_unchanged()


if __name__ == '__main__':
    unittest.main()
