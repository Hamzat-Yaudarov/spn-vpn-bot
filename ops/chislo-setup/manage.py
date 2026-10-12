"""Provision only the owner-authorized CHISLO objects; never copy subscriber keys.

Existing profiles/nodes/hosts are not overwritten. Activation is limited to DE.
Snapshots and the REALITY private key remain in the private staging directory.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.prepare_direct_transports import api, private_write

STATE = Path('/private/tmp/wayspn-chislo-setup')
ADDRESS = '31.76.50.87'
PROFILE = 'VK_CDN_CHISLO'
TEST_SQUAD = 'DE'
SOURCE = 'VK_CDN_TEKST'


def load(name):
    return json.loads((STATE / name).read_text())


def save(name, value):
    target = STATE / name
    if not target.exists():
        private_write(target, value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def target_profile():
    matches = [p for p in api('GET', '/config-profiles')['configProfiles'] if p['name'] == PROFILE]
    assert len(matches) == 1, 'Expected one dedicated CHISLO profile'
    profile = matches[0]
    assert profile['config'] == load('profile.json'), 'CHISLO profile differs; refusing overwrite'
    assert len(profile['inbounds']) == 4
    return profile


def target_node(profile):
    matches = [n for n in api('GET', '/nodes') if n['address'] == ADDRESS]
    assert len(matches) == 1, 'Expected one CHISLO node'
    node = matches[0]
    assert node['port'] == 2222
    assert node['configProfile']['activeConfigProfileUuid'] == profile['uuid']
    assert {i['uuid'] for i in node['configProfile']['activeInbounds']} == {i['uuid'] for i in profile['inbounds']}
    return node


def expected_hosts(profile, node, source, source_hosts):
    old_tags = {i['uuid']: i['tag'] for i in source['inbounds']}
    new_ids = {i['tag']: i['uuid'] for i in profile['inbounds']}
    allowed = {'address', 'port', 'path', 'sni', 'host', 'alpn', 'fingerprint',
               'securityLayer', 'xhttpExtraParams', 'muxParams', 'sockoptParams',
               'finalMask', 'serverDescription', 'pinnedPeerCertSha256',
               'verifyPeerCertByName', 'shuffleHost', 'mihomoX25519',
               'mihomoIpVersion', 'isHidden', 'overrideSniFromAddress',
               'keepSniBlank', 'vlessRouteId', 'xrayJsonTemplateUuid',
               'excludeFromSubscriptionTypes', 'mapper'}
    result = []
    for original in source_hosts:
        old_tag = old_tags[original['inbound']['configProfileInboundUuid']]
        tag = old_tag.replace('-tekst', '-chislo')
        host = {k: copy.deepcopy(v) for k, v in original.items() if k in allowed}
        host = json.loads(json.dumps(host).replace('tekst.site', 'chislo.site').replace('87.251.16.170', ADDRESS))
        label = 'CDN • ОБХОД' if 'xhttp' in tag else 'gRPC' if 'grpc' in tag else 'Hysteria2' if 'hy2' in tag else 'WS'
        host.update(remark=f'CHISLO • {label}', nodes=[node['uuid']], tags=['CHISLO'],
                    isDisabled=True,
                    inbound={'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': new_ids[tag]},
                    internalSquads={'mode': 'EXCLUDE', 'squads': []})
        if 'xhttp' in tag:
            host['path'] = load('public.json')['edgePath']
            # Use the proven TEKST client extras; server-only limits stay in the inbound.
        result.append(host)
    assert len(result) == 4 and len({h['inbound']['configProfileInboundUuid'] for h in result}) == 4
    return result


def assert_host(actual, wanted):
    for key in ('remark', 'address', 'port', 'path', 'sni', 'host', 'alpn',
                'fingerprint', 'securityLayer', 'xhttpExtraParams', 'nodes', 'inbound'):
        assert actual.get(key) == wanted.get(key), f'Existing CHISLO host changed: {key}'


def create():
    profiles = api('GET', '/config-profiles')['configProfiles']
    nodes = api('GET', '/nodes')
    hosts = api('GET', '/hosts')
    squads = api('GET', '/internal-squads')['internalSquads']
    assert len([s for s in squads if s['name'] == TEST_SQUAD]) == 1
    source = next(p for p in profiles if p['name'] == SOURCE)
    source_hosts = [h for h in hosts if h.get('inbound', {}).get('configProfileUuid') == source['uuid']]
    assert len(source_hosts) == 4, 'Template must have exactly four hosts'
    save('panel-profiles-before.json', profiles)
    save('panel-hosts-before.json', hosts)
    save('panel-squads-before.json', squads)
    save('source-profile.json', source)
    save('source-hosts.json', source_hosts)
    save('existing-node-bindings.json', {n['uuid']: {'address': n['address'], 'configProfile': n['configProfile']} for n in nodes if n['address'] != ADDRESS})
    matches = [p for p in profiles if p['name'] == PROFILE]
    assert len(matches) <= 1
    if matches:
        profile = matches[0]
        assert profile['config'] == load('profile.json'), 'Existing target profile differs'
    else:
        assert not any(n['address'] == ADDRESS for n in nodes), 'Target address already belongs to another node'
        profile = api('POST', '/config-profiles', {'name': PROFILE, 'config': load('profile.json')})
    assert len(profile['inbounds']) == 4
    save('created-profile.json', profile)
    matches = [n for n in nodes if n['address'] == ADDRESS]
    assert len(matches) <= 1
    if matches:
        node = target_node(profile)
    else:
        node = api('POST', '/nodes', {
            'name': 'node-chislo-50.87', 'address': ADDRESS, 'port': 2222,
            'countryCode': 'XX', 'tags': ['CHISLO', 'VK_CDN'],
            'note': 'chislo.site: VK CDN XHTTP, direct gRPC, Hysteria2, WS; test DE only',
            'configProfile': {'activeConfigProfileUuid': profile['uuid'],
                              'activeInbounds': [i['uuid'] for i in profile['inbounds']]},
        })
    save('created-node.json', node)
    existing = api('GET', '/hosts')
    for wanted in expected_hosts(profile, node, source, source_hosts):
        matches = [h for h in existing if h.get('inbound', {}).get('configProfileInboundUuid') == wanted['inbound']['configProfileInboundUuid']]
        assert len(matches) <= 1
        if matches:
            assert_host(matches[0], wanted)
            print('Already exists:', matches[0]['remark'])
        else:
            result = api('POST', '/hosts', wanted)
            assert_host(result, wanted)
            assert result['isDisabled']
            save('host-' + result['uuid'] + '.json', result)
            print('Created disabled:', result['remark'])
    print('Created dedicated CHISLO profile/node; no existing objects overwritten.')


def status():
    profile = target_profile()
    node = target_node(profile)
    print(json.dumps({k: node.get(k) for k in ('name', 'address', 'isConnected', 'isConnecting',
                      'isDisabled', 'lastStatusMessage', 'versions', 'xrayUptime', 'usersOnline')}, ensure_ascii=False))
    for host in api('GET', '/hosts'):
        if host.get('inbound', {}).get('configProfileUuid') == profile['uuid']:
            print(host['remark'], host['address'], host['port'], 'disabled:', host['isDisabled'])


def activate_test():
    profile = target_profile()
    node = target_node(profile)
    assert node['isConnected'] and not node['isDisabled'] and not node.get('lastStatusMessage'), 'Node not ready'
    selected = {i['uuid'] for i in profile['inbounds']}
    squads = api('GET', '/internal-squads')['internalSquads']
    for squad in squads:
        if squad['name'] != TEST_SQUAD:
            assert not selected.intersection(i['uuid'] for i in squad['inbounds']), 'CHISLO unexpectedly assigned outside DE'
    squad = next(s for s in squads if s['name'] == TEST_SQUAD)
    current = [i['uuid'] for i in squad['inbounds']]
    additions = [i['uuid'] for i in profile['inbounds'] if i['uuid'] not in current]
    if additions:
        save('test-squad-before.json', squad)
        updated = api('PATCH', '/internal-squads', {'uuid': squad['uuid'], 'inbounds': current + additions})
        assert {i['uuid'] for i in updated['inbounds']} == set(current + additions)
        print('Added four inbounds only to DE; existing membership/inbounds preserved.')
    wanted_by_id = {h['inbound']['configProfileInboundUuid']: h for h in expected_hosts(profile, node, load('source-profile.json'), load('source-hosts.json'))}
    for host in api('GET', '/hosts'):
        if host.get('inbound', {}).get('configProfileUuid') != profile['uuid']:
            continue
        assert_host(host, wanted_by_id[host['inbound']['configProfileInboundUuid']])
        if host['isDisabled']:
            enabled = api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': False})
            assert not enabled['isDisabled']
            print('Enabled for DE test:', enabled['remark'])


def audit():
    profile = target_profile()
    node = target_node(profile)
    old_profiles = load('panel-profiles-before.json')
    live_profiles = {p['uuid']: p for p in api('GET', '/config-profiles')['configProfiles']}
    for before in old_profiles:
        if before['name'] != PROFILE:
            assert digest(before['config']) == digest(live_profiles[before['uuid']]['config']), 'An existing profile changed'
    live_hosts = {h['uuid']: h for h in api('GET', '/hosts')}
    for before in load('panel-hosts-before.json'):
        assert before == live_hosts[before['uuid']], 'An existing host changed'
    live_nodes = {n['uuid']: n for n in api('GET', '/nodes')}
    for uuid, before in load('existing-node-bindings.json').items():
        assert all(before[k] == live_nodes[uuid][k] for k in before), 'An existing node binding changed'
    new_ids = {i['uuid'] for i in profile['inbounds']}
    for squad in api('GET', '/internal-squads')['internalSquads']:
        if new_ids.intersection(i['uuid'] for i in squad['inbounds']):
            assert squad['name'] == TEST_SQUAD
    assert node['isConnected'] and not node.get('lastStatusMessage')
    print('OK: existing profiles, hosts and node bindings unchanged; CHISLO access limited to DE.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['create', 'status', 'activate_test', 'audit'])
    command = parser.parse_args().command
    try:
        globals()[command]()
    except urllib.error.HTTPError as exc:
        # Validation errors only; never print request bodies or authentication headers.
        print('Remnawave API error:', exc.code, exc.read().decode()[:2000], file=sys.stderr)
        raise SystemExit(1)
