"""Bounded authenticated REALITY tests, with no TUN, DIRECT fallback or new users."""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('reality_probe_e2e', HERE.parent / 'host-health-audit/e2e.py')
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)
VERSION = '26.9.9'
ENGINE_SHA256 = '7616e4d8d5b8bedaee14ebe167954e24f35ccea58cdb941600e2ff76509fc31c'
EXISTING_ENGINE = Path('/private/tmp/spn-runtime-probe.wnRZpc/xray')
ENGINE_URL = 'https://github.com/XTLS/Xray-core/releases/download/v26.9.9/Xray-macos-arm64-v8a.zip'
# Fixed official v26.9.9 assets. Archive digests were checked against the
# release API; binary digests independently cover the executable we launch.
# Keep the original Darwin names/hash so existing Mac proof receipts stay valid.
LINUX_ENGINES = {
    'amd64': ('Xray-linux-64.zip',
              'c4ae6798c38e0e5343b192406746333cd0ba7ff3eb984f8c4b9939dcb68c3f8a',
              '1eb9175d0f0a8f8149c9230a7fc5ae66ce332ed20a53155ce61fe62e3f58b7df'),
    'arm64': ('Xray-linux-arm64-v8a.zip',
              'c1defe42b6db958a97c5e049a02a00a4baaedaca7b51c1c229f0830e288acef5',
              '3e38d72dfc5eb65c91df0e5583e9b6676c32232041da47de6ae73946b526d66c'),
}
SNI = 'www.cloudflare.com'
ALLOWED_SNI = (SNI, 'login.vk.com')
BODY_BYTES = 1048576


class ProbeError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProbeError(message)


def private_file(path):
    path = Path(path).expanduser().absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'Файл тестовой подписки не должен быть симлинком.')
    try:
        info = path.stat()
    except OSError:
        raise ProbeError('Нет файла тестовой подписки. Сохраните личную ссылку в файл с правами 0600.') from None
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1 and 0 < info.st_size < 4096, 'Файл тестовой подписки должен принадлежать вам и иметь права 0600.')
    return path


def subscription_url(path):
    value = private_file(path).read_text().strip()
    parsed = urlsplit(value)
    require(parsed.scheme == 'https' and parsed.hostname == 'sub.wayspn.online' and parsed.port in (None, 443)
            and parsed.path not in ('', '/') and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and not any(c in value for c in '\r\n"\\'),
            'Нужна HTTPS-ссылка подписки sub.wayspn.online без дополнительных параметров; сама ссылка не выводится.')
    return value


def credential(documents):
    require(isinstance(documents, (dict, list)), 'Подписка вернула неподдерживаемый формат.')
    docs = [documents] if isinstance(documents, dict) else documents
    require(len(docs) <= 1000, 'Слишком много конфигураций в тестовой подписке.')
    users = set()
    for document in docs:
        require(isinstance(document, dict) and isinstance(document.get('outbounds'), list), 'В подписке нет списка подключений.')
        for out in document['outbounds']:
            endpoint, reason = e.endpoint(out)
            if reason is not None or endpoint['protocol'] != 'vless':
                continue
            try:
                user = out['settings']['vnext'][0]['users'][0]['id']
                users.add(str(uuid.UUID(user)))
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                raise ProbeError('Не удалось однозначно прочитать VLESS-данные тестовой подписки.') from None
    require(len(users) == 1, 'В тестовой подписке должен быть один однозначный VLESS-пользователь.')
    return users.pop()


def validate_interface(interface, *, physical=True):
    require(isinstance(interface, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,15}', interface)
            and (not physical or not interface.lower().startswith(('utun', 'tun', 'tap', 'lo'))),
            'Для проверки нужен физический интерфейс Mac, например en0, а не VPN/TUN.')


