"""Owned Yandex CNAME provisioning: private disk state, fake Cloudflare only."""
import copy
import importlib.util
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


d = load('universal_yandex_dns_records_tests', HERE / 'dns_records.py')
u = load('universal_yandex_dns_private_store_tests', HERE.parent / 'universal-direct/deploy.py')
ZONE = {'id': '1' * 32, 'name': 'wayspn.com', 'status': 'active'}
CDN = 'cdn-a1b2c3.wayspn.com'
CDN_TARGET = 'e1a2b3c4d5.topology.gslb.yccdn.ru'
CERTIFICATE = '_acme-challenge.' + CDN
CERTIFICATE_TARGET = 'fpqabc123.cm.yandexcloud.net'


class FakeClient:
    """The two private client methods Records uses; never a network opener."""
    def __init__(self, store):
        self.store = store
        self.zone = copy.deepcopy(ZONE)
        self.records, self.calls = [], []
        self.post_mode = 'ok'
        self.list_override = None

    def _request(self, method, path, *, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == 'GET':
            assert path == '/zones/' + ZONE['id']
            return {'result': copy.deepcopy(self.zone)}
        assert method == 'POST', 'No PATCH, DELETE, PUT or arbitrary mutation is permitted'
        assert path == '/zones/' + ZONE['id'] + '/dns_records'
        kind = 'certificate' if body['name'].startswith('_acme-challenge.') else 'cdn'
        intent = self.store.load('dns-' + kind + '.intent.json')
        assert intent is not None, 'Private intent must exist before the sole POST attempt'
        assert all(intent[key] == value for key, value in body.items())
        record = {'id': format(len(self.posts()) + 1, '032x'), **copy.deepcopy(body)}
        if self.post_mode != 'lost-before':
            self.records.append(record)
        if self.post_mode.startswith('lost-'):
            raise d.dns.DNSOutcomeUnknown('Injected unknown POST outcome; no automatic retry')
        if self.post_mode == 'missing-result':
            return {}
        if self.post_mode == 'wrong-id':
            return {'result': {**record, 'id': 'f' * 32}}
        return {'result': copy.deepcopy(record)}

    def _list(self, path, query):
        self.calls.append(('LIST', path, copy.deepcopy(query)))
        assert path == '/zones/' + ZONE['id'] + '/dns_records'
        assert set(query) == {'name.exact'}
        if self.list_override is not None:
            return copy.deepcopy(self.list_override)
        return [copy.deepcopy(row) for row in self.records
                if d.hostname(row['name'], challenge=True) == query['name.exact']]

    def posts(self):
        return [call for call in self.calls if call[0] == 'POST']


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = u.Store(self.root / 'state')
        self.client = FakeClient(self.store)
        self.records = d.Records(self.client, copy.deepcopy(ZONE))

    def ensure(self, *, name=CDN, content=CDN_TARGET, kind='cdn'):
        return self.records.ensure(self.store, name, content, kind)

    def check(self, *, name=CDN, content=CDN_TARGET, kind='cdn'):
        return self.records.check(self.store, name, content, kind)

    def foreign(self, **changes):
        return {'id': 'd' * 32, 'name': CDN, 'type': 'CNAME', 'content': CDN_TARGET,
                'proxied': False, 'ttl': 60, 'comment': 'not-owned', **changes}

    def test_read_only_check_then_owned_creation_and_repeats_issue_one_post(self):
        self.assertIsNone(self.check())
        self.assertEqual(list(self.store.path.iterdir()), [])
        first = self.ensure()
        self.assertEqual(first['content'], CDN_TARGET)
        self.assertEqual(first['type'], 'CNAME')
        self.assertEqual(first['ttl'], 60)
        self.assertIs(first['proxied'], False)
        self.assertRegex(first['comment'], r'^SPN universal-yandex:[0-9a-f]{32}$')
        self.assertEqual(self.ensure(), first)
        self.assertEqual(self.check(), first)
        self.assertEqual(len(self.client.posts()), 1)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o700)
        for name in ('dns-cdn.intent.json', 'dns-cdn.receipt.json'):
            path = self.store.path / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(self.store.load('dns-cdn.receipt.json')['id'], first['id'])

    def test_certificate_underscore_name_is_normalized_and_retained_for_renewal(self):
        first = self.ensure(name=CERTIFICATE.upper() + '.', content=CERTIFICATE_TARGET.upper() + '.',
                            kind='certificate')
        self.assertEqual(first['name'], CERTIFICATE)
        self.assertEqual(first['content'], CERTIFICATE_TARGET)
        receipt = self.store.load('dns-certificate.receipt.json')
        self.assertEqual(receipt['content'], CERTIFICATE_TARGET)
        self.assertEqual(self.check(name=CERTIFICATE, content=CERTIFICATE_TARGET, kind='certificate'), first)
        self.assertEqual(self.ensure(name=CERTIFICATE, content=CERTIFICATE_TARGET, kind='certificate'), first)
        self.assertEqual(len(self.client.posts()), 1)
        self.assertTrue((self.store.path / 'dns-certificate.intent.json').is_file())
        self.assertFalse(any(method == 'DELETE' for method, *_ in self.client.calls))

    def test_certificate_and_delivery_records_have_separate_durable_states(self):
        certificate = self.ensure(name=CERTIFICATE, content=CERTIFICATE_TARGET, kind='certificate')
        cdn = self.ensure()
        self.assertNotEqual(certificate['id'], cdn['id'])
        self.assertNotEqual(certificate['name'], cdn['name'])
        self.assertEqual(self.store.load('dns-certificate.receipt.json')['id'], certificate['id'])
        self.assertEqual(self.store.load('dns-cdn.receipt.json')['id'], cdn['id'])
        self.assertEqual(self.ensure(), cdn)
        self.assertEqual(self.ensure(name=CERTIFICATE, content=CERTIFICATE_TARGET, kind='certificate'), certificate)
        self.assertEqual(len(self.client.posts()), 2)

    def test_lost_post_response_recovers_exact_owned_record_by_reads_only(self):
        self.client.post_mode = 'lost-after'
        with self.assertRaises(d.dns.DNSOutcomeUnknown):
            self.ensure()
        self.assertIsNotNone(self.store.load('dns-cdn.intent.json'))
        self.assertIsNone(self.store.load('dns-cdn.receipt.json'))
        self.client.post_mode = 'ok'
        recovered = self.ensure()
        self.assertEqual(recovered['id'], self.client.records[0]['id'])
        self.assertEqual(len(self.client.posts()), 1)

    def test_unknown_post_without_record_is_never_automatically_repeated(self):
        self.client.post_mode = 'lost-before'
        for _ in range(3):
            with self.assertRaises(d.dns.DNSOutcomeUnknown):
                self.ensure()
        with self.assertRaises(d.dns.DNSOutcomeUnknown):
            self.check()
        self.assertEqual(len(self.client.posts()), 1)
        self.assertIsNone(self.store.load('dns-cdn.receipt.json'))

    def test_unconfirmed_post_result_is_not_retried_but_get_can_reconcile(self):
        for mode in ('missing-result', 'wrong-id'):
            with self.subTest(mode=mode):
                store = u.Store(self.root / mode)
                fake = FakeClient(store); fake.post_mode = mode
                records = d.Records(fake, copy.deepcopy(ZONE))
                with self.assertRaises(d.dns.DNSOutcomeUnknown):
                    records.ensure(store, CDN, CDN_TARGET, 'cdn')
                self.assertIsNone(store.load('dns-cdn.receipt.json'))
                records.ensure(store, CDN, CDN_TARGET, 'cdn')
                self.assertEqual(len(fake.posts()), 1)

    def test_unowned_existing_records_are_not_adopted_even_if_values_match(self):
        for update in ({}, {'type': 'A', 'content': '8.8.8.8'},
                       {'type': 'AAAA', 'content': '2001:4860:4860::8888'},
                       {'type': 'TXT', 'content': 'existing text'}):
            with self.subTest(update=update):
                original = self.foreign(**update)
                self.client.records = [copy.deepcopy(original)]
                with self.assertRaises(d.dns.DNSConflict):
                    self.ensure()
                self.assertEqual(self.client.records, [original])
                self.assertEqual(self.client.posts(), [])
                self.assertEqual(list(self.store.path.iterdir()), [])

    def test_owned_proxy_ttl_comment_id_content_and_type_drift_are_rejected(self):
        self.ensure()
        original = copy.deepcopy(self.client.records[0])
        for change in ({'proxied': True}, {'ttl': 120}, {'ttl': True}, {'comment': 'operator changed'},
                       {'id': 'e' * 32}, {'content': 'another.topology.gslb.yccdn.ru'},
                       {'type': 'TXT', 'content': 'manual text'}):
            with self.subTest(change=change):
                self.client.records = [{**original, **change}]
                before = copy.deepcopy(self.client.records)
                with self.assertRaises(d.dns.DNSConflict):
                    self.ensure()
                self.assertEqual(self.client.records, before)
                self.assertEqual(len(self.client.posts()), 1)

    def test_changed_requested_target_fails_before_cloudflare_calls(self):
        self.ensure()
        count = len(self.client.calls)
        with self.assertRaises(d.dns.DNSConflict):
            self.ensure(content='new.topology.gslb.yccdn.ru')
        self.assertEqual(len(self.client.calls), count)
        self.assertEqual(len(self.client.posts()), 1)

    def test_deleted_owned_record_is_not_recreated(self):
        self.ensure()
        self.client.records.clear()
        with self.assertRaises(d.dns.DNSConflict):
            self.ensure()
        self.assertEqual(len(self.client.posts()), 1)

    def test_pending_intent_does_not_adopt_foreign_matching_record(self):
        self.client.post_mode = 'lost-before'
        with self.assertRaises(d.dns.DNSOutcomeUnknown):
            self.ensure()
        self.client.records = [self.foreign()]
        with self.assertRaises(d.dns.DNSConflict):
            self.ensure()
        self.assertEqual(len(self.client.posts()), 1)
        self.assertIsNone(self.store.load('dns-cdn.receipt.json'))

    def test_duplicate_and_mixed_record_sets_are_rejected(self):
        self.ensure()
        original = copy.deepcopy(self.client.records[0])
        for other in ({**original, 'id': 'c' * 32},
                      self.foreign(type='TXT', content='conflicting challenge'),
                      self.foreign(type='A', content='8.8.4.4')):
            with self.subTest(other=other['type']):
                self.client.records = [original, other]
                with self.assertRaises(d.dns.DNSConflict):
                    self.ensure()
                self.assertEqual(len(self.client.posts()), 1)

    def test_certificate_cname_conflicts_with_txt_at_same_challenge_name(self):
        self.client.records = [self.foreign(name=CERTIFICATE, type='TXT', content='old-acme-challenge')]
        with self.assertRaises(d.dns.DNSConflict):
            self.ensure(name=CERTIFICATE, content=CERTIFICATE_TARGET, kind='certificate')
        self.assertEqual(self.client.posts(), [])
        self.assertIsNone(self.store.load('dns-certificate.intent.json'))

    def test_wrong_yandex_target_or_name_outside_zone_never_posts(self):
        for content, kind in (('8.8.8.8', 'cdn'), ('wayspn.com', 'cdn'),
                              ('evil.example.org', 'cdn'), ('valid.yccdn.ru.evil.example', 'cdn'),
                              (CERTIFICATE_TARGET, 'cdn'), (CDN_TARGET, 'certificate'),
                              ('x.cm.yandexcloud.net.evil.example', 'certificate')):
            with self.subTest(content=content, kind=kind), self.assertRaises((ValueError, d.dns.DNSConflict)):
                self.ensure(name=CERTIFICATE if kind == 'certificate' else CDN, content=content, kind=kind)
        for name in ('wayspn.com', 'cdn.other.example', 'cdnwayspn.com'):
            with self.subTest(name=name), self.assertRaises(d.dns.DNSConflict):
                self.ensure(name=name)
        self.assertEqual(self.client.posts(), [])
        self.assertEqual(list(self.store.path.iterdir()), [])

    def test_exact_name_listing_and_active_zone_identity_are_enforced(self):
        self.client.list_override = [self.foreign(name='other.wayspn.com')]
        with self.assertRaises(d.dns.DNSConflict):
            self.ensure()
        self.client.list_override = None
        for change in ({'id': 'e' * 32}, {'status': 'pending'}, {'name': 'other.example'}):
            with self.subTest(change=change):
                self.client.zone = {**ZONE, **change}
                with self.assertRaises(d.dns.DNSConflict):
                    self.ensure()
        self.assertEqual(self.client.posts(), [])


if __name__ == '__main__':
    unittest.main()
