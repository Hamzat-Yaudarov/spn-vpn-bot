"""Fixed Remnawave migration: staged Germany direct and existing VK front to Sweden."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('reference', HERE.parent / 'direct-fi/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.STATE = Path('/private/tmp/wayspn-vk-rehome-20261109')
must = m.require

NODES = {'de': ('46ac00c9-937c-4f07-9ed4-f72956f6553c', '48.102.177.3'),
         'se': ('fcc9f57f-8f66-4a75-b090-2762c0f962aa', '31.59.153.161')}
PROFILES = {'de': '076d8171-6e3c-401e-bf84-88520145dbfb', 'se': '76b9408f-5a9d-4e00-89ed-f5e3109032ef'}
FRONT = '3c0cfecc-5888-4dd7-a38a-023d2edfc223'
RELAY = ('185.86.147.77', 10016, 'login.vk.com')
SE_SOURCE = {'grpc': 'a68764f9-662e-4598-94fc-2ba3afe0f4c8', 'ws': 'b60a0566-a1e2-4142-b52a-94aa4f1e7d20'}
DOMAIN = 'de177.wayspn.com'
PANEL_TUNNEL_PORT = 18774
PATHS = {'grpc': 'spngrpc', 'ws': '/direct/ws/'}
PORTS = {'grpc': 10087, 'ws': 10088}
REMARKS = {'grpc': '🇩🇪 Германия • gRPC • 177', 'ws': '🇩🇪 Германия • WS • 177'}
NEW_TAGS = {'se': {'vk': 'vk-se-relay-reality'}, 'de': {'grpc': 'de177-grpc', 'ws': 'de177-ws'}}


def one(rows, uid):
    found = [r for r in rows if r.get('uuid') == uid]
    must(len(found) == 1, 'Missing or ambiguous UUID')
    return found[0]


def ids(rows):
    result = [r['uuid'] if isinstance(r, dict) else r for r in rows]
    must(len(set(result)) == len(result), 'Duplicate inbound')
    return result


def node(live, region):
    uid, ip = NODES[region]
    n = one(live['nodes'], uid)
    must(n['address'] == ip and n['port'] == 2222 and n['isDisabled'] is False
         and n['configProfile']['activeConfigProfileUuid'] == PROFILES[region], 'Target node binding changed')
    users = [r['uuid'] for r in live['nodes'] if (r.get('configProfile') or {}).get('activeConfigProfileUuid') == PROFILES[region]]
    must(users == [uid], 'Target profile shared by another node')
    return n


def profile(live, region):
    return one(live['profiles'], PROFILES[region])


def tags(p):
    result = {i['tag']: i['uuid'] for i in p['inbounds']}
    must(len(result) == len(p['inbounds']) and set(result) == {i['tag'] for i in p['config']['inbounds']},
         'Unexpected profile tags')
    return result


def binding(p, tag):
    return {'configProfileUuid': p['uuid'], 'configProfileInboundUuid': tags(p)[tag]}


def active(n):
    return ids(n['configProfile']['activeInbounds'])


def scope(live, h):
    b = h['inbound']['configProfileInboundUuid']
    s = sorted(r['uuid'] for r in live['squads'] if b in ids(r['inbounds']))
    must(s and h['isDisabled'] is False and type(h['isHidden']) is bool, 'Expected active host audience')
    must(h['internalSquads']['mode'] in {'INCLUDE', 'EXCLUDE'}, 'Unexpected host visibility')
    return {'squads': s, 'isHidden': h['isHidden'], 'internalSquads': copy.deepcopy(h['internalSquads']),
            'excludeFromSubscriptionTypes': copy.deepcopy(h.get('excludeFromSubscriptionTypes'))}


def direct_inbound(kind):
    extra = {'grpcSettings': {'serviceName': PATHS[kind]}} if kind == 'grpc' else {'wsSettings': {'path': PATHS[kind]}}
    return {'tag': NEW_TAGS['de'][kind], 'listen': '127.0.0.1', 'port': PORTS[kind], 'protocol': 'vless',
            'settings': {'clients': [], 'decryption': 'none'},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
            'streamSettings': {'network': kind, 'security': 'none', **extra}}


def original_front(live, plan):
    f = one(live['hosts'], FRONT)
    must((f['address'], f['port'], f['sni']) == RELAY and f['isDisabled'] is False
         and f['securityLayer'] == 'DEFAULT', 'Published VK endpoint changed')
    before = plan['front']
    for key in ('inbound', 'nodes', 'remark', 'internalSquads', 'isHidden', 'excludeFromSubscriptionTypes'):
        must(f.get(key) == before.get(key), 'Existing VK host changed: ' + key)
    must(scope(live, f) == plan['se']['scope']['vk'], 'Existing VK audience changed')
    return f


def build(live):
    pde, pse = profile(live, 'de'), profile(live, 'se')
    nde, nse = node(live, 'de'), node(live, 'se')
    raw, = pde['config']['inbounds']
    must(len(pse['config']['inbounds']) == 2, 'Unexpected existing Sweden profile')
    front = one(live['hosts'], FRONT)
    must(front['inbound'] == binding(pde, raw['tag']) and front['nodes'] == [nde['uuid']], 'VK host not on original node')
    stream = raw['streamSettings']
    reality = stream['realitySettings']
    must(raw['listen'] == '0.0.0.0' and raw['port'] == 2443 and raw['protocol'] == 'vless'
         and raw['settings'] == {'clients': [], 'decryption': 'none'}
         and stream['network'] in {'raw', 'tcp'} and stream['security'] == 'reality'
         and reality['target'] == 'login.vk.com:443' and reality['serverNames'] == ['login.vk.com']
         and bool(reality['privateKey']) and bool(reality['shortIds']), 'Existing RAW inbound changed')
    copied = copy.deepcopy(raw)
    copied['tag'] = NEW_TAGS['se']['vk']
    plan = {'front': {k: copy.deepcopy(front.get(k)) for k in ('inbound', 'nodes', 'remark', 'internalSquads',
             'isHidden', 'excludeFromSubscriptionTypes')}, 'source': {}}
    source_scope = {}
    for kind, uid in SE_SOURCE.items():
        h = one(live['hosts'], uid)
        b = h['inbound']
        must(h['nodes'] == [nse['uuid']] and h['address'] == 'ch121.wayspn.com' and h['port'] == 443
             and b['configProfileUuid'] == pse['uuid'], 'Sweden reference host changed')
        meta = one(pse['inbounds'], b['configProfileInboundUuid'])
        candidate, = [i for i in pse['config']['inbounds'] if i['tag'] == meta['tag']]
        must(candidate['streamSettings']['network'] == kind and candidate['port'] == PORTS[kind]
             and candidate['listen'] == '127.0.0.1', 'Sweden reference transport changed')
        source_scope[kind] = scope(live, h)
    for region, n, p, append, scopes in (
        ('se', nse, pse, [copied], {'vk': scope(live, front)}),
        ('de', nde, pde, [direct_inbound(kind) for kind in PATHS], source_scope),
    ):
        before = copy.deepcopy(p['config'])
        after = copy.deepcopy(before)
        after['inbounds'] += append
        must(len({i['port'] for i in after['inbounds']}) == len(after['inbounds']), 'Inbound port already exists')
        must(set(active(n)) == set(tags(p).values()), 'Unexpected inactive old inbound')
        plan[region] = {'before': before, 'after': after, 'tags': tags(p), 'active': active(n), 'scope': scopes}
    plan['source'] = source_scope
    original_front(live, plan)
    must(not any(h['address'] == DOMAIN or h['remark'] in REMARKS.values() for h in live['hosts']),
         'German host or hostname already exists')
    return plan


def prepare():
    if not (m.STATE / 'plan.json').exists():
        m.save('plan.json', build(m.snapshot()))
    print('Private plan ready: DE add gRPC/WS, SE add original VK transport. No panel writes.')


def fresh(plan):
    live = m.snapshot()
    original_front(live, plan)
    for kind, uid in SE_SOURCE.items():
        must(scope(live, one(live['hosts'], uid)) == plan['source'][kind], 'Reference audience changed')
    return live


def new_ids(live, region, plan):
    p = profile(live, region)
    target = plan[region]
    must(p['config'] == target['after'], 'Staged profile does not match')
    by_tag = tags(p)
    must(all(by_tag.get(tag) == uid for tag, uid in target['tags'].items()), 'Old inbound UUID changed')
    return {kind: by_tag[tag] for kind, tag in NEW_TAGS[region].items()}


def host_body(kind, item, p, n):
    body = {'remark': REMARKS[kind], 'address': DOMAIN, 'port': 443, 'path': PATHS[kind], 'sni': DOMAIN, 'host': DOMAIN,
            'alpn': 'h2' if kind == 'grpc' else 'http/1.1', 'fingerprint': 'chrome', 'securityLayer': 'TLS',
            'isDisabled': True, 'isHidden': item['isHidden'], 'internalSquads': item['internalSquads'],
            'tags': ['DE177', 'DIRECT'], 'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
            'nodes': [n['uuid']], 'inbound': binding(p, NEW_TAGS['de'][kind])}
    if item['excludeFromSubscriptionTypes'] is not None:
        body['excludeFromSubscriptionTypes'] = item['excludeFromSubscriptionTypes']
    return body


def stage(region):
    plan = m.load('plan.json')
    live = fresh(plan)
    n, p, target = node(live, region), profile(live, region), plan[region]
    must(p['config'] in (target['before'], target['after']), 'Existing config changed')
    if p['config'] == target['before']:
        must(tags(p) == target['tags'] and active(n) == target['active'], 'Existing node inbounds changed')
        body = {'uuid': p['uuid'], 'config': target['after']}
        m.save(f'profile-{region}.intent.json', body)
        m.api('PATCH', '/config-profiles', body)
        live = fresh(plan)
    new = new_ids(live, region, plan)
    n = node(live, region)
    wanted = target['active'] + list(new.values())
    current = active(n)
    must(set(target['active']) <= set(current) <= set(wanted), 'Existing active inbound list changed')
    if set(current) != set(wanted):
        body = {'uuid': n['uuid'], 'configProfile': {'activeConfigProfileUuid': PROFILES[region], 'activeInbounds': wanted}}
        m.save(f'node-{region}.intent.json', body)
        m.api('PATCH', '/nodes', body)
    live = fresh(plan)
    must(set(active(node(live, region))) == set(wanted), 'Node did not activate candidate inbounds')
    for k, iid in new.items():
        selected = set(target['scope'][k]['squads'])
        squads = m.api('GET', '/internal-squads')['internalSquads']
        must({s['uuid'] for s in squads if iid in ids(s['inbounds'])} <= selected, 'New inbound in unexpected squad')
        for uid in sorted(selected):
            current = ids(one(m.api('GET', '/internal-squads')['internalSquads'], uid)['inbounds'])
            if iid not in current:
                body = {'uuid': uid, 'inbounds': current + [iid]}
                m.save(f'squad-{region}-{k}-{uid}.intent.json', body)
                row = m.api('PATCH', '/internal-squads', body)
                must(set(ids(row['inbounds'])) == set(body['inbounds']), 'Squad addition not verified')
    live = fresh(plan)
    check_audience(live, region, plan, new)
    if region == 'de':
        for k, iid in new.items():
            body = host_body(k, target['scope'][k], profile(live, region), node(live, region))
            pred = lambda h: h['remark'] == REMARKS[k] or (h.get('inbound') or {}).get('configProfileInboundUuid') == iid
            h = m.obtain(k, k, pred, body)
            print('Staged disabled:', h['remark'], h['uuid'])
    else:
        print('Sweden staged; current public VK host and relay remain unchanged.')


def check_audience(live, region, plan, new):
    must(set(active(node(live, region))) == set(plan[region]['active']) | set(new.values()), 'Wrong active inbounds')
    for k, iid in new.items():
        must({s['uuid'] for s in live['squads'] if iid in ids(s['inbounds'])}
             == set(plan[region]['scope'][k]['squads']), 'Inbound audience is not verified')


def enable_de():
    plan = m.load('plan.json')
    live = fresh(plan)
    check_audience(live, 'de', plan, new_ids(live, 'de', plan))
    n = node(live, 'de')
    must(n['isConnected'] is True and not n.get('lastStatusMessage'), 'German node not ready')
    for k in PATHS:
        h = one(live['hosts'], m.load('created-' + k + '.json')['uuid'])
        body = host_body(k, plan['de']['scope'][k], profile(live, 'de'), n)
        m.check(h, {**body, 'isDisabled': h['isDisabled']})
        if h['isDisabled']:
            payload = {'uuid': h['uuid'], 'isDisabled': False}
            m.save('enable-' + k + '.intent.json', payload)
            actual = m.api('PATCH', '/hosts', payload)
            m.check(actual, {**body, 'isDisabled': False})
        print('Enabled:', h['remark'], h['uuid'])


def cutover():
    plan = m.load('plan.json')
    live = fresh(plan)
    n, p = node(live, 'se'), profile(live, 'se')
    must(n['isConnected'] is True and not n.get('lastStatusMessage'), 'Sweden node not ready')
    check_audience(live, 'se', plan, new_ids(live, 'se', plan))
    payload = {'uuid': FRONT, 'remark': '🇸🇪 Швеция • ОБХОД VK', 'nodes': [n['uuid']],
               'inbound': binding(p, NEW_TAGS['se']['vk'])}
    m.save('front-cutover.intent.json', payload)
    changed = m.api('PATCH', '/hosts', payload)
    must((changed['address'], changed['port'], changed['sni']) == RELAY and changed['isDisabled'] is False,
         'Relay identity changed')
    must(all(changed[k] == value for k, value in payload.items()), 'Host migration not verified')
    print('Same VK public endpoint now published as Sweden.')


def retire_de_vk():
    plan = m.load('plan.json')
    live = m.snapshot()
    f = one(live['hosts'], FRONT)
    migration = m.load('front-cutover.intent.json')
    must((f['address'], f['port'], f['sni']) == RELAY and f['isDisabled'] is False
         and all(f[k] == value for k, value in migration.items()), 'VK front has not moved to Sweden')
    se_ids = new_ids(live, 'se', plan)
    check_audience(live, 'se', plan, se_ids)
    must(scope(live, f) == plan['se']['scope']['vk'], 'Migrated host audience changed')
    de_ids = new_ids(live, 'de', plan)
    old, = plan['de']['active']
    must(not any((h.get('inbound') or {}).get('configProfileInboundUuid') == old for h in live['hosts']),
         'Another published host still uses the German VK inbound')
    n = node(live, 'de')
    for k in PATHS:
        h = one(live['hosts'], m.load('created-' + k + '.json')['uuid'])
        expected = host_body(k, plan['de']['scope'][k], profile(live, 'de'), n)
        expected.pop('remark')  # Owner renamed the two German hosts; identity is the saved UUID.
        m.check(h, {**expected, 'isDisabled': False})
    current = active(n)
    wanted = list(de_ids.values())
    must(set(current) in (set(wanted), set(wanted) | {old}), 'Unexpected German active inbounds')
    if old in current:
        payload = {'uuid': n['uuid'], 'configProfile': {'activeConfigProfileUuid': PROFILES['de'], 'activeInbounds': wanted}}
        m.save('retire-de-vk.intent.json', payload)
        m.api('PATCH', '/nodes', payload)
    checked = node(m.snapshot(), 'de')
    must(set(active(checked)) == set(wanted), 'German old inbound not deactivated')
    print('German VK listener deactivated; profile and rollback state retained.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('prepare', 'stage-se', 'stage-de', 'enable-de', 'cutover', 'retire-de-vk'))
    parser.add_argument('--verified', action='store_true')
    parser.add_argument('--panel-tunnel', action='store_true')
    args = parser.parse_args()
    if args.panel_tunnel:
        p = importlib.util.spec_from_file_location('panel_tunnel', HERE.parent / 'direct-node198/panel_tunnel.py')
        relay = importlib.util.module_from_spec(p)
        p.loader.exec_module(relay)
        relay.PORT = PANEL_TUNNEL_PORT
        m.api = relay.adapter(m.ROOT)
    if not args.action:
        print(json.dumps({'planOnly': True, 'nodes': NODES, 'domain': DOMAIN, 'relay': RELAY}))
    elif args.action == 'prepare':
        prepare()
    elif args.action.startswith('stage-'):
        stage(args.action[-2:])
    else:
        must(args.verified, 'Actual tunnel checks are required before publication')
        {'enable-de': enable_de, 'cutover': cutover, 'retire-de-vk': retire_de_vk}[args.action]()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        print('VK rehome stopped:', detail, file=sys.stderr)
        raise SystemExit(2) from None
