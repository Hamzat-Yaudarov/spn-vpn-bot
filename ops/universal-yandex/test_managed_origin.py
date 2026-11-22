"""Actual Bash helper tests; no packages, services, VPS, DNS or firewall writes."""
from pathlib import Path
import re
import shlex
import subprocess
import unittest


HERE = Path(__file__).resolve().parent
PATH = HERE / 'managed-origin.sh'
SCRIPT = PATH.read_text()


def helper(name):
    found = re.findall(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}\n', SCRIPT, re.M | re.S)
    if not found:
        found = re.findall(r'^' + re.escape(name) + r'\(\) \{[^\n]+\}\n', SCRIPT, re.M)
    if len(found) != 1:
        raise AssertionError('Expected exactly one actual helper: ' + name)
    return found[0]


class OriginShellTests(unittest.TestCase):
    def shell(self, names, command, *, data='', prefix='', variables=None, expected=0):
        env = {'SSH_PORT': '22', 'PANEL_IP': '1.1.1.1', 'NODE_IP': '8.8.4.4', 'MONITOR_PORT': ''}
        env.update(variables or {})
        source = 'set -euo pipefail\n' + '\n'.join(k + '=' + shlex.quote(v) for k, v in env.items()) + '\n'
        source += helper('fail') + prefix + '\n' + ''.join(helper(n) for n in names) + '\n' + command + '\n'
        result = subprocess.run(['bash', '-c', source], input=data, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_syntax(self):
        result = subprocess.run(['bash', '-n', str(PATH)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_arguments_fail_before_host_checks(self):
        args = ['preflight', 'origin.example.com', '8.8.4.4', '1.1.1.1', '22', 'a' * 32]
        for index, value in ((0, 'erase'), (1, 'bad;id'), (2, '8.8.4.4/32'), (3, '8.8.4.4'),
                             (4, '443'), (4, '10050'), (4, '65536'), (5, '../secret')):
            with self.subTest(index=index, value=value):
                bad = list(args); bad[index] = value
                p = subprocess.run(['bash', str(PATH), *bad], text=True, capture_output=True)
                self.assertEqual(p.returncode, 1)
                self.assertIn('Stopped:', p.stderr)
                self.assertNotIn('Root is required', p.stderr)
        p = subprocess.run(['bash', str(PATH)], text=True, capture_output=True)
        self.assertEqual(p.returncode, 1)

    def test_ip_and_port_parsers(self):
        for value in ('8.8.4.4', '0.0.0.0', '255.255.255.255'):
            self.shell(['valid_ipv4'], 'valid_ipv4 ' + shlex.quote(value))
        for value in ('1.2.3', '1.2.3.256', '01.2.3.4', '::1', '1;id'):
            self.shell(['valid_ipv4'], 'valid_ipv4 ' + shlex.quote(value), expected=1)
        for value in ('1', '65535'):
            self.shell(['valid_port'], 'valid_port ' + shlex.quote(value))
        for value in ('0', '01', '65536', '443/tcp'):
            self.shell(['valid_port'], 'valid_port ' + shlex.quote(value), expected=1)

    def firewall(self, monitor=False):
        result = ('Added user rules:\nufw allow 22/tcp\nufw allow 80/tcp\nufw allow 443/tcp\n'
                  "ufw allow from 1.1.1.1/32 to any port 2222 proto tcp comment 'SPN-PANEL'\n")
        return result + ('ufw allow 10050/tcp\n' if monitor else '')

    def test_complete_exact_firewall_and_narrow_monitor_exception(self):
        self.shell(['firewall_rules_safe'], 'firewall_rules_safe', data=self.firewall())
        self.shell(['firewall_rules_safe'], 'firewall_rules_safe', data=self.firewall(True), variables={'MONITOR_PORT': '10050'})
        self.shell(['firewall_rules_safe'], 'firewall_rules_safe', data=self.firewall(True), expected=1)

    def test_firewall_rejects_missing_extra_broad_or_duplicate_rules(self):
        cases = [self.firewall().replace('ufw allow 443/tcp\n', ''),
                 self.firewall().replace('from 1.1.1.1/32', 'from any'),
                 self.firewall() + 'ufw allow 2222/tcp\n', self.firewall() + 'ufw allow 10085/tcp\n',
                 self.firewall() + 'ufw allow 80/tcp\n', self.firewall() + 'ufw allow out 53/udp\n',
                 self.firewall() + 'ufw route allow in on eth0\n', self.firewall() + 'ufw allow 2222:2443/tcp\n']
        for data in cases:
            self.shell(['firewall_rules_safe'], 'firewall_rules_safe', data=data, expected=1)

    def test_pending_firewall_permits_only_subsets_and_no_others(self):
        self.shell(['firewall_rules_safe'], 'firewall_rules_safe pending', data='ufw allow 22/tcp\n')
        self.shell(['firewall_rules_safe'], 'firewall_rules_safe pending', data='')
        self.shell(['firewall_rules_safe'], 'firewall_rules_safe pending', data='ufw allow 10085/tcp\n', expected=1)

    def test_fresh_listeners_allow_only_ssh_dhcp_loopback_and_explicit_monitor(self):
        good = ('tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n'
                'tcp LISTEN 0 4096 [::]:22 [::]:*\n'
                'udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*\n'
                'udp UNCONN 0 0 8.8.4.4%eth0:68 0.0.0.0:*\n'
                'udp UNCONN 0 0 [fe80::1]%eth0:546 [::]:*\n')
        for extra, allowed in (('', True), ('tcp LISTEN 0 1 0.0.0.0:80 0.0.0.0:*\n', False),
                               ('udp UNCONN 0 0 [::]:443 [::]:*\n', False),
                               ('tcp LISTEN 0 1 0.0.0.0:10085 0.0.0.0:*\n', False)):
            prefix = 'ss() { printf %s ' + shlex.quote(good + extra) + '; }\n'
            self.shell(['listeners_safe'], 'listeners_safe 0', prefix=prefix, expected=0 if allowed else 1)
        row = 'tcp LISTEN 0 1 0.0.0.0:10050 0.0.0.0:*\n'
        prefix = 'ss() { printf %s ' + shlex.quote(row) + '; }\n'
        self.shell(['listeners_safe'], 'listeners_safe 0', prefix=prefix, expected=1)
        self.shell(['listeners_safe'], 'listeners_safe 0', prefix=prefix, variables={'MONITOR_PORT': '10050'})

    def test_owned_listeners_never_allow_public_xhttp_port(self):
        base = 'tcp LISTEN 0 1 0.0.0.0:443 0.0.0.0:*\ntcp LISTEN 0 1 *:2222 *:*\n'
        for row, code in (('tcp LISTEN 0 1 127.0.0.1:10085 0.0.0.0:*\n', 0),
                          ('tcp LISTEN 0 1 0.0.0.0:10085 0.0.0.0:*\n', 1)):
            prefix = 'ss() { printf %s ' + shlex.quote(base + row) + '; }\n'
            self.shell(['listeners_safe'], 'listeners_safe 1', prefix=prefix, expected=code)

    def test_exact_listener_rejects_wildcards_duplicates_and_missing(self):
        for address, count, code in (('127.0.0.1', 1, 0), ('0.0.0.0', 1, 1), ('[::]', 1, 1),
                                      ('127.0.0.1', 2, 1), ('127.0.0.1', 0, 1)):
            data = ('LISTEN 0 4096 ' + address + ':10085 0.0.0.0:*\n') * count
            prefix = 'ss() { printf %s ' + shlex.quote(data) + '; }\n'
            self.shell(['listener_exact'], 'listener_exact 10085 127.0.0.1', prefix=prefix, expected=code)

    def test_apt_lock_retry_is_bounded_and_nonlock_errors_propagate(self):
        prefix = 'n=0\napt-get() { if [ "$n" -lt 2 ]; then echo "Could not get lock" >&2; return 100; fi; echo done; }\nsleep() { n=$((n+1)); }\n'
        p = self.shell(['apt_update'], 'apt_update; printf "attempts=%s\\n" "$n"', prefix=prefix)
        self.assertIn('attempts=2', p.stdout)
        self.shell(['apt_update'], 'apt_update', prefix='apt-get() { echo broken >&2; return 100; }\n', expected=100)
        self.shell(['apt_update'], 'apt_update', prefix='apt-get() { echo "Unable to acquire lock" >&2; return 100; }\nsleep() { :; }\n', expected=100)

    def test_boot_package_services_are_masked_before_apt_and_no_nat_settings(self):
        self.assertLess(SCRIPT.index('systemctl mask --runtime "$service"'), SCRIPT.index('    apt_update\n'))
        for item in ('"bridge":"none"', '"iptables":false', '"ip6tables":false', '"ip-forward":false', '"ip-masq":false'):
            self.assertIn(item, SCRIPT)
        self.assertNotIn('ufw reset', SCRIPT)
        self.assertNotIn('systemctl restart', SCRIPT)
        self.assertNotIn('wg show', SCRIPT)


if __name__ == '__main__':
    unittest.main()
