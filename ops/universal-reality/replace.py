#!/usr/bin/env python3
"""Replace one DNS-backed TLS/REALITY guest without recreating panel identities.

Without --apply this prints a local, network-free plan. This program never
buys/deletes a VPS, creates a panel object, changes keys, or publishes a host.
The original installation remains immutable; replacement journals are private.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import getpass
import importlib.util
import json
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('spn_reality_replacement_deploy', HERE / 'deploy.py')
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)
u = d.u
_spec = importlib.util.spec_from_file_location('spn_tls_replacement_certificate', HERE / 'tls_replacement_certificate.py')
tls_certificate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tls_certificate)


def find_original(base, domain, state_dir=None, home=None):
    """Exact domain lookup; never guess by IP, label, or the first directory."""
    if state_dir:
        path = Path(state_dir).expanduser().absolute()
        u.need(path.is_dir(), '--state-dir должен указывать на существующую исходную установку.')
        store = u.Store(path)
        plan = store.load('plan.json')
        u.need(isinstance(plan, dict) and plan.get('domain') == domain and plan.get('panel') == base,
               '--state-dir не относится к этому домену/панели; IP/VK-планы не мигрируют.')
        return store
    home = Path.home() if home is None else Path(home)
    root = home / '.spn-reality-deploy' / u.digest(base)[:12]
    u.need(not root.is_symlink(), 'Каталог исходных установок не должен быть симлинком.')
    matches = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.is_symlink() or not path.is_dir():
                continue
            # Store validates permissions before parsing any private state.
            candidate = u.Store(path)
            plan = candidate.load('plan.json')
            if isinstance(plan, dict) and plan.get('domain') == domain and plan.get('panel') == base:
                matches.append(candidate)
    u.need(len(matches) == 1,
           'Для домена нужна ровно одна исходная локальная установка; найдено: ' + str(len(matches))
           + '. Укажите её через --state-dir. IP-only/VK-планы здесь не поддерживаются.')
    return matches[0]


def _uid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def validate_original(store, domain, base):
    plan = store.load('plan.json')
    u.need(isinstance(plan, dict) and plan.get('format') == 1 and plan.get('panel') == base
           and plan.get('domain') == domain and plan.get('mode') in (None, 'direct')
           and plan.get('transport') in ('raw-reality-vision-safari-v1', 'raw-tls-vision-safari-v1'),
           'Нужен исходный прямой DNS-план TLS/REALITY; IP-only/VK-планы автоматически не мигрируют.')
    dns = plan.get('dns')
    u.need(isinstance(dns, dict) and dns.get('provider') == 'cloudflare' and dns.get('ttl') == 60
           and set(dns) == {'provider', 'zone', 'ttl'}, 'Исходный DNS-план не подтверждён.')
    try:
        valid_dns = (d.dns.normalize_name(domain) == domain and d.dns.normalize_name(dns['zone']) == dns['zone']
                     and (domain == dns['zone'] or domain.endswith('.' + dns['zone'])))
    except ValueError:
        valid_dns = False
    u.need(valid_dns and re.fullmatch('[0-9a-f]{32}', plan.get('id', ''))
           and re.fullmatch('[a-z0-9]{6}', plan.get('code', '')), 'Некорректная идентичность исходного плана.')
    d.public_ipv4(plan['ip'])
    for name in ('profile', 'node', 'tcp'):
        receipt = store.load(name + '.receipt.json')
        u.need(isinstance(receipt, dict) and _uid(receipt.get('uuid'))
               and isinstance(store.load(name + '.intent.json'), dict),
               'Сначала завершите исходный deploy.py: отсутствует квитанция ' + name + '.')
    activation = store.load('activation.verified.json')
    u.need(isinstance(activation, dict) and activation.get('trafficAndExportVerified') is True
           and activation.get('host') == store.load('tcp.receipt.json')['uuid']
           and activation.get('code') == plan['code'] and not store.load('activation.pending.json'),
           'Сначала завершите исходный deploy.py: активация и выгрузка подписки не подтверждены.')
    secret = store.load('node-secret.json')
    u.need(isinstance(secret, dict) and isinstance(secret.get('value'), str)
           and len(secret['value']) >= 20 and re.fullmatch('[A-Za-z0-9_/+=-]+', secret['value']),
           'Сначала завершите исходный deploy.py: служебный ключ отсутствует или повреждён; новый не создаю.')
    if d.is_tls(plan):
        u.need(plan.get('sni', domain) == domain, 'TLS SNI должен совпадать с постоянным доменом.')
    else:
        keys = store.load('reality.private.json')
        u.need(isinstance(keys, dict) and all(isinstance(keys.get(k), str) for k in ('private', 'public', 'short'))
               and re.fullmatch('[A-Za-z0-9_-]{43}', keys['private'])
               and re.fullmatch('[A-Za-z0-9_-]{43}', keys['public']) and re.fullmatch('[0-9a-f]{16}', keys['short']),
               'Исходные ключи REALITY отсутствуют или повреждены; новые не создаю.')
    initial, receipt = store.load('dns.intent.json'), store.load('dns.receipt.json')
    u.need(isinstance(initial, dict) and isinstance(receipt, dict) and initial.get('version') == 1
           and all(receipt.get(k) == v for k, v in initial.items() if k != 'version')
           and receipt.get('name') == domain and receipt.get('content') == plan['ip']
           and receipt.get('type') == 'A' and receipt.get('proxied') is False and receipt.get('ttl') == 60
           and isinstance(receipt.get('id'), str) and isinstance(receipt.get('zone_id'), str)
           and re.fullmatch(r'SPN universal-reality:[0-9a-f]{32}', receipt.get('comment', '')),
           'Сначала завершите исходный deploy.py: нет исходной квитанции владения DNS; запись не принимаю по имени.')
    return plan


def chain(store, plan):
    """Validate the entire append-only chain, including a single last pending op."""
    root = store.path / 'replacements'
    u.need(not root.is_symlink(), 'Небезопасный каталог замен.')
    if not root.exists():
        return [], None
    u.need(root.is_dir(), 'Некорректный каталог замен.')
    u.Store(root)  # validate owner/0700 even though child Stores are also private
    directories = sorted(root.iterdir())
    commits, pending, current, previous = [], None, plan['ip'], None
    for generation, directory in enumerate(directories, 1):
        u.need(directory.name == f'{generation:06d}' and directory.is_dir() and not directory.is_symlink(),
               'Цепочка замен повреждена: порядок каталогов неоднозначен.')
        op = u.Store(directory)
        intent, commit = op.load('intent.json'), op.load('commit.json')
        u.need(pending is None, 'Есть незавершённая замена перед другой операцией.')
        if intent is None:
            u.need(commit is None and not any(p.name != '.lock' for p in directory.iterdir()),
                   'В операции замены отсутствует исходное намерение.')
            pending = (op, None)
            continue
        u.need(isinstance(intent, dict) and intent.get('format') == 1 and intent.get('generation') == generation
               and intent.get('domain') == plan['domain'] and intent.get('planHash') == u.digest(plan)
               and intent.get('fromIp') == current and intent.get('previousCommitHash') == previous
               and isinstance(intent.get('snapshot'), dict), 'Цепочка замен не совпала с исходной установкой.')
        d.public_ipv4(intent.get('toIp', ''))
        u.need(intent['toIp'] != current, 'Операция замены не меняет IPv4.')
        if commit is None:
            pending_machine(op)
            pending = (op, intent)
            continue
        expected = {k: intent[k] for k in ('format', 'generation', 'domain', 'fromIp', 'toIp', 'previousCommitHash')}
        expected.update(intentHash=u.digest(intent))
        ssh_only = isinstance(commit, dict) and commit.get('verificationMode') == 'ssh-only'
        if ssh_only:
            verified = (commit.get('trafficAndExportVerified') is False
                        and commit.get('sshReachabilityVerified') is True
                        and commit.get('testRoute') == 'ssh-check'
                        and commit.get('directReachabilityVerified') is False
                        and op.load('ssh-only.verified.json') == {
                            'passed': True, 'verificationMode': 'ssh-only', 'ip': intent['toIp']}
                        and op.load('operation-kind.json') == {'format': 1, 'sameVps': True}
                        and op.load('verification-kind.json') == {'format': 1, 'mode': 'ssh-only'})
        else:
            verified = (isinstance(commit, dict) and commit.get('trafficAndExportVerified') is True
                        and commit.get('testRoute') in ('physical', 'system', 'ssh')
                        and commit.get('directReachabilityVerified') == (commit['testRoute'] == 'physical')
                        and op.load('traffic.verified.json') is not None
                        and op.load('export.verified.json') is not None)
        u.need(isinstance(commit, dict) and all(commit.get(k) == v for k, v in expected.items())
               and verified and op.load('dns.verified.json') is not None
               and op.load('node-domain.receipt.json') is not None,
               'Квитанция завершения замены не подтверждена всеми этапами.')
        commits.append(commit)
        current, previous = intent['toIp'], u.digest(commit)
    return commits, pending


def start_operation(store, plan, new_ip, snapshot):
    commits, pending = chain(store, plan)
    current = commits[-1]['toIp'] if commits else plan['ip']
    if pending and pending[1]:
        u.need(pending[1]['toIp'] == new_ip,
               'У домена уже есть незавершённая замена на ' + pending[1]['toIp']
               + '; повторите ту же команду. Другой IPv4 не перезаписывает её журнал.')
        return pending
    u.need(new_ip != current, 'Этот IPv4 уже является текущим; новая операция замены не нужна.')
    generation = len(commits) + 1
    u.Store(store.path / 'replacements')
    op = pending[0] if pending else u.Store(store.path / 'replacements' / f'{generation:06d}')
    intent = {'format': 1, 'generation': generation, 'domain': plan['domain'], 'planHash': u.digest(plan),
              'fromIp': current, 'toIp': new_ip, 'previousCommitHash': u.digest(commits[-1]) if commits else None,
              'snapshot': copy.deepcopy(snapshot)}
    op.save('intent.json', intent)
    return op, intent


def _host_snapshot(host):
    return {k: copy.deepcopy(v) for k, v in host.items() if k not in ('createdAt', 'updatedAt')}


def _node_matches(node, saved):
    return d.replacement_matches(node, {k: v for k, v in saved.items() if k not in ('address', 'isDisabled')})


def topology(api, store, plan, operation=None):
    """Read-only audit. Display names/extra metadata are captured, not rewritten."""
    profiles, nodes, hosts, squads = (api.rows(k) for k in ('config-profiles', 'nodes', 'hosts', 'internal-squads'))
    profile = u.one(profiles, store.load('profile.receipt.json')['uuid'])
    node = u.one(nodes, store.load('node.receipt.json')['uuid'])
    host = u.one(hosts, store.load('tcp.receipt.json')['uuid'])
    u.need(profile.get('config') == d.profile_body(plan, store)['config'],
           'Техническая конфигурация/ключи профиля изменились; профиль не перезаписываю.')
    inbounds = profile.get('inbounds')
    u.need(isinstance(inbounds, list) and len(inbounds) == 1
           and inbounds[0].get('tag') == d.profile_tag(plan), 'Исходный инбаунд не подтверждён.')
    inbound = inbounds[0]['uuid']
    original_node = store.load('node.intent.json')
    expected_binding = {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': [inbound]}
    u.need(original_node.get('configProfile') == expected_binding and node.get('port') == 2222
           and u.same(node, {'configProfile': expected_binding}), 'UUID инбаунда/привязка ноды изменились.')
    d.direct_controller(node)
    u.need(all(n['uuid'] == node['uuid'] for n in nodes
               if (n.get('configProfile') or {}).get('activeConfigProfileUuid') == profile['uuid']),
           'Исходный профиль используется другой нодой; общую конфигурацию автоматически не трогаю.')
    initial_host = store.load('tcp.intent.json')
    technical = ('address', 'port', 'nodes', 'inbound', 'sni', 'alpn', 'fingerprint', 'securityLayer',
                 'mihomoX25519', 'overrideSniFromAddress', 'keepSniBlank')
    expected_host = {key: initial_host[key] for key in technical if key in initial_host}
    u.need(initial_host.get('address') == plan['domain'] and initial_host.get('port') == 443
           and initial_host.get('nodes') == [node['uuid']]
           and initial_host.get('inbound') == {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbound}
           and initial_host.get('sni') == (plan['domain'] if d.is_tls(plan) else plan.get('sni', d.SNI))
           and initial_host.get('fingerprint') == d.FINGERPRINT and u.same(host, expected_host),
           'Технические параметры исходного доменного хоста изменились; хост не перезаписываю.')
    # Only this inbound's audience is relevant. A new, unrelated server may be
    # added to a shared squad while this operation runs; we never write squads
    # and must not pin their other members as if we owned the whole squad.
    audience = sorted(row['uuid'] for row in squads if inbound in u.ids(row['inbounds']))
    u.need(set(plan['squads']) <= set(audience), 'Исходный инбаунд отсутствует в выбранных сквадах.')
    snapshot = {'node': {**d.node_snapshot(node), 'isDisabled': False}, 'host': _host_snapshot(host),
                'profile': {'uuid': profile['uuid'], 'name': profile.get('name'), 'configHash': u.digest(profile['config'])},
                'inbound': inbound, 'squads': audience}
    allowed_addresses, allow_disabled = {plan['domain']}, False
    if operation:
        op, intent = operation
        saved = intent['snapshot']
        new_intent, new_receipt = op.load('node-new.intent.json'), op.load('node-new.receipt.json')
        domain_intent, domain_receipt = op.load('node-domain.intent.json'), op.load('node-domain.receipt.json')
        if new_intent:
            allowed_addresses.add(intent['toIp'])
        if new_receipt and not domain_intent:
            allowed_addresses = {intent['toIp']}
        if domain_receipt:
            allowed_addresses = {plan['domain']}
        allow_disabled = bool((new_intent and not new_receipt) or (domain_intent and not domain_receipt))
        u.need(_node_matches(node, saved['node']) and snapshot['host'] == saved['host']
               and snapshot['profile'] == saved['profile'] and snapshot['inbound'] == saved['inbound']
               and snapshot['squads'] == saved['squads'],
               'Нода, хост, профиль или аудитория изменились во время замены; чужие правки не перезаписываю.')
    u.need(node.get('address') in allowed_addresses and (node.get('isDisabled') is False or allow_disabled),
           'Адрес/состояние ноды не соответствует журналу. Выключенную владельцем ноду не включаю.')
    u.need(host.get('isDisabled') is False,
           'Исходный хост выключен; замена не включает выключенные владельцем хосты. Сначала решите это в панели.')
    if operation:
        new_ip = operation[1]['toIp']
        u.need(not any(n['uuid'] != node['uuid'] and n.get('address') in (new_ip, plan['domain']) for n in nodes),
               'Новый IPv4/домен уже занят другой нодой; её не меняю.')
    return snapshot


def route_node(api, op, intent, phase, target):
    """Narrow, restartable address-only PATCH on the existing node UUID."""
    u.need(phase in ('new', 'domain') and target == (intent['toIp'] if phase == 'new' else intent['domain']),
           'Недопустимый этап управления нодой.')
    saved = intent['snapshot']['node']
    uid = saved['uuid']
    marker = {'uuid': uid, 'target': target, 'intentHash': u.digest(intent)}
    name = 'node-' + phase
    op.save(name + '.intent.json', marker)  # durable BEFORE disable or PATCH
    def current():
        value = u.one(api.rows('nodes'), uid)
        d.direct_controller(value)
        u.need(_node_matches(value, saved) and value.get('address') in (intent['domain'], intent['toIp']),
               'Параметры ноды изменились во время переключения адреса.')
        u.need(not any(n['uuid'] != uid and n.get('address') == target for n in api.rows('nodes')),
               'Адрес занят другой нодой; переключение остановлено.')
        return value
    node = current()
    receipt = op.load(name + '.receipt.json')
    if receipt:
        u.need(receipt == marker and node.get('address') == target and node.get('isDisabled') is False,
               'Завершённый этап переключения ноды изменился.')
        return node
    if node.get('isDisabled') is not True:
        api.disable_node(uid)
    u.need(current().get('isDisabled') is True, 'Приостановка собственного контроллера не подтверждена.')
    if current().get('address') != target:
        api('PATCH', '/nodes', {'uuid': uid, 'address': target})
    u.need(current().get('address') == target, 'Новый управляющий адрес ноды не подтверждён.')
    if current().get('isDisabled') is True:
        api.enable_node(uid)
    node = current()
    u.need(node.get('address') == target and node.get('isDisabled') is False, 'Повторное включение ноды не подтверждено.')
    op.save(name + '.receipt.json', marker)
    return node


def _wait_dns(plan, zone, wait, *, not_before=0):
    """Check authoritative + explicit recursive DNS; allow one full old TTL."""
    deadline = time.monotonic() + wait
    while True:
        try:
            d.verify_authoritative_dns(plan, zone)
            for resolver in ('1.1.1.1', '8.8.8.8'):
                u.need(d._dig(resolver, plan['domain'], 'A') == [plan['ip']]
                       and not d._dig(resolver, plan['domain'], 'AAAA'),
                       'Рекурсивный DNS ещё содержит прежний адрес; нода остаётся на новом числовом IPv4.')
            u.need(time.time() >= not_before,
                   'Жду истечения прежнего TTL 60 для DNS-кэша панели; нода остаётся на новом числовом IPv4.')
            return
        except u.DeployError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _traffic(ssh, op, plan, keys, subscription, engine, args, probe_options=None):
    routes = ('physical', 'ssh') if args.test_route == 'auto' else (args.test_route,)
    proof, route = {}, None
    for route in routes:
        print('VPN-тест: ' + {'physical': 'прямой физический путь', 'system': 'текущее подключение (не прямое доказательство)',
                             'ssh': 'SSH-канал, только проверка настройки'}[route] + '.', flush=True)
        try:
            forward = ssh.test_forward() if route == 'ssh' else contextlib.nullcontext(None)
            with forward as port:
                proof = d.probe.run(plan['ip'], keys['public'], keys['short'], subscription,
                                    engine=engine, interface=args.interface, route=route, relay_port=port,
                                    **(probe_options or {}))
        except d.probe.ProbeError as error:
            if route != routes[-1]:
                print('Прямой доступ не подтверждён: ' + str(error) + '. Проверю настройку через SSH.', flush=True)
                continue
            raise u.DeployError(str(error) + ' Замена не завершена; журнал и текущий этап DNS сохранены. '
                               'Повторите ту же команду.') from None
        op.save('vpn-test-' + str(time.time_ns()) + '.json', proof)
        if proof.get('passed') is True or route == routes[-1]:
            break
    u.need(proof.get('passed') is True and proof.get('tests') == 9 and proof.get('ip') == plan['ip']
           and proof.get('testRoute') == route and len(proof.get('results', [])) == 9
           and all(row.get('passed') is True and row.get('clientAlive') is True for row in proof['results'])
           and ((route == 'physical' and proof.get('physicalSourceObserved') is True
                 and proof.get('directReachabilityVerified') is True)
                or (route in ('system', 'ssh') and proof.get('testRoute') == route
                    and proof.get('directReachabilityVerified') is False)),
           'Реальный VPN-тест не прошёл 9/9. Замена не завершена; текущий этап DNS сохранён в журнале.')
    marker = {'passed': True, 'tests': 9, 'testRoute': route, 'directReachabilityVerified': route == 'physical'}
    # A resume may choose a stronger route: the final commit records its latest proof.
    op.save('traffic-proof-' + u.digest(proof) + '.json', proof)
    if op.load('traffic.verified.json') is None:
        op.save('traffic.verified.json', marker)
    return marker


def finish_operation(op, intent, traffic):
    commit = {k: intent[k] for k in ('format', 'generation', 'domain', 'fromIp', 'toIp', 'previousCommitHash')}
    ssh_only = traffic.get('verificationMode') == 'ssh-only'
    commit.update(intentHash=u.digest(intent), trafficAndExportVerified=not ssh_only,
                  testRoute=traffic['testRoute'], directReachabilityVerified=traffic['directReachabilityVerified'])
    if ssh_only:
        commit.update(verificationMode='ssh-only', sshReachabilityVerified=True)
    proofs = ('ssh-only.verified.json',) if ssh_only else ('traffic.verified.json', 'export.verified.json')
    u.need(all(op.load(name) is not None for name in (*proofs, 'dns.verified.json', 'node-domain.receipt.json')),
           'Не все этапы замены подтверждены; завершение не записано.')
    op.save('commit.json', commit)
    return commit


def original_machine(store, commits):
    """Use the current guest's recorded identity, including after a VM replacement."""
    source = (u.Store(store.path / 'replacements' / f"{commits[-1]['generation']:06d}")
              if commits else store)
    identities = []
    for path in source.path.iterdir():
        if re.fullmatch(r'machine-[0-9a-f]{64}\.json', path.name):
            value = source.load(path.name)
            u.need(isinstance(value, dict) and re.fullmatch('[0-9a-f]{32}', value.get('machineId', ''))
                   and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', value.get('sshFingerprint', '')),
                   'Сохранённая идентификация VPS повреждена.')
            identities.append((value['machineId'], value['sshFingerprint']))
    u.need(identities and len(set(identities)) == 1,
           'Нет однозначной идентификации прежней VPS. Смена IP не должна переустанавливать сервер.')
    machine, fingerprint = identities[0]
    return {'machineId': machine, 'sshFingerprint': fingerprint}


