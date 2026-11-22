"""Offline proof validation and process/network contracts; never contacts a VPS."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('reality_vpn_probe_under_test', HERE / 'vpn_probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
IP = '8.8.4.4'
USER = '00000000-0000-4000-8000-000000000001'
PUBLIC = 'A' * 43
SHORT = '0123456789abcdef'


def outbound(user=USER):
    leaf = p.client_config(IP, PUBLIC, SHORT, user, 19999, 'en0')['outbounds'][0]
    leaf['streamSettings'].pop('sockopt')
    return leaf


class Alive:
    def poll(self):
        return None


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.file = self.root / 'subscription-link.txt'
        self.file.write_text('https://sub.wayspn.online/secret-test-path\n')
        self.file.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_url_validated_without_exposure(self):
        self.assertEqual(p.subscription_url(self.file), 'https://sub.wayspn.online/secret-test-path')
        self.file.chmod(0o644)
        with self.assertRaises(p.ProbeError) as error:
            p.subscription_url(self.file)
        self.assertNotIn('secret-test-path', str(error.exception))

    def test_url_rejects_wrong_host_queries_and_curl_injection(self):
        for url in ('http://sub.wayspn.online/secret', 'https://evil.example/secret',
                    'https://sub.wayspn.online/', 'https://sub.wayspn.online/a?token=x',
                    'https://user:password@sub.wayspn.online/a', 'https://sub.wayspn.online/a#fragment',
                    'https://sub.wayspn.online/a"\noutput="/tmp/file', 'https://sub.wayspn.online/a\\x'):
            with self.subTest(url=url):
                self.file.write_text(url)
                with self.assertRaises(p.ProbeError):
                    p.subscription_url(self.file)

    def test_url_rejects_symlink_and_hardlink(self):
        linked = self.root / 'linked'
        linked.symlink_to(self.file)
        with self.assertRaises(p.ProbeError):
            p.subscription_url(linked)
        linked.unlink()
        os.link(self.file, linked)
        with self.assertRaises(p.ProbeError):
            p.subscription_url(self.file)

    def test_credentials_deduplicate_same_user_across_many_hosts(self):
        first, second = outbound(), outbound()
        second['settings']['vnext'][0]['address'] = '8.8.8.8'
        self.assertEqual(p.credential([{'outbounds': [first, second]}, {'outbounds': [first]}]), USER)

    def test_credentials_reject_ambiguity_missing_or_malformed(self):
        second = outbound('00000000-0000-4000-8000-000000000002')
        for document in ({'outbounds': []}, {'outbounds': [outbound(), second]}, None,
                         {'outbounds': [{'protocol': 'freedom', 'settings': {}}]}):
            with self.subTest(document=document), self.assertRaises(p.ProbeError):
                p.credential(document)
        malformed = outbound()
        malformed['settings']['vnext'][0]['users'][0]['id'] = 'bad-user'
        with self.assertRaises(p.ProbeError):
            p.credential({'outbounds': [malformed]})

    def test_fetch_hides_url_and_uses_system_controlplane_route(self):
        response = types.SimpleNamespace(returncode=0, stdout=json.dumps({'outbounds': [outbound()]}).encode())
        with patch.object(p.subprocess, 'run', return_value=response) as run:
            self.assertEqual(p.fetch_credential(self.file, 'en0'), USER)
        argv = run.call_args.args[0]
        self.assertNotIn('secret-test-path', ' '.join(argv))
        self.assertIn(b'secret-test-path', run.call_args.kwargs['input'])
        self.assertNotIn('--interface', argv)
        self.assertNotIn('en0', argv)
        self.assertNotIn('--location', argv)
        self.assertEqual(argv[argv.index('--noproxy') + 1], '*')

    def test_fetch_error_never_returns_response_or_stderr(self):
        response = types.SimpleNamespace(returncode=22, stdout=b'SECRET_RESPONSE', stderr=b'SECRET_LINK')
        with patch.object(p.subprocess, 'run', return_value=response), patch.object(p.time, 'sleep'):
            with self.assertRaises(p.ProbeError) as error:
                p.fetch_credential(self.file, 'en0')
        self.assertNotIn('SECRET', str(error.exception))

    def test_config_has_only_selected_outbound_and_loopback_socks(self):
        config = p.client_config(IP, PUBLIC, SHORT, USER, 19999, 'en0')
        self.assertEqual(len(config['outbounds']), 1)
        self.assertEqual(config['outbounds'][0]['protocol'], 'vless')
        self.assertEqual(config['outbounds'][0]['streamSettings']['sockopt'], {'interface': 'en0'})
        self.assertEqual(config['outbounds'][0]['streamSettings']['realitySettings']['fingerprint'], 'safari')
        self.assertEqual(config['outbounds'][0]['settings']['vnext'][0]['users'][0]['flow'], 'xtls-rprx-vision')
        self.assertEqual(config['inbounds'][0]['listen'], '127.0.0.1')
        self.assertFalse(config['inbounds'][0]['settings']['udp'])
        self.assertNotIn('freedom', json.dumps(config))
        self.assertNotIn('dialerProxy', json.dumps(config))

    def test_config_rejects_loopback_target_and_tun_interfaces(self):
        for interface in ('utun9', 'tun0', 'tap0', 'lo0', 'en0;evil', ''):
            with self.subTest(interface=interface), self.assertRaises(p.ProbeError):
                p.client_config(IP, PUBLIC, SHORT, USER, 19999, interface)
        for ip in ('127.0.0.1', '10.0.0.1', '::1'):
            with self.subTest(ip=ip), self.assertRaises((p.ProbeError, ValueError)):
                p.client_config(ip, PUBLIC, SHORT, USER, 19999, 'en0')

    def test_config_fingerprint_contains_no_user_or_private_key(self):
        first = p.connection_fingerprint(IP, PUBLIC, SHORT, 'en0')
        self.assertRegex(first, r'^[0-9a-f]{64}$')
        for arguments in (('8.8.8.8', PUBLIC, SHORT, 'en0'), (IP, 'B'*43, SHORT, 'en0'),
                          (IP, PUBLIC, 'abcdef0123456789', 'en0'), (IP, PUBLIC, SHORT, 'en1')):
            self.assertNotEqual(first, p.connection_fingerprint(*arguments))

    def test_physical_observation_requires_exact_source_and_target(self):
        source = '192.168.1.2'
        good = ('n' + source + ':50123->' + IP + ':443\n').encode()
        for data, expected in ((good, True), (b'n127.0.0.1:1->8.8.4.4:443\n', False),
                               (b'n192.168.1.2:1->8.8.8.8:443\n', False),
                               (b'n192.168.1.20:1->8.8.4.4:443\n', False)):
            with patch.object(p.subprocess, 'run', return_value=types.SimpleNamespace(stdout=data)):
                self.assertEqual(p.physical_observed(10, source, IP), expected)

    def test_http_tests_require_exact_status_and_complete_body(self):
        for mode, stdout, passed in (('204', b'204 0.123 0', True), ('204', b'200 0.123 10', False),
                                    ('body', f'200 1.2 {p.BODY_BYTES}'.encode(), True),
                                    ('body', b'200 1.2 123', False)):
            result = types.SimpleNamespace(returncode=0, stdout=stdout, stderr=b'NEVER_PRINT_RAW')
            with patch.object(p.subprocess, 'run', return_value=result):
                actual = p.request(19999, Alive(), IP, 1, mode)
            self.assertEqual(actual['passed'], passed)
            self.assertNotIn('NEVER_PRINT_RAW', json.dumps(actual))

    def test_trace_rejects_fallback_exit(self):
        for exit_ip, passed in ((IP, True), ('8.8.8.8', False)):
            result = types.SimpleNamespace(returncode=0, stdout=('ip=' + exit_ip + '\n').encode())
            with patch.object(p.subprocess, 'run', return_value=result):
                actual = p.request(19999, Alive(), IP, 5, 'trace')
            self.assertEqual(actual['passed'], passed)
            self.assertNotIn(exit_ip, json.dumps(actual))

    def test_client_exit_invalidates_even_http_success(self):
        result = types.SimpleNamespace(returncode=0, stdout=b'204 0.1 0')
        child = types.SimpleNamespace(poll=lambda: 1)
        with patch.object(p.subprocess, 'run', return_value=result):
            self.assertFalse(p.request(19999, child, IP, 0)['passed'])

    def test_process_timeout_and_invalid_output_are_sanitized(self):
        with patch.object(p.subprocess, 'run', side_effect=subprocess.TimeoutExpired('secret', 1)):
            actual = p.request(19999, Alive(), IP, 0)
        self.assertFalse(actual['passed'])
        self.assertEqual(actual['error'], 'local_process_timeout')
        self.assertNotIn('secret', json.dumps(actual))
        with patch.object(p.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=b'204 nope 0')):
            actual = p.request(19999, Alive(), IP, 0)
        self.assertEqual(actual['error'], 'unexpected_probe_output')

    def test_summary_requires_nine_successful_live_tests_and_physical_source(self):
        rows = [{'passed': True, 'clientAlive': True} for _ in range(9)]
        self.assertTrue(p.summary(IP, 'a'*64, 'en0', True, rows)['passed'])
        self.assertFalse(p.summary(IP, 'a'*64, 'en0', False, rows)['passed'])
        self.assertFalse(p.summary(IP, 'a'*64, 'en0', True, rows[:8])['passed'])
        rows[3]['passed'] = False
        self.assertFalse(p.summary(IP, 'a'*64, 'en0', True, rows)['passed'])
        rows[3].update(passed=True, clientAlive=False)
        self.assertFalse(p.summary(IP, 'a'*64, 'en0', True, rows)['passed'])

    def test_engine_pinning_is_mandatory_for_custom_path(self):
        custom = self.root / 'engine'
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.platform, 'machine', return_value='arm64'), \
                patch.object(p.e, 'pinned_engine', side_effect=ValueError('secret')):
            with self.assertRaises(p.ProbeError) as error:
                p.select_engine(custom)
        self.assertNotIn('secret', str(error.exception))

    def test_unsupported_platform_does_not_try_running_engine(self):
        with patch.object(p.platform, 'system', return_value='FreeBSD'), \
                patch.object(p.e, 'pinned_engine', side_effect=AssertionError('must not run')):
            with self.assertRaises(p.ProbeError):
                p.select_engine(self.root / 'engine')

    def test_export_verifies_actual_leaf_including_tcp_alias(self):
        leaf = outbound()
        leaf['streamSettings']['network'] = 'tcp'
        with patch.object(p, 'fetch_documents', return_value={'outbounds': [leaf, copy.deepcopy(leaf)]}):
            result = p.verify_export(IP, PUBLIC, SHORT, self.file)
        self.assertTrue(result['passed'])
        self.assertTrue(result['flowMatches'])
        self.assertTrue(result['realityKeysMatch'])
        self.assertNotIn(USER, json.dumps(result))
        self.assertNotIn('secret-test-path', json.dumps(result))

    def test_export_deduplicates_only_top_level_tags_across_auto_and_standalone_documents(self):
        first, second = outbound(), outbound()
        first['tag'], second['tag'] = 'proxy-5', 'proxy'
        documents = [{'outbounds': [first]}, {'outbounds': [second]}]
        with patch.object(p, 'fetch_documents', return_value=documents):
            proof = p.verify_export(IP, PUBLIC, SHORT, self.file)
        self.assertTrue(proof['passed'])
        self.assertNotIn('proxy', json.dumps(proof))

    def test_tls_export_accepts_matching_domain_duplicates_with_different_tags(self):
        domain = 'aust2-1.wayspn.com'
        first = p.client_config(IP, '', '', USER, 19999, 'lo', domain, 'system', security='tls')['outbounds'][0]
        first['settings']['vnext'][0]['address'] = domain
        second = copy.deepcopy(first)
        first['tag'], second['tag'] = 'proxy-5', 'proxy'
        with patch.object(p, 'fetch_documents', return_value=[{'outbounds': [first]}, {'outbounds': [second]}]):
            proof = p.verify_export(IP, '', '', self.file, interface='lo', route='system', sni=domain,
                                    endpoint_address=domain, security='tls')
        self.assertTrue(proof['passed'])
        self.assertEqual(proof['endpointAddress'], domain)
        self.assertEqual(proof['security'], 'tls')

    def test_tag_dedup_does_not_merge_conflicting_parameters_or_nested_metadata(self):
        changed = []
        leaf = outbound(); leaf['streamSettings']['realitySettings']['fingerprint'] = 'chrome'; changed.append(leaf)
        leaf = outbound(); leaf['streamSettings']['realitySettings']['serverName'] = 'other.example'; changed.append(leaf)
        leaf = outbound(); leaf['streamSettings']['realitySettings']['publicKey'] = 'B' * 43; changed.append(leaf)
        leaf = outbound(); leaf['settings']['vnext'][0]['users'][0]['flow'] = ''; changed.append(leaf)
        leaf = outbound(); leaf['streamSettings']['realitySettings']['tag'] = 'nested-tag'; changed.append(leaf)
        leaf = outbound(); leaf['remark'] = 'another top-level field'; changed.append(leaf)
        for second in changed:
            with self.subTest(changed=second):
                first = outbound()
                first['tag'], second['tag'] = 'proxy-5', 'proxy'
                with patch.object(p, 'fetch_documents', return_value=[{'outbounds': [first]}, {'outbounds': [second]}]), \
                        self.assertRaises(p.ProbeError):
                    p.verify_export(IP, PUBLIC, SHORT, self.file)

    def test_export_rejects_changed_fingerprint_keys_flow_and_transport(self):
        cases = []
        leaf = outbound(); leaf['streamSettings']['realitySettings']['fingerprint'] = 'chrome'; cases.append(leaf)
        leaf = outbound(); leaf['streamSettings']['realitySettings']['publicKey'] = 'B'*43; cases.append(leaf)
        leaf = outbound(); leaf['streamSettings']['realitySettings']['shortId'] = 'abcdef0123456789'; cases.append(leaf)
        leaf = outbound(); leaf['settings']['vnext'][0]['users'][0]['flow'] = ''; cases.append(leaf)
        leaf = outbound(); leaf['streamSettings']['network'] = 'ws'; cases.append(leaf)
        leaf = outbound(); leaf['streamSettings']['realitySettings']['serverName'] = 'other.example'; cases.append(leaf)
        for leaf in cases:
            with patch.object(p, 'fetch_documents', return_value={'outbounds': [leaf]}):
                with self.assertRaises(p.ProbeError):
                    p.verify_export(IP, PUBLIC, SHORT, self.file)

    def test_export_domain_label_keeps_fingerprint_pinned_to_physical_ip(self):
        domain = 'n-a1b2c3.wayspn.com'
        leaf = outbound()
        leaf['settings']['vnext'][0]['address'] = domain
        with patch.object(p, 'fetch_documents', return_value={'outbounds': [leaf]}), \
                patch.object(p.socket, 'getaddrinfo', side_effect=AssertionError('DNS must not run')):
            result = p.verify_export(IP, PUBLIC, SHORT, self.file, endpoint_address=domain)
        self.assertTrue(result['passed'])
        self.assertEqual(result['endpointAddress'], domain)
        self.assertEqual(result['ip'], IP)
        self.assertEqual(result['configFingerprint'], p.connection_fingerprint(IP, PUBLIC, SHORT, 'en0'))

    def test_export_domain_does_not_accept_old_ip_or_other_domain(self):
        for address in (IP, 'other.wayspn.com'):
            leaf = outbound()
            leaf['settings']['vnext'][0]['address'] = address
            with self.subTest(address=address), \
                    patch.object(p, 'fetch_documents', return_value={'outbounds': [leaf]}), \
                    self.assertRaises(p.ProbeError):
                p.verify_export(IP, PUBLIC, SHORT, self.file, endpoint_address='n-a1b2c3.wayspn.com')

    def test_export_invalid_expected_domain_fails_before_fetch(self):
        for address in ('', ' bad.example', 'https://host.example', '*.example.com',
                        'localhost', 'a..example.com', '-bad.example', 'host.local', '127.0.0.1', '::1'):
            with self.subTest(address=address), \
                    patch.object(p, 'fetch_documents', side_effect=AssertionError('must not fetch')), \
                    self.assertRaises(p.ProbeError):
                p.verify_export(IP, PUBLIC, SHORT, self.file, endpoint_address=address)

    def test_export_rejects_missing_and_ambiguous_endpoint(self):
        other = outbound()
        other['settings']['vnext'][0]['address'] = '8.8.8.8'
        changed = outbound()
        changed['streamSettings']['realitySettings']['fingerprint'] = 'chrome'
        for leaves in ([other], [outbound(), changed]):
            with patch.object(p, 'fetch_documents', return_value={'outbounds': leaves}):
                with self.assertRaises(p.ProbeError):
                    p.verify_export(IP, PUBLIC, SHORT, self.file)

    def fake_download(self, args, binary, extra=None):
        archive = Path(args[args.index('--output') + 1])
        with zipfile.ZipFile(archive, 'w') as zipped:
            zipped.writestr('xray', binary)
            if extra:
                zipped.writestr(extra, b'not used')
        return types.SimpleNamespace(returncode=0, stderr=b'')

    def test_download_checks_binary_hash_before_installing_and_never_executes_it(self):
        binary = b'unit-test executable bytes; never launched'
        cache = self.root / 'cache'
        cache.mkdir(mode=0o700)
        def download(args, **kwargs):
            self.assertEqual(args[-1], p.ENGINE_URL)
            self.assertIn('--proto-redir', args)
            self.assertEqual(args[args.index('--proto-redir')+1], '=https')
            return self.fake_download(args, binary)
        with patch.object(p, 'ENGINE_SHA256', hashlib.sha256(binary).hexdigest()), \
                patch.object(p.subprocess, 'run', side_effect=download) as run:
            target = p._download_engine(cache)
        self.assertEqual(target.read_bytes(), binary)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual(run.call_count, 1)

    def test_download_rejects_hash_mismatch_and_archive_traversal(self):
        binary = b'not a trusted executable'
        cache = self.root / 'cache'
        cache.mkdir(mode=0o700)
        with patch.object(p.subprocess, 'run', side_effect=lambda args, **kw: self.fake_download(args, binary)):
            with self.assertRaises(p.ProbeError):
                p._download_engine(cache)
        self.assertFalse((cache / ('xray-' + p.VERSION)).exists())
        with patch.object(p, 'ENGINE_SHA256', hashlib.sha256(binary).hexdigest()), \
                patch.object(p.subprocess, 'run', side_effect=lambda args, **kw: self.fake_download(args, binary, '../bad')):
            with self.assertRaises(p.ProbeError):
                p._download_engine(cache)
        self.assertFalse((self.root / 'bad').exists())

    def test_select_engine_downloads_only_after_verified_caches_are_absent(self):
        expected = self.root / 'downloaded-engine'
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.platform, 'machine', return_value='arm64'), \
                patch.object(p, 'EXISTING_ENGINE', self.root / 'missing-source'), \
                patch.object(p, '_download_engine', return_value=expected) as download:
            self.assertEqual(p.select_engine(home=self.root), expected)
        download.assert_called_once()

    def test_physical_tcp_preflight_binds_af_inet_and_skips_control_on_success(self):
        context = MagicMock()
        connection = context.__enter__.return_value
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.socket, 'if_nametoindex', return_value=5), \
                patch.object(p.socket, 'socket', return_value=context) as factory:
            result = p.physical_tcp_preflight(IP, 'en0')
        self.assertTrue(result['passed'])
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        connection.setsockopt.assert_called_once_with(socket.IPPROTO_IP, 25, 5)
        connection.settimeout.assert_called_once_with(3)
        connection.connect.assert_called_once_with((IP, 443))

    def test_physical_tcp_timeout_reports_successful_control_without_blame(self):
        target, control = MagicMock(), MagicMock()
        target.__enter__.return_value.connect.side_effect = TimeoutError()
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.socket, 'if_nametoindex', return_value=5), \
                patch.object(p.socket, 'socket', side_effect=[target, control]):
            with self.assertRaises(p.ProbeError) as error:
                p.physical_tcp_preflight(IP)
        message = str(error.exception)
        self.assertIn(IP + ':443', message)
        self.assertIn('до начала TLS/VPN', message)
        self.assertIn('контроль 1.1.1.1:443 прошёл', message)
        control.__enter__.return_value.connect.assert_called_once_with(('1.1.1.1', 443))

    def test_physical_tcp_failed_control_does_not_claim_target_is_vpn_failure(self):
        target, control = MagicMock(), MagicMock()
        target.__enter__.return_value.connect.side_effect = ConnectionRefusedError(p.errno.ECONNREFUSED, 'private diagnostic')
        control.__enter__.return_value.connect.side_effect = TimeoutError()
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.socket, 'if_nametoindex', return_value=5), \
                patch.object(p.socket, 'socket', side_effect=[target, control]):
            with self.assertRaises(p.ProbeError) as error:
                p.physical_tcp_preflight(IP)
        self.assertIn('тоже не прошёл', str(error.exception))
        self.assertIn('соединение отклонено', str(error.exception))
        self.assertNotIn('private diagnostic', str(error.exception))

    def test_physical_bind_failure_is_local_error_not_target_blame(self):
        context = MagicMock()
        context.__enter__.return_value.setsockopt.side_effect = OSError('private error')
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.socket, 'if_nametoindex', return_value=5), \
                patch.object(p.socket, 'socket', return_value=context) as factory:
            with self.assertRaises(p.ProbeError) as error:
                p.physical_tcp_preflight(IP)
        self.assertIn('привязать', str(error.exception))
        self.assertEqual(factory.call_count, 1)

    def test_run_stops_before_fetch_engine_and_nine_tests_when_physical_tcp_fails(self):
        with patch.object(p, 'physical_tcp_preflight', side_effect=p.ProbeError('TCP unavailable')), \
                patch.object(p, 'select_engine') as engine, patch.object(p, 'fetch_credential') as fetch, \
                patch.object(p, 'request') as request:
            with self.assertRaises(p.ProbeError):
                p.run(IP, PUBLIC, SHORT, self.file)
        engine.assert_not_called()
        fetch.assert_not_called()
        request.assert_not_called()

    def test_system_route_config_has_no_physical_binding_and_no_direct_fallback(self):
        config = p.client_config(IP, PUBLIC, SHORT, USER, 19999, 'en0', route='system')
        self.assertEqual(len(config['outbounds']), 1)
        self.assertEqual(config['outbounds'][0]['protocol'], 'vless')
        self.assertNotIn('sockopt', config['outbounds'][0]['streamSettings'])
        self.assertNotIn('freedom', json.dumps(config))
        self.assertNotIn('dialerProxy', json.dumps(config))

    def test_system_proof_never_claims_physical_or_direct_reachability(self):
        rows = [{'passed': True, 'clientAlive': True} for _ in range(9)]
        proof = p.summary(IP, 'a'*64, 'en0', False, rows, route='system')
        self.assertTrue(proof['passed'])
        self.assertEqual(proof['testRoute'], 'system')
        self.assertIsNone(proof['physicalInterface'])
        self.assertFalse(proof['physicalSourceObserved'])
        self.assertFalse(proof['directReachabilityVerified'])
        # Even accidental caller input cannot inflate a system proof.
        self.assertFalse(p.summary(IP, 'a'*64, 'en0', True, rows, route='system')['physicalSourceObserved'])
        rows[3]['passed'] = False
        self.assertFalse(p.summary(IP, 'a'*64, 'en0', False, rows, route='system')['passed'])

    def test_test_route_is_bound_to_proof_fingerprint(self):
        physical = p.connection_fingerprint(IP, PUBLIC, SHORT, 'en0')
        system = p.connection_fingerprint(IP, PUBLIC, SHORT, 'en0', route='system')
        self.assertNotEqual(physical, system)
        self.assertEqual(system, p.connection_fingerprint(IP, PUBLIC, SHORT, 'en7', route='system'))
        with self.assertRaises(p.ProbeError):
            p.client_config(IP, PUBLIC, SHORT, USER, 19999, 'en0', route='auto')

    def test_run_system_mode_never_invokes_physical_preflight(self):
        # Stop at the first control-plane call, before spawning a local engine.
        with patch.object(p, 'physical_tcp_preflight', side_effect=AssertionError('physical called')) as tcp, \
                patch.object(p, 'select_engine', side_effect=p.ProbeError('stop before process')):
            with self.assertRaises(p.ProbeError):
                p.run(IP, PUBLIC, SHORT, self.file, route='system')
        tcp.assert_not_called()

    def test_ssh_route_is_still_one_authenticated_vless_outbound_to_owned_forward(self):
        config = p.client_config(IP, PUBLIC, SHORT, USER, 19999, 'en0', route='ssh', relay_port=20876)
        self.assertEqual(len(config['outbounds']), 1)
        leaf = config['outbounds'][0]
        self.assertEqual(leaf['protocol'], 'vless')
        server = leaf['settings']['vnext'][0]
        self.assertEqual((server['address'], server['port']), ('127.0.0.1', 20876))
        self.assertEqual(server['users'][0]['id'], USER)
        self.assertEqual(server['users'][0]['flow'], 'xtls-rprx-vision')
        self.assertEqual(leaf['streamSettings']['security'], 'reality')
        self.assertEqual(leaf['streamSettings']['realitySettings']['fingerprint'], 'safari')
        self.assertEqual(leaf['streamSettings']['realitySettings']['publicKey'], PUBLIC)
        self.assertNotIn('sockopt', leaf['streamSettings'])
        self.assertNotIn('freedom', json.dumps(config))
        self.assertNotIn('dialerProxy', json.dumps(config))

    def test_ssh_route_requires_valid_explicit_relay_port_and_public_original_target(self):
        for port in (None, 0, 22, 1023, 65536, True, '20876'):
            with self.subTest(port=port), self.assertRaises(p.ProbeError):
                p.client_config(IP, PUBLIC, SHORT, USER, 19999, 'en0', route='ssh', relay_port=port)
        with self.assertRaises(p.ProbeError):
            p.client_config('127.0.0.1', PUBLIC, SHORT, USER, 19999, 'en0', route='ssh', relay_port=20876)
        with self.assertRaises(p.ProbeError):
            p.client_config(IP, PUBLIC, SHORT, USER, 19999, 'en0', route='system', relay_port=20876)

    def test_ssh_proof_never_claims_direct_or_physical_reachability(self):
        rows = [{'passed': True, 'clientAlive': True} for _ in range(9)]
        proof = p.summary(IP, 'a'*64, 'en0', False, rows, route='ssh')
        self.assertTrue(proof['passed'])
        self.assertEqual(proof['testRoute'], 'ssh')
        self.assertFalse(proof['physicalSourceObserved'])
        self.assertFalse(proof['directReachabilityVerified'])
        self.assertIsNone(proof['physicalInterface'])
        self.assertNotEqual(p.connection_fingerprint(IP, PUBLIC, SHORT, 'en0', route='ssh'),
                            p.connection_fingerprint(IP, PUBLIC, SHORT, 'en0', route='system'))
        rows[5]['passed'] = False
        self.assertFalse(p.summary(IP, 'a'*64, 'en0', False, rows, route='ssh')['passed'])

    def test_run_ssh_mode_never_invokes_physical_preflight(self):
        with patch.object(p, 'physical_tcp_preflight', side_effect=AssertionError('physical called')) as tcp, \
                patch.object(p, 'select_engine', side_effect=p.ProbeError('stop before process')):
            with self.assertRaises(p.ProbeError):
                p.run(IP, PUBLIC, SHORT, self.file, route='ssh', relay_port=20876)
        tcp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
