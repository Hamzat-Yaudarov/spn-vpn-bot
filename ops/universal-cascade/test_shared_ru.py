"""Actual shared-RU manager exercised against a private fake Linux host.

No host command is executed; only files below TemporaryDirectory are written.
"""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


m = load('cascade_shared_manager_tests', HERE / 'shared_ru.py')
c = load('cascade_shared_config_tests', HERE / 'config.py')


def branch(number=1, port=443):
    return {'id': str(number) * 32, 'ruIP': '8.8.4.4', 'euIP': '9.9.9.9' if number == 1 else '8.8.8.8',
            'panelIP': '1.1.1.1', 'sshPort': 22, 'interface': 'spnwgbranch' + str(number),
            'localWG': f'10.253.{number}.1', 'peerWG': f'10.253.{number}.2',
            'entryPort': port, 'ruWGPort': port, 'euWGPort': 51820}


def rendered(b):
    keys = {role: {'private': base64.b64encode(bytes([i]) * 32).decode(),
                   'public': base64.b64encode(bytes([i + 10]) * 32).decode()}
            for i, role in enumerate(('ru', 'eu'), 1)}
    keys['psk'] = base64.b64encode(b'p' * 32).decode()
    plan = {'id': b['id'], 'entryPort': b['entryPort'], 'ru': {'ip': b['ruIP']},
            'eu': {'ip': b['euIP']}, 'link': {'ru': b['localWG'], 'eu': b['peerWG'],
            'interface': b['interface'], 'port': b['euWGPort'], 'ruPort': b['ruWGPort'], 'mtu': 1380}}
    return c._wireguard(plan, 'ru', keys).encode(), c._haproxy(plan).encode()


class Host:
    def __init__(self, root):
        self.root = root
        self.calls, self.rules, self.active = [], set(), set()
        self.installed, self.firewall_active = False, False
        self.branches = {}
        self.fail = None
        self.extra_sockets = ''

    def file(self, path, data=b'', mode=0o600):
        p = self.root / path.lstrip('/')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(mode)
        return p

    def directory(self, path):
        p = self.root / path.lstrip('/')
        p.mkdir(parents=True, exist_ok=True)
        p.chmod(0o700)

    def stage(self, b):
        self.branches.setdefault(b['id'], b)
        stage = '/root/spn-cascade-managed-' + b['id']
        self.directory(stage)
        wg, proxy = rendered(b)
        self.file(stage + '/.spn-deployment', (b['id'] + '|ru\n').encode())
        self.file(stage + '/wg.conf', wg)
        self.file(stage + '/haproxy.cfg', proxy)
        self.file(stage + '/host-policy.json', b'{"version":1,"keepTcpPorts":[]}')
        return m.Manager(b, root=self.root, uid=os.getuid(), runner=self.run, sleep=lambda _: None)

    def package_files(self):
        self.installed = True
        self.directory('/etc/haproxy')
        self.directory('/etc/wireguard')
        for path in m.BASE_FILES:
            self.file(path, b'package defaults\n', mode=0o644)

    def rule_text(self):
        return '\n'.join('ufw allow proto ' + p + ' from ' + source + ' to ' + target + ' port ' + port
                         for p, port, source, target, _ in sorted(self.rules))

    def run(self, args, *, data=None, optional=False):
        self.calls.append(args)
        if self.fail and self.fail(args):
            raise m.Error('Injected interrupted command')
        if args[:2] == ('ip', '-j'):
            if 'addr' in args:
                return json.dumps([{'addr_info': [{'local': '8.8.4.4'}]}])
            enabled = [b for b in self.branches.values()
                       if 'wg-quick@' + b['interface'] + '.service' in self.active]
            if 'link' in args:
                return json.dumps([{'ifname': 'lo'}, {'ifname': 'eth0'}] +
                                  [{'ifname': b['interface'], 'linkinfo': {'info_kind': 'wireguard'}} for b in enabled])
            return json.dumps([{'dst': b[key] + '/32', 'dev': b['interface']}
                               for b in enabled for key in ('localWG', 'peerWG')])
        if args[:4] == ('ip', '-4', 'route', 'get'):
            return args[4] + ' via 8.8.4.1 dev eth0 src 8.8.4.4\n'
        if args[0] == 'ss':
            if args[-1] == 'sport = :10050':
                return '\n'.join(row for row in self.extra_sockets.splitlines() if ':10050' in row)
            rows = 'tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n' + self.extra_sockets
            for b in self.branches.values():
                if 'wg-quick@' + b['interface'] + '.service' in self.active:
                    rows += 'udp UNCONN 0 0 0.0.0.0:' + str(b['ruWGPort']) + ' 0.0.0.0:*\n'
                if m.unit_name(b) in self.active:
                    rows += 'tcp LISTEN 0 4096 ' + b['ruIP'] + ':' + str(b['entryPort']) + ' 0.0.0.0:*\n'
            return rows
        if args[0] == 'ufw':
            if not self.installed:
                if optional:
                    return None
                raise m.Error('UFW missing')
            if args[1:3] == ('show', 'added'):
                return self.rule_text()
            if args[1] == 'status':
                return ('Status: active' if self.firewall_active else 'Status: inactive') + \
                    '\nDefault: deny (incoming), allow (outgoing), disabled (routed)\n'
            if args[1] == 'allow':
                self.rules |= m.parse_rules('ufw ' + shlex.join(args[1:]))
            if args[1:] == ('--force', 'enable'):
                self.firewall_active = True
            return ''
        if args[0] == 'systemctl':
            if args[1:3] == ('is-active', '--quiet'):
                m.need(args[3] in self.active, 'Service inactive')
            elif args[1:3] == ('enable', '--now'):
                self.active.add(args[3])
            elif args[1:3] == ('mask', '--runtime'):
                p = self.root / 'run/systemd/system/haproxy.service'
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.is_symlink():
                    p.symlink_to('/dev/null')
            elif args[1:3] == ('unmask', '--runtime'):
                (self.root / 'run/systemd/system/haproxy.service').unlink(missing_ok=True)
            return ''
        if args[:3] == ('mv', '-Tn', '--'):
            if not Path(args[4]).exists():
                Path(args[3]).rename(args[4])
            return ''
        if args[0] == 'apt-get':
            if 'install' in args:
                self.package_files()
            return ''
        if args[0] in ('haproxy', 'wg'):
            if not self.installed:
                if optional:
                    return None
                raise m.Error('Package missing')
            return 'installed\n'
        if args[0] == 'docker':
            return None
        if args[0] in ('nft', 'iptables-save', 'ip6tables-save', 'dpkg', 'sha256sum'):
            return ''
        raise AssertionError('Unexpected host command: ' + repr(args))


class SharedRUTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.host = Host(self.root)
        self.host.directory('/opt')
        self.host.directory('/etc/systemd/system')

    def install(self, b):
        manager = self.host.stage(b)
        manager.preflight(manager.load())
        manager.install(manager.load())
        return manager

    def test_public_private_identity_validation_uses_real_rfc1918_ranges(self):
        self.assertEqual(m.validate(branch()), branch())
        for key, value in (('ruIP', '127.0.0.1'), ('localWG', '1.2.3.4'),
                           ('peerWG', '10.253.1.1'), ('entryPort', 22), ('id', '../oops')):
            with self.subTest(key=key), self.assertRaises(m.Error):
                m.validate({**branch(), key: value})

    def test_first_install_and_second_add_preserve_old_files_services_and_rules(self):
        first = self.install(branch())
        old = first.load()['branches'][0]
        before = {name: first.read(name) for name in old['files']}
        self.host.calls.clear()
        second = self.install(branch(2, 8443))
        self.assertEqual(len(second.load()['branches']), 2)
        self.assertTrue(all(first.read(name) == data for name, data in before.items()))
        self.assertEqual(self.host.rules, m.rules([branch(), branch(2, 8443)]))
        service_changes = [args for args in self.host.calls if args[0] == 'systemctl']
        self.assertNotIn(('systemctl', 'enable', '--now', m.unit_name(old)), service_changes)
        self.assertFalse(any(args[1] in ('restart', 'reload', 'stop', 'disable') for args in service_changes))
        self.assertFalse(any(args[0] == 'apt-get' for args in self.host.calls))

    def test_inventory_does_not_reject_provisional_default_port_for_new_eu(self):
        self.install(branch())
        second = self.host.stage(branch(2))
        self.assertEqual(second.inventory(second.load())[0]['entryPort'], 443)
        with self.assertRaisesRegex(m.Error, 'port is already owned'):
            second.preflight(second.load())

    def test_repeated_install_has_no_service_or_firewall_mutations(self):
        manager = self.install(branch())
        self.host.calls.clear()
        manager.preflight(manager.load())
        manager.install(manager.load())
        self.assertFalse(any(args[0] == 'apt-get' or args[:2] == ('ufw', 'allow')
                             or (args[0] == 'systemctl' and args[1] != 'is-active') for args in self.host.calls))

    def test_interrupted_second_branch_rolls_forward_without_touching_first(self):
        self.install(branch())
        second = self.host.stage(branch(2, 8443))
        second.preflight(second.load())
        self.host.fail = lambda args: args[:3] == ('systemctl', 'enable', '--now')
        with self.assertRaises(m.Error):
            second.install(second.load())
        self.assertEqual(second.load()['branches'][1]['status'], 'pending')
        self.host.fail = None
        second.preflight(second.load())
        second.install(second.load())
        self.assertEqual(second.load()['branches'][1]['status'], 'active')

    def test_pending_branch_blocks_a_different_new_install_but_inventory_works(self):
        manager = self.host.stage(branch())
        manager.preflight(manager.load())
        self.host.fail = lambda args: args[0] == 'apt-get'
        with self.assertRaises(m.Error):
            manager.install(manager.load())
        self.host.fail = None
        second = self.host.stage(branch(2, 8443))
        self.assertEqual(len(second.inventory(second.load())), 1)
        with self.assertRaisesRegex(m.Error, 'previously interrupted'):
            second.preflight(second.load())

    def test_package_interruption_keeps_only_owned_mask_and_can_resume(self):
        manager = self.host.stage(branch())
        manager.preflight(manager.load())
        self.host.fail = lambda args: args[0] == 'apt-get'
        with self.assertRaises(m.Error):
            manager.install(manager.load())
        mask = self.root / 'run/systemd/system/haproxy.service'
        self.assertTrue(mask.is_symlink())
        self.host.fail = None
        manager.preflight(manager.load())
        manager.install(manager.load())
        self.assertFalse(mask.is_symlink())
        self.assertEqual(manager.load()['branches'][0]['status'], 'active')

    def test_interrupted_initial_claim_never_publishes_partial_registry(self):
        manager = self.host.stage(branch())
        manager.preflight(manager.load())
        self.host.fail = lambda args: args[0] == 'mv'
        with self.assertRaises(m.Error):
            manager.install(manager.load())
        self.assertFalse(manager.path(m.HOME).exists())
        self.host.fail = None
        manager.preflight(manager.load())
        manager.install(manager.load())
        self.assertEqual(manager.load()['branches'][0]['status'], 'active')

    def test_changed_existing_file_and_extra_firewall_rule_are_not_overwritten(self):
        manager = self.install(branch())
        self.host.file(m.branch_dir(manager.load()['branches'][0]) + '/haproxy.cfg', b'foreign\n')
        second = self.host.stage(branch(2, 8443))
        with self.assertRaisesRegex(m.Error, 'configuration changed'):
            second.preflight(second.load())
        self.host.file(m.branch_dir(manager.load()['branches'][0]) + '/haproxy.cfg', rendered(branch())[1])
        self.host.rules.add(('tcp', '80', 'any', 'any', ''))
        with self.assertRaisesRegex(m.Error, 'Firewall'):
            second.preflight(second.load())

    def test_foreign_listener_and_route_are_rejected_without_install(self):
        manager = self.host.stage(branch())
        self.host.extra_sockets = 'tcp LISTEN 0 4096 0.0.0.0:80 0.0.0.0:*\n'
        with self.assertRaisesRegex(m.Error, 'listener'):
            manager.preflight(manager.load())
        self.assertFalse((self.root / m.HOME.lstrip('/')).exists())

    def test_verified_provider_monitoring_is_preserved_and_not_restarted(self):
        self.host.extra_sockets = 'tcp LISTEN 0 128 0.0.0.0:10050 0.0.0.0:*\n'
        with patch.object(m.host_checks, 'verified_monitoring_ports', return_value=[10050]):
            manager = self.install(branch())
            self.assertIn(('tcp', '10050', 'any', 'any', ''), self.host.rules)
            manager.inventory(manager.load())
        self.assertFalse(any(args[0] == 'systemctl' and any('zabbix' in x for x in args) for args in self.host.calls))

    def test_explicit_keep_policy_preserves_unknown_listener_and_survives_service_downtime(self):
        self.host.extra_sockets = 'tcp LISTEN 0 128 0.0.0.0:10050 0.0.0.0:*\n'
        manager = self.host.stage(branch())
        self.host.file(manager.stage + '/host-policy.json', b'{"version":1,"keepTcpPorts":[10050]}')
        manager.preflight(manager.load())
        manager.install(manager.load())
        self.assertIn(('tcp', '10050', 'any', 'any', ''), self.host.rules)
        self.host.extra_sockets = ''
        manager.inventory(manager.load())
        self.assertIn(('tcp', '10050', 'any', 'any', ''), self.host.rules)
        self.assertFalse(any(args[0] == 'systemctl' and args[1] in ('stop', 'restart', 'reload')
                             for args in self.host.calls))

    def test_absent_local_exception_cannot_remove_owned_remote_firewall_rule(self):
        manager = self.host.stage(branch())
        self.host.file(manager.stage + '/host-policy.json', b'{"version":1,"keepTcpPorts":[10050]}')
        manager.preflight(manager.load())
        manager.install(manager.load())
        second = self.host.stage(branch(2, 8443))
        before = self.host.rules.copy()
        with self.assertRaisesRegex(m.Error, 'Firewall has unknown rules'):
            second.preflight(second.load())
        self.assertEqual(self.host.rules, before)

    def test_diagnostics_identify_phase_without_echoing_exception_inputs(self):
        manager = self.host.stage(branch())
        def bad_command(*args, **kwargs):
            raise KeyError('PRIVATE-INPUT-MUST-NOT-APPEAR')
        manager.run = bad_command
        try:
            manager.inventory(manager.load())
        except KeyError as error:
            message = m.error_message(error)
        self.assertIn('IPv4 address inventory', message)
        self.assertIn('KeyError', message)
        self.assertIn('shared_ru.py:', message)
        self.assertNotIn('PRIVATE-INPUT', message)

    def test_branch_collision_checks_ports_interfaces_and_private_addresses(self):
        first = branch()
        for changes in ({'entryPort': 443}, {'ruWGPort': 443}, {'interface': first['interface']},
                        {'localWG': first['localWG']}, {'peerWG': first['peerWG']}):
            with self.subTest(changes=changes), self.assertRaises(m.Error):
                m.collisions([first], {**branch(2, 8443), **changes})

    def seed_legacy(self):
        b = branch(); b['ruWGPort'] = 51820
        legacy = dict(b, legacy=True)
        self.host.branches[b['id']] = legacy
        self.host.package_files()
        self.host.directory(m.LEGACY)
        wg, proxy = rendered(b)
        for name, value in {'.spn-deployment': (b['id'] + '|ru\n').encode(),
                            '.spn-parameters': (m.params(b) + '\n').encode(), 'wg.conf': wg,
                            'haproxy.cfg': proxy, 'haproxy.service': m.unit_text(legacy),
                            'firewall.sha256': b'legacy verified manifest\n'}.items():
            self.host.file(m.LEGACY + '/' + name, value)
        self.host.file('/etc/wireguard/' + b['interface'] + '.conf', wg)
        self.host.file('/etc/systemd/system/' + m.unit_name(legacy), m.unit_text(legacy))
        self.host.active |= {'wg-quick@' + b['interface'] + '.service', m.unit_name(legacy)}
        self.host.rules |= m.rules([b]); self.host.firewall_active = True
        return b, legacy

    def test_legacy_adoption_requires_local_proof_and_preserves_old_units(self):
        b, legacy = self.seed_legacy()
        second = self.host.stage(branch(2, 8443))
        with self.assertRaises(m.Error):
            second.load()
        self.host.file(second.stage + '/shared-ru-proof.json', json.dumps({'branches': [b]}).encode())
        second.preflight(second.load())
        second.install(second.load())
        old = second.load()['branches'][0]
        self.assertTrue(old['legacy'])
        self.assertIn('spn-cascade-haproxy.service', self.host.active)
        self.assertEqual(second.read(m.LEGACY + '/haproxy.service'), m.unit_text(legacy))
        # Once adopted the saved registry, not a now-stale legacy UFW manifest,
        # owns the union. Future inventory must not re-run that old manifest.
        self.host.calls.clear()
        second.inventory(second.load())
        self.assertFalse(any(args[0] == 'sha256sum' for args in self.host.calls))

    def test_legacy_only_resume_preserves_immutable_firewall_base(self):
        b, _ = self.seed_legacy()
        manager = self.host.stage(b)
        self.host.file(manager.stage + '/shared-ru-proof.json', json.dumps({'branches': [b]}).encode())
        manager.preflight(manager.load())
        self.host.calls.clear()
        manager.install(manager.load())
        self.assertEqual(set(manager.load()['baseline']), set(m.BASE_FILES))
        self.assertTrue(manager.load()['initialized'])
        self.assertFalse(any(args[0] == 'apt-get' or args[:3] == ('systemctl', 'enable', '--now')
                             for args in self.host.calls))
        self.host.file(m.BASE_FILES[0], b'changed\n', mode=0o644)
        with self.assertRaisesRegex(m.Error, 'base configuration changed'):
            manager.inventory(manager.load())


if __name__ == '__main__':
    unittest.main()
