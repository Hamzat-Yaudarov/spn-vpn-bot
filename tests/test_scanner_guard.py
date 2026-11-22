"""Scanner-list integrity, bounded downloads and last-good cache behavior."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('spn_scanner_guard_test', ROOT / 'ops/universal-reality/scanner_guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
GOOD = b'# published scanners\n185.224.228.0/24\n185.224.229.0/24\n2a0c:a9c7:156::/48\n'
TIME = '2026-11-18T17:00:00Z'


class Response:
    def __init__(self, body=GOOD, status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {}
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        self.reads += 1
        return self.body.read(size)

    def isclosed(self):
        return self.body.closed

    def close(self):
        self.body.close()


class Connection:
    def __init__(self, response):
        self.response = response
        self.sock = self
        self.timeouts = []
        self.requests = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def close(self):
        self.closed = True


class ScannerGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.directory = Path(self.temp.name).resolve()
        self.directory.chmod(0o700)
        self.cache = self.directory / 'scanners.json'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value):
        self.cache.write_text(json.dumps(value))
        self.cache.chmod(0o600)

    def test_bundled_source_is_pinned_and_addresses_are_derived_from_verified_content(self):
        value = guard.load_snapshot()
        self.assertEqual(value['sources'][0]['revision'], guard.PINNED_REVISION)
        self.assertEqual(value['sources'][0]['sha256'], '807bf9cfc91dc621cceaa7880747ef2bb342a726543ee62bd29eab120344ef29')
        self.assertEqual(len(value['ipv4']), 139)
        self.assertEqual(len(value['ipv6']), 2)
        self.assertEqual([item['name'] for item in value['sources']], ['traffic-guard-antiscanner'])
        self.assertIn('no separate LICENSE', value['sources'][0]['license'])

    def test_overlapping_duplicate_and_adjacent_ranges_collapse_for_nft_interval_sets(self):
        raw = GOOD + b'185.224.228.0/24 # duplicate\n185.224.228.128/25\n'
        self.assertEqual(guard.parse_list(raw), (['185.224.228.0/23'], ['2a0c:a9c7:156::/48']))

    def test_invalid_non_public_overbroad_and_empty_lists_are_rejected_whole(self):
        for raw in (b'', b'# no entries', b'8.8.8.8', b'8.8.8.8/24', b'0.0.0.0/0',
                    b'8.0.0.0/19', b'2a0c:a9c7::/32', b'127.0.0.1/32', b'10.0.0.0/20',
                    b'224.0.0.0/24', b'ff01::/64', b'192.0.2.0/24', b'2001:db8::/48',
                    b'8.8.8.0/24; flush ruleset', b'8.8.8.0/24\ninvalid', b'\xff', b'8' * 513,
                    b'#\x00hidden\n8.8.8.8/32'):
            with self.subTest(raw=raw[:50]), self.assertRaises(guard.ScannerError):
                guard.parse_list(raw)

    def test_per_range_checks_cannot_be_used_to_cover_a_large_aggregate(self):
        raw = ('\n'.join('8.' + str(number) + '.0.0/20' for number in range(17))).encode()
        with self.assertRaisesRegex(guard.ScannerError, 'too many addresses'):
            guard.parse_list(raw)

    def test_snapshot_rejects_changed_source_hash_derived_sets_and_source_identity(self):
        value = guard.snapshot(GOOD, updated_at=TIME)
        for mutate in (lambda v: v['ipv4'].append('8.8.8.8/32'),
                       lambda v: v['sources'][0].update(sha256='0' * 64),
                       lambda v: v['sources'][0].update(content='8.8.8.8/32'),
                       lambda v: v['sources'][0].update(url='https://attacker.invalid/list'),
                       lambda v: v.update(updatedAt='2026-99-99T17:00:00Z'),
                       lambda v: v.update(version=True)):
            changed = copy.deepcopy(value)
            mutate(changed)
            with self.assertRaises(guard.ScannerError):
                guard.validate_snapshot(changed)

    def test_valid_refresh_is_private_atomic_and_round_trips(self):
        result = guard.refresh(self.cache, fetch=lambda: GOOD, updated_at=TIME)
        self.assertEqual(guard.load_snapshot(self.cache), result)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.directory.iterdir()), [self.cache])

    def test_invalid_download_never_changes_last_good_snapshot(self):
        old = guard.refresh(self.cache, fetch=lambda: GOOD, updated_at=TIME)
        before = self.cache.read_bytes()
        with self.assertRaises(guard.ScannerError):
            guard.refresh(self.cache, fetch=lambda: b'<html>Service unavailable</html>')
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(guard.load_snapshot(self.cache), old)
        self.assertEqual(list(self.directory.iterdir()), [self.cache])

    def test_network_failure_never_changes_last_good_snapshot(self):
        guard.refresh(self.cache, fetch=lambda: GOOD, updated_at=TIME)
        before = self.cache.read_bytes()
        def failing():
            raise guard.ScannerError('offline')
        with self.assertRaisesRegex(guard.ScannerError, 'offline'):
            guard.refresh(self.cache, fetch=failing)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_corrupt_existing_cache_is_not_silently_adopted_or_overwritten(self):
        self.cache.write_text('{broken')
        self.cache.chmod(0o600)
        with self.assertRaises(guard.ScannerError):
            guard.refresh(self.cache, fetch=lambda: self.fail('Corrupt cache must fail before network'))
        self.assertEqual(self.cache.read_text(), '{broken')

    def test_symlinks_and_hardlinks_are_not_followed_or_replaced(self):
        value = guard.snapshot(GOOD, updated_at=TIME)
        self.write(value)
        other = self.directory / 'other.json'
        self.cache.rename(other)
        self.cache.symlink_to(other)
        with self.assertRaises(guard.ScannerError):
            guard.refresh(self.cache, fetch=lambda: GOOD)
        self.cache.unlink()
        os.link(other, self.cache)
        with self.assertRaises(guard.ScannerError):
            guard.refresh(self.cache, fetch=lambda: GOOD)
        self.assertEqual(json.loads(other.read_text()), value)

    def test_cache_must_have_private_parent_and_file_permissions(self):
        self.write(guard.snapshot(GOOD, updated_at=TIME))
        self.cache.chmod(0o644)
        with self.assertRaises(guard.ScannerError):
            guard.load_snapshot(self.cache)
        self.cache.chmod(0o600)
        self.directory.chmod(0o755)
        with self.assertRaises(guard.ScannerError):
            guard.refresh(self.cache, fetch=lambda: GOOD)

    def test_concurrent_cache_change_is_preserved_instead_of_lost(self):
        old = guard.refresh(self.cache, fetch=lambda: GOOD, updated_at=TIME)
        concurrent = guard.snapshot(b'8.8.8.8/32\n', updated_at=TIME)
        def changed():
            self.write(concurrent)
            return GOOD
        with self.assertRaisesRegex(guard.ScannerError, 'changed during refresh'):
            guard.refresh(self.cache, fetch=changed)
        self.assertEqual(guard.load_snapshot(self.cache), concurrent)
        self.assertNotEqual(old, concurrent)
        self.assertEqual(list(self.directory.iterdir()), [self.cache])

    def test_download_uses_fixed_source_verified_tls_and_no_redirects(self):
        connection = Connection(Response())
        with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection) as https:
            self.assertEqual(guard.download(), GOOD)
        self.assertEqual(https.call_args.args, ('raw.githubusercontent.com',))
        self.assertTrue(https.call_args.kwargs['context'].check_hostname)
        self.assertEqual(connection.requests[0][0], ('GET', '/shadow-netlab/traffic-guard-lists/refs/heads/main/public/antiscanner.list'))
        self.assertTrue(connection.closed)
        connection = Connection(Response(status=302, headers={'Location':'https://elsewhere.invalid/'}))
        with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection), \
                self.assertRaisesRegex(guard.ScannerError, 'redirects are refused'):
            guard.download()
        self.assertEqual(connection.response.reads, 0)
        self.assertEqual(len(connection.requests), 1)

    def test_download_rejects_compression_oversized_headers_and_oversized_streams(self):
        for response in (Response(headers={'Content-Encoding':'gzip'}),
                         Response(headers={'Content-Length':str(guard.MAX_BYTES + 1)}),
                         Response(headers={'Content-Length':'invalid'}),
                         Response(body=b'x' * (guard.MAX_BYTES + 1))):
            connection = Connection(response)
            with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection), \
                    self.assertRaises(guard.ScannerError):
                guard.download()
            self.assertTrue(connection.closed)

    def test_download_deadline_catches_a_peer_that_drips_data(self):
        connection = Connection(Response())
        with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection), \
                patch.object(guard.time, 'monotonic', side_effect=[0, 1, 13]), \
                self.assertRaisesRegex(guard.ScannerError, 'deadline'):
            guard.download()
        self.assertEqual(connection.response.reads, 1)
        self.assertTrue(connection.closed)

    def test_download_socket_timeout_closes_connection_and_is_sanitized(self):
        connection = Connection(Response())
        connection.getresponse = lambda: (_ for _ in ()).throw(socket.timeout('sensitive transport detail'))
        with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection), \
                self.assertRaisesRegex(guard.ScannerError, '^Scanner list HTTPS download failed; previous snapshot is retained$'):
            guard.download()
        self.assertTrue(connection.closed)

    def real_http(self, response_bytes):
        """Actual HTTPResponse lifecycle, including makefile closing its FD."""
        receiving, sending = socket.socketpair()
        self.addCleanup(receiving.close)
        self.addCleanup(sending.close)
        sending.sendall(response_bytes)
        sending.shutdown(socket.SHUT_WR)
        response = guard.http.client.HTTPResponse(receiving)
        response.begin()
        connection = Connection(response)
        connection.sock = receiving
        original_getresponse = connection.getresponse
        def detached_response():
            result = original_getresponse()
            # HTTPConnection.getresponse does the equivalent for a response
            # carrying Connection: close: only the makefile keeps FD alive.
            receiving.close()
            return result
        connection.getresponse = detached_response
        return connection, response

    def test_real_http_content_length_final_read_closes_fd_without_breaking_download(self):
        headers = b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: ' + str(len(GOOD)).encode() + b'\r\n\r\n'
        connection, response = self.real_http(headers + GOOD)
        with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection):
            self.assertEqual(guard.download(), GOOD)
        self.assertTrue(response.isclosed())
        self.assertTrue(connection.closed)
        with self.assertRaises(OSError):
            connection.sock.settimeout(1)

    def test_real_http_truncated_content_length_is_rejected_and_response_closed(self):
        headers = b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: ' + str(len(GOOD) + 12).encode() + b'\r\n\r\n'
        connection, response = self.real_http(headers + GOOD)
        with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection), \
                self.assertRaisesRegex(guard.ScannerError, 'ended before its declared Content-Length'):
            guard.download()
        self.assertTrue(response.isclosed())
        self.assertTrue(connection.closed)

    def test_real_http_chunked_and_eof_bodies_close_the_response_cleanly(self):
        for body in (b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n' + GOOD,
                     b'HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n'
                     + hex(len(GOOD))[2:].encode() + b'\r\n' + GOOD + b'\r\n0\r\n\r\n'):
            with self.subTest(body=body[:70]):
                connection, response = self.real_http(body)
                with patch.object(guard.http.client, 'HTTPSConnection', return_value=connection):
                    self.assertEqual(guard.download(), GOOD)
                self.assertTrue(response.isclosed())
                self.assertTrue(connection.closed)


if __name__ == '__main__':
    unittest.main()
