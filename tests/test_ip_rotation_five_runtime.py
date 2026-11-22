"""Offline packaging/gate checks; no SSH, browser, service, payment or API calls."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

from spn_ip_failover import five_runtime as runtime, five_scope as scope
from spn_ip_failover.config import load_config
from spn_ip_failover.provider_gateway import Gateway
from spn_ip_failover.onecent import OneCentBilling

ROOT = Path(__file__).resolve().parents[1]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class ScopeTests(unittest.TestCase):
    def test_pure_config_matches_actual_enrolled_scope_and_reviewed_reverse_commands(self):
        enrollment = module('ops/ip-rotation-five/install_ro_monitor.py', 'five_ro_runtime_test')
        reverse = module('ops/ip-rotation-five/reverse_management_model.py', 'five_reverse_runtime_test')
        for actual, expected in zip(scope.SERVERS, enrollment.SELECTED):
            for key in ('key', 'host_uuid', 'node_uuid', 'service_id', 'service_name'):
                self.assertEqual(actual[key], expected[key])
            self.assertEqual(actual['ip'], expected['ip_at_enrollment'])
            self.assertEqual(scope.guest_ssh(actual), reverse.gateway_ssh(actual['service_id']))
        values = scope.configs()
        self.assertEqual(len(values['rotation.json']['servers']), 5)
        self.assertEqual(len(values['outside-relays.json']['relays']), 2)
        self.assertEqual(set(values['provider-gateway.json']['servers']), {r['service_id'] for r in scope.SERVERS})
        self.assertNotIn('0ae0ce08-7bbc-47a5-89b8-f6ea8c9b982b', json.dumps(values))
        self.assertTrue(all(row['dns'] == [] and not row['hosts'][0]['manage_visibility']
                            for row in values['rotation.json']['servers']))
        Gateway(values['provider-gateway.json'], Mock())

    def test_render_loadable_idempotent_and_has_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve() / 'private'
            args = ['render', '--output', str(directory)]
            for _ in range(2):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(runtime.main(args), 0)
            parsed = load_config(directory / 'rotation.json')
            self.assertEqual(parsed.max_attempts, 10)
            self.assertEqual(parsed.max_ips_per_server, 10)
            self.assertEqual([s.initial_ip for s in parsed.servers], [r['ip'] for r in scope.SERVERS])
            self.assertEqual(set(p.region for p in parsed.probes), {'ru', 'outside'})
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir()))
            self.assertFalse(any('onecent_password' in p.read_text().lower() for p in directory.iterdir()))

    def test_unit_uses_named_credentials_current_runtime_and_no_extraneous_providers(self):
        text = runtime.UNIT_TEXT
        for name in ('remnawave', 'onecent_email', 'onecent_password'):
            self.assertIn('LoadCredential=' + name + ':/etc/spn-ip-failover/credentials/' + name, text)
        self.assertNotIn('rotation_remnawave', text)
        self.assertIn('ExecStartPre=', text)
        self.assertIn('spn-failover', text)
        self.assertIn('NoNewPrivileges=yes', text)
        self.assertNotIn('root', text)

    def test_atomic_writer_rejects_links_and_keeps_identical_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            first = directory / 'real'
            self.assertTrue(runtime.write_owned(first, b'1', os.geteuid(), os.getegid(), 0o600))
            self.assertFalse(runtime.write_owned(first, b'1', os.geteuid(), os.getegid(), 0o600))
            linked = directory / 'link'
            linked.symlink_to(first)
            with self.assertRaises(ValueError):
                runtime.write_owned(linked, b'2', os.geteuid(), os.getegid(), 0o600)
            self.assertEqual(first.read_bytes(), b'1')


class FinancialGateTests(unittest.TestCase):
    def evidence(self):
        sid = scope.SERVERS[3]['service_id']
        request = 'five-vps-pilot-20261116-001'
        proof = {'version': 1, 'deployment': scope.DEPLOYMENT, 'service_id': sid,
                 'request_id': request, 'ip': '8.8.4.4', 'response_sha256': 'a'*64,
                 'intent_sha256': 'a'*64, 'dispatch_sha256': 'a'*64}
        row = {'phase': 'released', 'ip': proof['ip'], 'request_id': request,
               'receipt_sha256': 'a'*64, 'upfront_rub': '1.00'}
        from spn_ip_failover.budget import AccountBudget
        key = AccountBudget.key(sid, request)
        return {
            runtime.PROOF: proof,
            runtime.STATE/'onecent-pilot-20261116/intent.json': {'service_id': sid, 'request_key': request},
            runtime.STATE/'onecent-pilot-20261116/dispatch.json': {'service_id': sid, 'request_key': request},
            runtime.STATE/'onecent-pilot-20261116/response.json': {'service_id': sid, 'request_key': request,
                'http_status': 201, 'request': {'method': 'POST', 'path': '/api/services/'+sid+'/ipv4/add'}},
            runtime.STATE/'onecent-orders/state.json': {'servers': {sid: {'orders': {request: row},
                'releases': {proof['ip']: 'released'}}}},
            runtime.STATE/'onecent-daily-budget/state.json': {'budget': {'orders': {key: {
                'phase': 'confirmed', 'server_id': sid, 'request_id': request, 'amount_rub': '1.00'}}}},
        }

    def gate(self, evidence):
        with patch.object(OneCentBilling, 'RECEIPT_CORRELATED_OWNERSHIP', True, create=True), \
             patch.object(runtime, 'private_json', side_effect=evidence.__getitem__), \
             patch.object(runtime, 'digest', return_value='a'*64):
            return runtime.financial_gate()

    def test_boolean_readiness_or_legacy_delta_allocator_cannot_enable(self):
        with patch.object(runtime, 'private_json', return_value={'ready': True}), self.assertRaises(ValueError):
            runtime.financial_gate()
        evidence = self.evidence()
        evidence[runtime.PROOF] = {'ready': True}
        with self.assertRaises(ValueError):
            self.gate(evidence)

    def test_receipt_order_budget_and_cleanup_all_must_match(self):
        evidence = self.evidence()
        self.assertEqual(self.gate(evidence), evidence[runtime.PROOF])
        order_path = runtime.STATE/'onecent-orders/state.json'
        for mutation in ('submitting', 'wrong_receipt', 'unreleased', 'unresolved_budget', 'wrong_request_hash'):
            broken = copy.deepcopy(evidence)
            service = next(iter(broken[order_path]['servers'].values()))
            order = next(iter(service['orders'].values()))
            if mutation == 'submitting': order['phase'] = 'submitting'
            if mutation == 'wrong_receipt': order['receipt_sha256'] = 'b'*64
            if mutation == 'unreleased': service['releases'].clear()
            if mutation == 'wrong_request_hash': broken[runtime.PROOF]['intent_sha256'] = 'b'*64
            if mutation == 'unresolved_budget':
                next(iter(broken[runtime.STATE/'onecent-daily-budget/state.json']['budget']['orders'].values()))['phase'] = 'submitting'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.gate(broken)

    def test_readiness_only_cannot_enable_and_financial_runs_before_network(self):
        with patch.object(runtime.os, 'geteuid', return_value=0), patch.object(runtime.subprocess, 'run') as run, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.main(['enable', '--readiness-only']), 1)
            run.assert_not_called()
        with patch.object(runtime, 'validate_scope', return_value=scope.configs()), \
             patch.object(runtime, 'financial_gate', side_effect=ValueError('blocked')), \
             patch.object(runtime, 'Remnawave') as panel, self.assertRaises(ValueError):
            runtime.preflight()
        panel.assert_not_called()


if __name__ == '__main__':
    unittest.main()