def public_endpoint(ip, public_port=443):
    """Validate a concrete public transport endpoint before any socket/fetch."""
    try:
        address = ipaddress.IPv4Address(ip)
    except (TypeError, ValueError):
        raise ProbeError('Для проверки нужен публичный IPv4 подключения.') from None
    require(str(address) == ip and address.is_global and not address.is_multicast,
            'Для проверки нужен публичный IPv4 подключения.')
    require(type(public_port) is int and 1 <= public_port <= 65535, 'Недопустимый публичный TCP-порт проверки.')
    return str(address), public_port


def validate_sni(sni, security='reality'):
    require(security in ('reality', 'tls'), 'Неподдерживаемый транспорт проверки.')
    if security == 'tls':
        require(isinstance(sni, str) and sni == export_endpoint_address(sni)
                and not re.fullmatch(r'[0-9.]+', sni), 'Для TLS нужен DNS-домен сертификата.')
    else:
        require(sni in ALLOWED_SNI, 'Неизвестный SNI проверяемого шаблона.')


def _physical_tcp(ip, index, timeout, *, public_port=443):
    """Darwin AF_INET socket bound to the physical device, never its TUN route."""
    public_endpoint(ip, public_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        try:
            connection.setsockopt(socket.IPPROTO_IP, 25, index)  # Darwin IP_BOUND_IF
            connection.settimeout(timeout)
        except OSError:
            raise ProbeError('Не удалось привязать прямую IPv4-проверку к физическому интерфейсу Mac.') from None
        started = time.monotonic()
        try:
            connection.connect((ip, public_port))
        except (socket.timeout, TimeoutError):
            return {'passed': False, 'reason': 'timeout'}
        except OSError as error:
            return {'passed': False, 'reason': 'refused' if error.errno == errno.ECONNREFUSED else 'network_error'}
        return {'passed': True, 'connectMs': round((time.monotonic()-started)*1000)}


def physical_tcp_preflight(ip, interface='en0', timeout=3, *, public_port=443):
    """Fail fast before TLS/authentication; do not infer who blocks the path."""
    validate_interface(interface)
    public_endpoint(ip, public_port)
    require(platform.system() == 'Darwin', 'Прямая IPv4-проверка рассчитана на Mac.')
    try:
        address = ipaddress.IPv4Address(ip)
        require(address.is_global and not address.is_multicast, 'Нужен публичный IPv4 новой VPS.')
        index = socket.if_nametoindex(interface)
    except (OSError, ValueError):
        raise ProbeError('Не удалось определить IPv4/физический интерфейс для прямой проверки.') from None
    target = _physical_tcp(ip, index, timeout, public_port=public_port)
    if target['passed']:
        return {**target, 'ip': ip, 'port': public_port, 'physicalInterface': interface}
    control = _physical_tcp('1.1.1.1', index, timeout)
    detail = {'timeout': 'таймаут', 'refused': 'соединение отклонено', 'network_error': 'сетевая ошибка'}[target['reason']]
    control_detail = 'прошёл' if control['passed'] else 'тоже не прошёл'
    raise ProbeError(f'{ip}:{public_port} недоступен по прямому IPv4 до начала TLS/VPN ({detail}); '
                     f'контроль 1.1.1.1:443 {control_detail}. Работа VPN не подтверждена; '
                     'эта проверка не определяет причину или сторону, ограничивающую соединение.')


def fetch_documents(path, interface):
    validate_interface(interface, physical=False)
    config = 'url = "' + subscription_url(path) + '"\nuser-agent = "Happ/4.0"\nheader = "Accept: application/json"\n'
    # URL (including its secret path) travels only over stdin, never argv/logs.
    # This is control-plane credential retrieval, not a data-plane VPN test.
    # The subscription site may require the Mac's current ordinary VPN route.
    # Only the isolated Xray transport below must bind to the physical NIC.
    args = ['/usr/bin/curl', '-q', '--config', '-', '-4sS', '--noproxy', '*',
            '--proto', '=https', '--connect-timeout', '5', '--max-time', '15', '--max-filesize', '4000000', '--fail']
    for attempt in range(3):
        try:
            result = subprocess.run(args, input=config.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=18)
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0 and len(result.stdout) <= 4000000:
            try:
                documents = json.loads(result.stdout)
                credential(documents)
                return documents
            except (ValueError, UnicodeError):
                raise ProbeError('Тестовая подписка вернула некорректный JSON; содержимое скрыто.') from None
        if attempt < 2:
            time.sleep(.3)
    raise ProbeError('Не удалось загрузить тестовую подписку через текущую сеть; ссылка и ответ скрыты.')


def fetch_credential(path, interface):
    return credential(fetch_documents(path, interface))


def export_endpoint_address(value):
    """Validate only the published label; it is never used for transport/DNS."""
    require(isinstance(value, str) and bool(value) and value == value.strip(),
            'Некорректный адрес хоста в проверке подписки.')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            name = value.rstrip('.').encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise ProbeError('Некорректный домен хоста в проверке подписки.') from None
        labels = name.split('.')
        require(len(labels) >= 2 and len(name) <= 253
                and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
                and e.public_address(name), 'Некорректный домен хоста в проверке подписки.')
        return name
    require(isinstance(address, ipaddress.IPv4Address) and address.is_global and not address.is_multicast,
            'Некорректный IPv4 хоста в проверке подписки.')
    return str(address)


def verify_export(ip, public_key, short_id, subscription_file, interface='en0', sni=SNI, route='physical', *,
                  public_port=443, endpoint_address=None, security='reality'):
    """Verify the published label while retaining the concrete transport IP.

    A DNS host may be exported instead of ``ip``. No resolver or client here
    uses that label: the traffic proof/fingerprint remains pinned to ``ip``.
    """
    public_endpoint(ip, public_port)
    validate_sni(sni, security)
    expected_address = export_endpoint_address(ip if endpoint_address is None else endpoint_address)
    documents = fetch_documents(subscription_file, interface)
    expected_user = credential(documents)
    docs = [documents] if isinstance(documents, dict) else documents
    matched = []
    for document in docs:
        for out in document['outbounds']:
            endpoint, reason = e.endpoint(out)
            if reason is None and endpoint['address'] == expected_address and endpoint['port'] == public_port:
                matched.append(out)
    # Subscription exports repeat one leaf in AUTO and standalone documents,
    # assigning presentation tags such as proxy-5/proxy. Ignore only that
    # top-level label; every transport, credential and nested value stays pinned.
    unique = {json.dumps({key: value for key, value in out.items() if key != 'tag'},
                         sort_keys=True, separators=(',', ':')): out for out in matched}
    require(len(unique) == 1, 'Новый хост отсутствует в свежей подписке или найден неоднозначно.')
    out = next(iter(unique.values()))
    point, reason = e.endpoint(out)
    require(reason is None and point['protocol'] == 'vless' and point['network'] == 'raw'
            and point['security'] == security, 'Выгрузка подписки не совпала с транспортом нового хоста.')
    try:
        reality = out['streamSettings']['tlsSettings' if security == 'tls' else 'realitySettings']
        user = out['settings']['vnext'][0]['users'][0]
        expected = {'serverName': sni, 'fingerprint': 'safari'}
        if security == 'reality':
            expected.update(publicKey=public_key, shortId=short_id)
        require(all(reality.get(key) == value for key, value in expected.items())
                and reality.get('allowInsecure') is not True,
                'Выгрузка подписки изменила параметры TLS/Safari; ключи и ответ скрыты.')
        require(str(uuid.UUID(user['id'])) == expected_user and user.get('flow') == 'xtls-rprx-vision'
                and user.get('encryption') == 'none', 'Выгрузка подписки не подтвердила Vision/данные пользователя.')
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProbeError('Не удалось проверить параметры нового хоста в выгрузке подписки.') from None
    return {'passed': True, 'ip': ip, 'endpointAddress': expected_address, 'port': public_port,
            'fingerprint': 'safari', 'security': security,
            **({'tlsCertificateValidation': True} if security == 'tls' else {'realityKeysMatch': True}), 'flowMatches': True,
            'configFingerprint': connection_fingerprint(ip, public_key, short_id, interface, sni, route, public_port=public_port, security=security),
            'checkedAt': datetime.now(timezone.utc).isoformat()}


def engine_spec():
    """Resolve only explicitly pinned OS/architecture pairs, never a fallback."""
    system, machine = platform.system(), platform.machine().lower()
    if system == 'Darwin' and machine in ('arm64', 'aarch64'):
        return {'sha256': ENGINE_SHA256, 'archiveSha256': None, 'url': ENGINE_URL,
                'cacheName': 'xray-' + VERSION, 'existing': EXISTING_ENGINE}
    if system == 'Linux':
        architecture = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
        require(architecture in LINUX_ENGINES, 'Для Linux поддерживаются только amd64 и arm64.')
        asset, digest, archive_digest = LINUX_ENGINES[architecture]
        return {'sha256': digest, 'archiveSha256': archive_digest,
                'url': f'https://github.com/XTLS/Xray-core/releases/download/v{VERSION}/{asset}',
                'cacheName': f'xray-{VERSION}-linux-{architecture}', 'existing': None}
    raise ProbeError('Автопроверка поддерживает Mac Apple Silicon и Linux amd64/arm64.')


def _pinned(path, spec=None):
    spec = engine_spec() if spec is None else spec
    try:
        e.pinned_engine(Path(path), spec['sha256'])
    except Exception:
        raise ProbeError('Xray для проверки не прошёл проверку SHA256 или безопасных прав файла.') from None
    return Path(path)


def _download_engine(cache):
    """Download only an official fixed release; authenticate the binary itself."""
    selected = engine_spec()
    with tempfile.TemporaryDirectory(prefix='.download-', dir=cache) as temporary:
        directory = Path(temporary)
        archive = directory / 'xray.zip'
        try:
            result = subprocess.run(['/usr/bin/curl', '-q', '--fail', '--silent', '--show-error', '--location',
                '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '5', '--connect-timeout', '10',
                '--max-time', '120', '--max-filesize', str(80*1024*1024), '--output', str(archive), selected['url']],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=125)
            require(result.returncode == 0 and archive.stat().st_size <= 80*1024*1024,
                    'Не удалось скачать официальный Xray для теста.')
            if selected['archiveSha256'] is not None:
                require(hashlib.sha256(archive.read_bytes()).hexdigest() == selected['archiveSha256'],
                        'Архив Xray не совпал с закреплённым SHA256; распаковка запрещена.')
            with zipfile.ZipFile(archive) as zipped:
                require(all(not PurePosixPath(entry.filename).is_absolute()
                    and '..' not in PurePosixPath(entry.filename).parts and '\\' not in entry.filename
                    for entry in zipped.infolist()), 'Небезопасные пути в архиве Xray.')
                entries = [entry for entry in zipped.infolist() if entry.filename == 'xray' and not entry.is_dir()]
                require(len(entries) == 1 and 0 < entries[0].file_size <= 64*1024*1024,
                        'Неожиданный размер или структура официального архива Xray.')
                binary = zipped.read(entries[0])
                require(hashlib.sha256(binary).hexdigest() == selected['sha256'],
                        'Скачанный Xray не совпал с закреплённым SHA256; запуск запрещён.')
                target = directory / 'xray'
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
                with os.fdopen(fd, 'wb') as output:
                    output.write(binary)
                    output.flush()
                    os.fsync(output.fileno())
            _pinned(target, selected)
            destination = cache / selected['cacheName']
            os.replace(target, destination)
            return _pinned(destination, selected)
        except ProbeError:
            raise
        except Exception:
            raise ProbeError('Загрузка Xray остановлена; непроверенный файл не запускался.') from None


def select_engine(engine=None, home=None):
    """Reuse a pinned binary; never guess/download/execute an unverified asset."""
    selected = engine_spec()
    if engine is not None:
        return _pinned(Path(engine).expanduser().absolute(), selected)
    home = Path.home() if home is None else Path(home)
    cache = home / '.spn-reality-deploy/runtime'
    target = cache / selected['cacheName']
    if target.exists() or target.is_symlink():
        return _pinned(target, selected)
    require(not any(p.is_symlink() for p in (cache, *cache.parents)), 'Небезопасная папка кэша Xray.')
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = cache.stat()
    require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'Кэш Xray должен иметь права 0700.')
    existing = selected['existing']
    if existing is None or not existing.exists():
        return _download_engine(cache)
    source = _pinned(existing, selected)
    fd, temporary = tempfile.mkstemp(prefix='.xray-', dir=cache)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as output, source.open('rb') as original:
            for chunk in iter(lambda: original.read(65536), b''):
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        require(digest.hexdigest() == selected['sha256'], 'Xray изменился при копировании; проверка остановлена.')
        os.chmod(temporary, 0o700)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _pinned(target, selected)


