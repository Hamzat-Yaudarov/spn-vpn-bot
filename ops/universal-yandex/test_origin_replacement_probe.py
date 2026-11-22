"""Direct new-origin proofs remain distinct from public CDN activation proofs."""
import contextlib
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('new_origin_probe_under_test', HERE / 'probe.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
spec = importlib.util.spec_from_file_location('new_origin_probe_source', HERE / 'config.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
USER = '11111111-1111-4111-8111-111111111111'


class OriginReplacementProbeTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'cdnDomain': 'cdn.example.com', 'domain': 'origin.example.com',
                     'path': '/assets/edge-0123456789abcdef/', 'code': 'a1b2c3', 'ip': '9.9.9.9'}
        self.source = {'server': copy.deepcopy(c.SERVER), 'client': copy.deepcopy(c.CLIENT),
                       'fingerprint': 'firefox', 'extraEncoding': 'object', 'sourceHost': c.SOURCE_HOST}

    def run_probe(self, *, origin=True, route='system', failed_request=None, bad_engine=False):
        captured = []
        child = SimpleNamespace(poll=lambda: None, pid=123)
        reservation = MagicMock()
        reservation.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 19999)
        real_write = q.p.e.write_private
        def write(path, config):
            captured.append(copy.deepcopy(config))
            return real_write(path, config)
        def request(port, process, expected_ip, number, mode='204'):
            return {'passed': number != failed_request, 'clientAlive': True, 'number': number}
        with contextlib.ExitStack() as stack:
            patches = {
                'platform': stack.enter_context(patch.object(q.p.platform, 'system',
                    return_value='Darwin' if route == 'physical' else 'Linux')),
                'dns': stack.enter_context(patch.object(q, 'addresses', return_value=['8.8.8.8'])),
                'health': stack.enter_context(patch.object(q, 'health', return_value={'passed': True})),
                'engine': stack.enter_context(patch.object(q.p, 'select_engine', return_value=Path('/unused/xray'))),
                'credential': stack.enter_context(patch.object(q.p, 'fetch_credential', return_value=USER)),
                'tcp': stack.enter_context(patch.object(q.p, 'physical_tcp_preflight', return_value={'passed': True})),
                'local_ip': stack.enter_context(patch.object(q.subprocess, 'check_output', return_value='192.168.1.5')),
                'socket': stack.enter_context(patch.object(q.socket, 'socket', return_value=reservation)),
                'ready': stack.enter_context(patch.object(q.socket, 'create_connection', return_value=MagicMock())),
                'engine_test': stack.enter_context(patch.object(q.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=1 if bad_engine else 0))),
                'start': stack.enter_context(patch.object(q.subprocess, 'Popen', return_value=child)),
                'observed': stack.enter_context(patch.object(q.p, 'physical_observed', return_value=True)),
                'request': stack.enter_context(patch.object(q.p, 'request', side_effect=request)),
                'write': stack.enter_context(patch.object(q.p.e, 'write_private', side_effect=write)),
                'stop': stack.enter_context(patch.object(q.p.e, 'stop')),
            }
            try:
                runner = q.run_origin if origin else q.run
                proof = runner(self.plan, self.source, Path('/not-read/private-subscription'),
                               interface='en0' if route == 'physical' else 'lo', route=route)
            except q.ProbeError:
                if not bad_engine:
                    raise
                proof = None
        return proof, captured, patches, child

    def test_linux_direct_origin_uses_new_ip_origin_sni_host_and_exact_original_credentials(self):
        before = copy.deepcopy((self.plan, self.source))
        proof, configs, calls, child = self.run_probe()
        self.assertTrue(proof['passed'])
        self.assertEqual(proof['tests'], 9)
        self.assertEqual(proof['scope'], 'origin-direct')
        self.assertFalse(proof['cdnVerified'])
        self.assertEqual(proof['domain'], self.plan['domain'])
        self.assertEqual(proof['originIp'], self.plan['ip'])
        self.assertEqual(proof['expectedExitIp'], self.plan['ip'])
        self.assertNotIn('edgeIp', proof)
        self.assertEqual(proof['testRoute'], 'system')
        self.assertFalse(proof['directReachabilityVerified'])
        self.assertFalse(proof['physicalSourceObserved'])
        calls['dns'].assert_not_called()
        calls['health'].assert_called_once_with(self.plan['domain'], self.plan['ip'], self.plan['code'], require_h2=True)
        calls['credential'].assert_called_once_with(Path('/not-read/private-subscription'), 'lo')
        calls['tcp'].assert_not_called()
        calls['local_ip'].assert_not_called()
        calls['observed'].assert_not_called()
        calls['stop'].assert_called_once_with(child)
        self.assertEqual(calls['request'].call_count, 9)
        self.assertTrue(all(call.args[2] == self.plan['ip'] for call in calls['request'].call_args_list))
        self.assertEqual({call.args[3] for call in calls['request'].call_args_list}, set(range(9)))
        out = configs[0]['outbounds'][0]
        self.assertEqual(out['settings']['vnext'], [{'address': self.plan['ip'], 'port': 443,
                                                   'users': [{'id': USER, 'encryption': 'none'}]}])
        stream = out['streamSettings']
        self.assertEqual(stream['tlsSettings'], {'serverName': self.plan['domain'], 'allowInsecure': False,
                                                 'alpn': ['h2'], 'fingerprint': self.source['fingerprint']})
        self.assertEqual(stream['xhttpSettings'], {'host': self.plan['domain'], 'path': self.plan['path'],
                                                  'mode': 'packet-up', 'extra': self.source['client']})
        self.assertNotIn('sockopt', stream)
        self.assertEqual({row['protocol'] for row in configs[0]['outbounds']}, {'vless'})
        self.assertEqual((self.plan, self.source), before)

    def test_origin_physical_proof_observes_new_ip_and_never_uses_cdn_dns(self):
        proof, configs, calls, _ = self.run_probe(route='physical')
        self.assertTrue(proof['passed'])
        self.assertTrue(proof['directReachabilityVerified'])
        self.assertTrue(proof['physicalSourceObserved'])
        self.assertFalse(proof['cdnVerified'])
        calls['dns'].assert_not_called()
        calls['tcp'].assert_called_once_with(self.plan['ip'], 'en0')
        self.assertTrue(all(call.args[2] == self.plan['ip'] for call in calls['observed'].call_args_list))
        self.assertEqual(configs[0]['outbounds'][0]['streamSettings']['sockopt'], {'interface': 'en0'})

    def test_failed_authenticated_request_fails_origin_proof_and_still_stops_client(self):
        proof, _, calls, child = self.run_probe(failed_request=3)
        self.assertFalse(proof['passed'])
        self.assertEqual(proof['tests'], 9)
        self.assertFalse(proof['cdnVerified'])
        self.assertEqual(proof['scope'], 'origin-direct')
        calls['stop'].assert_called_once_with(child)

    def test_origin_proof_does_not_change_subsequent_cdn_contract(self):
        self.run_probe()
        proof, configs, calls, _ = self.run_probe(origin=False)
        self.assertEqual(proof['domain'], self.plan['cdnDomain'])
        self.assertEqual(proof['edgeIp'], '8.8.8.8')
        self.assertNotIn('scope', proof)
        self.assertNotIn('originIp', proof)
        calls['dns'].assert_called_once_with(self.plan['cdnDomain'])
        self.assertEqual(configs[0]['outbounds'][0]['streamSettings']['tlsSettings']['serverName'], self.plan['cdnDomain'])

    def test_ssh_and_private_origin_rejected_before_network_or_engine(self):
        for changed, route in (({}, 'ssh'), ({'ip': '127.0.0.1'}, 'system'),
                               ({'ip': '10.0.0.1'}, 'system'), ({'ip': '169.254.169.254'}, 'system')):
            with self.subTest(changed=changed, route=route), \
                    patch.object(q, 'addresses') as dns, patch.object(q, 'health') as health, \
                    patch.object(q.p, 'select_engine') as engine, self.assertRaises(q.ProbeError):
                q.run_origin({**self.plan, **changed}, self.source, Path('/not-read'), route=route)
            dns.assert_not_called()
            health.assert_not_called()
            engine.assert_not_called()

    def test_health_failure_stops_before_subscription_or_engine_without_fallback(self):
        with patch.object(q, 'health', side_effect=q.ProbeError('Invalid origin TLS')) as health, \
                patch.object(q, 'addresses') as dns, patch.object(q.p, 'select_engine') as engine, \
                patch.object(q.p, 'fetch_credential') as credential, self.assertRaises(q.ProbeError):
            q.run_origin(self.plan, self.source, Path('/not-read'))
        health.assert_called_once_with(self.plan['domain'], self.plan['ip'], self.plan['code'], require_h2=True)
        dns.assert_not_called()
        engine.assert_not_called()
        credential.assert_not_called()

    def test_engine_config_failure_does_not_start_or_fallback(self):
        proof, _, calls, _ = self.run_probe(bad_engine=True)
        self.assertIsNone(proof)
        calls['start'].assert_not_called()
        calls['request'].assert_not_called()
        calls['stop'].assert_called_once_with(None)


if __name__ == '__main__':
    unittest.main()
