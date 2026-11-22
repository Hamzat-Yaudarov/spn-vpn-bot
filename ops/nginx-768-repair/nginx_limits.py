"""Scoped 768→4096 Nginx repair; no Xray, route, firewall or panel writes."""
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
import tempfile
import time

TARGETS = ('191.217.245.225', '191.217.252.188')
CONFIG = Path('/etc/nginx/nginx.conf')
BACKUPS = Path('/var/backups')
LIMIT = 4096
NOFILE = 8192
NGINX = '/usr/sbin/nginx'
SYSTEMCTL = '/usr/bin/systemctl'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}


class RepairError(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise RepairError(reason)


def directives(data, name):
    return list(re.finditer(rb'(?m)^[ \t]*' + name + rb'[ \t]+([0-9]+)[ \t]*;', data))


def render(before):
    require(isinstance(before, bytes) and 0 < len(before) <= 65536, 'unexpected config size')
    limits = directives(before, b'worker_connections')
    require(len(limits) == 1 and int(limits[0][1]) == 768, 'expected one worker_connections 768')
    match, = limits
    candidate = before[:match.start(1)] + b'4096' + before[match.end(1):]
    limits = directives(candidate, b'worker_rlimit_nofile')
    require(len(limits) <= 1, 'duplicate worker_rlimit_nofile')
    if limits:
        match, = limits
        target = max(NOFILE, int(match[1]))
        return candidate[:match.start(1)] + str(target).encode() + candidate[match.end(1):], target
    anchors = list(re.finditer(rb'(?m)^[ \t]*worker_processes[ \t]+(?:auto|[0-9]+)[ \t]*;[^\r\n]*(?:\r?\n|$)', candidate))
    require(len(anchors) == 1, 'expected one main worker_processes directive')
    end = anchors[0].end()
    newline = b'\r\n' if b'\r\n' in candidate else b'\n'
    prefix = b'' if candidate[:end].endswith(b'\n') else newline
    return candidate[:end] + prefix + b'worker_rlimit_nofile 8192;' + newline + candidate[end:], NOFILE


def command(label, *args):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=ENV)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepairError(label + ' did not complete') from exc
    require(result.returncode == 0, label + ' failed')
    return result.stdout.decode().strip()


def trusted_parents(path):
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'unsafe parent directory')


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_uid, info.st_gid)


def snapshot():
    fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                and not before.st_mode & 0o022, 'unexpected config ownership or mode')
        content = stream.read(65537)
        after = os.fstat(stream.fileno())
    require(0 < len(content) <= 65536 and fingerprint(before) == fingerprint(after)
            and fingerprint(CONFIG.lstat()) == fingerprint(after), 'config changed while reading')
    return content, after


def same(expected):
    content, info = snapshot()
    return content == expected[0] and fingerprint(info) == fingerprint(expected[1])


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


def stage(data, mode):
    fd, name = tempfile.mkstemp(prefix='.spn-nginx-limits-', suffix='.conf', dir=CONFIG.parent)
    path = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), mode.st_uid, mode.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(mode.st_mode))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def replace(staged, expected, data):
    require(same(expected), 'concurrent config edit; replacement refused')
    staged_identity = staged.lstat()
    os.replace(staged, CONFIG)
    # Capture the installed identity before any further operation: rollback must
    # never overwrite a different inode or a subsequent edit to this inode.
    installed = snapshot()
    # Rename changes ctime, but does not change these other attributes.
    attrs = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_mode', 'st_uid', 'st_gid')
    require(installed[0] == data and all(getattr(installed[1], k) == getattr(staged_identity, k) for k in attrs),
            'concurrent config replacement; rollback refused')
    return installed


def nofile(pid):
    data = (Path('/proc') / str(pid) / 'limits').read_text()
    match = re.search(r'(?m)^Max open files\s+(\d+|unlimited)\s+(\d+|unlimited)', data)
    require(match, 'could not inspect process file limits')
    return tuple(int(x) if x != 'unlimited' else 2**63 for x in match.groups())


