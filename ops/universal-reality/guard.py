#!/usr/bin/env python3
"""Owned HAProxy TCP front guard for REALITY; no TLS termination or network reload."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time

import core_fd_health

CONFIG_MODES = (0o600, 0o640, 0o644)

class GuardError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise GuardError(message)


def command(*args, allowed=(0,)):
    result = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    need(result.returncode in allowed, 'HAProxy guard command failed; details suppressed')
    return result.stdout.strip()


def read_owned(path, *, modes=(0o600,), limit=131072):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as file:
            info = os.fstat(file.fileno())
            need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) in modes, 'Unsafe owned guard file')
            data = file.read(limit + 1)
            need(len(data) <= limit, 'Guard file is too large')
            return data
    except FileNotFoundError:
        return None


def replace_owned(path, data, *, expected, mode, expected_modes=None):
    modes = expected_modes or (mode,)
    need(read_owned(path, modes=modes) == expected, 'Guard configuration changed concurrently')
    fd, name = tempfile.mkstemp(prefix='.spn-guard-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            os.fchmod(file.fileno(), mode)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        need(read_owned(path, modes=modes) == expected, 'Guard configuration changed before commit')
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(name).unlink(missing_ok=True)


def tcp_connect(ip, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.4)
            connection.connect((ip, port))
        return True
    except OSError:
        return False


def partial_hello_probe():
    """One credential-free proof that an incomplete hello cannot reach/hold core."""
    start = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(7)
            connection.connect(('127.0.0.1', 443))
            # Record length100, ClientHello body96, but no body is delivered.
            connection.sendall(bytes.fromhex('160301006401000060'))
            try:
                data = connection.recv(1)
            except ConnectionResetError:
                data = b''
            need(data == b'', 'Incomplete ClientHello was not rejected')
    except OSError:
        raise GuardError('Incomplete ClientHello remained open beyond the guard deadline') from None
    elapsed = time.monotonic() - start
    need(elapsed <= 7, 'Incomplete ClientHello rejection exceeded 7 seconds')
    return {'partialHelloRejected': True, 'observedDeadlineMs': round(elapsed * 1000)}


def wait_backend(*, ready=lambda: tcp_connect('127.0.0.1', 2443), now=time.monotonic, sleep=time.sleep):
    deadline = now() + 45
    while now() < deadline:
        if ready():
            return
        sleep(min(1, max(0, deadline - now())))
    raise GuardError('Loopback REALITY backend 127.0.0.1:2443 did not become ready within 45 seconds')


class Guard:
    def __init__(self, ip, deployment, *, root=Path('/'), run=command, ready=tcp_connect,
                 wait=wait_backend, fd=core_fd_health.inspect, template=None, preread=partial_hello_probe):
        need(re.fullmatch(r'[0-9a-f]{32}', deployment) is not None, 'Invalid deployment identity')
        # Main installer validates global/unicast address; defend against shell/path text here.
        import ipaddress
        need(str(ipaddress.IPv4Address(ip)) == ip, 'Expected a plain IPv4')
        self.ip, self.id, self.root, self.run = ip, deployment, Path(root), run
        self.node = self.root / 'opt/spn-reality-node'
        self.state = self.node / 'guard'
        self.config = self.root / 'etc/haproxy/haproxy.cfg'
        self.ready, self.wait, self.fd = ready, wait, fd
        self.preread = preread
        self.template = Path(template) if template else Path(__file__).with_name('haproxy-reality.cfg')

    def ownership(self):
        need(self.node.is_dir() and not any(path.is_symlink() for path in (self.node, *self.node.parents)),
             'Unsafe owned node directory')
        info = self.node.stat()
        need(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'Unsafe owned node permissions')
        need(read_owned(self.node / '.spn-deployment') == (self.id + '\n').encode(), 'Node deployment identity differs')
        need(self.config.parent.is_dir() and not any(path.is_symlink() for path in (self.config.parent, *self.config.parent.parents)),
             'Unsafe HAProxy configuration directory')

    def rendered(self):
        raw = read_owned(self.template)
        need(raw is not None and raw.count(b'@DEPLOYMENT_ID@') == 1, 'Unexpected guard template')
        return raw.replace(b'@DEPLOYMENT_ID@', self.id.encode())

    def effective_rendered(self, *, require_ready=True):
        """Keep the original template/hash, accepting only a validated SNI upgrade."""
        base = self.rendered()
        directory = self.state / 'sni-v1'
        if not directory.exists() and not directory.is_symlink():
            return base
        import reality_sni_guard
        value = reality_sni_guard.upgrade_state(self.state, self.id, base,
                                                require_ready=require_ready)
        return value['desired']

    def addresses(self, port):
        rows = self.run('ss', '-H', '-lnt', 'sport = :' + str(port)).splitlines()
        need(all(len(row.split()) >= 4 for row in rows), 'Unexpected listener data')
        return [row.split()[3] for row in rows]

    def backend(self):
        self.wait(ready=lambda: self.ready('127.0.0.1', 2443))
        need(self.addresses(2443) == ['127.0.0.1:2443'], 'REALITY backend must listen only on 127.0.0.1:2443')

    def install(self):
        self.ownership()
        self.backend()
        base = self.rendered()
        desired = self.effective_rendered(require_ready=False)
        if desired != base:
            # A previous SNI upgrade may have stopped after its durable intent.
            # Finish that exact upgrade before the ordinary installer proceeds.
            import reality_sni_guard
            policy = reality_sni_guard.upgrade_state(self.state, self.id, base,
                                                     require_ready=False)
            if policy['ready'] is None:
                reality_sni_guard.SniGuard(self.ip, self.id, policy['intent']['sni'],
                                           manager=self).install()
        # Validate candidate before touching the system configuration/service.
        with tempfile.TemporaryDirectory(prefix='spn-guard-check-', dir=self.node) as directory:
            candidate = Path(directory) / 'haproxy.cfg'
            candidate.write_bytes(desired)
            candidate.chmod(0o600)
            self.run('haproxy', '-c', '-f', str(candidate))
        self.state.mkdir(mode=0o700, exist_ok=True)
        need(not self.state.is_symlink() and self.state.stat().st_uid == os.geteuid()
             and stat.S_IMODE(self.state.stat().st_mode) == 0o700, 'Unsafe guard state directory')
        current = read_owned(self.config, modes=CONFIG_MODES)
        need(current is not None, 'Install the Ubuntu haproxy package before guard installation')
        raw_intent = read_owned(self.state / 'intent.json')
        if raw_intent is None:
            conffiles = self.run('dpkg-query', '-W', '-f', '${Conffiles}', 'haproxy')
            checksums = re.findall(r'^\s*/etc/haproxy/haproxy\.cfg\s+([0-9a-f]{32})\s*$', conffiles, re.M)
            need(len(checksums) == 1 and hashlib.md5(current).hexdigest() == checksums[0],
                 'Existing HAProxy config is not pristine Ubuntu configuration; refusing to replace it')
            intent = {'deployment': self.id, 'beforeHash': hashlib.sha256(current).hexdigest(),
                      'afterHash': hashlib.sha256(base).hexdigest(),
                      'beforeMode': stat.S_IMODE(self.config.stat().st_mode),
                      'wasActive': self.run('systemctl', 'is-active', 'haproxy', allowed=(0, 3, 4)) == 'active',
                      'wasEnabled': self.run('systemctl', 'is-enabled', 'haproxy', allowed=(0, 1, 3, 4)) == 'enabled'}
            replace_owned(self.state / 'before.cfg', current, expected=None, mode=0o600)
            replace_owned(self.state / 'intent.json', (json.dumps(intent, sort_keys=True) + '\n').encode(), expected=None, mode=0o600)
        else:
            intent = json.loads(raw_intent)
        before = read_owned(self.state / 'before.cfg')
        need(intent['deployment'] == self.id and before is not None
             and hashlib.sha256(before).hexdigest() == intent['beforeHash']
             and hashlib.sha256(base).hexdigest() == intent['afterHash']
             and intent['beforeMode'] in CONFIG_MODES, 'Owned guard backup/intent differs')
        need(current in (before, desired), 'HAProxy config was modified outside this guard')
        complete = read_owned(self.state / 'ready.json')
        try:
            if current != desired:
                replace_owned(self.config, desired, expected=current, mode=0o644, expected_modes=CONFIG_MODES)
            self.run('systemctl', 'enable', 'haproxy')
            if complete is None or self.run('systemctl', 'is-active', 'haproxy', allowed=(0, 3, 4)) != 'active':
                self.run('systemctl', 'restart', 'haproxy')
            result = self.verify()
            if complete is None:
                result.update(self.preread())
                replace_owned(self.state / 'ready.json', b'{"guardInstalled":true,"partialHelloRejected":true}\n', expected=None, mode=0o600)
            return result
        except BaseException:
            # Never overwrite an administrator's concurrent configuration change.
            if desired == base and read_owned(self.config, modes=CONFIG_MODES) == desired:
                replace_owned(self.config, before, expected=desired, mode=intent['beforeMode'], expected_modes=CONFIG_MODES)
                self.run('systemctl', 'restart' if intent['wasActive'] else 'stop', 'haproxy')
                if not intent['wasEnabled']:
                    self.run('systemctl', 'disable', 'haproxy')
            raise

    def verify(self, *, allow_pending_sni=False):
        self.ownership()
        desired = self.effective_rendered(require_ready=not allow_pending_sni)
        need(read_owned(self.config, modes=CONFIG_MODES) == desired, 'Active guard config differs')
        self.run('haproxy', '-c', '-f', str(self.config))
        self.run('systemctl', 'is-active', '--quiet', 'haproxy')
        self.backend()
        addresses = self.addresses(443)
        need(bool(addresses) and set(addresses) <= {'0.0.0.0:443', '*:443', '[::]:443'}
             and self.ready(self.ip, 443), 'Public TCP443 guard is not IPv4-ready')
        health = self.fd(self.id)
        return {'guardVerified': True, 'tlsTerminated': False, 'clientHelloDeadlineSeconds': 5,
                'maxConnections': 8192, 'backendLoopbackOnly': True, 'coreHealth': health}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'verify'))
    parser.add_argument('--ip', required=True)
    parser.add_argument('--deployment', required=True)
    args = parser.parse_args(argv)
    need(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux VPS as root')
    print(json.dumps(getattr(Guard(args.ip, args.deployment), args.action)(), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
