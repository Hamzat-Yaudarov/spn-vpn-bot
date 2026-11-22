"""Add one owner-approved VK relay inbound to the existing Finland node.

No subscriber, rental, DNS, or payment mutations. Sweden is read-only.
"""
import argparse
import base64
import copy
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

OPS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reference', OPS / 'direct-fi/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.STATE = Path('/private/tmp/wayspn-vk-fi120-20261111')
IP = '83.219.97.120'
NODE = '5c608352-00f2-44b8-83a9-97c2bcfa255f'
PROFILE = '8811b5b8-9608-4734-89b0-725372c96037'
SOURCE = '3c0cfecc-5888-4dd7-a38a-023d2edfc223'
FRONT = ('185.86.147.77', 10038)
TAG = 'vk-fi120-relay-reality'
REMARK = '🇫🇮 Финляндия • ОБХОД VK'


def one(rows, uid):
    matches = [r for r in rows if r['uuid'] == uid]
    m.require(len(matches) == 1, 'Missing or ambiguous object')
    return matches[0]


def ids(items):
    return [r['uuid'] if isinstance(r, dict) else r for r in items]


def target(live):
    n, p = one(live['nodes'], NODE), one(live['profiles'], PROFILE)
    m.require(n['address'] == IP and n['configProfile']['activeConfigProfileUuid'] == PROFILE
              and n['isDisabled'] is False, 'Node binding changed')
    m.require([r['uuid'] for r in live['nodes'] if
               r['configProfile']['activeConfigProfileUuid'] == PROFILE] == [NODE], 'Shared profile')
    return n, p


def source_unchanged(live):
    before = m.load('baseline.json')
    for kind, uid in [('hosts', SOURCE), ('profiles', one(before['hosts'], SOURCE)['inbound']['configProfileUuid'])]:
        m.require(m.signatures({kind: [one(before[kind], uid)]}) ==
                  m.signatures({kind: [one(live[kind], uid)]}), 'Sweden changed since snapshot')


def prepare():
    if (m.STATE / 'plan.json').exists():
        print('Existing private additive plan retained')
        return
    live = m.load('baseline.json')
    n, p = target(live)
    source = one(live['hosts'], SOURCE)
    sp = one(live['profiles'], source['inbound']['configProfileUuid'])
    stag = one(sp['inbounds'], source['inbound']['configProfileInboundUuid'])['tag']
    raw, = [copy.deepcopy(i) for i in sp['config']['inbounds'] if i['tag'] == stag]
    m.require(raw['port'] == 2443 and raw['listen'] == '0.0.0.0'
              and raw['settings'] == {'clients': [], 'decryption': 'none'}
              and raw['streamSettings']['security'] == 'reality'
              and raw['streamSettings']['network'] in {'raw', 'tcp'}, 'Unexpected source transport')
    m.require(not any(i['port'] == 2443 or i['tag'] == TAG for i in p['config']['inbounds']), 'Port already used')
    key = m.X25519PrivateKey.generate()
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    private = encode(key.private_bytes(m.serialization.Encoding.Raw, m.serialization.PrivateFormat.Raw,
                                      m.serialization.NoEncryption()))
    public = encode(key.public_key().public_bytes(m.serialization.Encoding.Raw, m.serialization.PublicFormat.Raw))
    short = secrets.token_hex(8)
    raw['tag'] = TAG
    raw['streamSettings']['realitySettings'].update(privateKey=private, shortIds=[short])
    before = p['config']
    after = copy.deepcopy(before)
    after['inbounds'].append(raw)
    audience = [s['uuid'] for s in live['squads'] if source['inbound']['configProfileInboundUuid'] in ids(s['inbounds'])]
    m.require(audience, 'Missing source audience')
    m.save('plan.json', {'before': before, 'after': after, 'oldActive': ids(n['configProfile']['activeInbounds']),
                        'audience': audience, 'scope': {k: copy.deepcopy(source.get(k)) for k in
                         ('internalSquads', 'isHidden', 'excludeFromSubscriptionTypes')},
                        'publicKey': public, 'shortId': short})
    m.save('validation.json', after)
    print('Prepared one new inbound; existing three transports and Sweden preserved')


