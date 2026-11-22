"""Controller-only DNS-01 issuance for a clean TLS VPS replacement.

Cloudflare credentials never enter the certificate bundle or the guest. Certbot
uses an isolated account/lineage per replacement; the guest imports that account
with a standalone renewal configuration after cutover. Output is private.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('spn_tls_certificate_dns', HERE / 'cloudflare_dns.py')
dns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dns)
_spec = importlib.util.spec_from_file_location('spn_tls_certificate_store', HERE.parent / 'universal-direct' / 'deploy.py')
u = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(u)
SERVER = 'https://acme-v02.api.letsencrypt.org/directory'
CONTEXT = 'tls-acme-context.json'
BUNDLE = 'tls-certificate.bundle.json'
MAX_FILE = 128 * 1024


def require_tools():
    tools = {name: shutil.which(name) for name in ('certbot', 'openssl', 'dig')}
    u.need(all(tools.values()), 'Для переноса TLS нужны certbot, openssl и dig на контроллере; DNS/VPS не изменены.')
    return tools


def _read(path, root, *, private=False):
    path, root = Path(path), Path(root).resolve()
    resolved = path.resolve(strict=True)
    u.need(root in resolved.parents and not resolved.is_symlink(), 'Небезопасный файл TLS-сертификата.')
    info = resolved.stat()
    # Certbot stores its ACME account key read-only (0400). Both modes keep
    # private material owner-only; public certificates retain their own policy.
    allowed = (0o400, 0o600) if private else (0o600, 0o644)
    u.need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
           and stat.S_IMODE(info.st_mode) in allowed and 0 < info.st_size <= MAX_FILE,
           'Небезопасные права или размер TLS-сертификата.')
    return resolved.read_text()


def _validate(bundle, temporary):
    # Reuse the exact verifier which will also run on the trusted new guest.
    spec = importlib.util.spec_from_file_location('spn_tls_bundle_import', HERE / 'tls_certificate_import.py')
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    try:
        importer.validate_bundle(bundle, bundle.get('domain'))
        importer.validate_certificate(bundle['files'], bundle['domain'])
    except Exception:
        raise u.DeployError('TLS-сертификат, его домен, срок или приватный ключ не подтверждены; перенос остановлен.') from None


def _collect(config, domain):
    config = Path(config)
    renewal = _read(config / 'renewal' / (domain + '.conf'), config)
    account = re.findall(r'^account\s*=\s*([0-9a-f]{32})\s*$', renewal, re.MULTILINE)
    u.need(len(account) == 1, 'Certbot не сохранил однозначный ACME account; секреты скрыты.')
    account_dir = config / 'accounts' / 'acme-v02.api.letsencrypt.org' / 'directory' / account[0]
    files = {name: _read(config / 'live' / domain / name, config, private=name == 'privkey.pem')
             for name in ('cert.pem', 'chain.pem', 'fullchain.pem', 'privkey.pem')}
    files.update({name: _read(account_dir / name, config, private=name == 'private_key.json')
                  for name in ('meta.json', 'regr.json', 'private_key.json')})
    return {'format': 1, 'domain': domain, 'account_id': account[0], 'server': SERVER, 'files': files}


def _context(op):
    value = op.load(CONTEXT)
    intent = op.load('intent.json')
    u.need(isinstance(value, dict) and value.get('intentHash') == u.digest(intent)
           and value.get('domain') == intent.get('domain') and isinstance(value.get('zone'), dict)
           and isinstance(value.get('token'), str), 'Контекст DNS-01 не соответствует операции.')
    return value


def _challenge(op, context, validation):
    domain = os.environ.get('CERTBOT_DOMAIN', '')
    u.need(domain == context['domain'] and re.fullmatch(r'[A-Za-z0-9_-]{20,256}', validation or ''),
           'DNS-01 запрос не соответствует исходному TLS-домену.')
    name = '_acme-challenge.' + domain
    marker = 'SPN TLS replacement:' + context['intentHash']
    target = {'type': 'TXT', 'name': name, 'content': validation, 'ttl': 60, 'comment': marker}
    key = 'tls-dns-' + hashlib.sha256(validation.encode()).hexdigest()
    old = op.load(key + '.intent.json')
    u.need(old is None or old == target, 'Сохранённый DNS-01 запрос изменился.')
    return key, target


def _records(client, zone_id, target):
    rows = client._list('/zones/' + dns._identifier(zone_id) + '/dns_records',
                        {'type': 'TXT', 'name': target['name'], 'match': 'all'})
    return [row for row in rows if all(row.get(key) == value for key, value in target.items() if key != 'ttl')]


def _record(client, zone_id, target, receipt=None):
    rows = _records(client, zone_id, target)
    u.need(len(rows) <= 1, 'DNS-01 ownership неоднозначен; чужие TXT не удаляются.')
    if not rows:
        return None
    row = rows[0]
    dns._identifier(row.get('id'))
    u.need(receipt is None or receipt.get('id') == row['id'], 'Идентичность DNS-01 записи изменилась.')
    return row


def _wait_txt(zone, target, *, wait=180):
    servers = zone.get('name_servers')
    u.need(isinstance(servers, list) and servers and all(isinstance(s, str) and re.fullmatch(r'[a-z0-9.-]+', s)
                                                     for s in servers), 'Нет авторитетных DNS для DNS-01.')
    deadline = time.monotonic() + wait
    while True:
        passed = True
        for server in servers:
            try:
                result = subprocess.run(['dig', '@' + server, target['name'] + '.', 'TXT', '+norecurse',
                    '+time=2', '+tries=1', '+noall', '+comments', '+answer'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
                output = result.stdout.decode('ascii', errors='replace')
                flags = re.search(r';; flags: ([^;]+);', output)
                found = False
                for line in output.splitlines():
                    fields = line.split()
                    if (len(fields) == 5 and fields[0].rstrip('.').lower() == target['name']
                            and fields[2:4] == ['IN', 'TXT'] and fields[4] == '"' + target['content'] + '"'):
                        found = True
                passed &= result.returncode == 0 and 'status: NOERROR' in output and bool(flags and 'aa' in flags[1].split()) and found
            except (OSError, subprocess.TimeoutExpired):
                passed = False
        if passed:
            return
        u.need(time.monotonic() < deadline, 'Авторитетные DNS не подтвердили TXT для TLS; A-запись не изменена.')
        time.sleep(2)


def hook(action, path):
    op = u.Store(path)
    context = _context(op)
    key, target = _challenge(op, context, os.environ.get('CERTBOT_VALIDATION', ''))
    client = dns.CloudflareDNS(context['token'])
    zone_id = context['zone']['id']
    receipt = op.load(key + '.receipt.json')
    if action == 'auth':
        op.save(key + '.intent.json', target)
        if op.load(key + '.cleaned.json'):
            raise u.DeployError('Этот DNS-01 запрос уже завершён; повторная запись не создаётся.')
        pending = op.load(key + '.pending.json')
        if pending is None:
            op.save(key + '.pending.json', {'intentHash': u.digest(target)})
            try:
                client._request('POST', '/zones/' + dns._identifier(zone_id) + '/dns_records', body=target)
            except dns.DNSOutcomeUnknown:
                # Reconcile the ambiguous POST once by exact owner/content. No retry POST.
                u.need(_record(client, zone_id, target) is not None,
                       'Результат создания DNS-01 неизвестен; сохранённый запрос не отправляется повторно.')
        row = _record(client, zone_id, target, receipt)
        u.need(row is not None, 'Сохранённый DNS-01 запрос не найден; повторный POST запрещён.')
        op.save(key + '.receipt.json', {'id': row['id'], 'intentHash': u.digest(target)})
        _wait_txt(context['zone'], target)
    elif action == 'cleanup':
        # A failed auth may have no intent at all. Never delete by name alone.
        if op.load(key + '.intent.json') is None:
            return
        row = _record(client, zone_id, target, receipt)
        if row:
            client._request('DELETE', '/zones/' + dns._identifier(zone_id) + '/dns_records/' + row['id'])
        u.need(_record(client, zone_id, target, receipt) is None, 'Удаление собственной DNS-01 записи не подтверждено.')
        op.save(key + '.cleaned.json', {'intentHash': u.digest(target)})
    else:
        raise u.DeployError('Неизвестный DNS-01 hook.')


def _cleanup_owned(op, client, zone):
    """Reconcile failed hooks too, touching only this operation's exact TXT."""
    for path in sorted(op.path.glob('tls-dns-*.intent.json')):
        u.need(re.fullmatch(r'tls-dns-[0-9a-f]{64}\.intent\.json', path.name), 'Некорректный журнал DNS-01.')
        key = path.name.removesuffix('.intent.json')
        target = op.load(path.name)
        intent = op.load('intent.json')
        u.need(isinstance(target, dict) and target.get('name') == '_acme-challenge.' + intent['domain']
               and target.get('type') == 'TXT' and target.get('comment') == 'SPN TLS replacement:' + u.digest(intent),
               'DNS-01 журнал не принадлежит этой операции.')
        receipt = op.load(key + '.receipt.json')
        row = _record(client, zone['id'], target, receipt)
        if row:
            client._request('DELETE', '/zones/' + dns._identifier(zone['id']) + '/dns_records/' + row['id'])
        u.need(_record(client, zone['id'], target, receipt) is None, 'Очистка собственной DNS-01 записи не подтверждена.')
        op.save(key + '.cleaned.json', {'intentHash': u.digest(target)})