def pending_machine(op):
    files = list(op.path.glob('machine-*.json'))
    u.need(all(re.fullmatch(r'machine-[0-9a-f]{64}\.json', path.name) for path in files),
           'Повреждён журнал идентификации новой VPS.')
    return original_machine(op, []) if files else None


def pin_machine(op, guest, expected_fingerprint=None):
    """Pin a pending replacement guest before package/network/node mutations."""
    u.need(isinstance(guest, dict) and isinstance(guest.get('machineId'), str)
           and re.fullmatch('[0-9a-f]{32}', guest['machineId'])
           and isinstance(guest.get('sshFingerprint'), str)
           and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', guest['sshFingerprint'])
           and type(guest.get('ownedInstallation')) is bool, 'Идентификация новой VPS не подтверждена.')
    u.need(expected_fingerprint in (None, guest['sshFingerprint']),
           'Фактический SSH-ключ новой VPS отличается от подтверждённого отпечатка.')
    saved = pending_machine(op)
    if saved:
        u.need(all(guest[key] == value for key, value in saved.items()),
               'Новая VPS изменилась после начала замены: machine-id или SSH-ключ не совпал; '
               'установка, нода и DNS не переключаются.')
    value = {key: guest[key] for key in ('machineId', 'sshFingerprint', 'ownedInstallation')}
    op.save('machine-' + u.digest(value) + '.json', value)
    return value