def stage():
    plan = m.load('plan.json')
    live = m.snapshot()
    source_unchanged(live)
    n, p = target(live)
    m.require(p['config'] in (plan['before'], plan['after']), 'Existing profile changed')
    if p['config'] == plan['before']:
        body = {'uuid': PROFILE, 'config': plan['after']}
        m.save('profile.intent.json', body)
        m.api('PATCH', '/config-profiles', body)
    live = m.snapshot()
    n, p = target(live)
    m.require(p['config'] == plan['after'], 'Profile update not verified')
    iid, = [i['uuid'] for i in p['inbounds'] if i['tag'] == TAG]
    active = ids(n['configProfile']['activeInbounds'])
    wanted = plan['oldActive'] + [iid]
    m.require(set(active) in (set(plan['oldActive']), set(wanted)), 'Active inbounds changed')
    if set(active) != set(wanted):
        body = {'uuid': NODE, 'configProfile': {'activeConfigProfileUuid': PROFILE, 'activeInbounds': wanted}}
        m.save('node.intent.json', body)
        m.api('PATCH', '/nodes', body)
    for uid in plan['audience']:
        members = ids(one(m.api('GET', '/internal-squads')['internalSquads'], uid)['inbounds'])
        if iid not in members:
            body = {'uuid': uid, 'inbounds': members + [iid]}
            m.save('squad-' + uid + '.intent.json', body)
            m.api('PATCH', '/internal-squads', body)
    body = {'remark': REMARK, 'address': FRONT[0], 'port': FRONT[1], 'nodes': [NODE],
            'inbound': {'configProfileUuid': PROFILE, 'configProfileInboundUuid': iid},
            'sni': 'login.vk.com', 'alpn': 'h2', 'fingerprint': 'chrome', 'securityLayer': 'DEFAULT',
            'isDisabled': True, 'isHidden': plan['scope']['isHidden'], 'internalSquads': plan['scope']['internalSquads'],
            'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
            'tags': ['FI120', 'VK_RELAY']}
    if plan['scope']['excludeFromSubscriptionTypes'] is not None:
        body['excludeFromSubscriptionTypes'] = plan['scope']['excludeFromSubscriptionTypes']
    h = m.obtain('grpc', 'vk-front', lambda r: (r['address'], r['port']) == FRONT, body)
    m.save('ids.json', {'host': h['uuid'], 'inbound': iid})
    print('Separate VK host staged disabled:', h['uuid'])


def enable():
    plan, own = m.load('plan.json'), m.load('ids.json')
    live = m.snapshot()
    source_unchanged(live)
    n, p = target(live)
    m.require(n['isConnected'] and not n['lastStatusMessage'] and p['config'] == plan['after'], 'Node not ready')
    m.require(set(ids(n['configProfile']['activeInbounds'])) == set(plan['oldActive'] + [own['inbound']]), 'Wrong active inbounds')
    m.require({s['uuid'] for s in live['squads'] if own['inbound'] in ids(s['inbounds'])} == set(plan['audience']), 'Wrong audience')
    h = one(live['hosts'], own['host'])
    m.check(h, dict(m.load('intent-vk-front.json'), isDisabled=h['isDisabled']))
    if h['isDisabled']:
        body = {'uuid': h['uuid'], 'isDisabled': False}
        m.save('enable.intent.json', body)
        m.api('PATCH', '/hosts', body)
    print('Enabled:', REMARK, FRONT[0] + ':' + str(FRONT[1]))


def probe():
    spec = importlib.util.spec_from_file_location('probe', OPS / 'tekst-setup/physical_probe.py')
    physical = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(physical)
    e = physical.e
    engine, digest = physical.ENGINES['26.9.9']
    e.pinned_engine(engine, digest)
    plan = m.load('plan.json')
    owner = e.protected_json(physical.ROOT / 'owner-selected.private.json')
    link_file = m.ROOT / '.vpn-test-link.txt'
    m.require(not link_file.is_symlink() and link_file.stat().st_mode & 0o777 == 0o600, 'Unsafe owner link file')
    link = urlsplit(link_file.read_text().strip())
    m.require(link.scheme == 'https' and link.hostname == 'sub.wayspn.online'
              and link.path.strip('/') == owner['shortUuid'] and owner['status'] == 'ACTIVE'
              and set(ids(owner['activeInternalSquads'])) & set(plan['audience']),
              'Existing owner test account outside approved audience')
    out = {'tag': 'vk-fi120-test', 'protocol': 'vless',
           'settings': {'vnext': [{'address': FRONT[0], 'port': FRONT[1],
              'users': [{'id': owner['vlessUuid'], 'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
           'streamSettings': {'network': 'raw', 'security': 'reality', 'realitySettings': {
               'serverName': 'login.vk.com', 'fingerprint': 'chrome', 'publicKey': plan['publicKey'],
               'shortId': plan['shortId'], 'spiderX': '/'}}}
    current_path = m.STATE / 'subscription.current.private.json'
    if current_path.exists():
        current = e.protected_json(current_path)
        docs = current if isinstance(current, list) else [current]
        actual = []
        for doc in docs:
            for candidate in doc.get('outbounds', []):
                point, _ = e.endpoint(candidate)
                if point and (point['address'], point['port']) == FRONT:
                    actual.append(candidate)
        m.require(actual, 'New host not found in current owner subscription')
        out = copy.deepcopy(actual[0])
    physical_ip = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', 'en0'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='vk-fi120-probe-', dir='/private/tmp') as tmp:
        result = physical.probe(FRONT, out, Path(tmp), physical_ip, engine, '26.9.9', 'https204')
    m.save('probe.result.' + str(time.time_ns()) + '.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'stage', 'enable', 'probe'])
    parser.add_argument('--panel-tunnel', action='store_true')
    args = parser.parse_args()
    if args.panel_tunnel:
        spec = importlib.util.spec_from_file_location('tunnel', OPS / 'direct-node198/panel_tunnel.py')
        tunnel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tunnel)
        tunnel.PORT = 18775
        m.api = tunnel.adapter(m.ROOT)
    try:
        globals()[args.action]()
    except Exception as error:
        print('Stopped safely:', type(error).__name__, str(error)[:160])
        raise SystemExit(2)
