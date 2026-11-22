"""Stage a dedicated gRPC/WS TLS node; no relay, DNS, certificate or user writes.

Default: offline plan. Prepare reads existing infrastructure and obtains a new
node service key. Provision creates two disabled hosts. Bind-squads is
additive, keeping the hosts disabled for an existing-account test. Only an
explicit activate --verified enables them after the operator's checks.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys

OPS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('direct_reference', OPS / 'direct-fi/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.STATE = Path('/private/tmp/wayspn-node18253-panel')

ADDRESS = '48.102.182.53'
DOMAIN = 'node18253.wayspn.com'
PROFILE = 'DIRECT_NODE18253'
NODE = 'node-direct-18253'
KINDS = ('grpc', 'ws')
TAGS = ['NODE18253', 'DIRECT']
SOURCE_HOSTS = {'grpc': '3d366bb6-6ebb-4105-a4a3-9a0ad60457cc',
                'ws': '924c98ad-43e6-4b7b-abb5-ccfd37889ae4'}
PATHS = {'grpc': 'spngrpc', 'ws': '/direct/ws/'}
PORTS = {'grpc': 10087, 'ws': 10088}
REMARKS = {'grpc': 'Node 18253 • gRPC', 'ws': 'Node 18253 • WS'}
PANEL_TUNNEL_PORT = 18753


def one(rows, uid):
    found = [r for r in rows if r.get('uuid') == uid]
    m.require(len(found) == 1, 'Missing or ambiguous infrastructure identity')
    return found[0]


def ids(rows):
    result = [r['uuid'] if isinstance(r, dict) else r for r in rows]
    m.require(len(set(result)) == len(result), 'Duplicate infrastructure identity')
    return result


def tag(kind):
    return 'node18253-' + kind


def config():
    inbounds = []
    for kind in KINDS:
        extra = {'grpcSettings': {'serviceName': PATHS[kind]}} if kind == 'grpc' else {
            'wsSettings': {'path': PATHS[kind]}}
        inbounds.append({'tag': tag(kind), 'listen': '127.0.0.1', 'port': PORTS[kind],
                         'protocol': 'vless', 'settings': {'clients': [], 'decryption': 'none'},
                         'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
                         'streamSettings': {'network': kind, 'security': 'none', **extra}})
    return {'log': {'loglevel': 'warning', 'access': 'none'},
            'dns': {'servers': ['1.1.1.1', '8.8.8.8'], 'queryStrategy': 'UseIPv4'},
            'inbounds': inbounds,
            'outbounds': [{'protocol': 'freedom', 'tag': 'direct', 'settings': {'domainStrategy': 'UseIPv4'}},
                          {'protocol': 'blackhole', 'tag': 'block'}],
            'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [
                {'type': 'field', 'ip': ['geoip:private', '169.254.0.0/16', '168.63.129.16/32', '100.64.0.0/10'],
                 'outboundTag': 'block'},
                {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'block'},
            ]}}


def source_scope(live):
    result = {}
    for kind, uid in SOURCE_HOSTS.items():
        host = one(live['hosts'], uid)
        m.require(host.get('isDisabled') is False, 'Reference host is disabled')
        link = host['inbound']
        profile = one(live['profiles'], link['configProfileUuid'])
        m.require(profile['name'] == 'DIRECT_FI_102_103', 'Reference profile changed')
        inbound = one(profile['inbounds'], link['configProfileInboundUuid'])
        matches = [i for i in profile['config']['inbounds'] if i['tag'] == inbound['tag']]
        m.require(len(matches) == 1, 'Reference inbound identity changed')
        raw = matches[0]
        m.require(raw['protocol'] == 'vless' and raw['streamSettings']['network'] == kind,
                  'Reference transport changed')
        policy = host.get('internalSquads')
        m.require(isinstance(policy, dict) and policy.get('mode') in {'INCLUDE', 'EXCLUDE'}
                  and isinstance(policy.get('squads'), list), 'Unknown source host visibility')
        m.require(type(host.get('isHidden')) is bool, 'Unknown source host visibility')
        exclusions = host.get('excludeFromSubscriptionTypes')
        m.require(exclusions is None or isinstance(exclusions, list), 'Unknown source subscription exclusions')
        uid = inbound['uuid']
        squads = sorted(s['uuid'] for s in live['squads'] if uid in ids(s['inbounds']))
        m.require(squads, 'No reference audience')
        result[kind] = {'sourceInbound': uid, 'squads': squads, 'internalSquads': copy.deepcopy(policy),
                        'isHidden': host['isHidden'], 'exclusions': copy.deepcopy(exclusions)}
    return result


def prepare():
    if (m.STATE / 'plan.json').exists():
        plan()
        print('Existing private plan preserved; no new service key requested')
        return
    live = m.snapshot()
    m.require(not any(p['name'] == PROFILE for p in live['profiles']), 'Target profile already exists')
    m.require(not any(n['name'] == NODE or n['address'] == ADDRESS for n in live['nodes']), 'Target node already exists')
    m.require(not any(h['address'] in {ADDRESS, DOMAIN} or h['remark'] in REMARKS.values() for h in live['hosts']),
              'Target host already exists')
    scope = source_scope(live)
    candidate = config()
    m.save('candidate.json', candidate)
    m.save('signatures.before.json', m.signatures(live))
    affected = sorted({u for row in scope.values() for u in row['squads']})
    m.save('squads.before.json', {uid: ids(one(live['squads'], uid)['inbounds']) for uid in affected})
    if not (m.STATE / 'node.env').exists():
        secret = m.api('GET', '/keygen')['secretKey']
        m.require(isinstance(secret, str) and len(secret) > 20 and not any(c.isspace() for c in secret),
                  'Invalid node service key')
        m.save('node.env', 'NODE_PORT=2222\nSECRET_KEY=' + secret + '\n')
    m.save('plan.json', {'address': ADDRESS, 'domain': DOMAIN, 'profile': PROFILE,
                         'node': NODE, 'profileHash': m.digest(candidate), 'scope': scope})
    print('Prepared private candidate and dedicated node.env; no panel writes')


def plan():
    data = m.load('plan.json')
    m.require(data['address'] == ADDRESS and data['domain'] == DOMAIN and data['profile'] == PROFILE
              and data['node'] == NODE and set(data['scope']) == set(KINDS), 'Plan target changed')
    m.require(m.load('candidate.json') == config() and data['profileHash'] == m.digest(config()),
              'Candidate config changed')
    secret = m.STATE / 'node.env'
    m.require(secret.is_file() and not secret.is_symlink() and secret.stat().st_mode & 0o777 == 0o600,
              'Private node.env is missing or unsafe')
    return data


def host_payload(kind, scope, profile, node, inbound):
    body = {'remark': REMARKS[kind], 'address': DOMAIN, 'port': 443, 'path': PATHS[kind],
            'sni': DOMAIN, 'host': DOMAIN, 'alpn': 'h2' if kind == 'grpc' else 'http/1.1',
            'fingerprint': 'chrome', 'securityLayer': 'TLS', 'isDisabled': True,
            'isHidden': scope['isHidden'], 'internalSquads': scope['internalSquads'],
            'tags': TAGS, 'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
            'nodes': [node], 'inbound': {'configProfileUuid': profile, 'configProfileInboundUuid': inbound}}
    if scope['exclusions'] is not None:
        body['excludeFromSubscriptionTypes'] = scope['exclusions']
    return body


def provision():
    data = plan()
    m.require(source_scope(m.snapshot()) == data['scope'], 'Reference audience changed before provisioning')
    profile = m.obtain('profile', 'profile', lambda p: p['name'] == PROFILE, {'name': PROFILE, 'config': config()})
    inbound = {r['tag']: r['uuid'] for r in profile['inbounds']}
    m.require(len(profile['inbounds']) == 2 and set(inbound) == {tag(k) for k in KINDS}, 'Target inbounds changed')
    node = m.obtain('node', 'node', lambda n: n['name'] == NODE or n['address'] == ADDRESS,
                    {'name': NODE, 'address': ADDRESS, 'port': 2222, 'tags': TAGS,
                     'configProfile': {'activeConfigProfileUuid': profile['uuid'],
                                       'activeInbounds': [inbound[tag(k)] for k in KINDS]}})
    for kind in KINDS:
        iid = inbound[tag(kind)]
        payload = host_payload(kind, data['scope'][kind], profile['uuid'], node['uuid'], iid)
        match = lambda h: h['remark'] == REMARKS[kind] or (h.get('inbound') or {}).get('configProfileInboundUuid') == iid
        host = m.obtain(kind, kind, match, payload)
        print('Staged disabled:', host['remark'], host['uuid'])
    m.save('ids.json', {'profile': profile['uuid'], 'node': node['uuid'], 'inbounds': inbound})
    print('Created new dedicated profile/node/two disabled hosts:', profile['uuid'], node['uuid'])


def audit(*, ready=False):
    data = plan()
    live = m.snapshot()
    p = one(live['profiles'], m.load('created-profile.json')['uuid'])
    n = one(live['nodes'], m.load('created-node.json')['uuid'])
    m.check(p, m.load('intent-profile.json'))
    m.check(n, m.load('intent-node.json'))
    inbound = {r['tag']: r['uuid'] for r in p['inbounds']}
    m.require(inbound == m.load('ids.json')['inbounds'], 'Owned inbound identity changed')
    m.require([r['uuid'] for r in live['nodes'] if (r.get('configProfile') or {}).get('activeConfigProfileUuid') == p['uuid']]
              == [n['uuid']], 'Target profile is shared')
    hosts = {}
    for kind in KINDS:
        h = one(live['hosts'], m.load('created-' + kind + '.json')['uuid'])
        m.require(type(h.get('isDisabled')) is bool, 'Unknown host status')
        if not h['isDisabled']:
            m.require((m.STATE / ('enable-' + kind + '.json')).exists()
                      and m.load('enable-' + kind + '.json') == {'uuid': h['uuid'], 'isDisabled': False},
                      'Host enabled without owned intent')
        m.check(h, {**m.load('intent-' + kind + '.json'), 'isDisabled': h['isDisabled']})
        hosts[kind] = h
    m.require(source_scope(live) == data['scope'], 'Reference audience changed; stop')
    allowed = {kind: set(data['scope'][kind]['squads']) for kind in KINDS}
    for s in live['squads']:
        for kind in KINDS:
            if inbound[tag(kind)] in ids(s['inbounds']):
                m.require(s['uuid'] in allowed[kind], 'Target inbound exposed outside source audience')
    if ready:
        m.require(n['isConnected'] is True and n['isDisabled'] is False and not n.get('lastStatusMessage'),
                  'Target node not ready')
    return live, n, inbound, hosts, allowed


def bind_squads():
    _, _, inbound, _, allowed = audit(ready=True)
    for uid in sorted(set().union(*allowed.values())):
        fresh = one(m.api('GET', '/internal-squads')['internalSquads'], uid)
        current = ids(fresh['inbounds'])
        data = plan()
        for kind in KINDS:
            m.require((data['scope'][kind]['sourceInbound'] in current) == (uid in allowed[kind]),
                      'Reference membership changed during binding')
        intended = [inbound[tag(kind)] for kind in KINDS if uid in allowed[kind]]
        add = [iid for iid in intended if iid not in current]
        if add:
            payload = {'uuid': uid, 'inbounds': current + add}
            m.save('squad-intent-' + uid + '.json', payload)
            actual = m.api('PATCH', '/internal-squads', payload)
            m.require(actual['uuid'] == uid and set(ids(actual['inbounds'])) == set(payload['inbounds']),
                      'Additive membership did not persist')
    verified, _, bound, _, _ = audit(ready=True)
    check_bindings(verified, bound, allowed)
    print('Mirrored each transport audience independently; host activation is separate')


def check_bindings(live, inbound, allowed):
    for kind in KINDS:
        m.require({s['uuid'] for s in live['squads'] if inbound[tag(kind)] in ids(s['inbounds'])} == allowed[kind],
                  'Complete audience binding is required first')


def activate(verified=False):
    m.require(verified, 'Run end-to-end checks before activate --verified')
    live, _, inbound, hosts, allowed = audit(ready=True)
    check_bindings(live, inbound, allowed)
    for kind, h in hosts.items():
        if h['isDisabled']:
            payload = {'uuid': h['uuid'], 'isDisabled': False}
            m.save('enable-' + kind + '.json', payload)
            actual = m.api('PATCH', '/hosts', payload)
            m.require(actual['uuid'] == h['uuid'] and actual['isDisabled'] is False, 'Host not enabled')
    _, _, _, checked, _ = audit(ready=True)
    m.require(all(h['isDisabled'] is False for h in checked.values()), 'Enable readback failed')
    for h in checked.values():
        print('Enabled:', h['remark'], h['uuid'])


def status():
    _, n, _, hosts, _ = audit()
    print(json.dumps({'node': {k: n.get(k) for k in ('uuid', 'name', 'address', 'isConnected', 'isDisabled', 'versions')},
                      'hosts': [{k: h.get(k) for k in ('uuid', 'remark', 'address', 'port', 'isDisabled')}
                                for h in hosts.values()]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('prepare', 'provision', 'bind-squads', 'activate', 'status'))
    parser.add_argument('--verified', action='store_true')
    parser.add_argument('--panel-tunnel', action='store_true')
    args = parser.parse_args()
    if not args.action:
        print(json.dumps({'planOnly': True, 'address': ADDRESS, 'domain': DOMAIN, 'profile': PROFILE,
                          'node': NODE, 'ports': PORTS, 'paths': PATHS, 'publicPort': 443,
                          'privateState': str(m.STATE), 'panelTunnelPort': PANEL_TUNNEL_PORT}))
        return
    if args.panel_tunnel:
        spec = importlib.util.spec_from_file_location('fixed_panel_tunnel', OPS / 'direct-node198/panel_tunnel.py')
        tunnel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tunnel)
        tunnel.PORT = PANEL_TUNNEL_PORT
        m.api = tunnel.adapter(m.ROOT)
    actions = {'prepare': prepare, 'provision': provision, 'bind-squads': bind_squads,
               'activate': lambda: activate(args.verified), 'status': status}
    actions[args.action]()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never print raw API responses or exceptions containing infrastructure.
        if type(error) is ValueError and isinstance(error.args[0] if error.args else None, str):
            print('Node18253 stopped:', str(error), file=sys.stderr)
        else:
            print('Node18253 stopped:', type(error).__name__, '; private details suppressed', file=sys.stderr)
        raise SystemExit(2) from None
