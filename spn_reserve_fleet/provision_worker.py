"""Private, restartable worker for the existing installers. No public raw logs."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import importlib.util
import inspect
import json
import os
from pathlib import Path
import selectors
import select
import shlex
import signal
import subprocess
import sys
import time

from .enrollment import REPO_ROOT, replacement_module, synchronize
from .provisioning import JobStore
from .provision_ssh import PasswordMaster, SSHLoginError, validate_password
from .runtime import classify_switch_failure
from .store import Store, private_directory, private_file


def command_for(job, state_dir, env_file, subscription, control):
    p = job['parameters']
    script = 'replace.py' if job['kind'] == 'replace' else 'deploy.py'
    family = 'universal-yandex' if p['transport'] == 'cdn' else 'universal-reality'
    command = [sys.executable, str(REPO_ROOT / 'ops' / family / script), '--ip', p['ip'],
               '--domain', p['domain'], '--ssh-port', str(p['ssh_port']), '--state-dir', str(state_dir),
               '--env-file', str(env_file), '--test-subscription', str(subscription),
               '--host-key-sha256', p['host_key_sha256'], '--ssh-control', str(control),
               '--test-route', 'system', '--interface', 'lo' if sys.platform.startswith('linux') else 'en0',
               '--apply']
    if job['kind'] == 'create':
        command += ['--name', p['name']]
        if p.get('country'):
            command += ['--country', p['country']]
        for squad in p['squad_ids']:
            command += ['--squad', squad]
        if p['transport'] == 'cdn':
            command += ['--cdn-domain', p['cdn_domain'], '--agree-le-tos']
        else:
            command += ['--transport', p['transport'], '--fleet-dir', str(job['data_dir'])]
    if p['transport'] == 'cdn' and job['kind'] == 'create':
        continuation = job.get('continuation', {})
        for field, flag in (('certificate_target', '--certificate-target'), ('cdn_target', '--cdn-target')):
            if continuation.get(field):
                command += [flag, continuation[field]]
        if continuation.get('finish'):
            command += ['--finish']
    return command


STAGES = (
    ('tls', 'Подготавливаем TLS-сертификат', 12, ('Подготавливаю TLS-сертификат для переноса.',)),
    ('packages', 'Подготавливаем VPS', 18, ('VPS preflight OK', 'Проверяю сохранённую')),
    ('routes', 'Проверяем сеть новой VPS', 28, ('Проверяю маршруты новой VPS.',)),
    ('node', 'Устанавливаем VPN и защиту', 40, ('Код установки:', 'Managed TLS node installation ready',
                                            'Yandex CDN ', 'Managed origin installation ready')),
    ('verify', 'Проверяем VPN-трафик', 65, ('Проверка VPN:', 'Node listeners', 'VPN-тест:')),
    ('dns', 'Переключаем домен и проверяем подписку', 85, ('IP существующей DNS-записи обновлён',)),
)


def execute(command, jobs, job_id, log_path, timeout=2700):
    """Only fixed stage labels leave this boundary, never installer output."""
    descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    child = None
    tail, pending = b'', b''
    try:
        with os.fdopen(descriptor, 'wb', buffering=0) as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, cwd=REPO_ROOT, close_fds=True, start_new_session=True)
            selector = selectors.DefaultSelector()
            selector.register(child.stdout, selectors.EVENT_READ)
            deadline, last_progress = time.monotonic() + timeout, 0
            while selector.get_map():
                if time.monotonic() > deadline:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL); child.wait()
                    raise ValueError('installer_timeout')
                for key, _ in selector.select(.5):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    log.write(chunk)
                    tail = (tail + chunk)[-65536:]
                    pending = (pending + chunk)[-65536:]
                    lines = pending.split(b'\n')
                    pending = lines.pop()
                    for line in lines:
                        text = line.decode('utf-8', errors='replace')
                        for stage, label, progress, matches in STAGES:
                            if progress > last_progress and any(value in text for value in matches):
                                jobs.update(job_id, stage=stage, stage_label=label, progress=progress)
                                last_progress = progress
            result = child.wait(timeout=15)
        return result, tail
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()


def install_key(master, identity):
    from .remote import canonical_node_key, append_node_key
    path = identity.with_suffix('.pub')
    raw = path.read_text().strip()
    key = canonical_node_key(raw)
    script = ('import json,sys\n' + inspect.getsource(canonical_node_key) + '\n'
              + inspect.getsource(append_node_key)
              + '\nprint(json.dumps(append_node_key("/root", json.loads(sys.argv[1]))))\n')
    result = master.run(shlex.join(['python3', '-', json.dumps(key)]), script.encode(), capture=True)
    if json.loads(result).get('ok') is not True:
        raise SSHLoginError('Служебный SSH-ключ контроллера не подтверждён')


def cdn_public(state_dir):
    plan = json.loads((state_dir / 'plan.json').read_text())
    origin, cdn = plan['domain'], plan['cdnDomain']
    # No filesystem paths, CLI flags, tokens, client credentials or hidden logs.
    text = (f'1. В Certificate Manager Яндекса создайте сертификат Let’s Encrypt для {cdn}. '
            f'Выберите подтверждение DNS CNAME и скопируйте значение для _acme-challenge.{cdn} в поле ниже. '
            'Сохраните CNAME и дождитесь статуса Issued.\n\n'
            f'2. Создайте CDN-ресурс в том же каталоге. Домен раздачи: {cdn}. Источник: {origin}, HTTPS, порт 443. '
            f'Host и SNI источника: {origin}. Подключите выпущенный сертификат. '
            'Кеширование CDN и браузера, сжатие, slicing, shield, перенаправления и переписывание пути выключите. '
            'Не удаляйте query-параметры и не меняйте X-Transport-Data. Методы: GET, HEAD, OPTIONS. '
            'TLS-профиль: «Защищённый (TLSv1.2+ с поддержкой PFS и AEAD)». '
            'Не включайте токены доступа, географические ограничения и собственные заголовки.\n\n'
            '3. Скопируйте CNAME раздачи в поле ниже и нажмите финальную проверку. '
            'Применение настроек Яндекса может занять время. До успешной проверки CDN-хост выключен.')
    return {'origin_domain': origin, 'cdn_domain': cdn, 'transport_path': plan['path'],
            'certificate_name': 'spn-cdn-' + plan['code'] + '-tls', 'guide_text': text}


def finish_replacement(store, jobs, server_id, job_id, target, source):
    row = store.get_server(server_id)
    if row.get('operational_job_id') != job_id:
        raise ValueError('operation_owner_changed')
    updated = synchronize(store, row['state_dir'])
    if updated['current_ip'] != target or updated.get('pending_ip'):
        raise ValueError('replacement_not_committed')
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        value = json.loads(conn.execute('SELECT body FROM servers WHERE id=?', (server_id,)).fetchone()[0])
        job = json.loads(conn.execute('SELECT body FROM provisioning_jobs WHERE id=?', (job_id,)).fetchone()[0])
        if job.get('attempt_id') != jobs.attempt_id:
            raise ValueError('operation_owner_changed')
        if value.get('operational_job_id') != job_id:
            raise ValueError('operation_owner_changed')
        if not any(item.get('job_id') == job_id for item in value.get('previous_vps', [])):
            value.setdefault('previous_vps', []).append({'job_id': job_id, **source, 'replaced_at': time.time()})
        # The old VPS's IPs must never become candidates on the new machine.
        value.update(reserves=[], operational_job_id=None, auto_enabled=False, status='unknown',
                     last_error=None, last_error_code=None, last_error_log_id=None, failures=0)
        job.update(status='succeeded', stage='complete', progress=100, updated_at=time.time(),
                   stage_label='VPS заменена. Домен и подписка сохранены.', result_server_id=server_id,
                   error=None, error_code=None)
        conn.execute('UPDATE servers SET body=? WHERE id=?', (json.dumps(value, ensure_ascii=False), server_id))
        conn.execute('UPDATE provisioning_jobs SET body=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job_id))
    store.event(server_id, 'vps_replaced', 'VPS заменена; домен и подписка сохранены. Добавьте резервные IP новой VPS.')


def finish_creation(store, jobs, server_id, job_id, target):
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        value = json.loads(conn.execute('SELECT body FROM servers WHERE id=?', (server_id,)).fetchone()[0])
        job = json.loads(conn.execute('SELECT body FROM provisioning_jobs WHERE id=?', (job_id,)).fetchone()[0])
        if (job.get('attempt_id') != jobs.attempt_id or value.get('operational_job_id') != job_id
                or value['current_ip'] != target or value.get('pending_ip')):
            raise ValueError('operation_owner_changed')
        value.update(operational_job_id=None, auto_enabled=False)
        job.update(status='succeeded', stage='complete', progress=100, updated_at=time.time(),
                   stage_label='Сервер создан и проверен', result_server_id=server_id, error=None, error_code=None)
        conn.execute('UPDATE servers SET body=? WHERE id=?', (json.dumps(value, ensure_ascii=False), server_id))
        conn.execute('UPDATE provisioning_jobs SET body=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job_id))


def run(args, password):
    store = Store(args.data_dir)
    jobs = JobStore(store, attempt_id=args.attempt_id)
    job = jobs.get(args.job_id)
    if job['status'] != 'queued' or job.get('attempt_id') != args.attempt_id:
        raise ValueError('job_not_queued')
    p = job['parameters']
    directory = private_directory(jobs.root / job['id'])
    state_dir = Path(p['state_dir']) if job['kind'] == 'replace' else private_directory(directory / 'installation')
    trust_dir = private_directory(directory / 'trust')
    identity = Path.home() / '.ssh/id_ed25519'
    subscription = store.root.parent / 'config/vpn-test-link.txt'
    if not subscription.exists():
        subscription = REPO_ROOT / '.vpn-test-link.txt'
    log_id = __import__('uuid').uuid4().hex
    jobs.update(job['id'], status='running', stage='ssh', stage_label='Подключаемся к VPS', progress=5,
                log_id=log_id, error=None, error_code=None)
    with store.server_lock('provisioning-global'), store.server_lock(p['server_id'] or p['domain']):
        d = replacement_module().d
        trust_store = d.u.Store(trust_dir)
        known = d.trust.resolve(p['ip'], p['ssh_port'], trust_store,
                                expected=p['host_key_sha256'], interactive=False)
        with PasswordMaster(p['ip'], p['ssh_port'], known, identity, password) as master:
            password = ''
            job['data_dir'] = str(store.root)
            command = command_for(job, state_dir, args.env_file, subscription, master.control)
            code, tail = execute(command, jobs, job['id'], jobs.logs / (log_id + '.log'))
            if code:
                failure = classify_switch_failure(tail, log_id)
                jobs.update(job['id'], status='failed', error=failure.public_message,
                            error_code=failure.code, stage_label='Установка остановлена. Можно продолжить.')
                return
            install_key(master, identity)
        if p['transport'] == 'cdn' and job['kind'] == 'create':
            info = cdn_public(state_dir)
            if not job.get('continuation', {}).get('finish'):
                jobs.update(job['id'], status='waiting_cdn', stage='cdn',
                            stage_label='VPS готова. Подключите сертификат и CDN Яндекса.', progress=55, cdn=info)
                return
            jobs.update(job['id'], cdn=info)
            synchronize(store, state_dir)
            finish_creation(store, jobs, p['domain'], job['id'], p['ip'])
            return
        elif job['kind'] == 'replace':
            finish_replacement(store, jobs, p['server_id'], job['id'], p['ip'], p['source_vps'])
            return
        else:
            synchronize(store, state_dir)
            finish_creation(store, jobs, p['domain'], job['id'], p['ip'])
            return
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--attempt-id', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        chunks, total, deadline = [], 0, time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([sys.stdin.buffer], [], [], max(0, remaining))[0]:
                raise ValueError('password_input_timeout')
            part = os.read(sys.stdin.fileno(), 8193 - total)
            if not part:
                break
            chunks.append(part); total += len(part)
            if total > 8192:
                break
        payload = b''.join(chunks)
        del chunks
        if len(payload) > 8192:
            raise ValueError('password_input_too_large')
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {'password'}:
            raise ValueError('invalid_password_input')
        password = validate_password(value['password'])
        value.clear(); del payload
        run(args, password)
    except Exception as error:
        jobs = JobStore(Store(args.data_dir), attempt_id=args.attempt_id)
        message = ('SSH-вход не подтверждён. Проверьте пароль root и продолжите задачу.'
                   if isinstance(error, SSHLoginError) else
                   'Установка остановилась. Сохранённый прогресс можно продолжить; при повторе укажите пароль VPS.')
        jobs.update(args.job_id, status='needs_credentials' if isinstance(error, SSHLoginError) else 'failed',
                    error=message, error_code='ssh_login' if isinstance(error, SSHLoginError) else 'installation_failed',
                    stage_label='Ожидает продолжения')
        # Only a class name is diagnostic; values may contain provider secrets.
        print('Installation worker stopped:', type(error).__name__, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
