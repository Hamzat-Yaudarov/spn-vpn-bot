"""Scoped German backend and owner-supplied VK TCP-front deployment.

Never creates subscribers, leases or CDN resources; preserves existing hosts.
"""
import base64
import copy
import importlib.util
import json
import secrets
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('direct_reference', OPS / 'direct-fi/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.STATE = Path('/private/tmp/wayspn-vk-de-20261106')
IP = '152.234.235.106'
PORT = 2443
SNI = 'login.vk.com'
PROFILE = 'VK_HAPROXY_DE_235_106'
NODE = 'node-vk-de-235.106'
TAG = 'vk-de-235106-reality'
FRONT_IP, FRONT_PORT = '185.86.147.77', 10016
FRONT_REMARK = '🇩🇪 Германия • ОБХОД VK'
SOURCE_BYPASS_HOST = 'c546f062-1c03-45d4-9ee1-865967f37c06'


def config(private_key, short_id):
    return {
        'log': {'loglevel': 'warning', 'access': 'none'},
        'inbounds': [{
            'tag': TAG, 'listen': '0.0.0.0', 'port': PORT, 'protocol': 'vless',
            'settings': {'clients': [], 'decryption': 'none'},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic'], 'routeOnly': True},
            'streamSettings': {
                'network': 'raw', 'security': 'reality',
                'realitySettings': {
                    'show': False, 'target': SNI + ':443', 'xver': 0,
                    'serverNames': [SNI], 'privateKey': private_key,
                    'shortIds': [short_id],
                },
            },
        }],
        'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}, {'tag': 'BLOCK', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [
            {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'BLOCK'},
            {'type': 'field', 'domain': ['geosite:private'], 'outboundTag': 'BLOCK'},
        ]},
    }


def profiles():
    return m.api('GET', '/config-profiles')['configProfiles']


def nodes():
    return m.api('GET', '/nodes')


def check_plan():
    plan = m.load('plan.json')
    m.require(plan == {'address': IP, 'port': PORT, 'sni': SNI, 'profile': PROFILE, 'node': NODE, 'tag': TAG}, 'Unexpected plan')
    profile = m.load('profile.create.json')
    public = m.load('public.json')
    inbound, = profile['config']['inbounds']
    reality = inbound['streamSettings']['realitySettings']
    m.require(profile == {'name': PROFILE, 'config': config(reality['privateKey'], reality['shortIds'][0])}, 'Changed candidate')
    m.require(public['address'] == IP and public['port'] == PORT and public['sni'] == SNI, 'Changed public plan')
    return profile


def prepare():
    if (m.STATE / 'plan.json').exists():
        check_plan()
        print('Existing private plan preserved')
        return
    m.require(not any(n['address'] == IP or n['name'] == NODE for n in nodes()), 'Node already exists; inspect first')
    m.require(not any(p['name'] == PROFILE for p in profiles()), 'Profile already exists; inspect first')
    key = m.X25519PrivateKey.generate()
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    private = encode(key.private_bytes(m.serialization.Encoding.Raw, m.serialization.PrivateFormat.Raw, m.serialization.NoEncryption()))
    public = encode(key.public_key().public_bytes(m.serialization.Encoding.Raw, m.serialization.PublicFormat.Raw))
    short = secrets.token_hex(8)
    candidate = config(private, short)
    secret = m.api('GET', '/keygen')['secretKey']
    m.require(isinstance(secret, str) and len(secret) > 20 and not any(c.isspace() for c in secret), 'Invalid node service key')
    m.save('node.env', 'NODE_PORT=2222\nSECRET_KEY=' + secret + '\n')
    m.save('profile.create.json', {'name': PROFILE, 'config': candidate})
    m.save('validation.json', candidate)
    m.save('public.json', {'address': IP, 'port': PORT, 'sni': SNI, 'publicKey': public, 'shortId': short})
    m.save('plan.json', {'address': IP, 'port': PORT, 'sni': SNI, 'profile': PROFILE, 'node': NODE, 'tag': TAG})
    print('One private REALITY configuration prepared; no panel writes')


