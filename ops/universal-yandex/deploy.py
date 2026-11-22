#!/usr/bin/env python3
"""Prepare one Yandex XHTTP origin, then guide manual CDN creation.

Default is an offline plan. --apply never buys CDN, changes a Yandex account,
deletes an old VPN or enables a host before authenticated CDN verification.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time
import uuid


HERE = Path(__file__).resolve().parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


r = module('yandex_existing_reality', HERE.parent / 'universal-reality/deploy.py')
c = module('yandex_transport_config', HERE / 'config.py')
records = module('yandex_records', HERE / 'dns_records.py')
guide = module('yandex_manual_guide', HERE / 'guide.py')
probe = module('yandex_vpn_probe', HERE / 'probe.py')
bindings = module('yandex_origin_bindings', HERE / 'origin_bindings.py')
u = r.u


def validate(args):
    args.ip = r.public_ipv4(args.ip)
    u.need(1 <= args.ssh_port <= 65535 and args.ssh_port not in (80, 443, 2222, 10050, 10085), 'Недопустимый SSH-порт.')
    u.need(args.name is None or (1 <= len(args.name) <= 60 and all(ord(x) >= 32 and ord(x) != 127 for x in args.name)),
           'Недопустимое название.')
    u.need(args.country is None or re.fullmatch('[A-Z]{2}', args.country), 'Нужен код страны, например DE.')
    u.need(args.keep_tcp_port in (None, [10050]), 'Явно сохранить можно только TCP 10050, без повторов.')
    u.need(5 <= args.wait <= 600, '--wait: от 5 до 600 секунд.')
    try:
        args.dns_zone = r.dns.normalize_name(args.dns_zone)
        for key in ('domain', 'cdn_domain'):
            value = getattr(args, key)
            if value is not None:
                value = r.dns.normalize_name(value)
                u.need(value != args.dns_zone and value.endswith('.' + args.dns_zone), 'Домен должен быть отдельным именем в DNS-зоне.')
                setattr(args, key, value)
        if args.cdn_target:
            args.cdn_target = records.target(args.cdn_target, 'cdn')
        if args.certificate_target:
            args.certificate_target = records.target(args.certificate_target, 'certificate')
    except ValueError:
        raise u.DeployError('Неверный домен или CNAME Яндекса; указывай имя без https:// и пути.') from None


def directory(base, args):
    return Path(args.state_dir).expanduser() if args.state_dir else \
        Path.home() / '.spn-yandex-deploy' / u.digest(base)[:12] / (args.ip + '-ssh' + str(args.ssh_port))


def prepare(api, store, args, base):
    saved = store.load('plan.json')
    if saved is not None:
        c.validate_plan(saved)
        u.need(saved['ip'] == args.ip and saved['sshPort'] == args.ssh_port and saved['panel'] == base,
               'Адрес VPS/панели отличается от сохранённой установки.')
        for key, field in (('name', 'name'), ('country', 'country'), ('domain', 'domain'), ('cdn_domain', 'cdnDomain')):
            supplied = getattr(args, key)
            u.need(supplied is None or supplied == saved[field], 'Параметры отличаются от сохранённых; используй исходную команду.')
        u.need(args.dns_zone == saved['dns']['zone'], 'DNS-зона отличается от сохранённой.')
        if args.squad:
            u.need(u.choose_squads(api, args.squad) == saved['squads'], 'Выбраны другие сквады.')
        source = store.load('source.json')
        c._source(source)
        return saved, source
    rows = {kind: api.rows(kind) for kind in ('config-profiles', 'nodes', 'hosts')}
    u.need(not any(row.get('address') == args.ip for row in rows['nodes']),
           'В панели уже есть нода с этим IP. Этот установщик не стирает и не переделывает существующие VPN.')
    code = r.suffix(rows)
    domain = args.domain or 'origin-yc-' + code + '.' + args.dns_zone
    cdn = args.cdn_domain or 'cdn-yc-' + code + '.' + args.dns_zone
    u.need(not any(row.get('address') == cdn and row.get('port') == 443 for row in rows['hosts']), 'CDN-домен занят другим хостом.')
    plan = {'format': 1, 'mode': 'yandex-cdn', 'id': uuid.uuid4().hex, 'code': code,
            'ip': args.ip, 'sshPort': args.ssh_port, 'panel': base, 'panelIp': r.panel_ipv4(base),
            'name': args.name or 'VPN', 'country': args.country or '', 'domain': domain, 'cdnDomain': cdn,
            'dns': {'provider': 'cloudflare', 'zone': args.dns_zone, 'ttl': 60},
            'path': '/assets/edge-' + uuid.uuid4().hex[:16] + '/', 'squads': u.choose_squads(api, args.squad)}
    c.validate_plan(plan)
    return plan, c.extract_source(api)


def policy(store, args):
    saved = store.load('host-policy.json')
    value = {'version': 1, 'keepTcpPorts': args.keep_tcp_port or []} if args.keep_tcp_port is not None or saved is None else saved
    u.need(isinstance(value, dict) and type(value.get('version')) is int
           and value in ({'version': 1, 'keepTcpPorts': []}, {'version': 1, 'keepTcpPorts': [10050]})
           and (saved is None or saved == value), 'Политика сохраняемых портов отличается от исходной.')
    return value


class SSH(r.SSH):
    def __init__(self, plan, known=None, control_socket=None):
        super().__init__(plan, known, control_socket)
        self.environment = {key: value for key, value in self.environment.items()
                            if key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'SSH_AUTH_SOCK', 'TERM')}
        self.common += ['-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ClearAllForwardings=yes', '-o', 'SendEnv=-*']

    @property
    def stage(self):
        return '/root/spn-yandex-managed-' + self.plan['id']

    def __enter__(self):
        if self.external:
            return super().__enter__()
        try:
            result = subprocess.run(self.common + ['-M', '-N', '-f', '-o', 'ControlPersist=3600', 'root@' + self.plan['ip']],
                                    env=self.environment)
            u.need(result.returncode == 0, 'SSH не подключился. Изменённые ключи автоматически не удаляются.')
            return self
        except BaseException:
            self.tmp.cleanup(); raise

    def upload(self, files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            for name, content in files.items():
                u.need(re.fullmatch('[A-Za-z0-9_.-]+', name) and isinstance(content, bytes), 'Недопустимый файл установки.')
                item = tarfile.TarInfo(name); item.size, item.mode = len(content), 0o600
                archive.addfile(item, io.BytesIO(content))
        path = shlex.quote(self.stage)
        self.run(f'test ! -L {path} && install -d -m 700 {path} && test "$(stat -c %u:%a {path})" = 0:700 && '
                 f'tar -xmf - --no-same-owner -C {path}', output.getvalue())

    def action(self, action, capture=False):
        p = self.plan
        command = ['bash', self.stage + '/managed-origin.sh', action, p['domain'], p['ip'], p['panelIp'], str(p['sshPort']), p['id']]
        try:
            return self.run(shlex.join(command), capture=capture)
        except u.DeployError:
            raise u.DeployError('Origin: этап ' + action + ' остановлен; причина указана выше.') from None

    def source(self, action):
        state = self.stage + '/' + self.plan['id']
        self.run('install -d -m 700 ' + shlex.quote(state))
        self.run(shlex.join(['python3', self.stage + '/preferred_source.py', action, '--ip', self.plan['ip'], '--state-dir', state]))

    def origin_health(self):
        command = ['python3', self.stage + '/origin_health.py', '--domain', self.plan['domain'],
                   '--ip', self.plan['ip'], '--code', self.plan['code']]
        try:
            return json.loads(self.run(shlex.join(command), capture=True))
        except (u.DeployError, ValueError, TypeError):
            raise u.DeployError('Локальная HTTPS-проверка origin на VPS не прошла; причина выше. CDN ещё не проверялся.') from None


class Deployment:
    def __init__(self, api, store, plan, source):
        self.api, self.store, self.plan, self.source = api, store, plan, source
        self.helper = u.Deployment(api, store, plan, {})

    def ensure(self, key, kind, body, predicate):
        receipt = self.store.load(key + '.receipt.json')
        if receipt:
            if kind == 'hosts':
                row = u.one(self.api.rows(kind), receipt['uuid'])
                u.need(self.store.load(key + '.intent.json') == body
                       and u.same(row, {**body, 'isDisabled': row['isDisabled'], 'remark': row['remark']}),
                       'Технические параметры своего CDN-хоста изменены; не перезаписываю.')
                return row
            predicate = lambda row: row['uuid'] == receipt['uuid']
        return self.helper.ensure(key, kind, body, predicate)

    def provision(self):
        p = self.plan
        body = {'name': 'YANDEX_XHTTP_' + p['code'], 'config': c.profile(p, self.source)}
        profile = self.ensure('profile', 'config-profiles', body, lambda row: row['name'] == body['name'])
        inbounds = [row for row in profile['inbounds'] if row['tag'] == 'yandex-xhttp-' + p['code']]
        u.need(len(inbounds) == 1 and len(profile['inbounds']) == 1, 'Панель не подтвердила единственный XHTTP inbound.')
        inbound = inbounds[0]['uuid']
        node_body = {'name': 'yandex-' + p['code'], 'address': p['ip'], 'port': 2222,
                     'tags': ['AUTO_YANDEX_CDN'], 'note': 'Managed Yandex XHTTP origin: ' + p['domain'],
                     'configProfile': {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': [inbound]}}
        if p['country']:
            node_body['countryCode'] = p['country']
        node = self.ensure('node', 'nodes', node_body, lambda row: row['address'] == p['ip'] or row['name'] == node_body['name'])
        return profile, node, inbound

    def attach(self, inbound):
        for uid in self.plan['squads']:
            for _ in range(3):
                before = u.ids(u.one(self.api.rows('internal-squads'), uid)['inbounds'])
                if inbound in before:
                    break
                if before != u.ids(u.one(self.api.rows('internal-squads'), uid)['inbounds']):
                    continue
                body = {'uuid': uid, 'inbounds': before + [inbound]}
                self.store.save('squad-' + uid + '-' + u.digest(body)[:12] + '.intent.json', body)
                row = self.api('PATCH', '/internal-squads', body)
                u.need(set(u.ids(row['inbounds'])) == set(body['inbounds']), 'Добавление inbound в сквад не подтверждено.')
                break
            else:
                raise u.DeployError('Сквад меняется параллельно; повтори команду после редактирования.')
        actual = {row['uuid'] for row in self.api.rows('internal-squads') if inbound in u.ids(row['inbounds'])}
        u.need(actual == set(self.plan['squads']), 'Аудитория нового inbound отличается от выбранной.')

    def host(self, profile, node, inbound):
        body = c.host_body(self.plan, self.source, profile['uuid'], node['uuid'], inbound)
        return self.ensure('cdn', 'hosts', body, lambda row: (row.get('inbound') or {}).get('configProfileInboundUuid') == inbound)

    def owned_host(self, uid):
        intent, receipt = self.store.load('cdn.intent.json'), self.store.load('cdn.receipt.json')
        u.need(intent and receipt and receipt['uuid'] == uid, 'Нет квитанции собственного CDN-хоста.')
        row = u.one(self.api.rows('hosts'), uid)
        u.need(u.same(row, {**intent, 'isDisabled': row['isDisabled'], 'remark': row['remark']}),
               'Параметры CDN-хоста изменены во время проверки; активация/откат остановлены.')
        return row

    def finish(self, host, subscription, args):
        pending = self.store.load('activation.pending.json')
        current = self.owned_host(host['uuid'])
        if pending:
            u.need(set(pending) == {'host', 'code', 'beforeDisabled'} and pending['host'] == host['uuid']
                   and pending['code'] == self.plan['code'] and type(pending['beforeDisabled']) is bool,
                   'Незавершённая активация относится к другой операции.')
            # A lost enable/rollback reply must not leave a never-verified host
            # published while a retry runs its traffic test.
            if pending['beforeDisabled'] and not current['isDisabled']:
                self.api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': True})
                current = self.owned_host(host['uuid'])
                u.need(current['isDisabled'], 'Исходное выключенное состояние не восстановлено.')
        proof = probe.run(self.plan, self.source, subscription, engine=args.engine, interface=args.interface, route=args.test_route)
        self.store.save('vpn-test-' + str(time.time_ns()) + '.json', proof)
        u.need(proof.get('passed') is True and proof.get('tests') == 9 and proof.get('domain') == self.plan['cdnDomain']
               and proof.get('expectedExitIp') == self.plan['ip'] and proof.get('transport') == 'xhttp-get-header'
               and (args.test_route != 'physical' or proof.get('directReachabilityVerified') is True),
               'Полный VPN-тест через CDN не прошёл 9/9; новый хост не включаю.')
        complete = self.store.load('activation.verified.json')
        current = self.owned_host(host['uuid'])
        if complete and current['isDisabled'] and not pending:
            return False
        before_disabled = pending['beforeDisabled'] if pending else current['isDisabled']
        marker = pending or {'host': host['uuid'], 'code': self.plan['code'], 'beforeDisabled': before_disabled}
        self.store.save('activation.pending.json', marker)
        try:
            if current['isDisabled']:
                self.api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': False})
            u.need(self.owned_host(host['uuid'])['isDisabled'] is False, 'Панель не подтвердила включение CDN-хоста.')
            for attempt in range(3):
                try:
                    exported = probe.verify_export(self.plan, self.source, subscription, interface=args.interface)
                    u.need(exported.get('passed') is True, 'Выгрузка CDN-хоста не подтверждена.')
                    break
                except probe.ProbeError:
                    if attempt == 2:
                        raise
                    time.sleep(1)
            self.store.save('export-test-' + str(time.time_ns()) + '.json', exported)
            self.store.save('activation.verified.json', {'host': host['uuid'], 'code': self.plan['code']})
            u.need(self.store.load('activation.pending.json') == marker, 'Маркер активации изменился.')
            (self.store.path / 'activation.pending.json').unlink()
        except BaseException:
            observed = self.owned_host(host['uuid'])
            if before_disabled and not observed['isDisabled']:
                self.api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': True})
            raise
        return True


def show_guide(store, plan, args):
    fields = ['python3', str(HERE / 'deploy.py'), '--ip', plan['ip'],
              '--dns-zone', plan['dns']['zone'], '--panel-url', plan['panel'],
              '--env-file', str(Path(args.env_file).expanduser().absolute())]
    if args.cloudflare_token_file:
        fields += ['--cloudflare-token-file', str(Path(args.cloudflare_token_file).expanduser().absolute())]
    if args.engine:
        fields += ['--engine', str(Path(args.engine).expanduser().absolute())]
    fields += ['--interface', args.interface, '--test-route', args.test_route]
    resume = shlex.join(fields)
    if plan['sshPort'] != 22:
        resume += ' --ssh-port ' + str(plan['sshPort'])
    resume += ' --state-dir ' + shlex.quote(str(store.path))
    text = guide.render(plan, resume)
    filename = store.path / ('CDN-SETUP-' + u.digest(text)[:12] + '.md')
    if not filename.exists():
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
    print(text)
    print('\nЭта инструкция сохранена: ' + str(filename))


def dns_context(store, plan, args, settings):
    client = r.dns.CloudflareDNS(r.cloudflare_token(args, settings))
    zone = client.find_zone(plan['dns']['zone'])
    receipt = bindings.local_receipt(store, plan, c._nginx(plan).encode('ascii'))
    if receipt:
        bindings.check_dns(client, zone, store, plan, receipt)
    else:
        client.check_record(store, zone_id=zone['id'], name=plan['domain'], ipv4=plan['ip'], ttl=60)
    manager = records.Records(client, zone)
    manager.check_available(store, plan['cdnDomain'], 'cdn')
    manager.check_available(store, '_acme-challenge.' + plan['cdnDomain'], 'certificate')
    for kind, supplied, name in (('cdn', args.cdn_target, plan['cdnDomain']),
                                 ('certificate', args.certificate_target, '_acme-challenge.' + plan['cdnDomain'])):
        if supplied:
            manager.check(store, name, supplied, kind)
    return client, zone, manager


def ensure_manual_dns(store, plan, args, context):
    manager = context[2]
    for kind, supplied, name in (('certificate', args.certificate_target, '_acme-challenge.' + plan['cdnDomain']),
                                  ('cdn', args.cdn_target, plan['cdnDomain'])):
        intent = store.load('dns-' + kind + '.intent.json')
        chosen = supplied or (intent['content'] if intent else None)
        if chosen:
            manager.ensure(store, name, chosen, kind)


def run(args):
    validate(args)
    if not args.apply:
        print(json.dumps({'planOnly': True, 'changes': False, 'ip': args.ip,
                          'originDomain': args.domain or 'origin-yc-<random6>.' + args.dns_zone,
                          'cdnDomain': args.cdn_domain or 'cdn-yc-<random6>.' + args.dns_zone,
                          'transport': 'XHTTP packet-up GET/header', 'buysCDN': False,
                          'manualCDN': True, 'finishRequested': args.finish}, ensure_ascii=False))
        return
    settings = u.read_settings(Path(args.env_file).expanduser())
    base = u.panel_url(args.panel_url or settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    token = settings.get('REMNAWAVE_API_TOKEN') if settings.get('REMNAWAVE_BASE_URL', '').rstrip('/') == base else None
    api = r.API(base, token or getpass.getpass('API-токен Remnawave (на VPS не передаётся): '))
    store = u.Store(directory(base, args))
    with store.lock():
        plan, source = prepare(api, store, args, base)
        ports = policy(store, args)
        context = dns_context(store, plan, args, settings)
        ready = store.load('origin.ready.json')
        if ready and not args.finish and not args.recheck_origin:
            ensure_manual_dns(store, plan, args, context)
            show_guide(store, plan, args)
            return
        u.need(ready or args.agree_le_tos,
               'Для первого запуска добавь --agree-le-tos: нужен бесплатный TLS-сертификат origin с условиями Let’s Encrypt.')
        if args.finish:
            u.need(ready is not None, 'Сначала запусти подготовку origin без --finish.')
            for kind in ('certificate', 'cdn'):
                supplied = args.certificate_target if kind == 'certificate' else args.cdn_target
                u.need(supplied or store.load('dns-' + kind + '.receipt.json'),
                       'Сначала передай обе CNAME-цели из консоли Яндекса.')
        subscription = None
        if args.finish:
            subscription = r.probe.private_file(Path(args.test_subscription).expanduser())
            r.probe.subscription_url(subscription)
            r.probe.select_engine(args.engine)
        known = r.trust.resolve(plan['ip'], plan['sshPort'], u.Store(store.path / 'ssh'), expected=args.host_key_sha256)
        deployment = Deployment(api, store, plan, source)
        print('Yandex CDN ' + plan['code'] + ': origin ' + plan['domain'] + ', CDN ' + plan['cdnDomain'] + '.', flush=True)
        with SSH(plan, known, control_socket=args.ssh_control) as ssh:
            receipt = bindings.local_receipt(store, plan, c._nginx(plan).encode('ascii'))
            binding_files = {'origin_bindings.py': (HERE / 'origin_bindings.py').read_bytes()} if receipt else {}
            ssh.upload({'managed-origin.sh': (HERE / 'managed-origin.sh').read_bytes(),
                        'origin_health.py': (HERE / 'origin_health.py').read_bytes(),
                        'preferred_source.py': (r.HERE / 'preferred_source.py').read_bytes(),
                        'host_checks.py': (HERE.parent / 'universal-cascade/host_checks.py').read_bytes(),
                        'host-policy.json': json.dumps(ports).encode(), **binding_files})
            ssh.action('preflight')
            # Package hooks may restart networking. Settle them before the
            # preferred-source helper records the route it owns.
            ssh.action('packages')
            store.save('plan.json', plan); store.save('source.json', source); store.save('host-policy.json', ports)
            if receipt:
                # The extra listener has already been proven on this same VPS.
                # Keep the original creation journal immutable and never change
                # the owner's current origin A record back to the initial IP.
                bindings.check_dns(context[0], context[1], store, plan, receipt)
            else:
                r.ensure_dns(store, plan, context[:2], args.wait)
            ssh.source('apply')
            profile, node, inbound = deployment.provision()
            secret = store.load('node-secret.json')
            if secret is None:
                secret = {'value': api('GET', '/keygen')['secretKey']}
                store.save('node-secret.json', secret)
            ssh.upload(c.role_files(plan, secret['value']))
            ssh.action('install')
            identity = json.loads(ssh.action('identity', capture=True))
            u.need(isinstance(identity, dict) and re.fullmatch('[0-9a-f]{32}', identity.get('machineId', ''))
                   and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', identity.get('sshFingerprint', '')),
                   'Идентичность установленной CDN VPS не подтверждена.')
            store.save('machine-' + u.digest(identity) + '.json', identity)
            deployment.helper.wait_ready(node, args.wait)
            deployment.attach(inbound)
            ssh.action('verify'); ssh.source('verify')
            # The origin can intentionally be inaccessible from the owner's RU
            # network. Verify its TLS and exact health identity on the assigned
            # VPS through authenticated SSH, not via a mandatory Mac->origin dial.
            origin_proof = ssh.origin_health()
            u.need(isinstance(origin_proof, dict) and origin_proof.get('passed') is True
                   and origin_proof.get('scope') == 'origin-local-ssh' and origin_proof.get('tlsVerified') is True
                   and origin_proof.get('domain') == plan['domain'] and origin_proof.get('ip') == plan['ip']
                   and origin_proof.get('deployment') == plan['code'] and origin_proof.get('cdnVerified') is False,
                   'Локальная проверка origin вернула неподтверждённую идентичность; хост не готов.')
            store.save('origin-local-test-' + str(time.time_ns()) + '.json', origin_proof)
            host = deployment.host(profile, node, inbound)
            store.save('origin.ready.json', {'id': plan['id'], 'node': node['uuid'], 'profile': profile['uuid'],
                                            'inbound': inbound, 'host': host['uuid']})
            ensure_manual_dns(store, plan, args, context)
            if args.finish:
                enabled = deployment.finish(host, subscription, args)
                print(('Готово и включено: ' if enabled else 'Проверено; оставлено выключенным владельцем: ') + host['remark'])
                print('VPN через Яндекс CDN: 9/9. Доступность при белых списках требует отдельной проверки в сети оператора.')
                if args.test_route == 'system':
                    print('Проверка через текущую сеть Mac; прямой доступ к CDN без другого VPN не подтверждён.')
                return
        print('Origin и Remnawave готовы. CDN-хост пока выключен; платный ресурс Яндекса не создавался.')
        show_guide(store, plan, args)


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('--ip', required=True)
    value.add_argument('--domain', help='Origin hostname; DNS-only A -> VPS')
    value.add_argument('--cdn-domain', help='Separate CDN hostname; CNAME is added after manual CDN creation')
    value.add_argument('--dns-zone', default=r.DEFAULT_DNS_ZONE)
    value.add_argument('--name')
    value.add_argument('--country')
    value.add_argument('--squad', action='append', default=[])
    value.add_argument('--ssh-port', type=int, default=22)
    value.add_argument('--host-key-sha256')
    value.add_argument('--ssh-control', help=argparse.SUPPRESS)
    value.add_argument('--keep-tcp-port', action='append', type=int)
    value.add_argument('--agree-le-tos', action='store_true')
    value.add_argument('--cdn-target', help='Exact distribution CNAME target from Yandex')
    value.add_argument('--certificate-target', help='Exact DNS_CNAME target from Certificate Manager')
    value.add_argument('--finish', action='store_true', help='Test authenticated VPN through CDN and publish only after success')
    value.add_argument('--recheck-origin', action='store_true', help='Reconnect to an owned/reinstalled origin and resume VPS installation')
    value.add_argument('--panel-url')
    value.add_argument('--env-file', default=str(r.ROOT / '.env'))
    value.add_argument('--cloudflare-token-file')
    value.add_argument('--state-dir')
    value.add_argument('--wait', type=int, default=180)
    value.add_argument('--test-subscription', default=str(r.ROOT / '.vpn-test-link.txt'))
    value.add_argument('--engine')
    value.add_argument('--interface', default='en0')
    value.add_argument('--test-route', choices=('physical', 'system'), default='physical')
    value.add_argument('--apply', action='store_true')
    return value


if __name__ == '__main__':
    try:
        run(parser().parse_args())
    except (u.DeployError, c.ConfigError, r.dns.DNSProvisionError, records.dns.DNSProvisionError,
            r.trust.TrustError, probe.ProbeError, KeyboardInterrupt) as error:
        print('Остановлено: ' + ('прервано пользователем' if isinstance(error, KeyboardInterrupt) else str(error))
              + '. Сохранённые данные не удалены; CDN не покупался.', file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        print('Остановлено: ' + type(error).__name__ + '; приватные подробности скрыты. Работа CDN не подтверждена.', file=sys.stderr)
        raise SystemExit(2)
