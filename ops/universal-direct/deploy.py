#!/usr/bin/env python3
"""One-command deployment on a clean VPS and Remnawave. Default: offline plan.

No payments, DNS changes, subscriber creation, deletion or API-token upload.
Only fresh owned objects and additive squad memberships may be changed.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import getpass
import hashlib
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
IMAGE = 'remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e'
API_PATHS = {'/config-profiles', '/nodes', '/hosts', '/internal-squads', '/keygen'}
# Only this reviewed transition changes read-only UFW verification, not any
# install/profile/firewall operation. All other template changes still stop.
READ_ONLY_VERIFIER_UPGRADES = {
    ('9791f038a258b4635716599b1f672193dc2a1999a5afc65bd304f32ad0516f5d',
     'ecdb5b8420a63b97514e59c3450c3364eabfe13e87a39274371c89f56d3af539')
}


class DeployError(RuntimeError):
    pass


class PanelHTTPError(DeployError):
    def __init__(self, status, method, path):
        self.status, self.method, self.path = status, method, path
        hint = ' Конфликт имени профиля или тега инбаунда.' if status == 409 and path == '/config-profiles' else ''
        super().__init__(f'Панель: HTTP {status}, {method} {path}.{hint} '
                         'Тело ответа скрыто. Автоматического повтора нет; состояние сохранено.')


def need(condition, message):
    if not condition:
        raise DeployError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ids(rows):
    return [r['uuid'] if isinstance(r, dict) else r for r in rows]


def one(rows, uid):
    found = [r for r in rows if r.get('uuid') == uid]
    need(len(found) == 1, 'Объект исчез или его UUID неоднозначен; изменения остановлены.')
    return found[0]


def same(actual, expected):
    for key, value in expected.items():
        current = actual.get(key)
        if key == 'configProfile':
            if not current or current['activeConfigProfileUuid'] != value['activeConfigProfileUuid']:
                return False
            if sorted(ids(current['activeInbounds'])) != sorted(value['activeInbounds']):
                return False
        elif key == 'inbound':
            if not current or any(current.get(k) != v for k, v in value.items()):
                return False
        elif current != value:
            return False
    return True


def read_settings(path):
    """Parse only two literal dotenv values; never import bot configuration."""
    result = {}
    if not path.exists():
        return result
    need(not path.is_symlink() and path.is_file(), 'Небезопасный файл конфигурации панели.')
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key in {'REMNAWAVE_BASE_URL', 'REMNAWAVE_API_TOKEN'}:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key] = value
    return result


def panel_url(value):
    parsed = urlsplit(value.rstrip('/'))
    need(parsed.scheme == 'https' and parsed.hostname and parsed.path == '/api'
         and parsed.port in (None, 443) and not parsed.username and not parsed.password
         and not parsed.query and not parsed.fragment, 'Панель должна иметь адрес https://ДОМЕН/api.')
    return value.rstrip('/')


def tls_context():
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise DeployError('Панель вернула редирект; токен не перенаправляется.')


class API:
    def __init__(self, base, token):
        self.base = panel_url(base)
        need(token and not any(c.isspace() for c in token), 'Нужен API-токен панели.')
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context()))

    def __call__(self, method, path, body=None):
        need(path in API_PATHS and method in {'GET', 'POST', 'PATCH'}, 'Запрещённая операция API.')
        data = None if body is None else json.dumps(body).encode()
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=25) as response:
                raw = response.read(8_000_001)
                need(len(raw) <= 8_000_000, 'Слишком большой ответ API.')
                return json.loads(raw)['response']
        except urllib.error.HTTPError as error:
            raise PanelHTTPError(error.code, method, path) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            raise DeployError('Ответ панели потерян или некорректен. POST автоматически не повторяется; запустите ту же команду снова.') from None

    def rows(self, kind):
        result = self('GET', '/' + kind)
        return result[{'config-profiles': 'configProfiles', 'internal-squads': 'internalSquads'}[kind]] \
            if kind in {'config-profiles', 'internal-squads'} else result


class Store:
    def __init__(self, directory):
        self.path = Path(directory).expanduser().absolute()
        for parent in (self.path, *self.path.parents):
            need(not parent.is_symlink(), 'Каталог состояния не должен быть симлинком.')
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.stat()
        need(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'Каталог состояния должен принадлежать вам и иметь права 0700.')

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.path / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            need(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                 'Небезопасный файл блокировки.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeployError('Это развёртывание уже выполняется.') from None
            yield
        finally:
            os.close(fd)

    def load(self, name, default=None):
        path = self.path / name
        if not path.exists():
            return default
        need(not path.is_symlink(), 'Небезопасный файл состояния.')
        info = path.stat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'Небезопасные права файла состояния.')
        return json.loads(path.read_text())

    def save(self, name, value):
        old = self.load(name)
        if old is not None:
            need(old == value, 'Сохранённый план отличается; чужие изменения не перезаписываются.')
            return
        data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        fd, tmp = tempfile.mkstemp(prefix='.write-', dir=self.path)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path / name)
            parent = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def save_plan(store, proposed):
    saved = store.load('plan.json')
    if saved and saved.get('templateHash') != proposed['templateHash']:
        transition = (saved.get('templateHash'), proposed['templateHash'])
        compatible = {**proposed, 'templateHash': saved.get('templateHash')}
        need(transition in READ_ONLY_VERIFIER_UPGRADES and saved == compatible,
             'Сохранённый план или установочный шаблон отличаются; изменения остановлены.')
        store.save('verification-upgrade-' + proposed['templateHash'] + '.json', {
            'from': transition[0], 'to': transition[1], 'kind': 'read-only-ufw-verification'})
        # Preserve the original plan, including its original installation hash.
        proposed = compatible
    store.save('plan.json', proposed)
    return proposed


def default_state_directory(base, domain, ip, ssh_port, home=None):
    """One immutable operation per target, not one operation per hostname.

    Keep the old domain-only state readable/resumable for its exact original
    target. A different IP/SSH port gets a new private namespace automatically.
    Nothing from an earlier target is deleted or reused as proof of ownership.
    """
    home = Path.home() if home is None else Path(home)
    domain_store = Store(home / '.spn-direct-deploy' / digest(base)[:12] / domain)
    legacy = domain_store.load('plan.json')
    if legacy and legacy.get('ip') == ip and legacy.get('sshPort', 22) == ssh_port:
        return domain_store.path
    targets = Store(domain_store.path / 'targets')
    return targets.path / f'{ip}-ssh{ssh_port}'


def choose_squads(api, requested, saved=None):
    rows = api.rows('internal-squads')
    if saved is not None:
        need(set(saved) <= {r['uuid'] for r in rows}, 'Сохранённый сквад удалён.')
        return saved
    if not requested:
        need(sys.stdin.isatty(), 'Укажите --squad ИМЯ_ИЛИ_UUID; автоматического выбора всех сквадов нет.')
        print('В какие сквады добавить новые подключения?')
        for index, row in enumerate(rows, 1):
            print(f"  {index}. {row['name']}")
        answer = input('Номера через запятую: ').strip()
        need(bool(re.fullmatch(r'\d+(?:\s*,\s*\d+)*', answer)), 'Нужен явный выбор сквадов.')
        numbers = [int(n.strip()) for n in answer.split(',')]
        need(all(1 <= n <= len(rows) for n in numbers), 'Номер сквада вне списка.')
        requested = [rows[n - 1]['uuid'] for n in numbers]
    selected = []
    for value in requested:
        matches = [r for r in rows if r['uuid'] == value or r['name'] == value]
        need(len(matches) == 1, 'Имя сквада отсутствует или неоднозначно; используйте его UUID.')
        selected.append(matches[0]['uuid'])
    need(selected, 'Нужно выбрать хотя бы один сквад.')
    return sorted(set(selected))


class SSH:
    def __init__(self, ip, port):
        self.ip, self.port = ip, port
        self.tmp = tempfile.TemporaryDirectory(prefix='spn-auto-', dir='/private/tmp' if sys.platform == 'darwin' else None)
        self.control = str(Path(self.tmp.name) / 'ssh')
        self.common = ['ssh', '-p', str(port), '-S', self.control, '-o', 'StrictHostKeyChecking=ask',
                       '-o', 'ServerAliveInterval=15', '-o', 'ConnectTimeout=15']

    def __enter__(self):
        try:
            result = subprocess.run(self.common + ['-M', '-N', '-f', '-o', 'ControlPersist=600', 'root@' + self.ip])
            need(result.returncode == 0, 'SSH не подключился. Изменившийся ключ автоматически не удаляется.')
        except BaseException:
            self.tmp.cleanup()
            raise
        return self

    def __exit__(self, *args):
        subprocess.run(self.common + ['-O', 'exit', 'root@' + self.ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.tmp.cleanup()

    def run(self, command, data=None):
        result = subprocess.run(self.common + ['-o', 'BatchMode=yes', '-o', 'ControlMaster=no',
                                               'root@' + self.ip, command], input=data)
        need(result.returncode == 0, 'Шаг на VPS остановлен. Исправьте причину и повторите ту же команду; объекты панели сохраняются.')

    def upload(self, plan, files):
        stage = '/root/spn-direct-managed-' + plan['id']
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            for name, data in files.items():
                need(re.fullmatch(r'[a-zA-Z0-9_.-]+', name), 'Небезопасное имя файла.')
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o600
                archive.addfile(info, io.BytesIO(data))
        command = (f'test ! -L {shlex.quote(stage)} && install -d -m 700 {shlex.quote(stage)} && '
                   f'test "$(stat -c %u:%a {shlex.quote(stage)})" = 0:700 && '
                   f'tar -xmf - --no-same-owner -C {shlex.quote(stage)}')
        self.run(command, buffer.getvalue())

    def action(self, plan, name):
        fields = ['bash', '/root/spn-direct-managed-' + plan['id'] + '/managed-node.sh', name,
                  plan['domain'], plan['ip'], plan['panelIp'], str(plan['sshPort']), plan['id']]
        self.run(shlex.join(fields))


def render_files(plan, node_secret):
    template = (HERE / 'spn-direct-setup.sh').read_text()
    blocks = re.findall(r'<<NGINX\n(.*?)\nNGINX', template, re.S)
    need(len(blocks) == 2, 'Исходный шаблон Nginx изменён; нужна проверка.')
    render = lambda raw: (raw.replace('$DOMAIN', plan['domain']).replace('\\$', '$') + '\n').encode()
    compose, = re.findall(r"<<'YAML'\n(.*?)\nYAML", template, re.S)
    compose = compose.replace('    network_mode: host', '    labels:\n      spn.deploy.id: "' + plan['id'] + '"\n    network_mode: host')
    hook, = re.findall(r"<<'HOOK'\n(.*?)\nHOOK", template, re.S)
    need(len(node_secret) >= 20 and re.fullmatch(r'[A-Za-z0-9_/+=-]+', node_secret), 'Некорректный служебный ключ ноды.')
    return {'managed-node.sh': (HERE / 'managed-node.sh').read_bytes(),
            'bootstrap.conf': render(blocks[0]), 'nginx.conf': render(blocks[1]),
            'docker-compose.yml': (compose + '\n').encode(), 'renew-nginx.sh': (hook + '\n').encode(),
            'node.env': ('NODE_PORT=2222\nSECRET_KEY=' + node_secret + '\n').encode()}


def verify_health(plan):
    class Origin(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((plan['ip'], 443), timeout=10)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=plan['domain'])
            except Exception:
                raw.close()
                raise
    conn = Origin(plan['domain'], context=tls_context(), timeout=10)
    try:
        conn.request('GET', '/health')
        response = conn.getresponse()
        need(response.status == 200 and json.loads(response.read(4096)) == {'status': 'ok'}, 'Публичная проверка HTTPS не прошла.')
    except (OSError, ValueError, http.client.HTTPException):
        raise DeployError('Публичная проверка TLS/health не прошла; хосты не включены.') from None
    finally:
        conn.close()


class Deployment:
    def __init__(self, api, store, plan, profile, recover_profile_conflict=False):
        self.api, self.store, self.plan, self.profile = api, store, plan, profile
        self.recover_profile_conflict = recover_profile_conflict

    def ensure(self, key, kind, payload, predicate):
        intent_name, receipt_name = key + '.intent.json', key + '.receipt.json'
        receipt = self.store.load(receipt_name)
        intent = self.store.load(intent_name)
        rows = self.api.rows(kind)
        # A host's endpoint is NOT ownership. Several independent aliases can
        # legitimately share an address/path. Pin a confirmed host to its UUID;
        # before a receipt exists use only our uniquely generated inbound UUID.
        matches = [r for r in rows if (r.get('uuid') == receipt['uuid'] if kind == 'hosts' and receipt else predicate(r))]
        need(len(matches) <= 1, f'Найдены несколько объектов для шага {key} ({kind}); '
             'требуется ручная сверка их привязки, автоматические изменения остановлены.')
        if matches:
            need(intent == payload, 'В панели уже есть такой объект, но он не принадлежит этому запуску.')
            row = matches[0]
            if receipt:
                need(row['uuid'] == receipt['uuid'], 'UUID созданного объекта изменён.')
            expected = dict(payload)
            if kind == 'hosts' and self.store.load(key + '.enable.intent.json'):
                expected['isDisabled'] = row['isDisabled']
            need(same(row, expected), 'Созданный объект был изменён вручную; изменения не перезаписываются.')
        else:
            need(receipt is None, 'Ранее созданный объект удалён. Восстановление требует отдельного решения.')
            need(intent is None, 'Исход предыдущего создания не подтверждён: объект пока не найден. '
                 'Повторный POST запрещён во избежание дубликатов; сохраните состояние для ручной сверки.')
            self.store.save(intent_name, payload)
            try:
                row = self.api('POST', '/' + kind, payload)
            except PanelHTTPError as error:
                # Record the status/operation, never a potentially secret body.
                self.store.save(key + '.rejected.json', {'status': error.status, 'method': error.method,
                                                         'path': error.path, 'payloadHash': digest(payload)})
                raise
            need(same(row, payload), 'Панель не подтвердила параметры созданного объекта.')
        self.store.save(receipt_name, {'uuid': row['uuid']})
        return row

    def _profile_request(self):
        p = self.plan
        profile_name = 'AUTO_' + p['domain'].replace('.', '_')[:16] + '_' + p['id'][:8]
        logical = {f'direct-{kind}': f'direct-{kind}-{p["id"][:16]}' for kind in ('grpc', 'ws')}
        scoped = copy.deepcopy(self.profile)
        need({row['tag'] for row in scoped['inbounds']} == set(logical), 'Неизвестные теги исходного шаблона.')
        for inbound in scoped['inbounds']:
            inbound['tag'] = logical[inbound['tag']]
        body = {'name': profile_name, 'config': scoped}
        legacy = {'name': profile_name, 'config': self.profile}
        intent = self.store.load('profile.intent.json')
        marker = {'version': 2, 'planId': p['id'], 'legacyIntentHash': digest(legacy),
                  'newPayloadHash': digest(body), 'name': profile_name}
        recovery = self.store.load('profile.recovery.json')
        if recovery is not None:
            need(recovery == marker and intent == legacy and self.store.load('profile.receipt.json') is None,
                 'Сохранённое восстановление профиля отличается; изменения остановлены.')
            return 'profile-v2', body, logical
        if intent == legacy:
            profiles = self.api.rows('config-profiles')
            if self.store.load('profile.receipt.json') or any(row['name'] == profile_name for row in profiles):
                # Do not rename already-created old inbounds, including a POST
                # whose response was lost. ensure() still checks ownership/data.
                return 'profile', legacy, {key: key for key in logical}
            need(self.recover_profile_conflict, 'Сохранён незавершённый запрос старого профиля. '
                 'Если предыдущий запуск завершился HTTP 409, добавьте --recover-profile-conflict. '
                 'Для таймаута или неизвестного исхода этот флаг не используйте.')
            downstream = ('node.intent.json', 'node.receipt.json', 'node-secret.json', 'complete.json',
                          'grpc.intent.json', 'grpc.receipt.json', 'ws.intent.json', 'ws.receipt.json')
            need(all(self.store.load(name) is None for name in downstream),
                 'Установка уже продвинулась дальше профиля; автоматическое восстановление запрещено.')
            need(not any(row['address'] == p['ip'] for row in self.api.rows('nodes')),
                 'В панели уже есть нода с целевым IP; восстановление остановлено.')
            occupied = {inbound['tag'] for row in profiles for inbound in row['inbounds']}
            need(set(logical) <= occupied, 'Конфликт обоих старых тегов не подтверждён; повторять создание небезопасно.')
            # Preserve the rejected/unknown original intent. A new durable
            # branch uses the SAME unique profile name: a late old create can
            # only conflict, never produce a second profile. No foreign edits.
            self.store.save('profile.recovery.json', marker)
            print('Восстановление после конфликта тегов: старый запрос сохранён, новые теги уникальны.')
            return 'profile-v2', body, logical
        need(intent is None or intent == body, 'Неизвестный сохранённый запрос профиля; изменения остановлены.')
        return 'profile', body, logical

    def provision(self):
        p = self.plan
        key, body, logical = self._profile_request()
        profile_name = body['name']
        profile = self.ensure(key, 'config-profiles', body,
                              lambda r: r['name'] == profile_name)
        actual = {r['tag']: r['uuid'] for r in profile['inbounds']}
        need(set(actual) == set(logical.values()), 'Панель не создала ожидаемые инбаунды.')
        inbounds = {key: actual[tag] for key, tag in logical.items()}
        payload = {'name': 'auto-' + p['id'][:12], 'address': p['ip'], 'port': 2222,
                   'tags': ['AUTO_DIRECT'], 'note': 'Managed direct TLS node: ' + p['domain'],
                   'configProfile': {'activeConfigProfileUuid': profile['uuid'],
                                     'activeInbounds': [inbounds['direct-' + kind] for kind in ('grpc', 'ws')]}}
        if p['country']:
            payload['countryCode'] = p['country']
        node = self.ensure('node', 'nodes', payload,
                           lambda r: r['address'] == p['ip'] or r['name'] == payload['name'])
        return profile, node, inbounds

    def wait_ready(self, node, limit):
        deadline = time.monotonic() + limit
        while True:
            current = one(self.api.rows('nodes'), node['uuid'])
            if current.get('isConnected') is True and not current.get('lastStatusMessage') and not current.get('isDisabled'):
                return
            need(time.monotonic() < deadline, 'Нода пока не подключилась к панели. Проверьте порт 2222/firewall и повторите команду.')
            time.sleep(3)

    def attach(self, inbounds):
        for uid in self.plan['squads']:
            for attempt in range(3):
                members = ids(one(self.api.rows('internal-squads'), uid)['inbounds'])
                wanted = members + [inbounds['direct-' + kind] for kind in ('grpc', 'ws')
                                    if inbounds['direct-' + kind] not in members]
                if wanted == members:
                    break
                # Avoid overwriting a change observed between read and patch.
                latest = ids(one(self.api.rows('internal-squads'), uid)['inbounds'])
                if latest != members:
                    continue
                key = 'squad-' + uid + '-' + digest(wanted)[:12] + '.intent.json'
                body = {'uuid': uid, 'inbounds': wanted}
                self.store.save(key, body)
                result = self.api('PATCH', '/internal-squads', body)
                need(set(ids(result['inbounds'])) == set(wanted), 'Сквад не подтвердил добавление инбаундов.')
                break
            else:
                raise DeployError('Сквад редактируется параллельно. Повторите запуск после окончания правок.')
        rows = self.api.rows('internal-squads')
        for iid in inbounds.values():
            need({r['uuid'] for r in rows if iid in ids(r['inbounds'])} == set(self.plan['squads']), 'Аудитория инбаунда изменилась.')

    def hosts(self, profile, node, inbounds):
        result = {}
        for kind in ('grpc', 'ws'):
            body = {'remark': self.plan['name'] + (' • gRPC' if kind == 'grpc' else ' • WS'),
                    'address': self.plan['domain'], 'port': 443, 'path': 'spngrpc' if kind == 'grpc' else '/direct/ws/',
                    'sni': self.plan['domain'], 'host': self.plan['domain'],
                    'alpn': 'h2' if kind == 'grpc' else 'http/1.1', 'fingerprint': 'chrome', 'securityLayer': 'TLS',
                    'isDisabled': True, 'isHidden': False, 'internalSquads': {'mode': 'EXCLUDE', 'squads': []},
                    'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
                    'tags': ['AUTO_DIRECT'], 'nodes': [node['uuid']],
                    'inbound': {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbounds['direct-' + kind]}}
            result[kind] = self.ensure(kind, 'hosts', body, lambda r, k=kind:
                (r.get('inbound') or {}).get('configProfileInboundUuid') == inbounds['direct-' + k])
        return result

    def publish(self, hosts):
        for kind, host in hosts.items():
            body = {'uuid': host['uuid'], 'isDisabled': False}
            self.store.save(kind + '.enable.intent.json', body)
            current = one(self.api.rows('hosts'), host['uuid'])
            if current['isDisabled']:
                result = self.api('PATCH', '/hosts', body)
                need(result['isDisabled'] is False, 'Хост пока не включён; повторите ту же команду.')
        self.store.save('complete.json', {'hosts': {k: h['uuid'] for k, h in hosts.items()}})


def validate_args(args):
    try:
        ip = ipaddress.IPv4Address(args.ip)
    except ValueError:
        raise DeployError('Укажите публичный IPv4 новой VPS.') from None
    need(ip.is_global and not ip.is_multicast, 'Нужен публичный IPv4.')
    need(len(args.domain) <= 190 and all(len(label) <= 63 for label in args.domain.split('.'))
         and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+', args.domain), 'Некорректный домен.')
    need(1 <= args.ssh_port <= 65535 and args.ssh_port not in {80, 443, 2222, 10087, 10088}, 'Некорректный или конфликтующий SSH-порт.')
    need(not args.country or re.fullmatch('[A-Z]{2}', args.country), 'Страна: двухбуквенный код, например FI.')
    need(1 <= len(args.name or args.domain) <= 80 and not any(ord(c) < 32 for c in args.name or args.domain), 'Некорректное название.')
    need(5 <= args.wait <= 600, 'Ожидание ноды: от 5 до 600 секунд.')


def check_ipv6_resolution(domain, target_ip):
    """Ignore only the target's IPv4-mapped representation, not native IPv6.

    getaddrinfo is a system resolver, not an authoritative AAAA query. macOS
    can return ::ffff:IP even for an A-only name; that is the same IPv4 target.
    Other IPv6 (including unknown DNS64 prefixes) must still fail closed.
    """
    try:
        rows = socket.getaddrinfo(domain, 443, socket.AF_INET6, socket.SOCK_STREAM)
    except socket.gaierror as error:
        no_address = {socket.EAI_NONAME}
        if hasattr(socket, 'EAI_NODATA'):
            no_address.add(socket.EAI_NODATA)
        if error.errno in no_address:
            return
        raise DeployError('Не удалось проверить IPv6 домена: временная ошибка DNS. Повторите команду позже.') from None
    target = ipaddress.IPv4Address(target_ip)
    unexpected = sorted({str(address) for row in rows
                         if (address := ipaddress.IPv6Address(row[4][0])).ipv4_mapped != target})
    need(not unexpected, 'Системный DNS вернул несовместимые с IPv4-шаблоном адреса: '
         + ', '.join(unexpected) + '. Проверьте AAAA-запись или синтез IPv6 резолвером; установка остановлена.')


def run(args):
    validate_args(args)
    if not args.apply:
        print(json.dumps({'planOnly': True, 'ip': args.ip, 'domain': args.domain, 'sshPort': args.ssh_port,
                          'create': ['profile', 'node', 'grpc host', 'ws host'], 'server': 'clean Ubuntu24.04',
                          'dnsChanges': False, 'subscriberChanges': False}, ensure_ascii=False, indent=2))
        return
    need(args.agree_le_tos, 'Для выпуска сертификата добавьте --agree-le-tos: https://letsencrypt.org/repository/ .')
    settings = read_settings(Path(args.env_file).expanduser())
    base = panel_url(args.panel_url or settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    token = settings.get('REMNAWAVE_API_TOKEN') if settings.get('REMNAWAVE_BASE_URL', '').rstrip('/') == base else None
    if not token:
        token = getpass.getpass('API-токен панели (останется только на Mac): ')
    api = API(base, token)
    hostname = urlsplit(base).hostname
    try:
        panel_ips = {r[4][0] for r in socket.getaddrinfo(hostname, 443, socket.AF_INET, socket.SOCK_STREAM)}
        domain_ips = {r[4][0] for r in socket.getaddrinfo(args.domain, 443, socket.AF_INET, socket.SOCK_STREAM)}
    except OSError:
        raise DeployError('DNS не готов. Создайте DNS-only A-запись домена на IP новой VPS.') from None
    need(domain_ips == {args.ip}, 'A-запись домена должна указывать только на новый IP.')
    need(len(panel_ips) == 1 and ipaddress.IPv4Address(next(iter(panel_ips))).is_global, 'Панель должна иметь один прямой публичный IPv4, без CDN.')
    check_ipv6_resolution(args.domain, args.ip)
    profile = json.loads((HERE / 'profile.json').read_text())
    signature = digest({'profile': profile, 'installer': (HERE / 'spn-direct-setup.sh').read_text(),
                        'managed': (HERE / 'managed-node.sh').read_text()})
    state = Path(args.state_dir).expanduser() if args.state_dir else default_state_directory(
        base, args.domain, args.ip, args.ssh_port)
    store = Store(state)
    with store.lock():
        saved = store.load('plan.json')
        squads = choose_squads(api, args.squad, saved['squads'] if saved else None)
        if saved and args.squad:
            need(choose_squads(api, args.squad) == squads, 'Сквады отличаются от сохранённого плана.')
        plan = {'id': saved['id'] if saved else uuid.uuid4().hex, 'format': 1, 'ip': args.ip,
                'domain': args.domain, 'name': args.name or args.domain, 'country': args.country,
                'sshPort': args.ssh_port, 'panel': base, 'panelIp': next(iter(panel_ips)),
                'squads': squads, 'templateHash': signature}
        plan = save_plan(store, plan)
        deployment = Deployment(api, store, plan, profile, recover_profile_conflict=args.recover_profile_conflict)
        if store.load('complete.json') and not args.reinstall:
            p, n, inbound = deployment.provision()
            hosts = deployment.hosts(p, n, inbound)
            need(all(not h['isDisabled'] for h in hosts.values()), 'После завершения хост был выключен вручную; автоматически не включаю.')
            print('Это развёртывание уже завершено; дубликаты не созданы.')
            return
        print('Подключение к VPS. Новый SSH-ключ подтверждайте только после сверки; изменённые ключи не удаляются.')
        with SSH(args.ip, args.ssh_port) as ssh:
            ssh.upload(plan, {'managed-node.sh': (HERE / 'managed-node.sh').read_bytes()})
            ssh.action(plan, 'preflight')
            p, n, inbound = deployment.provision()
            secret = store.load('node-secret.json')
            if secret is None:
                secret = {'value': api('GET', '/keygen')['secretKey']}
                store.save('node-secret.json', secret)
            ssh.upload(plan, render_files(plan, secret['value']))
            ssh.action(plan, 'install')
            deployment.wait_ready(n, args.wait)
            ssh.action(plan, 'verify')
            deployment.attach(inbound)
            hosts = deployment.hosts(p, n, inbound)
            verify_health(plan)
            deployment.publish(hosts)
        print('Готово: ' + plan['name'] + ' • gRPC и • WS. Обновите подписку и проверьте сайты в приложении.')
        print('Проверены подключение ноды, слушатели, firewall и публичный TLS. Новые пользователи не создавались.')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ip', required=True)
    p.add_argument('--domain', required=True)
    p.add_argument('--name')
    p.add_argument('--country', default='')
    p.add_argument('--ssh-port', type=int, default=22)
    p.add_argument('--squad', action='append', default=[], help='Repeat a squad name or UUID; omitted: interactive choice')
    p.add_argument('--panel-url')
    p.add_argument('--env-file', default=str(ROOT / '.env'))
    p.add_argument('--state-dir')
    p.add_argument('--wait', type=int, default=120)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--agree-le-tos', action='store_true')
    p.add_argument('--recover-profile-conflict', action='store_true',
                   help='Recover a pending legacy profile only after a confirmed HTTP 409 tag conflict')
    p.add_argument('--reinstall', action='store_true',
                   help='Reinstall a clean VPS at the same IP, preserving owned panel objects and node key')
    return p


if __name__ == '__main__':
    try:
        run(parser().parse_args())
    except (DeployError, KeyboardInterrupt) as error:
        print('Остановлено: ' + (str(error) if isinstance(error, DeployError) else 'прервано пользователем; сохранённый прогресс оставлен.'), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        print('Остановлено: ' + type(error).__name__ + '; детали скрыты для защиты ключей. Повторите ту же команду после проверки.', file=sys.stderr)
        raise SystemExit(2)