def claim_kind(op, same_vps):
    """Do not reinterpret an interrupted clean-VM replacement as an IP-only move."""
    marker = {'format': 1, 'sameVps': same_vps}
    old = op.load('operation-kind.json')
    if old is None and same_vps:
        u.need(op.load('runtime.json') is None,
               'Уже начата замена VPS другим режимом. Продолжите прежнюю команду replace.py.')
    u.need(old is None or old == marker,
           'Режим незавершённой операции отличается. Повторите прежнюю команду.')
    op.save('operation-kind.json', marker)


def claim_verification(op, ssh_only):
    """An interrupted operation cannot silently weaken its proof requirements."""
    marker = {'format': 1, 'mode': 'ssh-only' if ssh_only else 'traffic'}
    old = op.load('verification-kind.json')
    u.need(old is None or old == marker,
           'Режим проверки незавершённой операции отличается. Повторите прежнюю команду.')
    if old is None and ssh_only:
        u.need(op.load('runtime.json') is None,
               'Уже начата замена с VPN-проверкой. Продолжите без --ssh-only.')
    op.save('verification-kind.json', marker)


def retarget_source(ssh, op, intent, identity):
    """Only the owned source/unit/metadata transition; no install or restart."""
    observed = json.loads(ssh.action('identity', capture=True))
    u.need(observed.get('ownedInstallation') is True
           and all(observed.get(key) == value for key, value in identity.items()),
           'Это не прежняя установленная VPS: SSH-ключ, machine-id или маркер отличаются. '
           'Для новой/переустановленной VPS нужен replace.py без --same-vps.')
    u.need(ssh.plan['ip'] in ssh.assigned_ipv4(),
           'Сначала назначьте новый IPv4 этой VPS у хостера. DNS не менялся.')
    op.save('machine-' + u.digest(observed) + '.json', observed)
    stage = '/root/spn-reality-managed-' + ssh.plan['id']
    result = json.loads(ssh.run(shlex.join([
        'python3', stage + '/retarget_source.py', '--ip', intent['toIp'], '--old-ip', intent['fromIp'],
        '--state-dir', stage, '--operation', f"{intent['generation']:06d}",
    ]), capture=True))
    u.need(result.get('ip') == intent['toIp'] and result.get('preferredSourceVerified') is True
           and result.get('bootPersistenceVerified') is True and result.get('networkRestarted') is False,
           'Смена исходящего IP и её сохранение после перезагрузки не подтверждены. DNS не меняю.')
    op.save('source-retarget.verified.json', result)


