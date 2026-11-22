"""Durable installation jobs. Passwords exist only in the request and worker pipe."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from .enrollment import REPO_ROOT, replacement_module
from .provision_ssh import validate_password
from .store import private_directory, private_file, public_ipv4, require_active


CREATE_FIELDS = {'kind', 'request_id', 'server_id', 'ip', 'ssh_port', 'password', 'host_key_sha256',
                 'domain', 'name', 'country', 'transport', 'squad_ids', 'cdn_domain'}
RESUME_FIELDS = {'password', 'certificate_target', 'cdn_target', 'finish'}
TERMINAL = {'succeeded'}
RUNNING = {'queued', 'running'}
PUBLIC_FIELDS = ('id', 'kind', 'status', 'stage', 'stage_label', 'progress', 'domain', 'target_ip',
                 'server_id', 'result_server_id', 'created_at', 'updated_at', 'error', 'error_code', 'log_id',
                 'transport', 'ssh_port')


def public_job(value):
    result = {key: value.get(key) for key in PUBLIC_FIELDS}
    result.update(can_resume=value['status'] not in RUNNING | TERMINAL,
                  needs_password=value['status'] in {'failed', 'needs_credentials'},
                  needs_credentials=value['status'] == 'needs_credentials')
    if isinstance(value.get('cdn'), dict):
        result['cdn'] = {key: value['cdn'].get(key) for key in
                         ('origin_domain', 'cdn_domain', 'transport_path', 'certificate_name', 'guide_text')}
    return result


def port(value):
    if type(value) is not int or not 1 <= value <= 65535 or value in (80, 443, 2222, 10050, 10085):
        raise ValueError('Укажите доступный SSH-порт, обычно 22')
    return value


def dns_name(value):
    if not isinstance(value, str):
        raise ValueError('Нужен домен в зоне wayspn.com')
    try:
        value = replacement_module().d.dns.normalize_name(value)
    except Exception:
        raise ValueError('Нужен домен без https:// и пути') from None
    if not value.endswith('.wayspn.com') or value == 'wayspn.com':
        raise ValueError('Для этой панели используйте отдельный домен в зоне wayspn.com')
    return value


class JobStore:
    def __init__(self, store, attempt_id=None):
        self.store, self.attempt_id = store, attempt_id
        self.root = private_directory(store.root / 'provisioning')
        self.logs = private_directory(store.root / 'logs')
        with store.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS provisioning_jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)')

    def get(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{32}', job_id):
            raise ValueError('Задача не найдена')
        with self.store.connection() as conn:
            row = conn.execute('SELECT body FROM provisioning_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Задача не найдена')
        return json.loads(row[0])

    def list(self):
        with self.store.connection() as conn:
            values = [json.loads(row[0]) for row in conn.execute('SELECT body FROM provisioning_jobs')]
        return sorted(values, key=lambda v: v['created_at'], reverse=True)

    def update(self, job_id, **changes):
        if 'password' in changes:
            raise ValueError('Пароли нельзя сохранять в журнале')
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT body FROM provisioning_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('Задача не найдена')
            value = json.loads(row[0])
            if self.attempt_id is not None and value.get('attempt_id') != self.attempt_id:
                return value
            if value['status'] == 'succeeded':
                return value
            value.update(changes, updated_at=time.time())
            conn.execute('UPDATE provisioning_jobs SET body=? WHERE id=?',
                         (json.dumps(value, ensure_ascii=False), job_id))
        return value

    def insert(self, value):
        if 'password' in value or 'password' in value.get('parameters', {}):
            raise ValueError('Пароли нельзя сохранять в журнале')
        with self.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = [json.loads(row[0]) for row in conn.execute('SELECT body FROM provisioning_jobs')]
            for old in current:
                if old['id'] == value['id']:
                    if old['parameters'] != value['parameters']:
                        raise ValueError('Этот запрос уже относится к другой установке')
                    return old, False
                if old['status'] in RUNNING:
                    raise ValueError('Дождитесь завершения выполняющейся установки или переноса')
                if old['status'] != 'succeeded' and (old['domain'] == value['domain']
                                                     or old['target_ip'] == value['target_ip']):
                    raise ValueError('Для этого домена или IP уже есть задача. Продолжите её.')
            if value['kind'] == 'replace':
                server_id = value['server_id']
                record = conn.execute('SELECT body FROM servers WHERE id=?', (server_id,)).fetchone()
                if record is None:
                    raise ValueError('Сервер не найден')
                server = json.loads(record[0])
                if server.get('pending_ip') or server.get('operational_job_id') or server.get('archived'):
                    raise ValueError('Сервер занят другой операцией')
                if value['target_ip'] == server['current_ip'] or value['target_ip'] in {r['ip'] for r in server['reserves']}:
                    raise ValueError('Для переноса нужен IP другой VPS')
                value['parameters']['source_vps'] = {'ip': server['current_ip'], 'reserves': server['reserves']}
                server.update(operational_job_id=value['id'], auto_enabled=False)
                conn.execute('UPDATE servers SET body=? WHERE id=?', (json.dumps(server, ensure_ascii=False), server_id))
            conn.execute('INSERT INTO provisioning_jobs(id,body) VALUES(?,?)',
                         (value['id'], json.dumps(value, ensure_ascii=False)))
        return value, True


class Provisioner:
    def __init__(self, runtime, env_file=None, *, launch=None, recover=True):
        self.runtime, self.store = runtime, runtime.store
        self.env_file = Path(env_file or REPO_ROOT / '.env').expanduser().absolute()
        self.jobs = JobStore(self.store)
        self.subscription = self.store.root.parent / 'config/vpn-test-link.txt'
        if not self.subscription.exists():
            self.subscription = REPO_ROOT / '.vpn-test-link.txt'
        self.identity = Path.home() / '.ssh/id_ed25519'
        self.launcher = launch or self._launch
        self.mutex = threading.Lock()
        if recover:
            # systemd kills this service's children on restart. Retain every
            # immutable parameter/journal; a password must be supplied again.
            for job in self.jobs.list():
                if job['status'] in RUNNING:
                    self.jobs.update(job['id'], status='needs_credentials', stage='interrupted',
                                     stage_label='Программа перезапущена. Продолжите ту же задачу.',
                                     error='Установка сохранена. Проверьте VPS и продолжите.',
                                     error_code='interrupted')

    def settings(self):
        d = replacement_module().d
        values = d.u.read_settings(self.env_file)
        base, token = values.get('REMNAWAVE_BASE_URL'), values.get('REMNAWAVE_API_TOKEN')
        if not base or not token:
            raise ValueError('На контроллере не настроен доступ к панели VPN')
        return d.u.panel_url(base), token

    def options(self):
        enabled, reason, squads = True, '', []
        try:
            base, token = self.settings()
            d = replacement_module().d
            d.probe.private_file(self.subscription)
            if not self.identity.is_file() or not self.identity.with_suffix('.pub').is_file():
                raise ValueError('Не настроен служебный SSH-ключ контроллера')
            d.cloudflare_token(type('Options', (), {'env_file': str(self.env_file),
                                                    'cloudflare_token_file': None})(), {})
            squads = [{'id': str(row['uuid']), 'name': str(row['name'])[:100]}
                      for row in d.API(base, token).rows('internal-squads')]
        except Exception:
            enabled, reason = False, 'Не удалось проверить доступ к панели VPN и настройкам установки. Повторите позже.'
        return {'enabled': enabled, 'disabled_reason': reason,
                'capabilities': {'create_tls': True, 'create_reality': True, 'create_cdn': True,
                                 'replace_tls': True, 'replace_reality': True, 'replace_cdn': True},
                'defaults': {'ssh_port': 22, 'transport': 'tls', 'dns_suffix': 'wayspn.com'}, 'squads': squads}

    def ssh_key(self, ip, ssh_port):
        ip, ssh_port = public_ipv4(ip), port(ssh_port)
        host = ip if ssh_port == 22 else '[' + ip + ']:' + str(ssh_port)
        try:
            _, fingerprint = replacement_module().d.trust._scan(ip, ssh_port, host)
        except Exception:
            raise ValueError('Не удалось получить SSH-ключ. Проверьте IP и порт VPS.') from None
        return {'fingerprint': fingerprint}

    def validate(self, data):
        if set(data) != CREATE_FIELDS:
            raise ValueError('Неверные поля формы установки')
        password = validate_password(data['password'])
        try:
            job_id = uuid.UUID(data['request_id']).hex
        except (TypeError, ValueError, AttributeError):
            raise ValueError('Нужен идентификатор запроса') from None
        ip, ssh_port = public_ipv4(data['ip']), port(data['ssh_port'])
        fp = data['host_key_sha256']
        if not isinstance(fp, str) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fp):
            raise ValueError('Получите и подтвердите отпечаток SSH-ключа VPS')
        parameters = {key: value for key, value in data.items() if key not in {'password', 'request_id'}}
        parameters.update(ip=ip, ssh_port=ssh_port)
        if data['kind'] == 'create':
            if data['server_id'] is not None or data['transport'] not in {'tls', 'reality', 'cdn'}:
                raise ValueError('Выберите поддерживаемый тип нового сервера')
            name, country = data['name'], data['country']
            if (not isinstance(name, str) or not 1 <= len(name) <= 60
                    or any(ord(c) < 32 or ord(c) == 127 for c in name)):
                raise ValueError('Название: от 1 до 60 символов без переносов строк')
            if country is not None and (not isinstance(country, str) or not re.fullmatch('[A-Z]{2}', country)):
                raise ValueError('Нужен двухбуквенный код страны, например FI')
            squads = data['squad_ids']
            if (not isinstance(squads, list) or not 1 <= len(squads) <= 100
                    or any(not isinstance(uid, str) or not re.fullmatch(
                        r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', uid) for uid in squads)):
                raise ValueError('Выберите хотя бы один сквад')
            parameters['squad_ids'] = sorted(set(squads))
            prefix = 'origin-yc-' if data['transport'] == 'cdn' else 'n-'
            parameters['domain'] = dns_name(data['domain'] or prefix + job_id[:6] + '.wayspn.com')
            parameters['cdn_domain'] = dns_name(data['cdn_domain'] or 'cdn-yc-' + job_id[:6] + '.wayspn.com') \
                if data['transport'] == 'cdn' else None
            if parameters['cdn_domain'] == parameters['domain']:
                raise ValueError('Для CDN нужны разные origin и клиентский домен')
        elif data['kind'] == 'replace':
            row = self.store.get_server(data['server_id'])
            if row is None:
                raise ValueError('Сервер не найден')
            require_active(row)
            if row.get('pending_ip') or row.get('operational_job_id'):
                raise ValueError('Сначала завершите предыдущую операцию этого сервера')
            if row['transport'] not in {'raw-tls-vision-safari-v1', 'raw-reality-vision-safari-v1', 'yandex-xhttp-cdn-v1'}:
                raise ValueError('Перенос поддерживается для управляемых TLS, REALITY и CDN установок')
            if ssh_port != row['ssh_port']:
                raise ValueError('При переносе сохраните прежний SSH-порт')
            if any(data[k] is not None for k in ('domain', 'name', 'country', 'transport', 'cdn_domain')) or data['squad_ids']:
                raise ValueError('Домен, тип и сквады при переносе наследуются от текущего сервера')
            parameters.update(domain=row['domain'], transport='cdn' if row.get('cdn_domain') else
                              'tls' if 'tls' in row['transport'] else 'reality', cdn_domain=row.get('cdn_domain'),
                              state_dir=row['state_dir'], installation_id=row['installation_id'],
                              source_vps={'ip': row['current_ip'], 'reserves': row['reserves']})
        else:
            raise ValueError('Неизвестный тип задачи')
        for row in self.store.list_servers():
            if parameters['kind'] == 'create' and parameters['domain'] == row['domain']:
                raise ValueError('Домен уже используется. Для другой VPS выберите перенос этого сервера.')
            if ip == row['current_ip'] or ip in {r['ip'] for r in row.get('reserves', [])}:
                raise ValueError('Этот IP уже относится к VPS в панели. Для переноса нужен другой сервер.')
        return job_id, parameters, password

    def create(self, data):
        # A repeated POST is an observation, never another attempt or password
        # login. Validate canonical immutable input against the existing job.
        try:
            old = self.jobs.get(uuid.UUID(data.get('request_id', '')).hex)
        except (ValueError, AttributeError, TypeError):
            old = None
        if old:
            original = old['request']
            candidate = {k: v for k, v in data.items() if k != 'password'}
            if candidate != original:
                raise ValueError('Идентификатор запроса уже связан с другими параметрами')
            validate_password(data.get('password'))
            return public_job(old)
        job_id, params, password = self.validate(data)
        with self.mutex:
            now = time.time()
            job = {'id': job_id, 'kind': params['kind'], 'parameters': params,
                   'request': {k: v for k, v in data.items() if k != 'password'},
                   'status': 'queued', 'stage': 'queued', 'stage_label': 'Задача принята', 'progress': 0,
                   'domain': params.get('cdn_domain') or params['domain'], 'target_ip': params['ip'],
                   'transport': params['transport'], 'ssh_port': params['ssh_port'],
                   'server_id': params['server_id'], 'created_at': now, 'updated_at': now,
                   'error': None, 'error_code': None, 'log_id': None, 'result_server_id': None}
            job['attempt_id'] = uuid.uuid4().hex
            # One cross-process lock serializes admission with worker lifetime.
            with self.store.server_lock('provisioning-global'):
                if job['kind'] == 'replace':
                    with self.store.server_lock(params['server_id']):
                        row = self.store.get_server(params['server_id'])
                        if row.get('pending_ip') or row.get('operational_job_id') or row.get('archived'):
                            raise ValueError('Сервер занят другой операцией')
                        job, inserted = self.jobs.insert(job)
                else:
                    job, inserted = self.jobs.insert(job)
            if inserted:
                self.launcher(job_id, password)
            return public_job(self.jobs.get(job_id))

    def resume(self, job_id, data):
        if set(data) != RESUME_FIELDS or type(data['finish']) is not bool:
            raise ValueError('Неверные поля продолжения задачи')
        password = validate_password(data['password'])
        with self.mutex, self.store.server_lock('provisioning-global'):
            job = self.jobs.get(job_id)
            if job['status'] in RUNNING | TERMINAL:
                raise ValueError('Эта задача уже выполняется или завершена')
            if any(j['status'] in RUNNING for j in self.jobs.list()):
                raise ValueError('Дождитесь текущей установки')
            if job['transport'] != 'cdn' and (data['finish'] or data['certificate_target'] or data['cdn_target']):
                raise ValueError('Поля CDN не относятся к этому серверу')
            continuation = {}
            if job['transport'] == 'cdn':
                for field in ('certificate_target', 'cdn_target'):
                    value = data[field]
                    if value is not None:
                        kind = 'certificate' if field == 'certificate_target' else 'cdn'
                        try:
                            # This pure validator accepts only the vendor's exact
                            # DNS suffix; arbitrary URLs/commands never reach CLI.
                            from importlib.util import spec_from_file_location, module_from_spec
                            spec = spec_from_file_location('spn_job_cname', REPO_ROOT / 'ops/universal-yandex/dns_records.py')
                            records = module_from_spec(spec); spec.loader.exec_module(records)
                            continuation[field] = records.target(value, kind)
                        except Exception:
                            raise ValueError('Скопируйте полное CNAME-значение из Яндекса без https:// и пути') from None
                continuation['finish'] = data['finish']
            self.jobs.update(job_id, status='queued', stage='queued', stage_label='Продолжаем задачу',
                             continuation=continuation, error=None, error_code=None, attempt_id=uuid.uuid4().hex)
        self.launcher(job_id, password)
        return public_job(self.jobs.get(job_id))

    def _launch(self, job_id, password):
        attempt_id = self.jobs.get(job_id)['attempt_id']
        scoped = JobStore(self.store, attempt_id=attempt_id)
        path = private_file(self.jobs.logs / (job_id + '-worker.log'))
        try:
            with open(path, 'ab', buffering=0) as log:
                child = subprocess.Popen([sys.executable, '-m', 'spn_reserve_fleet.provision_worker',
                    '--data-dir', str(self.store.root), '--job-id', job_id, '--env-file', str(self.env_file),
                    '--attempt-id', attempt_id],
                    stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                    cwd=REPO_ROOT, start_new_session=True, close_fds=True)
            child.stdin.write(json.dumps({'password': password}, ensure_ascii=False).encode())
            child.stdin.close()
            # Reap asynchronously; the worker itself owns durable status.
            def reap():
                result = child.wait()
                state = self.jobs.get(job_id)
                if state['status'] in RUNNING:
                    scoped.update(job_id, status='failed', error_code='worker_stopped',
                        error='Программа установки остановилась. Продолжите ту же задачу.',
                        stage='stopped', stage_label='Ожидает продолжения')
            threading.Thread(target=reap, daemon=True, name='provision-reaper').start()
        except OSError:
            scoped.update(job_id, status='failed', error_code='worker_start',
                             error='Не удалось запустить установщик. Продолжите задачу.',
                             stage_label='Ожидает продолжения')
