"""Origin-local TLS tests with fake sockets: no external connections."""
import importlib.util
from pathlib import Path
import ssl
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('yandex_origin_health_tests', HERE / 'origin_health.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class Socket:
    def __init__(self):
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def sendall(self, data):
        self.requests.append(data)


class OriginHealthTests(unittest.TestCase):
    def check(self, *, body=b'{"status":"ok","deployment":"a1b2c3"}\n', status=200, connect_error=None, tls_error=None):
        stream, tls = Socket(), Socket()
        context = Mock()
        context.wrap_socket.side_effect = tls_error
        context.wrap_socket.return_value = tls
        response = Mock(status=status)
        response.read.return_value = body
        with patch.object(h.ssl, 'create_default_context', return_value=context) as factory, \
                patch.object(h.socket, 'create_connection', return_value=stream, side_effect=connect_error) as connect, \
                patch.object(h.http.client, 'HTTPResponse', return_value=response):
            result = h.check('origin.example.com', '8.8.4.4', 'a1b2c3')
        factory.assert_called_once_with()
        connect.assert_called_once_with(('8.8.4.4', 443), timeout=5)
        context.wrap_socket.assert_called_once_with(stream, server_hostname='origin.example.com')
        self.assertIn(b'Host: origin.example.com\r\n', tls.requests[0])
        return result

    def test_local_proof_checks_exact_https_identity_and_never_claims_cdn_success(self):
        result = self.check()
        self.assertTrue(result['passed'])
        self.assertTrue(result['tlsVerified'])
        self.assertFalse(result['cdnVerified'])
        self.assertEqual(result['scope'], 'origin-local-ssh')

    def test_bad_health_status_wrong_origin_and_invalid_json_fail_without_body_leak(self):
        for values in ({'status': 503}, {'body': b'{"status":"ok","deployment":"old999"}'},
                       {'body': b'PRIVATE-BODY-DO-NOT-ECHO'}, {'body': b'x' * 4097}):
            with self.subTest(values=values), self.assertRaises(h.OriginError) as error:
                self.check(**values)
            self.assertNotIn('PRIVATE-BODY', str(error.exception))

    def test_tls_verification_failure_is_specific_and_never_disables_tls(self):
        error = ssl.SSLCertVerificationError(1, 'PRIVATE-PARSER-INPUT')
        with self.assertRaisesRegex(h.OriginError, 'certificate verification failed') as caught:
            self.check(tls_error=error)
        self.assertNotIn('PRIVATE-PARSER', str(caught.exception))

    def test_timeout_is_reported_as_origin_local_not_yandex_resource_failure(self):
        with self.assertRaisesRegex(h.OriginError, 'VPS itself'):
            self.check(connect_error=TimeoutError('PRIVATE-NETWORK-TEXT'))

    def test_bad_public_identity_is_rejected_before_any_network(self):
        for domain, ip, code in (('origin.example.com\r\nBad: x', '8.8.4.4', 'a1b2c3'),
                                 ('origin.example.com', '127.0.0.1', 'a1b2c3'),
                                 ('origin.example.com', '8.8.4.4', '../oops')):
            with self.subTest(domain=domain, ip=ip, code=code), \
                    patch.object(h.socket, 'create_connection', side_effect=AssertionError('network')), \
                    self.assertRaises(h.OriginError):
                h.check(domain, ip, code)


if __name__ == '__main__':
    unittest.main()