def provision():
    candidate = check_plan()
    p = m.obtain('profile', 'profile', lambda row: row['name'] == PROFILE, candidate)
    m.require(len(p['inbounds']) == 1 and p['inbounds'][0]['tag'] == TAG, 'Unexpected target inbounds')
    inbound = p['inbounds'][0]['uuid']
    n = m.obtain('node', 'node', lambda row: row['address'] == IP or row['name'] == NODE, {
        'name': NODE, 'address': IP, 'port': 2222, 'countryCode': 'DE', 'tags': ['VK_PORT_DE'],
        'note': 'Dedicated backend for rented VK TCP front. VPN TCP2443; control2222 panel-only. No public host until front is supplied.',
        'configProfile': {'activeConfigProfileUuid': p['uuid'], 'activeInbounds': [inbound]},
    })
    m.save('ids.json', {'profile': p['uuid'], 'node': n['uuid'], 'inbound': inbound})
    print('Created new profile', p['uuid'], 'and node', n['uuid'], '; no public host or squad changes')


def status():
    ids = m.load('ids.json')
    n = next(row for row in nodes() if row['uuid'] == ids['node'])
    p = next(row for row in profiles() if row['uuid'] == ids['profile'])
    m.require(p['config'] == check_plan()['config'], 'Target profile changed')
    m.require(n['address'] == IP and n['configProfile']['activeConfigProfileUuid'] == ids['profile'], 'Target node changed')
    members = [i['uuid'] if isinstance(i, dict) else i for i in n['configProfile']['activeInbounds']]
    m.require(members == [ids['inbound']], 'Target inbound bindings changed')
    print(json.dumps({k: n.get(k) for k in ('name', 'address', 'isConnected', 'isDisabled', 'lastStatusMessage', 'versions')}, ensure_ascii=False))
    print('Backend:', IP + ':' + str(PORT), 'SNI:', SNI)


def uuids(rows):
    return [row['uuid'] if isinstance(row, dict) else row for row in rows]


def prepare_front():
    if (m.STATE / 'front.plan.json').exists():
        m.require(m.load('front.plan.json')['endpoint'] == [FRONT_IP, FRONT_PORT], 'Changed front endpoint')
        print('Existing front plan preserved')
        return
    status()
    ids = m.load('ids.json')
    hosts = m.api('GET', '/hosts')
    m.require(not any(h.get('address') == FRONT_IP and h.get('port') == FRONT_PORT for h in hosts), 'Front already exists; inspect')
    m.require(not any(h.get('inbound', {}).get('configProfileInboundUuid') == ids['inbound'] for h in hosts), 'Target inbound already has a host')
    source = next(h for h in hosts if h['uuid'] == SOURCE_BYPASS_HOST)
    access = source.get('internalSquads')
    m.require(isinstance(access, dict) and access.get('mode') in {'EXCLUDE', 'INCLUDE'} and isinstance(access.get('squads'), list), 'Unknown source visibility')
    source_inbound = source['inbound']['configProfileInboundUuid']
    squads = m.api('GET', '/internal-squads')['internalSquads']
    chosen = [{'uuid': s['uuid'], 'name': s['name']} for s in squads if source_inbound in uuids(s['inbounds'])]
    m.require(chosen, 'No bypass squads')
    body = {
        'remark': FRONT_REMARK, 'address': FRONT_IP, 'port': FRONT_PORT,
        'inbound': {'configProfileUuid': ids['profile'], 'configProfileInboundUuid': ids['inbound']},
        'nodes': [ids['node']], 'sni': SNI, 'alpn': 'h2', 'fingerprint': 'chrome',
        'securityLayer': 'DEFAULT', 'isDisabled': True, 'isHidden': False,
        'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
        'tags': ['VK_PORT_DE'], 'internalSquads': copy.deepcopy(access),
    }
    m.save('front.plan.json', {'endpoint': [FRONT_IP, FRONT_PORT], 'body': body,
                              'sourceInbound': source_inbound, 'squads': chosen})
    print('Disabled-host plan prepared for', FRONT_IP + ':' + str(FRONT_PORT), 'audience:', [s['name'] for s in chosen])


