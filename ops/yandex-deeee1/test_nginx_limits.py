"""Offline fixed-target Nginx renderer checks; no commands or network calls."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('nginx_limits_under_test', Path(__file__).with_name('nginx_limits.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


ORIGINAL = b'''user www-data;
worker_processes auto; # preserve worker-selection comment
pid /run/nginx.pid;

events {
    # worker_connections 999; this is a comment, not a second directive
    worker_connections 768; # keep this limit comment
    # multi_accept on;
}

http {
    include /etc/nginx/mime.types;
    include /etc/nginx/sites-enabled/*;
}
'''


class RenderTests(unittest.TestCase):
    def test_changes_only_numeric_connection_value_and_one_inserted_directive(self):
        expected = ORIGINAL.replace(b'worker_processes auto; # preserve worker-selection comment\n',
                                    b'worker_processes auto; # preserve worker-selection comment\nworker_rlimit_nofile 65535;\n')
        expected = expected.replace(b'    worker_connections 768;', b'    worker_connections 8192;')
        self.assertEqual(m.render(ORIGINAL), expected)

    def test_every_original_comment_and_unrelated_line_is_preserved(self):
        candidate = m.render(ORIGINAL)
        for line in ORIGINAL.splitlines():
            if b'worker_connections 768;' in line:
                line = line.replace(b'768', b'8192', 1)
            self.assertIn(line + b'\n', candidate)
        self.assertEqual(candidate.count(b'worker_rlimit_nofile 65535;'), 1)
        self.assertIn(b'# worker_connections 999;', candidate)

    def test_existing_lower_nofile_is_raised_in_place_without_extra_line(self):
        original = ORIGINAL.replace(b'pid /run/nginx.pid;', b'worker_rlimit_nofile 4096; # retain rationale\npid /run/nginx.pid;')
        expected = original.replace(b'worker_connections 768;', b'worker_connections 8192;').replace(
            b'worker_rlimit_nofile 4096;', b'worker_rlimit_nofile 65535;')
        self.assertEqual(m.render(original), expected)
        self.assertEqual(m.render(original).count(b'worker_rlimit_nofile'), 1)

    def test_existing_equal_or_higher_nofile_is_preserved_exactly(self):
        for limit in (65535, 131072, 1048576):
            with self.subTest(limit=limit):
                directive = b'worker_rlimit_nofile\t' + str(limit).encode() + b' ; # keep capacity\n'
                original = directive + ORIGINAL
                self.assertEqual(m.render(original), original.replace(b'worker_connections 768;', b'worker_connections 8192;'))

    def test_commented_nofile_does_not_suppress_insertion(self):
        original = b'# worker_rlimit_nofile 1024; leave inactive\n' + ORIGINAL
        candidate = m.render(original)
        self.assertTrue(candidate.startswith(b'# worker_rlimit_nofile 1024; leave inactive\n'))
        self.assertEqual(candidate.count(b'worker_rlimit_nofile 65535;'), 1)

    def test_whitespace_around_numeric_values_is_not_reformatted(self):
        original = ORIGINAL.replace(b'    worker_connections 768;', b'\tworker_connections\t768 \t;')
        original = b'worker_rlimit_nofile\t4096 \t;\n' + original
        expected = original.replace(b'worker_connections\t768 \t;', b'worker_connections\t8192 \t;').replace(
            b'worker_rlimit_nofile\t4096 \t;', b'worker_rlimit_nofile\t65535 \t;')
        self.assertEqual(m.render(original), expected)

    def test_missing_or_duplicate_active_connection_directive_is_rejected(self):
        cases = (ORIGINAL.replace(b'    worker_connections 768; # keep this limit comment\n', b''),
                 ORIGINAL.replace(b'    worker_connections 768;', b'    worker_connections 768;\n    worker_connections 768;'),
                 ORIGINAL.replace(b'    worker_connections 768;', b'    worker_connections 768;\n    worker_connections 1024;'))
        for original in cases:
            with self.subTest(original=original), self.assertRaises(ValueError):
                m.render(original)

    def test_unsupported_current_connection_limits_are_rejected(self):
        for value in (b'0', b'512', b'1024', b'8192', b'65535', b'auto', b'unlimited'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.render(ORIGINAL.replace(b'worker_connections 768;', b'worker_connections ' + value + b';'))

    def test_candidate_is_not_silently_treated_as_an_original_on_retry(self):
        with self.assertRaises(ValueError):
            m.render(m.render(ORIGINAL))

    def test_duplicate_active_nofile_directives_are_rejected(self):
        for limits in ((1024, 65535), (65535, 65535), (131072, 262144)):
            prefix = b''.join(b'worker_rlimit_nofile ' + str(value).encode() + b';\n' for value in limits)
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                m.render(prefix + ORIGINAL)

    def test_missing_unsupported_or_duplicate_auto_anchor_is_rejected_when_inserting(self):
        for anchor in (b'', b'worker_processes 2;\n', b'# worker_processes auto;\n',
                       b'worker_processes auto;\nworker_processes auto;\n'):
            original = ORIGINAL.replace(b'worker_processes auto; # preserve worker-selection comment\n', anchor)
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                m.render(original)

    def test_existing_nofile_needs_no_worker_processes_edit(self):
        original = b'worker_rlimit_nofile 131072;\n' + ORIGINAL.replace(
            b'worker_processes auto; # preserve worker-selection comment\n', b'worker_processes 4;\n')
        self.assertEqual(m.render(original), original.replace(b'worker_connections 768;', b'worker_connections 8192;'))

    def test_invalid_input_types_empty_and_oversized_configs_are_rejected(self):
        for original in (None, ORIGINAL.decode(), bytearray(ORIGINAL), b'', b'x' * 65537):
            with self.subTest(kind=type(original)), self.assertRaises(ValueError):
                m.render(original)


class PlanTests(unittest.TestCase):
    def test_default_is_pure_no_command_or_config_io_plan(self):
        with (patch('sys.argv', ['nginx_limits.py']),
              patch('sys.stdout', new_callable=io.StringIO) as output,
              patch.object(m, 'apply', side_effect=AssertionError('unexpected apply')) as apply,
              patch.object(m, 'render', side_effect=AssertionError('unexpected render')) as render,
              patch.object(m, 'read_config', side_effect=AssertionError('unexpected config read')) as read,
              patch.object(m, 'command', side_effect=AssertionError('unexpected command')) as command,
              patch.object(m.subprocess, 'run', side_effect=AssertionError('unexpected subprocess')) as subprocess):
            m.main()
        for operation in (apply, render, read, command, subprocess):
            operation.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result, {'planOnly': True, 'network': False, 'target': '194.48.250.102',
                                 'workerConnections': 8192, 'workerNofileMinimum': 65535, 'reloadOnly': True})

    def test_apply_flag_is_the_only_path_to_apply(self):
        with (patch('sys.argv', ['nginx_limits.py', '--apply']),
              patch('sys.stdout', new_callable=io.StringIO),
              patch.object(m, 'apply', return_value={'applied': True}) as apply):
            m.main()
        apply.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
