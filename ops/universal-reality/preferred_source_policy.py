#!/usr/bin/env python3
"""Preferred IPv4 with narrowly audited supplemental source rules.

The existing preferred_source.py stays immutable for previously pinned plans.
This self-contained version never changes policy rules or auxiliary tables.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile


ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'HOME': '/nonexistent'}
PLAN = 'preferred-source-plan.json'


class SourceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise SourceError(message)


def ipv4(value):
    try:
        require(isinstance(value, str), 'IPv4 must be a plain address')
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        raise SourceError('IPv4 must be a plain address') from None


def command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                env=ENV, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        raise SourceError('A route/systemd command did not complete; details suppressed') from None
    require(result.returncode == 0, 'A route/systemd command failed; details suppressed')
    return result.stdout.strip()


def https_probe(expected, *, bind=False):
    """Two fixed HTTPS destinations; verify source and TLS, never disable cert checks."""
    class BoundHTTPS(http.client.HTTPSConnection):
        observed = None

        def connect(self):
            # Force IPv4 even for the unbound post-check: an IPv6-capable VPS
            # must not accidentally test a different address family.
            addresses = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
            require(bool(addresses), 'No IPv4 HTTPS destination was resolved')
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                raw.settimeout(self.timeout)
                if bind:
                    raw.bind((expected, 0))
                raw.connect(addresses[0][4])
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                self.observed = self.sock.getsockname()[0]
            except BaseException:
                raw.close()
                raise

    host = '1.1.1.1' if bind else 'www.gstatic.com'
    path = '/cdn-cgi/trace' if bind else '/generate_204'
    connection = BoundHTTPS(host, timeout=5, context=ssl.create_default_context(),
                            source_address=(expected, 0) if bind else None)
    try:
        connection.request('GET', path, headers={'Connection': 'close', 'User-Agent': 'SPN-route-check/1'})
        result = connection.getresponse()
        body = result.read(8193)
        require(len(body) <= 8192 and connection.observed == expected,
                'HTTPS did not use the chosen local IPv4')
        if bind:
            exits = [line[3:] for line in body.decode('ascii', errors='replace').splitlines()
                     if line.startswith('ip=')]
            require(result.status == 200 and exits == [expected], 'Chosen IPv4 HTTPS egress was not confirmed')
        else:
            require(result.status == 204, 'Default-route HTTPS check failed')
    except (OSError, http.client.HTTPException):
        raise SourceError('Chosen IPv4 HTTPS check failed; route must not be kept') from None
    finally:
        connection.close()


def route_args(row, dev, source):
    allowed = {'dst', 'dev', 'gateway', 'type', 'table', 'protocol', 'scope', 'prefsrc', 'metric', 'flags'}
    require(isinstance(row, dict) and row.keys() <= allowed, 'Unsupported default-route attributes')
    require(re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', dev) and row.get('dev') == dev
            and row.get('dst') == 'default' and row.get('type', 'unicast') == 'unicast'
            and row.get('table', 'main') in ('main', 254), 'Expected one simple main-table default route')
    flags = row.get('flags', [])
    require(flags in ([], ['onlink']), 'Unsupported default-route flags')
    args = ['default', 'via', ipv4(row.get('gateway')), 'dev', dev]
    for key, option in (('protocol', 'proto'), ('scope', 'scope'), ('metric', 'metric')):
        if key in row:
            value = row[key]
            require(type(value) in (str, int) and re.fullmatch(r'[A-Za-z0-9_-]+', str(value)),
                    'Unsupported default-route parameter')
            args.extend((option, str(value)))
    if source is not None:
        args.extend(('src', ipv4(source)))
    return args + flags


def safe_directory(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'Unsafe state directory path')
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, 'State directory must be owned with mode 0700')
    return path


def read_file(path, *, mode=0o600, limit=131072):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1, 'Unsafe owned file')
            data = stream.read(limit + 1)
            require(len(data) <= limit, 'Owned file exceeds size limit')
            return data
    except FileNotFoundError:
        return None


def owned_write(path, data, *, mode=0o600):
    existing = read_file(path, mode=mode)
    if existing is not None:
        require(existing == data, 'Owned file changed; refusing to overwrite it')
        return
    fd, name = tempfile.mkstemp(prefix='.source-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Existing immutable files are never silently replaced.
        os.link(name, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(name).unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


class PreferredSource:
    def __init__(self, target, state_dir, *, run=command, probe=https_probe, root=Path('/'), helper=None):
        self.target = ipv4(target)
        address = ipaddress.IPv4Address(self.target)
        require(address.is_global and not address.is_multicast, 'Choose an assigned public IPv4')
        self.state = safe_directory(state_dir)
        match = re.fullmatch(r'(?:spn-reality-managed-)?([0-9a-f]{32})', self.state.name)
        require(match is not None, 'State directory must identify the installer deployment')
        self.id = match[1]
        self.root, self.run, self.probe = Path(root), run, probe
        self.directory = self.root / 'opt/spn-reality-network' / self.id
        self.unit_name = 'spn-reality-source-' + self.id + '.service'
        self.unit = self.root / 'etc/systemd/system' / self.unit_name
        self.helper = Path(helper) if helper is not None else Path(__file__).resolve()

    def ip(self, *args):
        try:
            value = json.loads(self.run('ip', '-j', '-4', *args))
        except (ValueError, TypeError):
            raise SourceError('Unexpected iproute2 JSON') from None
        require(isinstance(value, list), 'Unexpected iproute2 JSON')
        return value

    def assigned(self):
        matches = [(row['ifname'], entry['prefixlen'])
                   for row in self.ip('address', 'show', 'scope', 'global') if 'UP' in row.get('flags', [])
                   for entry in row.get('addr_info', [])
                   if entry.get('family') == 'inet' and entry.get('local') == self.target]
        require(len(matches) == 1, 'Chosen IPv4 is not assigned once to an UP interface')
        require(type(matches[0][1]) is int and 0 <= matches[0][1] <= 32, 'Invalid assigned IPv4 prefix')
        return matches[0][0]

    def rules(self):
        rows = self.ip('rule', 'show')
        expected = [(0, 'local'), (32766, 'main'), (32767, 'default')]
        message = 'Policy routing is not supported; no route was changed'
        require(3 <= len(rows) <= 35 and all(isinstance(row, dict)
                and row.keys() <= {'priority', 'src', 'table', 'protocol'}
                and type(row.get('priority')) is int for row in rows), message)
        priorities = [row['priority'] for row in rows]
        require(priorities == sorted(set(priorities)), message)
        standard, additional = [], []
        standard_priorities = dict(expected)
        for row in rows:
            if row['priority'] in standard_priorities:
                require(row.get('src') == 'all' and row.get('table') == standard_priorities[row['priority']], message)
                standard.append((row['priority'], row['table']))
            else:
                require(set(row) == {'priority', 'src', 'table', 'protocol'}
                        and 1 <= row['priority'] < 32766 and row['protocol'] == 'static', message)
                additional.append(row)
        require(standard == expected, message)
        if not additional:
            return

        def table_id(value):
            require((type(value) is int or isinstance(value, str)
                     and re.fullmatch(r'[1-9][0-9]{0,9}', value))
                    and 1 <= int(value) <= 4294967295 and int(value) not in (253, 254, 255), message)
            return str(value)

        dev = self.assigned()
        addresses = self.ip('address', 'show', 'scope', 'global')
        sources, tables = set(), set()
        for row in additional:
            raw = row['src']
            require(isinstance(raw, str) and ('/' not in raw or raw.endswith('/32') and raw.count('/') == 1), message)
            address = ipv4(raw.removesuffix('/32'))
            parsed = ipaddress.IPv4Address(address)
            require(parsed.is_global and not parsed.is_multicast and address != self.target
                    and address not in sources, message)
            assigned = [(interface.get('ifname'), entry.get('prefixlen'))
                        for interface in addresses if 'UP' in interface.get('flags', [])
                        for entry in interface.get('addr_info', [])
                        if entry.get('family') == 'inet' and entry.get('local') == address]
            require(len(assigned) == 1 and assigned[0][0] == dev
                    and type(assigned[0][1]) is int and 0 <= assigned[0][1] <= 32, message)
            table = table_id(row['table'])
            require(table not in tables, message)
            sources.add(address)
            tables.add(table)
            routes = self.ip('route', 'show', 'table', table)
            require(len(routes) == 1 and isinstance(routes[0], dict), message)
            route = routes[0]
            require('table' not in route or table_id(route['table']) == table, message)
            require(route.get('dev') == dev and route.get('flags') == ['onlink']
                    and route.get('prefsrc') in (None, address), message)
            route_args({key: value for key, value in route.items() if key != 'table'}, dev, route.get('prefsrc'))

        # This supports a main IP which is already selected while other owned
        # addresses have provider routes. It deliberately cannot retarget into
        # one of those policy tables or change the presently selected main IP.
        main = self.default()
        route_args(main, dev, main.get('prefsrc'))
        for destination in ('1.1.1.1', '8.8.8.8'):
            selected = self.ip('route', 'get', destination)
            require(len(selected) == 1 and isinstance(selected[0], dict)
                    and selected[0].get('table', 'main') in ('main', 254)
                    and selected[0].get('dev') == dev and selected[0].get('gateway') == main['gateway']
                    and (selected[0].get('prefsrc') or selected[0].get('src')) == self.target,
                    'Policy routing target must already use the main gateway and assigned IPv4; no route was changed')

    def default(self):
        rows = self.ip('route', 'show', 'table', 'main', 'default')
        require(len(rows) == 1, 'Expected exactly one main-table default route')
        return rows[0]

    def selected(self, route):
        rows = self.ip('route', 'get', '1.1.1.1')
        require(len(rows) == 1 and rows[0].get('dev') == route['dev']
                and rows[0].get('gateway') == route['gateway'], 'Probe destination uses another route')
        return ipv4(rows[0].get('prefsrc') or rows[0].get('src'))

    def inspect(self):
        dev = self.assigned()
        self.rules()
        route = self.default()
        route_args(route, dev, route.get('prefsrc'))
        return route, self.selected(route)

    def plan(self):
        stage = read_file(self.state / PLAN)
        persistent = read_file(self.directory / 'plan.json') if self.directory.exists() else None
        if stage is not None and persistent is not None:
            require(stage == persistent, 'Stage and persistent source plans differ')
        raw = stage if stage is not None else persistent
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            raise SourceError('Invalid saved source plan') from None
        require(value.get('version') == 1 and value.get('deployment') == self.id
                and value.get('target') == self.target, 'Saved source plan belongs to another target')
        before, after = value['before'], value['after']
        route_args(before, before['dev'], before.get('prefsrc'))
        require(after == {**before, 'prefsrc': self.target}, 'Saved source transition differs')
        require(re.fullmatch('[0-9a-f]{64}', value.get('helperHash', '')) is not None, 'Invalid helper digest')
        return value

    def unit_bytes(self):
        # Only constant paths and hex deployment IDs are interpolated.
        base = '/opt/spn-reality-network/' + self.id
        return ('[Unit]\nDescription=SPN owned preferred IPv4 ' + self.id + '\n'
                'Wants=network-online.target\nAfter=network-online.target\n\n'
                '[Service]\nType=oneshot\nUser=root\nRemainAfterExit=yes\nTimeoutStartSec=45\n'
                'ExecStart=/usr/bin/python3 ' + base + '/source.py apply --ip ' + self.target
                + ' --state-dir ' + base + '\n\n[Install]\nWantedBy=multi-user.target\n').encode()

    def check(self):
        route, source = self.inspect()
        plan = self.plan()
        if plan:
            require(route in (plan['before'], plan['after']), 'Default route changed outside this operation')
        return {'ip': self.target, 'interface': route['dev'], 'gateway': route['gateway'],
                'currentSource': source, 'needsChange': source != self.target,
                'persistenceConfigured': read_file(self.unit, mode=0o644) == self.unit_bytes()}

    def prepare_persistence(self, plan):
        if self.directory.exists():
            safe_directory(self.directory)
        else:
            parent = self.directory.parent
            require(not any(p.is_symlink() for p in (parent, *parent.parents)), 'Unsafe persistence parent')
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            require(parent.stat().st_uid == os.geteuid() and not parent.stat().st_mode & 0o022,
                    'Unsafe persistence parent permissions')
            self.directory.mkdir(mode=0o700)
        helper = read_file(self.helper)
        require(helper is not None and hashlib.sha256(helper).hexdigest() == plan['helperHash'],
                'Preferred-source helper changed')
        owned_write(self.state / PLAN, json_bytes(plan))
        owned_write(self.directory / 'plan.json', json_bytes(plan))
        owned_write(self.directory / 'source.py', helper)
        require(self.unit.parent.is_dir() and not any(p.is_symlink() for p in (self.unit.parent, *self.unit.parent.parents)),
                'Unsafe systemd unit directory')
        old_unit = read_file(self.unit, mode=0o644)
        require(old_unit is None or old_unit == self.unit_bytes(), 'An unrelated persistence unit exists')

    def apply(self):
        route, source = self.inspect()
        plan = self.plan()
        if plan is None:
            helper = read_file(self.helper)
            require(helper is not None, 'Missing owned helper')
            plan = {'version': 1, 'deployment': self.id, 'target': self.target, 'before': route,
                    'after': {**route, 'prefsrc': self.target}, 'beforeSource': source,
                    'helperHash': hashlib.sha256(helper).hexdigest()}
        require(route in (plan['before'], plan['after']), 'Default route changed outside this operation')
        # Confirm the chosen IP actually exits before changing the default route.
        self.probe(self.target, bind=True)
        self.prepare_persistence(plan)
        attempted = False
        try:
            self.rules()
            require(self.default() == route, 'Default route changed during precheck')
            if route != plan['after']:
                attempted = True
                self.run('ip', '-4', 'route', 'change', *route_args(route, route['dev'], self.target))
            require(self.default() == plan['after'] and self.selected(plan['after']) == self.target,
                    'Preferred IPv4 readback failed')
            self.probe(self.target)
            require(self.default() == plan['after'], 'Default route changed during HTTPS verification')
            owned_write(self.unit, self.unit_bytes(), mode=0o644)
            self.run('systemctl', 'daemon-reload')
            self.run('systemctl', 'enable', self.unit_name)
            return {'ip': self.target, 'preferredSourceVerified': True, 'bootPersistence': self.unit_name,
                    'networkRestarted': False}
        except BaseException:
            # If a write was attempted, its response may have been lost. Always
            # compare actual state before deciding whether it is ours to undo.
            if attempted or route == plan['after']:
                try:
                    self.rollback()
                except (OSError, ValueError):
                    print('Source rollback could not be confirmed; saved plan retained for manual recovery', file=sys.stderr)
            raise

    def verify(self):
        plan = self.plan()
        require(plan is not None, 'No saved preferred-source operation')
        route, source = self.inspect()
        require(route == plan['after'] and source == self.target, 'Preferred source is not active')
        require(read_file(self.directory / 'plan.json') == json_bytes(plan)
                and read_file(self.unit, mode=0o644) == self.unit_bytes(), 'Boot persistence does not match')
        helper = read_file(self.directory / 'source.py')
        require(helper is not None and hashlib.sha256(helper).hexdigest() == plan['helperHash'], 'Persistent helper differs')
        self.run('systemctl', 'is-enabled', '--quiet', self.unit_name)
        self.probe(self.target)
        return {'ip': self.target, 'preferredSourceVerified': True, 'bootPersistenceVerified': True}

    def rollback(self):
        plan = self.plan()
        if plan is None:
            return {'rolledBack': True, 'changed': False}
        self.assigned()
        self.rules()
        route = self.default()
        require(route in (plan['before'], plan['after']), 'Concurrent route change; rollback refused to overwrite it')
        unit = read_file(self.unit, mode=0o644)
        require(unit is None or unit == self.unit_bytes(), 'Persistence unit changed; rollback will not remove it')
        if unit is not None:
            self.run('systemctl', 'disable', self.unit_name)
            require(read_file(self.unit, mode=0o644) == unit, 'Persistence unit changed during rollback')
            self.unit.unlink()
            self.run('systemctl', 'daemon-reload')
        if route != plan['before']:
            self.run('ip', '-4', 'route', 'change',
                     *route_args(plan['before'], plan['before']['dev'], plan['before'].get('prefsrc')))
        require(self.default() == plan['before'] and self.selected(plan['before']) == plan['beforeSource'],
                'Original source route rollback was not confirmed')
        return {'rolledBack': True, 'changed': route != plan['before']}


@contextlib.contextmanager
def operation_lock(state):
    path = state / '.preferred-source.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                'Unsafe preferred-source lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SourceError('Another preferred-source operation is running') from None
        yield
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'apply', 'verify', 'rollback'))
    parser.add_argument('--ip', required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux VPS as root')
    manager = PreferredSource(args.ip, args.state_dir)
    # check and verify do not create lock/state files. The mutating actions are
    # serialized; the boot service shares the canonical persistent lock.
    if args.action in ('check', 'verify'):
        result = getattr(manager, args.action)()
    else:
        with operation_lock(manager.state):
            result = getattr(manager, args.action)()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    def interrupted(number, frame):
        raise InterruptedError('Preferred-source operation interrupted')

    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyboardInterrupt) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
