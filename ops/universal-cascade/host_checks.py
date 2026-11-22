#!/usr/bin/env python3
"""Small read-only host checks shared by the RU and EU cascade installers.

Print only public interface names or verified monitoring port numbers. Never
include process command lines, raw /proc content, or command output in errors.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


class CheckError(RuntimeError):
    """Safe actionable host-check failure."""


def need(condition, message):
    if not condition:
        raise CheckError(message)


def _run(runner, *args):
    try:
        if runner is not None:
            output = runner(*args)
        else:
            result = subprocess.run(args, capture_output=True, text=True,
                                    env={**os.environ, 'LC_ALL': 'C'})
            need(result.returncode == 0, 'Host check command failed: ' + args[0])
            output = result.stdout
        need(isinstance(output, str), 'Host check command returned invalid output: ' + args[0])
        return output
    except (OSError, UnicodeError):
        raise CheckError('Cannot execute host check: ' + args[0]) from None


def wireguard_interfaces(runner=None):
    # Some iproute2 builds return [{}, {}] for a filtered JSON link query.
    # Inspect full detailed records instead of assuming each filter result has
    # an ifname. An unknown/malformed link kind must not silently pass as safe.
    try:
        rows = json.loads(_run(runner, 'ip', '-j', '-d', 'link', 'show'))
    except (ValueError, TypeError):
        raise CheckError('Cannot read interface inventory; ip must support detailed JSON link output') from None
    need(isinstance(rows, list), 'Invalid interface inventory; expected a JSON list')
    result, seen = [], set()
    for row in rows:
        need(isinstance(row, dict) and isinstance(row.get('ifname'), str)
             and re.fullmatch(r'[^\s/:\x00]{1,15}', row['ifname']),
             'Invalid interface inventory; a detailed link record has no valid interface name')
        name = row['ifname']
        need(name not in seen, 'Invalid interface inventory; duplicate interface name')
        seen.add(name)
        info = row.get('linkinfo')
        if info is None:
            continue
        need(isinstance(info, dict), 'Invalid interface inventory; malformed link information')
        kind = info.get('info_kind')
        need(isinstance(kind, str) and kind, 'Invalid interface inventory; link kind is missing')
        if kind == 'wireguard':
            result.append(name)
    return result


_AGENTS = {
    '/usr/sbin/zabbix_agentd': ('zabbix-agent', 'zabbix-agent.service'),
    '/usr/sbin/zabbix_agent2': ('zabbix-agent2', 'zabbix-agent2.service'),
}
_MONITOR_ERROR = ('TCP 10050 is occupied by an unverified service. Inspect it or explicitly preserve TCP 10050 '
                  'with the installer keep-port option for this role; the installer will not stop the service.')


def _verified_agent(pid, runner, root, uid):
    proc = root / 'proc' / str(pid)
    try:
        executable = os.readlink(proc / 'exe')
        need(executable in _AGENTS, _MONITOR_ERROR)
        package, service = _AGENTS[executable]
        file = root / executable.lstrip('/')
        for directory in (root / 'usr', root / 'usr/sbin'):
            info = directory.lstat()
            need(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022,
                 _MONITOR_ERROR)
        info = file.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == uid
             and not info.st_mode & 0o022 and info.st_mode & 0o111, _MONITOR_ERROR)
        groups = (proc / 'cgroup').read_text().splitlines()
        expected = '/system.slice/' + service
        matched = False
        for line in groups:
            fields = line.split(':', 2)
            need(len(fields) == 3, _MONITOR_ERROR)
            path = fields[2]
            if path == expected or path.startswith(expected + '/'):
                matched = True
        need(matched, _MONITOR_ERROR)
        owner = _run(runner, 'dpkg-query', '-S', executable).strip()
        need(re.fullmatch(re.escape(package) + r'(?::[A-Za-z0-9_-]+)?: ' + re.escape(executable), owner),
             _MONITOR_ERROR)
        need(_run(runner, 'dpkg-query', '-W', '-f=${Status}', package).strip() == 'install ok installed',
             _MONITOR_ERROR)
    except (OSError, ValueError, UnicodeError):
        raise CheckError(_MONITOR_ERROR) from None


def validate_keep_tcp_ports(value):
    """Validate the narrow explicit owner exception, not a generic ignore list."""
    need(type(value) in (list, tuple) and (not value or
         len(value) == 1 and type(value[0]) is int and value[0] == 10050),
         'Only an explicit TCP 10050 preservation exception is supported')
    return list(value)


def load_policy(path, uid=0):
    """Read one private role policy; return no permissive defaults on damage."""
    message = 'Unsafe or invalid host policy; expected a private version-1 TCP preservation policy'
    try:
        path = Path(path).absolute()
        for parent in (path.parent, *path.parent.parents):
            need(not parent.is_symlink(), message)
        parent = path.parent.lstat()
        need(stat.S_ISDIR(parent.st_mode) and parent.st_uid == uid
             and stat.S_IMODE(parent.st_mode) == 0o700, message)
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, message)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            need(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 4096, message)
            data = stream.read(4097)
        need(len(data) <= 4096, message)
        policy = json.loads(data)
        need(isinstance(policy, dict) and set(policy) == {'version', 'keepTcpPorts'}
             and type(policy['version']) is int and policy['version'] == 1, message)
        return {'version': 1, 'keepTcpPorts': validate_keep_tcp_ports(policy['keepTcpPorts'])}
    except (OSError, ValueError, TypeError, UnicodeError):
        raise CheckError(message) from None


def verified_monitoring_ports(runner=None, root=Path('/'), uid=0, keep_tcp_ports=()):
    """Keep an owner-approved port, otherwise verify every listener as Zabbix."""
    # The explicit policy keeps the firewall exception stable even while this
    # pre-existing service is temporarily down. It grants no process identity,
    # package trust or exception for any other listener/port.
    explicit = validate_keep_tcp_ports(keep_tcp_ports)
    if explicit:
        return explicit
    rows = _run(runner, 'ss', '-H', '-lntp', 'sport = :10050').splitlines()
    if not rows:
        return []
    pids = set()
    for row in rows:
        fields = row.split()
        need(len(fields) >= 6 and fields[0] == 'LISTEN', _MONITOR_ERROR)
        endpoint = fields[3].rsplit(':', 1)
        need(len(endpoint) == 2 and endpoint[1] == '10050', _MONITOR_ERROR)
        # Under root ss must identify every owner. Do not trust its displayed
        # process name: verify the kernel /proc executable for each PID instead.
        owners = re.findall(r'\("[^"\n]+",pid=([1-9][0-9]*),fd=[0-9]+\)', row)
        all_pids = re.findall(r'\bpid=([0-9]+)', row)
        need(owners and len(owners) == len(all_pids), _MONITOR_ERROR)
        pids.update(int(pid) for pid in owners)
    for pid in sorted(pids):
        _verified_agent(pid, runner, Path(root), uid)
    return [10050]


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    need(bool(args) and (args[0] == 'wireguard' and len(args) == 1
                         or args[0] == 'monitoring' and len(args) in (1, 2)),
         'Usage: host_checks.py wireguard | monitoring [private-policy.json]')
    if args[0] == 'wireguard':
        result = wireguard_interfaces()
    else:
        policy = load_policy(args[1]) if len(args) == 2 else {'keepTcpPorts': []}
        result = verified_monitoring_ports(keep_tcp_ports=policy['keepTcpPorts'])
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except CheckError as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
