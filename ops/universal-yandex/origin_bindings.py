#!/usr/bin/env python3
"""Add one explicit HTTPS origin address without changing the original install."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile


class BindingError(ValueError):
    pass


def need(value, message):
    if not value:
        raise BindingError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def ipv4(value):
    address = ipaddress.IPv4Address(value)
    need(str(address) == value and address.is_global and not address.is_multicast, 'Expected a public IPv4')
    return value


def extra_config(base, old, extra):
    ipv4(old); ipv4(extra)
    need(old != extra and base.count(('    listen ' + old + ':').encode()) == 4,
         'Expected the four original owned origin listeners')
    return base.replace(('    listen ' + old + ':').encode(), ('    listen ' + extra + ':').encode())


def local_receipt(store, plan, base):
    value = store.load('origin-bindings.receipt.json')
    if value is None:
        return None
    extra = value.get('extraIp') if isinstance(value, dict) else None
    expected = {'version': 1, 'deployment': plan['id'], 'domain': plan['domain'], 'code': plan['code'],
                'oldIp': plan['ip'], 'extraIp': extra, 'baseSha256': digest(base),
                'extraSha256': digest(extra_config(base, plan['ip'], extra)),
                'passed': True, 'baseConfigUnchanged': True}
    need(value == expected, 'Saved additional origin binding does not match this deployment')
    return value


def check_dns(client, zone, store, plan, receipt):
    original = store.load('dns.receipt.json')
    need(isinstance(original, dict) and zone['id'] == original['zone_id'], 'Original DNS ownership is missing')
    rows = client._list('/zones/' + zone['id'] + '/dns_records', {'name': plan['domain'], 'match': 'all'})
    expected = {key: original[key] for key in ('id', 'name', 'type', 'proxied', 'ttl', 'comment')}
    expected['content'] = receipt['extraIp']
    need(len(rows) == 1 and all(rows[0].get(key) == value for key, value in expected.items()),
         'Additional origin DNS no longer matches its owned record; DNS was not changed')
    return rows[0]


def read(path, *, modes=(0o600,), owner=None):
    owner = os.geteuid() if owner is None else owner
    need(not any(p.is_symlink() for p in (path, *path.parents)), 'Unsafe origin binding path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) in modes and info.st_size <= 131072, 'Unsafe owned origin file')
        return stream.read(131073)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_once(path, raw):
    if path.exists() or path.is_symlink():
        need(read(path) == raw, 'Owned binding journal changed')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    fsync_directory(path.parent)


def command(*args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    need(result.returncode == 0, 'Origin binding system check failed; private details suppressed')
    return result.stdout


def health(domain, ip, code):
    context = ssl.create_default_context(); context.set_alpn_protocols(['http/1.1'])
    with socket.create_connection((ip, 443), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=domain) as secured:
            secured.sendall(('GET /health HTTP/1.1\r\nHost: ' + domain + '\r\nConnection: close\r\n\r\n').encode())
            response = http.client.HTTPResponse(secured); response.begin()
            need(response.status == 200 and json.loads(response.read(4097)) == {'status': 'ok', 'deployment': code},
                 'Additional origin HTTPS identity was not verified')


class Bindings:
    def __init__(self, deployment, domain, old_ip, base_hash, *, root=Path('/'), run=command, probe=health):
        need(re.fullmatch('[0-9a-f]{32}', deployment) and re.fullmatch('[0-9a-f]{64}', base_hash), 'Invalid binding identity')
        need(re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,251}[a-z0-9]', domain), 'Invalid origin domain')
        self.id, self.domain, self.old, self.base_hash = deployment, domain, ipv4(old_ip), base_hash
        self.root, self.run, self.probe = Path(root), run, probe
        self.node = self.root / 'opt/spn-yandex-cdn'
        self.stage = self.root / ('root/spn-yandex-managed-' + deployment)
        self.site = self.root / 'etc/nginx/sites-available/spn-yandex-cdn.conf'
        self.enabled = self.root / 'etc/nginx/sites-enabled'
        self.directory = self.node / 'extra-listener-v1'
        self.link = self.enabled / ('spn-yandex-extra-' + deployment + '.conf')
        self.extra_file = self.directory / 'nginx.conf'

    def ownership(self):
        for directory in (self.node, self.stage):
            need(directory.is_dir() and not any(p.is_symlink() for p in (directory, *directory.parents))
                 and directory.stat().st_uid == os.geteuid()
                 and stat.S_IMODE(directory.stat().st_mode) == 0o700, 'Unsafe owned origin directory')
        need(read(self.node / '.spn-deployment') == (self.id + '\n').encode(), 'Origin deployment differs')
        params = read(self.node / '.spn-parameters').decode().strip().split('|')
        need(len(params) == 5 and params[0] == self.domain and params[1] == self.old and params[4] == self.id,
             'Original origin parameters differ')
        base = read(self.site, modes=(0o600, 0o644))
        need(digest(base) == self.base_hash and read(self.stage / 'nginx.conf') == base, 'Original Nginx configuration changed')
        manifest = read(self.node / 'files.sha256')
        expected = {'/opt/spn-yandex-cdn/node.env', '/opt/spn-yandex-cdn/docker-compose.yml',
                    '/opt/spn-yandex-cdn/docker-daemon.json', '/etc/docker/daemon.json',
                    '/etc/nginx/sites-available/spn-yandex-cdn.conf', '/etc/nginx/nginx.conf',
                    '/etc/letsencrypt/renewal-hooks/deploy/spn-yandex-nginx'}
        rows = [line.split('  ', 1) for line in manifest.decode().splitlines()]
        need(len(rows) == 7 and {line[1] for line in rows if len(line) == 2} == expected,
             'Owned origin file manifest differs')
        for saved_hash, name in rows:
            need(re.fullmatch('[0-9a-f]{64}', saved_hash)
                 and digest(read(self.root / name.lstrip('/'), modes=(0o600, 0o644, 0o700, 0o755))) == saved_hash,
                 'An owned origin file changed')
        original_link = self.enabled / 'spn-yandex-cdn.conf'
        need(original_link.is_symlink() and os.readlink(original_link) == '/etc/nginx/sites-available/spn-yandex-cdn.conf',
             'Original origin site link changed')
        need(set(self.enabled.iterdir()) <= {original_link, self.link}, 'An unrelated enabled site exists')
        return base

    def assigned(self, extra):
        rows = json.loads(self.run('ip', '-j', '-4', 'address', 'show', 'up', 'scope', 'global'))
        values = [v['local'] for row in rows for v in row.get('addr_info', []) if v.get('family') == 'inet']
        need(values.count(self.old) == 1 and values.count(extra) == 1, 'Both origin addresses must belong to this VPS')

    def intent(self, base, extra, code):
        need(re.fullmatch('[a-z0-9]{6}', code), 'Invalid origin code')
        return {'version': 1, 'deployment': self.id, 'domain': self.domain, 'code': code,
                'oldIp': self.old, 'extraIp': ipv4(extra), 'baseSha256': digest(base),
                'extraSha256': digest(extra_config(base, self.old, extra))}

    def verify(self):
        base = self.ownership()
        need(self.directory.is_dir() and not self.directory.is_symlink()
             and self.directory.stat().st_uid == os.geteuid()
             and stat.S_IMODE(self.directory.stat().st_mode) == 0o700, 'Unsafe additional origin journal')
        intent = json.loads(read(self.directory / 'intent.json'))
        need(intent == self.intent(base, intent['extraIp'], intent['code']), 'Additional origin intent differs')
        need(read(self.extra_file) == extra_config(base, self.old, intent['extraIp']), 'Additional origin configuration changed')
        need(self.link.is_symlink() and os.readlink(self.link) == str(self.extra_file), 'Additional origin site link differs')
        result = {**intent, 'passed': True, 'baseConfigUnchanged': True}
        need(json.loads(read(self.directory / 'complete.json')) == result, 'Additional origin is not verified')
        return result

    def install(self, extra, code):
        base = self.ownership(); self.assigned(ipv4(extra))
        intent = self.intent(base, extra, code)
        if not self.directory.exists():
            self.directory.mkdir(mode=0o700); fsync_directory(self.node)
        need(not self.directory.is_symlink() and self.directory.stat().st_uid == os.geteuid()
             and stat.S_IMODE(self.directory.stat().st_mode) == 0o700, 'Unsafe additional origin journal')
        write_once(self.directory / 'intent.json', encoded(intent))
        write_once(self.extra_file, extra_config(base, self.old, extra))
        main = read(self.root / 'etc/nginx/nginx.conf', modes=(0o600, 0o644))
        pattern = b'include /etc/nginx/sites-enabled/*;'
        need(main.count(pattern) == 1, 'Owned Nginx include layout changed')
        desired_include = ('include ' + str(self.site) + ';\n    include ' + str(self.extra_file) + ';').encode()
        with tempfile.TemporaryDirectory(prefix='extra-check-', dir=self.directory) as temporary:
            candidate = Path(temporary) / 'nginx.conf'; candidate.write_bytes(main.replace(pattern, desired_include))
            candidate.chmod(0o600); self.run('nginx', '-t', '-c', str(candidate))
        created = not self.link.exists() and not self.link.is_symlink()
        if not created:
            need(self.link.is_symlink() and os.readlink(self.link) == str(self.extra_file), 'An unrelated extra site already exists')
        else:
            os.symlink(self.extra_file, self.link); fsync_directory(self.enabled)
        try:
            self.run('nginx', '-t'); self.run('systemctl', 'reload', 'nginx')
            self.probe(self.domain, self.old, code); self.probe(self.domain, extra, code)
            result = {**intent, 'passed': True, 'baseConfigUnchanged': True}
            write_once(self.directory / 'complete.json', encoded(result))
            need(self.verify() == result, 'Additional origin verification differs')
            return result
        except BaseException:
            if created and self.link.is_symlink() and os.readlink(self.link) == str(self.extra_file):
                self.link.unlink(); fsync_directory(self.enabled)
                self.run('nginx', '-t'); self.run('systemctl', 'reload', 'nginx')
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'verify'))
    parser.add_argument('--deployment', required=True); parser.add_argument('--domain', required=True)
    parser.add_argument('--ip', required=True); parser.add_argument('--base-sha256', required=True)
    parser.add_argument('--extra-ip'); parser.add_argument('--code')
    args = parser.parse_args(argv)
    need(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the owned Linux origin as root')
    manager = Bindings(args.deployment, args.domain, args.ip, args.base_sha256)
    # The caller also holds the original local deployment journal lock.
    manager.ownership()
    fd = os.open(manager.stage / '.origin-bindings.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, 'Unsafe additional origin lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'install':
            need(args.extra_ip is not None and args.code is not None, 'Choose the additional IP and original code')
            result = manager.install(args.extra_ip, args.code)
        else:
            result = manager.verify()
        print(json.dumps(result, sort_keys=True))
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('Origin binding stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
