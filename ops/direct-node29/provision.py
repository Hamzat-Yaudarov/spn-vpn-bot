"""Provision a separate direct-only node; never use subscriber credentials."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import time
import urllib.error

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.prepare_direct_transports import api as panel_api

STATE = Path('/private/tmp/wayspn-direct-node29-20261008')
DOMAIN = 'node29.prestizh.site'
ADDRESS = '64.188.78.29'
PANEL = '144.31.170.147'
PROFILE = 'DIRECT_PRESTIZH_NODE29'
SOURCE = 'VK_CDN_PRESTIZH'
TEST_SQUAD = 'DE'


def api(method, path, body=None):
    # GETs can be retried safely. A mutation with a lost response must instead
    # reread current state, never blindly repeat POSTs that could duplicate data.
    attempts = 3 if method == 'GET' else 1
    for attempt in range(attempts):
        try:
            return panel_api(method, path, body)
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            time.sleep(3)


def write(name, value):
    path = STATE / name
    contents = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if path.exists():
        if path.read_text() != contents:
            raise RuntimeError('Refusing overwrite: ' + name)
        return
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        f.write(contents)


def load(name):
    return json.loads((STATE / name).read_text())


def prepare():
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    if (STATE / 'ready.json').exists():
        print('Artifacts already prepared; keys preserved.')
        return
    if any(STATE.iterdir()):
        raise RuntimeError('Incomplete staging exists; inspect before continuing.')
    profiles = api('GET', '/config-profiles')['configProfiles']
    assert not any(p['name'] == PROFILE for p in profiles)
    assert not any(n['address'] == ADDRESS for n in api('GET', '/nodes'))
    source = next(p for p in profiles if p['name'] == SOURCE)
    config = copy.deepcopy(source['config'])
    config['inbounds'] = [i for i in config['inbounds'] if i['streamSettings']['network'] in ('grpc', 'ws', 'hysteria')]
    assert len(config['inbounds']) == 3
    assert all(i['settings']['clients'] == [] for i in config['inbounds'])
    key = X25519PrivateKey.generate()
    b64 = lambda v: base64.urlsafe_b64encode(v).decode().rstrip('=')
    private_key = b64(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    public_key = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    short_id = secrets.token_hex(8)
    for inbound in config['inbounds']:
        inbound['tag'] = inbound['tag'].replace('-prestizh', '-node29')
        stream = inbound['streamSettings']
        if stream['network'] == 'grpc':
            stream['realitySettings'].update(privateKey=private_key, shortIds=[short_id], serverNames=[DOMAIN], target='127.0.0.1:443')
        elif stream['network'] == 'hysteria':
            stream['tlsSettings']['certificates'] = [{
                'keyFile': f'/etc/letsencrypt/live/{DOMAIN}/privkey.pem',
                'certificateFile': f'/etc/letsencrypt/live/{DOMAIN}/fullchain.pem',
            }]
    write('profile.json', config)
    secret = api('GET', '/keygen')['secretKey']
    assert isinstance(secret, str) and len(secret) > 20 and not any(c.isspace() for c in secret)
    write('node.env', 'NODE_PORT=2222\nSECRET_KEY=' + secret + '\n')
    write('public.json', dict(domain=DOMAIN, address=ADDRESS, panelIp=PANEL,
                              profileName=PROFILE, publicKey=public_key, shortId=short_id))
    for name in ('install.sh', 'finish.sh', 'renew-services.sh', 'docker-compose.yml', 'bootstrap.conf', 'nginx.conf', 'index.html'):
        shutil.copyfile(Path(__file__).parent / name, STATE / name)
        os.chmod(STATE / name, 0o600)
    write('ready.json', {'ready': True, 'source': SOURCE})
    print('Prepared three direct inbounds and private deployment files; no panel writes.')


def target_profile():
    matches = [p for p in api('GET', '/config-profiles')['configProfiles'] if p['name'] == PROFILE]
    assert len(matches) == 1
    p = matches[0]
    assert p['config'] == load('profile.json'), 'Target profile modified; refusing overwrite'
    assert len(p['inbounds']) == 3
    return p


def target_node(profile):
    matches = [n for n in api('GET', '/nodes') if n['address'] == ADDRESS]
    assert len(matches) == 1
    n = matches[0]
    assert n['port'] == 2222 and n['configProfile']['activeConfigProfileUuid'] == profile['uuid']
    assert {i['uuid'] for i in n['configProfile']['activeInbounds']} == {i['uuid'] for i in profile['inbounds']}
    return n


def wanted_hosts(profile, node):
    source = load('source-profile.json')
    source_tags = {i['uuid']: i['tag'] for i in source['inbounds']}
    target_ids = {i['tag']: i['uuid'] for i in profile['inbounds']}
    allowed = {'address', 'port', 'path', 'sni', 'host', 'alpn', 'fingerprint', 'securityLayer',
               'xhttpExtraParams', 'muxParams', 'sockoptParams', 'finalMask', 'serverDescription',
               'pinnedPeerCertSha256', 'verifyPeerCertByName', 'shuffleHost', 'mihomoX25519',
               'mihomoIpVersion', 'isHidden', 'overrideSniFromAddress', 'keepSniBlank',
               'vlessRouteId', 'xrayJsonTemplateUuid', 'excludeFromSubscriptionTypes', 'mapper'}
    result = []
    for old in load('source-hosts.json'):
        tag = source_tags[old['inbound']['configProfileInboundUuid']].replace('-prestizh', '-node29')
        if tag not in target_ids:
            continue
        h = {k: copy.deepcopy(v) for k, v in old.items() if k in allowed}
        kind = 'gRPC' if 'grpc' in tag else 'Hysteria2' if 'hy2' in tag else 'WS'
        h.update(remark=f'🇪🇪 Эстония • {kind} • 29', address=DOMAIN if kind == 'WS' else ADDRESS,
                 sni=DOMAIN, host=DOMAIN if kind == 'WS' else None,
                 nodes=[node['uuid']], tags=['NODE29', 'DIRECT'], isDisabled=True,
                 inbound={'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': target_ids[tag]},
                 internalSquads={'mode': 'EXCLUDE', 'squads': []})
        result.append(h)
    assert len(result) == 3 and len({h['inbound']['configProfileInboundUuid'] for h in result}) == 3
    return result


def check_host(actual, wanted):
    for k in ('remark', 'address', 'port', 'sni', 'host', 'path', 'alpn', 'fingerprint', 'securityLayer', 'inbound', 'nodes'):
        assert actual.get(k) == wanted.get(k), 'Host differs: ' + k


def create():
    profiles = api('GET', '/config-profiles')['configProfiles']
    nodes = api('GET', '/nodes')
    hosts = api('GET', '/hosts')
    squads = api('GET', '/internal-squads')['internalSquads']
    assert len([s for s in squads if s['name'] == TEST_SQUAD]) == 1
    source = next(p for p in profiles if p['name'] == SOURCE)
    source_hosts = [h for h in hosts if h.get('inbound', {}).get('configProfileUuid') == source['uuid']]
    assert len(source_hosts) == 4
    if not (STATE / 'panel-profiles-before.json').exists():
        write('panel-profiles-before.json', profiles)
        write('panel-hosts-before.json', hosts)
        write('panel-squads-before.json', squads)
        write('existing-node-bindings.json', {n['uuid']: {k:n[k] for k in ('address', 'configProfile')} for n in nodes})
        write('source-profile.json', source)
        write('source-hosts.json', source_hosts)
    matches = [p for p in profiles if p['name'] == PROFILE]
    assert len(matches) <= 1
    if matches:
        profile = target_profile()
    else:
        assert not any(n['address'] == ADDRESS for n in nodes)
        profile = api('POST', '/config-profiles', {'name': PROFILE, 'config': load('profile.json')})
    if not (STATE / 'created-profile.json').exists():
        write('created-profile.json', profile)
    if any(n['address'] == ADDRESS for n in nodes):
        node = target_node(profile)
    else:
        node = api('POST', '/nodes', {
            'name': 'node-direct-78.29', 'address': ADDRESS, 'port': 2222,
            'countryCode': 'EE', 'tags': ['NODE29', 'DIRECT'],
            'note': f'{DOMAIN}: direct gRPC REALITY, WS TLS, Hysteria2. No CDN. Initial access DE only.',
            'configProfile': {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': [i['uuid'] for i in profile['inbounds']]},
        })
    if not (STATE / 'created-node.json').exists():
        write('created-node.json', node)
    existing = api('GET', '/hosts')
    for wanted in wanted_hosts(profile, node):
        matches = [h for h in existing if h.get('inbound', {}).get('configProfileInboundUuid') == wanted['inbound']['configProfileInboundUuid']]
        assert len(matches) <= 1
        if matches:
            check_host(matches[0], wanted)
        else:
            h = api('POST', '/hosts', wanted)
            check_host(h, wanted)
            write('host-' + h['uuid'] + '.json', h)
            print('Created disabled:', h['remark'])
    print('Created separate profile, node and three disabled hosts. Existing objects untouched.')


def status():
    p = target_profile()
    n = target_node(p)
    print(json.dumps({k:n.get(k) for k in ('name', 'address', 'isConnected', 'isConnecting', 'isDisabled', 'lastStatusMessage', 'versions', 'xrayUptime', 'usersOnline')}, ensure_ascii=False))
    for h in api('GET', '/hosts'):
        if h.get('inbound', {}).get('configProfileUuid') == p['uuid']:
            print(h['remark'], h['address'], h['port'], 'disabled:', h['isDisabled'])


def activate_test():
    p = target_profile()
    n = target_node(p)
    assert n['isConnected'] and not n['isDisabled'] and not n.get('lastStatusMessage'), 'Node not ready'
    new_ids = {i['uuid'] for i in p['inbounds']}
    squads = api('GET', '/internal-squads')['internalSquads']
    for s in squads:
        if s['name'] != TEST_SQUAD:
            assert not new_ids.intersection(i['uuid'] for i in s['inbounds'])
    squad = next(s for s in squads if s['name'] == TEST_SQUAD)
    current = [i['uuid'] for i in squad['inbounds']]
    additions = [i['uuid'] for i in p['inbounds'] if i['uuid'] not in current]
    if additions:
        write('test-squad-before.json', squad)
        s = api('PATCH', '/internal-squads', {'uuid': squad['uuid'], 'inbounds': current + additions})
        assert {i['uuid'] for i in s['inbounds']} == set(current + additions)
    wanted = {h['inbound']['configProfileInboundUuid']:h for h in wanted_hosts(p, n)}
    for h in api('GET', '/hosts'):
        if h.get('inbound', {}).get('configProfileUuid') == p['uuid']:
            check_host(h, wanted[h['inbound']['configProfileInboundUuid']])
            if h['isDisabled']:
                api('PATCH', '/hosts', {'uuid': h['uuid'], 'isDisabled': False})
    print('Three hosts enabled only through DE; all other squad memberships preserved.')


def audit():
    p = target_profile()
    n = target_node(p)
    live_p = {v['uuid']:v for v in api('GET', '/config-profiles')['configProfiles']}
    for old in load('panel-profiles-before.json'):
        assert old['config'] == live_p[old['uuid']]['config'], 'An existing profile changed'
    live_h = {v['uuid']:v for v in api('GET', '/hosts')}
    for old in load('panel-hosts-before.json'):
        assert old == live_h[old['uuid']], 'An existing host changed'
    live_n = {v['uuid']:v for v in api('GET', '/nodes')}
    for uuid, old in load('existing-node-bindings.json').items():
        assert all(old[k] == live_n[uuid][k] for k in old), 'An existing node binding changed'
    old_squads = {s['uuid']:s for s in load('panel-squads-before.json')}
    new_ids = {i['uuid'] for i in p['inbounds']}
    for s in api('GET', '/internal-squads')['internalSquads']:
        before = {i['uuid'] for i in old_squads[s['uuid']]['inbounds']}
        after = {i['uuid'] for i in s['inbounds']}
        assert after == (before | new_ids if s['name'] == TEST_SQUAD else before)
    assert n['isConnected'] and not n.get('lastStatusMessage')
    print('PASS: existing profiles, hosts, node bindings and other squads unchanged; new node connected.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'create', 'status', 'activate_test', 'audit'])
    try:
        globals()[parser.parse_args().command]()
    except urllib.error.HTTPError as exc:
        print('API error:', exc.code, exc.read().decode()[:1500], file=sys.stderr)
        raise SystemExit(1)
    except (urllib.error.URLError, TimeoutError) as exc:
        print('API connection error; reread actual state before retry:', str(exc)[:250], file=sys.stderr)
        raise SystemExit(1)
