"""Exercise the actual UFW verifier helper without running UFW or SSH."""
from pathlib import Path
import re
import subprocess
import unittest


HERE = Path(__file__).resolve().parent
PANEL_IP = '77.221.156.108'
HEADER = 'Status: active\n\nTo                         Action      From\n--                         ------      ----\n'


class FirewallVerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = (HERE / 'managed-node.sh').read_text()
        matches = re.findall(r'^panel_rule_present\(\) \{\n.*?^\}\n', script, re.M | re.S)
        if len(matches) != 1:
            raise AssertionError('Expected exactly one real panel_rule_present shell helper')
        cls.command = 'set -euo pipefail\nPANEL_IP=' + PANEL_IP + '\n' + matches[0] + '\npanel_rule_present\n'

    def check_rule(self, text, expected):
        result = subprocess.run(['bash', '-c', self.command], input=text, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0 if expected else 1, result.stderr)
        self.assertEqual(result.stderr, '')

    def test_plain_allow(self):
        self.check_rule(HEADER + '2222/tcp                   ALLOW       ' + PANEL_IP + '\n', True)

    def test_allow_in(self):
        self.check_rule(HEADER + '2222/tcp                   ALLOW IN    ' + PANEL_IP + '\n', True)

    def test_installer_comment(self):
        self.check_rule(HEADER + '2222/tcp                   ALLOW       ' + PANEL_IP + '  # SPN-PANEL\n', True)

    def test_allow_in_comment_and_whitespace(self):
        self.check_rule(HEADER + ' 2222/tcp\tALLOW\tIN\t' + PANEL_IP + '\t# SPN-PANEL\n', True)

    def test_exact_host_cidr(self):
        self.check_rule(HEADER + '2222/tcp ALLOW IN ' + PANEL_IP + '/32 # SPN-PANEL\n', True)

    def test_other_ip_is_rejected(self):
        self.check_rule(HEADER + '2222/tcp ALLOW 77.221.156.109 # SPN-PANEL\n', False)

    def test_regex_dot_lookalike_is_rejected(self):
        self.check_rule(HEADER + '2222/tcp ALLOW 77x221x156x108\n', False)

    def test_extra_digit_in_source_is_rejected(self):
        self.check_rule(HEADER + '2222/tcp ALLOW 77.221.156.1080\n', False)

    def test_source_network_is_not_an_exact_host(self):
        self.check_rule(HEADER + '2222/tcp ALLOW 77.221.156.108/24\n', False)

    def test_other_port_is_rejected(self):
        self.check_rule(HEADER + '22222/tcp ALLOW ' + PANEL_IP + '\n', False)

    def test_outgoing_rule_is_rejected(self):
        self.check_rule(HEADER + '2222/tcp ALLOW OUT ' + PANEL_IP + '\n', False)

    def test_global_rule_does_not_replace_panel_rule(self):
        self.check_rule(HEADER + '2222/tcp ALLOW Anywhere\n', False)

    def test_missing_rule_is_rejected(self):
        self.check_rule(HEADER + '443/tcp ALLOW Anywhere\n', False)

    def test_rule_in_comment_is_not_a_rule(self):
        self.check_rule(HEADER + '# 2222/tcp ALLOW ' + PANEL_IP + '\n', False)

    def test_extra_fields_are_rejected(self):
        self.check_rule(HEADER + '2222/tcp ALLOW ' + PANEL_IP + ' unexpected\n', False)

    def test_actual_valid_rule_after_unrelated_rows(self):
        text = HEADER + '22/tcp ALLOW Anywhere # SSH\n443/tcp ALLOW Anywhere\n'
        text += '2222/tcp ALLOW IN ' + PANEL_IP + ' # SPN-PANEL\n'
        text += '443/tcp (v6) ALLOW Anywhere (v6)\n'
        self.check_rule(text, True)


if __name__ == '__main__':
    unittest.main()
