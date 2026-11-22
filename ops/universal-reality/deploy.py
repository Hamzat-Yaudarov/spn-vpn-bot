#!/usr/bin/env python3
"""Install one direct TCP TLS / Vision / Safari server and its panel objects.

Local plan by default. --apply changes only a clean or previously owned VPS,
its own panel objects, one DNS-only Cloudflare A record and additive squad
membership. TLS certificates renew automatically; legacy REALITY plans retain
their existing transport. No paid resources are ordered.
"""
from __future__ import annotations

import argparse
import contextlib
import base64
import copy
import getpass
import hashlib
import hmac
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import stat
import string
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('spn_direct_infrastructure', HERE.parent / 'universal-direct/deploy.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)
def local_module(name):
    definition = importlib.util.spec_from_file_location('spn_reality_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(definition)
    definition.loader.exec_module(module)
    return module


trust = local_module('ssh_trust')
probe = local_module('vpn_probe')
dns = local_module('cloudflare_dns')
security = local_module('node_security')
SNI = 'www.cloudflare.com'
FINGERPRINT = 'safari'
IMAGE = 'remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e'
ALPHABET = string.ascii_lowercase + string.digits
DEFAULT_DNS_ZONE = 'wayspn.com'


def is_tls(plan):
    return plan.get('transport') == 'raw-tls-vision-safari-v1'


def profile_tag(plan):
    return ('tls-' if is_tls(plan) else 'reality-') + plan['code']


def tls_profile_config(code, domain, *, site_version=0):
    domain = dns.normalize_name(domain)
    u.need(type(site_version) is int and site_version in (0, 1), 'Неподдерживаемая версия HTTPS-сайта.')
    value = {
        'log': {'loglevel': 'warning', 'access': 'none'},
        'dns': {'servers': ['localhost'], 'queryStrategy': 'UseIPv4'},
        'inbounds': [{
            'tag': 'tls-' + code, 'listen': '0.0.0.0', 'port': 443, 'protocol': 'vless',
            'settings': {'clients': [], 'decryption': 'none', 'flow': 'xtls-rprx-vision'},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
            'streamSettings': {'network': 'raw', 'security': 'tls', 'tlsSettings': {
                'serverName': domain, 'minVersion': '1.3', 'alpn': ['h2'],
                'certificates': [{
                    'certificateFile': '/etc/letsencrypt/live/' + domain + '/fullchain.pem',
                    'keyFile': '/etc/letsencrypt/live/' + domain + '/privkey.pem',
                }],
            }},
        }],
        'outbounds': [{'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
                      {'tag': 'block', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [{
            'type': 'field', 'ip': ['geoip:private', '169.254.0.0/16', '168.63.129.16/32', '100.64.0.0/10'],
            'outboundTag': 'block',
        }]},
        'policy': {'levels': {'0': {'handshake': 4, 'connIdle': 120, 'uplinkOnly': 5, 'downlinkOnly': 5}}},
    }
    if site_version:
        inbound = value['inbounds'][0]
        inbound['settings']['fallbacks'] = [
            {'dest': '127.0.0.1:18080'},
            {'alpn': 'h2', 'dest': '127.0.0.1:18081'},
        ]
        inbound['streamSettings']['tlsSettings']['alpn'] = ['h2', 'http/1.1']
        inbound['streamSettings']['tlsSettings']['rejectUnknownSni'] = True
    return value


def profile_body(plan, store):
    return {'name': ('TLS_' if is_tls(plan) else 'REALITY_') + plan['code'],
            'config': tls_profile_config(plan['code'], plan['domain'], site_version=tls_site_version(plan, store)) if is_tls(plan) else
                      profile_config(plan['code'], reality_keys(store), plan.get('sni', SNI))}


def tls_site_transition(plan, store):
    """Additive capability receipt; original deployment plans stay immutable."""
    before = {'name': 'TLS_' + plan['code'], 'config': tls_profile_config(plan['code'], plan['domain'])}
    after = {'name': before['name'], 'config': tls_profile_config(plan['code'], plan['domain'], site_version=1)}
    receipt = store.load('profile.receipt.json')
    u.need(isinstance(receipt, dict) and isinstance(receipt.get('uuid'), str),
           'Отсутствует квитанция собственного TLS-профиля.')
    return {'format': 1, 'version': 1, 'deploymentId': plan['id'], 'profileUuid': receipt['uuid'],
            'beforeHash': u.digest(before), 'afterHash': u.digest(after)}, before, after


def tls_site_version(plan, store=None):
    version = plan.get('tlsSiteVersion', 0)
    u.need(type(version) is int and version in (0, 1), 'Неподдерживаемая версия HTTPS-сайта.')
    if not is_tls(plan) or version or store is None:
        return version
    intent = store.load('tls-site-v1.intent.json')
    receipt = store.load('tls-site-v1.receipt.json')
    if intent is None:
        u.need(receipt is None, 'Квитанция HTTPS-сайта не имеет исходного намерения.')
        return 0
    expected, before, _ = tls_site_transition(plan, store)
    u.need(intent == expected and receipt in (None, expected) and store.load('profile.intent.json') == before,
           'Квитанция обновления HTTPS-сайта не соответствует исходному TLS-профилю.')
    return 1


def tls_runtime_plan(plan, store):
    """Derive an upgraded SSH runtime without rewriting the original plan."""
    return {**plan, 'tlsSiteVersion': 1} if is_tls(plan) and tls_site_version(plan, store) else plan


def upgrade_tls_site_profile(api, store, plan):
    """Call under the installation lock after site-install; never edits a shared profile."""
    u.need(is_tls(plan) and not plan.get('tlsSiteVersion'), 'Нужен исходный TLS-план без HTTPS-сайта.')
    transition, before, after = tls_site_transition(plan, store)
    u.need(store.load('profile.intent.json') == before, 'Исходный TLS-профиль не совпадает с сохранённым.')
    profile = u.one(api.rows('config-profiles'), transition['profileUuid'])
    node = store.load('node.receipt.json')
    u.need(isinstance(node, dict) and isinstance(node.get('uuid'), str), 'Отсутствует квитанция собственной TLS-ноды.')
    references = [row['uuid'] for row in api.rows('nodes')
                  if (row.get('configProfile') or {}).get('activeConfigProfileUuid') == profile['uuid']]
    u.need(references == [node['uuid']], 'TLS-профиль используется другой нодой или отсоединён; его не меняю.')
    u.need(profile.get('name') == before['name'] and profile.get('config') in (before['config'], after['config'])
           and isinstance(profile.get('inbounds'), list) and len(profile['inbounds']) == 1
           and profile['inbounds'][0].get('tag') == profile_tag(plan), 'TLS-профиль был изменён вне этой установки.')
    inbound_before = profile['inbounds'][0]['uuid']
    store.save('tls-site-v1.intent.json', transition)
    if profile['config'] == before['config']:
        api('PATCH', '/config-profiles', {'uuid': profile['uuid'], 'config': after['config']})
    profile = u.one(api.rows('config-profiles'), profile['uuid'])
    inbounds = profile.get('inbounds')
    u.need(profile.get('name') == before['name'] and profile.get('config') == after['config']
           and isinstance(inbounds, list) and len(inbounds) == 1
           and inbounds[0].get('uuid') == inbound_before and inbounds[0].get('tag') == profile_tag(plan),
           'Обновление TLS-сайта или сохранение UUID инбаунда не подтверждено.')
    store.save('tls-site-v1.receipt.json', transition)
    return profile


def probe_parameters(plan, store):
    if is_tls(plan):
        return {'public': '', 'short': ''}, {'sni': plan['domain'], 'security': 'tls'}
    return reality_keys(store), ({'sni': plan['sni']} if plan.get('sni') else {})


def managed_script(plan):
    return (HERE / ('tls-node.sh' if is_tls(plan) else 'managed-node.sh')).read_bytes()


def register_fleet(store, args):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from spn_reserve_fleet.remote import handoff_installation, load_controller
        controller = load_controller(getattr(args, 'fleet_controller_file', None))
        if controller is not None:
            return handoff_installation(store.path, controller,
                                        local_data_dir=getattr(args, 'fleet_dir', None), locked=True)
        from spn_reserve_fleet.enrollment import register_installation
        return register_installation(store.path, data_dir=getattr(args, 'fleet_dir', None), locked=True)
    except Exception as error:
        raise u.DeployError('VPN установлен, но регистрация в панели резервных IP не завершена ('
                            + type(error).__name__ + '). Установка сохранена. Если настроен VPS-контроллер, '
                            'повторите python3 -m spn_reserve_fleet handoff --state-dir КАТАЛОГ_УСТАНОВКИ; '
                            'иначе повторите прежний deploy.py.') from None


def enroll_controller_key(ssh, store, args):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from spn_reserve_fleet.remote import install_controller_key, load_controller
        controller = load_controller(getattr(args, 'fleet_controller_file', None))
        if controller is not None and controller.get('node_public_key'):
            proof = install_controller_key(ssh, controller)
            store.save('fleet-controller-key-' + u.digest(controller['node_public_key'])[:32] + '.json',
                       {'host': controller['host'], 'publicKey': controller['node_public_key'], 'verified': proof['ok']})
    except Exception as error:
        raise u.DeployError('VPN установлен, но доступ контроллера по публичному SSH-ключу не настроен ('
                            + type(error).__name__ + '). Передача управления ещё не начата; повторите deploy.py.') from None


def endpoint_address(plan):
    """Legacy IP plans and VK backend identities are never auto-migrated."""
    return plan.get('domain') or plan['ip']


def dns_options(args):
    try:
        zone = dns.normalize_name(getattr(args, 'dns_zone', None) or DEFAULT_DNS_ZONE)
        name = dns.normalize_name(args.domain) if getattr(args, 'domain', None) else None
    except ValueError:
        raise u.DeployError('--domain/--dns-zone: нужен корректный DNS-домен, не IP.') from None
    u.need(name is None or name == zone or name.endswith('.' + zone),
           '--domain должен находиться внутри --dns-zone.')
    return zone, name


def cloudflare_token(args, settings):
    """Local-only secret: never store it in a plan, argv or uploaded files."""
    token = os.environ.get('CLOUDFLARE_API_TOKEN') or settings.get('CLOUDFLARE_API_TOKEN')
    if not token:
        path = Path(args.env_file).expanduser()
        if path.exists():
            u.need(not path.is_symlink() and path.is_file(), 'Небезопасный .env для Cloudflare.')
            for line in path.read_text().splitlines():
                key, sep, value = line.strip().partition('=')
                if sep and key == 'CLOUDFLARE_API_TOKEN':
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    token = value
    if not token:
        path = Path(getattr(args, 'cloudflare_token_file', None) or '~/.config/spn/cloudflare-token').expanduser().absolute()
        u.need(not any(parent.is_symlink() for parent in (path, *path.parents)),
               'Файл токена Cloudflare не должен быть симлинком.')
        try:
            info = path.stat()
        except OSError:
            raise u.DeployError('Нет токена Cloudflare. Задайте CLOUDFLARE_API_TOKEN в окружении/.env '
                                'или сохраните его в ~/.config/spn/cloudflare-token с правами 0600.') from None
        u.need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
               and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and 0 < info.st_size < 4096,
               'Файл токена Cloudflare должен принадлежать вам и иметь права 0600.')
        token = path.read_text().strip()
    u.need(isinstance(token, str) and token and not any(char.isspace() for char in token),
           'Неверный формат токена Cloudflare; значение скрыто.')
    return token


def dns_preflight(store, plan, args, settings):
    if not plan.get('dns'):
        return None
    try:
        client = dns.CloudflareDNS(cloudflare_token(args, settings))
        zone = client.find_zone(plan['dns']['zone'])
        client.check_record(store, zone_id=zone['id'], name=plan['domain'], ipv4=plan['ip'], ttl=60)
        return client, zone
    except dns.DNSProvisionError as error:
        raise u.DeployError(str(error)) from None


def _dig(server, name, record_type, *, authoritative=False):
    """Never resolve the endpoint through macOS/FakeIP or accept a CNAME."""
    command = ['dig', '@' + server, name + '.', record_type, '+time=2', '+tries=1', '+noall', '+comments', '+answer']
    if authoritative:
        command.append('+norecurse')
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise u.DeployError('Авторитетная проверка DNS недоступна; нужен dig и доступ к DNS.') from None
    output = result.stdout.decode('ascii', errors='replace')
    u.need(result.returncode == 0 and re.search(r'status: NOERROR\b', output),
           'DNS ещё не подтверждён авторитетным сервером.')
    if authoritative:
        flags = re.search(r';; flags: ([^;]+);', output)
        u.need(flags and 'aa' in flags.group(1).split(), 'DNS-ответ не авторитетный; публикация остановлена.')
    answers = []
    for line in output.splitlines():
        if not line or line.startswith(';'):
            continue
        fields = line.split()
        u.need(len(fields) == 5 and fields[2] == 'IN' and fields[3] == record_type
               and fields[0].rstrip('.').lower() == name,
               'DNS содержит неожиданный адрес/алиас; публикация остановлена.')
        answers.append(fields[4])
    return answers


def verify_authoritative_dns(plan, zone):
    """Query every assigned nameserver directly, including explicit AAAA absence."""
    nameservers = zone.get('name_servers')
    u.need(isinstance(nameservers, list) and 1 <= len(nameservers) <= 8,
           'Cloudflare не вернул авторитетные DNS-серверы зоны.')
    try:
        nameservers = {dns.normalize_name(raw) for raw in nameservers}
        delegated = {dns.normalize_name(raw) for raw in _dig('1.1.1.1', plan['dns']['zone'], 'NS')}
    except ValueError:
        raise u.DeployError('Некорректный DNS-сервер зоны Cloudflare.') from None
    u.need(delegated == nameservers, 'Публичная DNS-делегация не совпала с серверами зоны Cloudflare.')
    for name in sorted(nameservers):
        # Resolve nameserver addresses through explicit public DNS, not the OS
        # resolver that can be intercepted by a VPN's FakeIP mode.
        addresses = _dig('1.1.1.1', name, 'A')
        u.need(addresses, 'Не удалось определить IPv4 авторитетного DNS-сервера.')
        for address in addresses:
            server = public_ipv4(address)
            u.need(_dig(server, plan['domain'], 'A', authoritative=True) == [plan['ip']],
                   'Авторитетная A-запись ещё не указывает только на IPv4 VPS.')
            u.need(not _dig(server, plan['domain'], 'AAAA', authoritative=True),
                   'У домена есть AAAA-запись; IPv4-only установка остановлена.')


def ensure_dns(store, plan, context, wait):
    if context is None:
        return
    client, zone = context
    try:
        client.ensure_record(store, zone_id=zone['id'], name=plan['domain'], ipv4=plan['ip'], ttl=60)
    except dns.DNSProvisionError as error:
        raise u.DeployError(str(error)) from None
    deadline = time.monotonic() + wait
    while True:
        try:
            verify_authoritative_dns(plan, zone)
            return
        except u.DeployError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


class API(u.API):
    def disable_node(self, uid):
        return self.node_action(uid, 'disable')

    def enable_node(self, uid):
        return self.node_action(uid, 'enable')

    def node_action(self, uid, action):
        # The documented Nodes Controller action, not PATCH isDisabled.
        u.need(str(uuid.UUID(uid)) == uid, 'Некорректный UUID выключаемой ноды.')
        u.need(action in ('disable', 'enable'), 'Недопустимое действие ноды.')
        path = '/nodes/' + uid + '/actions/' + action
        request = urllib.request.Request(self.base + path, method='POST',
                                         headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=25) as response:
                raw = response.read(8_000_001)
                u.need(len(raw) <= 8_000_000, 'Слишком большой ответ выключения ноды.')
                return json.loads(raw)['response']
        except urllib.error.HTTPError as error:
            raise u.PanelHTTPError(error.code, 'POST', path) from None
        except (OSError, ValueError, KeyError):
            raise u.DeployError('Ответ выключения ноды потерян; сохраните состояние и повторите команду.') from None

    def rows(self, kind):
        for attempt in range(3):
            try:
                return super().rows(kind)
            except u.PanelHTTPError:
                raise
            except u.DeployError:
                if attempt == 2:
                    raise
                time.sleep(.3)


def public_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise u.DeployError('Нужен публичный IPv4 VPS.') from None
    u.need(address.is_global and not address.is_multicast, 'Нужен публичный IPv4 VPS.')
    return str(address)


def state_directory(base, ip, ssh_port, home=None):
    home = Path.home() if home is None else Path(home)
    return home / '.spn-reality-deploy' / u.digest(base)[:12] / f'{ip}-ssh{ssh_port}'


def suffix(rows):
    """One shared six-character installation code; preflight every name/tag."""
    occupied = set()
    for kind, objects in rows.items():
        for row in objects:
            fields = [row.get('name', ''), row.get('remark', '')]
            if kind == 'config-profiles':
                fields += [inbound['tag'] for inbound in row['inbounds']]
            occupied.update(value[-6:].lower() for value in fields if len(value) >= 6)
    for _ in range(100):
        code = ''.join(secrets.choice(ALPHABET) for _ in range(6))
        if code not in occupied and any(c.isdigit() for c in code) and any(c.isalpha() for c in code):
            return code
    raise u.DeployError('Не удалось подобрать свободный код. Изменения не выполнялись.')


def reality_keys(store):
    cached = store.load('reality.private.json')
    if cached is not None:
        return cached
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except ImportError:
        raise u.DeployError('Установите зависимость: python3 -m pip install cryptography') from None
    key = X25519PrivateKey.generate()
    encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip('=')
    result = {
        'private': encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                           serialization.NoEncryption())),
        'public': encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
        'short': secrets.token_hex(8),
    }
    store.save('reality.private.json', result)
    return result


def legacy_profile_config(code, keys, sni=SNI):
    return {
        'log': {'loglevel': 'warning', 'access': 'none'},
        'dns': {'servers': ['localhost'], 'queryStrategy': 'UseIPv4'},
        'inbounds': [{
            'tag': 'reality-' + code, 'listen': '0.0.0.0', 'port': 443, 'protocol': 'vless',
            'settings': {'clients': [], 'decryption': 'none'},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
            'streamSettings': {'network': 'raw', 'security': 'reality', 'realitySettings': {
                'show': False, 'target': sni + ':443', 'xver': 0, 'serverNames': [sni],
                'privateKey': keys['private'], 'shortIds': [keys['short']],
            }},
        }],
        'outbounds': [{'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
                      {'tag': 'block', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [{
            'type': 'field', 'ip': ['geoip:private', '169.254.0.0/16', '168.63.129.16/32', '100.64.0.0/10'],
            'outboundTag': 'block',
        }]},
    }


def profile_config(code, keys, sni=SNI):
    # Same public TCP REALITY/Safari protocol. Only the private listener moves
    # behind a bounded, non-terminating TCP ClientHello guard.
    value = legacy_profile_config(code, keys, sni)
    value['inbounds'][0].update(listen='127.0.0.1', port=2443)
    value['inbounds'][0]['streamSettings']['sockopt'] = {'acceptProxyProtocol': True}
    value['policy'] = {'levels': {'0': {'handshake': 4, 'connIdle': 120, 'uplinkOnly': 5, 'downlinkOnly': 5}}}
    return value


def node_snapshot(node):
    """Editable identity/config only: never pin transient connection status."""
    result = {key: copy.deepcopy(node[key]) for key in
              ('uuid', 'name', 'address', 'port', 'tags', 'note', 'countryCode',
               'proxyUrl', 'activePluginUuid') if key in node}
    binding = node.get('configProfile') or {}
    u.need(binding.get('activeConfigProfileUuid') and isinstance(binding.get('activeInbounds'), list),
           'У прежней ноды нет однозначной привязки профиля; замена остановлена.')
    result['configProfile'] = {'activeConfigProfileUuid': binding['activeConfigProfileUuid'],
                               'activeInbounds': u.ids(binding['activeInbounds'])}
    return result


def replacement_matches(node, snapshot):
    return u.same(node, {key: value for key, value in snapshot.items() if key != 'uuid'})


def replacement_after_matches(node, body, before):
    preserved = {key: value for key, value in before.items() if key != 'uuid' and key not in body}
    return u.same(node, body) and u.same(node, preserved)


def direct_controller(node):
    u.need(not node.get('proxyUrl') and not node.get('activePluginUuid'),
           'У выбранной ноды настроен дополнительный прокси/плагин управления; его автоматически не меняю.')


def validate_replacement(api, store, args):
    intent = store.load('replacement.intent.json')
    requested = getattr(args, 'replace_node', None)
    if not intent:
        u.need(not requested, '--replace-node допустим только для явно выбранной прежней ноды с этим IP.')
        return
    before = intent['node']
    u.need(requested is None or requested == before['uuid'],
           '--replace-node отличается от сохранённой операции замены.')
    node = u.one(api.rows('nodes'), before['uuid'])
    direct_controller(node)
    after = store.load('node.intent.json')
    receipt = store.load('node.receipt.json')
    addresses = {before['address']}
    if after and (replacement_after_matches(node, after, before) or receipt):
        addresses.add(after['address'])
    u.need(node.get('address') in addresses and node.get('port') == 2222,
           'Адрес или управляющий порт выбранной прежней ноды изменился; замену не продолжаю.')
    # After a confirmed replacement, the ordinary own-object checks also allow
    # the explicit clean-guest config restoration in provision().
    u.need(receipt is not None or replacement_matches(node, before)
           or (after and replacement_after_matches(node, after, before)),
           'Выбранная прежняя нода изменилась после подтверждения; её не перезаписываю.')
    if receipt:
        u.need(receipt.get('uuid') == before['uuid'], 'UUID квитанции замены отличается от выбранной ноды.')


def stage_replacement(api, store, args, nodes, hosts):
    """Explicitly replace a legacy panel record, never adopt it by IP alone."""
    previous = store.load('replacement.intent.json')
    if previous:
        validate_replacement(api, store, args)
        return
    requested = getattr(args, 'replace_node', None)
    matches = [row for row in nodes if row.get('address') == args.ip]
    u.need(len(matches) <= 1, 'В панели несколько нод с этим IP. Сначала нужна ручная сверка; ничего не меняю.')
    if not matches:
        u.need(not requested, 'Нода из --replace-node не найдена на указанном IP; ничего не меняю.')
        return
    node = matches[0]
    if requested is None and sys.stdin.isatty():
        print('На этом IP уже есть нода: ' + node['name'] + ' (' + node['uuid'] + ').', flush=True)
        print('Замена использует эту запись для нового TCP REALITY. Прежние профиль и хосты не удаляются; '
              'её отдельные старые хосты будут выключены. VPS должна быть чистой.', flush=True)
        answer = input('Для замены именно этой ноды напишите ЗАМЕНИТЬ; Enter — отмена: ').strip()
        if answer == 'ЗАМЕНИТЬ':
            requested = node['uuid']
    u.need(requested == node['uuid'],
           'IP уже указан у ноды «' + node['name'] + '» (' + node['uuid'] + '). '
           'Это не ошибка названия. Для её явной замены добавьте --replace-node ' + node['uuid']
           + '; без подтверждения запись не меняется.')
    u.need(node.get('port') == 2222, 'У прежней ноды другой управляющий порт; автоматическая замена не выполняется.')
    direct_controller(node)
    before = node_snapshot(node)
    old_profile = before['configProfile']['activeConfigProfileUuid']
    old_inbounds = set(before['configProfile']['activeInbounds'])
    affected = []
    for host in hosts:
        binding = host.get('inbound') or {}
        if (host.get('nodes') == [node['uuid']] and binding.get('configProfileUuid') == old_profile
                and binding.get('configProfileInboundUuid') in old_inbounds):
            affected.append({key: copy.deepcopy(host[key]) for key in
                             ('uuid', 'nodes', 'inbound', 'address', 'port', 'isDisabled')})
    store.save('replacement.intent.json', {'node': before, 'hosts': affected})


def require_initial_installer(store):
    """A domain handed to replace.py must never be restored to its first IP."""
    if store.load('fleet-delegation.intent.json') or store.load('fleet-delegation.json'):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from spn_reserve_fleet.remote import require_local_control
        try:
            require_local_control(store)
        except ValueError as error:
            raise u.DeployError(str(error)) from None
    directory = store.path / 'replacements'
    if not directory.exists() and not directory.is_symlink():
        return
    u.need(not directory.is_symlink() and directory.is_dir(), 'Небезопасный журнал замены VPS.')
    for operation in directory.iterdir():
        u.need(not operation.is_symlink(), 'Небезопасный журнал замены VPS.')
        if operation.is_dir() and (operation / 'intent.json').exists():
            raise u.DeployError('Для этой установки уже начата замена VPS. Используйте '
                                'ops/universal-reality/replace.py --domain ДОМЕН --ip НОВЫЙ_IP --apply. '
                                'Начальный установщик не возвращает сервер на прежний IP.')


def prepare(api, store, args, base, panel_ip):
    require_initial_installer(store)
    zone, requested_domain = dns_options(args)
    saved = store.load('plan.json')
    if saved:
        u.need(saved.get('mode') in (None, 'direct'),
               'Эта установка использует VK-реле; продолжайте через ops/universal-vk/deploy.py.')
        u.need(saved['format'] == 1 and saved['ip'] == args.ip and saved['sshPort'] == args.ssh_port
               and saved['panel'] == base and saved['panelIp'] == panel_ip, 'Адрес VPS/панели отличается от сохранённой установки.')
        requested_transport = getattr(args, 'transport', None)
        u.need(requested_transport is None or (requested_transport == 'tls') == is_tls(saved),
               'Транспорт отличается от сохранённой установки. Для TLS создайте новую установку; прежний REALITY не мигрирует автоматически.')
        # Optional arguments may be omitted when continuing/reinstalling.
        u.need(args.name is None or args.name == saved['name'],
               'Для продолжения этого IP используйте прежнее --name или не указывайте его.')
        u.need(args.country is None or args.country == saved['country'],
               'Для продолжения этого IP используйте прежнее --country или не указывайте его.')
        u.need(requested_domain is None or requested_domain == saved.get('domain'),
               'Домен отличается от сохранённого плана; IP-установки автоматически не мигрируют.')
        if getattr(args, 'dns_zone_explicit', False) or zone != DEFAULT_DNS_ZONE:
            u.need(saved.get('dns', {}).get('zone') == zone,
                   'DNS-зона отличается от сохранённого плана.')
        if saved.get('domain') or saved.get('dns'):
            dns_settings = saved.get('dns')
            u.need(isinstance(dns_settings, dict) and dns_settings.get('provider') == 'cloudflare'
                   and dns_settings.get('ttl') == 60 and set(dns_settings) == {'provider', 'zone', 'ttl'},
                   'Сохранённые параметры DNS некорректны; их автоматически не меняю.')
            try:
                saved_zone = dns.normalize_name(dns_settings['zone'])
                saved_domain = dns.normalize_name(saved.get('domain'))
            except ValueError:
                raise u.DeployError('Некорректный домен в сохранённом плане.') from None
            u.need(saved_domain == saved['domain'] and saved_zone == dns_settings['zone']
                   and (saved_domain == saved_zone or saved_domain.endswith('.' + saved_zone)),
                   'Сохранённый домен не находится в сохранённой DNS-зоне.')
        if args.squad:
            u.need(u.choose_squads(api, args.squad) == saved['squads'], 'Выбранные сквады отличаются от прежней установки.')
        u.choose_squads(api, [], saved['squads'])
        validate_replacement(api, store, args)
        return saved
    # Reinstall is also valid after adding a new IP to an erased VPS. A new
    # target gets a new owned namespace; never borrow the former IP's objects.
    rows = {kind: api.rows(kind) for kind in ('config-profiles', 'nodes', 'hosts')}
    stage_replacement(api, store, args, rows['nodes'], rows['hosts'])
    squads = u.choose_squads(api, args.squad)
    code = suffix(rows)
    domain = requested_domain or 'n-' + code + '.' + zone
    u.need(not any(row.get('address') == domain for row in rows['nodes']),
           'Домен уже указан у другой ноды; владение ею не подтверждено.')
    # Saved first: an interruption before plan.json must never rotate the key.
    transport = getattr(args, 'transport', None) or 'tls'
    if transport == 'reality':
        reality_keys(store)
    plan = {
        'format': 1, 'id': uuid.uuid4().hex, 'code': code,
        'ip': args.ip, 'sshPort': args.ssh_port, 'panel': base, 'panelIp': panel_ip,
        'name': args.name or 'VPN', 'country': args.country or '', 'squads': squads,
        'transport': 'raw-' + transport + '-vision-safari-v1',
        'domain': domain,
        'dns': {'provider': 'cloudflare', 'zone': zone, 'ttl': 60},
    }
    if transport == 'tls':
        # Immutable capability marker keeps pre-site plans and their exact
        # profile intents reproducible when resuming an older installation.
        plan['tlsSiteVersion'] = 1
    store.save('plan.json', plan)
    return plan


class Deployment:
    def __init__(self, api, store, plan):
        self.api, self.store, self.plan = api, store, plan
        self.helper = u.Deployment(api, store, plan, {})
        self.restore_node_bindings = False

    def ensure(self, key, kind, body, predicate):
        """Never adopts an object merely because its endpoint/IP matches."""
        receipt = self.store.load(key + '.receipt.json')
        if receipt:
            if kind == 'hosts':
                actual = u.one(self.api.rows(kind), receipt['uuid'])
                expected = {**body, 'isDisabled': actual['isDisabled'], 'remark': actual['remark']}
                u.need(self.store.load(key + '.intent.json') == body and u.same(actual, expected),
                       'Технические параметры собственного хоста изменились; их не перезаписываю.')
                # Availability and a display label are user-controlled, not
                # proof that a different person owns this saved host UUID.
                return actual
            if kind == 'nodes':
                actual = u.one(self.api.rows(kind), receipt['uuid'])
                # An owner may correct a country flag while installation is
                # paused. Preserve that metadata on the receipted UUID while
                # still requiring the original intent and technical bindings.
                expected = {**body, 'countryCode': actual.get('countryCode')}
                u.need(self.store.load(key + '.intent.json') == body and u.same(actual, expected),
                       'Технические параметры собственной ноды изменились; их не перезаписываю.')
                return actual
            predicate = lambda row: row['uuid'] == receipt['uuid']
        return self.helper.ensure(key, kind, body, predicate)

    def replace_node(self, body):
        """Keep one controller/UUID: panel addresses are unique even if disabled."""
        intent = self.store.load('replacement.intent.json')
        before = intent['node']
        uid = before['uuid']
        current = u.one(self.api.rows('nodes'), uid)
        direct_controller(current)
        u.need(current.get('address') in (before['address'], endpoint_address(self.plan)) and current.get('port') == 2222
               and (replacement_matches(current, before) or replacement_after_matches(current, body, before)),
               'Прежняя нода изменилась после подтверждения; замену не выполняю.')
        hosts = self.api.rows('hosts')
        # Check every affected binding BEFORE any remote write. Availability
        # may already be false after a lost response; technical drift may not.
        for snapshot in intent['hosts']:
            host = u.one(hosts, snapshot['uuid'])
            expected = {key: value for key, value in snapshot.items() if key not in ('uuid', 'isDisabled')}
            u.need(u.same(host, expected), 'Привязка прежнего хоста изменилась; замену не выполняю.')
        self.store.save('node.intent.json', body)
        if not current.get('isDisabled'):
            self.api.disable_node(uid)
        u.need(u.one(self.api.rows('nodes'), uid).get('isDisabled') is True,
               'Приостановка прежнего контроллера не подтверждена.')
        for snapshot in intent['hosts']:
            host = u.one(self.api.rows('hosts'), snapshot['uuid'])
            expected = {key: value for key, value in snapshot.items() if key not in ('uuid', 'isDisabled')}
            u.need(u.same(host, expected), 'Привязка прежнего хоста изменилась во время замены.')
            if not host.get('isDisabled'):
                self.api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': True})
            u.need(u.one(self.api.rows('hosts'), host['uuid']).get('isDisabled') is True,
                   'Выключение прежнего хоста не подтверждено.')
        current = u.one(self.api.rows('nodes'), uid)
        direct_controller(current)
        u.need(replacement_matches(current, before) or replacement_after_matches(current, body, before),
               'Прежняя нода изменилась во время замены.')
        if not u.same(current, body):
            self.api('PATCH', '/nodes', {'uuid': uid, **body})
        current = u.one(self.api.rows('nodes'), uid)
        u.need(replacement_after_matches(current, body, before), 'Новые параметры выбранной ноды не подтверждены.')
        if current.get('isDisabled'):
            self.api.enable_node(uid)
        current = u.one(self.api.rows('nodes'), uid)
        u.need(replacement_after_matches(current, body, before) and current.get('isDisabled') is False,
               'Включение заменённой ноды не подтверждено.')
        self.store.save('replacement.receipt.json', {'uuid': uid, 'desiredHash': u.digest(body)})
        self.store.save('node.receipt.json', {'uuid': uid})
        print('Прежняя запись ноды настроена заново: ' + before['name'] + ' → ' + body['name']
              + '. UUID сохранён; прежний профиль не изменён.', flush=True)
        return current

    def provision(self):
        p, code = self.plan, self.plan['code']
        sni = p.get('sni', SNI)
        body = profile_body(p, self.store)
        initial = self.store.load('profile.intent.json')
        legacy = None if is_tls(p) else {'name': body['name'], 'config': legacy_profile_config(code, reality_keys(self.store), sni)}
        if is_tls(p) and not p.get('tlsSiteVersion') and self.store.load('tls-site-v1.intent.json'):
            profile = upgrade_tls_site_profile(self.api, self.store, p)
        elif legacy is not None and initial == legacy:
            # A narrow, durable upgrade for our former public listener. Never
            # edit a shared or foreign profile just because its name matches.
            receipt = self.store.load('profile.receipt.json')
            if receipt is None:
                profile = self.ensure('profile', 'config-profiles', legacy, lambda row: row['name'] == legacy['name'])
            else:
                profile = u.one(self.api.rows('config-profiles'), receipt['uuid'])
            node_receipt = self.store.load('node.receipt.json')
            own_node = (node_receipt or {}).get('uuid')
            references = [row['uuid'] for row in self.api.rows('nodes')
                          if row['configProfile']['activeConfigProfileUuid'] == profile['uuid']]
            u.need(all(uid == own_node for uid in references), 'Прежний профиль используется другой нодой; его не меняю.')
            u.need(profile['name'] == body['name'] and profile['config'] in (legacy['config'], body['config']),
                   'Техническая конфигурация прежнего профиля изменена; автоматическое обновление остановлено.')
            inbound_before = profile['inbounds'][0]['uuid']
            transition = {'uuid': profile['uuid'], 'beforeHash': u.digest(legacy), 'afterHash': u.digest(body)}
            self.store.save('profile-guard.intent.json', transition)
            if profile['config'] == legacy['config']:
                self.api('PATCH', '/config-profiles', {'uuid': profile['uuid'], 'config': body['config']})
            profile = u.one(self.api.rows('config-profiles'), profile['uuid'])
            u.need(profile['config'] == body['config'] and profile['inbounds'][0]['uuid'] == inbound_before,
                   'Обновление защищённого слушателя не подтверждено.')
            self.store.save('profile-guard.receipt.json', transition)
        else:
            profile = self.ensure('profile', 'config-profiles', body, lambda row: row['name'] == body['name'])
        u.need(len(profile['inbounds']) == 1 and profile['inbounds'][0]['tag'] == profile_tag(p),
               'Панель не подтвердила единственный инбаунд.')
        inbound = profile['inbounds'][0]['uuid']
        body = {
            'name': profile_tag(p), 'address': endpoint_address(p), 'port': 2222,
            'tags': ['AUTO_TLS' if is_tls(p) else 'AUTO_REALITY'],
            'note': 'Managed single TCP ' + ('TLS' if is_tls(p) else 'REALITY') + ' / Vision / Safari: ' + code,
            'configProfile': {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': [inbound]},
        }
        if p['country']:
            body['countryCode'] = p['country']
        receipt = self.store.load('node.receipt.json')
        if self.store.load('replacement.intent.json') and not receipt:
            return profile, self.replace_node(body), inbound
        if receipt and self.restore_node_bindings:
            current = u.one(self.api.rows('nodes'), receipt['uuid'])
            u.need(current['address'] == endpoint_address(p) and current['port'] == 2222,
                   'Собственная нода перенесена на другой адрес; её не перезаписываю.')
            if not u.same(current, {'configProfile': body['configProfile']}):
                self.api('PATCH', '/nodes', {'uuid': current['uuid'], 'configProfile': body['configProfile']})
                print('Привязка собственной ноды восстановлена после переустановки VPS.')
            if current.get('isDisabled'):
                self.api.enable_node(current['uuid'])
        for node in self.api.rows('nodes'):
            if node['address'] in (p['ip'], endpoint_address(p)):
                u.need(node['uuid'] == (receipt or {}).get('uuid') or (
                    self.store.load('node.intent.json') == body and u.same(node, body)),
                    'Адрес занят другой нодой; владение ею не подтверждено.')
        node = self.ensure('node', 'nodes', body, lambda row: row['name'] == body['name'])
        return profile, node, inbound

    def attach(self, inbound):
        for uid in self.plan['squads']:
            for _ in range(3):
                members = u.ids(u.one(self.api.rows('internal-squads'), uid)['inbounds'])
                if inbound in members:
                    break
                if u.ids(u.one(self.api.rows('internal-squads'), uid)['inbounds']) != members:
                    continue
                body = {'uuid': uid, 'inbounds': members + [inbound]}
                self.store.save('squad-' + uid + '-' + u.digest(body)[:12] + '.intent.json', body)
                actual = self.api('PATCH', '/internal-squads', body)
                u.need(set(u.ids(actual['inbounds'])) == set(body['inbounds']), 'Добавление в сквад не подтверждено.')
                break
            else:
                raise u.DeployError('Сквад одновременно редактируется; повторите ту же команду позже.')
        self.audit_audience(inbound)

    def audit_audience(self, inbound):
        actual = {row['uuid'] for row in self.api.rows('internal-squads') if inbound in u.ids(row['inbounds'])}
        # The owner may add this inbound to extra squads in the panel while an
        # installation is paused. Preserve those choices; only require ours.
        u.need(set(self.plan['squads']) <= actual, 'Инбаунд отсутствует в одном из выбранных сквадов.')

    def host_body(self, profile, node, inbound):
        return {
            'remark': self.plan['name'] + ' • TCP ' + self.plan['code'],
            'address': endpoint_address(self.plan), 'port': 443, 'nodes': [node['uuid']],
            'inbound': {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbound},
            'sni': self.plan['domain'] if is_tls(self.plan) else self.plan.get('sni', SNI),
            'alpn': 'h2', 'fingerprint': FINGERPRINT, 'securityLayer': 'TLS' if is_tls(self.plan) else 'DEFAULT',
            'isDisabled': True, 'isHidden': False, 'internalSquads': {'mode': 'EXCLUDE', 'squads': []},
            'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
            'tags': ['AUTO_TLS' if is_tls(self.plan) else 'AUTO_REALITY'],
        }
    def host(self, profile, node, inbound):
        body = self.host_body(profile, node, inbound)
        return self.ensure('tcp', 'hosts', body, lambda row:
                           (row.get('inbound') or {}).get('configProfileInboundUuid') == inbound)

    def publish(self, host):
        self.helper.publish({'tcp': host})
        actual = u.one(self.api.rows('hosts'), host['uuid'])
        u.need(actual['isDisabled'] is False and actual['fingerprint'] == FINGERPRINT,
               'Включение хоста или Safari не подтверждено.')


def retire_owned_aliases(api, store, plan, assigned, home=None):
    """An erased multi-IP guest must not have two panel controllers racing it.

    Only a local receipt + exact original payload + physically assigned old IP
    prove ownership. Never adopts or disables a foreign node by IP alone.
    """
    roots = {state_directory(plan['panel'], plan['ip'], plan['sshPort'], home).parent, store.path.parent}
    saved = {}
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for directory in root.iterdir():
            if directory == store.path or not directory.is_dir() or directory.is_symlink():
                continue
            try:
                previous = u.Store(directory)
                old_plan = previous.load('plan.json')
                receipt = previous.load('node.receipt.json')
                if old_plan and receipt and old_plan.get('panel') == plan['panel']:
                    saved[receipt['uuid']] = (previous, old_plan)
            except u.DeployError:
                continue  # Untrusted state can never authorize retirement.
    retired = []
    for node in api.rows('nodes'):
        pending = store.load('retire-' + node['uuid'] + '.intent.json')
        receipt = store.load('retire-' + node['uuid'] + '.receipt.json')
        known = saved.get(node['uuid'])
        physical_ip = known[1]['ip'] if known else node['address']
        if (node['address'] in (plan['ip'], endpoint_address(plan)) or physical_ip not in assigned
                or (node.get('isDisabled') and (not pending or receipt))):
            continue
        u.need(node['uuid'] in saved, 'На этой же VPS есть другая включённая нода панели (' + node['address']
               + '), владение которой не подтверждено. Автоматически её не выключаю.')
        old, old_plan = saved[node['uuid']]
        # Exclude a simultaneous resume that could restore/re-enable the old
        # controller. Busy owned state must fail closed, not be skipped.
        with old.lock():
            u.need(old.load('plan.json') == old_plan and old.load('node.receipt.json') == {'uuid': node['uuid']},
                   'Квитанция прежней установки изменилась; её ноду не выключаю.')
            node = u.one(api.rows('nodes'), node['uuid'])
            original_node = old.load('node.intent.json')
            u.need(old_plan.get('ip') in assigned and node['address'] == endpoint_address(old_plan) and original_node
                   and node['address'] == original_node['address'] and node['port'] == original_node['port'] == 2222,
                   'Адрес или управляющий порт прежней ноды изменён; её не выключаю.')
            # The owner may have selected another profile while troubleshooting.
            # UUID receipt + same guest's assigned IP + unchanged control port
            # suffice to retire this controller; neither profile is touched.
            marker = {'node': node['uuid'], 'previousIp': old_plan['ip'], 'newIp': plan['ip'],
                      'reason': 'same-assigned-guest-avoid-two-panel-controllers'}
            store.save('retire-' + node['uuid'] + '.intent.json', marker)
            if not node.get('isDisabled'):
                api.disable_node(node['uuid'])
            u.need(u.one(api.rows('nodes'), node['uuid']).get('isDisabled') is True, 'Выключение старой ноды не подтверждено.')
            host_receipt = old.load('tcp.receipt.json')
            if host_receipt:
                host = u.one(api.rows('hosts'), host_receipt['uuid'])
                original_host = old.load('tcp.intent.json')
                if (host.get('nodes') == [node['uuid']] and original_host
                        and host.get('address') == original_host.get('address')
                        and (host.get('address') == endpoint_address(old_plan) or host.get('address') in assigned)
                        and not host['isDisabled']):
                    api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': True})
                    u.need(u.one(api.rows('hosts'), host['uuid'])['isDisabled'] is True, 'Выключение старого хоста не подтверждено.')
            store.save('retire-' + node['uuid'] + '.receipt.json', marker)
        retired.append(node['uuid'])
        print('Прежняя запись этой же VPS выключена: ' + node['name'] + '. Не удалена.')
    return retired


def pinned_known_hosts(ip, port, fingerprint, store):
    """An externally verified fingerprint, not keyscan alone, authorizes a new key."""
    u.need(re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint) is not None, 'Неверный формат SHA256 отпечатка SSH.')
    result = subprocess.run(['ssh-keyscan', '-T', '10', '-p', str(port), '-t', 'ed25519', ip],
                            capture_output=True, timeout=35, check=False)
    u.need(result.returncode == 0 and 0 < len(result.stdout) < 16000, 'Не удалось получить SSH-ключ для сверки.')
    keys = set()
    for line in result.stdout.decode('ascii', errors='strict').splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[1] != 'ssh-ed25519':
            continue
        try:
            raw = base64.b64decode(fields[2], validate=True)
        except ValueError:
            continue
        actual = 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
        if hmac.compare_digest(actual, fingerprint):
            keys.add(fields[2])
    u.need(len(keys) == 1, 'SSH-ключ не совпал с отпечатком. Вход и изменения остановлены.')
    host = ip if port == 22 else f'[{ip}]:{port}'
    path = store.path / ('known-host-' + u.digest(fingerprint)[:16])
    # Store validates owner, mode, symlinks and exact content on repeated use.
    store.save(path.name + '.json', {'host': host, 'key': next(iter(keys)), 'fingerprint': fingerprint})
    data = (host + ' ssh-ed25519 ' + next(iter(keys)) + '\n').encode()
    if path.exists() or path.is_symlink():
        u.need(not path.is_symlink() and path.stat().st_uid == os.geteuid()
               and path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink == 1
               and path.read_bytes() == data, 'Небезопасный файл закреплённого SSH-ключа.')
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    return path


class SSH:
    def __init__(self, plan, known_hosts=None, control_socket=None):
        self.plan = plan
        # Even a user's broad SendEnv SSH configuration cannot forward this
        # local control-plane credential to the guest.
        self.environment = {key: value for key, value in os.environ.items() if key != 'CLOUDFLARE_API_TOKEN'}
        self.tmp = tempfile.TemporaryDirectory(prefix='spn-reality-', dir='/private/tmp' if sys.platform == 'darwin' else None)
        self.external = control_socket is not None
        self.control = str(Path(control_socket).expanduser().absolute()) if self.external else str(Path(self.tmp.name) / 'ssh')
        if self.external:
            path = Path(self.control)
            u.need(not any(p.is_symlink() for p in (path, *path.parents)) and path.exists(), 'Нет безопасного SSH control socket.')
            info, parent = path.stat(), path.parent.stat()
            u.need(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
                   and parent.st_uid == os.geteuid() and stat.S_IMODE(parent.st_mode) == 0o700,
                   'SSH control socket должен быть вашим, 0600, в приватной папке 0700.')
        self.common = ['ssh', '-p', str(plan['sshPort']), '-S', self.control,
                       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'ConnectTimeout=15']
        if plan.get('batchMode'):
            self.common += ['-o', 'BatchMode=yes', '-o', 'NumberOfPasswordPrompts=0']
        if known_hosts:
            self.common += ['-o', 'UserKnownHostsFile=' + str(known_hosts), '-o', 'GlobalKnownHostsFile=/dev/null',
                            '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlgorithms=ssh-ed25519']
        else:
            self.common += ['-o', 'StrictHostKeyChecking=ask']

    def __enter__(self):
        try:
            if self.external:
                result = subprocess.run(self.common + ['-o', 'ProxyCommand=/usr/bin/false', '-O', 'check', 'root@' + self.plan['ip']],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.environment)
                u.need(result.returncode == 0, 'Временное SSH-соединение закрыто; откройте его снова.')
                return self
            result = subprocess.run(self.common + ['-M', '-N', '-f', '-o', 'ControlPersist=600', 'root@' + self.plan['ip']],
                                    env=self.environment)
            u.need(result.returncode == 0, 'SSH не подключился. После переустановки сверьте новый ключ и используйте '
                   '--host-key-sha256; known_hosts автоматически не удаляется. См. README.md.')
            return self
        except BaseException:
            self.tmp.cleanup()
            raise

    def __exit__(self, *args):
        if not self.external:
            subprocess.run(self.common + ['-o', 'ProxyCommand=/usr/bin/false', '-O', 'exit', 'root@' + self.plan['ip']],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.environment)
        self.tmp.cleanup()

    def run(self, command, data=None, capture=False, network_retries=0):
        # If the master died, never silently reconnect via another key/route.
        for attempt in range(network_retries + 1):
            result = subprocess.run(self.common + ['-o', 'ControlMaster=no', '-o', 'BatchMode=yes',
                                     '-o', 'ProxyCommand=/usr/bin/false', 'root@' + self.plan['ip'], command],
                                    input=data, stdout=subprocess.PIPE if capture else None,
                                    stderr=subprocess.PIPE if network_retries else None, env=self.environment)
            diagnostic = (getattr(result, 'stderr', None) or b'').decode(errors='replace')
            if diagnostic:
                sys.stderr.write(diagnostic)
            if (result.returncode and attempt < network_retries
                    and 'HTTPS' in diagnostic and 'Stopped:' in diagnostic):
                # Only the fixed, read-only external HTTPS check is retried.
                # The owned route helper already rolled back its own failed
                # transition; unknown SSH outcomes/ownership errors stop now.
                print('Повтор внешней HTTPS-проверки ' + str(attempt + 2) + '/' + str(network_retries + 1), flush=True)
                time.sleep(.5)
                continue
            break
        u.need(result.returncode == 0, 'Шаг VPS остановлен. Повторите ту же команду: прогресс и объекты панели сохранены.')
        return result.stdout

    def upload(self, files):
        stage = '/root/spn-reality-managed-' + self.plan['id']
        preferred_source = files.get('preferred_source.py')
        if preferred_source is not None:
            files = {name: data for name, data in files.items() if name != 'preferred_source.py'}
            files.update({'preferred_source.incoming.py': preferred_source,
                          'source_bootstrap.py': (HERE / 'source_bootstrap.py').read_bytes()})
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            for name, data in files.items():
                u.need(re.fullmatch(r'[A-Za-z0-9_.-]+', name) is not None, 'Небезопасное имя файла установки.')
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o600
                archive.addfile(info, io.BytesIO(data))
        self.run(f'test ! -L {stage} && install -d -m 700 {stage} && '
                 f'test "$(stat -c %u:%a {stage})" = 0:700 && tar -xmf - --no-same-owner -C {stage}', buffer.getvalue())
        if preferred_source is not None:
            command = ['python3', stage + '/source_bootstrap.py', self.plan['id'],
                       hashlib.sha256(preferred_source).hexdigest()]
            self.run(shlex.join(command))

    def action(self, name, capture=False):
        p = self.plan
        fields = ['bash', '/root/spn-reality-managed-' + p['id'] + '/managed-node.sh', name,
                  p.get('nodeIp', p['ip']), p['panelIp'], str(p['sshPort']), p['id']]
        if is_tls(p):
            if p.get('tlsSiteVersion'):
                fields.append(str(p['tlsSiteVersion']))
            fields.append(p['domain'])
        return self.run(shlex.join(fields), capture=capture)

    def source(self, name, capture=False):
        stage = '/root/spn-reality-managed-' + self.plan['id']
        fields = ['python3', stage + '/preferred_source.py', name, '--ip', self.plan['ip'], '--state-dir', stage]
        return self.run(shlex.join(fields), capture=capture, network_retries=2 if name in ('apply', 'verify') else 0)

    def assigned_ipv4(self):
        rows = json.loads(self.run('ip -j -4 address show up scope global', capture=True))
        return {entry['local'] for row in rows for entry in row.get('addr_info', [])
                if entry.get('family') == 'inet' and ipaddress.IPv4Address(entry['local']).is_global}

    @contextlib.contextmanager
    def test_forward(self):
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        rule = f'127.0.0.1:{port}:127.0.0.1:443'
        command = self.common + ['-o', 'ProxyCommand=/usr/bin/false']
        result = subprocess.run(command + ['-O', 'forward', '-L', rule, 'root@' + self.plan['ip']],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=self.environment)
        u.need(result.returncode == 0, 'Не удалось открыть локальный SSH-канал для проверки настройки.')
        try:
            yield port
        finally:
            subprocess.run(command + ['-O', 'cancel', '-L', rule, 'root@' + self.plan['ip']],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.environment)


def render_files(plan, secret):
    u.need(isinstance(secret, str) and len(secret) >= 20 and re.fullmatch(r'[A-Za-z0-9_/+=-]+', secret),
           'Некорректный служебный ключ ноды.')
    compose = f'''services:
  remnanode:
    container_name: remnanode
    hostname: remnanode
    image: {IMAGE}
    restart: unless-stopped
    network_mode: host
    labels:
      spn.deploy.id: "{plan['id']}"
    env_file:
      - node.env
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    logging:
      driver: json-file
      options:
        max-size: 20m
        max-file: '3'
'''
    if is_tls(plan):
        compose = compose.replace('    env_file:\n', '    volumes:\n      - /etc/letsencrypt:/etc/letsencrypt:ro\n    env_file:\n')
        files = {'managed-node.sh': managed_script(plan),
                '.spn-deployment': (plan['id'] + '\n').encode(),
                'docker-compose.yml': compose.encode(),
                'node.env': ('NODE_PORT=2222\nSECRET_KEY=' + secret + '\n').encode()}
        if plan.get('tlsSiteVersion'):
            u.need(plan['tlsSiteVersion'] == 1, 'Неподдерживаемая версия HTTPS-сайта.')
            files.update(tls_site_files(plan))
        return {**files, **security.files()}
    return {**security.files(reality=True), 'managed-node.sh': managed_script(plan),
            'guard.py': (HERE / 'guard.py').read_bytes(),
            'haproxy-reality.cfg': (HERE / 'haproxy-reality.cfg').read_bytes(),
            'core_fd_health.py': (HERE / 'core_fd_health.py').read_bytes(),
            '.spn-deployment': (plan['id'] + '\n').encode(),
            'docker-compose.yml': compose.encode(),
            'node.env': ('NODE_PORT=2222\nSECRET_KEY=' + secret + '\n').encode()}


def tls_site_files(plan):
    """A dedicated loopback-only nginx instance; HTTP-01 keeps public TCP80."""
    u.need(re.fullmatch(r'[0-9a-f]{32}', plan['id']) is not None, 'Некорректный код установки HTTPS-сайта.')
    site = '/var/lib/spn-tls-site-' + plan['id']
    nginx = '''user www-data;
worker_processes 1;
pid /run/spn-tls-site-DEPLOY_ID/nginx.pid;
error_log stderr warn;
events { worker_connections 1024; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log off;
    server_tokens off;
    sendfile on;
    keepalive_timeout 65;
    client_body_temp_path /run/spn-tls-site-DEPLOY_ID/client;
    proxy_temp_path /run/spn-tls-site-DEPLOY_ID/proxy;
    fastcgi_temp_path /run/spn-tls-site-DEPLOY_ID/fastcgi;
    uwsgi_temp_path /run/spn-tls-site-DEPLOY_ID/uwsgi;
    scgi_temp_path /run/spn-tls-site-DEPLOY_ID/scgi;
    server {
        listen 127.0.0.1:18080;
        listen 127.0.0.1:18081 http2;
        server_name _;
        root SITE_ROOT;
        index index.html;
        charset utf-8;
        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy no-referrer always;
        add_header Content-Security-Policy "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'" always;
        location / { try_files $uri $uri/ =404; }
        location ~ /\\. { deny all; }
    }
}
'''.replace('DEPLOY_ID', plan['id']).replace('SITE_ROOT', site)
    unit = '''[Unit]
Description=Notes website
After=network.target

[Service]
Type=simple
ExecStartPre=/usr/sbin/nginx -t -c /opt/spn-reality-node/tls-site-nginx.conf
ExecStart=/usr/sbin/nginx -c /opt/spn-reality-node/tls-site-nginx.conf -g "daemon off;"
ExecReload=/bin/kill -s HUP $MAINPID
KillSignal=SIGQUIT
TimeoutStopSec=10
Restart=on-failure
RestartSec=3
RuntimeDirectory=spn-tls-site-DEPLOY_ID
RuntimeDirectoryMode=0755
PrivateTmp=true
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/run/spn-tls-site-DEPLOY_ID

[Install]
WantedBy=multi-user.target
'''.replace('DEPLOY_ID', plan['id'])
    policy = '''#!/bin/sh
case "${1:-}" in nginx|nginx.service) exit 101 ;; esac
exit 0
'''
    return {'tls-site-nginx.conf': nginx.encode(),
            'tls-site.service': unit.encode(),
            'tls-site-policy.sh': policy.encode(),
            'tls-site-index.html': (HERE / 'tls-site-index.html').read_bytes()}


def panel_ipv4(base):
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(u.urlsplit(base).hostname, 443,
                                                         socket.AF_INET, socket.SOCK_STREAM)}
    except OSError:
        raise u.DeployError('Не удалось определить IPv4 панели.') from None
    u.need(len(addresses) == 1, 'Для ограничения порта 2222 у панели должен быть один прямой IPv4 без CDN.')
    return public_ipv4(addresses.pop())


def validate(args):
    args.ip = public_ipv4(args.ip)
    dns_options(args)
    u.need(1 <= args.ssh_port <= 65535 and args.ssh_port not in (443, 2222), 'Недопустимый SSH-порт.')
    u.need(args.name is None or (1 <= len(args.name) <= 60 and not any(ord(c) < 32 or ord(c) == 127 for c in args.name)),
           '--name: от 1 до 60 символов без переносов строк.')
    u.need(args.country is None or re.fullmatch('[A-Z]{2}', args.country), '--country: двухбуквенный код, например FI.')
    u.need(5 <= args.wait <= 600, '--wait: от 5 до 600 секунд.')
    if args.replace_node:
        try:
            valid = str(uuid.UUID(args.replace_node)) == args.replace_node
        except ValueError:
            valid = False
        u.need(valid, '--replace-node: нужен полный UUID выбранной прежней ноды.')


def run(args):
    validate(args)
    if not args.apply:
        zone, domain = dns_options(args)
        print(json.dumps({'planOnly': True, 'ip': args.ip, 'domain': domain or 'n-<code>.' + zone,
                          'dnsZone': zone, 'dnsType': 'A', 'dnsProxied': False, 'dnsTtl': 60,
                          'create': ['one DNS-only A record (new direct plans only)', 'one ' + (args.transport or 'tls').upper() + ' profile', 'one inbound', 'one node', 'one host'],
                          'fingerprint': FINGERPRINT, 'port': 443, 'randomCodeLength': 6,
                          'needsDomain': True, 'needsCertificate': args.transport != 'reality', 'changes': False}, ensure_ascii=False))
        return
    settings = u.read_settings(Path(args.env_file).expanduser())
    base = u.panel_url(args.panel_url or settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    token = settings.get('REMNAWAVE_API_TOKEN') if settings.get('REMNAWAVE_BASE_URL', '').rstrip('/') == base else None
    if not token:
        token = getpass.getpass('API-токен панели (не передаётся на VPS): ')
    api = API(base, token)
    # Fail before server/panel mutations if the real-traffic test cannot run.
    try:
        subscription = probe.private_file(Path(args.test_subscription).expanduser())
        probe.subscription_url(subscription)
        engine = probe.select_engine(args.engine)
        probe.validate_interface(args.interface, physical=args.test_route in ('physical', 'auto'))
    except probe.ProbeError as error:
        raise u.DeployError(str(error)) from None
    store = u.Store(Path(args.state_dir).expanduser() if args.state_dir else state_directory(base, args.ip, args.ssh_port))
    with store.lock():
        print('Проверяю сохранённую установку и записи панели…', flush=True)
        plan = prepare(api, store, args, base, panel_ipv4(base))
        # Read-only DNS conflict/credential checks precede even an SSH upload.
        dns_context = dns_preflight(store, plan, args, settings)
        print('Код установки: ' + plan['code'])
        deployment = Deployment(api, store, plan)
        # Legacy complete.json only proved publication, not working traffic.
        completed = store.load('activation.verified.json') is not None
        # Always inspect/install the actual VPS, even after an earlier success:
        # the host may have been erased since that receipt was written.
        known_hosts = None
        if not args.ssh_control:
            try:
                known_hosts = trust.resolve(plan['ip'], plan['sshPort'], store, expected=args.host_key_sha256)
            except trust.TrustError as error:
                raise u.DeployError(str(error)) from None
        print('Подключение к VPS. Пароль вводится в SSH; изменённые ключи автоматически не принимаются.')
        runtime_plan = tls_runtime_plan(plan, store)
        with SSH(runtime_plan, known_hosts, args.ssh_control) as ssh:
            bootstrap = {'managed-node.sh': managed_script(plan),
                         'preferred_source.py': (HERE / 'preferred_source_policy.py').read_bytes()}
            if is_tls(runtime_plan) and runtime_plan.get('tlsSiteVersion'):
                bootstrap['tls-site-policy.sh'] = tls_site_files(runtime_plan)['tls-site-policy.sh']
            ssh.upload(bootstrap)
            ssh.action('preflight')
            # Package post-install hooks can restart networkd and restore its
            # configured route. Finish every package before snapshotting it.
            ssh.action('packages')
            network = json.loads(ssh.source('check', capture=True))
            if network.get('needsChange'):
                print('Настраиваю исходящий IPv4: ' + network['currentSource'] + ' → ' + plan['ip'])
            ssh.source('apply')
            assigned = ssh.assigned_ipv4()
            u.need(plan['ip'] in assigned, 'Выбранный IPv4 не назначен доверенной VPS; DNS не создаю.')
            ensure_dns(store, plan, dns_context, args.wait)
            retire_owned_aliases(api, store, plan, assigned)
            identity = json.loads(ssh.action('identity', capture=True))
            u.need(re.fullmatch('[0-9a-f]{32}', identity['machineId']) and
                   re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', identity['sshFingerprint']), 'Некорректная идентификация VPS.')
            store.save('machine-' + u.digest(identity) + '.json', identity)
            fresh_guest = identity.get('ownedInstallation') is False
            deployment.restore_node_bindings = fresh_guest or args.reinstall
            profile, node, inbound = deployment.provision()
            secret = store.load('node-secret.json')
            if secret is None:
                secret = {'value': api('GET', '/keygen')['secretKey']}
                store.save('node-secret.json', secret)
            ssh.upload(render_files(tls_runtime_plan(plan, store), secret['value']))
            ssh.action('install')
            if not is_tls(plan):
                security.sni(ssh)
            security.guard(ssh, plan['ip'], verify=True)
            deployment.helper.wait_ready(node, args.wait)
            # Push the selected audience before checking Xray readiness. A
            # connected control API alone does not prove that its core has
            # already received the subscriptions/inbound configuration.
            deployment.attach(inbound)
            ssh.action('verify')
            ssh.source('verify')
            host = deployment.host(profile, node, inbound)
            keys, probe_options = probe_parameters(plan, store)
            routes = ('physical', 'ssh') if args.test_route == 'auto' else (args.test_route,)
            proof, test_route = None, None
            for test_route in routes:
                label = {'physical': 'прямой физический путь…',
                         'system': 'через текущее подключение Mac (возможно, через действующий VPN)…',
                         'ssh': 'проверка настройки через временный SSH-канал; прямой доступ отдельно…'}
                print('Проверка VPN: ' + label[test_route], flush=True)
                try:
                    forward = ssh.test_forward() if test_route == 'ssh' else contextlib.nullcontext(None)
                    with forward as relay_port:
                        proof = probe.run(plan['ip'], keys['public'], keys['short'], subscription,
                                          engine=engine, interface=args.interface, route=test_route, relay_port=relay_port,
                                          **probe_options)
                except probe.ProbeError as error:
                    if test_route != routes[-1]:
                        print('Прямой путь не подтверждён: ' + str(error), flush=True)
                        print('Проверю саму настройку через SSH; это не докажет прямой доступ или обход белых списков.', flush=True)
                        continue
                    raise u.DeployError(str(error) + ' Установка сохранена; работа VPN не подтверждена.') from None
                store.save('vpn-test-' + str(time.time_ns()) + '.json', proof)
                if proof.get('passed') is True or test_route == routes[-1]:
                    break
                print('Прямой VPN-тест не прошёл; проверю настройку через SSH.', flush=True)
            proof = proof or {}
            print('VPN-тест: ' + str(sum(row.get('passed') is True for row in proof.get('results', [])))
                  + '/' + str(proof.get('tests', 0)), flush=True)
            u.need(proof.get('passed') is True and proof.get('tests') == 9
                   and ((test_route == 'physical' and proof.get('physicalSourceObserved') is True)
                        or (test_route in ('system', 'ssh') and proof.get('testRoute') == test_route
                            and proof.get('directReachabilityVerified') is False))
                   and len(proof.get('results', [])) == 9
                   and all(row.get('passed') is True and row.get('clientAlive') is True for row in proof['results']),
                   'Реальный VPN-тест не прошёл. Хост не включаю; повторная установка и новый IP вслепую не нужны. '
                   'Результат сохранён в приватной папке установки.')
            pending_activation = store.load('activation.pending.json')
            keep_disabled = completed and host['isDisabled'] and not args.reinstall and not fresh_guest and not pending_activation
            if not keep_disabled:
                was_disabled = host['isDisabled']
                store.save('activation.pending.json', {'host': host['uuid'], 'code': plan['code']})
                deployment.publish(host)
                try:
                    for attempt in range(3):
                        try:
                            exported = probe.verify_export(plan['ip'], keys['public'], keys['short'], subscription,
                                                           interface=args.interface, route=test_route,
                                                           endpoint_address=endpoint_address(plan), **probe_options)
                            if exported.get('passed') is not True:
                                raise probe.ProbeError('Параметры фактической выгрузки подписки не подтверждены.')
                            break
                        except probe.ProbeError:
                            if attempt == 2:
                                raise
                            time.sleep(1)
                    store.save('export-test-' + str(time.time_ns()) + '.json', exported)
                    store.save('activation.verified.json', {'host': host['uuid'], 'code': plan['code'], 'trafficAndExportVerified': True})
                    # Only this validated, owned operation's pending marker is
                    # mutable. All attempts and proofs remain append-only.
                    u.need(store.load('activation.pending.json') == {'host': host['uuid'], 'code': plan['code']},
                           'Маркер подтверждения публикации изменён.')
                    (store.path / 'activation.pending.json').unlink()
                except probe.ProbeError as error:
                    if was_disabled:
                        # Retract only our just-published host, never another one.
                        current = deployment.host(profile, node, inbound)
                        if not current['isDisabled']:
                            api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': True})
                    raise u.DeployError(str(error) + ' Фактическая выгрузка подписки не подтверждена.') from None
            if store.load('activation.verified.json'):
                enroll_controller_key(ssh, store, args)
        heading = 'Проверено, хост оставлен выключенным: ' if keep_disabled else (
            'Готово: ' if test_route == 'physical' else ('Настройка создана и проверена через SSH: ' if test_route == 'ssh'
                                                        else 'Настройка создана и проверена через текущее подключение: '))
        if store.load('activation.verified.json'):
            register_fleet(store, args)
        print(heading + host['remark'])
        print('Один TCP TLS / Vision, Safari, порт 443. Сертификат Let’s Encrypt с автопродлением.' if is_tls(plan)
              else 'Один TCP REALITY / Vision, Safari, порт 443. Сертификат не нужен.')
        if plan.get('dns'):
            print('DNS-only A: ' + plan['domain'] + ' → ' + plan['ip'] + ', TTL 60; SNI '
                  + (plan['domain'] if is_tls(plan) else plan.get('sni', SNI)) + '.')
        print('Реальный VPN-тест: 9/9, HTTPS, выходной IP, загрузка файла и параллельные запросы. Обновите подписку в Happ/INCY.')
        if test_route != 'physical':
            print('Прямой доступ пока не подтверждён. Этот прямой TCP-сервер не обещает обход белых списков.')
        print('Приватное состояние установки: ' + str(store.path))


class DNSZoneAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.dns_zone_explicit = True


def parser(*, test_route_help=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ip', required=True)
    p.add_argument('--domain', help='New direct plans: endpoint domain; default n-<six-character-code>.<dns-zone>')
    p.add_argument('--transport', choices=('tls', 'reality'), default=None,
                   help='New direct plans default to TLS; saved plans retain their transport')
    p.add_argument('--fleet-dir', help='Private reserve-IP panel directory; default ~/.spn-reserve-fleet')
    p.add_argument('--fleet-controller-file', help='Private SSH controller config; default ~/.config/spn/reserve-controller.json if present')
    p.add_argument('--dns-zone', default=DEFAULT_DNS_ZONE, action=DNSZoneAction,
                   help='Cloudflare zone for new direct plans (default: wayspn.com); immutable on resume')
    p.set_defaults(dns_zone_explicit=False)
    p.add_argument('--cloudflare-token-file', help='Local private 0600 token file path (never the token itself)')
    p.add_argument('--name', help='Display name; omit on resume to use the saved name')
    p.add_argument('--country', help='Optional two-letter metadata, e.g. FI')
    p.add_argument('--ssh-port', type=int, default=22)
    p.add_argument('--squad', action='append', default=[], help='Repeat a squad name or UUID; omitted: interactive selection')
    p.add_argument('--panel-url')
    p.add_argument('--env-file', default=str(ROOT / '.env'))
    p.add_argument('--state-dir')
    p.add_argument('--wait', type=int, default=120)
    p.add_argument('--host-key-sha256', help='ED25519 SHA256 fingerprint verified in the VPS hosting console')
    p.add_argument('--reinstall', action='store_true', help='Compatible with a new IP or an erased VPS; existing owned objects are reused')
    p.add_argument('--replace-node', help='Explicitly reconfigure this existing node UUID at the target IP on a clean VPS; preserve old profiles')
    p.add_argument('--test-subscription', default=str(ROOT / '.vpn-test-link.txt'), help='Private 0600 file containing an existing subscription link')
    p.add_argument('--engine', help='Optional path to the checksum-pinned macOS ARM64 Xray26.9.9 binary')
    p.add_argument('--interface', default='en0', help='Physical Mac network interface for the real VPN test (never utun)')
    p.add_argument('--test-route', choices=('auto', 'physical', 'system', 'ssh'), default='auto',
                   help=test_route_help or 'auto: direct test, then explicitly labelled SSH configuration-only test if restrictions interfere')
    p.add_argument('--ssh-control', help=argparse.SUPPRESS)
    p.add_argument('--apply', action='store_true')
    return p


if __name__ == '__main__':
    try:
        run(parser().parse_args())
    except (u.DeployError, KeyboardInterrupt) as error:
        print('Остановлено: ' + (str(error) if isinstance(error, u.DeployError) else
              'прервано пользователем; уже записанное состояние сохранено. Повторите ту же команду.'), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        print('Остановлено: ' + type(error).__name__ + '; детали скрыты для защиты ключей. Прогресс сохранён.', file=sys.stderr)
        raise SystemExit(2)
