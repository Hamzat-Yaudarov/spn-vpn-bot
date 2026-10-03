"""Provision the explicitly requested tekst.site node from the live Estonia template.

Secrets and snapshots are stored only in a private temporary directory.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import base64
import secrets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.prepare_direct_transports import api, private_write

STATE = Path('/private/tmp/wayspn-tekst-setup')
DOMAIN = 'tekst.site'
ADDRESS = '87.251.16.170'
PROFILE = 'VK_CDN_TEKST'

def save(name, value):
    STATE.mkdir(mode=0o700, exist_ok=True)
    target = STATE / name
    if not target.exists():
        private_write(target, value)

def inspect():
    profiles = api('GET', '/config-profiles')['configProfiles']
    source = next(p for p in profiles if p['name'] == 'VK_CDN_PRESTIZH')
    hosts = [h for h in api('GET', '/hosts') if h.get('inbound', {}).get('configProfileUuid') == source['uuid']]
    nodes = api('GET', '/nodes')
    node = next(n for n in nodes if n['address'] == '217.60.177.12')
    squads = api('GET', '/internal-squads')['internalSquads']
    save('source-profile.json', source)
    save('source-hosts.json', hosts)
    save('source-node.json', node)
    save('squads-before.json', squads)
    print('Source:', source['name'], source['uuid'])
    config = copy.deepcopy(source['config'])
    for inbound in config['inbounds']:
        inbound.get('streamSettings', {}).get('realitySettings', {}).pop('privateKey', None)
    print(json.dumps(config, ensure_ascii=False, indent=2))
    print('HOSTS:', json.dumps(hosts, ensure_ascii=False, indent=2))
    print('Source node fields:', list(node))
    print('Existing target:', [(n['name'], n['uuid']) for n in nodes if n['address'] == '87.251.16.170'])
    ids = {i['uuid'] for i in source['inbounds']}
    print('SQUADS:', [(s['name'], [i['tag'] for i in s['inbounds'] if i['uuid'] in ids]) for s in squads if any(i['uuid'] in ids for i in s['inbounds'])])

def load(name):
    return json.loads((STATE / name).read_text())

def write_text(name, text):
    fd = os.open(STATE / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(text)

def prepare():
    if (STATE / 'candidate.json').exists():
        print('Candidate already prepared; preserving keys and paths.')
        return
    source = load('source-profile.json')
    config = json.loads(json.dumps(source['config']).replace('prestizh', 'tekst'))
    key = X25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    b64 = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    edge_path = '/edge/' + secrets.token_hex(12) + '/'
    for inbound in config['inbounds']:
        assert inbound['settings']['clients'] == []
        stream = inbound['streamSettings']
        if stream['network'] == 'xhttp':
            stream['xhttpSettings']['path'] = edge_path
        if stream['security'] == 'reality':
            stream['realitySettings']['privateKey'] = b64(raw)
            stream['realitySettings']['shortIds'] = [secrets.token_hex(8)]
    save('candidate.json', config)
    save('public.json', {'publicKey': b64(public), 'edgePath': edge_path})
    secret = api('GET', '/keygen')['secretKey']
    write_text('node.env', 'NODE_PORT=2222\nSECRET_KEY=' + secret + '\n')
    template = (ROOT / 'ops/tekst-setup/nginx.conf.template').read_text()
    write_text('tekst.site.conf', template.replace('__EDGE_PATH__', edge_path))
    print('Prepared four inbounds, fresh REALITY key and private node environment.')

def create():
    config = load('candidate.json')
    profiles = api('GET', '/config-profiles')['configProfiles']
    matches = [p for p in profiles if p['name'] == PROFILE]
    assert len(matches) <= 1
    if matches:
        profile = matches[0]
        assert profile['config'] == config, 'Existing target profile differs; refusing overwrite'
    else:
        profile = api('POST', '/config-profiles', {'name': PROFILE, 'config': config})
    save('created-profile.json', profile)
    nodes = api('GET', '/nodes')
    matches = [n for n in nodes if n['address'] == ADDRESS]
    assert len(matches) <= 1
    if matches:
        node = matches[0]
        assert node['configProfile']['activeConfigProfileUuid'] == profile['uuid']
    else:
        node = api('POST', '/nodes', {
            'name': 'node-tekst-16.170', 'address': ADDRESS, 'port': 2222,
            'countryCode': 'FI', 'tags': ['FI', 'VK_CDN'],
            'note': 'tekst.site: VK CDN, direct gRPC, Hysteria2 and WS',
            'configProfile': {'activeConfigProfileUuid': profile['uuid'],
                              'activeInbounds': [i['uuid'] for i in profile['inbounds']]},
        })
    save('created-node.json', node)
    existing = api('GET', '/hosts')
    source = load('source-profile.json')
    old_tags = {i['uuid']: i['tag'] for i in source['inbounds']}
    new_ids = {i['tag']: i['uuid'] for i in profile['inbounds']}
    for original in load('source-hosts.json'):
        tag = old_tags[original['inbound']['configProfileInboundUuid']].replace('prestizh', 'tekst')
        matches = [h for h in existing if h.get('inbound', {}).get('configProfileInboundUuid') == new_ids[tag]]
        if matches:
            assert len(matches) == 1 and matches[0]['nodes'] == [node['uuid']]
            continue
        host = {k: v for k, v in original.items() if k not in ('uuid', 'viewPosition')}
        host = json.loads(json.dumps(host).replace('prestizh.site', DOMAIN).replace('217.60.177.12', ADDRESS))
        label = 'CDN' if 'xhttp' in tag else 'gRPC' if 'grpc' in tag else 'Hysteria2' if 'hy2' in tag else 'WS'
        host['remark'] = f'🇫🇮 SPN | Финляндия • {label} • TEKST'
        host['inbound'] = {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': new_ids[tag]}
        host['nodes'] = [node['uuid']]
        host['tags'] = ['TEKST']
        host['isDisabled'] = True
        if label == 'CDN':
            host['path'] = load('public.json')['edgePath']
        result = api('POST', '/hosts', host)
        assert result['isDisabled']
        print('Created (awaiting verification):', result['remark'])
    print('Created node and dedicated profile:', node['uuid'], profile['uuid'])

def status():
    node_id = load('created-node.json')['uuid']
    node = next(n for n in api('GET', '/nodes') if n['uuid'] == node_id)
    print(json.dumps({k: node.get(k) for k in ('name', 'address', 'isConnected', 'isDisabled', 'lastStatusMessage', 'versions', 'usersOnline')}, ensure_ascii=False))
    profile_id = load('created-profile.json')['uuid']
    for h in api('GET', '/hosts'):
        if h.get('inbound', {}).get('configProfileUuid') == profile_id:
            print(h['remark'], h['address'], h['port'], 'disabled:', h['isDisabled'])

def activate(include_cdn=True):
    source = load('source-profile.json')
    profile = load('created-profile.json')
    node_id = load('created-node.json')['uuid']
    node = next(n for n in api('GET', '/nodes') if n['uuid'] == node_id)
    assert node['isConnected'] and not node['isDisabled'], 'Target node is not ready'
    new_ids = {i['tag']: i['uuid'] for i in profile['inbounds']}
    source_ids = {i['uuid']: i['tag'] for i in source['inbounds']
                  if include_cdn or 'xhttp' not in i['tag']}
    selected_ids = {new_ids[tag.replace('prestizh', 'tekst')] for tag in source_ids.values()}
    for squad in api('GET', '/internal-squads')['internalSquads']:
        extra = [new_ids[source_ids[i['uuid']].replace('prestizh', 'tekst')]
                 for i in squad['inbounds'] if i['uuid'] in source_ids]
        current = [i['uuid'] for i in squad['inbounds']]
        additions = [i for i in extra if i not in current]
        if additions:
            save('squad-' + squad['uuid'] + '-before.json', squad)
            updated = api('PATCH', '/internal-squads', {'uuid': squad['uuid'], 'inbounds': current + additions})
            assert {i['uuid'] for i in updated['inbounds']} == set(current + additions)
            print('Assigned:', squad['name'], len(additions))
    for host in api('GET', '/hosts'):
        if (host.get('inbound', {}).get('configProfileUuid') == profile['uuid']
                and host['inbound']['configProfileInboundUuid'] in selected_ids and host['isDisabled']):
            assert host['nodes'] == [node_id]
            result = api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': False})
            assert not result['isDisabled']
            print('Enabled:', result['remark'])

def activate_direct():
    activate(include_cdn=False)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['inspect', 'prepare', 'create', 'status', 'activate', 'activate_direct'])
    args = parser.parse_args()
    globals()[args.command]()