def publish_front(*, enable=False):
    status()
    ids = m.load('ids.json')
    plan = m.load('front.plan.json')
    m.require(plan['endpoint'] == [FRONT_IP, FRONT_PORT] and plan['body']['nodes'] == [ids['node']]
              and plan['body']['address'] == FRONT_IP and plan['body']['port'] == FRONT_PORT
              and plan['body']['isDisabled'] is True and plan['body']['remark'] == FRONT_REMARK
              and plan['body']['inbound'] == {'configProfileUuid': ids['profile'], 'configProfileInboundUuid': ids['inbound']}
              and plan['body']['sni'] == SNI, 'Unexpected front plan')
    node = next(n for n in nodes() if n['uuid'] == ids['node'])
    m.require(node.get('isConnected') is True and node.get('isDisabled') is False and not node.get('lastStatusMessage'), 'Backend not ready')
    rows = [h for h in m.api('GET', '/hosts') if h.get('inbound', {}).get('configProfileInboundUuid') == ids['inbound']
            or (h.get('address') == FRONT_IP and h.get('port') == FRONT_PORT)]
    if rows:
        m.require(len(rows) == 1 and (m.STATE / 'intent-vk-front.json').exists(), 'Unowned or ambiguous existing host')
        host = rows[0]
        wanted = dict(plan['body'], isDisabled=host['isDisabled'])
        m.check(host, wanted)
        m.require(m.load('intent-vk-front.json') == plan['body'], 'Changed host creation intent')
    else:
        host = m.obtain('grpc', 'vk-front',
                        lambda h: h.get('inbound', {}).get('configProfileInboundUuid') == ids['inbound']
                        or (h.get('address') == FRONT_IP and h.get('port') == FRONT_PORT), plan['body'])
    m.save('front.host-id.json', {'uuid': host['uuid']})
    for selected in plan['squads']:
        current = next(s for s in m.api('GET', '/internal-squads')['internalSquads'] if s['uuid'] == selected['uuid'])
        members = uuids(current['inbounds'])
        m.require(plan['sourceInbound'] in members, 'Source access changed; inspect')
        if ids['inbound'] not in members:
            payload = {'uuid': current['uuid'], 'inbounds': members + [ids['inbound']]}
            m.save('front-squad-' + current['uuid'] + '.intent.json', payload)
            m.api('PATCH', '/internal-squads', payload)
            after = next(s for s in m.api('GET', '/internal-squads')['internalSquads'] if s['uuid'] == selected['uuid'])
            m.require(set(uuids(after['inbounds'])) >= set(payload['inbounds']), 'Squad readback changed')
    if enable and host['isDisabled']:
        payload = {'uuid': host['uuid'], 'isDisabled': False}
        m.save('front-enable.intent.json', payload)
        m.api('PATCH', '/hosts', payload)
    actual = next(h for h in m.api('GET', '/hosts') if h['uuid'] == host['uuid'])
    m.check(actual, dict(plan['body'], isDisabled=False if enable else actual['isDisabled']))
    print('Host:', actual['remark'], actual['uuid'], 'disabled:', actual['isDisabled'], 'front:', FRONT_IP + ':' + str(FRONT_PORT))


if __name__ == '__main__':
    action = sys.argv[1:]
    if '--panel-tunnel' in action:
        transport_spec = importlib.util.spec_from_file_location('vk_panel_transport', OPS / 'direct-node198/panel_tunnel.py')
        transport = importlib.util.module_from_spec(transport_spec)
        transport_spec.loader.exec_module(transport)
        transport.PORT = 18744
        m.api = transport.adapter(m.ROOT)
        action.remove('--panel-tunnel')
    if action == ['prepare']:
        prepare()
    elif action == ['provision']:
        provision()
    elif action == ['status']:
        status()
    elif action == ['prepare-front']:
        prepare_front()
    elif action == ['stage-front']:
        publish_front()
    elif action == ['enable-front']:
        publish_front(enable=True)
    else:
        print('New Germany backend only:', IP + ':' + str(PORT), '; prepare / provision / status are explicit steps')
