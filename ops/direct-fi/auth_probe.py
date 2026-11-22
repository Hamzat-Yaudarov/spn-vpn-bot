"""Owner-confirmed, fixed-Finland authenticated HTTPS probes.

Default is a pure plan. prepare reads PRIVATE infrastructure and owner-only
lookup snapshots; it never requests users/subscriptions or changes accounts.
Only the three reviewed FI endpoints are constructed. run needs a trusted,
SHA256-pinned local Xray executable and explicit owner confirmation. No direct
fallback, TUN, provider writes, subscription links or identity output exist.
Preparation uses cryptography; execution uses only Python's standard library.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid


OWNER_ID = 6910097562
IP, ORIGIN, CDN = '92.42.102.103', 'fi103.wayspn.com', 'cdn-fi103.wayspn.com'
PROFILE_NAME, NODE_NAME = 'DIRECT_FI_102_103', 'node-direct-fi-102.103'
TAGS = {'grpc': 'ordinary-grpc-reality-fi', 'ws': 'ordinary-ws-fi', 'xhttp': 'vless-xhttp-yandex-fi'}
WS_PATH, XHTTP_PATH = '/direct/ws/', '/assets/app/'
URL, CURL = 'https://www.gstatic.com/generate_204', '/usr/bin/curl'
ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8'}
MAX_INPUT = 16_000_000
XHTTP_REQUIRED = {'uplinkHTTPMethod', 'uplinkDataPlacement', 'uplinkDataKey', 'scMaxEachPostBytes'}
XHTTP_OPTIONAL = {'noGRPCHeader', 'noSSEHeader', 'xPaddingBytes', 'scMinPostsIntervalMs',
                  'scMaxBufferedPosts', 'scStreamUpServerSecs', 'xPaddingObfsMode',
                  'xPaddingPlacement', 'xPaddingKey', 'xPaddingHeader'}
FINGERPRINTS = {'chrome', 'firefox', 'safari', 'ios', 'android', 'edge', 'random', 'randomized'}


class ProbeError(ValueError): pass


def require(value):
    if not value:
        raise ProbeError('Fixed owner-only FI probe prerequisites failed; private details suppressed.')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProbeError('Only the fixed owner-confirmed FI probe options are accepted.')


def canonical(value):
    require(isinstance(value, str))
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ProbeError('Fixed owner-only FI probe identity prerequisites failed.') from None
    require(normalized == value)
    return value


def date(value):
    require(isinstance(value, str))
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ProbeError('Fixed owner-only FI probe date prerequisites failed.') from None
    require(result.tzinfo is not None)
    return result


def ids(rows):
    require(isinstance(rows, list))
    result = [canonical(row.get('uuid') if isinstance(row, dict) else row) for row in rows]
    require(len(result) == len(set(result)))
    return result


def one(rows, predicate):
    found = [row for row in rows if predicate(row)]
    require(len(found) == 1)
    return found[0]


def parsed(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def xhttp_extra(value):
    value = parsed(value) if isinstance(value, str) else copy.deepcopy(value)
    require(isinstance(value, dict) and XHTTP_REQUIRED <= set(value)
            and set(value) <= XHTTP_REQUIRED | XHTTP_OPTIONAL
            and all(type(item) in {str, int, bool, float} for item in value.values()))
    require(value['uplinkHTTPMethod'] == 'GET' and value['uplinkDataPlacement'] == 'header'
            and value['uplinkDataKey'] == 'X-Transport-Data')
    bound = str(value['scMaxEachPostBytes']).split('-')
    require(len(bound) in {1, 2} and all(part.isdecimal() for part in bound)
            and 0 < int(bound[0]) <= int(bound[-1]) <= 2048)
    return value


def selected_owner(document, account_name=None):
    require(isinstance(document, dict) and document.get('kind') == 'owner-only-lookup-not-subscription-config'
            and document.get('ownerTelegramId') == OWNER_ID and isinstance(document.get('users'), list))
    rows = document['users']
    require(0 < len(rows) <= 100 and all(isinstance(row, dict)
            and type(row.get('telegramId')) in {int, str}
            and str(row['telegramId']) == str(OWNER_ID) for row in rows))
    active = [row for row in rows if row.get('status') == 'ACTIVE']
    if account_name is not None:
        require(isinstance(account_name, str) and 0 < len(account_name) <= 256)
        active = [row for row in active if row.get('username') == account_name]
    require(len(active) == 1 and date(active[0].get('expireAt')) > datetime.now(timezone.utc))
    canonical(active[0].get('vlessUuid'))
    ids(active[0].get('activeInternalSquads'))
    return active[0]


def fingerprint(host):
    value = host.get('fingerprint') or 'chrome'
    require(value in FINGERPRINTS)
    return value


def tls(host, name, alpn):
    require(host.get('alpn') in {None, '', alpn})
    return {'serverName': name, 'fingerprint': fingerprint(host), 'alpn': [alpn], 'allowInsecure': False}


def public_key(private):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    require(isinstance(private, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', private))
    raw = base64.urlsafe_b64decode(private + '=')
    require(len(raw) == 32)
    result = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(result).decode().rstrip('=')


def build(snapshot, owner_document, account_name=None):
    require(isinstance(snapshot, dict) and set(snapshot) == {'profiles', 'hosts', 'nodes', 'squads'})
    for rows in snapshot.values():
        ids(rows)
    owner = selected_owner(owner_document, account_name)
    profile = one(snapshot['profiles'], lambda row: row.get('name') == PROFILE_NAME)
    node = one(snapshot['nodes'], lambda row: row.get('name') == NODE_NAME)
    require(node.get('address') == IP and node.get('port') == 2222
            and node.get('isConnected') is True and node.get('isDisabled') is False
            and not node.get('lastStatusMessage'))
    metadata = profile.get('inbounds')
    inbound_ids = ids(metadata)
    require(len(metadata) == 3 and {row.get('tag') for row in metadata} == set(TAGS.values()))
    binding = node.get('configProfile') or {}
    require(binding.get('activeConfigProfileUuid') == profile['uuid']
            and set(ids(binding.get('activeInbounds'))) == set(inbound_ids))
    config_inbounds = (profile.get('config') or {}).get('inbounds')
    require(isinstance(config_inbounds, list) and len(config_inbounds) == 3
            and {row.get('tag') for row in config_inbounds} == set(TAGS.values()))
    hosts = [row for row in snapshot['hosts'] if (row.get('inbound') or {}).get('configProfileUuid') == profile['uuid']]
    require(len(hosts) == 3)
    user_squads = set(ids(owner['activeInternalSquads']))
    granted = set()
    for squad in snapshot['squads']:
        if squad['uuid'] in user_squads:
            granted.update(ids(squad.get('inbounds')))
    require(set(inbound_ids) <= granted)
    result = []
    for kind, tag in TAGS.items():
        metadata_row = one(metadata, lambda row: row.get('tag') == tag)
        inbound = one(config_inbounds, lambda row: row.get('tag') == tag)
        host = one(hosts, lambda row: (row.get('inbound') or {}).get('configProfileInboundUuid') == metadata_row['uuid'])
        require(host.get('isDisabled') is False and ids(host.get('nodes')) == [node['uuid']]
                and host.get('internalSquads') == {'mode': 'EXCLUDE', 'squads': []}
                and host.get('keepSniBlank') is not True and host.get('overrideSniFromAddress') is not True)
        # Complex renderers/overrides require an actual reviewed client export.
        for field in ('muxParams', 'sockoptParams', 'finalMask', 'xrayJsonTemplateUuid', 'mapper',
                      'pinnedPeerCertSha256', 'verifyPeerCertByName'):
            require(host.get(field) in (None, '', {}, []))
        require(inbound.get('protocol') == 'vless'
                and inbound.get('settings') == {'clients': [], 'decryption': 'none'})
        stream = inbound.get('streamSettings') or {}
        require(stream.get('network') == kind)
        address, port = (IP, 2083) if kind == 'grpc' else (ORIGIN if kind == 'ws' else CDN, 443)
        require(host.get('address') == address and host.get('port') == port
                and host.get('sni') == (ORIGIN if kind != 'xhttp' else CDN))
        if kind == 'grpc':
            reality = stream.get('realitySettings') or {}
            require(inbound.get('port') == 2083 and stream.get('security') == 'reality'
                    and (stream.get('grpcSettings') or {}).get('serviceName') == host.get('path') == 'grpc'
                    and reality.get('serverNames') == [ORIGIN] and reality.get('target') == '127.0.0.1:443'
                    and host.get('host') in {None, ''}
                    and str(host.get('securityLayer') or 'DEFAULT').upper() in {'DEFAULT', 'REALITY'}
                    and host.get('alpn') in {None, '', 'h2'})
            short_ids = reality.get('shortIds')
            require(isinstance(short_ids, list) and len(short_ids) == 1
                    and isinstance(short_ids[0], str) and re.fullmatch(r'[0-9a-f]{16}', short_ids[0]))
            client_stream = {'network': 'grpc', 'security': 'reality', 'grpcSettings': {'serviceName': 'grpc'},
                'realitySettings': {'serverName': ORIGIN, 'fingerprint': fingerprint(host),
                    'publicKey': public_key(reality.get('privateKey')), 'shortId': short_ids[0]}}
        else:
            path, local_port = (WS_PATH, 10088) if kind == 'ws' else (XHTTP_PATH, 10085)
            require(inbound.get('listen') == '127.0.0.1' and inbound.get('port') == local_port
                    and stream.get('security') == 'none' and host.get('path') == path
                    and host.get('host') == address and host.get('securityLayer') == 'TLS')
            client_stream = {'network': kind, 'security': 'tls',
                             'tlsSettings': tls(host, address, 'http/1.1' if kind == 'ws' else 'h2')}
            if kind == 'ws':
                require((stream.get('wsSettings') or {}).get('path') == WS_PATH)
                client_stream['wsSettings'] = {'path': WS_PATH, 'headers': {'Host': ORIGIN}}
            else:
                server = stream.get('xhttpSettings') or {}
                require(server.get('mode') == 'packet-up' and server.get('path') == XHTTP_PATH
                        and server.get('serverMaxHeaderBytes') == 8192 and server.get('host') in {None, ''})
                xhttp_extra({key: server.get(key) for key in XHTTP_REQUIRED})
                client_stream['xhttpSettings'] = {'mode': 'packet-up', 'path': XHTTP_PATH, 'host': CDN,
                                                'extra': xhttp_extra(host.get('xhttpExtraParams'))}
        outbound = {'tag': 'fi-auth-' + kind, 'protocol': 'vless',
            'settings': {'vnext': [{'address': address, 'port': port,
                                   'users': [{'id': owner['vlessUuid'], 'encryption': 'none'}]}]},
            'streamSettings': client_stream}
        validate_outbound(kind, outbound)
        result.append({'kind': kind, 'host_uuid': host['uuid'], 'outbound': outbound})
    return result


def validate_outbound(kind, value):
    require(kind in TAGS and isinstance(value, dict)
            and set(value) == {'tag', 'protocol', 'settings', 'streamSettings'}
            and value['tag'] == 'fi-auth-' + kind and value['protocol'] == 'vless')
    settings, stream = value['settings'], value['streamSettings']
    require(isinstance(settings, dict) and set(settings) == {'vnext'} and isinstance(settings['vnext'], list)
            and len(settings['vnext']) == 1 and isinstance(stream, dict))
    server = settings['vnext'][0]
    address, port = (IP, 2083) if kind == 'grpc' else (ORIGIN if kind == 'ws' else CDN, 443)
    require(set(server) == {'address', 'port', 'users'} and server['address'] == address and server['port'] == port
            and isinstance(server['users'], list) and len(server['users']) == 1
            and set(server['users'][0]) == {'id', 'encryption'} and server['users'][0]['encryption'] == 'none')
    canonical(server['users'][0]['id'])
    require(stream.get('network') == kind)
    if kind == 'grpc':
        require(set(stream) == {'network', 'security', 'grpcSettings', 'realitySettings'}
                and stream['security'] == 'reality' and stream['grpcSettings'] == {'serviceName': 'grpc'})
        reality = stream['realitySettings']
        require(set(reality) == {'serverName', 'fingerprint', 'publicKey', 'shortId'}
                and reality['serverName'] == ORIGIN and reality['fingerprint'] in FINGERPRINTS
                and re.fullmatch(r'[A-Za-z0-9_-]{43}', reality['publicKey'])
                and re.fullmatch(r'[0-9a-f]{16}', reality['shortId']))
    else:
        key = 'wsSettings' if kind == 'ws' else 'xhttpSettings'
        require(set(stream) == {'network', 'security', 'tlsSettings', key} and stream['security'] == 'tls')
        expected_tls = {'serverName': address, 'fingerprint': stream['tlsSettings'].get('fingerprint'),
                        'alpn': ['http/1.1' if kind == 'ws' else 'h2'], 'allowInsecure': False}
        require(stream['tlsSettings'] == expected_tls and expected_tls['fingerprint'] in FINGERPRINTS)
        if kind == 'ws':
            require(stream[key] == {'path': WS_PATH, 'headers': {'Host': ORIGIN}})
        else:
            extra = stream[key]
            require(set(extra) == {'mode', 'path', 'host', 'extra'} and extra['mode'] == 'packet-up'
                    and extra['path'] == XHTTP_PATH and extra['host'] == CDN)
            xhttp_extra(extra['extra'])


def no_symlinks(path):
    require(path.is_absolute() and not any(item.is_symlink() for item in (path, *path.parents)))


def private_directory(path):
    no_symlinks(path)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)


def protected_json(path):
    no_symlinks(path)
    private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_mode, row.st_uid, row.st_nlink)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= MAX_INPUT)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_INPUT + 1)
        require(len(raw) == before.st_size and identity(os.fstat(fd)) == identity(before))
        return parsed(raw)
    finally:
        os.close(fd)


def private_output(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, 'wb')


def write_private(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    require(len(raw) <= MAX_INPUT)
    with private_output(path) as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def prepare(snapshot_path, owner_path, state, *, account_name=None, owner_confirmed=False):
    require(owner_confirmed is True)
    snapshot, owner_document = protected_json(snapshot_path), protected_json(owner_path)
    probes = build(snapshot, owner_document, account_name)
    owner = selected_owner(owner_document, account_name)
    no_symlinks(state)
    require(not state.exists())
    if state.parent != Path('/private/tmp'):
        private_directory(state.parent)
    bundle = {'format': 1, 'ip': IP, 'origin': ORIGIN, 'cdn': CDN,
              'preparedAt': datetime.now(timezone.utc).isoformat(), 'ownerExpiresAt': owner['expireAt'], 'probes': probes}
    state.mkdir(mode=0o700, exist_ok=False)
    private_directory(state)
    write_private(state / 'bundle.private.json', bundle)
    write_private(state / 'manifest.json', {'format': 1, 'bundleHash': digest(bundle), 'probeCount': 3})
    return {'prepared': True, 'state': str(state), 'probe_count': 3, 'network': False, 'account_changes': False}


def single_config(outbound, port):
    require(type(port) is int and 1024 <= port <= 65535)
    kind = outbound.get('streamSettings', {}).get('network')
    validate_outbound(kind, outbound)
    leaf = copy.deepcopy(outbound)
    leaf['tag'] = 'audit-selected'
    return {'log': {'loglevel': 'warning'},
        'inbounds': [{'tag': 'audit-socks', 'listen': '127.0.0.1', 'port': port, 'protocol': 'socks',
                      'settings': {'auth': 'noauth', 'udp': False}}],
        'outbounds': [leaf], 'routing': {'domainStrategy': 'AsIs', 'rules': [
            {'type': 'field', 'inboundTag': ['audit-socks'], 'outboundTag': 'audit-selected'}]}}


def pinned_engine(path, expected):
    require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected)
            and path.is_absolute() and not any(item.is_symlink() for item in (path, *path.parents)))
    parent = path.parent.stat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.geteuid()} and info.st_nlink == 1
                and not info.st_mode & 0o022 and info.st_mode & 0o111 and 0 < info.st_size <= 128_000_000
                and parent.st_uid in {0, os.geteuid()} and not parent.st_mode & 0o022)
        hasher = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(65_536), b""): hasher.update(chunk)
        after, named = os.fstat(fd), path.stat()
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_mode, row.st_nlink)
        require(hasher.hexdigest() == expected and identity(info) == identity(after) == identity(named))
    finally: os.close(fd)


def curl_args(port):
    return [CURL, "--disable", "--silent", "--show-error", "--retry", "0", "--connect-timeout", "6", "--max-time", "18",
            "--max-filesize", "4096", "--proto", "=https", "--tlsv1.2", "--noproxy", "", "--socks5-hostname",
            "127.0.0.1:" + str(port), "--output", "/dev/null", "--write-out", "%{http_code} %{time_total}", URL]


def stop(child):
    if child is None or child.poll() is not None: return
    try: os.killpg(child.pid, signal.SIGTERM); child.wait(timeout=3)
    except ProcessLookupError: pass
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=3)


def test_one(engine, outbound, directory, number):
    config = directory / ("config-" + str(number) + ".json")
    log = directory / ("engine-" + str(number) + ".log")
    curl_log = directory / ("curl-" + str(number) + ".log")
    child = None
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0)); port = reservation.getsockname()[1]
        write_private(config, single_config(outbound, port))
        with private_output(log) as output:
            checked = subprocess.run([str(engine), "run", "-test", "-config", str(config)], cwd=directory,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     env=ENV, timeout=12, check=False)
            if checked.returncode: return {"status": "uncovered", "reason": "engine_config_rejected"}
            child = subprocess.Popen([str(engine), "run", "-config", str(config)], cwd=directory,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     env=ENV, close_fds=True, start_new_session=True)
            ready, deadline = False, time.monotonic() + 6
            while time.monotonic() < deadline and child.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2): pass
                    ready = True; break
                except OSError: time.sleep(0.05)
            if not ready or child.poll() is not None: return {"status": "uncovered", "reason": "engine_listener_unavailable"}
            with private_output(curl_log) as errors:
                response = subprocess.run(curl_args(port), cwd=directory, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=errors, env=ENV, timeout=22, check=False)
            if len(response.stdout) > 128: return {"status": "uncovered", "reason": "unexpected_probe_output"}
            if child.poll() is not None: return {"status": "uncovered", "reason": "engine_exited_during_probe"}
            if response.returncode == 7: return {"status": "uncovered", "reason": "local_socks_unavailable"}
            if response.returncode:
                reason = {28: "https_timeout", 35: "https_tls_error", 60: "https_certificate_error"}.get(response.returncode, "https_transfer_failed")
                return {"status": "failed", "reason": reason}
            found = re.fullmatch(rb"([0-9]{3}) ([0-9]+(?:\.[0-9]+)?)", response.stdout.strip())
            if not found: return {"status": "uncovered", "reason": "unexpected_probe_output"}
            if child.poll() is not None: return {"status": "uncovered", "reason": "engine_exited_during_probe"}
            return {"status": "passed" if found[1] == b"204" else "failed",
                    "reason": "https_204" if found[1] == b"204" else "unexpected_https_status",
                    "elapsedMs": round(float(found[2]) * 1000)}
    except subprocess.TimeoutExpired: return {"status": "uncovered", "reason": "local_process_timeout"}
    except Exception: return {"status": "uncovered", "reason": "local_test_error"}
    finally:
        try: stop(child)
        finally:
            if config.exists(): config.unlink()


def run(state, engine, sha, *, owner_confirmed=False):
    require(owner_confirmed is True)
    private_directory(state)
    bundle = protected_json(state / 'bundle.private.json')
    manifest = protected_json(state / 'manifest.json')
    require(manifest == {'format': 1, 'bundleHash': digest(bundle), 'probeCount': 3}
            and set(bundle) == {'format', 'ip', 'origin', 'cdn', 'preparedAt', 'ownerExpiresAt', 'probes'}
            and bundle['format'] == 1 and bundle['ip'] == IP and bundle['origin'] == ORIGIN and bundle['cdn'] == CDN)
    now = datetime.now(timezone.utc)
    require(-60 <= (now - date(bundle['preparedAt'])).total_seconds() <= 3600
            and date(bundle['ownerExpiresAt']) > now)
    probes = bundle['probes']
    require(isinstance(probes, list) and len(probes) == 3 and {row.get('kind') for row in probes} == set(TAGS))
    require(len({canonical(row.get('host_uuid')) for row in probes}) == 3)
    keys = set()
    for row in probes:
        require(set(row) == {'kind', 'host_uuid', 'outbound'})
        validate_outbound(row['kind'], row['outbound'])
        keys.add(row['outbound']['settings']['vnext'][0]['users'][0]['id'])
    require(len(keys) == 1)
    pinned_engine(engine, sha)
    directory = Path(tempfile.mkdtemp(prefix='run-', dir=state))
    private_directory(directory)
    outcomes = []
    for number, row in enumerate(probes):
        outcome = test_one(engine, row['outbound'], directory, number)
        require(isinstance(outcome, dict) and outcome.get('status') in {'passed', 'failed', 'uncovered'}
                and isinstance(outcome.get('reason'), str) and set(outcome) <= {'status', 'reason', 'elapsedMs'})
        outcomes.append({'kind': row['kind'], **outcome})
    counts = {key: sum(item['status'] == key for item in outcomes) for key in ('passed', 'failed', 'uncovered')}
    result = {'format': 1, 'createdAt': datetime.now(timezone.utc).isoformat(), 'target': URL,
              'tests': outcomes, 'counts': counts, 'all_passed': counts == {'passed': 3, 'failed': 0, 'uncovered': 0},
              'notice': 'Three isolated authenticated HTTPS requests; not a carrier-network or throughput test.'}
    write_private(directory / 'results.json', result)
    return {'completed': True, 'results': str(directory / 'results.json'), 'counts': counts,
            'all_passed': result['all_passed'], 'account_changes': False, 'new_accounts': False}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('action', nargs='?', choices=('prepare', 'run'))
        parser.add_argument('--snapshot', type=Path)
        parser.add_argument('--owner-file', type=Path)
        parser.add_argument('--state', type=Path)
        parser.add_argument('--account-name')
        parser.add_argument('--xray', type=Path)
        parser.add_argument('--xray-sha256')
        parser.add_argument('--owner-confirmed', action='store_true')
        args = parser.parse_args(argv)
        if args.action is None:
            result = {'plan_only': True, 'network': False, 'filesystem_writes': False,
                      'owner_only': True, 'transports': list(TAGS), 'account_changes': False}
        elif args.action == 'prepare':
            require(args.snapshot is not None and args.owner_file is not None and args.state is not None
                    and args.xray is None and args.xray_sha256 is None)
            result = prepare(args.snapshot, args.owner_file, args.state,
                             account_name=args.account_name, owner_confirmed=args.owner_confirmed)
        else:
            require(args.state is not None and args.xray is not None and args.xray_sha256 is not None
                    and args.snapshot is None and args.owner_file is None and args.account_name is None)
            result = run(args.state, args.xray, args.xray_sha256, owner_confirmed=args.owner_confirmed)
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get('completed') and not result.get('all_passed') else 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        print('FI authenticated probe stopped; identities, links, credentials and raw output suppressed.', file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == '__main__':
    raise SystemExit(main())
