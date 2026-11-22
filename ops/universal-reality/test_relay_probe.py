"""Rental relay endpoint and backend exit are distinct; no network is used."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('relay_probe_under_test', HERE / 'vpn_probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
RELAY, BACKEND = '8.8.4.4', '9.9.9.9'
PUBLIC, SHORT, USER = 'A' * 43, '0123456789abcdef', '00000000-0000-4000-8000-000000000001'
SNI = 'login.vk.com'


def leaf(public_port=10038):
    return p.client_config(RELAY, PUBLIC, SHORT, USER, 19999, 'en0', sni=SNI,
                           route='system', public_port=public_port)['outbounds'][0]


class RelayProbeTests(unittest.TestCase):
    def test_public_port_is_distinct_from_local_socks_port(self):
        for public_port in (10016, 10038, 65535):
            config = p.client_config(RELAY, PUBLIC, SHORT, USER, 19999, 'en0', sni=SNI,
                                     route='system', public_port=public_port)
            self.assertEqual(config['inbounds'][0]['port'], 19999)
            endpoint = config['outbounds'][0]['settings']['vnext'][0]
            self.assertEqual((endpoint['address'], endpoint['port']), (RELAY, public_port))
            self.assertEqual(config['outbounds'][0]['streamSettings']['realitySettings']['serverName'], SNI)
            self.assertNotIn('freedom', json.dumps(config))

    def test_bad_endpoint_or_sni_rejected_without_network(self):
        for port in (0, -1, 65536, True, '10038', None):
            with self.subTest(port=port), self.assertRaises(p.ProbeError):
                p.client_config(RELAY, PUBLIC, SHORT, USER, 19999, 'en0', public_port=port)
        for ip in ('127.0.0.1', '10.0.0.1', 'not-an-ip', '224.0.0.1'):
            with self.subTest(ip=ip), self.assertRaises(p.ProbeError):
                p.client_config(ip, PUBLIC, SHORT, USER, 19999, 'en0', public_port=10038)
        for sni in ('other.example', 'login.vk.com:443', 'login.vk.com\n'):
            with self.subTest(sni=sni), self.assertRaises(p.ProbeError):
                p.client_config(RELAY, PUBLIC, SHORT, USER, 19999, 'en0', sni=sni)

    def test_ssh_forward_port_does_not_become_rental_endpoint(self):
        config = p.client_config(BACKEND, PUBLIC, SHORT, USER, 19999, 'en0', sni=SNI,
                                 route='ssh', relay_port=20876, public_port=443)
        endpoint = config['outbounds'][0]['settings']['vnext'][0]
        self.assertEqual((endpoint['address'], endpoint['port']), ('127.0.0.1', 20876))
        with patch.object(p, 'select_engine', side_effect=AssertionError('must stop before engine')):
            with self.assertRaises(p.ProbeError):
                p.run(RELAY, PUBLIC, SHORT, Path('unused'), route='ssh', relay_port=20876,
                      public_port=10038, expected_exit_ip=BACKEND)

    def test_physical_preflight_connects_to_exact_rental_port(self):
        context = MagicMock()
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.socket, 'if_nametoindex', return_value=5), \
                patch.object(p.socket, 'socket', return_value=context):
            result = p.physical_tcp_preflight(RELAY, public_port=10038)
        context.__enter__.return_value.connect.assert_called_once_with((RELAY, 10038))
        self.assertEqual(result['port'], 10038)

    def test_failed_rental_port_uses_independent_control_443(self):
        target, control = MagicMock(), MagicMock()
        target.__enter__.return_value.connect.side_effect = TimeoutError()
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.socket, 'if_nametoindex', return_value=5), \
                patch.object(p.socket, 'socket', side_effect=[target, control]):
            with self.assertRaises(p.ProbeError) as error:
                p.physical_tcp_preflight(RELAY, public_port=10038)
        self.assertIn(RELAY + ':10038', str(error.exception))
        control.__enter__.return_value.connect.assert_called_once_with(('1.1.1.1', 443))

    def test_physical_observation_requires_exact_port_not_numeric_prefix(self):
        for port, expected in ((10038, True), (100380, False), (443, False)):
            result = types.SimpleNamespace(stdout=f'n192.168.1.2:50123->{RELAY}:{port}\n'.encode())
            with patch.object(p.subprocess, 'run', return_value=result):
                self.assertEqual(p.physical_observed(10, '192.168.1.2', RELAY, public_port=10038), expected)
        result = types.SimpleNamespace(stdout=f'n192.168.1.2:50123->{RELAY}:4430\n'.encode())
        with patch.object(p.subprocess, 'run', return_value=result):
            self.assertFalse(p.physical_observed(10, '192.168.1.2', RELAY))

    def test_fingerprint_pins_rental_port_exit_and_sni(self):
        base = p.connection_fingerprint(RELAY, PUBLIC, SHORT, 'en0', sni=SNI,
                                        public_port=10038, expected_exit_ip=BACKEND)
        for extra in ({'public_port': 10016, 'expected_exit_ip': BACKEND, 'sni': SNI},
                      {'public_port': 10038, 'expected_exit_ip': RELAY, 'sni': SNI},
                      {'public_port': 10038, 'expected_exit_ip': BACKEND, 'sni': p.SNI}):
            self.assertNotEqual(base, p.connection_fingerprint(RELAY, PUBLIC, SHORT, 'en0', **extra))
        self.assertNotIn(USER, base)

    def test_export_matches_relay_ip_and_port_not_backend_or_443(self):
        good = leaf()
        wrong_port, wrong_ip = copy.deepcopy(good), copy.deepcopy(good)
        wrong_port['settings']['vnext'][0]['port'] = 443
        wrong_ip['settings']['vnext'][0]['address'] = BACKEND
        with patch.object(p, 'fetch_documents', return_value={'outbounds': [wrong_port, wrong_ip, good]}):
            result = p.verify_export(RELAY, PUBLIC, SHORT, Path('unused'), sni=SNI, public_port=10038)
        self.assertTrue(result['passed'])
        self.assertEqual(result['port'], 10038)
        for leaves in ([wrong_port], [wrong_ip]):
            with patch.object(p, 'fetch_documents', return_value={'outbounds': leaves}):
                with self.assertRaises(p.ProbeError):
                    p.verify_export(RELAY, PUBLIC, SHORT, Path('unused'), sni=SNI, public_port=10038)

    def test_export_rejects_wrong_sni_and_key_on_correct_rental_endpoint(self):
        for field, value in (('serverName', p.SNI), ('publicKey', 'B' * 43), ('fingerprint', 'chrome')):
            bad = leaf()
            bad['streamSettings']['realitySettings'][field] = value
            with patch.object(p, 'fetch_documents', return_value={'outbounds': [bad]}):
                with self.assertRaises(p.ProbeError):
                    p.verify_export(RELAY, PUBLIC, SHORT, Path('unused'), sni=SNI, public_port=10038)

    def test_trace_requires_backend_exit_not_relay(self):
        child = types.SimpleNamespace(poll=lambda: None)
        for observed, expected in ((BACKEND, True), (RELAY, False), ('1.1.1.1', False)):
            result = types.SimpleNamespace(returncode=0, stdout=('ip=' + observed + '\n').encode())
            with patch.object(p.subprocess, 'run', return_value=result):
                self.assertEqual(p.request(19999, child, BACKEND, 5, 'trace')['passed'], expected)

    def test_ssh_summary_cannot_claim_relay_path_even_with_good_rows(self):
        rows = [{'passed': True, 'clientAlive': True} for _ in range(9)]
        for route, verified in (('physical', True), ('system', True), ('ssh', False)):
            proof = p.summary(RELAY, 'a' * 64, 'en0', True, rows, route=route,
                              public_port=10038, expected_exit_ip=BACKEND)
            self.assertEqual(proof['relayPathVerified'], verified)
            self.assertEqual((proof['ip'], proof['port'], proof['expectedExitIp']), (RELAY, 10038, BACKEND))

    def test_run_uses_public_endpoint_but_checks_backend_exit(self):
        child = types.SimpleNamespace(poll=lambda: None, pid=123)
        reservation = MagicMock()
        reservation.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 19999)
        seen = []

        def request(port, process, expected_ip, number, mode='204'):
            seen.append((expected_ip, number, mode))
            return {'passed': True, 'clientAlive': True}

        with patch.object(p, 'select_engine', return_value=Path('/unused/unit-test-engine')), \
                patch.object(p, 'fetch_credential', return_value=USER), \
                patch.object(p, 'physical_tcp_preflight', return_value={'passed': True, 'port': 10038}) as tcp, \
                patch.object(p.subprocess, 'check_output', return_value='192.168.1.2\n'), \
                patch.object(p.socket, 'socket', return_value=reservation), \
                patch.object(p.socket, 'create_connection', return_value=MagicMock()), \
                patch.object(p.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
                patch.object(p.subprocess, 'Popen', return_value=child), \
                patch.object(p, 'physical_observed', return_value=True) as observed, \
                patch.object(p, 'request', side_effect=request), \
                patch.object(p.e, 'stop'), patch.object(p.time, 'sleep'):
            proof = p.run(RELAY, PUBLIC, SHORT, Path('unused'), sni=SNI,
                          public_port=10038, expected_exit_ip=BACKEND)
        self.assertTrue(proof['passed'])
        self.assertTrue(proof['relayPathVerified'])
        self.assertEqual(len(seen), 9)
        self.assertTrue(all(expected_ip == BACKEND for expected_ip, _, _ in seen))
        tcp.assert_called_once_with(RELAY, 'en0', public_port=10038)
        self.assertEqual(observed.call_args.kwargs['public_port'], 10038)


if __name__ == '__main__':
    unittest.main()
