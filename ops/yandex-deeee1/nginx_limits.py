"""Narrow, reversible Nginx connection-limit repair for the approved German VPS.

No VPN, DNS, certificates, firewall, accounts or container changes. The default
prints a plan only; --apply is explicitly for 194.48.250.102.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

TARGET = '194.48.250.102'
CONFIG = Path('/etc/nginx/nginx.conf')
BACKUP = Path('/var/backups/spn-nginx-limits-20261031')
CONNECTIONS = 8192
NOFILE = 65535
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}


def require(value, message):
    if not value:
        raise ValueError(message)


def render(original):
    require(isinstance(original, bytes) and 0 < len(original) <= 65536, 'Invalid main config size')
    connections = list(re.finditer(rb'(?m)^[ \t]*worker_connections[ \t]+([0-9]+)[ \t]*;', original))
    require(len(connections) == 1 and int(connections[0][1]) == 768,
            'Expected exactly one existing 768-connection limit; inspect before retry')
    match = connections[0]
    candidate = original[:match.start(1)] + str(CONNECTIONS).encode() + original[match.end(1):]
    limits = list(re.finditer(rb'(?m)^[ \t]*worker_rlimit_nofile[ \t]+([0-9]+)[ \t]*;', candidate))
    require(len(limits) <= 1, 'Duplicate file-limit directive')
    if limits:
        match = limits[0]
        if int(match[1]) < NOFILE:
            candidate = candidate[:match.start(1)] + str(NOFILE).encode() + candidate[match.end(1):]
    else:
        anchors = list(re.finditer(rb'(?m)^worker_processes[ \t]+auto[ \t]*;[^\r\n]*(?:\r?\n|$)', candidate))
        require(len(anchors) == 1, 'Expected one main worker_processes auto directive')
        end = anchors[0].end()
        separator = b'' if candidate[:end].endswith(b'\n') else b'\n'
        candidate = candidate[:end] + separator + b'worker_rlimit_nofile 65535;\n' + candidate[end:]
    return candidate


def command(args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=ENV, timeout=30, check=False)
    require(result.returncode == 0, 'Prerequisite command failed; raw output suppressed')
    return result.stdout.decode().strip()


def write_new(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_config():
    require(not any(p.is_symlink() for p in (CONFIG, *CONFIG.parents)), 'Config symlink refused')
    info = CONFIG.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o644, 'Unexpected config metadata')
    return CONFIG.read_bytes()


def replace_config(data):
    temporary = CONFIG.with_name('.nginx.conf.spn-limits-new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    owned = os.fstat(fd)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        os.replace(temporary, CONFIG)
        sync_dir(CONFIG.parent)
    finally:
        os.close(fd)
        try:
            remaining = temporary.lstat()
        except FileNotFoundError:
            pass
        else:
            if (remaining.st_dev, remaining.st_ino) == (owned.st_dev, owned.st_ino):
                temporary.unlink()


def apply():
    require(sys.platform == 'linux' and os.geteuid() == 0, 'Run on the approved VPS as root')
    addresses = json.loads(command(['/usr/sbin/ip', '-j', '-4', 'address', 'show', 'scope', 'global']))
    require(any(i.get('local') == TARGET for row in addresses for i in row.get('addr_info', [])),
            'Wrong VPS address')
    require(command(['/usr/bin/systemctl', 'is-active', 'nginx']) == 'active', 'Nginx must be active')
    master = int(command(['/usr/bin/systemctl', 'show', 'nginx', '-p', 'MainPID', '--value']))
    limits = Path('/proc') / str(master) / 'limits'
    row = next(line for line in limits.read_text().splitlines() if line.startswith('Max open files'))
    hard = row.split()[4]
    require(hard == 'unlimited' or int(hard) >= NOFILE, 'Master hard file limit is too low')
    command(['/usr/sbin/nginx', '-t'])
    original = read_config()
    candidate = render(original)
    require(not BACKUP.exists() and not BACKUP.is_symlink(), 'Backup already exists; inspect before retry')
    for parent in (BACKUP.parent, *BACKUP.parent.parents):
        info = parent.stat()
        require(not parent.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022,
                'Unsafe backup parent')
    BACKUP.mkdir(mode=0o700)
    write_new(BACKUP / 'nginx.before.conf', original, 0o600)
    write_new(BACKUP / 'nginx.candidate.conf', candidate, 0o600)
    manifest = {'target': TARGET, 'workerConnectionsBefore': 768, 'workerConnectionsAfter': CONNECTIONS,
                'workerNofileMinimum': NOFILE, 'beforeSha256': hashlib.sha256(original).hexdigest(),
                'candidateSha256': hashlib.sha256(candidate).hexdigest()}
    write_new(BACKUP / 'manifest.json', (json.dumps(manifest, sort_keys=True) + '\n').encode(), 0o600)
    sync_dir(BACKUP)
    sync_dir(BACKUP.parent)
    require(read_config() == original, 'Config changed concurrently')
    replaced = False
    try:
        replaced = True
        replace_config(candidate)
        command(['/usr/sbin/nginx', '-t'])
        command(['/usr/bin/systemctl', 'reload', 'nginx'])
        require(command(['/usr/bin/systemctl', 'is-active', 'nginx']) == 'active'
                and read_config() == candidate, 'Reload verification failed')
    except BaseException:
        if replaced:
            current = read_config()
            require(current in (original, candidate), 'Concurrent edit; automatic restore refused')
            if current == candidate:
                replace_config(original)
                command(['/usr/sbin/nginx', '-t'])
                command(['/usr/bin/systemctl', 'reload', 'nginx'])
        raise
    return {'applied': True, 'target': TARGET, 'workerConnections': CONNECTIONS,
            'workerNofileMinimum': NOFILE, 'backup': str(BACKUP),
            'nginx': 'active', 'vpsRestarted': False, 'vpnRestarted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = apply() if args.apply else {'planOnly': True, 'network': False, 'target': TARGET,
        'workerConnections': CONNECTIONS, 'workerNofileMinimum': NOFILE, 'reloadOnly': True}
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Nginx limits change stopped. Inspect backup/current configuration before retry.', file=sys.stderr)
        sys.exit(1)