def issue(op, plan, token, zone):
    """Issue before touching the target VPS or its A record; safe to resume."""
    tools = require_tools()
    saved = op.load(BUNDLE)
    if saved:
        try:
            _validate(saved, op.path)
            _cleanup_owned(op, dns.CloudflareDNS(token), zone)
            op.save('tls-certificate.verified.json', {'domain': plan['domain'], 'bundleHash': u.digest(saved), 'method': 'dns-01'})
        finally:
            old = op.load(CONTEXT)
            if old is not None:
                u.need(old.get('intentHash') == u.digest(op.load('intent.json')),
                       'Чужой контекст DNS-01; секреты не перезаписываются.')
                (op.path / CONTEXT).unlink()
        return saved
    directory = u.Store(op.path / 'tls-acme')
    for name in ('config', 'work', 'logs'):
        u.Store(directory.path / name)
    context = {'domain': plan['domain'], 'token': token, 'zone': zone,
               'intentHash': u.digest(op.load('intent.json'))}
    # A terminated certbot can leave this private, short-lived context behind.
    old = op.load(CONTEXT)
    if old is not None:
        u.need(old.get('intentHash') == context['intentHash'], 'Чужой контекст DNS-01; секреты не перезаписываются.')
        (op.path / CONTEXT).unlink()
    op.save(CONTEXT, context)
    auth = shlex.join([sys.executable, str(Path(__file__).resolve()), 'hook', 'auth', str(op.path)])
    cleanup = shlex.join([sys.executable, str(Path(__file__).resolve()), 'hook', 'cleanup', str(op.path)])
    command = [tools['certbot'], 'certonly', '--manual', '--preferred-challenges', 'dns',
        '--manual-auth-hook', auth, '--manual-cleanup-hook', cleanup, '--non-interactive', '--agree-tos',
        '--key-type', 'ecdsa', '--elliptic-curve', 'secp256r1',
        '--register-unsafely-without-email', '--server', SERVER, '--cert-name', plan['domain'], '-d', plan['domain'],
        '--config-dir', str(directory.path / 'config'), '--work-dir', str(directory.path / 'work'),
        '--logs-dir', str(directory.path / 'logs'), '--keep-until-expiring']
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        log_path = directory.path / 'certbot-output.log'
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'ab') as log:
            info = os.fstat(log.fileno())
            u.need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                   and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'Небезопасный журнал TLS.')
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           start_new_session=True)
                try:
                    returncode = process.wait(timeout=600)
                except BaseException:
                    # Certbot launches hook subprocesses. Kill their whole new
                    # group before reconciling TXT or removing its context.
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise
            except (OSError, subprocess.TimeoutExpired):
                raise u.DeployError('Выпуск TLS через DNS-01 не завершён; A-запись не изменена. Повторите ту же операцию.') from None
        u.need(returncode == 0, 'Выпуск TLS через DNS-01 не подтверждён; A-запись не изменена. Подробности в закрытом журнале операции.')
        bundle = _collect(directory.path / 'config', plan['domain'])
        _validate(bundle, op.path)
        op.save(BUNDLE, bundle)
        op.save('tls-certificate.verified.json', {'domain': plan['domain'], 'bundleHash': u.digest(bundle), 'method': 'dns-01'})
        return bundle
    finally:
        try:
            _cleanup_owned(op, dns.CloudflareDNS(token), zone)
        finally:
            try:
                (op.path / CONTEXT).unlink(missing_ok=True)
            finally:
                signal.signal(signal.SIGTERM, previous_term)


if __name__ == '__main__':
    try:
        u.need(len(sys.argv) == 4 and sys.argv[1] == 'hook', 'Неверный вызов DNS-01 hook.')
        hook(sys.argv[2], sys.argv[3])
    except Exception:
        # Certbot owns the private detailed log. Never print the token/challenge.
        print('DNS-01 hook не завершён; чужие записи не изменяются.', file=sys.stderr)
        raise SystemExit(2)
