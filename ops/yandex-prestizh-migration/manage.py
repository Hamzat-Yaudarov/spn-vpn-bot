"""Guarded migration of one CDN transport. Never reads subscriber endpoints.

Preparation/audit are GET-only. Apply is explicit, never retries a write, and
can resume a verified profile-only stage after an ambiguous transport failure.
Private state is stored outside the repository; no credentials are printed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.prepare_direct_transports import api, private_write

TARGET_HOST = '645c8877-c1ef-4224-993d-70a31b5cd4e5'
TARGET_PROFILE = '63c21878-f375-4383-af21-98c777c358c5'
TARGET_INBOUND = 'b3ee690f-2420-4441-8cdc-0e63bea75e80'
TARGET_TAG = 'vless-xhttp-vk-prestizh'
TARGET_NODE = 'a15c5e28-d144-49d8-b748-0b443de33da2'
TARGET_NODE_ADDRESS = '217.60.177.12'
TARGET_DOMAIN = 'cdn.prestizh.site'
TARGET_PATH = '/edge/766b55d4862f42c80e97aa52/'
SOURCE_PROFILE = '59aba40b-9ed3-4eec-b6af-b34a39da88d1'
SOURCE_HOST = 'c1614c56-403d-4a67-b917-928a04600db8'
SOURCE_TAG = 'vless-xhttp-yandex-de11'
PREFIX = 'wayspn-yandex-prestizh-migration-'
VOLATILE_HOST_FIELDS = {'updatedAt'}


class GuardError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise GuardError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def unique(rows, uuid, label):
    selected = [row for row in rows if row.get('uuid') == uuid]
    require(len(selected) == 1, f'{label}: expected exactly one selected object')
    return selected[0]


def by_tag(config, tag):
    selected = [item for item in config['inbounds'] if item.get('tag') == tag]
    require(len(selected) == 1, 'Selected inbound tag is missing or duplicated')
    return selected[0]


def node_bindings(nodes):
    require(len({node['uuid'] for node in nodes}) == len(nodes), 'Duplicate node UUIDs')
    return {node['uuid']: {'address': node['address'], 'port': node['port'],
        'profile': node['configProfile']['activeConfigProfileUuid'],
        'inbounds': sorted(item['uuid'] for item in node['configProfile']['activeInbounds'])}
        for node in nodes}


def inbound_bindings(profile):
    bindings = [(item['uuid'], item['tag']) for item in profile['inbounds']]
    require(len({uuid for uuid, _ in bindings}) == len(bindings)
            and len({tag for _, tag in bindings}) == len(bindings), 'Duplicate inbound UUIDs or tags')
    return sorted(bindings)


def host_state(host):
    # Only the selected host's server-maintained modification time may change.
    return {key: value for key, value in host.items() if key not in VOLATILE_HOST_FIELDS}


def snapshots():
    return {'profiles': api('GET', '/config-profiles')['configProfiles'],
            'hosts': api('GET', '/hosts'), 'nodes': api('GET', '/nodes')}


def parsed_extra(value):
    try:
        extra = json.loads(value) if isinstance(value, str) else copy.deepcopy(value)
    except (ValueError, TypeError) as exc:
        raise GuardError('Source client XHTTP extra is not valid JSON') from exc
    require(isinstance(extra, dict), 'Source client XHTTP extra must be an object')
    return extra


def upper_bound(value):
    try:
        parts = str(value).split('-')
        require(1 <= len(parts) <= 2 and all(part.isdecimal() for part in parts), 'Invalid packet-size limit')
        bounds = [int(part) for part in parts]
        require(0 < bounds[0] <= bounds[-1], 'Invalid packet-size range')
        return bounds[-1]
    except (ValueError, TypeError) as exc:
        raise GuardError('Invalid packet-size limit') from exc


def validate_source(server, client):
    # These are the already-verified DE11 header transport parameters, not a
    # guessed default packet-up/POST or the unrelated VK GET/body variant.
    require(server.get('mode') == 'packet-up', 'Source XHTTP mode changed')
    for value in (server, client):
        require(value.get('uplinkHTTPMethod') == 'GET', 'Source uplink method changed')
        require(value.get('uplinkDataPlacement') == 'header', 'Source uplink placement changed')
        require(value.get('uplinkDataKey') == 'X-Transport-Data', 'Source uplink header changed')
        require(upper_bound(value.get('scMaxEachPostBytes')) <= 2048, 'Source packet size exceeds verified header limit')
    require(server.get('serverMaxHeaderBytes') == 8192, 'Source server header limit changed')


def build_candidate(snapshot):
    require(len({profile['uuid'] for profile in snapshot['profiles']}) == len(snapshot['profiles']), 'Duplicate profile UUIDs')
    require(len({host['uuid'] for host in snapshot['hosts']}) == len(snapshot['hosts']), 'Duplicate host UUIDs')
    for profile in snapshot['profiles']:
        inbound_bindings(profile)
    node_bindings(snapshot['nodes'])
    target_host = unique(snapshot['hosts'], TARGET_HOST, 'Target host')
    source_host = unique(snapshot['hosts'], SOURCE_HOST, 'Source host')
    target_profile = unique(snapshot['profiles'], TARGET_PROFILE, 'Target profile')
    source_profile = unique(snapshot['profiles'], SOURCE_PROFILE, 'Source profile')
    node = unique(snapshot['nodes'], TARGET_NODE, 'Target node')
    link = target_host.get('inbound') or {}
    require(link.get('configProfileUuid') == TARGET_PROFILE and link.get('configProfileInboundUuid') == TARGET_INBOUND,
            'Target host binding changed')
    require(not any(host.get('uuid') != TARGET_HOST
                    and (host.get('inbound') or {}).get('configProfileUuid') == TARGET_PROFILE
                    and (host.get('inbound') or {}).get('configProfileInboundUuid') == TARGET_INBOUND
                    for host in snapshot['hosts']), 'Another host shares the selected CDN inbound; expanded authority required')
    require(target_host.get('address') == TARGET_DOMAIN and target_host.get('port') == 443
            and target_host.get('path') == TARGET_PATH and target_host.get('securityLayer') == 'TLS',
            'Target CDN address/port/path/security changed')
    metadata = unique(target_profile['inbounds'], TARGET_INBOUND, 'Target inbound')
    require(metadata.get('tag') == TARGET_TAG, 'Target inbound tag changed')
    require(node['address'] == TARGET_NODE_ADDRESS
            and node['configProfile']['activeConfigProfileUuid'] == TARGET_PROFILE
            and TARGET_INBOUND in {item['uuid'] for item in node['configProfile']['activeInbounds']},
            'Target node binding changed')
    host_nodes = target_host.get('nodes') or []
    host_node_ids = {item.get('uuid') if isinstance(item, dict) else item for item in host_nodes}
    require(not host_node_ids or host_node_ids == {TARGET_NODE}, 'Target host points to another node')
    require(not any(other['uuid'] != TARGET_NODE
                    and other['configProfile']['activeConfigProfileUuid'] == TARGET_PROFILE
                    and TARGET_INBOUND in {item['uuid'] for item in other['configProfile']['activeInbounds']}
                    for other in snapshot['nodes']), 'Another node uses the selected CDN inbound; expanded authority required')
    target = by_tag(target_profile['config'], TARGET_TAG)
    source = by_tag(source_profile['config'], SOURCE_TAG)
    source_metadata = [item for item in source_profile['inbounds'] if item.get('tag') == SOURCE_TAG]
    require(len(source_metadata) == 1 and (source_host.get('inbound') or {}).get('configProfileUuid') == SOURCE_PROFILE
            and (source_host.get('inbound') or {}).get('configProfileInboundUuid') == source_metadata[0]['uuid'],
            'Source host binding changed')
    require(target.get('listen') == '127.0.0.1' and target.get('port') == 10085
            and target['streamSettings'].get('network') == 'xhttp'
            and source['streamSettings'].get('network') == 'xhttp', 'Unexpected CDN listener or transport')
    require(target['streamSettings']['xhttpSettings'].get('path') == TARGET_PATH, 'Target server path differs from host')
    server = copy.deepcopy(source['streamSettings']['xhttpSettings'])
    require(server.get('path') == source_host.get('path'), 'Source host/server paths differ')
    source_extra = source_host.get('xhttpExtraParams')
    client = parsed_extra(source_extra)
    validate_source(server, client)
    # Client extras must not secretly override the preserved domain/path.
    require(not any(field in client for field in ('host', 'path', 'downloadSettings')),
            'Source client extra overrides endpoint; manual review required')
    server['path'] = TARGET_PATH
    config = copy.deepcopy(target_profile['config'])
    by_tag(config, TARGET_TAG)['streamSettings']['xhttpSettings'] = server
    wanted_host = copy.deepcopy(target_host)
    wanted_host['xhttpExtraParams'] = copy.deepcopy(source_extra)
    validation = copy.deepcopy(config)
    for inbound in validation['inbounds']:
        if isinstance(inbound.get('settings'), dict) and 'clients' in inbound['settings']:
            inbound['settings']['clients'] = []
    return config, wanted_host, validation


def state_directory(directory=None):
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix=PREFIX, dir='/private/tmp'))
    else:
        directory = Path(directory)
        require(directory.resolve().is_relative_to(Path('/private/tmp')), 'Private state must stay under /private/tmp')
        directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def prepare(directory=None):
    snapshot = snapshots()
    config, host, validation = build_candidate(snapshot)
    directory = state_directory(directory)
    private_write(directory / 'before-profiles.json', snapshot['profiles'])
    private_write(directory / 'before-hosts.json', snapshot['hosts'])
    private_write(directory / 'before-node-bindings.json', node_bindings(snapshot['nodes']))
    private_write(directory / 'candidate-profile.json', config)
    private_write(directory / 'candidate-host-extra.json', {'xhttpExtraParams': host['xhttpExtraParams']})
    private_write(directory / 'validation-profile.json', validation)
    private_write(directory / 'manifest.json', {'version': 1, 'target_host': TARGET_HOST,
        'target_profile': TARGET_PROFILE, 'source_host': SOURCE_HOST, 'source_profile': SOURCE_PROFILE,
        'hashes': {name: digest(json.loads((directory / name).read_text())) for name in (
            'before-profiles.json', 'before-hosts.json', 'before-node-bindings.json',
            'candidate-profile.json', 'candidate-host-extra.json', 'validation-profile.json')}})
    print(f'Prepared private state: {directory}')
    print('Only selected CDN XHTTP settings and client extras may change; no apply performed.')
    return directory


def load_state(directory):
    directory = Path(directory)
    require(directory.resolve().is_relative_to(Path('/private/tmp')) and not directory.is_symlink(),
            'Private state must stay under /private/tmp')
    require(stat.S_IMODE(directory.stat().st_mode) == 0o700, 'Private state directory must have mode 0700')
    def load(name):
        path = directory / name
        require(not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600, 'Private state file must have mode 0600')
        return json.loads(path.read_text())
    manifest = load('manifest.json')
    require(manifest.get('version') == 1 and manifest.get('target_host') == TARGET_HOST
            and manifest.get('target_profile') == TARGET_PROFILE and manifest.get('source_host') == SOURCE_HOST
            and manifest.get('source_profile') == SOURCE_PROFILE, 'State identity mismatch')
    require(isinstance(manifest.get('hashes'), dict) and set(manifest['hashes']) == {
        'before-profiles.json', 'before-hosts.json', 'before-node-bindings.json',
        'candidate-profile.json', 'candidate-host-extra.json', 'validation-profile.json'}, 'Unexpected state files')
    data = {name: load(name) for name in manifest['hashes']}
    require(all(digest(value) == manifest['hashes'][name] for name, value in data.items()), 'Private state hash mismatch')
    # Rebuild the intent rather than trusting a tampered manifest/candidate.
    profiles, hosts = data['before-profiles.json'], data['before-hosts.json']
    bindings = data['before-node-bindings.json']
    nodes = [{'uuid': uuid, 'address': value['address'], 'port': value['port'], 'configProfile': {
        'activeConfigProfileUuid': value['profile'], 'activeInbounds': [{'uuid': item} for item in value['inbounds']]}}
        for uuid, value in bindings.items()]
    expected, expected_host, validation = build_candidate({'profiles': profiles, 'hosts': hosts, 'nodes': nodes})
    require(expected == data['candidate-profile.json']
            and {'xhttpExtraParams': expected_host['xhttpExtraParams']} == data['candidate-host-extra.json']
            and validation == data['validation-profile.json'], 'Candidate is outside allowed migration scope')
    data['wanted_host'] = expected_host
    return data


def inspect_state(state, live):
    before_profiles = state['before-profiles.json']
    before_hosts = state['before-hosts.json']
    require({p['uuid'] for p in before_profiles} == {p['uuid'] for p in live['profiles']}, 'Profile set changed')
    require({h['uuid'] for h in before_hosts} == {h['uuid'] for h in live['hosts']}, 'Host set changed')
    for before in before_profiles:
        now = unique(live['profiles'], before['uuid'], 'Profile')
        require(now['name'] == before['name'], 'Profile name changed')
        require(inbound_bindings(now) == inbound_bindings(before), 'Profile inbound bindings changed')
        if before['uuid'] != TARGET_PROFILE:
            require(digest(now['config']) == digest(before['config']), 'An unrelated profile changed')
    target_before = unique(before_profiles, TARGET_PROFILE, 'Target profile')
    target_now = unique(live['profiles'], TARGET_PROFILE, 'Target profile')
    current = digest(target_now['config'])
    old, wanted = digest(target_before['config']), digest(state['candidate-profile.json'])
    require(current in (old, wanted), 'Target profile changed concurrently or outside allowed field')
    # This exact whole-config comparison also preserves all direct inbounds,
    # outbounds/routing and unrelated server settings.
    for before in before_hosts:
        now = unique(live['hosts'], before['uuid'], 'Host')
        if before['uuid'] != TARGET_HOST:
            require(digest(now) == digest(before), 'An unrelated host changed')
    target_host_now = unique(live['hosts'], TARGET_HOST, 'Target host')
    target_host_before = unique(before_hosts, TARGET_HOST, 'Target host')
    actual_host = digest(host_state(target_host_now))
    old_host = digest(host_state(target_host_before))
    wanted_host = digest(host_state(state['wanted_host']))
    require(actual_host in (old_host, wanted_host), 'Target host changed concurrently or outside client extras')
    require(node_bindings(live['nodes']) == state['before-node-bindings.json'], 'Node bindings changed')
    profile_done = current == wanted
    host_done = actual_host == wanted_host
    require(not host_done or profile_done or old_host == wanted_host,
            'Host-only partial state requires manual review')
    return 'complete' if profile_done and host_done else 'profile_applied' if profile_done else 'prepared'


def audit(directory):
    state = load_state(directory)
    stage = inspect_state(state, snapshots())
    print(f'Guarded stage: {stage}. Other profiles, direct inbounds, hosts and node bindings unchanged.')
    return stage


def apply(directory, *, confirm=False):
    require(confirm is True, 'Apply requires explicit confirmation after CDN/DNS/TLS validation')
    state = load_state(directory)
    stage = inspect_state(state, snapshots())
    if stage == 'prepared':
        # Exact documented PATCH contract; never send node/squad mutations.
        api('PATCH', '/config-profiles', {'uuid': TARGET_PROFILE, 'config': state['candidate-profile.json']})
        stage = inspect_state(state, snapshots())
        require(stage in ('profile_applied', 'complete'), 'Profile PATCH was not confirmed by a fresh GET')
    if stage == 'profile_applied':
        api('PATCH', '/hosts', {'uuid': TARGET_HOST,
            'xhttpExtraParams': state['candidate-host-extra.json']['xhttpExtraParams']})
    # A failed/ambiguous PATCH returns to the caller without repeating it.
    # A later explicit apply re-reads and resumes only the verified saved state.
    stage = inspect_state(state, snapshots())
    require(stage == 'complete', 'Migration not fully confirmed; inspect saved state before retry')
    print('Only selected CDN transport settings/client extras applied; audit passed.')
    return stage


def rollback(directory, *, confirm=False):
    require(confirm is True, 'Rollback requires explicit confirmation after inspecting CDN/DNS state')
    state = load_state(directory)
    current = snapshots()
    inspect_state(state, current)
    original_profile = unique(state['before-profiles.json'], TARGET_PROFILE, 'Target profile')
    original_host = unique(state['before-hosts.json'], TARGET_HOST, 'Target host')
    if digest(host_state(unique(current['hosts'], TARGET_HOST, 'Target host'))) != digest(host_state(original_host)):
        api('PATCH', '/hosts', {'uuid': TARGET_HOST,
            'xhttpExtraParams': original_host.get('xhttpExtraParams')})
        current = snapshots()
        inspect_state(state, current)
        require(digest(host_state(unique(current['hosts'], TARGET_HOST, 'Target host'))) == digest(host_state(original_host)),
                'Host rollback was not confirmed by a fresh GET')
    if digest(unique(current['profiles'], TARGET_PROFILE, 'Target profile')['config']) != digest(original_profile['config']):
        api('PATCH', '/config-profiles', {'uuid': TARGET_PROFILE, 'config': original_profile['config']})
    current = snapshots()
    inspect_state(state, current)
    # Stage names describe forward migration. Compare originals explicitly for
    # a legitimate all-no-op candidate or an already-restored component.
    require(digest(unique(current['profiles'], TARGET_PROFILE, 'Target profile')['config']) == digest(original_profile['config'])
            and digest(host_state(unique(current['hosts'], TARGET_HOST, 'Target host'))) == digest(host_state(original_host)),
            'Rollback not fully confirmed; GET-only audit before retry')
    print('Only selected CDN profile/settings and client extras restored. DNS/CDN resource not changed.')
    return 'restored'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'audit', 'apply', 'rollback'))
    parser.add_argument('directory', type=Path, nargs='?')
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            require(not args.confirm, 'Preparation is GET-only; confirmation is for apply')
            prepare(args.directory)
        else:
            require(args.directory is not None, 'Provide the private prepared state directory')
            if args.command == 'audit':
                require(not args.confirm, 'Audit is GET-only')
                audit(args.directory)
            elif args.command == 'apply':
                apply(args.directory, confirm=args.confirm)
            else:
                rollback(args.directory, confirm=args.confirm)
    except GuardError as exc:
        print(f'Guard stopped: {exc}', file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        # Do not print response bodies, profile configs, environment or tokens.
        code = f' HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else ''
        print(f'Operation not confirmed: {type(exc).__name__}{code}. Re-run GET-only audit before retry.', file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