def workers(master):
    rows = command('inspect workers', '/usr/bin/ps', '--ppid', str(master), '-o', 'pid=,args=')
    result = {}
    for row in rows.splitlines():
        fields = row.split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and fields[1].startswith('nginx: worker process') and 'shutting down' not in fields[1]:
            try:
                result[int(fields[0])] = nofile(int(fields[0]))[0]
            except FileNotFoundError:
                continue
    return result


def confirm_workers(master, old, target):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        now = workers(master)
        if len(now) >= len(old) and not set(now) & set(old) and min(now.values()) >= target:
            return sorted(now.values())
        time.sleep(0.25)
    raise RepairError('new worker limits did not verify')


def apply(target):
    require(sys.platform == 'linux' and os.geteuid() == 0 and target in TARGETS, 'wrong host or privileges')
    addresses = json.loads(command('inspect local IPv4', '/usr/sbin/ip', '-j', '-4', 'address', 'show', 'scope', 'global'))
    require(any(i.get('local') == target for row in addresses for i in row.get('addr_info', [])), 'expected IPv4 absent')
    require(command('check nginx active', SYSTEMCTL, 'is-active', 'nginx') == 'active', 'nginx is not active')
    master = int(command('inspect nginx master', SYSTEMCTL, 'show', 'nginx', '-p', 'MainPID', '--value'))
    require(master > 1, 'no nginx master')
    old = workers(master)
    require(old, 'no active workers')
    trusted_parents(CONFIG)
    original = snapshot()
    candidate, target_nofile = render(original[0])
    require(nofile(master)[1] >= target_nofile, 'master hard limit too low')
    command('original nginx -t', NGINX, '-t')

    trusted_parents(BACKUPS / 'placeholder')
    backup = Path(tempfile.mkdtemp(prefix=f'spn-nginx-768-{target}-', dir=BACKUPS))
    write_private(backup / 'nginx.before.conf', original[0])
    write_private(backup / 'nginx.candidate.conf', candidate)
    manifest = {'target': target, 'beforeSHA256': hashlib.sha256(original[0]).hexdigest(),
                'candidateSHA256': hashlib.sha256(candidate).hexdigest()}
    write_private(backup / 'manifest.json', (json.dumps(manifest) + '\n').encode())
    sync_dir(backup)
    sync_dir(BACKUPS)
    staged = stage(candidate, original[1])
    installed = None
    reload_attempted = False
    try:
        command('candidate nginx -t', NGINX, '-t', '-c', str(staged))
        installed = replace(staged, original, candidate)
        sync_dir(CONFIG.parent)
        require(installed[0] == candidate, 'installed config mismatch')
        command('installed nginx -t', NGINX, '-t')
        reload_attempted = True
        command('nginx graceful reload', SYSTEMCTL, 'reload', 'nginx')
        worker_limits = confirm_workers(master, old, target_nofile)
        require(same(installed), 'concurrent config edit after reload')
        require(command('verify nginx active', SYSTEMCTL, 'is-active', 'nginx') == 'active', 'nginx did not remain active')
    except BaseException as exc:
        if installed is not None:
            restore = stage(original[0], original[1])
            try:
                replace(restore, installed, original[0])
                sync_dir(CONFIG.parent)
                command('restored nginx -t', NGINX, '-t')
                if reload_attempted:
                    command('rollback nginx graceful reload', SYSTEMCTL, 'reload', 'nginx')
            finally:
                restore.unlink(missing_ok=True)
            raise RepairError('change failed; original restored; backup: ' + str(backup)) from exc
        raise
    finally:
        staged.unlink(missing_ok=True)
    return {'applied': True, 'target': target, 'workerConnections': LIMIT,
            'newWorkerSoftLimits': worker_limits, 'backup': str(backup), 'reloadOnly': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, choices=TARGETS)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = apply(args.target) if args.apply else {'planOnly': True, 'target': args.target,
        'workerConnections': LIMIT, 'workerNofileMinimum': NOFILE, 'reloadOnly': True}
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except RepairError as error:
        print('Nginx limits stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Nginx limits stopped: unexpected diagnostic failure; raw output suppressed', file=sys.stderr)
        sys.exit(1)
