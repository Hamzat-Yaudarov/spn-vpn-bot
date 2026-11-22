#!/usr/bin/env python3
"""Install User -> RU TCP entry -> encrypted WireGuard -> EU REALITY exit.

Plan only unless --apply. Clean Ubuntu 24.04 guests or an owned shared RU are required. No VPS
purchases, OS reinstallation, CDN, global routing, NAT or old-node conversion.
The public subscription uses only the RU endpoint; the internet exit is EU.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import ipaddress
import importlib.util
import io
import json
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
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


r = module('spn_cascade_reality', HERE.parent / 'universal-reality' / 'deploy.py')
c = module('spn_cascade_config', HERE / 'config.py')
u = r.u


def validate(args):
    args.ru_ip, args.eu_ip = r.public_ipv4(args.ru_ip), r.public_ipv4(args.eu_ip)
    u.need(args.ru_ip != args.eu_ip, 'Нужны две разные VPS: вход RU и выход EU.')
    for port in (args.ru_ssh_port, args.eu_ssh_port):
        u.need(1 <= port <= 65535 and port not in (443, 2222, 2443), 'Недопустимый SSH-порт.')
    for port in (args.entry_port, args.ru_wg_port):
        u.need(port is None or ((port == 443 or 1024 <= port <= 65535)
               and port not in (2222, 2443, args.ru_ssh_port)), 'Недопустимый входной/UDP-порт RU.')
    eu_port = getattr(args, 'eu_wg_port', None)
    u.need(eu_port is None or ((eu_port == 443 or 1024 <= eu_port <= 65535)
           and eu_port not in (2222, 2443, args.eu_ssh_port)), 'Недопустимый UDP-порт EU.')
    for role in ('ru', 'eu'):
        ports = getattr(args, role + '_keep_tcp_port', None)
        u.need(ports is None or ports == [10050], 'Явно сохранить можно только TCP 10050, одним параметром.')
    u.need(args.name is None or (1 <= len(args.name) <= 60
           and all(ord(char) >= 32 and ord(char) != 127 for char in args.name)), 'Неверное название сервера.')
    u.need(args.country is None or re.fullmatch('[A-Z]{2}', args.country), 'Код страны выхода, например NL.')
    u.need(5 <= args.wait <= 600, '--wait: от 5 до 600 секунд.')
    try:
        zone = r.dns.normalize_name(args.dns_zone)
        domain = r.dns.normalize_name(args.domain) if args.domain else None
    except ValueError:
        raise u.DeployError('Нужен корректный домен, не IP.') from None
    u.need(domain is None or (domain != zone and domain.endswith('.' + zone)),
           'Укажите отдельный поддомен внутри --dns-zone.')
    args.dns_zone, args.domain = zone, domain


def directory(base, args):
    if args.state_dir:
        return Path(args.state_dir).expanduser()
    pair = args.ru_ip + '-ssh' + str(args.ru_ssh_port) + '__' + args.eu_ip + '-ssh' + str(args.eu_ssh_port)
    return Path.home() / '.spn-cascade-deploy' / u.digest(base)[:12] / pair


def local_branches(store, base, ru):
    """Only validated private local receipts prove legacy/shared DNS ownership."""
    result = []
    for path in sorted(store.path.parent.iterdir()):
        # Siblings also contain the per-RU lock tree and may contain unrelated
        # state. Do not try to adopt or chmod those as branch stores.
        plan_file = path / 'plan.json'
        if (path.is_symlink() or not path.is_dir() or plan_file.is_symlink()
                or not plan_file.is_file()):
            continue
        candidate = u.Store(path)
        plan = candidate.load('plan.json')
        if not isinstance(plan, dict) or plan.get('mode') != 'cascade-wg':
            continue
        c.validate_plan(plan)
        if plan.get('panel') == base and plan.get('ru') == ru:
            result.append((candidate, plan))
    return result


def host_policy(store, base, ru, args):
    """An explicit, immutable exception, separate from legacy VPN plan formats."""
    def checked(value):
        u.need(isinstance(value, dict) and set(value) == {'version', 'ru', 'eu'}
               and type(value['version']) is int and value['version'] == 1, 'Повреждена политика портов VPS.')
        for role in ('ru', 'eu'):
            item = value[role]
            u.need(isinstance(item, dict) and set(item) == {'version', 'keepTcpPorts'}
                   and type(item['version']) is int and item['version'] == 1
                   and isinstance(item['keepTcpPorts'], list)
                   and item['keepTcpPorts'] in ([], [10050])
                   and all(type(p) is int for p in item['keepTcpPorts']), 'Недопустимое исключение порта VPS.')
        return value

    saved = store.load('host-policy.json')
    if saved is not None:
        checked(saved)
    inherited = []
    for source, _ in local_branches(store, base, ru):
        previous = source.load('host-policy.json')
        if previous is not None:
            ports = checked(previous)['ru']['keepTcpPorts']
            if ports:
                u.need(not inherited or inherited == ports, 'Политики общего RU противоречат друг другу.')
                inherited = ports
    result = {'version': 1}
    for role in ('ru', 'eu'):
        requested = getattr(args, role + '_keep_tcp_port', None)
        default = saved[role]['keepTcpPorts'] if saved is not None else (inherited if role == 'ru' else [])
        ports = default if requested is None else sorted(set(requested))
        u.need(ports in ([], [10050]) and (requested is None or len(requested) == len(ports)),
               'Явно сохранить можно только TCP 10050, без повторов.')
        if role == 'ru' and inherited:
            u.need(ports == inherited, 'Общий RU уже сохраняет TCP 10050; исключение нельзя убрать новой веткой.')
        result[role] = {'version': 1, 'keepTcpPorts': ports}
    u.need(saved is None or saved == result, 'Исключения портов отличаются от сохранённых; повторите прежние параметры.')
    return result


def shared_dns(plan, branches):
    for source, saved in branches:
        if saved['domain'] != plan['domain'] or saved['dns'] != plan['dns']:
            continue
        intent, receipt = source.load('dns.intent.json'), source.load('dns.receipt.json')
        if (isinstance(intent, dict) and isinstance(receipt, dict)
                and intent.get('name') == plan['domain'] and intent.get('content') == plan['ru']['ip']
                and all(receipt.get(key) == value for key, value in intent.items() if key != 'version')):
            return {'dns.intent.json': intent, 'dns.receipt.json': receipt}
    return {}


def legacy_proof(branches):
    return {'branches': [{'id': p['id'], 'ruIP': p['ru']['ip'], 'euIP': p['eu']['ip'],
        'panelIP': p['panelIp'], 'sshPort': p['ru']['sshPort'], 'interface': p['link']['interface'],
        'localWG': p['link']['ru'], 'peerWG': p['link']['eu'], 'entryPort': c.entry_port(p),
        'ruWGPort': c.ru_wg_port(p), 'euWGPort': p['link']['port']} for _, p in branches]}


def allocate_ports(plan, inventory, args, *, saved):
    u.need(isinstance(inventory, list) and all(isinstance(row, dict) for row in inventory),
           'RU вернул некорректный список существующих каскадов.')
    others = [row for row in inventory if row.get('id') != plan['id']]
    used_tcp = {row.get('entryPort') for row in others}
    used_udp = {row.get('ruWGPort') for row in others}
    if not saved:
        choices = ([args.entry_port] if args.entry_port is not None else
                   [443, 8443, 2053, 2083, 2087, 2096, 9443, 10443] + list(range(12000, 13000)))
        chosen = next((port for port in choices if port not in used_tcp
                       and (args.ru_wg_port if args.ru_wg_port is not None else port) not in used_udp), None)
        u.need(chosen is not None, 'Входной или UDP-порт RU занят. Укажите другой --entry-port/--ru-wg-port.')
        plan['entryPort'] = chosen
        plan['link']['ruPort'] = args.ru_wg_port if args.ru_wg_port is not None else chosen
        used_ips = {row.get(key) for row in others for key in ('localWG', 'peerWG')}
        start = int(ipaddress.IPv4Address(plan['link']['ru'])) - 1 - int(c.LINK_POOL.network_address)
        for offset in range(16384):
            base = int(c.LINK_POOL.network_address) + ((start + offset * 4) % 65536)
            ru, eu = str(ipaddress.IPv4Address(base + 1)), str(ipaddress.IPv4Address(base + 2))
            if ru not in used_ips and eu not in used_ips:
                plan['link'].update(ru=ru, eu=eu)
                break
        else:
            raise u.DeployError('Внутренние адреса WireGuard на RU исчерпаны.')
    u.need(c.entry_port(plan) not in used_tcp and c.ru_wg_port(plan) not in used_udp,
           'Сохранённые порты RU заняты другой веткой; действующие каскады не меняю.')
    c.validate_plan(plan)


def check_host_endpoint(api, store, plan):
    own, intent = store.load('cascade.receipt.json'), store.load('cascade.intent.json')
    for row in api.rows('hosts'):
        if row.get('address') == plan['domain'] and row.get('port') == c.entry_port(plan):
            u.need((own and row['uuid'] == own['uuid']) or (intent and u.same(row, intent)),
                   'Домен и входной порт уже используются чужим хостом; выберите другой порт.')


def prepare(api, store, args, base, panel_ip):
    """No remote mutation; leave NO saved wrong-domain plan after DNS conflict."""
    saved = store.load('plan.json')
    if saved:
        c.validate_plan(saved)
        u.need(saved['ru'] == {'ip': args.ru_ip, 'sshPort': args.ru_ssh_port}
               and saved['eu'] == {'ip': args.eu_ip, 'sshPort': args.eu_ssh_port}
               and saved['panel'] == base and saved['panelIp'] == panel_ip,
               'Адреса/порты отличаются от сохранённого каскада; не переношу его вслепую.')
        u.need(args.entry_port is None or args.entry_port == c.entry_port(saved), 'Входной порт отличается от сохранённого.')
        u.need(args.ru_wg_port is None or args.ru_wg_port == c.ru_wg_port(saved), 'UDP-порт RU отличается от сохранённого.')
        u.need(getattr(args, 'eu_wg_port', None) is None or args.eu_wg_port == saved['link']['port'],
               'UDP-порт EU отличается от сохранённого; существующий каскад меняется только через repair_udp.py.')
        for key in ('name', 'country', 'domain'):
            value = getattr(args, key)
            u.need(value is None or value == saved[key], 'Параметр --' + key + ' отличается от сохранённого.')
        if getattr(args, 'dns_zone_explicit', False) or args.dns_zone != r.DEFAULT_DNS_ZONE:
            u.need(args.dns_zone == saved['dns']['zone'], 'DNS-зона отличается от сохранённой.')
        if args.squad:
            u.need(u.choose_squads(api, args.squad) == saved['squads'], 'Выбраны другие сквады.')
        u.choose_squads(api, [], saved['squads'])
        return saved
    rows = {kind: api.rows(kind) for kind in ('config-profiles', 'nodes', 'hosts')}
    u.need(not any(row.get('address') in (args.ru_ip, args.eu_ip) for row in rows['nodes']),
           'Этот IP уже указан у ноды панели. Чужие/старые ноды не меняю; нужны две чистые VPS.')
    code, identity = r.suffix(rows), uuid.uuid4().hex
    branch_sources = local_branches(store, base, {'ip': args.ru_ip, 'sshPort': args.ru_ssh_port})
    reused_domain = next((p['domain'] for source, p in branch_sources
                          if source.load('dns.receipt.json') and p['dns']['zone'] == args.dns_zone), None)
    initial_port = args.entry_port or 443
    plan = {'format': 2, 'id': identity, 'code': code, 'mode': 'cascade-wg', 'entryPort': initial_port,
            'ru': {'ip': args.ru_ip, 'sshPort': args.ru_ssh_port},
            'eu': {'ip': args.eu_ip, 'sshPort': args.eu_ssh_port},
            'panel': base, 'panelIp': panel_ip, 'name': args.name or 'RU → EU',
            'country': args.country or '', 'domain': args.domain or reused_domain or 'c-' + code + '.' + args.dns_zone,
            'dns': {'provider': 'cloudflare', 'zone': args.dns_zone, 'ttl': 60},
            'link': c.generate_link(identity, code), 'squads': u.choose_squads(api, args.squad)}
    plan['link']['ruPort'] = args.ru_wg_port or initial_port
    plan['link']['port'] = getattr(args, 'eu_wg_port', None) or 443
    c.validate_plan(plan)
    return plan


def dns_preflight(store, plan, args, settings):
    try:
        client = r.dns.CloudflareDNS(r.cloudflare_token(args, settings))
        zone = client.find_zone(plan['dns']['zone'])
        shared = {} if store.load('dns.intent.json') else shared_dns(plan, local_branches(store, plan['panel'], plan['ru']))
        class Overlay:
            def load(self, key):
                return store.load(key) or shared.get(key)
        client.check_record(Overlay(), zone_id=zone['id'], name=plan['domain'], ipv4=plan['ru']['ip'], ttl=60)
        return client, zone, shared
    except r.dns.DNSProvisionError as error:
        raise u.DeployError(str(error)) from None


class SSH(r.SSH):
    def __init__(self, plan, role, known_hosts=None, control_socket=None):
        self.cascade, self.role = plan, role
        super().__init__({**plan, 'ip': plan[role]['ip'], 'sshPort': plan[role]['sshPort']}, known_hosts, control_socket)
        # An owner's broad SendEnv/ForwardAgent configuration must not grant
        # the entry relay control-plane secrets or a usable SSH signing agent.
        self.environment = {key: value for key, value in self.environment.items()
                            if key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'SSH_AUTH_SOCK', 'TERM')}
        self.common += ['-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ClearAllForwardings=yes',
                        '-o', 'SendEnv=-*']

    @property
    def stage(self):
        return '/root/spn-cascade-managed-' + self.cascade['id']

    def __enter__(self):
        # The other VPS may spend ten minutes waiting for Ubuntu's APT lock.
        # Keep both independently trusted masters alive through that wait.
        if self.external:
            return super().__enter__()
        try:
            result = subprocess.run(self.common + ['-M', '-N', '-f', '-o', 'ControlPersist=3600',
                                     'root@' + self.plan['ip']], env=self.environment)
            u.need(result.returncode == 0, 'SSH не подключился. Изменённый ключ автоматически не принимается.')
            return self
        except BaseException:
            self.tmp.cleanup()
            raise

    def upload(self, files):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            for name, value in files.items():
                u.need(re.fullmatch(r'[A-Za-z0-9_.-]+', name) and isinstance(value, bytes), 'Неверное имя/тип файла установки.')
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(value), 0o600
                archive.addfile(item, io.BytesIO(value))
        stage = shlex.quote(self.stage)
        self.run(f'test ! -L {stage} && install -d -m 700 {stage} && '
                 f'test "$(stat -c %u:%a {stage})" = 0:700 && tar -xmf - --no-same-owner -C {stage}', buffer.getvalue())

    def action(self, action, capture=False):
        p, role = self.cascade, self.role
        peer = 'eu' if role == 'ru' else 'ru'
        command = ['bash', self.stage + '/managed-cascade.sh', action, role, p[role]['ip'], p[peer]['ip'],
                   p['panelIp'], str(p[role]['sshPort']), p['id'], p['link']['interface'],
                   p['link'][role], p['link'][peer], str(p['link']['port']),
                   str(c.entry_port(p)), str(c.ru_wg_port(p))]
        try:
            return self.run(shlex.join(command), capture=capture)
        except u.DeployError:
            raise u.DeployError(role.upper() + ': этап ' + action + ' остановлен; причина указана выше.') from None

    def source(self, action, capture=False):
        # The existing helper deliberately accepts only a UUID-named state
        # directory (or its legacy reality prefix), not arbitrary state paths.
        state = self.stage + '/' + self.cascade['id']
        quoted = shlex.quote(state)
        self.run(f'test ! -L {quoted} && install -d -m 700 {quoted} && test "$(stat -c %u:%a {quoted})" = 0:700')
        command = ['python3', self.stage + '/preferred_source.py', action, '--ip', self.plan['ip'], '--state-dir', state]
        return self.run(shlex.join(command), capture=capture, network_retries=2 if action in ('apply', 'verify') else 0)


class Deployment:
    def __init__(self, api, store, plan):
        self.api, self.store, self.plan = api, store, plan
        self.helper = u.Deployment(api, store, plan, {})

    def ensure(self, key, kind, body, predicate):
        receipt = self.store.load(key + '.receipt.json')
        if kind == 'hosts' and receipt:
            actual = u.one(self.api.rows('hosts'), receipt['uuid'])
            u.need(self.store.load(key + '.intent.json') == body
                   and u.same(actual, {**body, 'isDisabled': actual['isDisabled'], 'remark': actual['remark']}),
                   'Технические параметры каскадного хоста изменены; их не перезаписываю.')
            return actual
        return self.helper.ensure(key, kind, body, predicate)

    def provision(self):
        p, code = self.plan, self.plan['code']
        body = {'name': 'CASCADE_' + code, 'config': c.profile_config(p, r.reality_keys(self.store))}
        profile = self.ensure('profile', 'config-profiles', body, lambda row: row['name'] == body['name'])
        u.need(len(profile['inbounds']) == 1 and profile['inbounds'][0]['tag'] == 'cascade-' + code,
               'Панель не подтвердила единственный приватный инбаунд.')
        inbound = profile['inbounds'][0]['uuid']
        body = {'name': 'cascade-eu-' + code, 'address': p['eu']['ip'], 'port': 2222,
                'tags': ['AUTO_CASCADE'], 'note': 'Managed RU/EU cascade ' + code,
                'configProfile': {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': [inbound]}}
        if p['country']:
            body['countryCode'] = p['country']
        receipt = self.store.load('node.receipt.json')
        for row in self.api.rows('nodes'):
            if row.get('address') == p['eu']['ip']:
                u.need(row['uuid'] == (receipt or {}).get('uuid') or
                       (self.store.load('node.intent.json') == body and u.same(row, body)),
                       'EU-адрес занят другой нодой; её не меняю.')
        node = self.ensure('node', 'nodes', body, lambda row: row['name'] == body['name'])
        return profile, node, inbound

    def attach(self, inbound):
        for squad in self.plan['squads']:
            for _ in range(3):
                current = u.ids(u.one(self.api.rows('internal-squads'), squad)['inbounds'])
                if inbound in current:
                    break
                if u.ids(u.one(self.api.rows('internal-squads'), squad)['inbounds']) != current:
                    continue
                body = {'uuid': squad, 'inbounds': current + [inbound]}
                self.store.save('squad-' + squad + '-' + u.digest(body)[:12] + '.intent.json', body)
                actual = self.api('PATCH', '/internal-squads', body)
                u.need(set(u.ids(actual['inbounds'])) == set(body['inbounds']), 'Привязка к скваду не подтверждена.')
                break
            else:
                raise u.DeployError('Сквад изменяется одновременно; повторите команду.')
        actual = {row['uuid'] for row in self.api.rows('internal-squads') if inbound in u.ids(row['inbounds'])}
        u.need(set(self.plan['squads']) <= actual, 'Инбаунд отсутствует в выбранном скваде.')

    def host_body(self, profile, node, inbound):
        return {'remark': self.plan['name'] + ' • Каскад ' + self.plan['code'],
                'address': self.plan['domain'], 'port': c.entry_port(self.plan), 'nodes': [node['uuid']],
                'inbound': {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbound},
                'sni': c.SNI, 'alpn': 'h2', 'fingerprint': c.FINGERPRINT, 'securityLayer': 'DEFAULT',
                'isDisabled': True, 'isHidden': False, 'internalSquads': {'mode': 'EXCLUDE', 'squads': []},
                'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
                'tags': ['AUTO_CASCADE']}

    def host(self, profile, node, inbound):
        body = self.host_body(profile, node, inbound)
        return self.ensure('cascade', 'hosts', body, lambda row:
            (row.get('inbound') or {}).get('configProfileInboundUuid') == inbound)

    def publish(self, host):
        self.helper.publish({'cascade': host})
        u.need(u.one(self.api.rows('hosts'), host['uuid'])['isDisabled'] is False, 'Хост не включился.')


def proof_valid(proof, plan, route):
    return (isinstance(proof, dict) and proof.get('passed') is True and proof.get('tests') == 9
            and proof.get('ip') == plan['ru']['ip'] and proof.get('port') == c.entry_port(plan)
            and proof.get('expectedExitIp') == plan['eu']['ip']
            and proof.get('relayPathVerified') is True and proof.get('testRoute') == route
            and len(proof.get('results', [])) == 9
            and all(row.get('passed') is True and row.get('clientAlive') is True for row in proof['results'])
            and ((route == 'physical' and proof.get('physicalSourceObserved') is True
                  and proof.get('directReachabilityVerified') is True)
                 or (route == 'system' and proof.get('directReachabilityVerified') is False)))


def run(args):
    validate(args)
    if not args.apply:
        print(json.dumps({'planOnly': True, 'changes': False,
            'entryRU': args.ru_ip + ':' + (str(args.entry_port) if args.entry_port else '<auto free port>'),
            'exitEU': args.eu_ip, 'transport': 'TCP REALITY / Vision / Safari', 'link': 'WireGuard, peer-only /32',
            'domain': args.domain or 'c-<code>.' + args.dns_zone, 'dnsPointsTo': args.ru_ip,
            'euPublicVpnListener': False, 'ruReceivesVpnPrivateKey': False,
            'explicitKeptTcpPorts': {'ru': args.ru_keep_tcp_port or [], 'eu': args.eu_keep_tcp_port or []},
            'creates': ['one RU DNS record', 'one EU node', 'one profile/inbound', 'one RU-facing host'],
            'testRoute': args.test_route, 'buysResources': False}, ensure_ascii=False))
        return
    settings = u.read_settings(Path(args.env_file).expanduser())
    base = u.panel_url(args.panel_url or settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    token = settings.get('REMNAWAVE_API_TOKEN') if settings.get('REMNAWAVE_BASE_URL', '').rstrip('/') == base else None
    api = r.API(base, token or getpass.getpass('API-токен Remnawave (на VPS не передаётся): '))
    try:
        subscription = r.probe.private_file(Path(args.test_subscription).expanduser())
        r.probe.subscription_url(subscription)
        engine = r.probe.select_engine(args.engine)
        r.probe.validate_interface(args.interface)
    except r.probe.ProbeError as error:
        raise u.DeployError(str(error)) from None
    store = u.Store(directory(base, args))
    host_lock = u.Store(store.path.parent / '.ru-locks' / (args.ru_ip + '-ssh' + str(args.ru_ssh_port)))
    with host_lock.lock(), store.lock():
        already_saved = store.load('plan.json') is not None
        plan = prepare(api, store, args, base, r.panel_ipv4(base))
        policy = host_policy(store, base, plan['ru'], args)
        u.need(c.entry_port(plan) not in policy['ru']['keepTcpPorts'],
               'Входной порт каскада занят сохраняемой службой; выберите другой --entry-port.')
        dns_context = dns_preflight(store, plan, args, settings)
        keys, wgkeys = r.reality_keys(store), c.wg_keys(store)
        deployment = Deployment(api, store, plan)
        known = {}
        for role in ('ru', 'eu'):
            try:
                known[role] = r.trust.resolve(plan[role]['ip'], plan[role]['sshPort'],
                    u.Store(store.path / ('ssh-' + role)), expected=getattr(args, role + '_host_key_sha256'))
            except r.trust.TrustError as error:
                raise u.DeployError(str(error)) from None
        print('Каскад ' + plan['code'] + ': вход RU ' + plan['ru']['ip'] + ', выход EU ' + plan['eu']['ip'] + '.', flush=True)
        with contextlib.ExitStack() as stack:
            servers = {role: stack.enter_context(
                SSH(plan, role, known[role], control_socket=getattr(args, role + '_ssh_control'))
                if getattr(args, role + '_ssh_control', None) else SSH(plan, role, known[role])) for role in ('ru', 'eu')}
            for role, ssh in servers.items():
                files = {'managed-cascade.sh': (HERE / 'managed-cascade.sh').read_bytes(),
                         'host_checks.py': (HERE / 'host_checks.py').read_bytes(),
                         'host-policy.json': json.dumps(policy[role]).encode(),
                         'preferred_source.py': (r.HERE / 'preferred_source.py').read_bytes()}
                if role == 'ru':
                    files['shared_ru.py'] = (HERE / 'shared_ru.py').read_bytes()
                    files['shared-ru-proof.json'] = json.dumps(legacy_proof(local_branches(store, base, plan['ru']))).encode()
                ssh.upload(files)
            inventory = json.loads(servers['ru'].action('inventory', capture=True))
            allocate_ports(plan, inventory, args, saved=already_saved)
            check_host_endpoint(api, store, plan)
            for role, ssh in servers.items():
                ssh.action('preflight')
            identities = {role: json.loads(ssh.action('identity', capture=True)) for role, ssh in servers.items()}
            for identity in identities.values():
                u.need(re.fullmatch('[0-9a-f]{32}', identity.get('machineId', ''))
                       and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', identity.get('sshFingerprint', '')),
                       'Не удалось подтвердить отдельную идентичность VPS.')
            u.need(identities['ru']['machineId'] != identities['eu']['machineId']
                   and identities['ru']['sshFingerprint'] != identities['eu']['sshFingerprint'],
                   'RU и EU должны быть разными VPS, а не двумя IP одной машины или клонами с общими ключами.')
            store.save('machines-' + u.digest(identities) + '.json', identities)
            # Port conflicts and failed target checks must not pin a wrong plan.
            store.save('plan.json', plan)
            store.save('host-policy.json', policy)
            if len(dns_context) == 3:
                for name, value in dns_context[2].items():
                    store.save(name, value)
            # Both targets are trusted and known clean/owned before networking,
            # packages, panel objects, or DNS are changed.
            for role, ssh in servers.items():
                ssh.source('apply')
                u.need(plan[role]['ip'] in ssh.assigned_ipv4(), 'IPv4 не назначен доверенной VPS; установка остановлена.')
            profile, node, inbound = deployment.provision()
            secret = store.load('node-secret.json')
            if secret is None:
                secret = {'value': api('GET', '/keygen')['secretKey']}
                store.save('node-secret.json', secret)
            for role in ('eu', 'ru'):
                files = c.role_files(plan, role, wgkeys, node_secret=secret['value'] if role == 'eu' else None)
                servers[role].upload(files)
                servers[role].action('install')
            deployment.helper.wait_ready(node, args.wait)
            deployment.attach(inbound)
            for ssh in servers.values():
                ssh.action('verify')
                ssh.source('verify')
            r.ensure_dns(store, {**plan, 'ip': plan['ru']['ip']}, dns_context[:2], args.wait)
            host = deployment.host(profile, node, inbound)
            try:
                proof = r.probe.run(plan['ru']['ip'], keys['public'], keys['short'], subscription,
                    engine=engine, interface=args.interface, route=args.test_route,
                    expected_exit_ip=plan['eu']['ip'], public_port=c.entry_port(plan))
            except r.probe.ProbeError as error:
                raise u.DeployError(str(error) + ' Каскад не подтверждён; новая VPS/переустановка вслепую не нужны.') from None
            store.save('vpn-test-' + str(time.time_ns()) + '.json', proof)
            u.need(proof_valid(proof, plan, args.test_route),
                   'Полный тест RU → EU не прошёл: нужны 9/9 запросов и выход именно с EU-IP. Хост не включаю.')
            # Tunnel counters and EU bind/firewall are checked again after traffic.
            for ssh in servers.values():
                ssh.action('verify')
            completed = store.load('activation.verified.json') is not None
            pending = store.load('activation.pending.json')
            keep_disabled = completed and host['isDisabled'] and not pending
            if not keep_disabled:
                was_disabled = host['isDisabled']
                marker = {'host': host['uuid'], 'code': plan['code']}
                store.save('activation.pending.json', marker)
                deployment.publish(host)
                try:
                    exported = None
                    for attempt in range(3):
                        try:
                            exported = r.probe.verify_export(plan['ru']['ip'], keys['public'], keys['short'], subscription,
                                interface=args.interface, route=args.test_route, endpoint_address=plan['domain'],
                                public_port=c.entry_port(plan))
                            u.need(exported.get('passed') is True, 'Выгрузка RU-хоста не подтверждена.')
                            break
                        except r.probe.ProbeError:
                            if attempt == 2:
                                raise
                            time.sleep(1)
                    store.save('export-test-' + str(time.time_ns()) + '.json', exported)
                    store.save('activation.verified.json', {**marker, 'trafficAndExportVerified': True})
                    u.need(store.load('activation.pending.json') == marker, 'Маркер активации изменён.')
                    (store.path / 'activation.pending.json').unlink()
                except (r.probe.ProbeError, u.DeployError) as error:
                    if was_disabled:
                        actual = deployment.host(profile, node, inbound)
                        if not actual['isDisabled']:
                            api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': True})
                    raise u.DeployError(str(error) + ' Экспорт подписки не подтверждён; хост возвращён в прежнее состояние.') from None
        print(('Проверено, хост оставлен выключенным владельцем: ' if keep_disabled else 'Каскад создан и проверен: ') + host['remark'])
        print('В подписке: ' + plan['domain'] + ':' + str(c.entry_port(plan)) + ' → RU. Реальный выход: EU '
              + plan['eu']['ip'] + '. VPN-тест 9/9.')
        print('EU-IP отсутствует в адресе подключения, но виден сайтам и пользователю при проверке внешнего IP.')
        if args.test_route != 'physical':
            print('Проверено через текущее подключение Mac; прямой доступ из сети клиента НЕ подтверждён.')
        print('Приватное состояние и ключи: ' + str(store.path))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ru-ip', required=True, help='Entry public IPv4; clean RU VPS or an existing owned cascade RU')
    p.add_argument('--eu-ip', required=True, help='Exit public IPv4; clean EU VPS')
    p.add_argument('--entry-port', type=int, help='RU public TCP port; default chooses an unused owned branch port')
    p.add_argument('--ru-wg-port', type=int, help='RU WireGuard UDP port; default equals the new TCP entry port')
    p.add_argument('--eu-wg-port', type=int, help='EU WireGuard UDP port for a new cascade; default 443')
    p.add_argument('--ru-keep-tcp-port', action='append', type=int,
                   help='Explicitly keep TCP 10050 open on RU without requiring Zabbix identification; no service is stopped')
    p.add_argument('--eu-keep-tcp-port', action='append', type=int,
                   help='Explicitly keep TCP 10050 open on EU without requiring Zabbix identification; no service is stopped')
    p.add_argument('--domain', help='Public endpoint, A -> RU only; default c-<random6>.wayspn.com')
    p.add_argument('--dns-zone', default=r.DEFAULT_DNS_ZONE, action=r.DNSZoneAction)
    p.set_defaults(dns_zone_explicit=False)
    p.add_argument('--name')
    p.add_argument('--country', help='Exit country code, e.g. NL or DE')
    p.add_argument('--ru-ssh-port', type=int, default=22)
    p.add_argument('--eu-ssh-port', type=int, default=22)
    p.add_argument('--ru-host-key-sha256')
    p.add_argument('--eu-host-key-sha256')
    p.add_argument('--ru-ssh-control', help=argparse.SUPPRESS)
    p.add_argument('--eu-ssh-control', help=argparse.SUPPRESS)
    p.add_argument('--squad', action='append', default=[])
    p.add_argument('--panel-url')
    p.add_argument('--env-file', default=str(r.ROOT / '.env'))
    p.add_argument('--cloudflare-token-file')
    p.add_argument('--state-dir')
    p.add_argument('--wait', type=int, default=120)
    p.add_argument('--test-subscription', default=str(r.ROOT / '.vpn-test-link.txt'))
    p.add_argument('--engine')
    p.add_argument('--interface', default='en0')
    p.add_argument('--test-route', choices=('physical', 'system'), default='physical')
    p.add_argument('--apply', action='store_true')
    return p


if __name__ == '__main__':
    try:
        run(parser().parse_args())
    except (u.DeployError, c.ConfigError, KeyboardInterrupt) as error:
        print('Остановлено: ' + (str(error) if not isinstance(error, KeyboardInterrupt) else 'прервано пользователем.')
              + ' Сохранённые данные не удалены.', file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        print('Остановлено: ' + type(error).__name__ + '; подробности скрыты для защиты ключей. '
              'Работа каскада не подтверждена, состояние сохранено.', file=sys.stderr)
        raise SystemExit(2)
