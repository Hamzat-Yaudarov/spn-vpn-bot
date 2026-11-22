"""Offline XHTTP/TLS/export tests; no network or private subscription reads."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


q = load('yandex_probe_under_test', HERE / 'probe.py')
c = load('yandex_probe_source_fixture', HERE / 'config.py')
USER = '11111111-1111-4111-8111-111111111111'


class FakeSocket:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def sendall(self, data):
        self.owner.requests.append(data)

    def selected_alpn_protocol(self):
        return self.owner.alpn


class FakeContext:
    def __init__(self):
        self.protocols, self.server_names, self.requests = [], [], []
        self.alpn = 'h2'

    def set_alpn_protocols(self, names):
        self.protocols.append(names)

    def wrap_socket(self, stream, server_hostname):
        self.server_names.append(server_hostname)
        return FakeSocket(self)


class FakeReply:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}

    def begin(self):
        pass

    def read(self, size):
        return self.body[:size]

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'cdnDomain': 'cdn.example.com', 'domain': 'origin.example.com',
                     'path': '/assets/edge-0123456789abcdef/', 'code': 'a1b2c3', 'ip': '8.8.4.4'}
        self.source = {'server': copy.deepcopy(c.SERVER), 'client': copy.deepcopy(c.CLIENT),
                       'fingerprint': 'firefox', 'extraEncoding': 'object', 'sourceHost': c.SOURCE_HOST}

    def config(self, **kw):
        values = {'user': USER, 'local_port': 1081, 'edge_ip': '8.8.8.8', 'interface': 'en0', 'route': 'physical'}
        values.update(kw)
        return q.client_config(self.plan, self.source, **values)

    def document(self):
        out = self.config(route='system')['outbounds'][0]
        out['settings']['vnext'][0]['address'] = self.plan['cdnDomain']
        return {'outbounds': [out]}

    def export(self, documents):
        with patch.object(q.p, 'fetch_documents', return_value=documents) as fetch:
            result = q.verify_export(self.plan, self.source, Path('/not-read/subscription'), interface='en0')
        fetch.assert_called_once_with(Path('/not-read/subscription'), 'en0')
        return result

    def test_client_uses_exact_reviewed_header_transport_tls_firefox_and_h2(self):
        before = copy.deepcopy(self.source)
        config = self.config()
        self.assertEqual(len(config['outbounds']), 1)
        out = config['outbounds'][0]
        self.assertEqual(out['protocol'], 'vless')
        self.assertEqual(out['settings']['vnext'], [{'address': '8.8.8.8', 'port': 443,
                                                   'users': [{'id': USER, 'encryption': 'none'}]}])
        stream = out['streamSettings']
        self.assertEqual(stream['network'], 'xhttp')
        self.assertEqual(stream['security'], 'tls')
        self.assertEqual(stream['tlsSettings'], {'serverName': 'cdn.example.com', 'allowInsecure': False,
                                                 'alpn': ['h2'], 'fingerprint': 'firefox'})
        self.assertEqual(stream['xhttpSettings']['mode'], 'packet-up')
        self.assertEqual(stream['xhttpSettings']['path'], self.plan['path'])
        self.assertEqual(stream['xhttpSettings']['host'], self.plan['cdnDomain'])
        self.assertEqual(stream['xhttpSettings']['extra'], c.CLIENT)
        self.assertEqual(stream['xhttpSettings']['extra']['uplinkHTTPMethod'], 'GET')
        self.assertEqual(stream['xhttpSettings']['extra']['uplinkDataPlacement'], 'header')
        self.assertEqual(self.source, before)

    def test_physical_route_is_bound_to_nic_without_direct_or_socks_fallback(self):
        config = self.config()
        self.assertEqual(config['outbounds'][0]['streamSettings']['sockopt'], {'interface': 'en0'})
        self.assertEqual({out['protocol'] for out in config['outbounds']}, {'vless'})
        self.assertNotIn('routing', config)
        self.assertEqual(config['inbounds'][0]['listen'], '127.0.0.1')
        self.assertFalse(config['inbounds'][0]['settings']['udp'])

    def test_system_route_does_not_claim_a_physical_nic(self):
        self.assertNotIn('sockopt', self.config(route='system')['outbounds'][0]['streamSettings'])

    def test_linux_system_allows_unused_lo_label_with_real_public_cdn_outbound(self):
        with patch.object(q.p.platform, 'system', return_value='Linux'):
            out = self.config(interface='lo', route='system')['outbounds'][0]
        self.assertEqual(out['settings']['vnext'][0]['address'], '8.8.8.8')
        self.assertEqual(out['streamSettings']['network'], 'xhttp')
        self.assertNotIn('sockopt', out['streamSettings'])
        self.assertNotIn('freedom', json.dumps(out))

    def test_linux_physical_is_rejected_before_dns_health_and_engine(self):
        with patch.object(q.p.platform, 'system', return_value='Linux'), \
                patch.object(q, 'addresses', side_effect=AssertionError('must not resolve')) as dns, \
                patch.object(q.p, 'select_engine', side_effect=AssertionError('must not run')) as engine:
            with self.assertRaises(q.ProbeError):
                q.run(self.plan, self.source, Path('/not-read/subscription'), interface='eth0')
        dns.assert_not_called()
        engine.assert_not_called()

    def test_linux_system_uses_native_temp_root_and_nine_authenticated_requests(self):
        child = SimpleNamespace(poll=lambda: None, pid=123)
        reservation = MagicMock()
        reservation.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 19999)
        real_temporary_directory = tempfile.TemporaryDirectory
        def temporary_directory(*args, **kwargs):
            self.assertIsNone(kwargs['dir'])
            return real_temporary_directory(*args, **kwargs)
        with patch.object(q.p.platform, 'system', return_value='Linux'), \
                patch.object(q, 'addresses', return_value=['8.8.8.8']), \
                patch.object(q, 'health', return_value={'passed': True}), \
                patch.object(q.p, 'select_engine', return_value=Path('/unused/test-engine')), \
                patch.object(q.p, 'fetch_credential', return_value=USER), \
                patch.object(q.p, 'physical_tcp_preflight', side_effect=AssertionError('Mac preflight')) as physical, \
                patch.object(q.subprocess, 'check_output', side_effect=AssertionError('Mac command')) as mac, \
                patch.object(q.socket, 'socket', return_value=reservation), \
                patch.object(q.socket, 'create_connection', return_value=MagicMock()), \
                patch.object(q.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                patch.object(q.subprocess, 'Popen', return_value=child), \
                patch.object(q.p, 'physical_observed', side_effect=AssertionError('Mac lsof')) as observed, \
                patch.object(q.p, 'request', return_value={'passed': True, 'clientAlive': True}) as request, \
                patch.object(q.tempfile, 'TemporaryDirectory', side_effect=temporary_directory), \
                patch.object(q.p.e, 'stop') as stop:
            proof = q.run(self.plan, self.source, Path('/not-read/subscription'), interface='lo', route='system')
        self.assertTrue(proof['passed'])
        self.assertEqual(proof['tests'], 9)
        self.assertEqual(request.call_count, 9)
        self.assertEqual(proof['testRoute'], 'system')
        self.assertIsNone(proof['physicalTcp'])
        self.assertFalse(proof['physicalSourceObserved'])
        self.assertFalse(proof['directReachabilityVerified'])
        self.assertTrue(all(call.args[2] == self.plan['ip'] for call in request.call_args_list))
        physical.assert_not_called()
        mac.assert_not_called()
        observed.assert_not_called()
        stop.assert_called_once_with(child)

    def test_probe_rejects_ssh_route_and_vpn_interfaces(self):
        for values in ({'route': 'ssh'}, {'interface': 'utun4'}, {'interface': 'lo0'}, {'interface': 'en0;id'}):
            with self.subTest(values=values), self.assertRaises(q.ProbeError):
                self.config(**values)

    def test_client_does_not_target_private_loopback_or_multicast_edge(self):
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '224.0.0.1', '::1'):
            with self.subTest(ip=ip), self.assertRaises(q.ProbeError):
                self.config(edge_ip=ip)

    def test_invalid_test_uuid_is_a_sanitized_probe_error(self):
        with self.assertRaises(q.ProbeError) as error:
            self.config(user='DO-NOT-ECHO-PRIVATE-INPUT')
        self.assertNotIn('DO-NOT-ECHO', str(error.exception))

    def test_public_edge_dns_results_are_deduplicated_and_private_answers_rejected(self):
        def rows(ips):
            return [(q.socket.AF_INET, q.socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in ips]
        with patch.object(q.socket, 'getaddrinfo', return_value=rows(['9.9.9.9', '8.8.8.8', '9.9.9.9'])):
            self.assertEqual(q.addresses('cdn.example.com'), ['8.8.8.8', '9.9.9.9'])
        for ips in ([], ['127.0.0.1'], ['8.8.8.8', '10.0.0.1'], ['224.0.0.1']):
            with self.subTest(ips=ips), patch.object(q.socket, 'getaddrinfo', return_value=rows(ips)), \
                    self.assertRaises(q.ProbeError):
                q.addresses('cdn.example.com')

    def test_valid_happ_json_export_with_nested_extra_is_verified(self):
        self.assertTrue(self.export(self.document())['exportVerified'])

    def test_export_supports_list_documents_and_string_encoded_extra(self):
        document = self.document()
        document['outbounds'][0]['streamSettings']['xhttpSettings']['extra'] = json.dumps(c.CLIENT)
        self.assertTrue(self.export([document])['passed'])

    def test_export_supports_flat_xhttp_extra_representation(self):
        document = self.document()
        xhttp = document['outbounds'][0]['streamSettings']['xhttpSettings']
        del xhttp['extra']
        xhttp.update(copy.deepcopy(c.CLIENT))
        self.assertTrue(self.export(document)['passed'])

    def test_identical_duplicate_documents_are_not_an_ambiguous_endpoint(self):
        document = self.document()
        self.assertTrue(self.export([document, copy.deepcopy(document)])['passed'])

    def test_export_rejects_altered_method_body_placement_tls_host_path_and_client_limits(self):
        changes = [lambda out: out['streamSettings']['xhttpSettings']['extra'].update(uplinkHTTPMethod='POST'),
                   lambda out: out['streamSettings']['xhttpSettings']['extra'].update(uplinkDataPlacement='body'),
                   lambda out: out['streamSettings']['xhttpSettings']['extra'].update(scMaxEachPostBytes='4096'),
                   lambda out: out['streamSettings']['xhttpSettings'].update(host='wrong.example.com'),
                   lambda out: out['streamSettings']['xhttpSettings'].update(path='/other/'),
                   lambda out: out['streamSettings']['xhttpSettings'].update(mode='stream-up'),
                   lambda out: out['streamSettings']['tlsSettings'].update(allowInsecure=True),
                   lambda out: out['streamSettings']['tlsSettings'].update(serverName='wrong.example.com'),
                   lambda out: out['streamSettings']['tlsSettings'].update(fingerprint='safari'),
                   lambda out: out['streamSettings']['tlsSettings'].update(alpn=['http/1.1']),
                   lambda out: out['streamSettings'].update(security='none')]
        for change in changes:
            document = self.document(); change(document['outbounds'][0])
            with self.subTest(change=change), self.assertRaises(q.ProbeError):
                self.export(document)

    def test_nested_extra_cannot_override_download_endpoint_host_or_path(self):
        for overrides in ({'downloadSettings': {'address': 'other.example.com'}},
                          {'host': 'other.example.com'}, {'path': '/other/'}, {'mode': 'stream-up'}):
            document = self.document()
            document['outbounds'][0]['streamSettings']['xhttpSettings']['extra'].update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(q.ProbeError):
                self.export(document)

    def test_export_cannot_hide_matching_cdn_in_second_vnext_server(self):
        document = self.document()
        out = document['outbounds'][0]
        correct = copy.deepcopy(out['settings']['vnext'][0])
        out['settings']['vnext'] = [{**copy.deepcopy(correct), 'address': 'wrong.example.com'}, correct]
        unrelated = self.document()['outbounds'][0]
        unrelated['settings']['vnext'][0]['address'] = 'other.example.com'
        document['outbounds'].append(unrelated)
        with self.assertRaises(q.ProbeError):
            self.export(document)

    def test_export_cannot_add_another_proxy_hop_or_incompatible_vision_flow(self):
        changes = [lambda out: out.update(proxySettings={'tag': 'unreviewed-proxy'}),
                   lambda out: out['streamSettings'].update(sockopt={'dialerProxy': 'unreviewed-proxy'}),
                   lambda out: out['settings']['vnext'][0]['users'][0].update(flow='xtls-rprx-vision')]
        for change in changes:
            document = self.document(); change(document['outbounds'][0])
            unrelated = self.document()['outbounds'][0]
            unrelated['settings']['vnext'][0]['address'] = 'other.example.com'
            document['outbounds'].append(unrelated)
            with self.subTest(change=change), self.assertRaises(q.ProbeError):
                self.export(document)

    def test_malformed_nested_extra_is_a_sanitized_probe_error(self):
        document = self.document()
        document['outbounds'][0]['streamSettings']['xhttpSettings']['extra'] = '[]'
        with self.assertRaises(q.ProbeError):
            self.export(document)

    def test_export_rejects_missing_candidate_or_two_different_candidate_configs(self):
        missing = self.document()
        missing['outbounds'][0]['settings']['vnext'][0]['address'] = 'other.example.com'
        with self.assertRaises(q.ProbeError):
            self.export(missing)
        first, second = self.document(), self.document()
        second['outbounds'][0]['streamSettings']['xhttpSettings']['extra']['scMaxEachPostBytes'] = '512'
        with self.assertRaises(q.ProbeError):
            self.export([first, second])

    def health(self, *, body=None, status=200, headers=None, require_h2=True, alpn='h2'):
        context = FakeContext(); context.alpn = alpn
        reply = FakeReply(body if body is not None else b'{"status":"ok","deployment":"a1b2c3"}\n', status, headers)
        factory = SimpleNamespace(create_default_context=lambda **kw: context)
        with patch.object(q, 'ssl', factory, create=True), \
                patch.object(q.p.e, 'tls_context', return_value=context, create=True), \
                patch.object(q.socket, 'create_connection', side_effect=lambda *a, **kw: FakeSocket(context)), \
                patch.object(q.http.client, 'HTTPResponse', return_value=reply):
            result = q.health('cdn.example.com', '8.8.8.8', 'a1b2c3', require_h2=require_h2)
        return result, context

    def test_health_checks_h2_then_verified_https_host_and_deployment_identity(self):
        result, context = self.health()
        self.assertTrue(result['tlsVerified'])
        self.assertTrue(result['h2Verified'])
        self.assertEqual(context.protocols, [['h2'], ['http/1.1']])
        self.assertEqual(context.server_names, ['cdn.example.com', 'cdn.example.com'])
        self.assertIn(b'GET /health HTTP/1.1\r\nHost: cdn.example.com\r\n', context.requests[0])

    def test_health_rejects_wrong_origin_cached_replies_bad_status_and_missing_h2(self):
        cases = [{'body': b'{"status":"ok","deployment":"old999"}'}, {'body': b'not-json'}, {'body': b'x' * 4097},
                 {'status': 502}, {'headers': {'Age': '12'}}, {'headers': {'X-Cache': 'HIT'}}, {'alpn': 'http/1.1'}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(q.ProbeError):
                self.health(**values)


if __name__ == '__main__':
    unittest.main()
