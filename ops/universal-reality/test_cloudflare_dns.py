"""Offline Cloudflare provisioning tests: no network, credentials or DNS writes."""
import copy
from http.client import IncompleteRead
import importlib.util
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse


spec = importlib.util.spec_from_file_location('reality_cloudflare_under_test', Path(__file__).with_name('cloudflare_dns.py'))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
ZONE, RECORD = '1' * 32, '2' * 32
NAME, IP = 'vpn.example.com', '192.0.2.7'


class Store:
    def __init__(self):
        self.data = {}

    def load(self, name):
        return copy.deepcopy(self.data.get(name))

    def save(self, name, value):
        if name in self.data and self.data[name] != value:
            raise AssertionError('Immutable state changed')
        self.data[name] = copy.deepcopy(value)


class FakeCloudflare:
    def __init__(self, store):
        self.store = store
        self.records = []
        self.zones = [{'id': ZONE, 'name': 'example.com', 'status': 'active'}]
        self.calls = []
        self.post_mode = 'ok'
        self.patch_mode = 'ok'
        self.after_patch = None
        self.page_size = 50

    def open(self, request, *, timeout):
        parsed = urlparse(request.full_url)
        self.calls.append((request.get_method(), parsed.path, request.data))
        assert parsed.scheme == 'https' and parsed.netloc == 'api.cloudflare.com'
        assert request.get_header('Authorization') == 'Bearer test-token'
        assert 'test-token' not in request.full_url
        if request.get_method() == 'POST':
            assert self.store.load(d.INTENT), 'Intent must be durable before POST'
            assert parsed.path.endswith('/zones/' + ZONE + '/dns_records')
            record = {'id': RECORD, **json.loads(request.data)}
            if self.post_mode != 'lost-before':
                self.records.append(record)
            if self.post_mode.startswith('lost-'):
                raise OSError('test-token must not leak')
            result = {'success': True, 'result': record}
        elif request.get_method() == 'PATCH':
            assert self.store.load(d.UPDATE_INTENT), 'Replacement intent must be durable before PATCH'
            assert self.store.load(d.UPDATE_PENDING), 'Replacement pending marker must be durable before PATCH'
            assert parsed.path.endswith('/zones/' + ZONE + '/dns_records/' + RECORD)
            body = json.loads(request.data)
            assert list(body) == ['content'], 'Only content may be patched'
            record = next(row for row in self.records if row['id'] == RECORD)
            if self.patch_mode not in ('lost-before', 'http-before'):
                record.update(body)
            if self.after_patch is not None:
                self.after_patch(record)
            if self.patch_mode.startswith('lost-'):
                raise OSError('test-token must not leak')
            if self.patch_mode.startswith('http-'):
                raise HTTPError(request.full_url, 503, 'test-token', None, io.BytesIO(b'test-token'))
            if self.patch_mode == 'invalid-after':
                return io.BytesIO(b'test-token is not JSON')
            if self.patch_mode == 'truncated-after':
                raise IncompleteRead(b'test-token must not leak')
            result = {'success': self.patch_mode != 'false-after', 'result': record}
        else:
            assert request.get_method() == 'GET', 'Deletes are never allowed'
            if parsed.path.endswith('/zones/' + ZONE):
                result = {'success': True, 'result': self.zones[0]}
            elif '/dns_records/' in parsed.path:
                identifier = parsed.path.rsplit('/', 1)[-1]
                result = {'success': True, 'result': next((row for row in self.records if row['id'] == identifier), None)}
            else:
                query = parse_qs(parsed.query)
                page = int(query['page'][0])
                if parsed.path.endswith('/dns_records'):
                    assert query['name.exact'] == [NAME]
                    rows = self.records
                else:
                    assert parsed.path.endswith('/zones')
                    assert query['name'] == ['example.com']
                    rows = self.zones
                size = self.page_size
                result = {'success': True, 'result': rows[(page - 1) * size:page * size],
                          'result_info': {'page': page, 'total_pages': max(1, (len(rows) + size - 1) // size)}}
        return io.BytesIO(json.dumps(result).encode())


class CloudflareProvisionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.fake = FakeCloudflare(self.store)
        self.client = d.CloudflareDNS('test-token', opener=self.fake.open)

    def ensure(self, **extra):
        return self.client.ensure_record(self.store, **{'zone_id': ZONE, 'name': NAME, 'ipv4': IP, **extra})

    def check(self, **extra):
        return self.client.check_record(self.store, **{'zone_id': ZONE, 'name': NAME, 'ipv4': IP, **extra})

    def record(self, **extra):
        return {'id': RECORD, 'name': NAME, 'type': 'A', 'content': IP, 'proxied': False, 'ttl': 60, **extra}

    def posts(self):
        return [call for call in self.fake.calls if call[0] == 'POST']

    def test_tls_context_uses_certifi_or_system_roots_without_disabling_verification(self):
        context = d.ssl.create_default_context()
        for certifi, kwargs in ((types.SimpleNamespace(where=lambda: '/test/ca-bundle.pem'),
                                  {'cafile': '/test/ca-bundle.pem'}), (None, {})):
            with self.subTest(certifi=certifi is not None), patch.dict('sys.modules', {'certifi': certifi}), \
                    patch.object(d.ssl, 'create_default_context', return_value=context) as create:
                self.assertIs(d.tls_context(), context)
                create.assert_called_once_with(**kwargs)
                self.assertEqual(context.verify_mode, d.ssl.CERT_REQUIRED)
                self.assertIs(context.check_hostname, True)

    def test_default_opener_has_verified_https_context_and_rejects_redirects(self):
        context = d.ssl.create_default_context()
        with patch.object(d, 'tls_context', return_value=context), patch.object(d, 'build_opener') as build:
            client = d.CloudflareDNS('test-token')
            handlers = build.call_args.args
            self.assertIsInstance(handlers[0], d._RejectRedirects)
            self.assertIsInstance(handlers[1], d.HTTPSHandler)
            self.assertIs(handlers[1]._context, context)
            self.assertIs(client._open, build.return_value.open)

    def test_preflight_is_read_only_and_creation_is_owned_dns_only_ttl_60(self):
        self.assertIsNone(self.check())
        self.assertEqual(self.store.data, {})
        record = self.ensure()
        self.assertEqual(record['id'], RECORD)
        intent, receipt = self.store.load(d.INTENT), self.store.load(d.RECEIPT)
        self.assertEqual(receipt['id'], RECORD)
        self.assertEqual(intent['content'], IP)
        self.assertEqual(intent['ttl'], 60)
        self.assertIs(intent['proxied'], False)
        self.assertNotIn('test-token', json.dumps(self.store.data))
        self.assertTrue(intent['comment'].startswith('SPN universal-reality:'))
        self.ensure()
        self.assertEqual(len(self.posts()), 1)

    def test_exact_active_zone_lookup_is_read_only(self):
        self.assertEqual(self.client.find_zone('EXAMPLE.com.')['id'], ZONE)
        self.assertEqual(self.store.data, {})
        self.assertEqual(self.posts(), [])
        self.fake.zones.append({**self.fake.zones[0], 'id': '3' * 32})
        with self.assertRaises(d.DNSConflict):
            self.client.find_zone('example.com')

    def test_zone_must_contain_hostname_and_be_active(self):
        for update in ({'name': 'other.example.net'}, {'status': 'pending'}, {'id': '3' * 32}):
            with self.subTest(update=update):
                self.fake.zones[0] = {'id': ZONE, 'name': 'example.com', 'status': 'active', **update}
                with self.assertRaises(d.DNSConflict):
                    self.ensure()
                self.assertEqual(self.posts(), [])

    def test_refuses_unowned_matching_or_changed_records(self):
        for update in ({}, {'content': '192.0.2.8'}, {'proxied': True}, {'type': 'AAAA', 'content': '2001:db8::1'},
                       {'type': 'CNAME', 'content': 'another.example.com'}):
            with self.subTest(update=update):
                self.fake.records = [self.record(**update)]
                with self.assertRaises(d.DNSConflict):
                    self.ensure()
                self.assertEqual(self.posts(), [])
                self.assertEqual(self.store.data, {})

    def test_refuses_duplicates_including_records_on_later_pages(self):
        self.fake.records = [self.record(), self.record(id='3' * 32)]
        self.fake.page_size = 1
        with self.assertRaises(d.DNSConflict):
            self.ensure()
        self.assertEqual(self.posts(), [])
        self.assertEqual(len(self.fake.calls), 3)  # zone GET and both DNS pages

    def test_rerun_does_not_overwrite_manual_ip_proxy_ttl_or_identity_changes(self):
        self.ensure()
        original = copy.deepcopy(self.fake.records[0])
        for update in ({'content': '192.0.2.8'}, {'proxied': True}, {'ttl': 120}, {'id': '3' * 32}, {'comment': 'operator'}):
            with self.subTest(update=update):
                self.fake.records = [{**original, **update}]
                with self.assertRaises(d.DNSConflict):
                    self.ensure()
                self.assertEqual(len(self.posts()), 1)

    def test_new_requested_ip_needs_a_separate_explicit_move_workflow(self):
        self.ensure()
        count = len(self.fake.calls)
        with self.assertRaises(d.DNSConflict):
            self.ensure(ipv4='192.0.2.8')
        self.assertEqual(len(self.fake.calls), count)
        self.assertEqual(len(self.posts()), 1)

    def test_lost_post_response_recovers_one_exact_owned_record_by_get_only(self):
        self.fake.post_mode = 'lost-after'
        with self.assertRaises(d.DNSOutcomeUnknown) as error:
            self.ensure()
        self.assertNotIn('test-token', str(error.exception))
        self.assertIsNotNone(self.store.load(d.INTENT))
        self.assertIsNone(self.store.load(d.RECEIPT))
        self.assertEqual(self.ensure()['id'], RECORD)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.store.load(d.RECEIPT)['id'], RECORD)

    def test_lost_post_without_record_is_never_automatically_repeated(self):
        self.fake.post_mode = 'lost-before'
        with self.assertRaises(d.DNSOutcomeUnknown):
            self.ensure()
        for _ in range(2):
            with self.assertRaises(d.DNSOutcomeUnknown):
                self.ensure()
        with self.assertRaises(d.DNSOutcomeUnknown):
            self.check()
        self.assertEqual(len(self.posts()), 1)

    def test_pending_intent_cannot_adopt_a_foreign_matching_record(self):
        self.fake.post_mode = 'lost-before'
        with self.assertRaises(d.DNSOutcomeUnknown):
            self.ensure()
        self.fake.records = [self.record()]
        with self.assertRaises(d.DNSConflict):
            self.ensure()
        self.assertEqual(len(self.posts()), 1)
        self.assertIsNone(self.store.load(d.RECEIPT))

    def test_missing_receipted_record_is_not_recreated(self):
        self.ensure()
        self.fake.records.clear()
        with self.assertRaises(d.DNSConflict):
            self.ensure()
        self.assertEqual(len(self.posts()), 1)

    def test_new_aaaa_conflicts_even_with_owned_a(self):
        self.ensure()
        self.fake.records.append(self.record(id='3' * 32, type='AAAA', content='2001:db8::1'))
        with self.assertRaises(d.DNSConflict):
            self.ensure()
        self.assertEqual(len(self.posts()), 1)

    def test_bad_pagination_or_body_stops_without_mutation(self):
        for payload in ({'success': True, 'result': []}, {'success': True, 'result': 'bad'}, {'success': False}):
            with self.subTest(payload=payload):
                client = d.CloudflareDNS('test-token', opener=lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
                with self.assertRaises(d.DNSProvisionError):
                    client.find_zone('example.com')
        self.assertEqual(self.store.data, {})

    def test_http_error_body_does_not_expose_credentials(self):
        def reject(request, *, timeout):
            raise HTTPError(request.full_url, 403, 'test-token', None, io.BytesIO(b'test-token'))
        client = d.CloudflareDNS('test-token', opener=reject)
        with self.assertRaises(d.DNSProvisionError) as error:
            client.find_zone('example.com')
        self.assertNotIn('test-token', str(error.exception))
        self.assertIn('HTTP 403', str(error.exception))

    def test_domain_and_ttl_validation_happens_before_network(self):
        for name in ('192.0.2.7', 'vpn', '*.example.com', 'vpn.example.com/path', ' vpn.example.com', 'a..example.com'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.ensure(name=name)
        for ttl in (1, 30, True, 86401):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.ensure(ttl=ttl)
        self.assertEqual(self.fake.calls, [])


class CloudflareReplacementTests(unittest.TestCase):
    NEW_IP = '192.0.2.8'

    def setUp(self):
        self.original_store = Store()
        self.fake = FakeCloudflare(self.original_store)
        self.client = d.CloudflareDNS('test-token', opener=self.fake.open)
        self.client.ensure_record(self.original_store, zone_id=ZONE, name=NAME, ipv4=IP)
        self.original_state = copy.deepcopy(self.original_store.data)
        self.receipt = self.original_store.load(d.RECEIPT)
        self.original_record = copy.deepcopy(self.fake.records[0])
        self.store = Store()
        self.fake.store = self.store
        self.fake.calls.clear()

    def update(self, **extra):
        return self.client.update_record(self.store, **{
            'receipt': self.receipt, 'old_ipv4': IP, 'new_ipv4': self.NEW_IP, **extra})

    def check(self, **extra):
        return self.client.check_update(self.store, **{
            'receipt': self.receipt, 'old_ipv4': IP, 'new_ipv4': self.NEW_IP, **extra})

    def writes(self):
        return [call for call in self.fake.calls if call[0] != 'GET']

    def test_preflight_reads_exact_id_and_full_name_without_saving_or_mutating(self):
        self.assertEqual(self.check()['content'], IP)
        self.assertEqual(self.store.data, {})
        self.assertEqual(self.writes(), [])
        self.assertTrue(any(call[1].endswith('/dns_records/' + RECORD) for call in self.fake.calls))

    def test_patch_only_changes_content_with_durable_snapshots_and_markers(self):
        self.fake.records[0]['tags'] = ['keep-this']
        self.fake.records[0]['settings'] = {'ipv4_only': True}
        initial = copy.deepcopy(self.fake.records[0])
        result = self.update()
        self.assertEqual(result, {**initial, 'content': self.NEW_IP})
        self.assertEqual(self.original_store.data, self.original_state)
        self.assertEqual(self.receipt['content'], IP)
        intent = self.store.load(d.UPDATE_INTENT)
        self.assertRegex(intent['operation_id'], r'^[0-9a-f]{32}$')
        self.assertEqual(intent['before'], self.receipt)
        self.assertEqual(intent['after'], {**self.receipt, 'content': self.NEW_IP})
        self.assertEqual(self.store.load(d.UPDATE_PENDING)['operation_id'], intent['operation_id'])
        self.assertEqual(self.store.load(d.UPDATE_RECEIPT)['record'], intent['after'])
        self.assertEqual([(method, json.loads(body)) for method, _, body in self.writes()],
                         [('PATCH', {'content': self.NEW_IP})])
        self.assertNotIn('test-token', json.dumps(self.store.data))
        self.check()
        self.update()
        self.assertEqual(len(self.writes()), 1)

    def test_later_move_uses_committed_predecessor_not_original_receipt_ip(self):
        self.update()
        self.store = Store()
        self.fake.store = self.store
        result = self.update(old_ipv4=self.NEW_IP, new_ipv4='192.0.2.9')
        self.assertEqual(result['content'], '192.0.2.9')
        self.assertEqual(self.store.load(d.UPDATE_INTENT)['before']['content'], self.NEW_IP)
        self.assertEqual(self.original_store.data, self.original_state)
        self.assertEqual(len(self.writes()), 2)

    def test_ambiguous_success_is_reconciled_immediately_without_second_patch(self):
        for mode in ('lost-after', 'http-after', 'invalid-after', 'false-after', 'truncated-after'):
            with self.subTest(mode=mode):
                self.setUp()
                self.fake.patch_mode = mode
                self.assertEqual(self.update()['content'], self.NEW_IP)
                self.assertEqual(self.update()['content'], self.NEW_IP)
                self.assertEqual(len(self.writes()), 1)
                self.assertIsNotNone(self.store.load(d.UPDATE_RECEIPT))

    def test_ambiguous_unchanged_ip_is_never_retried(self):
        for mode in ('lost-before', 'http-before'):
            with self.subTest(mode=mode):
                self.setUp()
                self.fake.patch_mode = mode
                for operation in (self.update, self.update, self.check):
                    with self.assertRaises(d.DNSOutcomeUnknown) as error:
                        operation()
                    self.assertNotIn('test-token', str(error.exception))
                self.assertEqual(self.fake.records[0]['content'], IP)
                self.assertEqual(len(self.writes()), 1)
                self.assertIsNone(self.store.load(d.UPDATE_RECEIPT))

    def test_readback_failure_stays_pending_and_read_only_rerun_recovers(self):
        def unavailable_after_patch(request, **kwargs):
            if request.get_method() == 'GET' and self.writes():
                raise OSError('test-token must not leak')
            return self.fake.open(request, **kwargs)
        self.client._open = unavailable_after_patch
        with self.assertRaises(d.DNSOutcomeUnknown) as error:
            self.update()
        self.assertNotIn('test-token', str(error.exception))
        self.assertEqual(self.fake.records[0]['content'], self.NEW_IP)
        self.assertIsNone(self.store.load(d.UPDATE_RECEIPT))
        self.client._open = self.fake.open
        saved = copy.deepcopy(self.store.data)
        self.assertEqual(self.check()['content'], self.NEW_IP)
        self.assertEqual(self.store.data, saved)
        self.assertEqual(self.update()['content'], self.NEW_IP)
        self.assertEqual(len(self.writes()), 1)

    def test_receipt_save_failure_is_recovered_by_exact_id_without_new_patch(self):
        save = self.store.save
        def fail_receipt(name, value):
            if name == d.UPDATE_RECEIPT:
                raise OSError('Local receipt save failed')
            save(name, value)
        with patch.object(self.store, 'save', side_effect=fail_receipt), self.assertRaises(OSError):
            self.update()
        self.assertEqual(self.update()['content'], self.NEW_IP)
        self.assertEqual(len(self.writes()), 1)

    def test_crash_before_pending_can_resume_but_crash_after_pending_cannot_retry(self):
        for durable_pending in (False, True):
            with self.subTest(durable_pending=durable_pending):
                self.setUp()
                save = self.store.save
                def fail_pending(name, value):
                    if name != d.UPDATE_PENDING or durable_pending:
                        save(name, value)
                    if name == d.UPDATE_PENDING:
                        raise OSError('Interrupted before PATCH')
                with patch.object(self.store, 'save', side_effect=fail_pending), self.assertRaises(OSError):
                    self.update()
                self.assertEqual(self.writes(), [])
                if durable_pending:
                    with self.assertRaises(d.DNSOutcomeUnknown):
                        self.update()
                    self.assertEqual(self.writes(), [])
                else:
                    self.assertEqual(self.update()['content'], self.NEW_IP)
                    self.assertEqual(len(self.writes()), 1)

    def test_no_patch_when_intent_cannot_be_durable(self):
        with patch.object(self.store, 'save', side_effect=OSError('No durable state')), self.assertRaises(OSError):
            self.update()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fake.records[0]['content'], IP)

    def test_refuses_drift_duplicates_missing_record_and_foreign_ownership(self):
        for change in ({'id': '3' * 32}, {'content': '192.0.2.99'}, {'proxied': True}, {'ttl': 120},
                       {'type': 'AAAA', 'content': '2001:db8::1'}, {'comment': 'operator'},
                       {'comment': 'SPN universal-reality:' + 'f' * 32}):
            with self.subTest(change=change):
                self.fake.records = [{**self.original_record, **change}]
                with self.assertRaises(d.DNSConflict):
                    self.update()
                self.assertEqual(self.store.data, {})
        for other in (None, {'id': '3' * 32}, {'id': '3' * 32, 'type': 'AAAA', 'content': '2001:db8::1'},
                      {'id': '3' * 32, 'type': 'CNAME', 'content': 'other.example.com'}):
            with self.subTest(other=other):
                self.fake.records = [] if other is None else [self.original_record, {**self.original_record, **other}]
                self.fake.page_size = 1
                with self.assertRaises(d.DNSConflict):
                    self.update()
                self.assertEqual(self.store.data, {})
        self.assertEqual(self.writes(), [])

    def test_new_ip_without_this_operation_pending_marker_is_not_adopted(self):
        self.fake.records[0]['content'] = self.NEW_IP
        with self.assertRaises(d.DNSConflict):
            self.update()
        self.assertEqual(self.store.data, {})
        self.assertEqual(self.writes(), [])

    def test_concurrent_drift_after_pending_is_detected_before_patch(self):
        save = self.store.save
        def drift(name, value):
            save(name, value)
            if name == d.UPDATE_PENDING:
                self.fake.records[0]['ttl'] = 120
        with patch.object(self.store, 'save', side_effect=drift), self.assertRaises(d.DNSConflict):
            self.update()
        self.assertIsNotNone(self.store.load(d.UPDATE_PENDING))
        self.assertEqual(self.writes(), [])

    def test_concurrent_drift_between_listing_and_exact_id_is_detected(self):
        def drift(request, **kwargs):
            if request.full_url.endswith('/dns_records/' + RECORD):
                self.fake.records[0]['content'] = '192.0.2.99'
            return self.fake.open(request, **kwargs)
        self.client._open = drift
        with self.assertRaises(d.DNSConflict):
            self.update()
        self.assertEqual(self.store.data, {})
        self.assertEqual(self.writes(), [])

    def test_postpatch_concurrent_metadata_or_duplicate_conflict_is_not_committed(self):
        for change in ('ttl', 'comment', 'id', 'duplicate', 'AAAA'):
            with self.subTest(change=change):
                self.setUp()
                def drift(record):
                    if change in ('duplicate', 'AAAA'):
                        self.fake.records.append({**record, 'id': '3' * 32,
                                                  'type': 'AAAA' if change == 'AAAA' else 'A'})
                    else:
                        record[change] = {'ttl': 120, 'comment': 'operator', 'id': '3' * 32}[change]
                self.fake.after_patch = drift
                with self.assertRaises(d.DNSConflict):
                    self.update()
                with self.assertRaises(d.DNSConflict):
                    self.update()
                self.assertEqual(len(self.writes()), 1)
                self.assertIsNone(self.store.load(d.UPDATE_RECEIPT))

    def test_completed_update_drift_back_is_not_reapplied(self):
        self.update()
        self.fake.records[0]['content'] = IP
        with self.assertRaises(d.DNSConflict):
            self.update()
        self.assertEqual(len(self.writes()), 1)

    def test_saved_operation_cannot_be_reused_for_different_desired_state(self):
        self.update()
        for changed in ({'new_ipv4': '192.0.2.9'}, {'old_ipv4': '192.0.2.99'},
                        {'receipt': {**self.receipt, 'id': '3' * 32}}):
            with self.subTest(changed=changed), self.assertRaises(d.DNSConflict):
                self.update(**changed)
        self.assertEqual(len(self.writes()), 1)

    def test_corrupt_saved_operation_is_rejected_before_network(self):
        self.update()
        complete = copy.deepcopy(self.store.data)
        corruptions = [
            {d.UPDATE_PENDING: complete[d.UPDATE_PENDING]},
            {d.UPDATE_INTENT: complete[d.UPDATE_INTENT], d.UPDATE_RECEIPT: complete[d.UPDATE_RECEIPT]},
            {**complete, d.UPDATE_PENDING: {'version': 1, 'operation_id': 'a' * 32}},
            {**complete, d.UPDATE_INTENT: {**complete[d.UPDATE_INTENT], 'operation_id': 'invalid'}},
            {**complete, d.UPDATE_INTENT: {**complete[d.UPDATE_INTENT], 'version': True}},
            {**complete, d.UPDATE_PENDING: {**complete[d.UPDATE_PENDING], 'version': True}},
            {**complete, d.UPDATE_RECEIPT: {**complete[d.UPDATE_RECEIPT], 'version': True}},
            {**complete, d.UPDATE_RECEIPT: {**complete[d.UPDATE_RECEIPT], 'record': self.receipt}},
        ]
        calls = len(self.fake.calls)
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                self.store.data = corrupt
                with self.assertRaises(d.DNSConflict):
                    self.update()
                self.assertEqual(len(self.fake.calls), calls)

    def test_invalid_initial_receipt_and_input_are_rejected_before_network(self):
        for receipt in (None, {}, {**self.receipt, 'ttl': 120}, {**self.receipt, 'proxied': True},
                        {**self.receipt, 'type': 'AAAA'}, {**self.receipt, 'comment': 'operator'},
                        {**self.receipt, 'zone_id': '../other'}, {**self.receipt, 'id': ''}):
            with self.subTest(receipt=receipt), self.assertRaises(d.DNSConflict):
                self.update(receipt=receipt)
        for changed in ({'old_ipv4': self.NEW_IP}, {'new_ipv4': '2001:db8::1'}, {'old_ipv4': 'bad'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.update(**changed)
        self.assertEqual(self.fake.calls, [])

    def test_original_store_cannot_be_used_as_replacement_store(self):
        self.store = self.original_store
        with self.assertRaises(d.DNSConflict):
            self.update()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.original_store.data, self.original_state)


if __name__ == '__main__':
    unittest.main()
