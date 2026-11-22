"""Exercise actual Bash helpers offline: no SSH, package/network/service writes."""
import base64
import importlib.util
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
PATH = HERE / 'managed-cascade.sh'
SCRIPT = PATH.read_text()
spec = importlib.util.spec_from_file_location('cascade_shell_config', HERE / 'config.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
KEYS = {role: {'private': base64.b64encode(bytes([i]) * 32).decode(),
               'public': base64.b64encode(bytes([i + 10]) * 32).decode()}
        for i, role in enumerate(('ru', 'eu'), 1)}
KEYS['psk'] = base64.b64encode(b'p' * 32).decode()
PLAN = {'id': 'a' * 32, 'ru': {'ip': '8.8.4.4'}, 'eu': {'ip': '9.9.9.9'},
        'link': {'ru': '10.253.0.1', 'eu': '10.253.0.2', 'interface': 'spnwga1b2c3',
                 'port': 51820, 'mtu': 1380}}


def helper(name):
    found = re.findall(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}\n', SCRIPT, re.M | re.S)
    if len(found) != 1:
        # The tiny leaf helpers are intentionally single-line.
        found = re.findall(r'^' + re.escape(name) + r'\(\) \{[^\n]+\}\n', SCRIPT, re.M)
    if len(found) != 1:
        raise AssertionError('Expected one actual shell helper: ' + name)
    return found[0]


def environment(role='ru'):
    peer = 'eu' if role == 'ru' else 'ru'
    return {'ROLE': role, 'LOCAL_PUBLIC': PLAN[role]['ip'], 'PEER_PUBLIC': PLAN[peer]['ip'],
            'PANEL_IP': '1.1.1.1', 'SSH_PORT': '22', 'DEPLOY_ID': PLAN['id'],
            'INTERFACE': PLAN['link']['interface'], 'LOCAL_WG': PLAN['link'][role],
            'PEER_WG': PLAN['link'][peer], 'WG_PORT': '51820', 'LOCAL_WG_PORT': '51820',
            'PEER_WG_PORT': '51820', 'ENTRY_PORT': '443', 'RU_WG_PORT': '51820', 'IMAGE': c.IMAGE}


class ShellTests(unittest.TestCase):
    def shell(self, names, command, *, data='', role='ru', prefix='', expected=0, variables=None):
        values = environment(role)
        values.update(variables or {})
        source = 'set -euo pipefail\n'
        source += '\n'.join(key + '=' + shlex.quote(str(value)) for key, value in values.items()) + '\n'
        source += helper('fail') + prefix + '\n'
        source += ''.join(helper(name) for name in names) + '\n' + command + '\n'
        result = subprocess.run(['bash', '-c', source], input=data, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_shell_syntax(self):
        result = subprocess.run(['bash', '-n', str(PATH)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_arg_contract_fails_before_host_inspection(self):
        values = ['preflight', 'ru', '8.8.4.4', '9.9.9.9', '1.1.1.1', '22', 'a' * 32,
                  'spnwga1b2c3', '10.253.0.1', '10.253.0.2', '51820']
        for index, bad in ((0, 'erase'), (1, 'xx'), (2, '8.8.4.4/32'), (5, '65536'),
                           (5, '443'), (6, '../secret'), (7, 'eth0'), (7, 'wg;id'),
                           (8, '10.0.1.01'), (10, '01')):
            with self.subTest(index=index, bad=bad):
                args = list(values); args[index] = bad
                result = subprocess.run(['bash', str(PATH)] + args, text=True, capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Stopped:', result.stderr)
                self.assertNotIn('Root is required', result.stderr)
                self.assertNotIn('Ubuntu 24.04 is required', result.stderr)
        result = subprocess.run(['bash', str(PATH)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)

    def test_ipv4_and_port_parsers(self):
        for address in ('8.8.4.4', '0.0.0.0', '255.255.255.255', '10.253.0.1'):
            self.shell(['valid_ipv4'], 'valid_ipv4 ' + shlex.quote(address))
        for address in ('1.2.3', '1.2.3.256', '01.2.3.4', '1.2.3.4/32', '::1', '1;id'):
            self.shell(['valid_ipv4'], 'valid_ipv4 ' + shlex.quote(address), expected=1)
        for port in ('1', '65535'):
            self.shell(['valid_port'], 'valid_port ' + shlex.quote(port))
        for port in ('0', '01', '65536', '22/tcp'):
            self.shell(['valid_port'], 'valid_port ' + shlex.quote(port), expected=1)

    def test_ru_entry_port_can_equal_the_other_guests_ssh_port(self):
        header = SCRIPT.split('IMAGE=remnawave/')[0] + '\nprintf "validated\\n"\n'
        args = ['preflight', 'eu', '9.9.9.9', '8.8.4.4', '1.1.1.1', '8443', 'a' * 32,
                'spnwga1b2c3', '10.253.0.2', '10.253.0.1', '51820', '8443', '8443']
        result = subprocess.run(['bash', '-c', header, 'managed-cascade', *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        args[1] = 'ru'
        result = subprocess.run(['bash', '-c', header, 'managed-cascade', *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('conflicts with RU SSH', result.stderr)

    def test_wg_configs_accept_both_real_renderer_roles(self):
        for role in ('ru', 'eu'):
            self.shell(['validate_wg_config'], 'validate_wg_config', role=role,
                       data=c._wireguard(PLAN, role, KEYS))

    def test_wg_configs_reject_broad_routes_hooks_extra_peers_or_secrets(self):
        source = c._wireguard(PLAN, 'ru', KEYS)
        changes = [source.replace('/32', '/24'), source.replace('10.253.0.2/32', '0.0.0.0/0'),
                   source.replace('1380', '1420'), source.replace('25\n', '0\n'),
                   source.replace('9.9.9.9:51820', '8.8.8.8:51820'),
                   source + '\n[Peer]\nPublicKey = ' + KEYS['eu']['public'] + '\n',
                   source.replace('[Peer]', 'PostUp = iptables -t nat -A POSTROUTING -j MASQUERADE\n[Peer]'),
                   source.replace('[Peer]', 'Table = off\n[Peer]'),
                   source.replace('[Peer]', 'DNS = 1.1.1.1\n[Peer]'),
                   source.replace(KEYS['psk'], 'A' * 43 + '='),
                   source.replace(KEYS['psk'], 'unsafe'),
                   source.replace('MTU = 1380', 'MTU = 1380\nMTU = 1380')]
        for data in changes:
            result = self.shell(['validate_wg_config'], 'validate_wg_config', data=data, expected=1)
            self.assertNotIn(KEYS['ru']['private'], result.stderr)
            self.assertNotIn(KEYS['psk'], result.stderr)

    def test_proxy_matches_renderer_and_rejects_fallback_and_unbounded_traffic(self):
        source = c._haproxy(PLAN)
        self.shell(['validate_proxy_config'], 'validate_proxy_config', data=source)
        for data in (source.replace('10.253.0.2:2443', '9.9.9.9:2443'),
                     source.replace('send-proxy-v2', ''), source.replace('8192', '9999999'),
                     source.replace('5s', '500s'), source.replace('8.8.4.4:443', '0.0.0.0:443'),
                     source.replace('    tcp-request content reject if WAIT_END\n', ''),
                     source + '\nbackend direct\n server fallback 1.1.1.1:443\n',
                     source.replace(':443\n', ':443 ssl crt /secret/key.pem\n')):
            self.shell(['validate_proxy_config'], 'validate_proxy_config', data=data, expected=1)

    def firewall(self, role='ru'):
        text = 'Added user rules (see ufw status for running firewall):\n'
        text += "ufw allow 22/tcp comment 'SPN-CASCADE-SSH'\n"
        text += 'ufw allow from ' + environment(role)['PEER_PUBLIC'] + ' to any port 51820 proto udp\n'
        if role == 'ru': text += 'ufw allow 443/tcp\n'
        else:
            text += 'ufw allow proto tcp from 1.1.1.1/32 to any port 2222\n'
            text += 'ufw allow in on spnwga1b2c3 from 10.253.0.1 to 10.253.0.2 port 2443 proto tcp\n'
        return text

    def test_exact_firewall_role_allowlists_accept(self):
        for role in ('ru', 'eu'):
            self.shell(['firewall_rules_safe'], 'firewall_rules_safe', role=role, data=self.firewall(role))

    def test_verified_monitoring_rule_is_preserved_but_not_blindly_allowed(self):
        for role in ('ru', 'eu'):
            text = self.firewall(role) + 'ufw allow 10050/tcp\n'
            self.shell(['firewall_rules_safe'], 'firewall_rules_safe', role=role, data=text,
                       variables={'MONITOR_PORT': '10050'})
            self.shell(['firewall_rules_safe'], 'firewall_rules_safe', role=role, data=text, expected=1)

    def test_firewall_rejects_every_extra_rule_and_public_eu_bypass(self):
        for role in ('ru', 'eu'):
            for rule in ('ufw allow 80/tcp', 'ufw allow 2443/tcp', 'ufw allow 2222/tcp',
                         'ufw allow 2222:2443/tcp', 'ufw allow from 10.0.0.0/8',
                         'ufw route allow in on spnwga1b2c3', 'ufw allow out 443/tcp',
                         'ufw allow 22/tcp', 'ufw allow Anywhere'):
                self.shell(['firewall_rules_safe'], 'firewall_rules_safe', role=role,
                           data=self.firewall(role) + rule + '\n', expected=1)
        good = self.firewall('eu')
        for data in (good.replace('in on spnwga1b2c3 ', ''),
                     good.replace('from 1.1.1.1/32', 'from any'),
                     good.replace('to 10.253.0.2', 'to any'),
                     good.replace('from 10.253.0.1', 'from 10.253.0.0/24'),
                     good.replace('proto udp', 'proto tcp')):
            self.shell(['firewall_rules_safe'], 'firewall_rules_safe', role='eu', data=data, expected=1)

    def test_firewall_default_policy(self):
        self.shell(['firewall_defaults_safe'], 'firewall_defaults_safe',
                   data='Default: deny (incoming), allow (outgoing), disabled (routed)\n')
        for data in ('Default: allow (incoming), allow (outgoing), disabled (routed)\n',
                     'Default: deny (incoming), deny (outgoing), disabled (routed)\n', ''):
            self.shell(['firewall_defaults_safe'], 'firewall_defaults_safe', data=data, expected=1)

    def test_routes_reject_any_nonowned_subnet_but_allow_only_owned_host_routes(self):
        sets = [([], 0), ([{'dst': 'default', 'dev': 'eth0'}], 0),
                ([{'dst': '10.253.0.0/24', 'dev': 'eth0'}], 1),
                ([{'dst': '10.0.0.0/8', 'dev': 'eth0'}], 1),
                ([{'dst': '10.253.0.2', 'dev': 'eth0'}], 1),
                ([{'dst': '10.253.0.2', 'dev': 'spnwga1b2c3'}], 0),
                ([{'dst': '10.253.0.1', 'dev': 'spnwga1b2c3', 'table': 'local'}], 0),
                ([{'dst': '10.253.0.0/24', 'dev': 'spnwga1b2c3'}], 1)]
        for routes, status in sets:
            prefix = 'ip() { printf %s ' + shlex.quote(json.dumps(routes)) + '; }\n'
            self.shell(['routes_safe'], 'routes_safe', prefix=prefix, expected=status)

    def listener_rows(self, rows, *, fresh=True, role='ru', expected=0):
        prefix = 'ss() { printf %s ' + shlex.quote(rows) + '; }\n'
        name = 'fresh_listeners_safe' if fresh else 'owned_listeners_safe'
        self.shell([name], name, prefix=prefix, role=role, expected=expected)

    def test_fresh_tcp_udp_listener_gate(self):
        allowed = ('tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n'
                   'tcp LISTEN 0 4096 [::]:22 [::]:*\n'
                   'udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*\n'
                   'udp UNCONN 0 0 8.8.4.4%eth0:68 0.0.0.0:*\n')
        self.listener_rows(allowed)
        for row in ('tcp LISTEN 0 1 0.0.0.0:80 0.0.0.0:*\n',
                    'udp UNCONN 0 0 0.0.0.0:51820 0.0.0.0:*\n',
                    'udp UNCONN 0 0 [::]:53 [::]:*\n'):
            self.listener_rows(allowed + row, expected=1)

    def test_existing_monitor_listener_requires_the_verified_port_flag(self):
        rows = 'tcp LISTEN 0 128 0.0.0.0:10050 0.0.0.0:*\n'
        prefix = 'ss() { printf %s ' + shlex.quote(rows) + '; }\n'
        for name in ('fresh_listeners_safe', 'owned_listeners_safe'):
            self.shell([name], name, prefix=prefix, expected=1)
            self.shell([name], name, prefix=prefix, variables={'MONITOR_PORT': '10050'})

    def test_owned_listener_gate_distinguishes_ru_public_and_eu_private(self):
        self.listener_rows('tcp LISTEN 0 1 8.8.4.4:443 0.0.0.0:*\n', fresh=False)
        self.listener_rows('tcp LISTEN 0 1 0.0.0.0:443 0.0.0.0:*\n', fresh=False, expected=1)
        eu = 'tcp LISTEN 0 1 10.253.0.2:2443 0.0.0.0:*\ntcp LISTEN 0 1 *:2222 *:*\n'
        self.listener_rows(eu, fresh=False, role='eu')
        self.listener_rows(eu + 'tcp LISTEN 0 1 9.9.9.9:443 0.0.0.0:*\n',
                           fresh=False, role='eu', expected=1)
        self.listener_rows(eu.replace('10.253.0.2:2443', '*:2443'), fresh=False, role='eu', expected=1)

    def test_exact_listener_disallows_wildcards_duplicates_and_wrong_address(self):
        for address, count, status in (('10.253.0.2', 1, 0), ('0.0.0.0', 1, 1),
                                       ('[::]', 1, 1), ('10.253.0.2', 0, 1),
                                       ('10.253.0.2', 2, 1)):
            rows = ('LISTEN 0 1 ' + address + ':2443 0.0.0.0:*\n') * count
            prefix = 'ss() { printf %s ' + shlex.quote(rows) + '; }\n'
            self.shell(['listener_exact'], 'listener_exact 2443 10.253.0.2', prefix=prefix, expected=status)

    def test_apt_waits_for_locks_and_preserves_nonlock_failures(self):
        result = self.shell(['apt_install'], 'apt_install haproxy', prefix='apt-get() { printf "%s\\n" "$@"; }\n')
        self.assertIn('DPkg::Lock::Timeout=600\ninstall\n-y\nhaproxy', result.stdout)
        prefix = ('round=0\napt-get() { if [ "$round" -lt 2 ]; then echo "Could not get lock" >&2; return 100; fi; echo updated; }\n'
                  'sleep() { round=$((round+1)); }\n')
        result = self.shell(['apt_update'], 'apt_update; printf "round=%s\\n" "$round"', prefix=prefix)
        self.assertIn('round=2', result.stdout)
        self.shell(['apt_update'], 'apt_update', expected=100,
                   prefix='apt-get() { echo "broken repository" >&2; return 100; }\nsleep() { exit 99; }\n')
        prefix = 'apt-get() { echo "Unable to acquire lock" >&2; return 100; }\nsleep() { :; }\n'
        self.shell(['apt_update'], 'apt_update', expected=100, prefix=prefix)

    def test_safe_file_rejects_symlinks_and_unexpected_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owned'
            path.write_text('private')
            link = Path(directory) / 'link'
            link.symlink_to(path)
            for metadata, status in (('0:600:1', 0), ('0:644:1', 1), ('1000:600:1', 1), ('0:600:2', 1)):
                self.shell(['safe_file'], 'safe_file ' + shlex.quote(str(path)), expected=status,
                           prefix='stat() { printf "%s\\n" ' + shlex.quote(metadata) + '; }\n')
            self.shell(['safe_file'], 'safe_file ' + shlex.quote(str(link)), expected=1,
                       prefix='stat() { echo 0:600:1; }\n')

    def test_ru_role_rejects_eu_secret_files_before_proxy_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.spn-deployment').write_text('a' * 32 + '|ru\n')
            (root / 'wg.conf').write_text(c._wireguard(PLAN, 'ru', KEYS))
            (root / 'haproxy.cfg').write_text(c._haproxy(PLAN))
            for filename in ('node.env', 'docker-compose.yml'):
                path = root / filename
                path.write_text('SENTINEL-EU-ONLY-SECRET')
                result = self.shell(['validate_wg_config', 'validate_proxy_config', 'validate_role_files'],
                                    'validate_role_files', variables={'STAGE': directory, 'OWNER': 'a' * 32 + '|ru'},
                                    prefix='safe_file() { [ -f "$1" ] && [ ! -L "$1" ]; }\n', expected=1)
                self.assertIn('EU-only configuration', result.stderr)
                self.assertNotIn('SENTINEL-EU-ONLY-SECRET', result.stderr)
                path.unlink()

    def test_padded_wireguard_keys_are_not_split_and_handshake_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            node = Path(directory)
            (node / 'wg.conf').write_text(c._wireguard(PLAN, 'ru', KEYS))
            prefix = 'MOCK_PEER=' + shlex.quote(KEYS['eu']['public']) + '\n'
            prefix += 'MOCK_PRIVATE=' + shlex.quote(KEYS['ru']['private']) + '\n'
            prefix += 'MOCK_PUBLIC=' + shlex.quote(KEYS['ru']['public']) + '\n'
            prefix += 'MOCK_PSK=' + shlex.quote(KEYS['psk']) + '\n'
            prefix += '''systemctl() { return 0; }
probe_tcp() { return 0; }
date() { echo 1000; }
ip() {
  case "$*" in
    '-o -4 addr show dev '*) echo '1: spnwga1b2c3 inet 10.253.0.1/32 scope global spnwga1b2c3';;
    '-o link show dev '*) echo '1: spnwga1b2c3: <UP> mtu 1380';;
    '-4 route get '*) echo '10.253.0.2 dev spnwga1b2c3 src 10.253.0.1';;
    *) return 99;;
  esac
}
round=0; SECONDS=0
sleep() { round=$((round+1)); SECONDS=$round; }
wg() {
  if [ "$1" = pubkey ]; then
    read -r input; [ "$input" = "$MOCK_PRIVATE" ] || return 90; printf '%s\n' "$MOCK_PUBLIC"; return
  fi
  case "$3" in
    public-key) printf '%s\n' "$MOCK_PUBLIC";;
    peers) printf '%s\n' "$MOCK_PEER";;
    listen-port) echo 51820;;
    fwmark) echo off;;
    allowed-ips) printf '%s\t10.253.0.2/32\n' "$MOCK_PEER";;
    persistent-keepalive) printf '%s\t25\n' "$MOCK_PEER";;
    endpoints) printf '%s\t9.9.9.9:51820\n' "$MOCK_PEER";;
    preshared-keys) printf '%s\t%s\n' "$MOCK_PEER" "$MOCK_PSK";;
    latest-handshakes) if [ "$round" -ge "$ready_after" ]; then printf '%s\t%s\n' "$MOCK_PEER" "$stamp"; else printf '%s\t0\n' "$MOCK_PEER"; fi;;
    *) return 99;;
  esac
}
'''
            for ready, stamp, status in ((0, 1000, 0), (3, 820, 0), (0, 819, 1), (0, 1001, 1), (36, 1000, 1)):
                result = self.shell(['wireguard_runtime_verified'], 'wireguard_runtime_verified', prefix=prefix,
                                    variables={'NODE': directory, 'ready_after': ready, 'stamp': stamp}, expected=status)
                self.assertNotIn(KEYS['ru']['private'], result.stderr)
                self.assertNotIn(KEYS['psk'], result.stderr)

    def test_no_handshake_wait_during_install_or_network_restarts(self):
        install = SCRIPT.split('\ninstall)\n', 1)[1].split('\nverify)\n', 1)[0]
        self.assertNotIn('wireguard_runtime_verified', install)
        self.assertNotIn('latest-handshakes', install)
        self.assertNotIn('systemctl restart', SCRIPT)
        for forbidden in ('ufw reset', 'iptables -F', 'ip route replace', 'sysctl -w',
                          'docker rm', 'kill -9', 'rm /var/lib/dpkg', 'systemctl restart ssh'):
            self.assertNotIn(forbidden, SCRIPT)
        self.assertIn('"ip-forward":false', helper('write_docker_config'))
        self.assertIn('"iptables":false', helper('write_docker_config'))
        self.assertIn('"bridge":"none"', helper('write_docker_config'))
        packages = helper('install_packages')
        self.assertLess(packages.index('systemctl mask --runtime'), packages.index('\n    apt_update'))
        self.assertNotIn('systemctl enable --now docker', packages)
        self.assertLess(install.index('write_docker_unit'), install.index('systemctl enable --now docker'))
        self.assertLess(install.index('systemctl enable --now "wg-quick@'), install.index('systemctl enable --now docker'))
        self.assertIn('Requires=wg-quick@', helper('render_docker_unit'))

    def test_verification_is_read_only_and_preflight_checks_repeat_before_claim(self):
        verify = SCRIPT.split('\nverify)\n', 1)[1]
        for forbidden in ('write_proxy_unit', 'install_firewall', 'install_packages', 'systemctl enable', ' up -d'):
            self.assertNotIn(forbidden, verify)
        install = SCRIPT.split('\ninstall)\n', 1)[1].split('\nverify)\n', 1)[0]
        self.assertLess(install.index('fresh_firewall_safe'), install.index('mktemp -d /opt/'))
        self.assertLess(install.index('unowned_configs_absent'), install.index('mktemp -d /opt/'))
        self.assertIn('sha256sum --check --status', helper('firewall_verified'))
        self.assertIn('safe_file "$NODE/firewall.sha256"', helper('firewall_verified'))


if __name__ == '__main__':
    unittest.main()