def run(args):
    same_vps = getattr(args, 'same_vps', False)
    ssh_only = getattr(args, 'ssh_only', False)
    non_interactive = getattr(args, 'non_interactive', False)
    u.need(not ssh_only or same_vps, '--ssh-only разрешён только для смены IP на прежней VPS.')
    args.ip = d.public_ipv4(args.ip)
    try:
        args.domain = d.dns.normalize_name(args.domain)
    except ValueError:
        raise u.DeployError('--domain: нужен существующий DNS-домен, не IP.') from None
    u.need(5 <= args.wait <= 600, '--wait: от 5 до 600 секунд.')
    u.need(args.ssh_port is None or (1 <= args.ssh_port <= 65535 and args.ssh_port not in (443, 2222)),
           'Недопустимый SSH-порт.')
    if not args.apply:
        print(json.dumps({'planOnly': True, 'domain': args.domain, 'newIp': args.ip,
                          'preserve': ['node UUID', 'host UUID', 'profile/inbound UUID', 'names', 'keys', 'squads'],
                          'changes': False, 'createsPanelObjects': False, 'buysOrDeletesVps': False,
                          'sameVps': same_vps, 'testRoute': 'ssh-check' if ssh_only else args.test_route,
                          'verificationMode': 'ssh-only' if ssh_only else 'traffic'}, ensure_ascii=False))
        return
    settings = u.read_settings(Path(args.env_file).expanduser())
    base = u.panel_url(args.panel_url or settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    original = find_original(base, args.domain, args.state_dir)
    with original.lock():
        if original.load('fleet-delegation.intent.json') or original.load('fleet-delegation.json'):
            if str(d.ROOT) not in sys.path:
                sys.path.insert(0, str(d.ROOT))
            from spn_reserve_fleet.remote import require_local_control
            try:
                require_local_control(original)
            except ValueError as error:
                raise u.DeployError(str(error)) from None
        plan = validate_original(original, args.domain, base)
        u.need(same_vps or args.ssh_port in (None, plan['sshPort']),
               'При полной замене VPS сохраните прежний SSH-порт; перенос порта пока не поддерживается.')
        commits, pending = chain(original, plan)
        current = commits[-1]['toIp'] if commits else plan['ip']
        if pending and pending[1]:
            u.need(pending[1]['toIp'] == args.ip,
                   'Есть незавершённая замена на ' + pending[1]['toIp'] + '; продолжайте с тем же --ip.')
        elif args.ip == current:
            print('IPv4 уже текущий: ' + current + '. Новая замена не создана. '
                  'Сохранена только историческая проверка; текущая доступность сейчас не тестировалась.')
            return
        identity = original_machine(original, commits) if same_vps else None
        if same_vps:
            u.need(args.host_key_sha256 in (None, identity['sshFingerprint']),
                   'При смене IP на прежней VPS ключ SSH должен остаться прежним.')
        subscription, engine = None, None
        if not ssh_only:
            try:
                subscription = d.probe.private_file(Path(args.test_subscription).expanduser())
                d.probe.subscription_url(subscription)
                engine = d.probe.select_engine(args.engine)
                d.probe.validate_interface(args.interface, physical=args.test_route in ('physical', 'auto'))
            except d.probe.ProbeError as error:
                raise u.DeployError(str(error)) from None
        if d.is_tls(plan) and not same_vps:
            try:
                tls_certificate.require_tools()
            except tls_certificate.u.DeployError as error:
                raise u.DeployError(str(error)) from None
        token = settings.get('REMNAWAVE_API_TOKEN') if settings.get('REMNAWAVE_BASE_URL', '').rstrip('/') == base else None
        u.need(token or not non_interactive,
               'Для --non-interactive нужен REMNAWAVE_API_TOKEN и совпадающий REMNAWAVE_BASE_URL в --env-file.')
        api = d.API(base, token or getpass.getpass('API-токен панели (не передаётся на VPS): '))
        snapshot = topology(api, original, plan, pending if pending and pending[1] else None)
        # Claim the operation before trust/upload/source changes. Creation cannot race it.
        op, intent = start_operation(original, plan, args.ip, snapshot)
        claim_kind(op, same_vps)
        claim_verification(op, ssh_only)
        topology(api, original, plan, (op, intent))
        runtime = {**d.tls_runtime_plan(plan, original), 'ip': args.ip, 'panelIp': d.panel_ipv4(base),
                   'sshPort': args.ssh_port if args.ssh_port is not None else plan['sshPort']}
        if non_interactive:
            runtime['batchMode'] = True
        op.save('runtime.json', runtime)
        try:
            cloudflare_token = d.cloudflare_token(args, settings)
            client = d.dns.CloudflareDNS(cloudflare_token)
            zone = client.find_zone(plan['dns']['zone'])
            receipt = original.load('dns.receipt.json')
            u.need(zone.get('id') == receipt['zone_id'], 'Cloudflare вернул другую DNS-зону.')
            client.check_update(op, receipt=receipt, old_ipv4=intent['fromIp'], new_ipv4=intent['toIp'])
        except d.dns.DNSProvisionError as error:
            raise u.DeployError(str(error)) from None
        certificate = None
        if d.is_tls(plan) and not same_vps:
            print('Подготавливаю TLS-сертификат для переноса.', flush=True)
            # Issue via DNS-01 on this controller while the original A record
            # still serves the old guest. Neither CF credentials nor a need
            # for the old guest's SSH availability crosses to the new guest.
            try:
                certificate = tls_certificate.issue(op, runtime, cloudflare_token, zone)
            except (tls_certificate.u.DeployError, tls_certificate.dns.DNSProvisionError) as error:
                raise u.DeployError(str(error)) from None
        known_hosts = None
        if not args.ssh_control:
            try:
                trust_options = {'interactive': False} if non_interactive else {}
                known_hosts = d.trust.resolve(runtime['ip'], runtime['sshPort'], op,
                                              expected=identity['sshFingerprint'] if same_vps else args.host_key_sha256,
                                              **trust_options)
            except d.trust.TrustError as error:
                raise u.DeployError(str(error)) from None
        print('Продолжаю замену ' + plan['domain'] + ': ' + intent['fromIp'] + ' → ' + intent['toIp']
              + '. UUID, имена и ключи сохраняются.', flush=True)
        with d.SSH(runtime, known_hosts, args.ssh_control) as ssh:
            bootstrap = {'managed-node.sh': d.managed_script(runtime),
                         'preferred_source.py': (HERE / 'preferred_source_policy.py').read_bytes()}
            if d.is_tls(runtime) and runtime.get('tlsSiteVersion'):
                bootstrap['tls-site-policy.sh'] = d.tls_site_files(runtime)['tls-site-policy.sh']
            ssh.upload(bootstrap)
            if same_vps:
                ssh.upload({'retarget_source.py': (HERE / 'retarget_source.py').read_bytes(),
                            **d.security.files(reality=not d.is_tls(plan))})
                retarget_source(ssh, op, intent, identity)
                op.save('node-guard.verified.json', d.security.guard(ssh, runtime['ip'], old_ip=intent['fromIp']))
            else:
                ssh.action('preflight')
                pin_machine(op, json.loads(ssh.action('identity', capture=True)), args.host_key_sha256)
                # Package hooks may change route protocol boot -> static. Do
                # not snapshot the route until all package hooks have finished.
                ssh.action('packages')
                print('Проверяю маршруты новой VPS.', flush=True)
                ssh.source('check', capture=True)
                ssh.source('apply')
                u.need(runtime['ip'] in ssh.assigned_ipv4(), 'Новый IPv4 не назначен доверенной VPS; DNS не меняю.')
                guest = json.loads(ssh.action('identity', capture=True))
                pin_machine(op, guest, args.host_key_sha256)
                files = d.render_files(runtime, original.load('node-secret.json')['value'])
                if certificate:
                    files.update({'tls-certificate.bundle.json': json.dumps(certificate).encode(),
                                  'tls_certificate_import.py': (HERE / 'tls_certificate_import.py').read_bytes()})
                ssh.upload(files)
                ssh.action('install')
                op.save('node-guard.verified.json', d.security.guard(ssh, runtime['ip']))
            if not d.is_tls(plan):
                d.security.sni(ssh)
            topology(api, original, plan, (op, intent))
            if not op.load('node-domain.intent.json'):
                node = route_node(api, op, intent, 'new', runtime['ip'])
            else:
                node = route_node(api, op, intent, 'domain', plan['domain'])
            d.u.Deployment(api, op, runtime, {}).wait_ready(node, args.wait)
            ssh.action('verify')
            ssh.source('verify')
            keys, probe_options = d.probe_parameters(runtime, original)
            if ssh_only:
                traffic = {'passed': True, 'verificationMode': 'ssh-only',
                           'testRoute': 'ssh-check', 'directReachabilityVerified': False}
                print('Доступность подтверждена по SSH; проверены прежняя VPS и новый исходящий IP.', flush=True)
            else:
                traffic = _traffic(ssh, op, runtime, keys, subscription, engine, args, probe_options)
            # DNS follows the explicitly selected SSH-only or full VPN proof.
            try:
                client.update_record(op, receipt=receipt, old_ipv4=intent['fromIp'], new_ipv4=intent['toIp'])
            except d.dns.DNSProvisionError as error:
                raise u.DeployError(str(error)) from None
            observed = op.load('dns-cutover.observed.json')
            if observed is None:
                observed = {'domain': plan['domain'], 'ip': runtime['ip'], 'observedAt': time.time()}
                op.save('dns-cutover.observed.json', observed)
            u.need(observed.get('domain') == plan['domain'] and observed.get('ip') == runtime['ip']
                   and isinstance(observed.get('observedAt'), (int, float)),
                   'Маркер времени DNS-переключения повреждён.')
            remaining = max(0, int(observed['observedAt'] + plan['dns']['ttl'] - time.time()) + 1)
            print('IP существующей DNS-записи обновлён. Проверяю DNS и жду обновления кэша'
                  + (' — около ' + str(remaining) + ' с.' if remaining else '.'), flush=True)
            _wait_dns(runtime, zone, args.wait, not_before=observed['observedAt'] + plan['dns']['ttl'])
            op.save('dns.verified.json', {'domain': plan['domain'], 'ip': runtime['ip'], 'recursiveVerified': True})
            node = route_node(api, op, intent, 'domain', plan['domain'])
            d.u.Deployment(api, op, runtime, {}).wait_ready(node, args.wait)
            ssh.action('verify')
            ssh.source('verify')
            topology(api, original, plan, (op, intent))
            if ssh_only:
                op.save('ssh-only.verified.json', {'passed': True, 'verificationMode': 'ssh-only', 'ip': runtime['ip']})
            else:
                export_options = {'sni': plan.get('sni', d.SNI), **probe_options}
                try:
                    exported = d.probe.verify_export(runtime['ip'], keys['public'], keys['short'], subscription,
                                                     interface=args.interface, route=traffic['testRoute'],
                                                     endpoint_address=plan['domain'], **export_options)
                    u.need(exported.get('passed') is True, 'Фактическая выгрузка подписки не подтверждена.')
                except d.probe.ProbeError as error:
                    raise u.DeployError(str(error) + ' Замена ещё не завершена; повторите ту же команду.') from None
                op.save('export-test-' + str(time.time_ns()) + '.json', exported)
                if op.load('export.verified.json') is None:
                    op.save('export.verified.json', {'passed': True, 'domain': plan['domain'], 'ip': runtime['ip']})
            d.security.guard(ssh, runtime['ip'], verify=True)
            finish_operation(op, intent, traffic)
            chain(original, plan)
        proof_message = ('SSH-доступность и DNS подтверждены.' if ssh_only
                         else 'VPN-тест 9/9; выгрузка домена подтверждена.')
        print('Замена завершена: ' + plan['domain'] + ' → ' + runtime['ip'] + '. ' + proof_message)
        if not traffic['directReachabilityVerified'] and not ssh_only:
            print('Проверена только настройка через ' + traffic['testRoute'] + '; прямой доступ НЕ подтверждён.')
        print(('Сеть и VPN не переустанавливались. IP не удалялись. ' if same_vps
               else 'Прежняя VPS не удалялась этим скриптом. ') + 'Журнал: ' + str(op.path))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--domain', required=True, help='Existing direct DNS endpoint; never an IP-only/VK migration')
    p.add_argument('--ip', required=True, help='Public IPv4 of the clean replacement VPS')
    p.add_argument('--same-vps', action='store_true', help='Retarget an assigned IPv4 on the SAME installed VPS; no reinstall')
    p.add_argument('--ssh-only', action='store_true',
                   help='Same VPS only: verify pinned SSH, source, node and DNS; skip VPN traffic/subscription tests')
    p.add_argument('--non-interactive', action='store_true',
                   help='Use SSH agent/keys only and require configured panel token; never ask for credentials')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--state-dir', help='Original deployment state directory, not a replacement operation directory')
    p.add_argument('--panel-url')
    p.add_argument('--env-file', default=str(d.ROOT / '.env'))
    p.add_argument('--cloudflare-token-file')
    p.add_argument('--ssh-port', type=int, help='New guest SSH port (default: original SSH port)')
    p.add_argument('--host-key-sha256', help='ED25519 SHA256 fingerprint independently verified in the VPS console')
    p.add_argument('--ssh-control', help=argparse.SUPPRESS)
    p.add_argument('--wait', type=int, default=120)
    p.add_argument('--test-subscription', default=str(d.ROOT / '.vpn-test-link.txt'))
    p.add_argument('--engine')
    p.add_argument('--interface', default='en0')
    p.add_argument('--test-route', choices=('physical', 'auto', 'system', 'ssh'), default='physical',
                   help='Default physical: direct public TCP443 proof. Explicit ssh/system prove configuration only.')
    return p


if __name__ == '__main__':
    try:
        run(parser().parse_args())
    except (u.DeployError, KeyboardInterrupt) as error:
        print('Остановлено: ' + (str(error) if isinstance(error, u.DeployError) else 'прервано пользователем.')
              + ' Журнал сохранён; не удаляйте его. Продолжайте с теми же --domain/--ip. '
              'DNS/адрес ноды могут быть на промежуточном этапе; автоматического отката на старую VPS нет.', file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        print('Остановлено: ' + type(error).__name__ + '; детали скрыты для защиты ключей. '
              'Замена не объявлена завершённой; журнал сохранён, повторите ту же команду.', file=sys.stderr)
        raise SystemExit(2)
