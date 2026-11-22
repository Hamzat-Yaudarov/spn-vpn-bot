"""Synthetic, network-free tests for the bounded Nginx config repair."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('limits', Path(__file__).with_name('nginx_limits.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

CONFIG = b'user www-data;\nworker_processes auto;\nevents {\n\tworker_connections 768; # expected\n}\nhttp { include /etc/nginx/sites-enabled/*; }\n'


class Tests(unittest.TestCase):
    def test_only_two_directives(self):
        candidate, soft = m.render(CONFIG)
        self.assertEqual(soft, 8192)
        self.assertEqual(candidate, CONFIG.replace(b'worker_processes auto;\n', b'worker_processes auto;\nworker_rlimit_nofile 8192;\n').replace(b'connections 768;', b'connections 4096;'))

    def test_existing_limit(self):
        for initial, desired in ((1024, 8192), (8192, 8192), (65535, 65535)):
            base = b'worker_rlimit_nofile %d;\n' % initial + CONFIG
            candidate, soft = m.render(base)
            self.assertEqual(soft, desired)
            self.assertTrue(candidate.startswith(b'worker_rlimit_nofile %d;\n' % desired))

    def test_reject_unexpected_or_duplicate(self):
        for base in (CONFIG.replace(b'768', b'4096'), CONFIG + b'worker_connections 768;\n',
                     b'worker_rlimit_nofile 8192;\n' * 2 + CONFIG):
            with self.assertRaises(m.RepairError):
                m.render(base)

    def test_crlf(self):
        rendered, _ = m.render(CONFIG.replace(b'\n', b'\r\n'))
        self.assertNotIn(b'\n', rendered.replace(b'\r\n', b''))

    def test_optimistic_guard_refuses_concurrent_change(self):
        with mock.patch.object(m, 'same', return_value=False), mock.patch.object(m.os, 'replace') as overwrite:
            with self.assertRaisesRegex(m.RepairError, 'concurrent'):
                m.replace(mock.Mock(), ('before', None), b'candidate')
            overwrite.assert_not_called()

    def test_post_install_race_refuses_rollback_snapshot(self):
        meta = SimpleNamespace(st_dev=1, st_ino=2, st_size=9, st_mtime_ns=3, st_mode=0o100644, st_uid=0, st_gid=0)
        staged = mock.Mock()
        staged.lstat.return_value = meta
        with mock.patch.object(m, 'same', return_value=True), mock.patch.object(m.os, 'replace'), \
             mock.patch.object(m, 'snapshot', return_value=(b'concurrent edit', meta)):
            with self.assertRaisesRegex(m.RepairError, 'rollback refused'):
                m.replace(staged, (b'before', None), b'candidate')

    def test_confirm_workers_requires_new_pids(self):
        with mock.patch.object(m, 'workers', side_effect=[{10: 1024}, {11: 8192, 12: 8192}]), \
             mock.patch.object(m.time, 'sleep'):
            self.assertEqual(m.confirm_workers(1, {10: 1024}, 8192), [8192, 8192])


if __name__ == '__main__':
    unittest.main()
