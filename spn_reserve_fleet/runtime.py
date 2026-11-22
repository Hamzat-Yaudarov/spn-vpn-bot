"""Small SSH monitor. It only switches preassigned IPs using the owned journal."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

from .auth import Auth
from .enrollment import (REPO_ROOT, InstallationBusy, discover, installation_snapshot, synchronize)
from .probe import check_ssh
from .store import DEFAULT_ROOT, Store, private_directory, public_ipv4, require_active, require_reserve_support


SWITCH_FAILURE_MESSAGES = {
    'topology_changed': 'Нода, хост, профиль или аудитория изменились. Проверьте запись установки в панели; чужие настройки не перезаписаны.',
    'ssh_identity_changed': 'SSH-ключ или идентичность VPS не совпали с журналом. Проверьте, не заменена ли эта установка новой.',
    'ssh_unavailable': 'Резервный IP не отвечает по SSH из сети контроллера. Проверьте доступность и повторите тот же адрес.',
    'ssh_auth_failed': 'Контроллер не смог войти на VPS по служебному SSH-ключу. Проверьте его доступ и повторите тот же адрес.',
    'dns_failed': 'DNS-запись не подтверждена. Проверьте DNS и продолжите переключение на тот же адрес.',
    'certificate_failed': 'Подготовка TLS-сертификата не завершена. Продолжите перенос после проверки сертификата в журнале.',
    'subscription_export_failed': 'Не удалось подтвердить параметры подключения в тестовой подписке. Проверьте её и продолжите ту же задачу.',
    'route_changed': 'Маршрут или исходящий IP отличаются от сохранённого плана. Проверьте сеть VPS и продолжите тот же адрес.',
    'policy_routing': 'Дополнительные правила маршрутизации VPS не прошли проверку. Проверьте правила в журнале операции.',
    'guard_failed': 'Защита резервных IP не подтверждена. Проверьте защиту VPS и продолжите переключение на тот же адрес.',
    'api_failed': 'Панель VPN не подтвердила изменение. Проверьте её доступность и продолжите тот же адрес.',
    'verification_mode_changed': 'Прежняя операция требует другого режима проверки. Продолжите её с исходными параметрами.',
    'switch_timeout': 'Время смены IP истекло. Проверьте журнал и продолжите переключение на тот же адрес.',
    'executor_failed': 'На контроллере не удалось запустить программу смены IP. Проверьте службу панели.',
    'switch_failed': 'Скрипт остановил переключение. Проверьте приватный журнал и продолжите тот же адрес.',
}


class SwitchFailure(ValueError):
    """Only fixed, reviewed messages may leave the private execution boundary."""
    def __init__(self, code='switch_failed', public_message=None, log_id=None):
        self.code = code if code in SWITCH_FAILURE_MESSAGES else 'switch_failed'
        self.public_message = SWITCH_FAILURE_MESSAGES[self.code]
        self.log_id = log_id if isinstance(log_id, str) and re.fullmatch(r'[0-9a-f]{32}', log_id) else None
        # Never forward an exception/log string supplied as public_message.
        super().__init__(self.public_message)


def classify_switch_failure(tail, log_id=None):
    """Classify bounded diagnostic text; values and raw lines are never returned."""
    if isinstance(tail, bytes):
        tail = tail[-65536:].decode('utf-8', errors='replace')
    elif isinstance(tail, str):
        tail = tail[-65536:]
    else:
        tail = ''
    diagnostic = [line for line in tail.splitlines() if re.search(
        r'(?i)^\s*(?:остановлено:|stopped:|ssh:)|permission denied \((?:publickey|password)|'
        r'remote host identification has changed|host key verification failed|connection timed out|'
        r'connection refused|no route to host|connection reset by peer', line)]
    text = ('\n'.join(diagnostic) if diagnostic else tail).casefold()
    patterns = (
        ('topology_changed', ('нода, хост, профиль или аудитория изменились', 'технические параметры',
                              'uuid инбаунда/привязка ноды изменились', 'исходный профиль используется другой нодой',
                              'адрес/состояние ноды не соответствует журналу', 'исходный хост выключен')),
        ('ssh_identity_changed', ('remote host identification has changed', 'host key verification failed',
                                  'ssh-ключ не совпал', 'ssh-ключ, machine-id или маркер отличаются',
                                  'это не прежняя установленная vps', 'vps была переустановлена')),
        ('ssh_auth_failed', ('permission denied (publickey', 'permission denied (password', 'authentication failed')),
        ('ssh_unavailable', ('connection timed out', 'connection refused', 'no route to host',
                             'не удалось получить ssh ed25519-ключ', 'не удалось получить ssh-ключ для сверки',
                             'connection closed by remote host', 'connection reset by peer')),
        ('verification_mode_changed', ('режим проверки незавершённой операции отличается',
                                       'режим незавершённой операции отличается', 'продолжите без --ssh-only')),
        ('certificate_failed', ('небезопасные права или размер tls-сертификата', 'выпуск tls через dns-01',
                                'tls certificate import failed', 'tls-сертификат, его домен')),
        ('subscription_export_failed', ('отсутствует в свежей подписке', 'выгрузка подписки не совпала',
                                        'выгрузка подписки изменила параметры',
                                        'выгрузка подписки не подтвердила')),
        ('dns_failed', ('dns ipv4', 'dns-запись', 'dns record', 'dns replacement', 'cloudflare dns',
                        'dnsconflict', 'dnsprovisionerror', 'dnsoutcomeunknown',
                        'распространения dns', 'проверить dns', 'dns не подтверд')),
        ('guard_failed', ('node guard', 'node_guard', 'защита резервных ip', 'scanner filter',
                         'kernel firewall', 'nftables', 'guard policy')),
        ('policy_routing', ('policy routing is not supported', 'policy routing target must already use')),
        ('route_changed', ('default route changed', 'preferred source is not active', 'preferred ipv4',
                           'сохранение после перезагрузки не подтверждены', 'policy routing', 'маршрут')),
        ('api_failed', ('ответ панели потерян', 'панель: http', 'ответ выключения ноды потерян',
                       'api-токен', 'remnawave_api_token', 'remnawave_base_url')),
    )
    for code, markers in patterns:
        if any(marker in text for marker in markers):
            return SwitchFailure(code, log_id=log_id)
    return SwitchFailure(log_id=log_id)


class ChangeExecutor:
    def __init__(self, root, *, env_file=None, timeout=900):
        self.logs = private_directory(Path(root) / 'logs')
        self.env_file, self.timeout = env_file, timeout

    def __call__(self, server, ip):
        command = [sys.executable, str(REPO_ROOT / 'ops/universal-reality/change_ip.py'),
                   '--domain', server['domain'], '--ip', ip, '--state-dir', server['state_dir'],
                   '--panel-url', server['panel_url'], '--ssh-only', '--non-interactive', '--apply']
        if self.env_file:
            command += ['--env-file', str(self.env_file)]
        path = self.logs / (uuid.uuid4().hex + '.log')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT, cwd=REPO_ROOT, start_new_session=True)
            except OSError:
                raise SwitchFailure('executor_failed', log_id=path.stem) from None
            try:
                result = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                raise SwitchFailure('switch_timeout', log_id=path.stem) from None
        if result != 0:
            tail = b''
            try:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as log:
                    info = os.fstat(log.fileno())
                    if (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1):
                        log.seek(max(0, info.st_size - 65536))
                        tail = log.read(65536)
            except OSError:
                pass
            raise classify_switch_failure(tail, path.stem)


class Runtime:
    def __init__(self, data_dir=None, *, probe=check_ssh, executor=None,
                 snapshot=installation_snapshot, clock=time.time, failure_threshold=3,
                 cooldown=300, interval=30, env_file=None):
        self.store = Store(data_dir or DEFAULT_ROOT, clock=clock)
        self.env_file = env_file
        self.auth = Auth(self.store.root / 'password.json', clock=clock)
        self.probe, self.snapshot, self.clock = probe, snapshot, clock
        self.executor = executor or ChangeExecutor(self.store.root, env_file=env_file)
        self.threshold, self.cooldown, self.interval = failure_threshold, cooldown, interval
        if (not 2 <= self.threshold <= 20 or not 10 <= self.interval <= 3600
                or not 60 <= self.cooldown <= 86400):
            raise ValueError('Порог: 2–20, интервал: 10–3600 с, пауза: 60–86400 с')

    def _server(self, server_id):
        value = self.store.get_server(server_id)
        if value is None:
            raise ValueError('Сервер не найден')
        return value

    def _active_server(self, server_id):
        return require_active(self._server(server_id))

    def _sync(self, server_id):
        server = self._active_server(server_id)
        if self.snapshot is installation_snapshot:
            return synchronize(self.store, server['state_dir'])
        installation = self.snapshot(server['state_dir'])
        return self.store.register(installation)

    def scan(self):
        return discover(self.store)

    def check(self, server_id):
        with self.store.server_lock(server_id):
            if self._server(server_id).get('operational_job_id'):
                return self._server(server_id)
            self._active_server(server_id)
            try:
                server = self._sync(server_id)
            except InstallationBusy:
                return self._server(server_id)
            except (ValueError, RuntimeError, OSError):
                return self.store.update(server_id, auto_enabled=False, status='error',
                                         last_error='Журнал установки не подтверждён; автосмена остановлена')
            now = self.clock()
            previous_error = server.get('last_error')
            passed = self.probe(server['current_ip'], server['ssh_port'])
            recent = server.get('last_checked') is not None and now - server['last_checked'] <= self.interval * 3
            failures = 0 if passed else ((server['failures'] if recent else 0) + 1)
            status = ('pending' if server.get('pending_ip') else
                      ('error' if server.get('last_error_code') else ('online' if passed else 'offline')))
            changes = {'last_checked': now, 'failures': failures, 'status': status}
            if not server.get('pending_ip') and not server.get('last_error_code'):
                changes['last_error'] = None if passed else 'SSH не отвечает из сети этой машины'
            server = self.store.update(server_id, **changes)
            if passed or failures < self.threshold or not server['auto_enabled'] or server.get('pending_ip'):
                return server
            attempt = server.get('last_switch_attempt')
            if attempt is not None and now - attempt < self.cooldown:
                return server
            candidates = list(server['reserves'])
            for candidate in candidates:
                available = self.probe(candidate['ip'], server['ssh_port'])
                self.store.reserve_result(server_id, candidate['ip'], available)
                if not available:
                    continue
                # A temporary outage can recover while reserves were checked.
                if self.probe(server['current_ip'], server['ssh_port']):
                    return self.store.update(server_id, status='online', failures=0, last_error=None)
                if not self._server(server_id)['auto_enabled']:
                    return self._server(server_id)
                return self._switch(server_id, candidate['ip'])
            self.store.update(server_id, last_error='Текущий и все резервные IP не отвечают по SSH; адрес не изменён')
            # Avoid identical event spam each cycle while a home connection is down.
            if previous_error != 'Текущий и все резервные IP не отвечают по SSH; адрес не изменён':
                self.store.event(server_id, 'no_reserve', 'Рабочих резервных IP нет; переключение не выполнялось')
            return self._server(server_id)

    def switch(self, server_id, ip):
        ip = public_ipv4(ip)
        with self.store.server_lock(server_id):
            self._active_server(server_id)
            self._sync(server_id)
            return self._switch(server_id, ip)

    def _switch(self, server_id, ip):
        server = self._active_server(server_id)
        require_reserve_support(server)
        pending = server.get('pending_ip')
        if pending and pending != ip:
            raise ValueError('Продолжите незавершённую смену на тот же IP')
        if ip == server['current_ip'] and not pending:
            return server
        if not any(candidate['ip'] == ip for candidate in server['reserves']):
            raise ValueError('Сначала добавьте IP в резерв этой VPS')
        if not self.probe(ip, server['ssh_port']):
            self.store.reserve_result(server_id, ip, False)
            raise ValueError('Резервный IP не отвечает по SSH; переключение не начато')
        self.store.update(server_id, status='switching', last_switch_attempt=self.clock(), last_error=None,
                          last_error_code=None, last_error_log_id=None, last_switch_target=ip)
        self.store.event(server_id, 'switch_started', server['current_ip'] + ' → ' + ip)
        try:
            self.executor(server, ip)
            updated = self._sync(server_id)
            if updated['current_ip'] != ip or updated.get('pending_ip'):
                raise ValueError('Завершение смены IP не подтверждено журналом')
        except Exception as error:
            # An interrupted command may have changed DNS or a route. Never
            # try the next IP until its exact journal has been reconciled.
            pending_ip = ip
            try:
                reconciled = self._sync(server_id)
                pending_ip = reconciled.get('pending_ip')
                if reconciled['current_ip'] == ip and not pending_ip:
                    return self._complete(server_id, server['current_ip'], ip)
            except Exception:
                pass
            failure = error if isinstance(error, SwitchFailure) else SwitchFailure()
            self.store.update(server_id, auto_enabled=False, status='pending' if pending_ip else 'error',
                              pending_ip=pending_ip,
                              last_error=failure.public_message, last_error_code=failure.code,
                              last_error_log_id=failure.log_id, last_switch_target=ip)
            self.store.event(server_id, 'switch_failed', failure.public_message + ' Автосмена выключена.')
            raise failure from None
        return self._complete(server_id, server['current_ip'], ip)

    def _complete(self, server_id, old_ip, ip):
        def change(value):
            reserves = [candidate for candidate in value['reserves'] if candidate['ip'] != ip]
            if old_ip != ip and not any(candidate['ip'] == old_ip for candidate in reserves):
                reserves.append({'ip': old_ip, 'status': 'unknown', 'last_checked': None})
            value.update(reserves=reserves, current_ip=ip, pending_ip=None, status='online',
                         failures=0, last_checked=self.clock(), last_switch=self.clock(), last_error=None,
                         last_error_code=None, last_error_log_id=None, last_switch_target=None)
        self.store._edit(server_id, change)
        self.store.event(server_id, 'switch_completed', 'Смена подтверждена: ' + old_ip + ' → ' + ip)
        return self._server(server_id)

    def tick(self):
        self.scan()
        ids = [server['id'] for server in self.store.list_servers()]
        def checked(server_id):
            try:
                return self.check(server_id)
            except ValueError:
                return self._server(server_id)
        with ThreadPoolExecutor(max_workers=8) as workers:
            return list(workers.map(checked, ids))
