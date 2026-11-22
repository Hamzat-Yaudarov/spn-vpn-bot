"""Pure/offline tests for actual scoped rw-core FD verification."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core_fd_health as health


ID = '1a2b3c4d' * 4


class FDHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.proc = Path(self.temp.name)
        self.directory = self.proc / '42'
        self.directory.mkdir()
        (self.directory / 'comm').write_text('rw-core\n')
        (self.directory / 'limits').write_text('Max open files            65536                65536                files\n')
        (self.directory / 'stat').write_text('42 (rw-core) S ' + '0 ' * 18 + '100 0\n')
        (self.directory / 'fd').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def read(self, count=100):
        with patch.object(health.os, 'listdir', return_value=['fd'] * count):
            return health.read_core(42, proc=self.proc)

    def test_normal_actual_core_limits_and_count(self):
        value = self.read()
        self.assertEqual(value['fdInUse'], 100)
        self.assertEqual(value['softLimit'], 65536)
        self.assertEqual(value['hardLimit'], 65536)
        self.assertEqual(value['fdFree'], 65436)

    def test_eighty_percent_boundary_rejects_pressure(self):
        self.assertEqual(self.read(52428)['fdInUse'], 52428)
        with self.assertRaisesRegex(health.HealthError, 'FD pressure'):
            self.read(52429)

    def test_actual_observed_exhaustion_is_rejected(self):
        with self.assertRaisesRegex(health.HealthError, '65429/65536 used, 107 free'):
            self.read(65429)

    def test_wrong_or_unlimited_actual_limits_fail_closed(self):
        for line in ('Max open files 1024 65536 files\n', 'Max open files 65536 1048576 files\n',
                     'Max open files unlimited unlimited files\n'):
            with self.subTest(line=line):
                (self.directory / 'limits').write_text(line)
                with self.assertRaises(health.HealthError):
                    self.read()

    def test_process_disappeared_or_unreadable_fails_closed(self):
        (self.directory / 'comm').unlink()
        with self.assertRaisesRegex(health.HealthError, 'disappeared'):
            self.read()

    def test_fd_permission_error_fails_closed(self):
        with patch.object(health.os, 'listdir', side_effect=PermissionError):
            with self.assertRaisesRegex(health.HealthError, 'unreadable'):
                health.read_core(42, proc=self.proc)

    def test_pid_reuse_is_detected(self):
        original = Path.read_text
        counter = [0]

        def changed(path, *args, **kwargs):
            if path == self.directory / 'stat':
                counter[0] += 1
                return '42 (rw-core) S ' + '0 ' * 18 + str(100 + counter[0]) + ' 0\n'
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', new=changed):
            with self.assertRaisesRegex(health.HealthError, 'process changed'):
                self.read()

    def test_non_core_process_is_not_treated_as_core(self):
        (self.directory / 'comm').write_text('node\n')
        with self.assertRaisesRegex(health.HealthError, 'not rw-core'):
            self.read()

    def inspect(self, *, owner=ID, top='PID COMMAND\n42 rw-core\n17 node\n'):
        commands = []

        def run(*args):
            commands.append(args)
            return owner if args[1] == 'inspect' else top

        with patch.object(health.os, 'listdir', return_value=['fd'] * 100):
            value = health.inspect(ID, run=run, proc=self.proc)
        return value, commands

    def test_container_ownership_and_real_pid_scope(self):
        value, commands = self.inspect()
        self.assertEqual(value['corePid'], 42)
        self.assertEqual(commands[1], ('docker', 'top', 'remnanode', '-eo', 'pid,comm'))
        self.assertNotIn('args', repr(commands))

    def test_wrong_container_owner_is_rejected(self):
        with self.assertRaisesRegex(health.HealthError, 'does not belong'):
            self.inspect(owner='0' * 32)

    def test_missing_or_ambiguous_core_is_rejected(self):
        for top in ('PID COMMAND\n17 node\n', 'PID COMMAND\n42 rw-core\n43 rw-core\n'):
            with self.subTest(top=top):
                with self.assertRaisesRegex(health.HealthError, 'exactly one'):
                    self.inspect(top=top)


if __name__ == '__main__':
    unittest.main()
