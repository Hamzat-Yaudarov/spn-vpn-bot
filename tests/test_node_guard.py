"""Owned firewall transactions, reserved address isolation and boot ordering."""
import copy
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / 'ops/universal-reality'
sys.path.insert(0, str(SOURCES))
try:
    SPEC = importlib.util.spec_from_file_location('spn_node_guard_test', SOURCES / 'node_guard.py')
    guard = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(guard)
finally:
    sys.path.pop(0)
ID = 'a1b2c3d4' * 4
ACTIVE, NEW, THIRD = '217.60.30.79', '217.60.30.86', '217.60.30.85'
PANEL, CONTROLLER = '193.23.216.11', '147.45.164.28'


class Kernel:
    def __init__(self):
        self.table = None
        self.calls = []
        self.batches = []
        self.addresses = [ACTIVE, NEW, THIRD]
        self.fail_check = self.fail_apply = self.lost_response = False
        self.change_at_check = False
        self.counter = 0
        self.unrelated = {'family': 'ip', 'name': 'docker-owned-by-someone-else', 'handle': 7}
        self.enabled = set()

    def run(self, *args, input=None, timeout=30):
        self.calls.append((args, input, timeout))
        if args[:3] == ('ip', '-j', '-4'):
            return json.dumps([{'ifname':'ens3','flags':['UP'], 'addr_info':[
                {'family':'inet', 'local':ip, 'prefixlen':32} for ip in self.addresses]}])
        if args == ('nft', '-j', 'list', 'tables'):
            objects = [{'table':copy.deepcopy(self.unrelated)}]
            if self.table is not None:
                objects.insert(0, copy.deepcopy(self.table[0]))
            return json.dumps({'nftables':objects})
        if args == ('nft', '-j', 'list', 'table', 'inet', guard.TABLE):
            result = copy.deepcopy(self.table)
            self.counter += 1
            for number, item in enumerate(result):
                kind, body = next(iter(item.items()))
                body['handle'] = number + 100
                if kind == 'rule':
                    for expression in body['expr']:
                        if 'counter' in expression:
                            expression['counter'] = {'packets':self.counter, 'bytes':self.counter * 143}
            return json.dumps({'nftables':[{'metainfo':{'version':'fake'}}] + result})
        if args[:2] == ('nft', '-j') and args[-2:] == ('-f', '-'):
            operations = json.loads(input)['nftables']
            if '-c' in args:
                if self.fail_check:
                    raise guard.GuardError('fake syntax failure')
                if self.change_at_check:
                    self.table[-1]['rule']['comment'] = 'modified during check'
                return ''
            self.batches.append(operations)
            if self.fail_apply:
                raise guard.GuardError('fake apply failure')
            result = None
            for operation in operations:
                action, item = next(iter(operation.items()))
                if action == 'delete':
                    if result is None:
                        result = copy.deepcopy(self.table)
                    result = None
                elif action == 'create':
                    result = [copy.deepcopy(item)]
                elif action == 'add':
                    result.append(copy.deepcopy(item))
                else:
                    raise AssertionError('Unexpected mutation')
            self.table = result
            if self.lost_response:
                raise guard.GuardError('fake lost response after kernel commit')
            return ''
        if args[0] == 'systemctl':
            if args[1] == 'enable':
                self.enabled.update(args[2:])
            if args[1:3] == ('is-enabled', '--quiet') and args[3] not in self.enabled:
                raise guard.GuardError('fake disabled unit')
            return ''
        if args[0] == 'apt-get':
            return ''
        raise AssertionError('Unexpected command ' + repr(args))


