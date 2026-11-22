#!/usr/bin/env python3
"""Private RPC for a bounded owned EU-UDP-port repair; stdout is captured only.

Snapshots contain the role's already-owned WireGuard configuration. Never run
this tool interactively or log its JSON output; the local repair driver stores
it only in the installer's private 0600 state.
"""
import base64
import configparser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

import repair_udp_remote as helper


class Error(RuntimeError):
    pass


def need(ok, message):
    if not ok:
        raise Error(message)


def command(*args):
    p = subprocess.run(args, capture_output=True, text=True)
    need(p.returncode == 0, 'Owned repair command failed: ' + args[0])
    return p.stdout


def paths(plan, role):
    stage = '/root/spn-cascade-managed-' + plan['id']
    wg = '/etc/wireguard/' + plan['link']['interface'] + '.conf'
    if role == 'ru':
        own = '/opt/spn-cascade-ru/branches/' + plan['id']
        return [wg, own + '/wg.conf', stage + '/wg.conf', stage + '/preflight-ok',
                '/opt/spn-cascade-ru/registry.json']
    return [wg, '/opt/spn-cascade/wg.conf', stage + '/wg.conf', stage + '/preflight-ok',
            '/opt/spn-cascade/.spn-parameters', '/opt/spn-cascade/firewall.sha256',
            '/etc/ufw/user.rules', '/etc/ufw/user6.rules']


def snapshot(names):
    result = {}
    for name in names:
        p = Path(name)
        need(not any(x.is_symlink() for x in (p, *p.parents)), 'Unsafe repair path')
        s = p.stat()
        need(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and s.st_nlink == 1
             and not stat.S_IMODE(s.st_mode) & 0o022, 'Unsafe repair file')
        result[name] = {'data': base64.b64encode(p.read_bytes()).decode(), 'mode': stat.S_IMODE(s.st_mode)}
    return result


def decode(item):
    return base64.b64decode(item['data'], validate=True)


def write(name, data, mode):
    p = Path(name)
    fd, tmp = tempfile.mkstemp(prefix='.spn-udp-repair-', dir=p.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, p)
        parent = os.open(p.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def runtime(plan, role, source):
    p = configparser.ConfigParser(interpolation=None)
    p.optionxform = str
    p.read_string(source.decode('ascii'))
    name = plan['link']['interface']
    if role == 'eu':
        command('wg', 'set', name, 'listen-port', str(plan['link']['port']))
    else:
        command('wg', 'set', name, 'peer', p['Peer']['PublicKey'], 'endpoint',
                plan['eu']['ip'] + ':' + str(plan['link']['port']))


def restore(before, plan, role):
    for name, item in before.items():
        write(name, decode(item), item['mode'])
    if role == 'eu':
        command('ufw', 'reload')
        command('sha256sum', '--check', '--status', '/opt/spn-cascade/firewall.sha256')
    runtime(plan, role, decode(before['/etc/wireguard/' + plan['link']['interface'] + '.conf']))


def main():
    request = json.load(sys.stdin)
    before, after, role, action = (request[k] for k in ('beforePlan', 'afterPlan', 'role', 'action'))
    need(role in ('ru', 'eu') and action in ('inspect', 'apply', 'rollback'), 'Invalid repair action')
    need(re.fullmatch('[0-9a-f]{32}', before['id']) and re.fullmatch('spnwg[a-z0-9]{6}', before['link']['interface']),
         'Invalid repair identity')
    expected = json.loads(json.dumps(before)); expected['link']['port'] = after['link']['port']
    need(after == expected and before['format'] == 2 and type(after['link']['port']) is int
         and (after['link']['port'] == 443 or 1024 <= after['link']['port'] <= 65535)
         and after['link']['port'] not in (2222, 2443, before['eu']['sshPort']), 'Repair may change only the EU UDP port')
    names = paths(before, role)
    lockfd = os.open('/run/lock/spn-cascade-ru.lock' if role == 'ru' else '/run/lock/spn-cascade-eu-repair.lock',
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, 'r+') as lock:
        metadata = os.fstat(lock.fileno())
        need(metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1,
             'Unsafe repair lock')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = snapshot(names)
        stage = '/root/spn-cascade-managed-' + before['id']
        owner = Path(stage + '/.spn-deployment').read_text().strip()
        need(owner == before['id'] + '|' + role, 'Staged deployment owner changed')
        if action == 'inspect':
            print(json.dumps(current)); return
        backup = request['snapshot']
        need(set(backup) == set(names), 'Repair snapshot paths differ')
        if action == 'rollback':
            need(current == request['expected'], 'Repair files changed concurrently; rollback refused')
            restore(backup, before, role)
            print(json.dumps({'restored': True})); return
        need(current == backup, 'Repair files changed concurrently')
        changes = request['changes']
        need(set(changes) <= set(names) and all(name not in changes for name in
             ('/etc/ufw/user.rules', '/etc/ufw/user6.rules', '/opt/spn-cascade/firewall.sha256')),
             'Invalid repair replacement paths')
        live = '/etc/wireguard/' + before['link']['interface'] + '.conf'
        need(all(decode(backup[name]) == decode(backup[live]) for name in names if name.endswith('/wg.conf')),
             'Owned WireGuard files differ')
        if role == 'eu':
            need(decode(backup['/opt/spn-cascade/.spn-parameters']).decode().strip() == helper.eu_parameters(before),
                 'EU ownership parameters differ')
            command('sha256sum', '--check', '--status', '/opt/spn-cascade/firewall.sha256')
            need(not command('ss', '-H', '-lnu', 'sport = :' + str(after['link']['port'])).strip(),
                 'Requested EU UDP port is already listening')
        else:
            need(decode(backup[stage + '/preflight-ok']).decode().strip() == helper.ru_preflight(before),
                 'RU ownership parameters differ')
        try:
            if role == 'eu':
                command('ufw', 'allow', 'from', before['ru']['ip'], 'to', 'any', 'port',
                        str(after['link']['port']), 'proto', 'udp', 'comment', 'SPN-CASCADE-WG')
            for name, item in changes.items():
                write(name, base64.b64decode(item, validate=True), backup[name]['mode'])
            runtime(after, role, base64.b64decode(changes[live], validate=True))
            if role == 'eu':
                command('ufw', '--force', 'delete', 'allow', 'from', before['ru']['ip'], 'to', 'any', 'port',
                        str(before['link']['port']), 'proto', 'udp')
                write('/opt/spn-cascade/firewall.sha256', helper.firewall_manifest(),
                      backup['/opt/spn-cascade/firewall.sha256']['mode'])
                command('sha256sum', '--check', '--status', '/opt/spn-cascade/firewall.sha256')
            print(json.dumps(snapshot(names)))
        except BaseException:
            restore(backup, before, role)
            raise


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Owned UDP repair stopped; private error details are not logged. Restore status must be checked.', file=sys.stderr)
        raise SystemExit(1)
