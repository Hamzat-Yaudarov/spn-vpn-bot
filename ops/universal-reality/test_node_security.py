"""Trusted SSH integration contracts for the IP/scanner and REALITY SNI helpers."""
import copy
import json
import shlex
import unittest

import node_security as security


ID = 'a1b2c3d4' * 4
OLD, NEW, PANEL = '8.8.4.4', '9.9.9.9', '1.1.1.1'


class FakeSSH:
    def __init__(self):
        self.plan = {'id': ID, 'ip': OLD, 'panelIp': PANEL, 'sni': 'www.cloudflare.com'}
        self.calls = []
        self.result = {'deployment': ID, 'activeIp': NEW, 'reserveVpnBlocked': True,
                       'scannerFilterEnabled': True, 'bootPersistenceVerified': True}

    def run(self, command, data=None, capture=False):
        self.calls.append((shlex.split(command), capture))
        return json.dumps(self.result).encode()


class NodeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeSSH()

    def test_guard_uses_requested_new_ip_instead_of_stale_original_plan_ip(self):
        value = security.guard(self.ssh, NEW, old_ip=OLD)
        upgrade, upgrade_capture = self.ssh.calls[0]
        self.assertTrue(upgrade_capture)
        self.assertEqual(upgrade[:2], ['python3', '/root/spn-reality-managed-' + ID + '/node_guard_upgrade.py'])
        command, capture = self.ssh.calls[1]
        self.assertTrue(capture)
        self.assertEqual(command[:3], ['python3', '/root/spn-reality-managed-' + ID + '/node_guard.py', 'install'])
        self.assertEqual(command[command.index('--active-ip') + 1], NEW)
        self.assertEqual(command[command.index('--old-ip') + 1], OLD)
        self.assertEqual(command[command.index('--deployment') + 1], ID)
        self.assertEqual(command[command.index('--panel-ip') + 1], PANEL)
        self.assertEqual(command[command.index('--controller-ip') + 1], security.CONTROLLER_IP)
        self.assertEqual(value['activeIp'], NEW)

    def test_guard_verification_is_read_only_and_never_repeats_old_ip_transition(self):
        security.guard(self.ssh, NEW, old_ip=OLD, verify=True)
        command, _ = self.ssh.calls[0]
        self.assertEqual(command[2], 'verify')
        self.assertNotIn('--old-ip', command)

    def test_guard_contract_rejects_missing_or_false_claims_and_wrong_identity(self):
        original = copy.deepcopy(self.ssh.result)
        invalid = [None, [], True, {'activeIp': NEW}]
        for field in ('reserveVpnBlocked', 'scannerFilterEnabled', 'bootPersistenceVerified'):
            for wrong in (False, 'true', 1, None):
                invalid.append({**original, field: wrong})
        invalid += [{**original, 'activeIp': OLD}, {**original, 'deployment': '0' * 32}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ssh.result = value
                security.guard(self.ssh, NEW)

    def sni_result(self):
        return {'sniFiltered': True, 'allowedSni': self.ssh.plan['sni'], 'tlsTerminated': False,
                'validSniHandshakeVerified': True, 'unknownSniRejected': True, 'missingSniRejected': True}

    def test_sni_helper_receives_actual_runtime_ip_and_configured_hostname(self):
        self.ssh.plan.update(ip=NEW, sni='custom.example.com')
        self.ssh.result = self.sni_result()
        value = security.sni(self.ssh, verify=True)
        command, capture = self.ssh.calls[0]
        self.assertTrue(capture)
        self.assertEqual(command[:3], ['python3', '/root/spn-reality-managed-' + ID + '/reality_sni_guard.py', 'verify'])
        self.assertEqual(command[command.index('--ip') + 1], NEW)
        self.assertEqual(command[command.index('--sni') + 1], 'custom.example.com')
        self.assertFalse(value['tlsTerminated'])

    def test_sni_contract_rejects_unproven_or_mismatched_filter(self):
        original = self.sni_result()
        invalid = [None, [], True, {'sniFiltered': True}]
        for field in ('sniFiltered', 'validSniHandshakeVerified', 'unknownSniRejected', 'missingSniRejected'):
            for wrong in (False, 'true', 1, None):
                invalid.append({**original, field: wrong})
        invalid += [{**original, 'allowedSni': 'other.example.com'}, {**original, 'tlsTerminated': True},
                    {key: value for key, value in original.items() if key != 'tlsTerminated'}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ssh.result = value
                security.sni(self.ssh)


if __name__ == '__main__':
    unittest.main()
