"""Pinned OpenSSH master with a password delivered over a private PTY only."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import re
import select
import signal
import stat
import subprocess
import tempfile
import termios
import time
import tty


class SSHLoginError(ValueError):
    pass


def validate_password(value):
    if (not isinstance(value, str) or len(value) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Недопустимый пароль VPS')
    return value


class PasswordMaster:
    """Never forwards SSH prompt text, passwords, environment, or agent to logs."""
    def __init__(self, ip, port, known_hosts, identity_file, password='', timeout=50):
        self.ip, self.port, self.known = ip, port, Path(known_hosts)
        self.identity, self.password = Path(identity_file), validate_password(password)
        self.timeout, self.process, self.fd, self.temporary = timeout, None, None, None

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='spn-provision-ssh-')
        os.chmod(self.temporary.name, 0o700)
        self.control = str(Path(self.temporary.name) / 'ssh')
        master, slave = pty.openpty()
        tty.setraw(slave)
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        command = ['ssh', '-F', '/dev/null', '-M', '-N', '-S', self.control, '-p', str(self.port),
                   '-o', 'ControlPersist=no', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'UserKnownHostsFile=' + str(self.known), '-o', 'GlobalKnownHostsFile=/dev/null',
                   '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'ConnectTimeout=15',
                   '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                   '-o', 'ConnectionAttempts=1', '-o', 'NumberOfPasswordPrompts=1',
                   '-o', 'PreferredAuthentications=publickey,password,keyboard-interactive',
                   '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                   '-o', 'ForwardAgent=no', '-i', str(self.identity), 'root@' + self.ip]
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C', 'LC_ALL': 'C'}
        def session():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        try:
            self.fd = master
            self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                            env=environment, preexec_fn=session, close_fds=True)
            os.close(slave)
            slave = None
            deadline, buffer, supplied = time.monotonic() + self.timeout, b'', False
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise SSHLoginError('Не удалось войти по SSH. Проверьте пароль root, IP, порт и разрешённый способ входа.')
                if Path(self.control).exists():
                    check = subprocess.run(['ssh', '-F', '/dev/null', '-S', self.control, '-O', 'check',
                                            'root@' + self.ip], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                    if check.returncode == 0:
                        self.password = ''
                        return self
                ready, _, _ = select.select([master], [], [], .15)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b''
                    buffer = (buffer + chunk)[-8192:]
                    if re.search(rb'(?i)(?:password|passphrase)[^\r\n]*:\s*$', buffer):
                        if supplied or not self.password:
                            raise SSHLoginError('Для входа на эту VPS нужен пароль root. Введите его и продолжите задачу.')
                        os.write(master, self.password.encode() + b'\n')
                        self.password, supplied, buffer = '', True, b''
            raise SSHLoginError('VPS не завершила подключение по SSH за отведённое время.')
        except BaseException:
            if slave is not None:
                os.close(slave)
            self.__exit__()
            raise

    def run(self, command, data=None, capture=False):
        result = subprocess.run(['ssh', '-F', '/dev/null', '-S', self.control,
                                 '-o', 'ProxyCommand=/usr/bin/false', '-o', 'BatchMode=yes',
                                 '-p', str(self.port), 'root@' + self.ip, command], input=data,
                                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=40)
        if result.returncode:
            raise SSHLoginError('Служебный SSH-доступ к VPS не подтверждён. Повторите задачу.')
        return result.stdout

    def __exit__(self, *args):
        self.password = ''
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.temporary:
            self.temporary.cleanup()
