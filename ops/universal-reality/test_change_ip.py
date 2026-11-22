"""Offline integration regressions for same-VPS address changes; no real secrets or network."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('change_ip_test_support', HERE / 'test_replace.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
r, d = h.r, h.d
PARSER = r.parser
BASE_SSH = h.FakeSSH
IDENTITY = {'machineId': 'a' * 32, 'sshFingerprint': 'SHA256:' + 'A' * 43}


class SameVpsSSH(BASE_SSH):
    calls = []
    owner = None
    owned = True
    machine = IDENTITY
    assigned = True
    proof = None
    fail = False

    def action(self, action, capture=False):
        self.calls.append(('action', self.plan['ip'], action))
        if action == 'identity':
            return json.dumps({**self.machine, 'ownedInstallation': self.owned}).encode()

    def assigned_ipv4(self):
        return {self.plan['ip']} if self.assigned else set()

    def run(self, command, data=None, capture=False, **kwargs):
        self.calls.append(('retarget', self.plan['ip'], shlex.split(command)))
        assert not any(row[0] == 'update' for row in self.owner.dns_calls)
        if self.fail:
            raise d.u.DeployError('Simulated helper failure')
        result = {'ip': self.plan['ip'], 'preferredSourceVerified': True,
                  'bootPersistenceVerified': True, 'networkRestarted': False}
        result.update(self.proof or {})
        return json.dumps(result).encode()


class ChangeIPTests(unittest.TestCase):
    """Reuse fixtures/helpers, not inherited test methods from clean-VM replacement."""
    tearDown = h.ReplacementTests.tearDown
    snapshot = h.ReplacementTests.snapshot
    start = h.ReplacementTests.start

    def setUp(self):
        h.ReplacementTests.setUp(self)
        identity = {**IDENTITY, 'ownedInstallation': False}
        self.store.save('machine-' + d.u.digest(identity) + '.json', identity)
        SameVpsSSH.calls = []
        SameVpsSSH.owner = self
        SameVpsSSH.owned, SameVpsSSH.assigned = True, True
        SameVpsSSH.machine, SameVpsSSH.proof, SameVpsSSH.fail = IDENTITY, None, False

    def apply(self, ip=h.NEW, route='system', extra=()):
        def parser():
            value = PARSER()
            value.set_defaults(same_vps=True)
            return value
        with patch.object(r, 'parser', side_effect=parser), patch.object(h, 'FakeSSH', SameVpsSSH):
            return h.ReplacementTests.apply(self, ip, route, extra)

    def test_wrapper_defaults_to_same_vps_and_plan_is_network_free(self):
        spec = importlib.util.spec_from_file_location('change_ip_wrapper_under_test', HERE / 'change_ip.py')
        wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wrapper)
        with patch.object(wrapper.r, 'find_original', side_effect=AssertionError('state access')), \
                patch.object(wrapper.r.d, 'API', side_effect=AssertionError('network access')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wrapper.main(['--domain', h.DOMAIN, '--ip', h.NEW]), 0)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['sameVps'])
        self.assertFalse(plan['changes'])
        self.assertFalse(plan['createsPanelObjects'])

    def test_change_preserves_objects_and_never_installs_or_rekeys(self):
        before = copy.deepcopy(self.api.data)
        keys = (self.store.path / 'reality.private.json').read_bytes()
        self.apply()
        self.assertEqual(self.api.data, before)
        self.assertEqual((self.store.path / 'reality.private.json').read_bytes(), keys)
        calls = SameVpsSSH.calls
        self.assertNotIn(('action', h.NEW, 'install'), calls)
        self.assertNotIn(('action', h.NEW, 'preflight'), calls)
        self.assertNotIn(('source', h.NEW, 'apply'), calls)
        self.assertNotIn(('source', h.NEW, 'check'), calls)
        uploads = set().union(*(row[2] for row in calls if row[0] == 'upload'))
        self.assertEqual(uploads, {'managed-node.sh', 'preferred_source.py', 'retarget_source.py',
                                  *d.security.files(reality=True)})
        self.assertFalse({'node.env', 'docker-compose.yml'} & uploads)
        self.assertFalse(any(method == 'POST' and path in ('/hosts', '/nodes', '/config-profiles')
                             for method, path, body in self.api.calls))
        commands = [row[2] for row in calls if row[0] == 'retarget']
        self.assertEqual(len(commands), 1)
        self.assertIn('--old-ip', commands[0])
        self.assertEqual(commands[0][commands[0].index('--old-ip') + 1], h.IP)
        self.assertEqual(commands[0][commands[0].index('--operation') + 1], '000001')
        operation = d.u.Store(self.store.path / 'replacements' / '000001')
        self.assertTrue(operation.load('operation-kind.json')['sameVps'])
        self.assertEqual(operation.load('source-retarget.verified.json')['ip'], h.NEW)
        self.assertEqual(operation.load('commit.json')['toIp'], h.NEW)
        self.assertEqual(self.dns_ip, h.NEW)

    def test_guard_failure_stops_before_dns_and_requires_resuming_the_same_ip(self):
        self.guard_error = d.u.DeployError('Simulated guard persistence failure')
        self.guard_fail_phase = 'install'
        with self.assertRaisesRegex(d.u.DeployError, 'guard persistence'):
            self.apply(extra=['--ssh-only'])
        self.assertEqual(self.dns_ip, h.IP)
        self.assertFalse(any(row[0] == 'update' for row in self.dns_calls))
        operation = d.u.Store(self.store.path / 'replacements' / '000001')
        self.assertIsNone(operation.load('commit.json'))
        self.guard_error = None
        with self.assertRaises(d.u.DeployError):
            self.apply(h.NEXT, extra=['--ssh-only'])
        self.apply(extra=['--ssh-only'])
        self.assertEqual(operation.load('node-guard.verified.json')['activeIp'], h.NEW)
        self.assertEqual(operation.load('commit.json')['toIp'], h.NEW)
        self.assertEqual(len(r.chain(self.store, self.plan)[0]), 1)

    def test_final_guard_verification_failure_never_commits_a_partially_verified_move(self):
        self.guard_error = d.u.DeployError('Simulated final guard verification failure')
        self.guard_fail_phase = 'verify'
        with self.assertRaisesRegex(d.u.DeployError, 'final guard'):
            self.apply(extra=['--ssh-only'])
        operation = d.u.Store(self.store.path / 'replacements' / '000001')
        self.assertIsNone(operation.load('commit.json'))
        self.guard_error = None
        self.dns_calls.clear()
        self.apply(extra=['--ssh-only'])
        self.assertEqual(operation.load('commit.json')['toIp'], h.NEW)

    def test_sni_filter_failure_prevents_dns_publication(self):
        self.sni_error = d.u.DeployError('Simulated SNI filtering failure')
        with self.assertRaisesRegex(d.u.DeployError, 'SNI filtering'):
            self.apply(extra=['--ssh-only'])
        self.assertEqual(self.dns_ip, h.IP)
        self.assertFalse(any(row[0] == 'update' for row in self.dns_calls))
        self.assertIsNone(d.u.Store(self.store.path / 'replacements' / '000001').load('commit.json'))
        self.sni_error = None
        self.apply(extra=['--ssh-only'])
        self.assertEqual(self.dns_ip, h.NEW)

    def test_machine_identity_is_checked_before_source_and_dns(self):
        SameVpsSSH.machine = {**IDENTITY, 'machineId': 'b' * 32}
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertFalse(any(row[0] == 'retarget' for row in SameVpsSSH.calls))
        self.assertFalse(any(row[0] == 'update' for row in self.dns_calls))
        self.assertEqual(self.api.calls, [])

    def test_same_machine_without_owned_installation_is_not_adopted(self):
        SameVpsSSH.owned = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertFalse(any(row[0] == 'retarget' for row in SameVpsSSH.calls))
        self.assertFalse(any(row[0] == 'update' for row in self.dns_calls))

    def test_new_ip_must_already_be_assigned_to_this_guest(self):
        SameVpsSSH.assigned = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertFalse(any(row[0] == 'retarget' for row in SameVpsSSH.calls))
        self.assertFalse(any(row[0] == 'update' for row in self.dns_calls))

    def test_helper_boot_proof_is_mandatory_before_panel_or_dns_mutation(self):
        SameVpsSSH.proof = {'bootPersistenceVerified': False}
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(self.api.calls, [])
        self.assertFalse(any(row[0] == 'update' for row in self.dns_calls))
        self.assertEqual(self.probe_calls, [])

    def test_helper_failure_is_resumed_without_new_generation(self):
        SameVpsSSH.fail = True
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(self.dns_ip, h.IP)
        SameVpsSSH.fail = False
        self.apply()
        commits, pending = r.chain(self.store, self.plan)
        self.assertIsNone(pending)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]['toIp'], h.NEW)

    def test_vpn_failure_does_not_publish_dns_and_retry_finishes(self):
        self.proof_passed = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(self.dns_ip, h.IP)
        self.proof_passed = True
        self.apply()
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(len(commits), 1)
        self.assertIsNone(pending)

    def test_repeated_rotations_use_last_committed_address_and_same_identity(self):
        self.apply()
        self.dns_calls.clear()
        self.apply(h.NEXT)
        commits, pending = r.chain(self.store, self.plan)
        self.assertEqual(len(commits), 2)
        self.assertIsNone(pending)
        self.assertEqual(commits[1]['fromIp'], h.NEW)
        self.assertEqual(commits[1]['toIp'], h.NEXT)
        self.assertEqual(r.original_machine(self.store, commits), IDENTITY)
        self.assertEqual(self.dns_ip, h.NEXT)
        commands = [row[2] for row in SameVpsSSH.calls if row[0] == 'retarget']
        self.assertEqual(commands[-1][commands[-1].index('--old-ip') + 1], h.NEW)

    def test_completed_current_address_is_no_op(self):
        self.apply()
        before = len(SameVpsSSH.calls), len(self.api.calls), len(self.dns_calls)
        self.apply()
        self.assertEqual((len(SameVpsSSH.calls), len(self.api.calls), len(self.dns_calls)), before)

    def test_original_machine_requires_one_unambiguous_saved_identity(self):
        self.assertEqual(r.original_machine(self.store, []), IDENTITY)
        other = {**IDENTITY, 'machineId': 'c' * 32, 'ownedInstallation': True}
        self.store.save('machine-' + d.u.digest(other) + '.json', other)
        with self.assertRaises(d.u.DeployError):
            r.original_machine(self.store, [])

    def test_original_machine_after_vm_replacement_uses_replacement_not_initial_guest(self):
        replacement = d.u.Store(self.store.path / 'replacements' / '000001')
        other = {'machineId': 'd' * 32, 'sshFingerprint': 'SHA256:' + 'B' * 43,
                 'ownedInstallation': False}
        replacement.save('machine-' + d.u.digest(other) + '.json', other)
        self.assertEqual(r.original_machine(self.store, [{'generation': 1}]),
                         {key: other[key] for key in ('machineId', 'sshFingerprint')})

    def test_mode_cannot_change_after_clean_replacement_intent(self):
        op, intent = self.start()
        r.claim_kind(op, False)
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(SameVpsSSH.calls, [])
        self.assertEqual(self.api.calls, [])

    def test_legacy_pending_clean_replacement_cannot_be_reinterpreted(self):
        op, intent = self.start()
        op.save('runtime.json', {'ip': h.NEW})
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.assertEqual(SameVpsSSH.calls, [])
        self.assertEqual(self.api.calls, [])

    def test_changed_ssh_fingerprint_cannot_be_overridden_in_same_vps_mode(self):
        args = PARSER().parse_args(['--domain', h.DOMAIN, '--ip', h.NEW, '--same-vps', '--apply',
                                   '--state-dir', str(self.store.path), '--panel-url', h.BASE,
                                   '--host-key-sha256', 'SHA256:' + 'Z' * 43])
        with patch.object(d.u, 'read_settings', return_value={}), \
                patch.object(d, 'API', side_effect=AssertionError('network after mismatch')), \
                self.assertRaises(d.u.DeployError):
            r.run(args)

    def test_ssh_only_skips_vpn_and_commits_distinct_truthful_proof(self):
        self.apply(extra=['--ssh-only', '--non-interactive'])
        self.assertEqual(self.probe_calls, [])
        operation = d.u.Store(self.store.path / 'replacements' / '000001')
        commit = operation.load('commit.json')
        self.assertEqual(commit['verificationMode'], 'ssh-only')
        self.assertEqual(commit['testRoute'], 'ssh-check')
        self.assertTrue(commit['sshReachabilityVerified'])
        self.assertFalse(commit['trafficAndExportVerified'])
        self.assertFalse(commit['directReachabilityVerified'])
        self.assertIsNone(operation.load('traffic.verified.json'))
        self.assertIsNone(operation.load('export.verified.json'))
        self.assertTrue(operation.load('runtime.json')['batchMode'])
        self.assertEqual(len(r.chain(self.store, self.plan)[0]), 1)

    def test_ssh_only_requires_same_original_machine(self):
        SameVpsSSH.machine = {**IDENTITY, 'machineId': 'e' * 32}
        with self.assertRaises(d.u.DeployError):
            self.apply(extra=['--ssh-only'])
        self.assertEqual(self.probe_calls, [])
        self.assertEqual(self.dns_ip, h.IP)
        self.assertEqual(self.api.calls, [])

    def test_ssh_only_pending_cannot_change_verification_mode(self):
        SameVpsSSH.fail = True
        with self.assertRaises(d.u.DeployError):
            self.apply(extra=['--ssh-only'])
        SameVpsSSH.fail = False
        with self.assertRaises(d.u.DeployError):
            self.apply()
        self.apply(extra=['--ssh-only'])
        self.assertEqual(self.dns_ip, h.NEW)

    def test_full_traffic_pending_cannot_weaken_to_ssh_only(self):
        SameVpsSSH.fail = True
        with self.assertRaises(d.u.DeployError):
            self.apply()
        SameVpsSSH.fail = False
        with self.assertRaises(d.u.DeployError):
            self.apply(extra=['--ssh-only'])
        self.assertEqual(self.dns_ip, h.IP)

    def test_ssh_only_chain_rejects_forged_vpn_success(self):
        self.apply(extra=['--ssh-only'])
        path = self.store.path / 'replacements' / '000001' / 'commit.json'
        commit = json.loads(path.read_text())
        commit['trafficAndExportVerified'] = True
        path.write_text(json.dumps(commit))
        with self.assertRaises(d.u.DeployError):
            r.chain(self.store, self.plan)

    def test_ssh_only_disallowed_for_clean_vps_replacement(self):
        args = PARSER().parse_args(['--domain', h.DOMAIN, '--ip', h.NEW, '--ssh-only'])
        with self.assertRaises(d.u.DeployError):
            r.run(args)


class TLSChangeIPTests(unittest.TestCase):
    transport = 'tls'
    setUp = ChangeIPTests.setUp
    tearDown = ChangeIPTests.tearDown
    snapshot = ChangeIPTests.snapshot
    start = ChangeIPTests.start
    apply = ChangeIPTests.apply

    def test_tls_moves_same_server_without_reality_keys_or_reissuing_certificate(self):
        (self.store.path / 'reality.private.json').unlink()
        before = copy.deepcopy(self.api.data)
        self.assertEqual(r.validate_original(self.store, h.DOMAIN, h.BASE), self.plan)
        self.apply(extra=['--ssh-only', '--non-interactive'])
        self.assertEqual(self.api.data, before)
        self.assertFalse((self.store.path / 'reality.private.json').exists())
        self.assertEqual(self.probe_calls, [])
        self.assertFalse(any(row == ('action', h.NEW, 'install') for row in SameVpsSSH.calls))
        commits, pending = r.chain(self.store, self.plan)
        self.assertIsNone(pending)
        self.assertEqual(commits[-1]['toIp'], h.NEW)
        self.assertEqual(self.dns_ip, h.NEW)

    def test_tls_full_validation_supplies_tls_client_options(self):
        (self.store.path / 'reality.private.json').unlink()
        self.apply()
        commits, pending = r.chain(self.store, self.plan)
        self.assertIsNone(pending)
        self.assertTrue(commits[0]['trafficAndExportVerified'])
        self.assertEqual(self.probe_options[0]['security'], 'tls')
        self.assertEqual(self.probe_options[0]['sni'], h.DOMAIN)
        self.assertEqual(self.export_options[0]['security'], 'tls')
        self.assertEqual(self.export_options[0]['sni'], h.DOMAIN)

    def test_tls_certificate_profile_change_blocks_switch(self):
        profile = self.api.data['config-profiles'][0]
        profile['config']['inbounds'][0]['streamSettings']['tlsSettings']['certificates'][0]['keyFile'] = '/wrong/key.pem'
        with self.assertRaises(d.u.DeployError):
            self.apply(extra=['--ssh-only'])
        self.assertEqual(SameVpsSSH.calls, [])
        self.assertEqual(self.dns_ip, h.IP)

    def test_clean_tls_vps_migration_rejected_before_network_mutations(self):
        args = PARSER().parse_args(['--domain', h.DOMAIN, '--ip', h.NEW, '--apply',
                                   '--state-dir', str(self.store.path), '--panel-url', h.BASE])
        with patch.object(d.u, 'read_settings', return_value={}), \
                patch.object(d, 'API', side_effect=AssertionError('Must reject before API')), \
                self.assertRaises(d.u.DeployError):
            r.run(args)
        self.assertEqual(self.dns_ip, h.IP)
        self.assertEqual(SameVpsSSH.calls, [])


if __name__ == '__main__':
    unittest.main()
