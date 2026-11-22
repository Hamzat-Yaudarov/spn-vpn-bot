"""Private SSH trust for a managed VPS; never changes global known_hosts.

The caller serializes its Store with the installation lock. A keyscan obtains
the offered key, not permission to trust it. Changed keys require an independent
fingerprint OR explicit, IP-scoped operator confirmation of their own reinstall.
The latter is a conscious trust reset, not independent identity verification.
First-ever keys retain OpenSSH-style interactive trust-on-first-use.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile


class TrustError(RuntimeError):
    """Safe user-facing SSH-trust failure; contains no passwords or tokens."""


CURRENT = 'ssh-trust.current.json'
FINGERPRINT = re.compile(r'SHA256:[A-Za-z0-9+/]{43}')


def _require(condition, message):
    if not condition:
        raise TrustError(message)


def _fingerprint(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error):
        return None
    # Validate the actual SSH ED25519 wire key, not merely an arbitrary blob.
    prefix = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32)
    if len(raw) != len(prefix) + 32 or not raw.startswith(prefix):
        return None
    if base64.b64encode(raw).decode() != encoded:
        return None
    return 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')


def _private_file(path):
    _require(not path.is_symlink(), 'Небезопасный файл доверия SSH: символическая ссылка.')
    info = path.stat()
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
             'Небезопасные права файла доверия SSH; нужны 0600 и ваш владелец.')


def _directory(store):
    path = Path(store.path)
    _require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)),
             'Небезопасный каталог доверия SSH.')
    info = path.stat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o700, 'Каталог доверия SSH должен иметь права 0700.')
    return path


def _scan(ip, port, host):
    try:
        result = subprocess.run(['/usr/bin/ssh-keyscan', '-T', '8', '-p', str(port), '-t', 'ed25519', ip],
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise TrustError('Не удалось получить SSH-ключ для сверки. Изменения не выполнялись.') from None
    _require(result.returncode == 0 and 0 < len(result.stdout) <= 16000,
             'Не удалось получить SSH ED25519-ключ; проверьте IP и SSH-порт.')
    try:
        lines = result.stdout.decode('ascii').splitlines()
    except UnicodeDecodeError:
        raise TrustError('Некорректный ответ проверки SSH-ключа.') from None
    keys = {}
    for line in lines:
        fields = line.split()
        if len(fields) == 3 and fields[0] == host and fields[1] == 'ssh-ed25519':
            fingerprint = _fingerprint(fields[2])
            if fingerprint:
                keys[fields[2]] = fingerprint
    _require(len(keys) == 1, 'SSH вернул отсутствующий или неоднозначный ED25519-ключ. Вход остановлен.')
    return next(iter(keys.items()))


def _global_keys(host):
    """Reuse only safe existing user trust, including hashed known_hosts entries."""
    path = Path.home() / '.ssh' / 'known_hosts'
    if not path.exists() or any(p.is_symlink() for p in (path, path.parent)):
        return set(), set()
    info = path.stat()
    directory = path.parent.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022
            or info.st_size > 4_000_000 or directory.st_uid != os.geteuid() or directory.st_mode & 0o022):
        return set(), set()
    try:
        result = subprocess.run(['/usr/bin/ssh-keygen', '-F', host, '-f', str(path)],
                                capture_output=True, timeout=5, check=False)
        if result.returncode not in (0, 1) or len(result.stdout) > 64000:
            return set(), set()
        lines = result.stdout.decode('ascii').splitlines()
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return set(), set()
    trusted, revoked = set(), set()
    for line in lines:
        fields = line.split()
        if not fields or fields[0].startswith('#'):
            continue
        marker = fields.pop(0) if fields[0].startswith('@') else None
        if len(fields) >= 3 and fields[1] == 'ssh-ed25519':
            fingerprint = _fingerprint(fields[2])
            if fingerprint and marker in (None, '@revoked'):
                (revoked if marker == '@revoked' else trusted).add(fingerprint)
        elif len(fields) >= 3 and marker is None and fields[1].startswith(('ssh-', 'ecdsa-')):
            # A hostname already trusted with RSA/ECDSA is not new TOFU.
            trusted.add('existing-non-ed25519-host-key')
    return trusted, revoked


def _known_file(directory, record):
    fingerprint = record['fingerprint']
    name = 'known-host-' + hashlib.sha256(fingerprint.encode()).hexdigest()
    path = directory / name
    data = (record['host'] + ' ssh-ed25519 ' + record['key'] + '\n').encode()
    if path.exists() or path.is_symlink():
        _private_file(path)
        _require(path.read_bytes() == data, 'Сохранённый SSH-ключ изменён. Автоматической перезаписи нет.')
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    return path


def _write_current(store, value):
    """An atomic mutable pointer; immutable per-key receipts keep old trust."""
    directory = _directory(store)
    target = directory / CURRENT
    if target.exists() or target.is_symlink():
        _private_file(target)
    data = (json.dumps(value, sort_keys=True) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.ssh-trust-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolve(ip, port, store, expected=None, interactive=True):
    """Return a strict private known_hosts path after authorizing the offered key.

    expected is the ED25519 SHA256 fingerprint independently obtained from the
    hosting console. An already saved, unchanged key needs neither it nor input.
    """
    try:
        address = ipaddress.IPv4Address(ip)
    except (ValueError, TypeError):
        raise TrustError('Для доверия SSH нужен публичный IPv4 VPS.') from None
    _require(str(address) == ip and address.is_global and type(port) is int and 1 <= port <= 65535,
             'Некорректный IPv4 или порт SSH.')
    if expected is not None:
        _require(isinstance(expected, str) and FINGERPRINT.fullmatch(expected),
                 'Неверный SHA256-отпечаток SSH. Скопируйте полный отпечаток из консоли VPS.')
    directory = _directory(store)
    host = ip if port == 22 else f'[{ip}]:{port}'
    current = store.load(CURRENT)
    if current is not None:
        _require(isinstance(current, dict) and current.get('format') == 1
                 and current.get('host') == host and current.get('ip') == ip and current.get('port') == port
                 and isinstance(current.get('fingerprint'), str) and FINGERPRINT.fullmatch(current['fingerprint'])
                 and _fingerprint(current.get('key', '')) == current['fingerprint'],
                 'Сохранённое доверие SSH не соответствует этой VPS.')
        # A missing or altered previous pin is a state error, not new TOFU.
        previous_path = directory / ('known-host-' + hashlib.sha256(current['fingerprint'].encode()).hexdigest())
        _require(previous_path.exists() or previous_path.is_symlink(), 'Сохранённый файл SSH-ключа исчез. Проверьте локальное состояние.')
        _private_file(previous_path)
        _require(previous_path.read_bytes() == (host + ' ssh-ed25519 ' + current['key'] + '\n').encode(),
                 'Сохранённый SSH-ключ изменён. Вход остановлен.')
    key, fingerprint = _scan(ip, port, host)
    acceptance = None
    global_trusted, global_revoked = _global_keys(host)
    _require(fingerprint not in global_revoked, 'Этот SSH-ключ помечен @revoked в known_hosts. Вход остановлен.')
    if expected is not None:
        _require(hmac.compare_digest(expected, fingerprint), 'SSH-ключ не совпал с проверенным отпечатком. Вход и изменения остановлены.')
    elif current and hmac.compare_digest(current['fingerprint'], fingerprint):
        return previous_path
    elif current is None and fingerprint in global_trusted:
        # Reusing globally established trust is not accepting keyscan blindly.
        pass
    else:
        changed = current is not None or bool(global_trusted)
        _require(interactive and sys.stdin.isatty(),
                 'SSH-ключ изменился или ещё не закреплён. Повторите с --host-key-sha256, проверенным в консоли VPS.')
        print('SSH ' + host + ' — ED25519 ' + fingerprint, flush=True)
        if changed:
            print('Ключ SSH изменился. После переустановки это ожидаемо, но возможно и чужое подключение.\n'
                  'Для независимой сверки в консоли VPS можно выполнить:\n'
                  'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256\n'
                  'Либо, только если ВЫ САМИ переустановили эту VPS и проверили её IP у хостинга,\n'
                  'можно явно принять новый ключ без VNC. Это не исключает риск подмены сервера.', flush=True)
            confirmation = 'ПЕРЕУСТАНОВЛЕН ' + ip
            print('Для такого принятия введите точно: ' + confirmation, flush=True)
            verified = input('Отпечаток из консоли / подтверждение переустановки / Enter — отмена: ').strip()
            if verified == confirmation:
                acceptance = 'operator-confirmed-reinstall'
                print('Новый ключ принят по вашему подтверждению переустановки, без независимой сверки.', flush=True)
            else:
                _require(FINGERPRINT.fullmatch(verified) and hmac.compare_digest(verified, fingerprint),
                         'Подтверждение не совпало. Старое доверие сохранено, вход остановлен.')
        else:
            print('Первое подключение: этот ключ пока не подтверждён. Лучше сверить его в консоли хостинга.\n'
                  'Если вы уверены в адресе и принимаете первое доверие этому ключу, подтвердите явно.', flush=True)
            _require(input('Для первого подключения введите yes: ').strip() == 'yes',
                     'Первое доверие SSH не подтверждено. Вход остановлен.')
    record = {'format': 1, 'host': host, 'ip': ip, 'port': port, 'key': key, 'fingerprint': fingerprint}
    path = _known_file(directory, record)
    previous_receipt = store.load(path.name + '.json')
    if previous_receipt is not None:
        # A later explicit A -> B -> A rotation must not rewrite the original
        # immutable per-key record, nor mistake a new acceptance basis for a
        # different public key. Older receipts may include acceptance metadata.
        _require(all(previous_receipt.get(field) == value for field, value in record.items()),
                 'Сохранённая квитанция SSH-ключа изменилась. Вход остановлен.')
    else:
        store.save(path.name + '.json', record)
    if acceptance:
        prior = {current['fingerprint']} if current else global_trusted
        marker = {'format': 1, 'host': host, 'ip': ip, 'port': port,
                  'previousFingerprints': sorted(prior), 'fingerprint': fingerprint, 'acceptance': acceptance}
        identifier = hashlib.sha256(json.dumps(marker, sort_keys=True).encode()).hexdigest()
        store.save('ssh-reinstall-' + identifier + '.json', marker)
        record['acceptance'] = acceptance
    _write_current(store, record)
    return path
