"""One additive TEKST TCP pilot and explicitly approved direct-host repairs.

Only infrastructure endpoints are used. No subscriber, balance or global
template writes. Private before/intent files are outside the repository.
"""
import argparse
import base64
import copy
import importlib.util
import json
from pathlib import Path
import secrets

SPEC = importlib.util.spec_from_file_location('direct_reference', Path(__file__).resolve().parents[1] / 'direct-fi/manage.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
m.STATE = Path('/private/tmp/wayspn-tcp-tekst-20261106')
IP = '64.188.80.39'
PROFILE = 'fa9617b8-8025-4e1c-a28e-c51f639c57fc'
NODE = 'dae63e17-3258-488d-ba4c-62858fe81df4'
GRPC = 'ac8404f0-0cf6-4709-9fad-be2ceb40dddd'
TAG = 'ordinary-tcp-reality-tekst'
PORT = 2443


def rows(kind):
    value = m.api('GET', '/' + kind)
    return value['configProfiles'] if kind == 'config-profiles' else value['internalSquads'] if kind == 'internal-squads' else value


def one(items, uid):
    return next(row for row in items if row['uuid'] == uid)


def ids(items):
    return [row['uuid'] if isinstance(row, dict) else row for row in items]


def target():
    p = one(rows('config-profiles'), PROFILE)
    n = one(rows('nodes'), NODE)
    m.require(n['address'] == IP and n['configProfile']['activeConfigProfileUuid'] == PROFILE,
              'Target node association changed')
    others = [r['uuid'] for r in rows('nodes') if r['configProfile']['activeConfigProfileUuid'] == PROFILE and r['uuid'] != NODE]
    m.require(not others, 'Profile is shared with another node; stop')
    return p, n


def prepare():
    if (m.STATE / 'plan.json').exists():
        print('Existing private pilot plan preserved')
        return
    p, n = target()
    h = one(rows('hosts'), GRPC)
    m.require(h['address'] == IP and h['port'] == 2083 and not h['isDisabled'], 'Source host changed')
    m.require(not any(i['tag'] == TAG or i['port'] == PORT for i in p['config']['inbounds']), 'Pilot already exists')
    squads = rows('internal-squads')
    source = h['inbound']['configProfileInboundUuid']
    audience = [s['uuid'] for s in squads if source in ids(s['inbounds'])]
    m.require(audience, 'No source audience')
    config = copy.deepcopy(p['config'])
    grpc = next(i for i in config['inbounds'] if i['tag'] == 'ordinary-grpc-reality-tekst')
    ws = next(i for i in config['inbounds'] if i['tag'] == 'ordinary-ws-tekst')
    m.require(grpc['streamSettings']['grpcSettings']['serviceName'] == 'grpc'
              and ws['streamSettings']['wsSettings']['path'] == '/direct/ws/', 'Source paths changed')
    # The existing certificate covers this SNI; localhost removes broken public
    # DNS from REALITY's server-side handshake without changing key or SNI.
    m.require(grpc['streamSettings']['realitySettings']['serverNames'] == ['www.tekst.site'], 'Unexpected source SNI')
    grpc['streamSettings']['realitySettings']['target'] = '127.0.0.1:443'
    key = m.X25519PrivateKey.generate()
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    private = encode(key.private_bytes(m.serialization.Encoding.Raw, m.serialization.PrivateFormat.Raw, m.serialization.NoEncryption()))
    inbound = {
        'tag': TAG, 'listen': '0.0.0.0', 'port': PORT, 'protocol': 'vless',
        'settings': {'clients': [], 'decryption': 'none'},
        'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
        'streamSettings': {'network': 'raw', 'security': 'reality', 'realitySettings': {
            'show': False, 'target': '127.0.0.1:443', 'xver': 0,
            'serverNames': ['www.tekst.site'], 'privateKey': private,
            'shortIds': [secrets.token_hex(8)],
        }},
    }
    config['inbounds'].append(inbound)
    m.save('profile.before.json', p)
    m.save('node.before.json', n)
    m.save('grpc.before.json', h)
    m.save('squads.before.json', [s for s in squads if s['uuid'] in audience])
    m.save('candidate.json', config)
    m.save('plan.json', {'address': IP, 'profile': PROFILE, 'node': NODE, 'port': PORT,
                         'tag': TAG, 'audience': audience, 'visibility': h['internalSquads']})
    print('Private rollback and candidate saved. Old inbounds retained; only gRPC target changes to localhost.')


def apply_profile():
    p, n = target()
    before = m.load('profile.before.json')
    candidate = m.load('candidate.json')
    if p['config'] != candidate:
        m.require(p['config'] == before['config'], 'Concurrent profile edit; refusing overwrite')
        m.save('profile.patch.intent.json', {'uuid': PROFILE, 'config': candidate})
        m.api('PATCH', '/config-profiles', {'uuid': PROFILE, 'config': candidate})
    p, n = target()
    m.require(p['config'] == candidate, 'Candidate was not applied')
    new = next(i['uuid'] for i in p['inbounds'] if i['tag'] == TAG)
    current = ids(n['configProfile']['activeInbounds'])
    m.require(set(ids(m.load('node.before.json')['configProfile']['activeInbounds'])).issubset(current), 'Existing node inbounds removed')
    if new not in current:
        body = {'uuid': NODE, 'configProfile': {'activeConfigProfileUuid': PROFILE, 'activeInbounds': current + [new]}}
        m.save('node.patch.intent.json', body)
        m.api('PATCH', '/nodes', body)
    m.save('inbound-id.json', {'uuid': new})
    print('TCP inbound added to existing profile and node; all old inbounds retained.')


def ensure_host(name, body):
    m.save(name + '.create.intent.json', body)
    hosts = rows('hosts')
    matching = [h for h in hosts if h.get('inbound', {}).get('configProfileInboundUuid') == body['inbound']['configProfileInboundUuid']]
    if not matching:
        m.api('POST', '/hosts', body)
        matching = [h for h in rows('hosts') if h.get('inbound', {}).get('configProfileInboundUuid') == body['inbound']['configProfileInboundUuid']]
    m.require(len(matching) == 1, 'Ambiguous host')
    h = matching[0]
    m.require(all(h.get(k) == v for k, v in body.items() if k != 'isDisabled'), 'Existing host differs from intent')
    m.save(name + '.id.json', {'uuid': h['uuid']})
    return h


def publish_disabled():
    p, n = target()
    m.require(p['config'] == m.load('candidate.json'), 'Profile changed')
    plan = m.load('plan.json')
    tags = {i['tag']: i['uuid'] for i in p['inbounds']}
    grpc, ws, tcp = [tags[k] for k in ('ordinary-grpc-reality-tekst', 'ordinary-ws-tekst', TAG)]
    for squad in rows('internal-squads'):
        if squad['uuid'] not in plan['audience']:
            continue
        current = ids(squad['inbounds'])
        extra = [uid for uid in (grpc, ws, tcp) if uid not in current]
        if extra:
            body = {'uuid': squad['uuid'], 'inbounds': current + extra}
            m.save('squad-' + squad['uuid'] + '.patch.intent.json', body)
            m.api('PATCH', '/internal-squads', body)
    current = one(rows('hosts'), GRPC)
    before = m.load('grpc.before.json')
    m.require(current['address'] == IP and current['port'] == 2083, 'Source endpoint changed')
    repair = {'uuid': GRPC, 'inbound': {'configProfileUuid': PROFILE, 'configProfileInboundUuid': grpc},
              'nodes': [NODE], 'sni': 'www.tekst.site', 'alpn': 'h2', 'path': 'grpc',
              'securityLayer': 'DEFAULT', 'overrideSniFromAddress': False, 'keepSniBlank': False}
    if not all(current.get(k) == v for k, v in repair.items()):
        m.require(all(current.get(k) == before.get(k) for k in repair), 'Concurrent gRPC host edit')
        m.save('grpc.patch.intent.json', repair)
        m.api('PATCH', '/hosts', repair)
    common = {'address': IP, 'nodes': [NODE], 'isDisabled': True, 'isHidden': False,
              'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
              'internalSquads': plan['visibility'], 'tags': ['TEKST'], 'fingerprint': 'chrome'}
    ensure_host('tcp', dict(common, remark='🇫🇮 Финляндия 5 • TCP', port=PORT,
               inbound={'configProfileUuid': PROFILE, 'configProfileInboundUuid': tcp},
               sni='www.tekst.site', alpn='h2', securityLayer='DEFAULT'))
    ensure_host('ws', dict(common, remark='🇫🇮 Финляндия 5 • WS', port=443,
               inbound={'configProfileUuid': PROFILE, 'configProfileInboundUuid': ws},
               sni='tekst.site', host='tekst.site', path='/direct/ws/', alpn='http/1.1', securityLayer='TLS'))
    print('gRPC repaired; separate TCP and WS hosts created disabled for verification.')


def enable():
    p, n = target()
    m.require(p['config'] == m.load('candidate.json') and n['isConnected'] and not n['isDisabled']
              and not n.get('lastStatusMessage'), 'Node/profile not ready')
    m.require(m.load('inbound-id.json')['uuid'] in ids(n['configProfile']['activeInbounds']), 'TCP not active on node')
    publish_disabled()
    for name in ('tcp', 'ws'):
        uid = m.load(name + '.id.json')['uuid']
        h = one(rows('hosts'), uid)
        if h['isDisabled']:
            m.save(name + '.enable.intent.json', {'uuid': uid, 'isDisabled': False})
            m.api('PATCH', '/hosts', {'uuid': uid, 'isDisabled': False})
        m.require(not one(rows('hosts'), uid)['isDisabled'], 'Host did not enable')
        print('Enabled:', h['remark'], uid)


def status():
    p, n = target()
    print('Node:', n['name'], n['address'], 'connected:', n['isConnected'], 'error:', n.get('lastStatusMessage'))
    for h in rows('hosts'):
        if h.get('inbound', {}).get('configProfileUuid') == PROFILE:
            print(h['uuid'], h['remark'], h['address'], h['port'], 'disabled:', h['isDisabled'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'apply_profile', 'publish_disabled', 'enable', 'status'])
    args = parser.parse_args()
    globals()[args.command]()
