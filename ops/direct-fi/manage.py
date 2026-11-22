"""Scoped direct gRPC/WS deployment for the explicitly approved Finland VPS.

Never prints credentials, subscriber records or HTTP response bodies. Private
state is exclusive/0600; mutations are separate explicit commands, not retries.

The default is a pure no-I/O plan. fi103.wayspn.com is a provisional operation
name and must be confirmed before invoking any network action.
This helper owns exactly two direct inbounds. Finish and audit it before adding
XHTTP with a separate helper; the third inbound intentionally fails this audit.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import certifi
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
STATE = Path('/private/tmp/wayspn-direct-fi-20261030')
ADDRESS = '92.42.102.103'
DOMAIN = 'fi103.wayspn.com'
PROFILE = 'DIRECT_FI_102_103'
NODE = 'node-direct-fi-102.103'
SOURCE_HOSTS = ('52edd62d-7e98-4b04-8cb4-67086a3660ea', 'cd19762c-685e-4d91-8124-2ddfa33ee59a')


def require(value, message):
    if not value:
        raise ValueError(message)


def private_dir():
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    require(not STATE.is_symlink() and STATE.stat().st_mode & 0o777 == 0o700, 'Private state directory required')


def save(name, value):
    private_dir()
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    path = STATE / name
    if path.exists():
        require(not path.is_symlink() and path.stat().st_mode & 0o777 == 0o600 and path.read_text() == text,
                'Refusing changed existing state: ' + name)
        return
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    directory_fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def load(name):
    path = STATE / name
    require(not path.is_symlink() and path.stat().st_mode & 0o777 == 0o600, 'Unsafe state file')
    return json.loads(path.read_text())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('Panel redirect refused')


def api(method, path, body=None):
    require(path in {'/hosts', '/nodes', '/config-profiles', '/internal-squads', '/keygen'}, 'API path outside infrastructure scope')
    # Only the two relevant settings are parsed; no main settings are imported.
    lines = [line for line in (ROOT / '.env').read_text().splitlines()
             if line.split('=', 1)[0].strip() in {'REMNAWAVE_BASE_URL', 'REMNAWAVE_API_TOKEN'}]
    values = dotenv_values(stream=io.StringIO('\n'.join(lines)), interpolate=False)
    base, token = values['REMNAWAVE_BASE_URL'].rstrip('/'), values['REMNAWAVE_API_TOKEN']
    parsed = urlsplit(base)
    require(parsed.scheme == 'https' and parsed.hostname in {'panel.wayspn.online', 'panel.chislo.site'}
            and parsed.path == '/api' and parsed.port in {None, 443} and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment, 'Unexpected panel origin')
    require(isinstance(token, str) and token and not any(c.isspace() for c in token), 'Missing panel credentials')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(',', ':')).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    try:
        with opener.open(req, timeout=25) as response:
            raw = response.read(8_000_001)
            require(len(raw) <= 8_000_000, 'Oversized panel response')
            return json.loads(raw)['response']
    except urllib.error.HTTPError as error:
        raise RuntimeError('Panel HTTP ' + str(error.code) + '; response suppressed') from None


def snapshot():
    return {'hosts': api('GET', '/hosts'), 'profiles': api('GET', '/config-profiles')['configProfiles'],
            'nodes': api('GET', '/nodes'), 'squads': api('GET', '/internal-squads')['internalSquads']}


def sources(live):
    hosts = [next(h for h in live['hosts'] if h['uuid'] == uid) for uid in SOURCE_HOSTS]
    profile_ids = {h['inbound']['configProfileUuid'] for h in hosts}
    require(len(profile_ids) == 1, 'Source hosts do not share a profile')
    profile = next(p for p in live['profiles'] if p['uuid'] in profile_ids)
    inbound_ids = {h['inbound']['configProfileInboundUuid'] for h in hosts}
    tags = {i['tag'] for i in profile['inbounds'] if i['uuid'] in inbound_ids}
    inbounds = [i for i in profile['config']['inbounds'] if i['tag'] in tags]
    require(len(inbounds) == 2 and {i['streamSettings']['network'] for i in inbounds} == {'grpc', 'ws'}, 'Expected exact gRPC/WS source pair')
    require(all(i['settings']['clients'] == [] for i in inbounds), 'Source profile unexpectedly contains clients')
    return hosts, profile, inbounds


def inspect_live():
    live = snapshot()
    hosts, profile, inbounds = sources(live)
    save('baseline.json', live)
    save('source-profile.json', profile)
    save('source-hosts.json', hosts)
    # Private configs stay private; report only transport/association metadata.
    safe_hosts = [{k: h.get(k) for k in ('uuid', 'remark', 'address', 'port', 'path', 'sni', 'host', 'alpn', 'fingerprint', 'securityLayer', 'isDisabled', 'nodes', 'internalSquads')} for h in hosts]
    safe_inbounds = [{'tag': i['tag'], 'listen': i.get('listen'), 'port': i['port'], 'protocol': i['protocol'], 'network': i['streamSettings']['network'], 'security': i['streamSettings'].get('security'), 'grpcSettings': i['streamSettings'].get('grpcSettings'), 'wsSettings': i['streamSettings'].get('wsSettings')} for i in inbounds]
    safe_nodes = [{k: n.get(k) for k in ('uuid', 'name', 'address', 'port', 'isConnected', 'versions')} for n in live['nodes'] if n['uuid'] in {uid for h in hosts for uid in h.get('nodes', [])} or n.get('address') == ADDRESS]
    ids = {h['inbound']['configProfileInboundUuid'] for h in hosts}
    safe_squads = [{'uuid': s['uuid'], 'name': s['name'], 'sourceInboundIds': [i['uuid'] for i in s['inbounds'] if i['uuid'] in ids]} for s in live['squads'] if ids.intersection(i['uuid'] for i in s['inbounds'])]
    print(json.dumps({'profile': profile['name'], 'hosts': safe_hosts, 'inbounds': safe_inbounds, 'nodes': safe_nodes, 'squads': safe_squads}, ensure_ascii=False))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def signatures(live):
    result = {}
    for kind, rows in live.items():
        result[kind] = {}
        for row in rows:
            if kind == 'profiles':
                value = {k: row[k] for k in ('name', 'config', 'inbounds')}
            elif kind == 'nodes':
                value = {k: row.get(k) for k in ('name', 'address', 'port', 'configProfile')}
            elif kind == 'squads':
                value = {'name': row['name'], 'inbounds': sorted(i['uuid'] for i in row['inbounds'])}
            else:
                # Panel insertion/reordering changes these metadata fields on
                # existing hosts. Every other existing host field stays locked.
                value = {k: v for k, v in row.items() if k not in {'updatedAt', 'viewPosition'}}
            result[kind][row['uuid']] = digest(value)
    return result


def prepare():
    baseline = load('baseline.json')
    hosts, source, selected = sources(baseline)
    require(not any(p['name'] == PROFILE for p in baseline['profiles']), 'Target profile already exists')
    require(not any(n['address'] == ADDRESS or n['name'] == NODE for n in baseline['nodes']), 'Target node already exists')
    require(not any(h['address'] in {ADDRESS, DOMAIN} for h in baseline['hosts']), 'Target host already exists')
    if (STATE / 'plan.json').exists():
        print('Existing private plan preserved; no new keys generated.')
        return
    config = copy.deepcopy(source['config'])
    require(all(o.get('protocol') in {'freedom', 'blackhole', 'dns'} for o in config.get('outbounds', [])), 'Source has non-direct egress; explicit review needed')
    key = X25519PrivateKey.generate()
    b64 = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    private_key = b64(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    public_key = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    short_id = secrets.token_hex(8)
    mapping, inbounds = {}, []
    for old in selected:
        inbound = copy.deepcopy(old)
        stream = inbound['streamSettings']
        network = stream['network']
        tag = 'ordinary-grpc-reality-fi' if network == 'grpc' else 'ordinary-ws-fi'
        inbound['tag'] = tag
        mapping[old['tag']] = tag
        if network == 'grpc':
            require(inbound['port'] == 2083 and stream['security'] == 'reality' and stream['grpcSettings']['serviceName'] == 'grpc', 'Unexpected source gRPC')
            stream['realitySettings'].update(privateKey=private_key, shortIds=[short_id], serverNames=[DOMAIN], target='127.0.0.1:443')
        else:
            require(inbound['port'] == 10088 and inbound['listen'] == '127.0.0.1' and stream['security'] == 'none'
                    and stream['wsSettings']['path'] == '/direct/ws/', 'Unexpected source WS')
        inbounds.append(inbound)
    config['inbounds'] = inbounds
    kept_rules = []
    for rule in config.get('routing', {}).get('rules', []):
        rule = copy.deepcopy(rule)
        if rule.get('inboundTag'):
            rule['inboundTag'] = [mapping[tag] for tag in rule['inboundTag'] if tag in mapping]
            if not rule['inboundTag']:
                continue
        kept_rules.append(rule)
    if 'routing' in config:
        config['routing']['rules'] = kept_rules
    save('profile.create.json', {'name': PROFILE, 'config': config})
    save('public.json', {'domain': DOMAIN, 'address': ADDRESS, 'panelIp': '144.31.170.147', 'publicKey': public_key, 'shortId': short_id})
    secret = api('GET', '/keygen')['secretKey']
    require(isinstance(secret, str) and len(secret) > 20 and not any(c.isspace() for c in secret), 'Invalid node service key')
    save('node.env', 'NODE_PORT=2222\nSECRET_KEY=' + secret + '\n')
    inbound_by_id = {i['uuid']: i for i in source['inbounds']}
    plan_hosts = []
    fields = {'address', 'port', 'path', 'sni', 'host', 'alpn', 'fingerprint', 'securityLayer', 'xhttpExtraParams', 'muxParams', 'sockoptParams',
              'finalMask', 'serverDescription', 'pinnedPeerCertSha256', 'verifyPeerCertByName', 'shuffleHost', 'mihomoX25519', 'mihomoIpVersion',
              'isHidden', 'overrideSniFromAddress', 'keepSniBlank', 'vlessRouteId', 'xrayJsonTemplateUuid', 'excludeFromSubscriptionTypes', 'mapper'}
    for old in hosts:
        source_tag = inbound_by_id[old['inbound']['configProfileInboundUuid']]['tag']
        kind = 'gRPC' if mapping[source_tag] == 'ordinary-grpc-reality-fi' else 'WS'
        wanted = {k: copy.deepcopy(v) for k, v in old.items() if k in fields}
        wanted.update(remark=f'🇫🇮 Финляндия • {kind}', address=ADDRESS if kind == 'gRPC' else DOMAIN, sni=DOMAIN,
                      host=None if kind == 'gRPC' else DOMAIN, isDisabled=True, tags=['FI_102_103', 'DIRECT'],
                      internalSquads=copy.deepcopy(old['internalSquads']))
        plan_hosts.append({'tag': mapping[source_tag], 'sourceInbound': old['inbound']['configProfileInboundUuid'], 'body': wanted})
    save('hosts.plan.json', plan_hosts)
    save('signatures.before.json', signatures(baseline))
    save('plan.json', {'format': 1, 'address': ADDRESS, 'domain': DOMAIN, 'profile': PROFILE, 'node': NODE,
                       'profileHash': digest(config), 'sourceHostIds': list(SOURCE_HOSTS)})
    print('Prepared exactly two direct inbounds; private keys preserved; no panel writes.')


def check(actual, expected):
    for key, value in expected.items():
        current = actual.get(key)
        if key == 'configProfile':
            require(current and current['activeConfigProfileUuid'] == value['activeConfigProfileUuid']
                    and sorted(i['uuid'] if isinstance(i, dict) else i for i in current['activeInbounds']) == sorted(value['activeInbounds']), 'Node binding differs')
        elif key == 'inbound':
            require(current and all(current.get(k) == v for k, v in value.items()), 'Host inbound differs')
        else:
            require(current == value, 'Owned object differs: ' + key)


def obtain(kind, key, predicate, payload):
    # Persist an intent before a mutation. If the response is lost, the next run
    # first rereads the uniquely identified object, never blindly repeats POST.
    path = {'profile': '/config-profiles', 'node': '/nodes', 'grpc': '/hosts', 'ws': '/hosts'}[kind]
    def reread():
        result = api('GET', path)
        if kind == 'profile': result = result['configProfiles']
        matches = [row for row in result if predicate(row)]
        require(len(matches) <= 1, 'Ambiguous target object: ' + kind)
        return matches[0] if matches else None
    current = reread()
    intent = STATE / ('intent-' + key + '.json')
    if current is not None:
        require(intent.exists(), 'Target already exists without our durable intent')
        require(load(intent.name) == payload, 'Saved creation intent differs')
        check(current, payload)
    else:
        if intent.exists():
            raise RuntimeError('A prior create may have timed out; target absent; inspect before retry: ' + kind)
        save(intent.name, payload)
        current = api('POST', path, payload)
        check(current, payload)
    record = 'created-' + key + '.json'
    if (STATE / record).exists():
        require(load(record)['uuid'] == current['uuid'], 'Created object UUID changed')
    else:
        save(record, current)
    return current


def create():
    plan = load('plan.json')
    require(plan == {'format': 1, 'address': ADDRESS, 'domain': DOMAIN, 'profile': PROFILE, 'node': NODE,
                     'profileHash': digest(load('profile.create.json')['config']), 'sourceHostIds': list(SOURCE_HOSTS)},
            'Wrong Finland plan')
    profile = obtain('profile', 'profile', lambda p: p['name'] == PROFILE, load('profile.create.json'))
    require(len(profile['inbounds']) == 2
            and {i['tag'] for i in profile['inbounds']} == {'ordinary-grpc-reality-fi', 'ordinary-ws-fi'},
            'Unexpected created inbounds')
    inbound_ids = {i['tag']: i['uuid'] for i in profile['inbounds']}
    node = obtain('node', 'node', lambda n: n['address'] == ADDRESS or n['name'] == NODE,
                  {'name': NODE, 'address': ADDRESS, 'port': 2222, 'countryCode': 'FI', 'tags': ['FI_102_103', 'DIRECT'],
                   'note': 'fi103.wayspn.com: gRPC REALITY + WS TLS; dedicated Finland node; XHTTP CDN added separately.',
                   'configProfile': {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': list(inbound_ids.values())}})
    for item in load('hosts.plan.json'):
        payload = copy.deepcopy(item['body'])
        payload.update(nodes=[node['uuid']], inbound={'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbound_ids[item['tag']]})
        kind = 'grpc' if 'grpc' in item['tag'] else 'ws'
        h = obtain(kind, kind, lambda h: h.get('inbound', {}).get('configProfileInboundUuid') == inbound_ids[item['tag']], payload)
        print('Created disabled:', h['remark'], h['uuid'])
    print('Separate profile/node/two disabled hosts saved. Existing infrastructure untouched.')


def status():
    live = snapshot()
    profile = next(p for p in live['profiles'] if p['uuid'] == load('created-profile.json')['uuid'])
    node = next(n for n in live['nodes'] if n['uuid'] == load('created-node.json')['uuid'])
    require(profile['config'] == load('profile.create.json')['config'], 'Profile changed')
    print(json.dumps({'node': {k: node.get(k) for k in ('uuid', 'name', 'address', 'isConnected', 'isConnecting', 'isDisabled', 'lastStatusMessage', 'versions', 'usersOnline')},
                      'hosts': [{k: h.get(k) for k in ('uuid', 'remark', 'address', 'port', 'isDisabled')} for h in live['hosts'] if h.get('inbound', {}).get('configProfileUuid') == profile['uuid']]}, ensure_ascii=False))


def owned(live, *, ready=False):
    p = next(p for p in live['profiles'] if p['uuid'] == load('created-profile.json')['uuid'])
    n = next(n for n in live['nodes'] if n['uuid'] == load('created-node.json')['uuid'])
    check(p, load('intent-profile.json'))
    check(n, load('intent-node.json'))
    require(len(p['inbounds']) == 2 and {i['tag'] for i in p['inbounds']} == {'ordinary-grpc-reality-fi', 'ordinary-ws-fi'}, 'Unexpected owned inbounds')
    require(sorted((i['uuid'], i['tag']) for i in p['inbounds']) == sorted((i['uuid'], i['tag']) for i in load('created-profile.json')['inbounds']), 'Owned inbound identities changed')
    for kind in ('grpc', 'ws'):
        h = next(h for h in live['hosts'] if h['uuid'] == load('created-' + kind + '.json')['uuid'])
        wanted = load('intent-' + kind + '.json')
        require(type(h.get('isDisabled')) is bool, 'Invalid owned host status')
        if h['isDisabled'] is False:
            require((STATE / ('enable-' + kind + '.json')).exists()
                    and load('enable-' + kind + '.json') == {'uuid': h['uuid'], 'isDisabled': False}, 'Host enabled without an exact durable intent')
        check(h, {**wanted, 'isDisabled': h['isDisabled']})
    if ready:
        require(n['isConnected'] is True and n['isDisabled'] is False and not n.get('lastStatusMessage'), 'New node not ready')
    return p, n


def expected_squad(squad, profile):
    mapping = {item['sourceInbound']: next(i['uuid'] for i in profile['inbounds'] if i['tag'] == item['tag']) for item in load('hosts.plan.json')}
    old = [i['uuid'] for i in squad['inbounds']]
    return old + [mapping[uid] for uid in old if uid in mapping and mapping[uid] not in old]


def audit_live(live=None):
    live = live or snapshot()
    profile, node = owned(live)
    before, now = load('baseline.json'), signatures(live)
    initial = load('signatures.before.json')
    additions = {'profiles': {profile['uuid']}, 'nodes': {node['uuid']},
                 'hosts': {load('created-grpc.json')['uuid'], load('created-ws.json')['uuid']}, 'squads': set()}
    for kind in ('profiles', 'nodes', 'hosts', 'squads'):
        require(len({row['uuid'] for row in live[kind]}) == len(live[kind]), 'Duplicate infrastructure UUIDs')
        require(set(now[kind]) == set(initial[kind]) | additions[kind], 'Unexpected added or removed infrastructure objects: ' + kind)
    for kind in ('profiles', 'nodes', 'hosts'):
        for uid, wanted in initial[kind].items():
            require(now[kind].get(uid) == wanted, 'Existing ' + kind + ' changed; review before continuing')
    current = {s['uuid']: s for s in live['squads']}
    for old in before['squads']:
        new = current[old['uuid']]
        require(new['name'] == old['name'], 'Existing squad renamed')
        old_ids, new_ids = sorted(i['uuid'] for i in old['inbounds']), sorted(i['uuid'] for i in new['inbounds'])
        allowed = sorted(expected_squad(old, profile))
        if new_ids != old_ids:
            name = 'squad-intent-' + old['uuid'] + '.json'
            require(new_ids == allowed and (STATE / name).exists(), 'Unexpected squad membership change')
            require(load(name) == {'uuid': old['uuid'], 'inbounds': expected_squad(old, profile)}, 'Squad change differs from exact durable intent')
    return live, profile, node


def bind_squads(*, only_test=False):
    live, profile, node = audit_live()
    owned(live, ready=True)
    current = {s['uuid']: s for s in live['squads']}
    for old in load('baseline.json')['squads']:
        if only_test and old['name'] != 'DE':
            continue
        wanted = expected_squad(old, profile)
        if len(wanted) == len(old['inbounds']):
            continue
        s = current[old['uuid']]
        payload = {'uuid': s['uuid'], 'inbounds': wanted}
        if sorted(i['uuid'] for i in s['inbounds']) != sorted(wanted):
            save('squad-intent-' + s['uuid'] + '.json', payload)
            s = api('PATCH', '/internal-squads', payload)
            require(s['uuid'] == payload['uuid'] and sorted(i['uuid'] for i in s['inbounds']) == sorted(wanted), 'Squad update differs')
        print('Bound new inbounds:', s['name'])
    audit_live()


def activate():
    bind_squads()
    live, profile, node = audit_live()
    owned(live, ready=True)
    for kind in ('grpc', 'ws'):
        old = load('created-' + kind + '.json')
        h = next(h for h in live['hosts'] if h['uuid'] == old['uuid'])
        if h['isDisabled']:
            save('enable-' + kind + '.json', {'uuid': h['uuid'], 'isDisabled': False})
            h = api('PATCH', '/hosts', {'uuid': h['uuid'], 'isDisabled': False})
            require(h['uuid'] == old['uuid'] and h['isDisabled'] is False, 'Host not enabled')
        print('Enabled:', h['remark'], h['uuid'])
    final, _, _ = audit_live()
    require(all(next(h for h in final['hosts'] if h['uuid'] == load('created-' + kind + '.json')['uuid'])['isDisabled'] is False for kind in ('grpc', 'ws')), 'Enabled host state did not persist')
    require(all(sorted(i['uuid'] for i in next(s for s in final['squads'] if s['uuid'] == old['uuid'])['inbounds']) == sorted(expected_squad(old, profile)) for old in load('baseline.json')['squads']), 'Mirrored access did not persist')
    print('Enabled only the new hosts; existing objects unchanged; source squad access mirrored.')


def audit():
    live, profile, node = audit_live()
    print(json.dumps({'existingInfrastructureUnchanged': True, 'newProfile': profile['uuid'], 'newNode': node['uuid'],
                      'newInbounds': len(profile['inbounds']), 'sourceScopeMirrored': all(sorted(i['uuid'] for i in next(s for s in live['squads'] if s['uuid'] == old['uuid'])['inbounds']) == sorted(expected_squad(old, profile)) for old in load('baseline.json')['squads'])}))


def acknowledge_owner_enabled(*, confirmed=False):
    require(confirmed is True, 'Explicit owner confirmation required')
    live = snapshot()
    # The owner explicitly reported enabling these exact new hosts. Preserve
    # that change; validate every other field before accepting it into our audit.
    for kind in ('grpc', 'ws'):
        uid = load('created-' + kind + '.json')['uuid']
        matches = [h for h in live['hosts'] if h['uuid'] == uid]
        require(len(matches) == 1, 'Owned host missing or ambiguous')
        check(matches[0], {**load('intent-' + kind + '.json'), 'isDisabled': False})
        save('enable-' + kind + '.json', {'uuid': uid, 'isDisabled': False})
    save('owner-enabled-confirmation.json', {'date': '2026-10-30', 'source': 'explicit user message',
                                           'hosts': [load('created-' + kind + '.json')['uuid'] for kind in ('grpc', 'ws')]})
    audit_live(live)
    print('Owner-enabled host state acknowledged; no panel mutation performed.')


def plan_only():
    return {'plan_only': True, 'network': False, 'filesystem_writes': False,
            'address': ADDRESS, 'domain': DOMAIN, 'profile': PROFILE, 'node': NODE,
            'countryCode': 'FI', 'new_inbounds': ['grpc', 'ws'], 'hosts_initially_disabled': True,
            'existing_node_reuse': False, 'cdn_stage': 'separate, after direct activation and audit',
            'actions': ['inspect', 'prepare', 'create', 'status', 'bind-test', 'activate', 'audit']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['inspect', 'prepare', 'create', 'status', 'bind-test', 'activate', 'audit', 'ack-owner-enabled'])
    parser.add_argument('--user-confirmed', action='store_true')
    args = parser.parse_args()
    if args.action is None:
        print(json.dumps(plan_only(), ensure_ascii=False, sort_keys=True))
        return
    {'inspect': inspect_live, 'prepare': prepare, 'create': create, 'status': status,
     'bind-test': lambda: bind_squads(only_test=True), 'activate': activate, 'audit': audit,
     'ack-owner-enabled': lambda: acknowledge_owner_enabled(confirmed=args.user_confirmed)}[args.action]()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__, 'message': str(error) if isinstance(error, (ValueError, RuntimeError)) else 'Infrastructure operation failed; details suppressed'}))
        sys.exit(1)
