#!/usr/bin/env python3
"""Owned pre-DNAT reserve-IP and known-scanner filtering for one SPN node.

Only our dedicated inet table is changed. UFW, Docker rules, routes and SSH
configuration are preserved. Saved transitions are forward-only and resumable.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import scanner_guard as scanners


TABLE = 'spn_node_guard'
OWNER = 'spn-node-guard-v1'
SERVICE = 'spn-node-guard.service'
REFRESH = 'spn-node-guard-refresh'
CODE = ('node_guard.py', 'scanner_guard.py', 'scanner_lists.json')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'HOME': '/root',
       'DEBIAN_FRONTEND': 'noninteractive', 'PYTHONDONTWRITEBYTECODE': '1'}


class GuardError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GuardError(message)


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError):
        raise GuardError('Choose a public IPv4 address') from None
    require(isinstance(value, str) and str(address) == value and address.is_global
            and not address.is_multicast, 'Choose a public IPv4 address')
    return value


def command(*args, input=None, timeout=30):
    try:
        stdin = {'stdin': subprocess.DEVNULL} if input is None else {'input': input}
        result = subprocess.run(args, **stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=ENV, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise GuardError('A node-guard system command did not complete; details suppressed') from None
    require(result.returncode == 0, 'A node-guard system command failed; details suppressed')
    return result.stdout


def safe_path(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'Unsafe node-guard path')


def read_file(path, *, owner, modes=(0o600,), limit=1048576):
    safe_path(path)
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) in modes, 'Unsafe node-guard file owner/type/mode')
            raw = stream.read(limit + 1)
            require(len(raw) <= limit, 'Node-guard file exceeds its size limit')
            return raw
    except FileNotFoundError:
        return None


def atomic_write(path, raw, *, owner, mode=0o600, before=None):
    require(read_file(path, owner=owner, modes=(mode,)) == before,
            'Owned node-guard file changed outside this operation')
    descriptor, temporary = tempfile.mkstemp(prefix='.node-guard-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        require(read_file(path, owner=owner, modes=(mode,)) == before,
                'Owned node-guard file changed during this operation')
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def require_directory(path, *, owner, mode=None):
    safe_path(path)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o022
            and (mode is None or stat.S_IMODE(info.st_mode) == mode), 'Unsafe node-guard directory')


def make_owned_directory(path, *, owner, mode, marker):
    """Publish the ownership marker and its new directory together."""
    safe_path(path)
    require_directory(path.parent, owner=owner)
    if not path.exists():
        temporary = Path(tempfile.mkdtemp(prefix='.node-guard-', dir=path.parent))
        try:
            temporary.chmod(mode)
            atomic_write(temporary / '.owner', marker.encode(), owner=owner)
            require(not path.exists() and not path.is_symlink(), 'Node-guard directory appeared concurrently')
            os.rename(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary.exists():
                (temporary / '.owner').unlink(missing_ok=True)
                temporary.rmdir()
    require_directory(path, owner=owner, mode=mode)
    require(read_file(path / '.owner', owner=owner) == marker.encode(),
            'Existing node-guard directory belongs to another installer')


@contextlib.contextmanager
def operation_lock(directory, owner):
    path = directory / '.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, 'Unsafe node-guard lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuardError('Another node-guard operation is running') from None
        yield
    finally:
        os.close(descriptor)


def validate_policy(value, deployment):
    require(isinstance(value, dict) and set(value) == {
        'version', 'deployment', 'activeIp', 'panelIp', 'controllerIp', 'scanners'}
        and type(value['version']) is int and value['version'] == 1
        and value['deployment'] == deployment, 'Invalid node-guard policy identity')
    for key in ('activeIp', 'panelIp', 'controllerIp'):
        ipv4(value[key])
    scanners.validate_snapshot(value['scanners'])
    return value


def match(left, right, op='=='):
    return {'match': {'op': op, 'left': left, 'right': right}}


def meta(key):
    return {'meta': {'key': key}}


def payload(protocol, field):
    return {'payload': {'protocol': protocol, 'field': field}}


def element(value):
    network = ipaddress.ip_network(value)
    return (str(network.network_address) if network.prefixlen == network.max_prefixlen else
            {'prefix': {'addr': str(network.network_address), 'len': network.prefixlen}})


def policy_objects(policy):
    """Use nft's canonical dependencies implied by typed payload expressions.

    nft removes redundant nfproto/l4proto comparisons on readback. The IP/IP6
    payloads already constrain family, and a TCP payload constrains protocol.
    The IPv6 reserve rule still needs an explicit family match.
    """
    deployment = policy['deployment']
    objects = [{'table': {'family': 'inet', 'name': TABLE, 'comment': OWNER + ':' + deployment}}]
    for name, kind, values in (
        ('blocked4', 'ipv4_addr', policy['scanners']['ipv4']),
        ('blocked6', 'ipv6_addr', policy['scanners']['ipv6']),
        ('trusted4', 'ipv4_addr', sorted({policy['panelIp'], policy['controllerIp']}))):
        objects.append({'set': {'family': 'inet', 'table': TABLE, 'name': name,
                               'type': kind, 'flags': ['interval'], 'elem': [element(v) for v in values]}})
    objects.append({'chain': {'family': 'inet', 'table': TABLE, 'name': 'ingress',
                             'type': 'filter', 'hook': 'prerouting', 'prio': -310, 'policy': 'accept'}})
    rules = [
        ('loopback', [match(meta('iifname'), 'lo'), {'return': None}]),
        ('not-local', [match({'fib': {'result': 'type', 'flags': ['daddr']}}, 'local', '!='), {'return': None}]),
        ('reserve-v6', [match(meta('nfproto'), 'ipv6'),
                        match(payload('tcp', 'dport'), 443), {'counter': None}, {'drop': None}]),
        ('reserve-v4', [match(payload('ip', 'daddr'), policy['activeIp'], '!='),
                        match(payload('tcp', 'dport'), 443), {'counter': None}, {'drop': None}]),
        ('trusted', [match(payload('ip', 'saddr'), '@trusted4'), {'return': None}]),
        ('scanner-v4', [match(payload('ip', 'saddr'), '@blocked4'),
                        {'counter': None}, {'drop': None}]),
        ('scanner-v6', [match(payload('ip6', 'saddr'), '@blocked6'),
                        {'counter': None}, {'drop': None}]),
    ]
    for name, expressions in rules:
        objects.append({'rule': {'family': 'inet', 'table': TABLE, 'chain': 'ingress',
                                'expr': expressions, 'comment': OWNER + ':' + name}})
    return objects


def _set_networks(elements, version):
    result = []
    for value in elements:
        if isinstance(value, str):
            result.append(ipaddress.ip_network(value))
        elif isinstance(value, dict) and set(value) == {'prefix'}:
            prefix = value['prefix']
            require(isinstance(prefix, dict) and set(prefix) == {'addr', 'len'}, 'Unexpected nft set prefix')
            result.append(ipaddress.ip_network(str(prefix['addr']) + '/' + str(prefix['len'])))
        elif isinstance(value, dict) and set(value) == {'range'}:
            endpoints = value['range']
            require(isinstance(endpoints, list) and len(endpoints) == 2, 'Unexpected nft set range')
            result.extend(ipaddress.summarize_address_range(*(ipaddress.ip_address(v) for v in endpoints)))
        else:
            raise GuardError('Unexpected nft set element')
    require(all(n.version == version for n in result), 'Unexpected nft set address family')
    return [str(n) for n in ipaddress.collapse_addresses(result)]


def normalize(objects):
    """Ignore handles/counter values, preserving every policy-bearing field."""
    normalized = []
    for wrapped in objects:
        require(isinstance(wrapped, dict) and len(wrapped) == 1, 'Unexpected nft JSON object')
        kind, value = next(iter(wrapped.items()))
        if kind == 'metainfo':
            continue
        require(kind in ('table', 'set', 'chain', 'rule') and isinstance(value, dict), 'Unexpected nft object kind')
        value = copy.deepcopy(value)
        value.pop('handle', None)
        if kind == 'set':
            version = 4 if value.get('type') == 'ipv4_addr' else 6 if value.get('type') == 'ipv6_addr' else None
            require(version is not None, 'Unexpected nft set type')
            value['elem'] = _set_networks(value.get('elem', []), version)
        if kind == 'rule':
            require(isinstance(value.get('expr'), list), 'Unexpected nft rule expression')
            for expression in value['expr']:
                if isinstance(expression, dict) and set(expression) == {'counter'}:
                    counter = expression['counter']
                    require(counter is None or (isinstance(counter, dict) and set(counter) == {'packets', 'bytes'}
                            and all(type(v) is int and v >= 0 for v in counter.values())), 'Unexpected nft counter')
                    expression['counter'] = None
        normalized.append({kind: value})
    return normalized


class NodeGuard:
    def __init__(self, deployment, *, root=Path('/'), run=command, source_dir=None, owner=None):
        require(isinstance(deployment, str) and re.fullmatch(r'[0-9a-f]{32}', deployment), 'Invalid deployment ID')
        self.deployment, self.root, self.run = deployment, Path(root), run
        self.owner = os.geteuid() if owner is None else owner
        self.directory = self.root / 'var/lib/spn-node-guard'
        self.config = self.directory / 'config.json'
        self.library = self.root / 'usr/local/lib/spn-node-guard'
        self.units = self.root / 'etc/systemd/system'
        self.source = Path(source_dir).resolve() if source_dir else Path(__file__).resolve().parent

    def ownership(self):
        node = self.root / 'opt/spn-reality-node'
        require_directory(node, owner=self.owner, mode=0o700)
        require(read_file(node / '.spn-deployment', owner=self.owner) == (self.deployment + '\n').encode(),
                'Installed node belongs to another deployment')

    def assigned(self, active_ip):
        try:
            rows = json.loads(self.run('ip', '-j', '-4', 'address', 'show', 'scope', 'global'))
            matches = [row for row in rows if 'UP' in row.get('flags', [])
                       for address in row.get('addr_info', [])
                       if address.get('family') == 'inet' and address.get('local') == active_ip]
        except (ValueError, TypeError, AttributeError):
            raise GuardError('Unexpected assigned-address response') from None
        require(len(matches) == 1, 'Active IPv4 is not assigned once to an UP interface')

    def ensure_tools(self):
        if shutil.which('nft', path=ENV['PATH']) is None:
            # /etc/os-release is commonly a symlink to this regular file.
            release_path = self.root / 'usr/lib/os-release'
            if not release_path.exists():
                release_path = self.root / 'etc/os-release'
            release = read_file(release_path, owner=self.owner, modes=(0o644,), limit=32768)
            require(release is not None and re.search(rb'^ID="?(?:debian|ubuntu)"?$', release, re.M),
                    'Installing nftables is supported only on Debian/Ubuntu')
            self.run('apt-get', '-o', 'DPkg::Lock::Timeout=120', 'update', timeout=300)
            self.run('apt-get', '-o', 'DPkg::Lock::Timeout=120', 'install', '-y', 'nftables', timeout=300)

    def prepare_directory(self):
        make_owned_directory(self.directory, owner=self.owner, mode=0o700,
                             marker=OWNER + ':' + self.deployment + '\n')

    def source_files(self):
        files = {name: read_file(self.source / name, owner=self.owner, modes=(0o600, 0o644)) for name in CODE}
        require(all(raw is not None for raw in files.values()), 'Node-guard source bundle is incomplete')
        scanners.validate_snapshot(json.loads(files['scanner_lists.json']))
        return files

    def state(self, *, required=True):
        require(read_file(self.directory / '.owner', owner=self.owner)
                == (OWNER + ':' + self.deployment + '\n').encode(), 'Saved node-guard ownership marker differs')
        raw = read_file(self.config, owner=self.owner)
        if raw is None:
            require(not required, 'Node guard has no saved policy')
            return None
        try:
            value = json.loads(raw)
            require(isinstance(value, dict) and set(value) == {'version', 'deployment', 'current', 'pending', 'codeHashes'}
                    and type(value['version']) is int and value['version'] == 1
                    and value['deployment'] == self.deployment, 'Invalid saved node-guard identity')
            hashes = value['codeHashes']
            require(isinstance(hashes, dict) and set(hashes) == set(CODE)
                    and all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) for v in hashes.values()),
                    'Invalid saved node-guard code hashes')
            if value['current'] is not None:
                validate_policy(value['current'], self.deployment)
            pending = value['pending']
            if pending is not None:
                require(isinstance(pending, dict) and set(pending) == {'before', 'after'}
                        and pending['before'] == value['current'], 'Invalid pending node-guard transition')
                validate_policy(pending['after'], self.deployment)
            require(value['current'] is not None or pending is not None, 'Saved node-guard policy is empty')
            require(raw == encode(value), 'Saved node-guard state changed outside this installer')
            return value
        except (TypeError, KeyError, json.JSONDecodeError):
            raise GuardError('Invalid saved node-guard state') from None

    def save_state(self, value, before):
        atomic_write(self.config, encode(value), owner=self.owner, before=None if before is None else encode(before))

    def table(self):
        try:
            tables = json.loads(self.run('nft', '-j', 'list', 'tables'))['nftables']
            matches = [item['table'] for item in tables if 'table' in item
                       and item['table'].get('family') == 'inet' and item['table'].get('name') == TABLE]
            require(len(matches) <= 1, 'Unexpected duplicate node-guard table')
            if not matches:
                return None
            value = json.loads(self.run('nft', '-j', 'list', 'table', 'inet', TABLE))['nftables']
            return normalize(value)
        except (ValueError, KeyError, TypeError) as error:
            if isinstance(error, GuardError):
                raise
            raise GuardError('Unexpected nft table response') from None

    def transition(self, state, desired):
        pending = state['pending']
        if pending is None:
            pending = {'before': state['current'], 'after': desired}
            next_state = {**state, 'pending': pending}
            self.save_state(next_state, state)
            state = next_state
        require(pending['after'] == desired, 'Resume the same pending node-guard transition')
        expected = normalize(policy_objects(desired))
        previous = normalize(policy_objects(pending['before'])) if pending['before'] is not None else None
        actual = self.table()
        require(actual is None or actual == previous or actual == expected,
                'Node-guard table changed outside this operation; refusing to overwrite it')
        if actual != expected:
            operations = ([{'delete': {'table': {'family': 'inet', 'name': TABLE}}}] if actual is not None else [])
            operations += [{'create' if 'table' in item else 'add': item} for item in policy_objects(desired)]
            batch = json.dumps({'nftables': operations})
            self.run('nft', '-j', '-c', '-f', '-', input=batch)
            require(self.table() == actual, 'Node-guard table changed during the preflight')
            try:
                self.run('nft', '-j', '-f', '-', input=batch)
            except GuardError:
                # Netlink/SSH responses can be lost after the atomic commit.
                require(self.table() == expected, 'Node-guard apply was not confirmed; repeat the same operation')
            require(self.table() == expected, 'Node-guard kernel policy readback differs')
        completed = {**state, 'current': desired, 'pending': None}
        self.save_state(completed, state)
        return completed

    def persistence_files(self):
        base = '/usr/local/lib/spn-node-guard/node_guard.py'
        marker = '# ' + OWNER + '\n'
        service = marker + '''[Unit]
Description=SPN reserve IP and known-scanner guard
DefaultDependencies=no
After=local-fs.target systemd-sysctl.service
Before=network-pre.target docker.service haproxy.service
Wants=network-pre.target

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 ''' + base + ''' apply-saved
RemainAfterExit=yes
TimeoutStartSec=45
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
'''
        refresh = marker + '''[Unit]
Description=Refresh SPN known-scanner list
After=network-online.target spn-node-guard.service
Wants=network-online.target
Requires=spn-node-guard.service

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 ''' + base + ''' refresh-saved
TimeoutStartSec=90
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
'''
        timer = marker + '''[Unit]
Description=Daily SPN known-scanner list refresh

[Timer]
OnBootSec=10min
OnCalendar=daily
RandomizedDelaySec=30min
Persistent=true
Unit=spn-node-guard-refresh.service

[Install]
WantedBy=timers.target
'''
        dropin = (marker + '[Unit]\nRequires=spn-node-guard.service\nAfter=spn-node-guard.service\n').encode()
        return {self.units / SERVICE: service.encode(), self.units / (REFRESH + '.service'): refresh.encode(),
                self.units / (REFRESH + '.timer'): timer.encode(),
                self.units / 'docker.service.d/30-spn-node-guard.conf': dropin,
                self.units / 'haproxy.service.d/30-spn-node-guard.conf': dropin}

    def persist(self, state):
        files = self.source_files()
        require({name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()} == state['codeHashes'],
                'Staged node-guard code differs from the owned installation')
        make_owned_directory(self.library, owner=self.owner, mode=0o755, marker=OWNER + '\n')
        for name, raw in files.items():
            path = self.library / name
            old = read_file(path, owner=self.owner, modes=(0o644,))
            require(old is None or old == raw, 'Installed node-guard code was externally modified')
        persistence = self.persistence_files()
        require_directory(self.units, owner=self.owner)
        for path, raw in persistence.items():
            if path.parent != self.units:
                safe_path(path.parent)
                if not path.parent.exists():
                    path.parent.mkdir(mode=0o755)
                    path.parent.chmod(0o755)
                require_directory(path.parent, owner=self.owner, mode=0o755)
            old = read_file(path, owner=self.owner, modes=(0o644,))
            require(old is None or old == raw, 'An unrelated node-guard systemd file already exists')
        for name, raw in files.items():
            path = self.library / name
            if read_file(path, owner=self.owner, modes=(0o644,)) is None:
                atomic_write(path, raw, owner=self.owner, mode=0o644)
        for path, raw in persistence.items():
            if read_file(path, owner=self.owner, modes=(0o644,)) is None:
                atomic_write(path, raw, owner=self.owner, mode=0o644)
        self.run('systemctl', 'daemon-reload')
        self.run('systemctl', 'enable', SERVICE, REFRESH + '.timer')

    def verify_persistence(self, state):
        require_directory(self.library, owner=self.owner, mode=0o755)
        require(read_file(self.library / '.owner', owner=self.owner) == (OWNER + '\n').encode(),
                'Installed node-guard code has another owner')
        for name in CODE:
            raw = read_file(self.library / name, owner=self.owner, modes=(0o644,))
            require(raw is not None and hashlib.sha256(raw).hexdigest() == state['codeHashes'][name],
                    'Installed node-guard source digest differs')
        for path, expected in self.persistence_files().items():
            require(read_file(path, owner=self.owner, modes=(0o644,)) == expected,
                    'Node-guard systemd persistence differs')
        self.run('systemctl', 'is-enabled', '--quiet', SERVICE)
        self.run('systemctl', 'is-enabled', '--quiet', REFRESH + '.timer')

    def result(self, state):
        require(state['pending'] is None and state['current'] is not None, 'Node-guard transition remains pending')
        require(self.table() == normalize(policy_objects(state['current'])), 'Node-guard kernel policy differs')
        self.verify_persistence(state)
        return {'activeIp': state['current']['activeIp'], 'deployment': self.deployment,
                'reserveVpnBlocked': True, 'scannerFilterEnabled': True, 'bootPersistenceVerified': True}

    def install(self, active_ip, panel_ip, controller_ip, old_ip=None):
        active_ip, panel_ip, controller_ip = map(ipv4, (active_ip, panel_ip, controller_ip))
        if old_ip is not None:
            old_ip = ipv4(old_ip)
        self.ownership()
        self.assigned(active_ip)
        self.ensure_tools()
        self.prepare_directory()
        with operation_lock(self.directory, self.owner):
            state = self.state(required=False)
            files = self.source_files()
            hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
            if state is None:
                require(self.table() is None, 'An unowned node-guard table already exists')
                source = scanners.validate_snapshot(json.loads(files['scanner_lists.json']))
                desired = {'version': 1, 'deployment': self.deployment, 'activeIp': active_ip,
                           'panelIp': panel_ip, 'controllerIp': controller_ip, 'scanners': source}
                state = {'version': 1, 'deployment': self.deployment, 'current': None,
                         'pending': {'before': None, 'after': desired}, 'codeHashes': hashes}
                self.save_state(state, None)
            else:
                require(state['codeHashes'] == hashes, 'Staged node-guard code differs from the saved installation')
                base = state['pending']['after'] if state['pending'] else state['current']
                desired = {**base, 'activeIp': active_ip, 'panelIp': panel_ip, 'controllerIp': controller_ip}
                before = state['current']
                if before is not None:
                    require(before['panelIp'] == panel_ip and before['controllerIp'] == controller_ip,
                            'Controller/panel exceptions changed outside this operation')
                    require(active_ip == before['activeIp'] or old_ip == before['activeIp'],
                            'Changing the active IP requires the saved current address as --old-ip')
                if state['pending'] is not None:
                    require(desired == state['pending']['after'], 'Resume the same pending active IP')
            # Publish and enable recovery before changing the kernel. A crash
            # after the nft commit must already have a boot-time replay path.
            self.persist(state)
            state = self.transition(state, desired)
            return self.result(state)

    def verify(self, active_ip=None, panel_ip=None, controller_ip=None):
        self.ownership()
        require_directory(self.directory, owner=self.owner, mode=0o700)
        with operation_lock(self.directory, self.owner):
            state = self.state()
            current = state['current']
            for name, value in (('activeIp', active_ip), ('panelIp', panel_ip), ('controllerIp', controller_ip)):
                require(value is None or (current is not None and current[name] == ipv4(value)),
                        'Saved node-guard policy differs from the requested ' + name)
            return self.result(state)

    def apply_saved(self):
        self.ownership()
        require_directory(self.directory, owner=self.owner, mode=0o700)
        with operation_lock(self.directory, self.owner):
            state = self.state()
            self.verify_persistence(state)
            desired = state['pending']['after'] if state['pending'] else state['current']
            # At boot local-fs is ready but addresses need not yet be assigned.
            state = self.transition(state, desired)
            return self.result(state)

    def refresh_saved(self):
        self.ownership()
        require_directory(self.directory, owner=self.owner, mode=0o700)
        with operation_lock(self.directory, self.owner):
            state = self.state()
            self.verify_persistence(state)
            desired = state['pending']['after'] if state['pending'] else state['current']
            state = self.transition(state, desired)
            updated = scanners.refresh(self.directory / 'scanners.json')
            state = self.transition(state, {**state['current'], 'scanners': updated})
            return self.result(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'verify', 'apply-saved', 'refresh-saved'))
    parser.add_argument('--active-ip')
    parser.add_argument('--deployment')
    parser.add_argument('--panel-ip')
    parser.add_argument('--controller-ip', default='147.45.164.28')
    parser.add_argument('--old-ip')
    args = parser.parse_args(argv)
    require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run node guard on the Linux VPS as root')
    deployment = args.deployment
    if deployment is None and args.action in ('apply-saved', 'refresh-saved'):
        raw = read_file(Path('/opt/spn-reality-node/.spn-deployment'), owner=0)
        require(raw is not None, 'No installed node deployment marker')
        deployment = raw.decode('ascii').strip()
    require(deployment is not None, 'Choose --deployment')
    manager = NodeGuard(deployment)
    if args.action == 'install':
        require(args.active_ip is not None and args.panel_ip is not None, 'Choose --active-ip and --panel-ip')
        result = manager.install(args.active_ip, args.panel_ip, args.controller_ip, args.old_ip)
        # Lock is released before systemd starts apply-saved in another process.
        manager.run('systemctl', 'start', SERVICE, REFRESH + '.timer')
        result = manager.verify(args.active_ip, args.panel_ip, args.controller_ip)
    elif args.action == 'verify':
        result = manager.verify(args.active_ip, args.panel_ip, args.controller_ip)
    else:
        result = manager.apply_saved() if args.action == 'apply-saved' else manager.refresh_saved()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyboardInterrupt) as error:
        print('Node guard stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
