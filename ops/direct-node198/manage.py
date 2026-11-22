"""Provision only the node198 profile, node and hosts in the SPN panel."""
import copy
import importlib.util
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('direct_reference', OPS / 'direct-fi/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.STATE = Path('/private/tmp/wayspn-node198-panel')
IP = '179.236.145.198'
ORIGIN = 'node198.wayspn.com'
EDGE = 'cdn-node198.wayspn.com'
PROFILE = 'DIRECT_NODE198'
NODE = 'node-direct-145.198'
SOURCES = ('3d366bb6-6ebb-4105-a4a3-9a0ad60457cc', '924c98ad-43e6-4b7b-abb5-ccfd37889ae4', 'e3b6e73d-9c3a-449a-b8b2-e4e313e81e44')
KINDS = ('grpc', 'ws', 'xhttp')
REMARKS = ('Node 198 • gRPC', 'Node 198 • WS', 'Node 198 • CDN XHTTP')


def one(rows, uid):
    return next(r for r in rows if r['uuid'] == uid)


def create(key, path, read, match, payload):
    rows = [row for row in read() if match(row)]
    m.require(len(rows) <= 1, 'Ambiguous target ' + key)
    intent = 'intent-' + key + '.json'
    if rows:
        m.require(m.load(intent) == payload, 'Existing target not owned by this operation')
        row = rows[0]
    else:
        m.require(not (m.STATE / intent).exists(), 'Prior attempt uncertain; inspect ' + key)
        m.save(intent, payload)
        row = m.api('POST', path, payload)
    m.check(row, payload)
    return row


def read_hosts():
    return m.api('GET', '/hosts')


def read_profiles():
    return m.api('GET', '/config-profiles')['configProfiles']


def read_nodes():
    return m.api('GET', '/nodes')


def prepare():
    if (m.STATE / 'plan.json').exists():
        print('Existing plan preserved')
        return
    live = m.snapshot()
    m.require(not any(p['name'] == PROFILE for p in live['profiles']), 'Profile exists')
    m.require(not any(n['address'] == IP or n['name'] == NODE for n in live['nodes']), 'Node exists')
    m.require(not any(h['address'] in {IP, ORIGIN, EDGE} or h['remark'] in REMARKS for h in live['hosts']), 'Host exists')
    source = [one(live['hosts'], uid) for uid in SOURCES]
    profile_ids = {h['inbound']['configProfileUuid'] for h in source}
    m.require(len(profile_ids) == 1, 'Reference profile changed')
    profile = one(live['profiles'], profile_ids.pop())
    m.require(len(profile['config']['inbounds']) == 3, 'Reference inbounds changed')
    config = copy.deepcopy(profile['config'])
    m.require(all(o.get('protocol') in {'freedom', 'blackhole', 'dns'} for o in config['outbounds']), 'Unexpected source egress')
    by_tag = {i['tag']: i for i in config['inbounds']}
    mapping = {}
    host_plans = []
    for kind, old, remark in zip(KINDS, source, REMARKS):
        source_id = old['inbound']['configProfileInboundUuid']
        old_tag = one(profile['inbounds'], source_id)['tag']
        inbound = by_tag[old_tag]
        stream = inbound['streamSettings']
        m.require(inbound['protocol'] == 'vless' and inbound['settings']['clients'] == [] and stream['network'] == kind, 'Unexpected inbound')
        tag = 'node198-' + kind
        mapping[old_tag] = tag
        inbound['tag'] = tag
        domain = EDGE if kind == 'xhttp' else ORIGIN
        fields = ('port', 'path', 'alpn', 'fingerprint', 'securityLayer', 'xhttpExtraParams', 'internalSquads', 'isHidden')
        host = {k: old[k] for k in fields if old.get(k) is not None}
        host.update(remark=remark, address=IP if kind == 'grpc' else domain, sni=domain,
                    isDisabled=True, tags=['NODE198'], mihomoX25519=False, overrideSniFromAddress=False, keepSniBlank=False)
        if kind == 'grpc':
            m.require(stream['security'] == 'reality' and inbound['port'] == host['port'] == 2083 and host['path'] == stream['grpcSettings']['serviceName'], 'Unexpected gRPC')
            key = m.X25519PrivateKey.generate().private_bytes(m.serialization.Encoding.Raw, m.serialization.PrivateFormat.Raw, m.serialization.NoEncryption())
            stream['realitySettings'].update(privateKey=m.base64.urlsafe_b64encode(key).decode().rstrip('='),
                                             shortIds=[m.secrets.token_hex(8)], serverNames=[ORIGIN], target='127.0.0.1:443')
        else:
            m.require(inbound['listen'] == '127.0.0.1' and stream['security'] == 'none' and host['port'] == 443 and host['securityLayer'] == 'TLS', 'Unexpected TLS upstream')
            host['host'] = domain
            if kind == 'ws':
                m.require(inbound['port'] == 10088 and stream['wsSettings']['path'] == host['path'] == '/direct/ws/', 'Unexpected WS path')
            else:
                cfg = stream['xhttpSettings']
                client = m.json.loads(host['xhttpExtraParams']) if isinstance(host['xhttpExtraParams'], str) else host['xhttpExtraParams']
                m.require(inbound['port'] == 10085 and cfg['path'] == host['path'] == '/assets/app/' and cfg['mode'] == 'packet-up', 'Unexpected XHTTP path')
                m.require(all(s['uplinkHTTPMethod'] == 'GET' and s['uplinkDataPlacement'] == 'header' and s['uplinkDataKey'] == 'X-Transport-Data' for s in (cfg, client)), 'Unexpected XHTTP method')
        host_plans.append({'tag': tag, 'source_id': source_id, 'host': host})
    for rule in config.get('routing', {}).get('rules', []):
        if rule.get('inboundTag'):
            m.require(all(t in mapping for t in rule['inboundTag']), 'Unexpected routing rule')
            rule['inboundTag'] = [mapping[t] for t in rule['inboundTag']]
    if not (m.STATE / 'node.env').exists():
        secret = m.api('GET', '/keygen')['secretKey']
        m.require(isinstance(secret, str) and len(secret) > 20 and not any(c.isspace() for c in secret), 'Invalid node secret')
        m.save('node.env', 'NODE_PORT=2222\nSECRET_KEY=' + secret + '\n')
    m.save('plan.json', {'profile': {'name': PROFILE, 'config': config}, 'hosts': host_plans})
    print('Three scoped inbounds prepared; no panel writes')


def provision():
    plan = m.load('plan.json')
    p = create('profile', '/config-profiles', read_profiles, lambda r: r['name'] == PROFILE, plan['profile'])
    ids = {i['tag']: i['uuid'] for i in p['inbounds']}
    m.require(set(ids) == {'node198-' + k for k in KINDS}, 'Unexpected target inbounds')
    n = create('node', '/nodes', read_nodes, lambda r: r['address'] == IP or r['name'] == NODE,
               {'name': NODE, 'address': IP, 'port': 2222, 'tags': ['NODE198'],
                'configProfile': {'activeConfigProfileUuid': p['uuid'], 'activeInbounds': list(ids.values())}})
    m.save('ids.json', {'node': n['uuid'], 'profile': p['uuid'], 'inbounds': ids})
    print('Profile:', p['uuid'], 'Node:', n['uuid'], 'connected:', n.get('isConnected'))


def publish(kinds):
    plan, ids = m.load('plan.json'), m.load('ids.json')
    profile = one(read_profiles(), ids['profile'])
    m.require(profile['name'] == PROFILE and profile['config'] == plan['profile']['config'], 'Target profile changed')
    m.require({i['tag']: i['uuid'] for i in profile['inbounds']} == ids['inbounds'], 'Target inbound identities changed')
    node = one(read_nodes(), ids['node'])
    m.require(node['address'] == IP and node['name'] == NODE and node['isConnected'] and not node['isDisabled'], 'Target node not connected')
    binding = node['configProfile']
    m.require(binding['activeConfigProfileUuid'] == ids['profile'] and
              {i['uuid'] if isinstance(i, dict) else i for i in binding['activeInbounds']} == set(ids['inbounds'].values()),
              'Target node inbound binding changed')
    for item in plan['hosts']:
        tag = item['tag']
        if tag.removeprefix('node198-') not in kinds:
            continue
        inbound = ids['inbounds'][tag]
        body = dict(item['host'], nodes=[ids['node']], inbound={'configProfileUuid': ids['profile'], 'configProfileInboundUuid': inbound})
        match = lambda h: h['remark'] == body['remark'] or h.get('inbound', {}).get('configProfileInboundUuid') == inbound
        rows = [h for h in read_hosts() if match(h)]
        if len(rows) == 1 and rows[0].get('isDisabled') is False:
            m.require(m.load('intent-' + tag + '.json') == body, 'Unexpected enabled host')
            host = rows[0]
            m.check(host, dict(body, isDisabled=False))
        else:
            host = create(tag, '/hosts', read_hosts, match, body)
        squads = m.api('GET', '/internal-squads')['internalSquads']
        count = 0
        for squad in squads:
            members = [i['uuid'] for i in squad['inbounds']]
            if item['source_id'] not in members:
                continue
            count += 1
            if inbound not in members:
                m.api('PATCH', '/internal-squads', {'uuid': squad['uuid'], 'inbounds': members + [inbound]})
        m.require(count > 0, 'No reference squads')
        if host['isDisabled']:
            m.api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': False})
        checked = one(read_hosts(), host['uuid'])
        m.require(checked['isDisabled'] is False, 'Host still disabled')
        print('Enabled:', checked['remark'], checked['uuid'], 'squads:', count)


if __name__ == '__main__':
    action = sys.argv[1:] or ['plan']
    if '--panel-tunnel' in action:
        from panel_tunnel import adapter
        m.api = adapter(m.ROOT)
        action.remove('--panel-tunnel')
    if action == ['prepare']:
        prepare()
    elif action == ['provision']:
        provision()
    elif action == ['publish-direct']:
        publish({'grpc', 'ws'})
    elif action == ['publish-cdn']:
        publish({'xhttp'})
    elif action == ['status']:
        ids = m.load('ids.json')
        node = one(read_nodes(), ids['node'])
        print({k: node.get(k) for k in ('name', 'address', 'isConnected', 'isDisabled', 'versions', 'lastStatusMessage')})
        for host in read_hosts():
            if host.get('inbound', {}).get('configProfileUuid') == ids['profile']:
                print({k: host.get(k) for k in ('uuid', 'remark', 'address', 'port', 'isDisabled')})
    else:
        print(IP, ORIGIN, EDGE, PROFILE, NODE, REMARKS)