def validate_route(route):
    require(route in ('physical', 'system', 'ssh'), 'Маршрут проверки должен быть physical, system или ssh.')


def connection_fingerprint(ip, public_key, short_id, interface, sni=SNI, route='physical', *, public_port=443, expected_exit_ip=None, security='reality'):
    validate_route(route)
    public_endpoint(ip, public_port)
    validate_sni(sni, security)
    expected_exit_ip = ip if expected_exit_ip is None else public_endpoint(expected_exit_ip)[0]
    payload = {'ip': ip, 'port': public_port, 'network': 'raw', 'security': security, 'sni': sni,
               'publicKey': public_key, 'shortId': short_id, 'flow': 'xtls-rprx-vision',
               'fingerprint': 'safari', 'interface': interface if route == 'physical' else None,
               'testRoute': route, 'engineSha256': engine_spec()['sha256']}
    # Keep the existing direct proof hash stable. A distinct backend is part
    # of the identity of a relay test, never inferred from its public address.
    if expected_exit_ip != ip:
        payload['expectedExitIp'] = expected_exit_ip
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def client_config(ip, public_key, short_id, user_id, port, interface, sni=SNI, route='physical', relay_port=None, *, public_port=443, security='reality'):
    validate_route(route)
    validate_interface(interface, physical=route == 'physical')
    require((route == 'ssh' and type(relay_port) is int and 1024 <= relay_port <= 65535)
            or (route != 'ssh' and relay_port is None), 'SSH-проверка требует доверенный локальный порт переадресации.')
    public_endpoint(ip, public_port)
    validate_sni(sni, security)
    if security == 'reality':
        require(re.fullmatch(r'[A-Za-z0-9_-]{43}', public_key) and re.fullmatch(r'[0-9a-f]{16}', short_id),
                'Некорректные открытые параметры REALITY.')
    user_id = str(uuid.UUID(user_id))
    outbound = {'tag': 'reality-probe', 'protocol': 'vless', 'settings': {'vnext': [{
        'address': ip, 'port': public_port, 'users': [{'id': user_id, 'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
        'streamSettings': {'network': 'raw', 'security': 'reality', 'realitySettings': {
            'serverName': sni, 'fingerprint': 'safari', 'publicKey': public_key, 'shortId': short_id, 'spiderX': '/'}}}
    if security == 'tls':
        outbound['streamSettings'] = {'network': 'raw', 'security': 'tls', 'tlsSettings': {
            'serverName': sni, 'fingerprint': 'safari', 'allowInsecure': False, 'alpn': ['h2']}}
    config = e.single_config(outbound, port)
    # The generic validator correctly refuses arbitrary pre-bound leaves.
    # Add our deliberate physical-interface binding only after validation.
    if route == 'physical':
        config['outbounds'][0]['streamSettings']['sockopt'] = {'interface': interface}
    elif route == 'ssh':
        # The installer owns this localhost forward: SSH transports only the
        # outer TCP stream to the server's same REALITY listener. Authenticate
        # VLESS/REALITY inside it; do not substitute a SOCKS/DIRECT outbound.
        # Validate the public target FIRST, above, before this deliberate bind.
        server = config['outbounds'][0]['settings']['vnext'][0]
        server['address'], server['port'] = '127.0.0.1', relay_port
    config['log'] = {'loglevel': 'error', 'access': 'none'}
    return config


def physical_observed(pid, source, ip, *, public_port=443):
    public_endpoint(ip, public_port)
    try:
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-iTCP', '-Fn'], capture_output=True, timeout=3)
    except subprocess.TimeoutExpired:
        return False
    exact = re.compile(rb'^n' + re.escape(source.encode()) + rb':[0-9]+->' + re.escape(ip.encode())
                       + rb':' + str(public_port).encode() + rb'(?: \(ESTABLISHED\))?$')
    return any(exact.fullmatch(line) for line in result.stdout.splitlines())


def request(port, child, ip, number, mode='204'):
    args = ['/usr/bin/curl', '-q', '-4sS', '--retry', '0', '--connect-timeout', '4', '--max-time', '8',
            '--proto', '=https', '--noproxy', '', '--socks5-hostname', '127.0.0.1:' + str(port)]
    if mode == 'trace':
        args += ['--max-filesize', '4096', 'https://1.1.1.1/cdn-cgi/trace']
    else:
        url = 'https://www.gstatic.com/generate_204' if mode == '204' else 'https://speed.cloudflare.com/__down?bytes=' + str(BODY_BYTES)
        args += ['--max-filesize', str(BODY_BYTES + 4096), '--output', '/dev/null', '--write-out', '%{http_code} %{time_total} %{size_download}']
        if mode == 'body':
            args += ['--max-time', '20']
        args += [url]
    row = {'test': number, 'mode': mode, 'passed': False}
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=22)
        row['curlExit'] = result.returncode
        if mode == 'trace':
            exits = [line[3:] for line in result.stdout.decode(errors='replace').splitlines() if line.startswith('ip=')]
            row['exitMatches'] = exits == [ip]
            row['passed'] = result.returncode == 0 and row['exitMatches']
        else:
            parts = result.stdout.decode(errors='replace').split()
            if len(parts) == 3 and len(result.stdout) < 256:
                row.update(http=parts[0], ms=round(float(parts[1])*1000), bytes=int(parts[2]))
                row['passed'] = result.returncode == 0 and parts[0] == ('204' if mode == '204' else '200') \
                    and int(parts[2]) == (0 if mode == '204' else BODY_BYTES)
    except subprocess.TimeoutExpired:
        row.update(curlExit=-1, error='local_process_timeout')
    except (ValueError, UnicodeError):
        row['error'] = 'unexpected_probe_output'
    row['clientAlive'] = child.poll() is None
    row['passed'] = row['passed'] and row['clientAlive']
    return row


def summary(ip, fingerprint, interface, physical, rows, route='physical', *, public_port=443, expected_exit_ip=None):
    validate_route(route)
    public_endpoint(ip, public_port)
    expected_exit_ip = ip if expected_exit_ip is None else public_endpoint(expected_exit_ip)[0]
    passed = len(rows) == 9 and all(row['passed'] and row['clientAlive'] for row in rows)
    if route == 'physical':
        passed = passed and physical
    return {'passed': passed, 'testRoute': route, 'directReachabilityVerified': passed and route == 'physical',
            'ip': ip, 'port': public_port, 'expectedExitIp': expected_exit_ip,
            'relayPathVerified': passed and route in ('physical', 'system') and expected_exit_ip != ip,
            'tests': len(rows), 'results': rows, 'physicalInterface': interface if route == 'physical' else None,
            'physicalSourceObserved': physical and route == 'physical', 'fingerprint': 'safari', 'configFingerprint': fingerprint,
            'engineVersion': VERSION, 'checkedAt': datetime.now(timezone.utc).isoformat()}


def run(ip, public_key, short_id, subscription_file, engine=None, interface='en0', sni=SNI, route='physical', relay_port=None, *, public_port=443, expected_exit_ip=None, security='reality'):
    validate_route(route)
    validate_interface(interface, physical=route == 'physical')
    public_endpoint(ip, public_port)
    validate_sni(sni, security)
    expected_exit_ip = ip if expected_exit_ip is None else public_endpoint(expected_exit_ip)[0]
    require(route != 'ssh' or expected_exit_ip == ip,
            'SSH-проверка бэкенда не проверяет публичное реле. Для реле выберите physical или system.')
    tcp_proof = physical_tcp_preflight(ip, interface, public_port=public_port) if route == 'physical' else None
    selected = select_engine(engine)
    user_id = fetch_credential(subscription_file, interface)
    source = None
    if route == 'physical':
        try:
            source = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', interface], text=True, timeout=3).strip()
            address = ipaddress.IPv4Address(source)
            require(not address.is_loopback and not address.is_unspecified, 'На физическом интерфейсе нет IPv4.')
        except (subprocess.SubprocessError, ValueError):
            raise ProbeError('Не удалось определить IPv4 физического интерфейса Mac.') from None
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    config = client_config(ip, public_key, short_id, user_id, port, interface, sni, route, relay_port, public_port=public_port, security=security)
    child = None
    temporary_root = '/private/tmp' if platform.system() == 'Darwin' else '/tmp'
    with tempfile.TemporaryDirectory(prefix='spn-reality-vpn-test-', dir=temporary_root) as temporary:
        directory = Path(temporary)
        e.write_private(directory / 'config.json', config)
        try:
            with e.private_output(directory / 'engine.log') as log:
                checked = subprocess.run([str(selected), 'run', '-test', '-c', str(directory / 'config.json')],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=e.ENV, timeout=12)
                require(checked.returncode == 0, 'Xray отклонил тестовую конфигурацию; секретные подробности скрыты.')
                child = subprocess.Popen([str(selected), 'run', '-c', str(directory / 'config.json')],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=e.ENV, start_new_session=True)
                ready = False
                for _ in range(100):
                    if child.poll() is not None:
                        break
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            ready = True
                        break
                    except OSError:
                        time.sleep(.05)
                require(ready and child.poll() is None, 'Локальный клиент проверки не запустился.')
                physical, rows = False, []
                for number in range(7):
                    mode = 'trace' if number == 5 else 'body' if number == 6 else '204'
                    if route == 'physical' and number in (0, 6):
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            pending = pool.submit(request, port, child, expected_exit_ip, number, mode)
                            time.sleep(.12)
                            physical = physical_observed(child.pid, source, ip, public_port=public_port) or physical
                            rows.append(pending.result())
                    else:
                        rows.append(request(port, child, expected_exit_ip, number, mode))
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    rows.extend(pool.map(lambda number: request(port, child, expected_exit_ip, number), (7, 8)))
                result = summary(ip, connection_fingerprint(ip, public_key, short_id, interface, sni, route,
                                 public_port=public_port, expected_exit_ip=expected_exit_ip, security=security),
                                 interface, physical, rows, route, public_port=public_port, expected_exit_ip=expected_exit_ip)
                result['physicalTcp'] = tcp_proof
                return result
        except ProbeError:
            raise
        except Exception:
            raise ProbeError('VPN-проверка остановилась; ключи, ссылка и журнал клиента скрыты.') from None
        finally:
            e.stop(child)
