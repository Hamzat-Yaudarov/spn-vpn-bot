"""Private SQLite inventory; original deployment journals remain authoritative."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import stat
import time


DEFAULT_ROOT = '~/.spn-reserve-fleet'


def server_defaults(value):
    """Old inventories acquire additive UI/control fields without a migration."""
    for key, default in (('archived', False), ('archive_reason', ''), ('replacement_id', None),
                         ('last_error_code', None), ('last_error_log_id', None),
                         ('last_switch_target', None)):
        value.setdefault(key, default)
    return value


def require_active(value):
    if value.get('archived'):
        raise ValueError('Сервер находится в архиве; управление отключено до восстановления записи')
    if value.get('operational_job_id'):
        raise ValueError('Для сервера выполняется перенос VPS. Продолжите его в задачах установки.')
    return value


def require_reserve_support(value):
    if value.get('cdn_domain'):
        raise ValueError('Для CDN используйте перенос VPS. Автосмена резервных IP пока не поддерживается.')


def public_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError('Нужен публичный IPv4') from None
    if not isinstance(value, str) or not address.is_global or address.is_multicast:
        raise ValueError('Нужен публичный IPv4')
    return str(address)


def private_directory(value):
    path = Path(value).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Каталог панели не должен быть символической ссылкой')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Для каталога панели нужны ваш владелец и права 0700')
    return path


def private_file(path):
    if path.is_symlink():
        raise ValueError('Небезопасный файл панели')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except FileExistsError:
        pass
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError('Файлы панели должны принадлежать вам и иметь права 0600')
    return path


class Store:
    def __init__(self, root=DEFAULT_ROOT, clock=time.time):
        self.root, self.clock = private_directory(root), clock
        self.path = private_file(self.root / 'fleet.sqlite3')
        self.locks = private_directory(self.root / 'locks')
        with self.connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS servers (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                    server_id TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute('PRAGMA busy_timeout=10000')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def server_lock(self, server_id):
        path = private_file(self.locks / (hashlib.sha256(server_id.encode()).hexdigest() + '.lock'))
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Этот сервер уже проверяется или переключается') from None
            yield
        finally:
            os.close(fd)

    def get_server(self, server_id):
        with self.connection() as conn:
            row = conn.execute('SELECT body FROM servers WHERE id=?', (server_id,)).fetchone()
        return server_defaults(json.loads(row[0])) if row else None

    def list_servers(self, include_archived=False):
        with self.connection() as conn:
            rows = [server_defaults(json.loads(row[0])) for row in
                    conn.execute('SELECT body FROM servers ORDER BY id')]
        return rows if include_archived else [row for row in rows if not row['archived']]

    def _edit(self, server_id, change):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT body FROM servers WHERE id=?', (server_id,)).fetchone()
            if not row:
                raise ValueError('Сервер не найден')
            value = server_defaults(json.loads(row[0]))
            change(value)
            conn.execute('UPDATE servers SET body=? WHERE id=?', (json.dumps(value, ensure_ascii=False), server_id))
        return value

    def update(self, server_id, **changes):
        return self._edit(server_id, lambda value: value.update(changes))

    def register(self, installation):
        server_id = installation['domain']
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT body FROM servers WHERE id=?', (server_id,)).fetchone()
            if row:
                value = server_defaults(json.loads(row[0]))
                if value['state_dir'] != installation['state_dir'] or value['installation_id'] != installation['installation_id']:
                    raise ValueError('Домен уже относится к другой установке; журнал автоматически не заменяется')
                if value['archived']:
                    # Discovery and duplicate handoffs cannot revive an obsolete
                    # installation or reinterpret its unfinished operation.
                    return value
                if installation.get('generation', 0) < value.get('generation', 0):
                    raise ValueError('Журнал замен старее зарегистрированного; автосмена остановлена')
                # Repeated scans must not clear failure counters or enable automation.
                old_ip = value['current_ip']
                previous_pending = value.get('pending_ip')
                changed = old_ip != installation['current_ip']
                value.update(installation)
                if changed:
                    value.update(failures=0, status='unknown', last_error=None,
                                 last_error_code=None, last_error_log_id=None, last_switch_target=None,
                                 last_switch=self.clock(), last_switch_attempt=self.clock())
                    value['reserves'] = [item for item in value['reserves'] if item['ip'] != value['current_ip']]
                    if not any(item['ip'] == old_ip for item in value['reserves']):
                        value['reserves'].append({'ip': old_ip, 'status': 'unknown', 'last_checked': None})
                if installation.get('pending_ip'):
                    value.update(auto_enabled=False, status='pending')
                    if previous_pending != installation['pending_ip'] or not value.get('last_error_code'):
                        value.update(last_error='Незавершённая смена IP; повторите переключение на тот же адрес',
                                     last_error_code=None, last_error_log_id=None)
            else:
                value = {'id': server_id, 'reserves': [], 'auto_enabled': False, 'status': 'unknown',
                         'failures': 0, 'last_checked': None, 'last_error': None, 'last_switch': None,
                         'last_switch_attempt': None, **installation}
                server_defaults(value)
                if value.get('pending_ip'):
                    value['status'] = 'pending'
                conn.execute('INSERT INTO events(time,server_id,kind,message) VALUES(?,?,?,?)',
                             (self.clock(), server_id, 'registered', 'Сервер добавлен из журнала установки'))
            # Installer registration happens before its worker's final SSH-key
            # check. Claim the first visible card in the same transaction so
            # neither monitoring nor an API action can race that final step.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provisioning_jobs'").fetchone():
                for job_row in conn.execute('SELECT body FROM provisioning_jobs'):
                    job = json.loads(job_row[0])
                    if (job.get('kind') == 'create' and job.get('status') != 'succeeded'
                            and job.get('parameters', {}).get('domain') == server_id):
                        value.update(operational_job_id=job['id'], auto_enabled=False)
                        break
            conn.execute('INSERT INTO servers(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                         (server_id, json.dumps(value, ensure_ascii=False)))
        return value

    def archive(self, server_id, reason, replacement_id=None):
        if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500
                or any(ord(char) < 32 for char in reason)):
            raise ValueError('Укажите причину архивации длиной до 500 символов')
        if replacement_id is not None:
            replacement = self.get_server(replacement_id)
            if replacement_id == server_id or replacement is None or replacement.get('archived'):
                raise ValueError('Заменяющая запись должна быть другим действующим сервером панели')
        with self.server_lock(server_id):
            before = self.get_server(server_id)
            if before is None:
                raise ValueError('Сервер не найден')
            def change(value):
                value.update(archived=True, archive_reason=reason.strip(), replacement_id=replacement_id,
                             auto_enabled=False, status='archived')
            result = self._edit(server_id, change)
            if not before['archived'] or before['archive_reason'] != reason.strip() or before['replacement_id'] != replacement_id:
                self.event(server_id, 'archived', 'Запись перенесена в архив: ' + reason.strip())
            return result

    def restore(self, server_id):
        with self.server_lock(server_id):
            before = self.get_server(server_id)
            if before is None:
                raise ValueError('Сервер не найден')
            if not before['archived']:
                return before
            def change(value):
                value.update(archived=False, auto_enabled=False,
                             status='pending' if value.get('pending_ip') else
                             ('error' if value.get('last_error_code') else 'unknown'))
            result = self._edit(server_id, change)
            self.event(server_id, 'restored', 'Запись восстановлена из архива; автосмена выключена')
            return result

    def add_reserve(self, server_id, ip):
        ip = public_ipv4(ip)
        def change(value):
            require_active(value)
            require_reserve_support(value)
            if ip == value['current_ip']:
                raise ValueError('Это уже текущий IP')
            if any(item['ip'] == ip for item in value['reserves']):
                return
            if len(value['reserves']) >= 32:
                raise ValueError('Не более 32 резервных IP на сервер')
            value['reserves'].append({'ip': ip, 'status': 'unknown', 'last_checked': None})
        result = self._edit(server_id, change)
        self.event(server_id, 'reserve_added', 'Добавлен резервный IP: ' + ip)
        return result

    def remove_reserve(self, server_id, ip):
        ip = public_ipv4(ip)
        def change(value):
            require_active(value)
            require_reserve_support(value)
            if ip == value.get('pending_ip') or (value.get('status') == 'switching'):
                raise ValueError('Нельзя удалить адрес во время незавершённого переключения')
            value['reserves'] = [item for item in value['reserves'] if item['ip'] != ip]
        result = self._edit(server_id, change)
        self.event(server_id, 'reserve_removed', 'Резерв удалён из панели: ' + ip)
        return result

    def set_auto(self, server_id, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled должен быть true или false')
        def change(value):
            require_active(value)
            require_reserve_support(value)
            if enabled and (not value['reserves'] or value.get('pending_ip')):
                raise ValueError('Добавьте резервный IP и завершите прежнее переключение')
            value['auto_enabled'] = enabled
        result = self._edit(server_id, change)
        self.event(server_id, 'auto_enabled' if enabled else 'auto_disabled',
                   'Автосмена IP включена' if enabled else 'Автосмена IP выключена')
        return result

    def reserve_result(self, server_id, ip, passed):
        def change(value):
            require_active(value)
            for item in value['reserves']:
                if item['ip'] == ip:
                    item.update(status='online' if passed else 'offline', last_checked=self.clock())
        return self._edit(server_id, change)

    def event(self, server_id, kind, message):
        with self.connection() as conn:
            conn.execute('INSERT INTO events(time,server_id,kind,message) VALUES(?,?,?,?)',
                         (self.clock(), server_id, kind, message))
            conn.execute('DELETE FROM events WHERE id < (SELECT MAX(id)-2000 FROM events)')

    def overview(self, include_archived=False):
        with self.connection() as conn:
            events = [dict(zip(('id', 'time', 'server_id', 'kind', 'message'), row)) for row in
                      conn.execute('SELECT id,time,server_id,kind,message FROM events ORDER BY id DESC LIMIT 100')]
        return {'servers': self.list_servers(include_archived=include_archived), 'events': events}
