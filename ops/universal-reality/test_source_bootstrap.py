"""Existing guests stay pinned when the controller ships another route helper."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import source_bootstrap as bootstrap


class SourceBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.stage = self.root / 'stage'
        self.stage.mkdir(mode=0o700)
        self.persistent = self.root / 'persistent'
        self.persistent.mkdir(mode=0o700)
        self.original = Path(__file__).with_name('preferred_source.py').read_bytes()
        self.incoming = b'new independently approved helper'
        self.expected = hashlib.sha256(self.incoming).hexdigest()
        self.write(self.stage / bootstrap.HELPER, self.original)
        self.write(self.stage / bootstrap.INCOMING, self.incoming)

    @staticmethod
    def write(path, content):
        path.write_bytes(content)
        path.chmod(0o600)

    def select(self):
        bootstrap.select(self.stage, self.persistent, self.expected)

    def test_previously_staged_but_unplanned_guest_uses_new_helper(self):
        self.select()
        self.assertEqual((self.stage / bootstrap.HELPER).read_bytes(), self.incoming)
        self.assertFalse((self.stage / bootstrap.INCOMING).exists())

    def test_existing_legacy_plan_keeps_original_helper_and_boot_files(self):
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), bootstrap.LEGACY_HASH)
        original_plan = json.dumps({'helperHash': bootstrap.LEGACY_HASH}).encode()
        self.write(self.stage / 'preferred-source-plan.json', original_plan)
        self.write(self.persistent / 'plan.json', original_plan)
        self.write(self.persistent / 'source.py', self.original)
        self.select()
        self.assertEqual((self.stage / bootstrap.HELPER).read_bytes(), self.original)
        self.assertEqual((self.persistent / 'source.py').read_bytes(), self.original)
        self.assertEqual((self.persistent / 'plan.json').read_bytes(), original_plan)

    def test_changed_plan_or_changed_executable_is_not_replaced(self):
        original_plan = json.dumps({'helperHash': bootstrap.LEGACY_HASH}).encode()
        self.write(self.stage / 'preferred-source-plan.json', original_plan)
        self.write(self.persistent / 'plan.json', b'{}')
        with self.assertRaisesRegex(ValueError, 'pins differ'):
            self.select()
        self.write(self.persistent / 'plan.json', original_plan)
        self.write(self.persistent / 'source.py', b'changed')
        with self.assertRaisesRegex(ValueError, 'Persistent preferred-source helper differs'):
            self.select()
        self.assertEqual((self.stage / bootstrap.HELPER).read_bytes(), self.original)

    def test_partial_retarget_with_same_helper_pin_can_resume(self):
        for path, ip in ((self.stage / 'preferred-source-plan.json', '8.8.8.8'),
                         (self.persistent / 'plan.json', '1.1.1.1')):
            self.write(path, json.dumps({'helperHash': bootstrap.LEGACY_HASH, 'target': ip}).encode())
        self.select()
        self.assertEqual((self.stage / bootstrap.HELPER).read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
