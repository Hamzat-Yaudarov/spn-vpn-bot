"""One-way SSH handoff to a single authoritative controller, never journal mirroring."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import io
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import uuid

from .store import DEFAULT_ROOT, Store, private_directory, private_file
from .controller_transport import ssh_transport_options


DEFAULT_CONFIG = '~/.config/spn/reserve-controller.json'
INTENT = 'fleet-delegation.intent.json'
DELEGATED = 'fleet-delegation.json'
RECEIVED = 'fleet-received.json'
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_FILES = 2000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _path(value):
    return (isinstance(value, str) and value.startswith('/') and value != '/'
            and re.fullmatch(r'/[A-Za-z0-9_./-]+', value) is not None
            and '..' not in PurePosixPath(value).parts and str(PurePosixPath(value)) == value)


def validate_controller(value):
    require(isinstance(value, dict), 'Нужен JSON-объект настроек контроллера')
    allowed = {'host', 'port', 'user', 'service_user', 'repo_dir', 'python', 'data_dir', 'journal_root',
               'identity_file', 'node_public_key'}
    require(set(value) <= allowed and 'host' in value, 'Неизвестные настройки контроллера')
    config = {'port': 22, 'user': 'root', 'service_user': 'spn-reserve',
              'repo_dir': '/opt/spn-reserve-fleet/current',
              'python': '/opt/spn-reserve-fleet/venv/bin/python',
              'data_dir': '/var/lib/spn-reserve-fleet/.spn-reserve-fleet',
              'journal_root': '/var/lib/spn-reserve-fleet/.spn-reality-deploy', **value}
    host = config['host']
    require(isinstance(host, str) and 1 <= len(host) <= 253
            and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host)
            and '..' not in host, 'Некорректный адрес SSH-контроллера')
    require(type(config['port']) is int and 1 <= config['port'] <= 65535, 'Неверный SSH-порт контроллера')
    for key in ('user', 'service_user'):
        require(isinstance(config[key], str) and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', config[key]),
                'Некорректный пользователь контроллера')
    require(config['user'] in ('root', config['service_user']),
            'SSH-пользователь должен быть root или пользователем службы')
    for key in ('repo_dir', 'python', 'data_dir', 'journal_root'):
        require(_path(config[key]), 'Нужны абсолютные пути контроллера без .. и пробелов')
    if config.get('identity_file') is not None:
        require(_path(config['identity_file']), 'identity_file должен быть абсолютным путём')
    if config.get('node_public_key') is not None:
        config['node_public_key'] = canonical_node_key(config['node_public_key'])
    return config


def canonical_node_key(value):
    """An ED25519 public key, never authorized_keys options or a private key."""
    import base64
    import struct
    import re
    if (not isinstance(value, str) or len(value) > 512 or not value.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Неверный публичный ED25519-ключ контроллера')
    parts = value.split(' ', 2)
    if (len(parts) < 2 or parts[0] != 'ssh-ed25519' or not parts[1]
            or (len(parts) == 3 and not re.fullmatch(r'[A-Za-z0-9@._ -]{1,120}', parts[2]))):
        raise ValueError('Нужен ED25519 public key без SSH-опций и переносов строк')
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError):
        raise ValueError('Неверный публичный ED25519-ключ контроллера') from None
    prefix = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32)
    if len(raw) != len(prefix) + 32 or not raw.startswith(prefix) or base64.b64encode(raw).decode() != parts[1]:
        raise ValueError('Неверный публичный ED25519-ключ контроллера')
    return ' '.join(parts)


def append_node_key(home, key, owner=0):
    """Run only on the already trusted VPN node; retain every existing key."""
    import fcntl
    import os
    from pathlib import Path
    import shlex
    import stat
    key = canonical_node_key(key)
    home = Path(home)
    if os.geteuid() != owner or not home.is_absolute() or any(path.is_symlink() for path in (home, *home.parents)):
        raise ValueError('Небезопасный каталог SSH')
    home_info = home.stat()
    if not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != owner or home_info.st_mode & 0o022:
        raise ValueError('Небезопасные права домашнего каталога SSH')
    directory = home / '.ssh'
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError('Небезопасные права каталога SSH')
    path = directory / 'authorized_keys'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > 2 * 1024 * 1024):
            raise ValueError('Небезопасный authorized_keys')
        existing = os.read(fd, 2 * 1024 * 1024 + 1)
        encoded = key.split()[1]
        for raw in existing.splitlines():
            try:
                parts = shlex.split(raw.decode('utf-8'), comments=True)
            except (ValueError, UnicodeDecodeError):
                continue
            if 'ssh-ed25519' in parts:
                index = parts.index('ssh-ed25519')
                if len(parts) > index + 1 and parts[index + 1] == encoded:
                    if index != 0:
                        raise ValueError('Этот ключ уже есть с ограничениями; существующие опции не изменены')
                    return {'ok': True, 'installed': False}
        suffix = (b'\n' if existing and not existing.endswith(b'\n') else b'') + key.encode() + b'\n'
        if os.write(fd, suffix) != len(suffix):
            raise ValueError('Ключ SSH не записан полностью')
        os.fsync(fd)
    finally:
        os.close(fd)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {'ok': True, 'installed': True}


def install_controller_key(ssh, controller):
    config = validate_controller(controller)
    key = config.get('node_public_key')
    if key is None:
        return None
    script = ('import json, sys\n' + inspect.getsource(canonical_node_key) + '\n'
              + inspect.getsource(append_node_key)
              + '\nprint(json.dumps(append_node_key("/root", json.loads(sys.argv[1]))))\n').encode()
    result = ssh.run(shlex.join(['python3', '-', json.dumps(key)]), data=script, capture=True)
    try:
        proof = json.loads(result)
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValueError('Добавление публичного ключа контроллера не подтверждено') from None
    require(isinstance(proof, dict) and proof.get('ok') is True and type(proof.get('installed')) is bool,
            'Добавление публичного ключа контроллера не подтверждено')
    return proof


def load_controller(path=None):
    explicit = path is not None
    selected = Path(path or DEFAULT_CONFIG).expanduser().absolute()
    require(not any(item.is_symlink() for item in (selected, *selected.parents)),
            'Настройки контроллера не должны быть символической ссылкой')
    if not selected.exists():
        require(not explicit, 'Файл настроек SSH-контроллера не найден')
        return None
    info = selected.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and info.st_size <= 16384,
            'Файл настроек контроллера должен принадлежать вам и иметь права 0600')
    try:
        value = json.loads(selected.read_text())
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Некорректный JSON настроек контроллера') from None
    return validate_controller(value)


def delegation(store):
    return store.load(DELEGATED) or store.load(INTENT)


def require_local_control(store):
    marker = delegation(store)
    if marker:
        require(isinstance(marker, dict) and isinstance(marker.get('controller'), dict),
                'Повреждён маркер передачи управления; локальные изменения запрещены')
        config = validate_controller(marker['controller'])
        raise ValueError('Установкой управляет VPS ' + config['host']
                         + '. Меняйте IP через её панель или CLI spn_reserve_fleet на контроллере. '
                         'Локальная копия журнала больше не управляет сервером. '
                         'Незавершённую передачу продолжите командой handoff с прежним --state-dir.')


def _allowed(name):
    parts = PurePosixPath(name).parts
    if len(parts) == 3:
        if parts[0] != 'replacements' or re.fullmatch(r'[0-9]{6}', parts[1]) is None:
            return False
    elif len(parts) != 1:
        return False
    base = parts[-1]
    return (re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}', base) is not None
            and (base.endswith('.json') or base.startswith('known-host-'))
            and base not in (INTENT, DELEGATED, RECEIVED, '__handoff__.json')
            and str(PurePosixPath(name)) == name)


def make_archive(store, request_id):
    """Called under the original journal lock; includes no SSH private keys/env."""
    files = {}
    for path in sorted(store.path.rglob('*')):
        relative = path.relative_to(store.path).as_posix()
        require(not path.is_symlink(), 'Журнал содержит символическую ссылку')
        if path.is_dir():
            require(relative == 'replacements' or re.fullmatch(r'replacements/[0-9]{6}', relative),
                    'Неизвестный каталог в журнале')
            info = path.stat()
            require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                    'Каталоги журнала должны иметь права 0700')
            continue
        if path.name == '.lock' or (len(PurePosixPath(relative).parts) == 1 and path.name in (INTENT, DELEGATED)):
            continue
        require(_allowed(relative), 'Неизвестный файл в журнале; передача остановлена')
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= MAX_FILE,
                'Для файлов журнала нужны 0600, ваш владелец и допустимый размер')
        files[relative] = path.read_bytes()
    require(0 < len(files) <= MAX_FILES and sum(map(len, files.values())) <= MAX_ARCHIVE // 2,
            'Журнал слишком большой для передачи')
    plan = store.load('plan.json')
    manifest = {'format': 1, 'request_id': request_id, 'installation_id': plan['id'],
                'plan_hash': _hash(plan), 'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in [('__handoff__.json', _json(manifest)), *files.items()]:
            info = tarfile.TarInfo(name)
            info.mode, info.size, info.mtime = 0o600, len(data), 0
            archive.addfile(info, io.BytesIO(data))
    require(buffer.tell() <= MAX_ARCHIVE, 'Архив журнала слишком большой')
    return buffer.getvalue(), manifest


def receiver_command(config, request_id):
    config = validate_controller(config)
    args = [config['python'], '-m', 'spn_reserve_fleet', '--data-dir', config['data_dir'],
            'receive', '--journal-root', config['journal_root'], '--request-id', request_id]
    if config['user'] == 'root' and config['service_user'] != 'root':
        args = ['runuser', '-u', config['service_user'], '--', *args]
    return 'cd ' + shlex.quote(config['repo_dir']) + ' && ' + shlex.join(args)


def send_archive(config, request_id, archive):
    config = validate_controller(config)
    command = ['ssh', '-p', str(config['port']), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'StrictHostKeyChecking=yes']
    if config.get('identity_file'):
        path = Path(config['identity_file'])
        require(not any(item.is_symlink() for item in (path, *path.parents)),
                'Ключ SSH-контроллера не должен быть символической ссылкой')
        try:
            info = path.stat()
        except OSError:
            raise ValueError('Приватный ключ SSH-контроллера не найден') from None
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                'Ключ SSH-контроллера должен принадлежать вам и иметь права 0600')
        command += ['-i', str(path), '-o', 'IdentitiesOnly=yes']
    command += ssh_transport_options(config)
    command += ['--', config['user'] + '@' + config['host'], receiver_command(config, request_id)]
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('CLOUDFLARE_API_TOKEN', 'REMNAWAVE_API_TOKEN')}
    try:
        result = subprocess.run(command, input=archive, capture_output=True, timeout=120, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Ответ контроллера потерян. Повторите handoff с тем же журналом; локальное управление остановлено') from None
    require(result.returncode == 0 and 0 < len(result.stdout) <= 16384,
            'Передача контроллеру не подтверждена. Проверьте SSH-доступ и повторите handoff; локальное управление остановлено')
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Некорректный ответ контроллера; повторите handoff с тем же журналом') from None


def handoff_installation(state_dir, controller, *, local_data_dir=None, locked=False, sender=None):
    from .enrollment import installation_snapshot, replacement_module
    r = replacement_module()
    original = r.u.Store(Path(state_dir).expanduser().absolute())
    from contextlib import nullcontext
    config = validate_controller(controller)
    sender = sender or send_archive
    with nullcontext() if locked else original.lock():
        snapshot = installation_snapshot(original.path, locked=True, allow_delegated=True)
        require(not snapshot['pending_ip'], 'Сначала завершите незавершённую смену IP')
        plan = original.load('plan.json')
        marker = original.load(INTENT)
        if marker:
            require(marker.get('format') == 1 and marker.get('controller') == config
                    and marker.get('installation_id') == plan['id'] and marker.get('plan_hash') == _hash(plan)
                    and re.fullmatch(r'[0-9a-f]{32}', marker.get('request_id', '')),
                    'Установка уже передаётся другому контроллеру или журнал изменён')
        else:
            marker = {'format': 1, 'request_id': uuid.uuid4().hex, 'installation_id': plan['id'],
                      'domain': plan['domain'], 'plan_hash': _hash(plan), 'controller': config}
            # Fence FIRST. A lost response must never leave two controllers active.
            original.save(INTENT, marker)
        local = Store(local_data_dir or DEFAULT_ROOT)
        if local.get_server(snapshot['domain']):
            local.update(snapshot['domain'], auto_enabled=False, status='delegated',
                         last_error='Управление передано контроллеру ' + config['host'])
        archive, manifest = make_archive(original, marker['request_id'])
        response = sender(config, marker['request_id'], archive)
        require(isinstance(response, dict) and response.get('ok') is True
                and response.get('request_id') == marker['request_id']
                and response.get('installation_id') == plan['id'] and response.get('domain') == plan['domain']
                and response.get('manifest_hash') == _hash(manifest) and _path(response.get('state_dir')),
                'Контроллер не подтвердил точную установку. Повторите handoff; локальные изменения запрещены')
        completed = {**marker, 'state_dir': response['state_dir'], 'manifest_hash': response['manifest_hash']}
        original.save(DELEGATED, completed)
        return response


@contextmanager
def _receive_lock(root):
    fd = os.open(private_file(root / '.receive.lock'), os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def receive_archive(data, *, data_dir, journal_root, request_id):
    """Authenticated SSH receiver; only NEW journals, retries never overwrite."""
    from .enrollment import installation_snapshot, replacement_module
    require(re.fullmatch(r'[0-9a-f]{32}', request_id or ''), 'Неверный ID передачи')
    require(isinstance(data, bytes) and 0 < len(data) <= MAX_ARCHIVE, 'Неверный размер архива')
    root = private_directory(journal_root)
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
            for member in archive:
                require(member.isfile() and member.mode == 0o600 and 0 <= member.size <= MAX_FILE
                        and member.name not in files and (member.name == '__handoff__.json' or _allowed(member.name))
                        and len(files) <= MAX_FILES, 'Небезопасная запись архива журнала')
                stream = archive.extractfile(member)
                files[member.name] = stream.read(MAX_FILE + 1)
                require(len(files[member.name]) == member.size, 'Архив журнала неполон')
    except (tarfile.TarError, OSError, EOFError):
        raise ValueError('Некорректный архив журнала') from None
    try:
        manifest = json.loads(files.pop('__handoff__.json'))
        plan = json.loads(files['plan.json'])
        require(isinstance(manifest, dict) and isinstance(plan, dict)
                and manifest.get('format') == 1 and manifest.get('request_id') == request_id
                and re.fullmatch(r'[0-9a-f]{32}', plan.get('id', ''))
                and manifest.get('installation_id') == plan['id'] and manifest.get('plan_hash') == _hash(plan)
                and manifest.get('files') == {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
                'Манифест передачи не совпал с журналом')
    except (KeyError, ValueError, TypeError, UnicodeDecodeError):
        raise ValueError('Манифест передачи не совпал с журналом') from None
    # Match the installer's normal two-level discovery layout.
    r = replacement_module()
    namespace = private_directory(root / r.u.digest(plan.get('panel', ''))[:12])
    target = namespace / plan['id']
    receipt = {'format': 1, 'request_id': request_id, 'installation_id': plan['id'],
               'plan_hash': _hash(plan), 'manifest_hash': _hash(manifest)}
    with _receive_lock(root):
        if not target.exists() and not target.is_symlink():
            with tempfile.TemporaryDirectory(prefix='.receive-', dir=namespace) as temporary:
                stage = Path(temporary)
                for name, raw in files.items():
                    destination = stage / name
                    parent = stage
                    for component in PurePosixPath(name).parts[:-1]:
                        parent = private_directory(parent / component)
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                verified = installation_snapshot(stage)
                require(not verified['pending_ip'], 'Незавершённый журнал нельзя передать контроллеру')
                registered = Store(data_dir).get_server(verified['domain'])
                require(not registered or registered['state_dir'] == str(target),
                        'Домен уже управляется из другого журнала; ничего не перезаписано')
                stage_store = r.u.Store(stage)
                stage_store.save(RECEIVED, receipt)
                os.rename(stage, target)
                # TemporaryDirectory cleanup tolerates its already-moved path.
        original = r.u.Store(target)
        with original.lock():
            require(original.load(RECEIVED) == receipt,
                    'На контроллере уже есть другой журнал этой установки; ничего не перезаписано')
            actual = installation_snapshot(target, locked=True)
            inventory = Store(data_dir)
            registered = inventory.get_server(actual['domain'])
            if registered:
                require(registered['state_dir'] == str(target),
                        'Домен уже управляется из другого журнала; ничего не перезаписано')
            actual = inventory.register(actual)
        return {'ok': True, **receipt, 'domain': actual['domain'], 'state_dir': str(target),
                'current_ip': actual['current_ip'], 'generation': actual['generation'],
                'pending_ip': actual['pending_ip']}