def packet_verdict(objects, *, destination=ACTIVE, source='8.8.8.8', port=443,
                   loopback=False, local=True, protocol='tcp'):
    """Small independent evaluator for our emitted rule AST, including order."""
    sets = {item['set']['name']:[ipaddress.ip_network(v) for v in guard._set_networks(item['set']['elem'],
             4 if item['set']['type'] == 'ipv4_addr' else 6)] for item in objects if 'set' in item}
    family = 'ipv4' if ipaddress.ip_address(destination).version == 4 else 'ipv6'
    for item in objects:
        if 'rule' not in item:
            continue
        matched = True
        for expression in item['rule']['expr']:
            if 'match' in expression:
                match = expression['match']
                left, right = match['left'], match['right']
                if 'meta' in left:
                    actual = {'iifname':'lo' if loopback else 'ens3', 'nfproto':family, 'l4proto':protocol}[left['meta']['key']]
                elif 'fib' in left:
                    actual = 'local' if local else 'unicast'
                else:
                    payload_protocol = left['payload']['protocol']
                    if ((payload_protocol == 'ip' and family != 'ipv4')
                            or (payload_protocol == 'ip6' and family != 'ipv6')
                            or (payload_protocol in ('tcp', 'udp') and protocol != payload_protocol)):
                        matched = False
                        break
                    actual = {'saddr':source,'daddr':destination,'dport':port}[left['payload']['field']]
                if isinstance(right, str) and right.startswith('@'):
                    address = ipaddress.ip_address(actual)
                    equal = any(address.version == network.version and address in network for network in sets[right[1:]])
                else:
                    equal = actual == right
                matched = equal if match['op'] == '==' else not equal
                if not matched:
                    break
            elif matched and 'drop' in expression:
                return 'drop'
            elif matched and 'return' in expression:
                return 'return'
    return 'return'


class NodeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.root = Path(self.temp.name).resolve()
        for name in ('var/lib', 'usr/local/lib', 'etc/systemd/system', 'opt/spn-reality-node'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / 'opt/spn-reality-node').chmod(0o700)
        marker = self.root / 'opt/spn-reality-node/.spn-deployment'
        marker.write_text(ID + '\n')
        marker.chmod(0o600)
        self.kernel = Kernel()
        self.manager = guard.NodeGuard(ID, root=self.root, run=self.kernel.run, source_dir=SOURCES)
        self.which = patch.object(guard.shutil, 'which', return_value='/usr/sbin/nft')
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.temp.cleanup()

    def install(self, active=ACTIVE, old=None):
        return self.manager.install(active, PANEL, CONTROLLER, old)

    def test_install_changes_only_isolated_table_and_confirms_boot_persistence(self):
        before = copy.deepcopy(self.kernel.unrelated)
        result = self.install()
        self.assertEqual(result, {'activeIp':ACTIVE,'deployment':ID,'reserveVpnBlocked':True,
                                 'scannerFilterEnabled':True,'bootPersistenceVerified':True})
        self.assertEqual(self.kernel.unrelated, before)
        self.assertEqual(self.manager.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.manager.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.kernel.batches), 1)
        self.assertTrue(all(action in ('create','add') for operation in self.kernel.batches[0] for action in operation))
        self.assertEqual(self.manager.verify(ACTIVE, PANEL, CONTROLLER), result)

    def test_reserved_ipv4_ipv6_and_trusted_controller_cannot_reach_reserve_443(self):
        self.install()
        for destination in (NEW, THIRD, '2a0c:a9c7:200::1'):
            for source in ('8.8.8.8', CONTROLLER, PANEL):
                with self.subTest(destination=destination,source=source):
                    self.assertEqual(packet_verdict(self.kernel.table,destination=destination,source=source), 'drop')
        self.assertEqual(packet_verdict(self.kernel.table), 'return')
        self.assertEqual(packet_verdict(self.kernel.table,destination=NEW,port=22), 'return')
        self.assertEqual(packet_verdict(self.kernel.table,destination=NEW,local=False), 'return')
        self.assertEqual(packet_verdict(self.kernel.table,destination=NEW,loopback=True), 'return')

    def test_known_scanners_are_dropped_on_all_ports_after_controller_exceptions(self):
        self.install()
        for port in (22,80,443,2222):
            self.assertEqual(packet_verdict(self.kernel.table,source='185.224.228.10',port=port), 'drop')
            self.assertEqual(packet_verdict(self.kernel.table,source=CONTROLLER,port=port), 'return')
            self.assertEqual(packet_verdict(self.kernel.table,source=PANEL,port=port), 'return')
        self.assertEqual(packet_verdict(self.kernel.table,destination='2a0c:a9c7:200::1',
                                       source='2a0c:a9c7:156::1',port=22), 'drop')

    def test_active_ip_change_is_one_checked_atomic_batch_and_retains_ssh(self):
        self.install()
        self.install(NEW, ACTIVE)
        self.assertEqual(len(self.kernel.batches), 2)
        replacement = self.kernel.batches[-1]
        self.assertEqual(replacement[0], {'delete':{'table':{'family':'inet','name':guard.TABLE}}})
        self.assertEqual(next(iter(replacement[1])), 'create')
        self.assertEqual(packet_verdict(self.kernel.table,destination=ACTIVE), 'drop')
        self.assertEqual(packet_verdict(self.kernel.table,destination=NEW), 'return')
        self.assertEqual(packet_verdict(self.kernel.table,destination=ACTIVE,port=22), 'return')
        self.assertIsNone(self.manager.state()['pending'])

    def test_active_ip_change_requires_correct_prior_address(self):
        self.install()
        for old in (None, THIRD):
            with self.subTest(old=old), self.assertRaisesRegex(guard.GuardError, '--old-ip'):
                self.install(NEW, old)
        self.assertEqual(self.manager.state()['current']['activeIp'], ACTIVE)

    def test_unassigned_target_and_foreign_node_do_not_install_any_firewall(self):
        self.kernel.addresses = [ACTIVE]
        with self.assertRaisesRegex(guard.GuardError, 'not assigned'):
            self.install(NEW)
        self.assertFalse(self.manager.directory.exists())
        marker = self.root / 'opt/spn-reality-node/.spn-deployment'
        marker.write_text('b' * 32 + '\n')
        with self.assertRaisesRegex(guard.GuardError, 'another deployment'):
            self.install()
        self.assertEqual(self.kernel.batches, [])

    def test_preexisting_unowned_table_is_never_adopted_or_deleted(self):
        fake = guard.policy_objects({'version':1,'deployment':ID,'activeIp':ACTIVE,
            'panelIp':PANEL,'controllerIp':CONTROLLER,'scanners':guard.scanners.load_snapshot()})
        fake[0]['table']['comment'] = 'unrelated owner'
        self.kernel.table = copy.deepcopy(fake)
        with self.assertRaisesRegex(guard.GuardError, 'unowned'):
            self.install()
        self.assertEqual(self.kernel.table, fake)
        self.assertFalse(self.manager.config.exists())
        self.assertEqual(self.kernel.batches, [])

    def test_modified_owned_table_is_rejected_even_if_owner_comment_remains(self):
        self.install()
        self.kernel.table[-1]['rule']['expr'][-1] = {'accept':None}
        before = copy.deepcopy(self.kernel.table)
        with self.assertRaisesRegex(guard.GuardError, 'changed outside'):
            self.install(NEW, ACTIVE)
        self.assertEqual(self.kernel.table, before)
        self.assertEqual(len(self.kernel.batches), 1)

    def test_dry_check_failure_preserves_kernel_and_requires_same_transition(self):
        self.install()
        before = copy.deepcopy(self.kernel.table)
        self.kernel.fail_check = True
        with self.assertRaisesRegex(guard.GuardError, 'syntax'):
            self.install(NEW, ACTIVE)
        self.assertEqual(self.kernel.table, before)
        self.assertEqual(self.manager.state()['pending']['after']['activeIp'], NEW)
        with self.assertRaisesRegex(guard.GuardError, 'same pending'):
            self.install(THIRD, ACTIVE)
        self.kernel.fail_check = False
        self.assertEqual(self.install(NEW, ACTIVE)['activeIp'], NEW)

    def test_lost_apply_response_is_reconciled_by_exact_kernel_readback(self):
        self.install()
        self.kernel.lost_response = True
        self.assertEqual(self.install(NEW, ACTIVE)['activeIp'], NEW)
        self.assertIsNone(self.manager.state()['pending'])

    def test_failed_kernel_apply_retains_forward_only_state_and_boot_completes(self):
        self.install()
        self.kernel.fail_apply = True
        with self.assertRaisesRegex(guard.GuardError, 'not confirmed'):
            self.install(NEW, ACTIVE)
        self.assertEqual(self.manager.state()['current']['activeIp'], ACTIVE)
        self.kernel.fail_apply = False
        self.kernel.table = None
        self.kernel.addresses = []
        self.assertEqual(self.manager.apply_saved()['activeIp'], NEW)

    def test_changed_table_during_preflight_is_not_overwritten(self):
        self.install()
        self.kernel.change_at_check = True
        with self.assertRaisesRegex(guard.GuardError, 'during the preflight'):
            self.install(NEW, ACTIVE)
        self.assertEqual(len(self.kernel.batches), 1)

    def test_saved_policy_is_durable_before_first_kernel_mutation(self):
        self.kernel.fail_apply = True
        with self.assertRaises(guard.GuardError):
            self.install()
        state = self.manager.state()
        self.assertIsNone(state['current'])
        self.assertEqual(state['pending']['after']['activeIp'], ACTIVE)
        self.assertEqual(self.manager.config.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.manager.units / guard.SERVICE).is_file())
        self.assertIn(guard.SERVICE, self.kernel.enabled)
        self.assertTrue((self.manager.library / 'node_guard.py').is_file())
        self.kernel.fail_apply = False
        self.assertEqual(self.install()['activeIp'], ACTIVE)

    def test_boot_restore_needs_no_assigned_ip_and_recreates_only_own_missing_table(self):
        self.install()
        self.kernel.table = None
        self.kernel.addresses = []
        self.assertEqual(self.manager.apply_saved()['activeIp'], ACTIVE)
        self.assertEqual(len(self.kernel.batches), 2)
        self.assertTrue(all('delete' not in operation for operation in self.kernel.batches[-1]))

    def test_changed_exception_saved_state_code_and_unit_are_not_silently_adopted(self):
        self.install()
        with self.assertRaisesRegex(guard.GuardError, 'exceptions changed'):
            self.manager.install(ACTIVE, '8.8.8.8', CONTROLLER)
        unit = self.manager.units / guard.SERVICE
        old = unit.read_bytes()
        unit.write_bytes(old + b'# external change\n')
        with self.assertRaisesRegex(guard.GuardError, 'persistence differs'):
            self.manager.verify()
        unit.write_bytes(old)
        library = self.manager.library / 'scanner_guard.py'
        old = library.read_bytes()
        library.write_bytes(old + b'# external change\n')
        with self.assertRaisesRegex(guard.GuardError, 'digest differs'):
            self.manager.verify()
        library.write_bytes(old)
        state = self.manager.config.read_text()
        self.manager.config.write_text(state + ' ')
        with self.assertRaisesRegex(guard.GuardError, 'changed outside'):
            self.manager.verify()

    def test_ssh_umask_does_not_make_public_code_or_dropins_private(self):
        old = os.umask(0o077)
        try:
            self.install()
        finally:
            os.umask(old)
        self.assertEqual(self.manager.library.stat().st_mode & 0o777, 0o755)
        for path in self.manager.library.glob('*.py'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.manager.units / 'docker.service.d').stat().st_mode & 0o777, 0o755)

    def test_systemd_orders_guard_before_public_listeners_and_keeps_other_unit_settings(self):
        self.install()
        service = (self.manager.units / guard.SERVICE).read_text()
        self.assertIn('Before=network-pre.target docker.service haproxy.service', service)
        self.assertIn('After=local-fs.target systemd-sysctl.service', service)
        for name in ('docker','haproxy'):
            dropin = (self.manager.units / (name + '.service.d/30-spn-node-guard.conf')).read_text()
            self.assertIn('Requires=spn-node-guard.service', dropin)
            self.assertIn('After=spn-node-guard.service', dropin)
            self.assertNotIn('ExecStart', dropin)
        all_commands = [args for args, _, _ in self.kernel.calls]
        self.assertNotIn(('systemctl','enable','nftables.service'), all_commands)
        self.assertFalse(any(args[0] in ('ufw','iptables','ip6tables') for args in all_commands))

    def test_refresh_failure_keeps_existing_kernel_filter_and_cache(self):
        self.install()
        old = copy.deepcopy(self.kernel.table)
        with patch.object(guard.scanners, 'refresh', side_effect=guard.scanners.ScannerError('offline')), \
                self.assertRaisesRegex(guard.scanners.ScannerError, 'offline'):
            self.manager.refresh_saved()
        self.assertEqual(self.kernel.table, old)
        self.assertIsNone(self.manager.state()['pending'])

    def test_refresh_updates_only_scanners_and_keeps_active_ip_and_exceptions(self):
        self.install()
        updated = guard.scanners.snapshot(b'8.8.4.4/32\n', updated_at='2026-11-18T18:00:00Z')
        with patch.object(guard.scanners, 'refresh', return_value=updated):
            self.assertEqual(self.manager.refresh_saved()['activeIp'], ACTIVE)
        current = self.manager.state()['current']
        self.assertEqual((current['panelIp'], current['controllerIp']), (PANEL,CONTROLLER))
        self.assertEqual(packet_verdict(self.kernel.table,source='8.8.4.4',port=22), 'drop')
        self.assertEqual(packet_verdict(self.kernel.table,destination=NEW), 'drop')

    def test_nft_json_normalizer_ignores_only_handles_counters_and_equivalent_set_ranges(self):
        self.install()
        changed = copy.deepcopy(self.kernel.table)
        for item in changed:
            body = next(iter(item.values()))
            body['handle'] = 1000
            if 'set' in item and body['name'] == 'trusted4':
                body['elem'] = list(reversed(body['elem']))
        self.assertEqual(guard.normalize(changed), guard.normalize(self.kernel.table))
        changed[-1]['rule']['comment'] = 'unexpected'
        self.assertNotEqual(guard.normalize(changed), guard.normalize(self.kernel.table))

    def test_private_state_symlink_and_foreign_marker_are_refused(self):
        self.install()
        marker = self.manager.directory / '.owner'
        marker.write_text('foreign\n')
        with self.assertRaisesRegex(guard.GuardError, 'ownership marker'):
            self.manager.verify()
        marker.write_text(guard.OWNER + ':' + ID + '\n')
        real = self.manager.config.with_name('original.json')
        self.manager.config.rename(real)
        self.manager.config.symlink_to(real)
        with self.assertRaisesRegex(guard.GuardError, 'Unsafe'):
            self.manager.verify()

    def test_missing_nft_is_installed_from_os_packages_without_enabling_global_rules(self):
        release = self.root / 'etc/os-release'
        release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        release.chmod(0o644)
        with patch.object(guard.shutil, 'which', return_value=None):
            self.install()
        apt = [args for args, _, _ in self.kernel.calls if args[0] == 'apt-get']
        self.assertEqual(apt, [('apt-get','-o','DPkg::Lock::Timeout=120','update'),
                               ('apt-get','-o','DPkg::Lock::Timeout=120','install','-y','nftables')])
        self.assertFalse(any(args[:3] == ('systemctl','enable','nftables.service')
                             for args, _, _ in self.kernel.calls))

    def test_foreign_systemd_file_is_never_replaced(self):
        path = self.manager.units / guard.SERVICE
        path.write_text('[Service]\nExecStart=/bin/other-service\n')
        path.chmod(0o644)
        with self.assertRaisesRegex(guard.GuardError, 'unrelated'):
            self.install()
        self.assertEqual(path.read_text(), '[Service]\nExecStart=/bin/other-service\n')

    def test_parallel_mutation_is_rejected_before_another_firewall_batch(self):
        self.install()
        with guard.operation_lock(self.manager.directory, self.manager.owner):
            with self.assertRaisesRegex(guard.GuardError, 'Another node-guard operation'):
                self.install(NEW, ACTIVE)
        self.assertEqual(len(self.kernel.batches), 1)

    def test_crash_after_kernel_commit_before_state_commit_resumes_without_reapplying(self):
        self.install()
        original = self.manager.save_state
        def failed(value, before):
            if value['current'] is not None and value['current']['activeIp'] == NEW and value['pending'] is None:
                raise guard.GuardError('fake process died before receipt fsync')
            original(value, before)
        with patch.object(self.manager, 'save_state', side_effect=failed), self.assertRaisesRegex(guard.GuardError, 'receipt fsync'):
            self.install(NEW, ACTIVE)
        self.assertEqual(self.manager.state()['current']['activeIp'], ACTIVE)
        self.assertEqual(packet_verdict(self.kernel.table,destination=NEW), 'return')
        batches = len(self.kernel.batches)
        self.assertEqual(self.install(NEW, ACTIVE)['activeIp'], NEW)
        self.assertEqual(len(self.kernel.batches), batches)


if __name__ == '__main__':
    unittest.main()
