"""Provision one owned, DNS-only Cloudflare A record without overwriting DNS.

The caller supplies an API token (normally read from the environment), and a
locked durable Store exposing load(name) and save(name, value). A saved pending
intent is never posted again: recovery only reads Cloudflare and requires the
unique record with the intent's operation marker. Creation never updates or
deletes records. Explicit IP replacement uses a separate operation Store and
the original immutable ownership receipt; it can PATCH only the record content.
"""
from __future__ import annotations

from http.client import HTTPException
from ipaddress import IPv4Address, ip_address
import json
import re
import ssl
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
import uuid


INTENT = 'dns.intent.json'
RECEIPT = 'dns.receipt.json'
UPDATE_INTENT = 'dns-update.intent.json'
UPDATE_PENDING = 'dns-update.pending.json'
UPDATE_RECEIPT = 'dns-update.receipt.json'
BASE = 'https://api.cloudflare.com/client/v4'


class DNSProvisionError(RuntimeError):
    """A safe DNS operation could not be verified."""


class DNSConflict(DNSProvisionError):
    """DNS or saved state differs from the explicitly requested identity."""


class DNSOutcomeUnknown(DNSProvisionError):
    """A pending write must be reconciled by reads, never automatically repeated."""


def normalize_name(value):
    """Canonical, non-wildcard FQDN; never accept an IP literal as a domain."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('A DNS hostname is required')
    try:
        name = value.rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise ValueError('Invalid DNS hostname') from None
    labels = name.split('.')
    if (len(labels) < 2 or len(name) > 253
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
        raise ValueError('Invalid DNS hostname')
    try:
        ip_address(name)
    except ValueError:
        return name
    raise ValueError('A domain name, not an IP literal, is required')


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', value):
        raise ValueError('Invalid Cloudflare identifier')
    return value


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def tls_context():
    """Verified TLS, using certifi's CA bundle when available on local Python."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class CloudflareDNS:
    def __init__(self, token, *, timeout=10, opener=None):
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise ValueError('A Cloudflare API token is required')
        self._token = token
        self._timeout = timeout
        self._open = opener if opener is not None else build_opener(
            _RejectRedirects(), HTTPSHandler(context=tls_context())).open

    def _request(self, method, path, *, query=None, body=None):
        url = BASE + path + (('?' + urlencode(query)) if query else '')
        request = Request(url, method=method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer ' + self._token,
                                   'Accept': 'application/json', 'Content-Type': 'application/json'})
        error_type = DNSOutcomeUnknown if method in ('POST', 'PATCH') else DNSProvisionError
        try:
            with self._open(request, timeout=self._timeout) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ValueError('Response too large')
                payload = json.loads(raw)
        except HTTPError as error:
            code = error.code
            error.close()
            raise error_type(f'Cloudflare DNS {method} failed (HTTP {code}); no automatic retry') from None
        except (OSError, ValueError, HTTPException):
            raise error_type(f'Cloudflare DNS {method} response is unavailable or invalid; no automatic retry') from None
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise error_type(f'Cloudflare DNS {method} was not confirmed; no automatic retry')
        return payload

    def _list(self, path, query):
        rows = []
        page = 1
        while True:
            payload = self._request('GET', path, query={**query, 'page': page, 'per_page': 50})
            result, info = payload.get('result'), payload.get('result_info')
            if (not isinstance(result, list) or any(not isinstance(row, dict) for row in result)
                    or not isinstance(info, dict) or info.get('page') != page
                    or type(info.get('total_pages')) is not int
                    or not 0 <= info['total_pages'] <= 100
                    or info['total_pages'] < page and not (page == 1 and info['total_pages'] == 0 and not result)):
                raise DNSProvisionError('Cloudflare DNS listing is incomplete or invalid')
            rows.extend(result)
            if page >= info['total_pages']:
                return rows
            page += 1

    def find_zone(self, name):
        """Read-only lookup: require exactly one active zone with this name."""
        name = normalize_name(name)
        zones = self._list('/zones', {'name': name, 'status': 'active', 'match': 'all'})
        exact = [zone for zone in zones if isinstance(zone.get('name'), str)
                 and normalize_name(zone['name']) == name and zone.get('status') == 'active']
        if len(exact) != 1:
            raise DNSConflict('Cloudflare zone name is missing or ambiguous')
        try:
            _identifier(exact[0].get('id'))
        except ValueError:
            raise DNSProvisionError('Cloudflare returned an invalid zone identity') from None
        return exact[0]

    def _desired(self, zone_id, name, ipv4, ttl):
        if type(ttl) is not int or not 60 <= ttl <= 86400:
            raise ValueError('DNS-only TTL must be between 60 and 86400 seconds')
        return {'zone_id': _identifier(zone_id), 'name': normalize_name(name), 'type': 'A',
                'content': str(IPv4Address(ipv4)), 'proxied': False, 'ttl': ttl}

    def _state(self, store, desired):
        intent, receipt = store.load(INTENT), store.load(RECEIPT)
        if intent is not None:
            if (not isinstance(intent, dict) or intent.get('version') != 1
                    or any(intent.get(key) != value for key, value in desired.items())
                    or not isinstance(intent.get('comment'), str)
                    or not re.fullmatch(r'SPN universal-reality:[0-9a-f]{32}', intent['comment'])):
                raise DNSConflict('Saved DNS intent differs from this request; no DNS changes made')
        if receipt is not None:
            if (intent is None or not isinstance(receipt, dict)
                    or any(receipt.get(key) != value for key, value in intent.items() if key != 'version')):
                raise DNSConflict('Saved DNS receipt differs from its intent')
            try:
                _identifier(receipt.get('id'))
            except ValueError:
                raise DNSConflict('Saved DNS receipt has an invalid record ID') from None
        return intent, receipt

    def _zone(self, desired):
        zone = self._request('GET', '/zones/' + desired['zone_id']).get('result')
        if not isinstance(zone, dict) or zone.get('id') != desired['zone_id'] or zone.get('status') != 'active':
            raise DNSConflict('Cloudflare zone is not the requested active zone')
        try:
            zone_name = normalize_name(zone.get('name'))
        except ValueError:
            raise DNSProvisionError('Cloudflare returned an invalid zone name') from None
        if desired['name'] != zone_name and not desired['name'].endswith('.' + zone_name):
            raise DNSConflict('Requested DNS name is outside the selected Cloudflare zone')

    def _matches(self, record, desired, *, comment=None):
        try:
            _identifier(record.get('id'))
            matches = (normalize_name(record.get('name')) == desired['name']
                       and record.get('type') == 'A'
                       and str(IPv4Address(record.get('content'))) == desired['content']
                       and record.get('proxied') is False
                       and type(record.get('ttl')) is int and record['ttl'] == desired['ttl'])
        except (ValueError, TypeError):
            return False
        return matches and (comment is None or record.get('comment') == comment)

    def _inspect(self, desired, intent, receipt):
        self._zone(desired)
        rows = self._list('/zones/' + desired['zone_id'] + '/dns_records', {'name.exact': desired['name']})
        exact = []
        for row in rows:
            try:
                if normalize_name(row.get('name')) != desired['name']:
                    raise ValueError('Wrong name')
            except ValueError:
                raise DNSProvisionError('Cloudflare exact-name listing returned an invalid record') from None
            exact.append(row)
        if any(row.get('type') in ('AAAA', 'CNAME') for row in exact):
            raise DNSConflict('An AAAA or CNAME record already exists at the requested DNS name')
        records = [row for row in exact if row.get('type') == 'A']
        if len(records) > 1:
            raise DNSConflict('Multiple A records exist at the requested DNS name')
        if not records:
            if receipt is not None:
                raise DNSConflict('The owned DNS record is missing; it will not be recreated automatically')
            if intent is not None:
                raise DNSOutcomeUnknown('Pending DNS POST has no exact record yet; inspect Cloudflare before any manual retry')
            return None
        record = records[0]
        if receipt is None and intent is None:
            raise DNSConflict('An unowned A record already exists; it will not be adopted or overwritten')
        if receipt is not None and record.get('id') != receipt['id']:
            raise DNSConflict('DNS record ID changed; it will not be adopted or overwritten')
        if not self._matches(record, desired, comment=intent['comment']):
            raise DNSConflict('DNS record changed or is proxied; no IP, proxy or TTL changes will be made')
        return record

    def check_record(self, store, *, zone_id, name, ipv4, ttl=60):
        """Read-only preflight. None means an absent, not-yet-attempted record."""
        desired = self._desired(zone_id, name, ipv4, ttl)
        intent, receipt = self._state(store, desired)
        return self._inspect(desired, intent, receipt)

    def ensure_record(self, store, *, zone_id, name, ipv4, ttl=60):
        """Create once, read back, and durably record ownership; never PATCH."""
        desired = self._desired(zone_id, name, ipv4, ttl)
        intent, receipt = self._state(store, desired)
        record = self._inspect(desired, intent, receipt)
        if record is None:
            intent = {'version': 1, **desired, 'comment': 'SPN universal-reality:' + uuid.uuid4().hex}
            # Must be fsynced by Store before issuing the only POST attempt.
            store.save(INTENT, intent)
            body = {key: value for key, value in intent.items() if key not in ('version', 'zone_id')}
            created = self._request('POST', '/zones/' + desired['zone_id'] + '/dns_records', body=body).get('result')
            if not isinstance(created, dict) or not self._matches(created, desired, comment=intent['comment']):
                raise DNSOutcomeUnknown('DNS POST returned no exact record; reconcile by read-only rerun')
            # List again: POST success alone cannot exclude a concurrent record.
            record = self._inspect(desired, intent, None)
            if record.get('id') != created['id']:
                raise DNSConflict('DNS record changed between creation and read-back')
        if receipt is None:
            store.save(RECEIPT, {**{key: value for key, value in intent.items() if key != 'version'}, 'id': record['id']})
        return record

    def _update_snapshot(self, record, zone_id):
        """Keep only the identity and protected fields, never arbitrary API data."""
        try:
            snapshot = {'zone_id': _identifier(zone_id), 'id': _identifier(record.get('id')),
                        'name': normalize_name(record.get('name')), 'type': record.get('type'),
                        'content': str(IPv4Address(record.get('content'))),
                        'proxied': record.get('proxied'), 'ttl': record.get('ttl'),
                        'comment': record.get('comment')}
            if (snapshot['type'] != 'A' or snapshot['proxied'] is not False
                    or type(snapshot['ttl']) is not int or snapshot['ttl'] != 60
                    or not isinstance(snapshot['comment'], str)
                    or not re.fullmatch(r'SPN universal-reality:[0-9a-f]{32}', snapshot['comment'])):
                raise ValueError('Invalid ownership metadata')
        except (AttributeError, ValueError, TypeError):
            raise DNSConflict('DNS replacement requires an owned DNS-only A record with TTL 60') from None
        return snapshot

    def _update_desired(self, receipt, old_ipv4, new_ipv4):
        if not isinstance(receipt, dict):
            raise DNSConflict('The original DNS ownership receipt is required for replacement')
        original = self._update_snapshot(receipt, receipt.get('zone_id'))
        before = {**original, 'content': str(IPv4Address(old_ipv4))}
        after = {**original, 'content': str(IPv4Address(new_ipv4))}
        if before == after:
            raise ValueError('DNS replacement requires different old and new IPv4 addresses')
        return before, after

    def _update_state(self, store, before, after):
        # Never share the creation Store: the initial receipt remains immutable,
        # and each explicit replacement gets its own locked durable namespace.
        if store.load(INTENT) is not None or store.load(RECEIPT) is not None:
            raise DNSConflict('DNS replacement requires a separate operation Store')
        intent, pending, receipt = (store.load(name) for name in
                                    (UPDATE_INTENT, UPDATE_PENDING, UPDATE_RECEIPT))
        if intent is None:
            if pending is not None or receipt is not None:
                raise DNSConflict('Saved DNS replacement state has no intent')
            return None, None, None
        if (not isinstance(intent, dict) or type(intent.get('version')) is not int or intent['version'] != 1
                or set(intent) != {'version', 'operation_id', 'before', 'after'}
                or not isinstance(intent.get('operation_id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', intent['operation_id'])
                or intent.get('before') != before or intent.get('after') != after):
            raise DNSConflict('Saved DNS replacement intent differs from this request')
        marker = {'version': 1, 'operation_id': intent['operation_id']}
        if pending is not None and (not isinstance(pending, dict)
                                    or type(pending.get('version')) is not int or pending != marker):
            raise DNSConflict('Saved DNS replacement pending marker differs from its intent')
        if receipt is not None and (not isinstance(receipt, dict)
                                   or type(receipt.get('version')) is not int
                                   or pending is None or receipt != {**marker, 'record': after}):
            raise DNSConflict('Saved DNS replacement receipt differs from its intent')
        return intent, pending, receipt

    def _read_update_record(self, before):
        """Check zone, all same-name addresses, then the exact receipted ID."""
        self._zone(before)
        path = '/zones/' + before['zone_id'] + '/dns_records'
        rows = self._list(path, {'name.exact': before['name']})
        for row in rows:
            try:
                if normalize_name(row.get('name')) != before['name']:
                    raise ValueError('Wrong name')
            except ValueError:
                raise DNSProvisionError('Cloudflare exact-name listing returned an invalid record') from None
        if any(row.get('type') in ('AAAA', 'CNAME') for row in rows):
            raise DNSConflict('An AAAA or CNAME record conflicts with DNS replacement')
        records = [row for row in rows if row.get('type') == 'A']
        if len(records) != 1:
            raise DNSConflict('DNS replacement requires exactly one existing A record; it will not be recreated')
        listed = self._update_snapshot(records[0], before['zone_id'])
        metadata = {key: value for key, value in before.items() if key != 'content'}
        if any(listed.get(key) != value for key, value in metadata.items()):
            raise DNSConflict('DNS record identity or ownership metadata changed; no replacement is allowed')
        record = self._request('GET', path + '/' + before['id']).get('result')
        observed = self._update_snapshot(record, before['zone_id'])
        if observed != listed:
            raise DNSConflict('DNS record changed between exact-name and exact-ID reads')
        return record

    def _inspect_update(self, before, after, intent, pending, receipt):
        record = self._read_update_record(before)
        observed = self._update_snapshot(record, before['zone_id'])
        if observed == after:
            if intent is None or pending is None:
                raise DNSConflict('New DNS IPv4 exists without this operation pending marker; it will not be adopted')
            return record
        if observed != before:
            raise DNSConflict('DNS IPv4 differs from the committed predecessor and this replacement')
        if receipt is not None:
            raise DNSConflict('Completed DNS replacement was changed back; no write will be repeated')
        if pending is not None:
            raise DNSOutcomeUnknown('Pending DNS PATCH still has the old IPv4; inspect Cloudflare before any manual action')
        return record

    def check_update(self, store, *, receipt, old_ipv4, new_ipv4):
        """Read-only replacement preflight or recovery, using the original receipt.

        ``store`` must be a separate, locked, durable Store for this one move.
        ``old_ipv4`` is the committed predecessor, not necessarily the initial
        receipt's IP. A pending attempt can be recovered only at its exact new
        IP with the same ID and metadata; an unchanged old IP is ambiguous.
        """
        before, after = self._update_desired(receipt, old_ipv4, new_ipv4)
        state = self._update_state(store, before, after)
        return self._inspect_update(before, after, *state)

    def update_record(self, store, *, receipt, old_ipv4, new_ipv4):
        """PATCH only content once; ambiguous attempts are reconciled by reads.

        Store.save must fsync before returning. Never remove a pending marker
        or use a fresh operation Store to retry an ambiguous write automatically.
        The initial ownership receipt and every other DNS field stay unchanged.
        """
        before, after = self._update_desired(receipt, old_ipv4, new_ipv4)
        intent, pending, committed = self._update_state(store, before, after)
        record = self._inspect_update(before, after, intent, pending, committed)
        if self._update_snapshot(record, before['zone_id']) == before:
            if intent is None:
                intent = {'version': 1, 'operation_id': uuid.uuid4().hex,
                          'before': before, 'after': after}
                store.save(UPDATE_INTENT, intent)
            pending = {'version': 1, 'operation_id': intent['operation_id']}
            store.save(UPDATE_PENDING, pending)
            # Recheck immediately before the sole PATCH, after both durable
            # writes. Any failure now leaves a pending, never-retried operation.
            record = self._read_update_record(before)
            if self._update_snapshot(record, before['zone_id']) != before:
                raise DNSConflict('DNS IPv4 changed before PATCH; no replacement was attempted')
            try:
                self._request('PATCH', '/zones/' + before['zone_id'] + '/dns_records/' + before['id'],
                              body={'content': after['content']})
            except DNSOutcomeUnknown:
                # Even an error or lost response may have committed the write.
                # Reconcile this exact ID; never issue a second PATCH.
                pass
            try:
                record = self._inspect_update(before, after, intent, pending, None)
            except (DNSConflict, DNSOutcomeUnknown):
                raise
            except DNSProvisionError:
                raise DNSOutcomeUnknown('DNS PATCH read-back is unavailable; reconcile by read-only rerun') from None
        if committed is None:
            store.save(UPDATE_RECEIPT, {**pending, 'record': after})
        return record
