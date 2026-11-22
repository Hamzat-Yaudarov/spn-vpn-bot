"""Rechecks preserve an explicitly verified extra origin IP and all old journals."""
import copy
import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('yandex_binding_orchestration_support', HERE / 'test_deploy.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
d = support.d


class OriginBindingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.InstallerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.install('--agree-le-tos')
        self.state = self.fixture.state()
        self.plan = self.state.load('plan.json')
        self.receipt = {'version': 1, 'domain': self.plan['domain'], 'oldIp': self.plan['ip'],
                        'extraIp': '9.9.9.9', 'deployment': self.plan['id'], 'code': self.plan['code'],
                        'baseSha256': 'a' * 64, 'extraSha256': 'b' * 64,
                        'passed': True, 'baseConfigUnchanged': True}

    def dns_context(self, receipt):
        client, manager = Mock(), Mock()
        zone = {'id': 'zone-id', 'name': self.plan['dns']['zone']}
        client.find_zone.return_value = zone
        with patch.object(d.r.dns, 'CloudflareDNS', return_value=client), \
                patch.object(d.r, 'cloudflare_token', return_value='private-token'), \
                patch.object(d.records, 'Records', return_value=manager), \
                patch.object(d.bindings, 'local_receipt', return_value=receipt) as local, \
                patch.object(d.bindings, 'check_dns') as checked:
            result = d.dns_context(self.state, self.plan, self.fixture.args(), {})
        local.assert_called_once_with(self.state, self.plan, d.c._nginx(self.plan).encode('ascii'))
        manager.check_available.assert_any_call(self.state, self.plan['cdnDomain'], 'cdn')
        manager.check_available.assert_any_call(self.state, '_acme-challenge.' + self.plan['cdnDomain'], 'certificate')
        return result, client, zone, checked

    def test_verified_extra_binding_checks_current_dns_read_only(self):
        _, client, zone, checked = self.dns_context(self.receipt)
        client.check_record.assert_not_called()
        checked.assert_called_once_with(client, zone, self.state, self.plan, self.receipt)
        client.ensure_record.assert_not_called()
        client.update_record.assert_not_called()

    def test_original_installation_keeps_existing_strict_dns_path(self):
        _, client, zone, checked = self.dns_context(None)
        checked.assert_not_called()
        client.check_record.assert_called_once_with(self.state, zone_id=zone['id'],
                                                     name=self.plan['domain'], ipv4=self.plan['ip'], ttl=60)

    def test_binding_receipt_failure_stops_before_dns_adoption(self):
        client = Mock()
        client.find_zone.return_value = {'id': 'zone-id', 'name': self.plan['dns']['zone']}
        with patch.object(d.r.dns, 'CloudflareDNS', return_value=client), \
                patch.object(d.r, 'cloudflare_token', return_value='private-token'), \
                patch.object(d.bindings, 'local_receipt', side_effect=ValueError('Invalid extra listener proof')), \
                patch.object(d.records, 'Records') as manager, self.assertRaisesRegex(ValueError, 'Invalid'):
            d.dns_context(self.state, self.plan, self.fixture.args(), {})
        client.check_record.assert_not_called()
        client.ensure_record.assert_not_called()
        manager.assert_not_called()

    def test_recheck_does_not_restore_old_dns_or_rewrite_plan_and_uploads_verifier_before_preflight(self):
        before = (self.state.path / 'plan.json').read_bytes()
        api_before = copy.deepcopy(self.fixture.api.data)
        self.fixture.origin_dns.clear()
        support.SSH.calls.clear()
        support.SSH.payloads.clear()
        with patch.object(d.bindings, 'local_receipt', return_value=self.receipt), \
                patch.object(d.bindings, 'check_dns') as checked:
            self.fixture.install('--recheck-origin')
        self.assertEqual(self.fixture.origin_dns, [])
        checked.assert_called_once()
        self.assertEqual(checked.call_args.args[3:], (self.plan, self.receipt))
        self.assertIn('origin_bindings.py', support.SSH.payloads[0])
        self.assertLess(support.SSH.calls.index('upload'), support.SSH.calls.index('preflight'))
        self.assertEqual((self.state.path / 'plan.json').read_bytes(), before)
        self.assertEqual(self.fixture.api.data, api_before)
        self.assertEqual(support.SSH.payloads[-1]['nginx.conf'], d.c._nginx(self.plan).encode('ascii'))
        self.assertNotIn(b'9.9.9.9', support.SSH.payloads[-1]['nginx.conf'])


if __name__ == '__main__':
    unittest.main()
