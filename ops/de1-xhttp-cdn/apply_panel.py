#!/usr/bin/env python3
"""Create/resume only the dedicated DE1 objects. No subscriber endpoints or SSH."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import stat
import sys

import panel_plan as plan
import prepare as origin

DOMAINS = {'cdn-de1.chislo.site', '1sd2ljvb74.cdn.twcstorage.ru'}
SOURCE_TAG = 'vless-xhttp-yandex-de11'


def api(method, path, body=None):
    sys.path.insert(0, str(plan.ROOT))
    from scripts.prepare_direct_transports import api as request
    return request(method, path, body)


def require(value, message):
    if not value:
        raise ValueError(message)


def snapshots():
    return {'profiles': api('GET', '/config-profiles')['configProfiles'],
            'nodes': api('GET', '/nodes'), 'hosts': api('GET', '/hosts'),
            'squads': api('GET', '/internal-squads')['internalSquads']}


def ids(items):
    return sorted(item['uuid'] if isinstance(item, dict) else item for item in items)


def signatures(live):
    result = {}
    for kind, rows in live.items():
        require(len({row['uuid'] for row in rows}) == len(rows), 'Duplicate infrastructure UUIDs')
        values = {}
        for row in rows:
            if kind == 'profiles':
                value = {'name': row['name'], 'config': row['config'],
                    'inbounds': sorted((item['uuid'], item['tag']) for item in row['inbounds'])}
            elif kind == 'nodes':
                binding = row.get('configProfile') or {}
                value = {'name': row['name'], 'address': row['address'], 'port': row['port'],
                    'profile': binding.get('activeConfigProfileUuid'), 'inbounds': ids(binding.get('activeInbounds', []))}
            elif kind == 'squads':
                value = {'name': row['name'], 'inbounds': ids(row['inbounds'])}
            else:
                value = row  # Whole host hash: SNI/path/extras/template changes must not disappear.
            values[row['uuid']] = plan.digest(value)
        result[kind] = values
    return result


def read_private(path):
    return plan.read(Path(path))


def load_plan(directory):
    directory = Path(directory)
    require(not directory.is_symlink() and stat.S_IMODE(directory.stat().st_mode) == 0o700,
            'Plan must be a real private 0700 directory')
    profile = read_private(directory / 'profile.create.json')
    node = read_private(directory / 'node.create.template.json')
    host = read_private(directory / 'host.create.template.json')
    metadata = read_private(directory / 'plan.json')
    path = host.get('path')
    require(isinstance(path, str) and origin.EDGE_PATH.fullmatch(path), 'Invalid saved XHTTP path')
    expected_config = json.loads((origin.SOURCE / 'configprofile.json.template').read_text().replace('__EDGE_PATH__', path))
    expected_config['inbounds'][0]['tag'] = plan.TAG
    require(profile == {'name': plan.PROFILE, 'config': expected_config}, 'Profile is outside approved single-origin template')
    expected_node = {'name': plan.NODE, 'address': origin.ORIGIN_IP, 'port': 2222, 'countryCode': 'DE',
        'tags': ['DE1_XHTTP_TIMEWEB_TEST'],
        'note': 'Dedicated DE1 Timeweb XHTTP pilot; no RU cascade; DE test access only',
        'configProfile': {'activeConfigProfileUuid': plan.NEW_PROFILE, 'activeInbounds': [plan.NEW_INBOUND]}}
    require(node == expected_node, 'Unexpected node payload')
    require(metadata.get('format') == 1 and metadata.get('originIp') == origin.ORIGIN_IP
            and metadata.get('originDomain') == origin.DOMAIN and metadata.get('profileName') == plan.PROFILE
            and metadata.get('nodeName') == plan.NODE and metadata.get('hostRemark') == plan.HOST
            and metadata.get('existingObjectWritesAllowed') is False, 'Unexpected plan identity')
    domain = host.get('address')
    squad = metadata.get('testSquadUuid')
    require(domain in DOMAINS and metadata.get('cdnDomain') == domain and isinstance(squad, str) and squad,
            'CDN domain or test squad is not explicitly approved')
    require(host.get('alpn') == 'h2' and host.get('fingerprint') in ('chrome', 'firefox'), 'Unexpected client TLS settings')
    extras = host.get('xhttpExtraParams')
    parsed = json.loads(extras) if isinstance(extras, str) else extras
    require(parsed == origin.CLIENT_XHTTP, 'Unexpected client XHTTP extras')
    expected_host = {'remark': plan.HOST, 'address': domain, 'port': 443, 'sni': domain, 'host': domain,
        'path': path, 'securityLayer': 'TLS', 'alpn': host['alpn'], 'fingerprint': host['fingerprint'],
        'isDisabled': True, 'isHidden': False, 'shuffleHost': False, 'tags': ['DE1_XHTTP_TIMEWEB_TEST'],
        'nodes': [plan.NEW_NODE], 'inbound': {'configProfileUuid': plan.NEW_PROFILE, 'configProfileInboundUuid': plan.NEW_INBOUND},
        'internalSquads': {'mode': 'ALLOW_ONLY', 'squads': [squad]}, 'xrayJsonTemplateUuid': None,
        'xhttpExtraParams': extras}
    require(host == expected_host, 'Unexpected host payload fields')
    return {'profile': profile, 'node': node, 'host': host, 'squad': squad}


def state_file(directory):
    directory = Path(directory)
    require(directory.is_absolute() and directory.resolve().is_relative_to(Path('/private/tmp'))
            and not directory.is_symlink(), 'State must stay under /private/tmp')
    if not directory.exists():
        directory.mkdir(mode=0o700)
    require(stat.S_IMODE(directory.stat().st_mode) == 0o700, 'State directory must have mode 0700')
    return directory / 'manifest.json'


def save(directory, state):
    path = state_file(directory)
    temporary = path.with_name('manifest.next.json')
    plan.write(temporary, state)
    os.replace(temporary, path)


def load_state(directory):
    state = read_private(state_file(directory))
    require(state.get('format') == 1 and state.get('originIp') == origin.ORIGIN_IP,
            'Unexpected manifest identity')
    # Revalidate the original immutable plan, including its path and full config.
    original = load_plan(state['planDirectory'])
    require(original['profile'] == state['profile'] and original['node'] == state['node']
            and original['squad'] == state['squad'], 'Saved core plan changed')
    require(type(state.get('activationRequested')) is bool, 'Unexpected activation state')
    if state.get('host') is not None:
        require(isinstance(state['host'], dict) and state['host'].get('address') in DOMAINS,
                'Saved host domain is not explicitly approved')
        expected_host = copy.deepcopy(original['host'])
        domain = state['host']['address']
        expected_host.update(address=domain, sni=domain, host=domain)
        require(state['host'] == expected_host, 'Saved host plan changed outside approved domain override')
    return state


def one(rows, predicate, label):
    found = [row for row in rows if predicate(row)]
    require(len(found) <= 1, f'Ambiguous {label}; manual review required')
    return found[0] if found else None


def resolved(state, kind):
    payload = copy.deepcopy(state[kind])
    if kind == 'node':
        payload['configProfile'] = {'activeConfigProfileUuid': state['owned']['profile'],
            'activeInbounds': [state['owned']['inbound']]}
    if kind == 'host':
        payload['nodes'] = [state['owned']['node']]
        payload['inbound'] = {'configProfileUuid': state['owned']['profile'],
            'configProfileInboundUuid': state['owned']['inbound']}
    return payload


def check_fields(actual, expected):
    for key, wanted in expected.items():
        value = actual.get(key)
        if key == 'configProfile':
            require(value and value.get('activeConfigProfileUuid') == wanted['activeConfigProfileUuid']
                    and ids(value.get('activeInbounds', [])) == wanted['activeInbounds'], 'Owned node binding differs')
        elif key == 'nodes':
            require(ids(value or []) == wanted, 'Owned host node association differs')
        elif key == 'inbound':
            require(value and all(value.get(k) == v for k, v in wanted.items()), 'Owned host inbound differs')
        elif key == 'xhttpExtraParams':
            parse = lambda x: json.loads(x) if isinstance(x, str) else x
            require(parse(value) == parse(wanted), 'Owned host XHTTP extras differ')
        else:
            require(value == wanted, f'Owned object {key} differs')


def refresh(directory, state):
    live = snapshots()
    current = signatures(live)
    profile = one(live['profiles'], lambda p: p['name'] == plan.PROFILE, 'profile name')
    if profile:
        require(profile['uuid'] not in state['baseline']['profiles'], 'Dedicated profile existed before this operation')
        check_fields(profile, state['profile'])
        require(len(profile['inbounds']) == 1 and profile['inbounds'][0]['tag'] == plan.TAG, 'Unexpected created inbound')
        old_inbounds = {item['uuid'] for old in live['profiles']
                        if old['uuid'] in state['baseline']['profiles'] for item in old['inbounds']}
        require(profile['inbounds'][0]['uuid'] not in old_inbounds, 'Created inbound collides with an existing profile')
        require(state['owned'].get('profile') in (None, profile['uuid'])
                and state['owned'].get('inbound') in (None, profile['inbounds'][0]['uuid']), 'Owned profile identity changed')
        state['owned'].update(profile=profile['uuid'], inbound=profile['inbounds'][0]['uuid'])
    node = one(live['nodes'], lambda n: n['name'] == plan.NODE or n['address'] == origin.ORIGIN_IP, 'node name/address')
    if node:
        require(profile and node['uuid'] not in state['baseline']['nodes'], 'Node existed before or has no owned profile')
        check_fields(node, resolved(state, 'node'))
        require(state['owned'].get('node') in (None, node['uuid']), 'Owned node identity changed')
        state['owned']['node'] = node['uuid']
    host_domain = (state.get('host') or {}).get('address')
    host = one(live['hosts'], lambda h: h['remark'] == plan.HOST or (host_domain and h['address'] == host_domain and h['port'] == 443), 'host name/address')
    if host:
        require(node and state.get('host') and host['uuid'] not in state['baseline']['hosts'], 'Host is not owned by this operation')
        wanted = resolved(state, 'host')
        if state.get('activationRequested'):
            wanted['isDisabled'] = host.get('isDisabled')
            require(type(wanted['isDisabled']) is bool, 'Invalid host enabled state')
        check_fields(host, wanted)
        require(state['owned'].get('host') in (None, host['uuid']), 'Owned host identity changed')
        state['owned']['host'] = host['uuid']
    for kind in ('profiles', 'nodes', 'hosts', 'squads'):
        own = state['owned'].get(kind[:-1])
        require(set(current[kind]) == set(state['baseline'][kind]) | ({own} if own else set()), 'Unrelated infrastructure objects added or removed')
        for uuid, before in state['baseline'][kind].items():
            if kind == 'squads' and uuid == state['squad'] and state.get('activationRequested'):
                squad = one(live['squads'], lambda s: s['uuid'] == uuid, 'DE squad')
                old = state['deInbounds']
                require(squad and squad['name'] == plan.TEST_SQUAD
                        and ids(squad['inbounds']) in (old, sorted(set(old + [state['owned']['inbound']]))), 'DE squad changed outside allowed append')
            else:
                require(current[kind][uuid] == before, 'Existing infrastructure changed; stop before any write')
    for kind, row in (('profile', profile), ('node', node), ('host', host)):
        require(not state['owned'].get(kind) or row is not None, 'Owned object disappeared; do not recreate automatically')
    save(directory, state)
    return live


def create_core(plan_dir, state_dir, *, confirm=False):
    require(confirm is True, 'Explicit create-core confirmation required')
    payloads = load_plan(plan_dir)
    path = state_file(state_dir)
    if path.exists():
        state = load_state(state_dir)
        require(payloads['profile'] == state['profile'] and payloads['node'] == state['node'], 'Core plan/path changed')
    else:
        live = snapshots()
        require(not any(p['name'] == plan.PROFILE for p in live['profiles'])
                and not any(n['name'] == plan.NODE or n['address'] == origin.ORIGIN_IP for n in live['nodes'])
                and not any(h['remark'] == plan.HOST for h in live['hosts']), 'Dedicated names/address already exist without manifest')
        squad = one(live['squads'], lambda s: s['uuid'] == payloads['squad'] and s['name'] == plan.TEST_SQUAD, 'DE squad')
        require(squad, 'DE test squad changed')
        source = one(live['profiles'], lambda p: p['uuid'] == plan.SOURCE_PROFILE, 'DE11 source profile')
        source_host = one(live['hosts'], lambda h: h['uuid'] == plan.SOURCE_HOST, 'DE11 source host')
        require(source and source_host and any(i['uuid'] == (source_host.get('inbound') or {}).get('configProfileInboundUuid')
                and i['tag'] == SOURCE_TAG for i in source['inbounds'])
                and source_host['inbound'].get('configProfileUuid') == plan.SOURCE_PROFILE, 'DE11 source inbound changed')
        state = {'format': 1, 'originIp': origin.ORIGIN_IP, 'planDirectory': str(Path(plan_dir).resolve()),
            'profile': payloads['profile'], 'node': payloads['node'], 'host': None, 'squad': payloads['squad'],
            'deInbounds': ids(squad['inbounds']), 'baseline': signatures(live), 'owned': {}, 'activationRequested': False}
        save(state_dir, state)
    refresh(state_dir, state)
    if not state['owned'].get('profile'):
        api('POST', '/config-profiles', state['profile'])
        refresh(state_dir, state)
        require(state['owned'].get('profile'), 'Profile creation not confirmed by fresh GET')
    if not state['owned'].get('node'):
        api('POST', '/nodes', resolved(state, 'node'))
        refresh(state_dir, state)
        require(state['owned'].get('node'), 'Node creation not confirmed by fresh GET')
    print('Dedicated profile and node confirmed. No host/squad activation performed.')
    return state


def create_host(plan_dir, state_dir, *, confirm=False, cdn_verified=False, cdn_domain=None):
    require(confirm is True, 'Explicit disabled-host creation confirmation required')
    payloads = load_plan(plan_dir)
    state = load_state(state_dir)
    require(payloads['profile'] == state['profile'] and payloads['node'] == state['node']
            and payloads['squad'] == state['squad'], 'Host plan does not belong to saved core/path')
    wanted = copy.deepcopy(payloads['host'])
    if cdn_domain is not None:
        require(cdn_domain in DOMAINS, 'CDN domain is not explicitly approved')
        wanted.update(address=cdn_domain, sni=cdn_domain, host=cdn_domain)
    require(state.get('host') in (None, wanted), 'Saved host/domain cannot change during resume')
    state['host'] = wanted
    save(state_dir, state)
    refresh(state_dir, state)
    require(state['owned'].get('node'), 'Create the dedicated core first')
    if not state['owned'].get('host'):
        api('POST', '/hosts', resolved(state, 'host'))
        refresh(state_dir, state)
        require(state['owned'].get('host'), 'Host creation not confirmed by fresh GET')
    print('Dedicated host confirmed; it remains disabled until separate CDN-ready DE activation.')
    return state


def audit(state_dir):
    state = load_state(state_dir)
    refresh(state_dir, state)
    print('Existing infrastructure unchanged; dedicated object scope verified.')
    return state


def enable_de(state_dir, *, confirm=False, cdn_verified=False):
    require(confirm is True and cdn_verified is True, 'Explicit confirmation and CDN DNS/TLS/route verification required')
    state = load_state(state_dir)
    live = refresh(state_dir, state)
    require(state['owned'].get('host'), 'No owned disabled host to activate')
    node = one(live['nodes'], lambda n: n['uuid'] == state['owned']['node'], 'owned node')
    require(node and node.get('isConnected') is True and node.get('isDisabled') is False and not node.get('lastStatusMessage'),
            'Dedicated node is not healthy/connected')
    state['activationRequested'] = True
    save(state_dir, state)
    live = refresh(state_dir, state)
    squad = one(live['squads'], lambda s: s['uuid'] == state['squad'], 'DE squad')
    current_ids = ids(squad['inbounds'])
    if state['owned']['inbound'] not in current_ids:
        api('PATCH', '/internal-squads', {'uuid': state['squad'], 'inbounds': current_ids + [state['owned']['inbound']]})
        live = refresh(state_dir, state)
        squad = one(live['squads'], lambda s: s['uuid'] == state['squad'], 'DE squad')
        require(state['owned']['inbound'] in ids(squad['inbounds']), 'DE append not confirmed by fresh GET')
    host = one(live['hosts'], lambda h: h['uuid'] == state['owned']['host'], 'owned host')
    if host['isDisabled']:
        node = one(live['nodes'], lambda n: n['uuid'] == state['owned']['node'], 'owned node')
        require(node and node.get('isConnected') is True and node.get('isDisabled') is False
                and not node.get('lastStatusMessage'), 'Dedicated node became unhealthy before host activation')
        api('PATCH', '/hosts', {'uuid': state['owned']['host'], 'isDisabled': False})
        live = refresh(state_dir, state)
        host = one(live['hosts'], lambda h: h['uuid'] == state['owned']['host'], 'owned host')
        require(host['isDisabled'] is False, 'Host activation not confirmed by fresh GET')
    print('Only the dedicated host enabled; only its inbound appended to DE.')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('create-core', 'create-host', 'audit', 'enable-DE'))
    parser.add_argument('--plan-dir', type=Path)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--cdn-domain', choices=sorted(DOMAINS))
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--cdn-verified', action='store_true')
    args = parser.parse_args()
    try:
        if args.command in ('create-core', 'create-host'):
            require(args.plan_dir is not None, 'Provide the private plan directory')
            if args.command == 'create-core':
                create_core(args.plan_dir, args.state_dir, confirm=args.confirm)
            else:
                create_host(args.plan_dir, args.state_dir, confirm=args.confirm, cdn_domain=args.cdn_domain)
        elif args.command == 'audit':
            audit(args.state_dir)
        else:
            enable_de(args.state_dir, confirm=args.confirm, cdn_verified=args.cdn_verified)
    except Exception as exc:
        print(f'Operation stopped ({type(exc).__name__}); inspect GET-only audit before retry. No response/credential printed.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
