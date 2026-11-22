"""One additive DE4 gRPC/TLS443 transport; no users or subscriptions.

Default is a pure plan. prepare makes only infrastructure GETs and saves a
private, minimal rollback projection. Each confirmed mutation has a durable
intent and a fresh whole-scope GET audit. Never creates a node/profile or
retries an unresolved write. Nginx and runtime validation are separate stages.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import sys
import uuid


ROOT = Path(__file__).resolve().parents[2]
IP, DOMAIN = '194.48.250.102', 'deeee1.wayspn.com'
PROFILE = '60714af0-bc08-44e5-bbb3-9aa0d2dedb53'
NODE = 'f87b14cf-438a-4c61-acc1-7522491c05ae'
SOURCE_HOST = '421b6c60-88e2-4aca-980f-e5cf8114794d'
TAG, SERVICE, PORT = 'ordinary-grpc-tls-de4-test', 'grpc-de4-tls-test', 10089
OLD_TAGS = {'ordinary-grpc-reality-deeee1', 'ordinary-ws-deeee1', 'vless-xhttp-yandex-deeee1'}
REMARK = '🇩🇪 Германия • gRPC 443 • ТЕСТ'
DEFAULT_STATE = Path('/private/tmp/wayspn-grpc-tls-de4-20261031')
KINDS = ('profiles', 'nodes', 'hosts', 'squads')
ACTIONS = ('prepare', 'audit', 'apply-profile', 'bind-node', 'create-host', 'bind-squads', 'enable',
           'rollback-disable', 'rollback-squads', 'rollback-node', 'rollback-profile')
MAX_BYTES = 8_000_000


class GuardError(ValueError): pass


def require(value):
    if not value:
        raise GuardError('Scoped DE4 gRPC/TLS prerequisites changed; private details suppressed.')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise GuardError('Only fixed, confirmed DE4 infrastructure stages are accepted.')


def canonical(value):
    try:
        require(isinstance(value, str) and str(uuid.UUID(value)) == value)
    except (ValueError, TypeError, AttributeError):
        raise GuardError('Infrastructure identity prerequisite failed.') from None
    return value


def ids(rows):
    require(isinstance(rows, list))
    result = [canonical(row.get('uuid') if isinstance(row, dict) else row) for row in rows]
    require(len(result) == len(set(result)))
    return result


def one(rows, uid):
    found = [row for row in rows if row.get('uuid') == uid]
    require(len(found) == 1)
    return found[0]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def host_state(row):
    return {key: value for key, value in row.items() if key not in {'updatedAt', 'viewPosition'}}


def node_state(row):
    result = {key: copy.deepcopy(row.get(key)) for key in ('uuid', 'name', 'address', 'port', 'countryCode', 'tags')}
    binding = row.get('configProfile') or {}
    result['configProfile'] = {'activeConfigProfileUuid': binding.get('activeConfigProfileUuid'),
                              'activeInbounds': ids(binding.get('activeInbounds'))}
    return result


def signature(kind, row):
    if kind == 'profiles':
        value = {key: row.get(key) for key in ('uuid', 'name', 'config', 'inbounds')}
    elif kind == 'nodes':
        value = node_state(row)
    elif kind == 'hosts':
        value = host_state(row)
    else:
        value = {'uuid': row['uuid'], 'name': row.get('name'), 'inbounds': sorted(ids(row.get('inbounds')))}
    return digest(value)


def api(method, path, body=None):
    allowed = ((method == 'GET' and path in {'/hosts', '/nodes', '/config-profiles', '/internal-squads'} and body is None)
               or (method == 'PATCH' and path in {'/hosts', '/nodes', '/config-profiles', '/internal-squads'} and isinstance(body, dict))
               or (method == 'POST' and path == '/hosts' and isinstance(body, dict)))
    require(allowed)
    # Fixed HTTPS origin, verified CA, no redirects and credential redaction are
    # inherited; importing the adapter is deferred until an explicit action.
    adapter = runpy.run_path(str(ROOT / 'ops/direct-fi/manage.py'))['api']
    return adapter(method, path, body)


def snapshot():
    return {'profiles': api('GET', '/config-profiles')['configProfiles'], 'nodes': api('GET', '/nodes'),
            'hosts': api('GET', '/hosts'), 'squads': api('GET', '/internal-squads')['internalSquads']}


def _candidate(profile, source, squads, scope):
    config = copy.deepcopy(profile['config'])
    config['inbounds'].append({'tag': TAG, 'listen': '127.0.0.1', 'port': PORT, 'protocol': 'vless',
        'settings': {'clients': [], 'decryption': 'none'},
        'streamSettings': {'network': 'grpc', 'security': 'none', 'grpcSettings': {'serviceName': SERVICE}},
        'sniffing': {'enabled': True, 'destOverride': ['http', 'tls']}})
    host = {key: copy.deepcopy(source[key]) for key in ('fingerprint', 'isHidden', 'serverDescription',
            'mihomoIpVersion', 'excludeFromSubscriptionTypes', 'internalSquads') if key in source}
    if type(source.get('shuffleHost')) is bool:
        host['shuffleHost'] = source['shuffleHost']
    host.update(remark=REMARK, address=DOMAIN, port=443, path=SERVICE, sni=DOMAIN, host=DOMAIN,
        alpn='h2', securityLayer='TLS', isDisabled=True, nodes=[NODE], tags=['DE4_GRPC_TLS_TEST'],
        xrayJsonTemplateUuid=None, xhttpExtraParams=None, muxParams=None, sockoptParams=None,
        finalMask=None, pinnedPeerCertSha256=None, verifyPeerCertByName=None, mihomoX25519=False,
        keepSniBlank=False, overrideSniFromAddress=False)
    host.setdefault('fingerprint', 'chrome')
    host.setdefault('internalSquads', {'mode': 'EXCLUDE', 'squads': []})
    return {'config': config, 'new_host': host,
            'squads': {uid: copy.deepcopy(row['inbounds']) for uid, row in squads.items()}, 'scope': copy.deepcopy(scope)}


def build(live):
    require(isinstance(live, dict) and set(live) == set(KINDS))
    for rows in live.values():
        ids(rows)
    profile, node, source = one(live['profiles'], PROFILE), one(live['nodes'], NODE), one(live['hosts'], SOURCE_HOST)
    metadata = profile.get('inbounds')
    old_ids = ids(metadata)
    config = profile.get('config') or {}
    inbounds = config.get('inbounds')
    require(len(metadata) == 3 and {row.get('tag') for row in metadata} == OLD_TAGS
            and isinstance(inbounds, list) and len(inbounds) == 3
            and {row.get('tag') for row in inbounds} == OLD_TAGS
            and {row.get('streamSettings', {}).get('network') for row in inbounds} == {'grpc', 'ws', 'xhttp'})
    require(node.get('address') == IP and node.get('port') == 2222
            and node_state(node)['configProfile']['activeConfigProfileUuid'] == PROFILE
            and set(node_state(node)['configProfile']['activeInbounds']) == set(old_ids))
    bound_nodes = [row for row in live['nodes'] if (row.get('configProfile') or {}).get('activeConfigProfileUuid') == PROFILE]
    require(len(bound_nodes) == 1 and bound_nodes[0]['uuid'] == NODE)
    old_hosts = [row for row in live['hosts'] if (row.get('inbound') or {}).get('configProfileUuid') == PROFILE]
    require(len(old_hosts) == 3 and {row['inbound']['configProfileInboundUuid'] for row in old_hosts} == set(old_ids))
    source_id = source.get('inbound', {}).get('configProfileInboundUuid')
    definition = one(metadata, source_id)
    selected = [row for row in inbounds if row.get('tag') == definition.get('tag')]
    require(len(selected) == 1)
    grpc = selected[0]
    require(source.get('inbound', {}).get('configProfileUuid') == PROFILE
            and source.get('address') == IP and source.get('port') == 2083 and source.get('sni') == DOMAIN
            and source.get('path') == 'grpc' and source.get('isDisabled') is False
            and ids(source.get('nodes')) == [NODE]
            and grpc.get('protocol') == 'vless' and grpc.get('port') == 2083
            and grpc.get('streamSettings', {}).get('network') == 'grpc'
            and grpc['streamSettings'].get('security') == 'reality'
            and grpc['streamSettings'].get('grpcSettings', {}).get('serviceName') == 'grpc')
    require(all(row.get('settings') == {'clients': [], 'decryption': 'none'} for row in inbounds))
    require(all(row.get('port') != PORT and row.get('tag') != TAG
                and row.get('streamSettings', {}).get('grpcSettings', {}).get('serviceName') != SERVICE for row in inbounds)
            and not any(row.get('address') == DOMAIN and row.get('port') == 443 and row.get('path') == SERVICE for row in live['hosts']))
    mirrored = {row['uuid']: {'name': row.get('name'), 'inbounds': ids(row.get('inbounds'))}
                for row in live['squads'] if source_id in ids(row.get('inbounds'))}
    require(mirrored)
    scope = {'old_inbounds': old_ids, 'old_hosts': ids(old_hosts),
             'baseline_ids': {kind: ids(live[kind]) for kind in KINDS}}
    return _candidate(profile, source, mirrored, scope)


def no_symlinks(path):
    require(path.is_absolute() and not any(item.is_symlink() for item in (path, *path.parents)))


def directory(path):
    no_symlinks(path)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)


def read(path):
    directory(path.parent)
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_uid, row.st_mode, row.st_nlink)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and 0 < info.st_size <= MAX_BYTES)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) == info.st_size and identity(os.fstat(fd)) == identity(info))
        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result)
                result[key] = value
            return result
        return json.loads(raw, object_pairs_hook=unique)
    finally:
        os.close(fd)


def save(state, name, value):
    directory(state)
    path = state / name
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    require(len(raw) <= MAX_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(state):
    no_symlinks(state)
    require(not state.exists())
    if state.parent != Path('/private/tmp'):
        directory(state.parent)
    live = snapshot()
    candidate = build(live)
    source = one(live['hosts'], SOURCE_HOST)
    mirrored = {uid: {'name': one(live['squads'], uid).get('name'), 'inbounds': values}
                for uid, values in candidate['squads'].items()}
    hashes = {kind: {row['uuid']: signature(kind, row) for row in live[kind]
                    if not ((kind == 'profiles' and row['uuid'] == PROFILE)
                            or (kind == 'nodes' and row['uuid'] == NODE)
                            or (kind == 'squads' and row['uuid'] in mirrored))} for kind in KINDS}
    before = {'profile': copy.deepcopy(one(live['profiles'], PROFILE)),
              'targetnode': node_state(one(live['nodes'], NODE)), 'sourcehost': copy.deepcopy(source),
              'squads': mirrored, 'hashes': hashes, 'scope': copy.deepcopy(candidate['scope'])}
    state.mkdir(mode=0o700, exist_ok=False)
    save(state, 'before.json', before)
    save(state, 'candidate.json', candidate)
    save(state, 'validation-profile.json', candidate['config'])
    save(state, 'manifest.json', {'format': 1, 'ip': IP, 'domain': DOMAIN, 'profile': PROFILE, 'node': NODE,
        'sourceHost': SOURCE_HOST, 'beforeHash': digest(before), 'candidateHash': digest(candidate)})
    return {'prepared': True, 'networkWrites': 0, 'state': str(state), 'newInboundCount': 1, 'newHostsInitiallyDisabled': True}


def load(state):
    before, candidate, manifest = (read(state / name) for name in ('before.json', 'candidate.json', 'manifest.json'))
    require(manifest == {'format': 1, 'ip': IP, 'domain': DOMAIN, 'profile': PROFILE, 'node': NODE,
        'sourceHost': SOURCE_HOST, 'beforeHash': digest(before), 'candidateHash': digest(candidate)}
        and candidate == _candidate(before['profile'], before['sourcehost'], before['squads'], before['scope'])
        and read(state / 'validation-profile.json') == candidate['config'])
    return before, candidate


def intent_matches(state, name, method, endpoint, body):
    path = state / ('intent-' + name + '.json')
    require(path.exists() and read(path) == {'method': method, 'endpoint': endpoint, 'body': body})


def _audit(state, live=None):
    before, candidate = load(state)
    live = snapshot() if live is None else live
    require(set(live) == set(KINDS))
    current_ids = {kind: ids(live[kind]) for kind in KINDS}
    scope = before['scope']
    for kind in ('profiles', 'nodes', 'squads'):
        require(set(current_ids[kind]) == set(scope['baseline_ids'][kind]))
    baseline_hosts = set(scope['baseline_ids']['hosts'])
    require(baseline_hosts <= set(current_ids['hosts']) and len(set(current_ids['hosts']) - baseline_hosts) <= 1)
    for kind in KINDS:
        for uid, expected in before['hashes'][kind].items():
            require(signature(kind, one(live[kind], uid)) == expected)
    profile, node = one(live['profiles'], PROFILE), one(live['nodes'], NODE)
    require(profile.get('name') == before['profile'].get('name'))
    applied = profile.get('config') == candidate['config']
    require(applied or profile.get('config') == before['profile']['config'])
    new = [row for row in profile.get('inbounds', []) if row.get('tag') == TAG]
    old_ids = scope['old_inbounds']
    require((len(new) == 1 and set(ids(profile['inbounds'])) == set(old_ids) | {new[0]['uuid']}) if applied
            else (not new and set(ids(profile['inbounds'])) == set(old_ids)))
    for old in before['profile']['inbounds']:
        require(one(profile['inbounds'], old['uuid']) == old)
    inbound = canonical(new[0]['uuid']) if new else None
    if applied:
        intent_matches(state, 'profile', 'PATCH', '/config-profiles', {'uuid': PROFILE, 'config': candidate['config']})
    actual_node, old_node = node_state(node), before['targetnode']
    require({key: value for key, value in actual_node.items() if key != 'configProfile'}
            == {key: value for key, value in old_node.items() if key != 'configProfile'})
    require(actual_node['configProfile']['activeConfigProfileUuid'] == PROFILE)
    active = actual_node['configProfile']['activeInbounds']
    original = old_node['configProfile']['activeInbounds']
    bound = inbound is not None and set(active) == set(original) | {inbound}
    require(set(active) == set(original) or bound)
    if bound:
        intent_matches(state, 'node', 'PATCH', '/nodes', {'uuid': NODE, 'configProfile': {
            'activeConfigProfileUuid': PROFILE, 'activeInbounds': original + [inbound]}})
    added = [row for row in live['hosts'] if row['uuid'] not in baseline_hosts]
    host = added[0] if added else None
    if host:
        receipt = state / 'host-receipt.json'
        if receipt.exists():
            require(read(receipt) == {'uuid': host['uuid']})
        expected = {**candidate['new_host'], 'inbound': {'configProfileUuid': PROFILE, 'configProfileInboundUuid': inbound}}
        if not bound:
            require(applied and host.get('isDisabled') is True)
            intent_matches(state, 'rollback-node', 'PATCH', '/nodes', {'uuid': NODE, 'configProfile': old_node['configProfile']})
        require(all(host.get(key) == value for key, value in expected.items() if key != 'isDisabled')
                and type(host.get('isDisabled')) is bool and host.get('mapper') in (None, {}))
        intent_matches(state, 'host', 'POST', '/hosts', expected)
        if host['isDisabled'] is False:
            intent_matches(state, 'enable', 'PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': False})
    squad_bound = []
    for uid, old in before['squads'].items():
        current = one(live['squads'], uid)
        require(current.get('name') == old['name'])
        current_members = ids(current.get('inbounds'))
        if set(current_members) != set(old['inbounds']):
            require(inbound is not None and set(current_members) == set(old['inbounds']) | {inbound})
            intent_matches(state, 'squad-' + uid, 'PATCH', '/internal-squads', {'uuid': uid, 'inbounds': old['inbounds'] + [inbound]})
            squad_bound.append(uid)
    return {'profile_applied': applied, 'node_bound': bound, 'new_inbound_uuid': inbound,
            'host_uuid': host['uuid'] if host else None, 'squads_bound': sorted(squad_bound),
            'enabled': host is not None and host['isDisabled'] is False,
            'node_ready': node.get('isConnected') is True and node.get('isDisabled') is False and not node.get('lastStatusMessage')}, live, before, candidate


def audit(state, live=None):
    return _audit(state, live)[0]


def write_once(state, name, method, endpoint, body, done, verified):
    path = state / ('intent-' + name + '.json')
    intent = {'method': method, 'endpoint': endpoint, 'body': body}
    if path.exists():
        require(read(path) == intent and done)
        return
    require(not done)
    save(state, path.name, intent)
    response = api(method, endpoint, body)
    if name == 'host' and isinstance(response, dict) and response.get('uuid') is not None:
        save(state, 'host-receipt.json', {'uuid': canonical(response['uuid'])})
    require(verified(audit(state)))


def apply_stage(state, action, *, tls_verified=False):
    status, live, before, candidate = _audit(state)
    inbound, host = status['new_inbound_uuid'], status['host_uuid']
    if action == 'apply-profile':
        write_once(state, 'profile', 'PATCH', '/config-profiles', {'uuid': PROFILE, 'config': candidate['config']},
                   status['profile_applied'], lambda current: current['profile_applied'])
    elif action == 'bind-node':
        require(status['profile_applied'])
        body = {'uuid': NODE, 'configProfile': {'activeConfigProfileUuid': PROFILE,
                'activeInbounds': before['targetnode']['configProfile']['activeInbounds'] + [inbound]}}
        write_once(state, 'node', 'PATCH', '/nodes', body, status['node_bound'], lambda current: current['node_bound'])
    elif action == 'create-host':
        require(status['node_bound'])
        body = {**candidate['new_host'], 'inbound': {'configProfileUuid': PROFILE, 'configProfileInboundUuid': inbound}}
        write_once(state, 'host', 'POST', '/hosts', body, host is not None, lambda current: current['host_uuid'] is not None)
    elif action == 'bind-squads':
        require(host is not None and status['node_ready'])
        for uid, old in before['squads'].items():
            current = audit(state)
            write_once(state, 'squad-' + uid, 'PATCH', '/internal-squads', {'uuid': uid, 'inbounds': old['inbounds'] + [inbound]},
                       uid in current['squads_bound'], lambda observed, uid=uid: uid in observed['squads_bound'])
    elif action == 'enable':
        require(tls_verified is True and host is not None and status['node_ready']
                and set(status['squads_bound']) == set(before['squads']))
        write_once(state, 'enable', 'PATCH', '/hosts', {'uuid': host, 'isDisabled': False}, status['enabled'], lambda current: current['enabled'])
    elif action == 'rollback-disable':
        if host is not None and status['enabled']:
            write_once(state, 'rollback-disable', 'PATCH', '/hosts', {'uuid': host, 'isDisabled': True}, False,
                       lambda current: not current['enabled'])
    elif action == 'rollback-squads':
        require(not status['enabled'])
        for uid in status['squads_bound']:
            write_once(state, 'rollback-squad-' + uid, 'PATCH', '/internal-squads',
                       {'uuid': uid, 'inbounds': before['squads'][uid]['inbounds']}, False,
                       lambda observed, uid=uid: uid not in observed['squads_bound'])
    elif action == 'rollback-node':
        require(not status['enabled'] and not status['squads_bound'])
        if status['node_bound']:
            write_once(state, 'rollback-node', 'PATCH', '/nodes', {'uuid': NODE, 'configProfile': before['targetnode']['configProfile']},
                       False, lambda current: not current['node_bound'])
    elif action == 'rollback-profile':
        # The fixed adapter deliberately implements no unreviewed DELETE route.
        # Remove ONLY the owned new host in the panel first; then this stage can
        # safely remove its inactive inbound without leaving a dangling host.
        require(host is None and not status['node_bound'] and not status['squads_bound'])
        if status['profile_applied']:
            write_once(state, 'rollback-profile', 'PATCH', '/config-profiles', {'uuid': PROFILE, 'config': before['profile']['config']},
                       False, lambda current: not current['profile_applied'])
    else:
        raise GuardError('Unknown fixed DE4 stage.')
    return audit(state)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('action', nargs='?', choices=ACTIONS)
        parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
        parser.add_argument('--confirm', action='store_true')
        parser.add_argument('--tls-verified', action='store_true')
        args = parser.parse_args(argv)
        if args.action is None:
            result = {'plan_only': True, 'network': False, 'filesystem_writes': False,
                'ip': IP, 'domain': DOMAIN, 'new_inbound': '127.0.0.1:10089 gRPC', 'service_name': SERVICE,
                'new_host_initially_disabled': True, 'creates_node_or_profile': False,
                'user_or_subscription_access': False, 'nginx_external_stage': True,
                'rollback_host_removal': 'Remove only the owned host in the panel before rollback-node/profile.',
                'stages': list(ACTIONS)}
        elif args.action == 'prepare':
            result = prepare(args.state)
        elif args.action == 'audit':
            result = audit(args.state)
        else:
            require(args.confirm is True)
            result = apply_stage(args.state, args.action, tls_verified=args.tls_verified)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        print('DE4 stage stopped; private state and raw responses suppressed. Audit before retry.', file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == '__main__':
    raise SystemExit(main())
