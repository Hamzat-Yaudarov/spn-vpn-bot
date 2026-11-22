"""Create only owned DNS-only Yandex CNAMEs, never adopt/edit/delete records."""
import importlib.util
from pathlib import Path
import re
import uuid


_spec = importlib.util.spec_from_file_location('yandex_cloudflare_dns',
    Path(__file__).resolve().parent.parent / 'universal-reality/cloudflare_dns.py')
dns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dns)


def hostname(value, *, challenge=False):
    prefix = '_acme-challenge.'
    if challenge and isinstance(value, str) and value.lower().startswith(prefix):
        return prefix + dns.normalize_name(value[len(prefix):])
    return dns.normalize_name(value)


def target(value, kind):
    if kind not in ('cdn', 'certificate'):
        raise dns.DNSConflict('Неизвестное назначение CNAME.')
    name = dns.normalize_name(value)
    suffixes = ('.yccdn.ru', '.cdn.yandex.net') if kind == 'cdn' else ('.cm.yandexcloud.net',)
    if not any(name.endswith(suffix) for suffix in suffixes):
        raise dns.DNSConflict('Нужна точная CNAME-цель, показанная Яндексом, а не IP или имя вашего домена.')
    return name


class Records:
    def __init__(self, client, zone):
        self.client, self.zone = client, zone

    def desired(self, name, content, kind):
        name, content = hostname(name, challenge=kind == 'certificate'), target(content, kind)
        if kind == 'certificate' and not name.startswith('_acme-challenge.'):
            raise dns.DNSConflict('Для сертификата нужна отдельная запись _acme-challenge.CDN_DOMAIN.')
        zone_name = dns.normalize_name(self.zone['name'])
        if not name.endswith('.' + zone_name):
            raise dns.DNSConflict('CNAME находится вне выбранной зоны.')
        return {'zone_id': dns._identifier(self.zone['id']), 'name': name, 'type': 'CNAME',
                'content': content, 'proxied': False, 'ttl': 60}

    def check_available(self, store, name, kind):
        """Reserve nothing: detect conflicting names before installing a VPS."""
        if kind not in ('cdn', 'certificate'):
            raise dns.DNSConflict('Неизвестное назначение CNAME.')
        intent = store.load('dns-' + kind + '.intent.json')
        if intent is not None:
            return self.check(store, name, intent.get('content'), kind)
        rows = self.client._list('/zones/' + self.zone['id'] + '/dns_records', {'name.exact': hostname(name, challenge=True)})
        if rows:
            raise dns.DNSConflict('Будущее CDN-имя или имя проверки сертификата уже занято чужой DNS-записью.')
        return None

    def state(self, store, kind, desired):
        key = 'dns-' + kind
        intent, receipt = store.load(key + '.intent.json'), store.load(key + '.receipt.json')
        if intent is not None and (not isinstance(intent, dict) or intent.get('version') != 1
                or any(intent.get(k) != v for k, v in desired.items())
                or not re.fullmatch(r'SPN universal-yandex:[0-9a-f]{32}', str(intent.get('comment', '')))):
            raise dns.DNSConflict('Сохранённая CNAME-цель отличается; чужие DNS не меняются.')
        if receipt is not None:
            if (not isinstance(receipt, dict) or intent is None or
                    any(receipt.get(k) != v for k, v in intent.items() if k != 'version')):
                raise dns.DNSConflict('Квитанция CNAME не совпала с сохранённым запросом.')
            dns._identifier(receipt.get('id'))
        return key, intent, receipt

    def inspect(self, desired, intent, receipt):
        zone = self.client._request('GET', '/zones/' + desired['zone_id']).get('result')
        if not isinstance(zone, dict) or zone.get('id') != self.zone['id'] or zone.get('status') != 'active' \
                or dns.normalize_name(zone.get('name')) != dns.normalize_name(self.zone['name']):
            raise dns.DNSConflict('Активная DNS-зона изменилась.')
        rows = self.client._list('/zones/' + desired['zone_id'] + '/dns_records', {'name.exact': desired['name']})
        for row in rows:
            if hostname(row.get('name'), challenge=True) != desired['name']:
                raise dns.DNSConflict('Cloudflare вернул другую DNS-запись.')
        if not rows:
            if receipt:
                raise dns.DNSConflict('Собственная CNAME исчезла; автоматически не восстанавливается.')
            if intent:
                raise dns.DNSOutcomeUnknown('Исход создания CNAME неизвестен; повторного заказа DNS не будет.')
            return None
        if len(rows) != 1 or rows[0].get('type') != 'CNAME' or not intent:
            raise dns.DNSConflict('Это имя уже занято чужой DNS-записью; она не перезаписывается.')
        row = rows[0]
        dns._identifier(row.get('id'))
        if (receipt and row['id'] != receipt['id']) or row.get('comment') != intent['comment'] \
                or row.get('proxied') is not False or type(row.get('ttl')) is not int or row['ttl'] != 60 \
                or dns.normalize_name(row.get('content')) != desired['content']:
            raise dns.DNSConflict('CNAME изменена или проксируется Cloudflare; автоматические правки запрещены.')
        return row

    def check(self, store, name, content, kind):
        desired = self.desired(name, content, kind)
        _, intent, receipt = self.state(store, kind, desired)
        return self.inspect(desired, intent, receipt)

    def ensure(self, store, name, content, kind):
        desired = self.desired(name, content, kind)
        key, intent, receipt = self.state(store, kind, desired)
        row = self.inspect(desired, intent, receipt)
        if row is None:
            intent = {'version': 1, **desired, 'comment': 'SPN universal-yandex:' + uuid.uuid4().hex}
            store.save(key + '.intent.json', intent)
            body = {k: v for k, v in intent.items() if k not in ('version', 'zone_id')}
            posted = self.client._request('POST', '/zones/' + desired['zone_id'] + '/dns_records', body=body).get('result')
            row = self.inspect(desired, intent, None)
            if not isinstance(posted, dict) or row is None or posted.get('id') != row['id']:
                raise dns.DNSOutcomeUnknown('Создание CNAME не подтверждено; повторный POST запрещён.')
        if receipt is None:
            store.save(key + '.receipt.json', {**{k: v for k, v in intent.items() if k != 'version'}, 'id': row['id']})
        return row
