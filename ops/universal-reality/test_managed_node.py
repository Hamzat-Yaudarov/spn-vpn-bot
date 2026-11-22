"""Exercise real shell helpers, without SSH, package installs, Docker or UFW."""
from pathlib import Path
import re
import shlex
import subprocess
import unittest


HERE = Path(__file__).resolve().parent
SCRIPT = (HERE / 'managed-node.sh').read_text()
PANEL_IP = '144.31.170.147'


def helper(name):
    found = re.findall(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}\n', SCRIPT, re.M | re.S)
    if len(found) != 1:
        raise AssertionError('Expected one actual shell helper: ' + name)
    return found[0]


class ShellTests(unittest.TestCase):
    def run_helper(self, name, argument='', data='', expected=True):
        command = 'set -euo pipefail\nPANEL_IP=' + PANEL_IP + '\n' + helper(name)
        command += '\n' + name + (' ' + argument if argument else '') + '\n'
        result = subprocess.run(['bash', '-c', command], input=data, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0 if expected else 1, result.stderr)
        self.assertEqual(result.stderr, '')

    def test_syntax(self):
        result = subprocess.run(['bash', '-n', str(HERE / 'managed-node.sh')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_lets_apt_wait_for_dpkg_lock(self):
        command = 'set -euo pipefail\napt-get() { printf "%s\\n" "$@"; }\n'
        command += helper('apt_install') + '\napt_install haproxy\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DPkg::Lock::Timeout=600', result.stdout)
        self.assertIn('install\n-y\nhaproxy', result.stdout)

    def test_update_waits_only_for_lock_errors_without_killing_or_unlocking(self):
        command = 'set -euo pipefail\nround=0\n'
        command += 'apt-get() { if [ "$round" -lt 2 ]; then echo "E: Could not get lock" >&2; return 100; fi; echo updated; }\n'
        command += 'sleep() { round=$((round+1)); }\n' + helper('apt_update')
        command += '\napt_update\nprintf "round=%s\\n" "$round"\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('updated', result.stdout)
        self.assertIn('round=2', result.stdout)
        self.assertNotIn('kill', helper('apt_update'))
        command = 'set -euo pipefail\napt-get() { echo "E: invalid repository" >&2; return 100; }\n'
        command += 'sleep() { exit 99; }\n' + helper('apt_update') + '\napt_update\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 100)
        self.assertIn('invalid repository', result.stderr)

    def test_ipv4_accepts_plain_addresses(self):
        for address in ('144.31.170.147', '1.1.1.1', '0.0.0.0', '255.255.255.255'):
            with self.subTest(address=address):
                self.run_helper('valid_ipv4', address)

    def test_ipv4_rejects_malformed_or_shell_text(self):
        for address in ('1.2.3', '1.2.3.256', '01.2.3.4', '1.2.3.4.5', '1x2x3x4', '::1', '1.2.3.4/32'):
            with self.subTest(address=address):
                self.run_helper('valid_ipv4', address, expected=False)

    def test_exact_panel_rule_forms(self):
        for rule in (f'2222/tcp ALLOW {PANEL_IP}\n', f'2222/tcp ALLOW IN {PANEL_IP}\n',
                     f'2222/tcp ALLOW IN {PANEL_IP}/32 # SPN-REALITY-PANEL\n'):
            with self.subTest(rule=rule):
                self.run_helper('panel_rule_present', data=rule)

    def test_panel_rule_rejects_wrong_or_extra_fields(self):
        for rule in ('2222/tcp ALLOW 144.31.170.148\n', '22222/tcp ALLOW '+PANEL_IP+'\n',
                     '2222/tcp ALLOW OUT '+PANEL_IP+'\n', '2222/tcp ALLOW '+PANEL_IP+' unexpected\n',
                     '# 2222/tcp ALLOW '+PANEL_IP+'\n', '2222/tcp ALLOW 144x31x170x147\n'):
            with self.subTest(rule=rule):
                self.run_helper('panel_rule_present', data=rule, expected=False)

    def test_broad_panel_rule_rejected_even_with_exact_rule(self):
        for broad in ('2222/tcp ALLOW Anywhere\n', '2222/tcp ALLOW IN 0.0.0.0/0\n',
                      '2222/tcp (v6) ALLOW Anywhere (v6)\n', 'Anywhere ALLOW Anywhere\n',
                      'Anywhere ALLOW IN 10.0.0.0/8\n', '2000:3000/tcp ALLOW Anywhere\n',
                      '2222 ALLOW Anywhere\n', '22,443,2222/tcp ALLOW Anywhere\n',
                      '2222/tcp on eth0 ALLOW Anywhere\n'):
            self.run_helper('panel_rule_present', data='2222/tcp ALLOW '+PANEL_IP+'\n'+broad, expected=False)

    def test_unrelated_port_ranges_and_udp_do_not_poison_panel_check(self):
        for rule in ('2222/udp ALLOW Anywhere\n', '2200:2221/tcp ALLOW Anywhere\n',
                     '2223:3000/tcp ALLOW Anywhere\n', 'Anywhere ALLOW OUT Anywhere\n'):
            self.run_helper('panel_rule_present', data='2222/tcp ALLOW '+PANEL_IP+'\n'+rule)

    def test_default_firewall_policy(self):
        self.run_helper('firewall_defaults_safe', data='Status: active\nDefault: deny (incoming), allow (outgoing), disabled (routed)\n')
        for policy in ('Default: allow (incoming), allow (outgoing), disabled (routed)\n',
                       'Default: deny (incoming), deny (outgoing), disabled (routed)\n',
                       'Status: active\n'):
            self.run_helper('firewall_defaults_safe', data=policy, expected=False)

    def wait_for_listeners(self, ready_after, expected, expected_sleeps):
        command = 'set -euo pipefail\nround=0\nslept=0\nSECONDS=0\n'
        command += f'listeners_public() {{ [ "$round" -ge {ready_after} ]; }}\n'
        command += 'sleep() { round=$((round+1)); slept=$((slept+1)); SECONDS=$round; }\n'
        command += helper('wait_listeners')
        command += '\nif wait_listeners; then status=0; else status=$?; fi\n'
        command += 'printf "%s %s\\n" "$status" "$slept"\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f'{0 if expected else 1} {expected_sleeps}\n')

    def test_listener_wait_is_bounded_without_real_sleep(self):
        self.wait_for_listeners(0, True, 0)
        self.wait_for_listeners(5, True, 5)
        self.wait_for_listeners(45, True, 45)
        self.wait_for_listeners(46, False, 45)

    def test_listener_wait_counts_probe_time_toward_deadline(self):
        command = 'set -euo pipefail\nSECONDS=0\nslept=0\n'
        command += 'listeners_public() { SECONDS=$((SECONDS+3)); return 1; }\n'
        command += 'sleep() { slept=$((slept+1)); SECONDS=$((SECONDS+1)); }\n'
        command += helper('wait_listeners')
        command += '\nif wait_listeners; then status=0; else status=$?; fi\n'
        command += 'printf "%s %s %s\\n" "$status" "$slept" "$SECONDS"\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '1 11 47\n')

    def test_firewall_failure_is_checked_before_listener_wait(self):
        verify = SCRIPT.split('verify)\n', 1)[1]
        self.assertLess(verify.index('firewall_defaults_safe'), verify.index('wait_listeners'))
        self.assertLess(verify.index('panel_rule_present'), verify.index('wait_listeners'))

    def test_other_ipv6_rule_does_not_poison_panel_check(self):
        self.run_helper('panel_rule_present', data='443/tcp (v6) ALLOW Anywhere (v6)\n2222/tcp ALLOW '+PANEL_IP+'\n')

    def test_public_rule_exact_port(self):
        self.run_helper('public_rule_present', '443', data='443/tcp ALLOW Anywhere # clients\n')
        self.run_helper('public_rule_present', '443', data='443/tcp ALLOW IN Anywhere\n')
        self.run_helper('public_rule_present', '443', data='443/tcp ALLOW OUT Anywhere\n', expected=False)
        self.run_helper('public_rule_present', '443', data='2443/tcp ALLOW Anywhere\n', expected=False)

    def test_network_checks_unique_up_address_and_delegates_source_configuration(self):
        self.assertIn('ip -o -4 addr show up scope global', helper('target_network'))
        self.assertIn('[ "$count" = 1 ]', helper('target_network'))
        self.assertNotIn('[ "$source" = "$NODE_IP" ]', helper('target_network'))
        self.assertIn('preferred_source.py', helper('target_network'))
        self.assertNotIn('route replace', SCRIPT)

    def fresh_listener_rows(self, rows, expected):
        command = 'set -euo pipefail\nSSH_PORT=22\n'
        command += 'ss() { printf %s ' + shlex.quote(rows) + '; }\n'
        command += helper('fresh_listeners_safe') + '\nfresh_listeners_safe\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if expected else 1, result.stderr)

    def test_clean_listener_check_allows_ssh_and_local_services(self):
        rows = 'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 4096 [::]:22 [::]:*\n'
        for address in ('127.0.0.1:8080', '127.0.0.53%lo:53', '127.0.0.54:53', '[::1]:8080',
                        '[::ffff:127.0.0.1]:8080'):
            with self.subTest(address=address):
                self.fresh_listener_rows(rows + f'LISTEN 0 4096 {address} 0.0.0.0:*\n', True)

    def test_clean_listener_check_rejects_native_non_loopback_services(self):
        rows = 'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n'
        for address in ('0.0.0.0:80', '0.0.0.0:8080', '177.3.250.75:8080', '[::]:8080', '*:8080'):
            with self.subTest(address=address):
                self.fresh_listener_rows(rows + f'LISTEN 0 4096 {address} 0.0.0.0:*\n', False)

    def test_clean_listener_check_runs_before_any_firewall_enable(self):
        preflight = SCRIPT.split('preflight)\n', 1)[1].split('install)\n', 1)[0]
        self.assertIn('fresh_listeners_safe || fail', preflight)
        self.assertIn('if ! owned_node; then\n        fresh_listeners_safe', preflight)

    def listener(self, port, address, ipv4_ready, expected):
        row = f'LISTEN 0 4096 {address}:{port} 0.0.0.0:*\n'
        command = 'set -euo pipefail\n'
        command += 'ss() { printf %s ' + shlex.quote(row) + '; }\n'
        command += f'ipv4_connect() {{ return {0 if ipv4_ready else 1}; }}\n'
        command += helper('listeners_public') + f'\nlisteners_public {port}\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if expected else 1, result.stderr)

    def test_both_listeners_accept_ipv4_capable_wildcards(self):
        for port in (443, 2222):
            for address in ('*', '[::]'):
                with self.subTest(port=port, address=address):
                    self.listener(port, address, True, True)
            self.listener(port, '0.0.0.0', True, True)

    def test_ipv6_only_and_unreachable_ipv4_wildcards_are_rejected(self):
        for port in (443, 2222):
            for address in ('0.0.0.0', '*', '[::]'):
                with self.subTest(port=port, address=address):
                    self.listener(port, address, False, False)

    def test_loopback_only_listeners_are_rejected_even_if_connection_stub_passes(self):
        for port in (443, 2222):
            for address in ('127.0.0.1', '[::1]'):
                with self.subTest(port=port, address=address):
                    self.listener(port, address, True, False)

    def test_ipv4_probe_uses_fixed_family_timeout_and_no_data(self):
        source = helper('ipv4_connect')
        self.assertIn('socket.socket(socket.AF_INET, socket.SOCK_STREAM)', source)
        self.assertIn('connection.settimeout(0.4)', source)
        self.assertIn('connection.connect((sys.argv[1], int(sys.argv[2])))', source)
        self.assertNotIn('.send', source)
        self.assertNotIn('bindv6only', helper('listeners_public'))

    def test_diagnostics_separate_ports_and_never_print_process_fields(self):
        command = 'set -euo pipefail\n'
        command += 'ss() { case "$*" in *443*) printf "%s\\n" "LISTEN 0 4096 *:443 0.0.0.0:* SECRET_PROCESS_FIELD";; *) return 0;; esac; }\n'
        command += 'ipv4_connect() { [ "$1" = 443 ]; }\n'
        command += helper('diagnose_listeners') + '\ndiagnose_listeners\n'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'TCP443: local IPv4 connection=ok; listener addresses=*:443\n'
                         'TCP2222: local IPv4 connection=failed; listener addresses=none\n')

    def test_contract_preserves_secrets_and_existing_services(self):
        self.assertIn('case "$ACTION" in preflight|packages|install|verify|identity)', SCRIPT)
        self.assertIn('NODE=/opt/spn-reality-node', SCRIPT)
        self.assertIn('STAGE=/root/spn-reality-managed-$DEPLOY_ID', SCRIPT)
        self.assertIn('"$IMAGE|host"', SCRIPT)
        self.assertIn('IMAGE=remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e', SCRIPT)
        self.assertIn('= 65536:65536', SCRIPT)
        self.assertIn('apt_install ca-certificates curl python3 docker.io docker-compose-v2 ufw haproxy', SCRIPT)
        self.assertIn('[ ! -f "$STAGE/packages-ok" ] || ! command -v python3', SCRIPT)
        self.assertNotIn('ufw reset', SCRIPT)
        self.assertIn("ufw show added | grep -Eq '^ufw[[:space:]]'", SCRIPT)
        self.assertNotIn('docker rm', SCRIPT)
        self.assertNotIn('docker inspect remnanode\n', SCRIPT)
        self.assertNotIn('cat "$NODE/node.env"', SCRIPT)
        self.assertNotIn('nginx', SCRIPT.lower())
        self.assertNotIn('certbot', SCRIPT.lower())


if __name__ == '__main__':
    unittest.main()
