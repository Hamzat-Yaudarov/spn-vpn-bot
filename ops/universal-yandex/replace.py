#!/usr/bin/env python3
"""Replace one owned Yandex CDN origin VPS while keeping its client endpoint.

An offline plan is the default. Existing CDN/certificate CNAMEs, profile, inbound,
host, squads, user UUIDs and the private XHTTP path are never recreated/changed.
No Yandex resource is bought, edited or deleted. The old VPS is retained.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
import importlib.util
_spec = importlib.util.spec_from_file_location('spn_yandex_replacement_deploy', HERE / 'deploy.py')
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)
r, u, c = d.r, d.u, d.c
cert = d.module('spn_yandex_replacement_certificate', r.HERE / 'tls_replacement_certificate.py')


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _plan(store, domain=None, base=None):
    plan = store.load('plan.json')
    c.validate_plan(plan)
    u.need((domain is None or plan['domain'] == domain) and (base is None or plan['panel'] == base),
           'Исходный CDN-план относится к другому домену или панели.')
    c._source(store.load('source.json'))
    return plan


def _base_ip(store, plan):
    receipt = d.bindings.local_receipt(store, plan, c._nginx(plan).encode('ascii'))
    return receipt['extraIp'] if receipt else plan['ip']


def validate_original(store, domain, base):
    plan = _plan(store, domain, base)
    for key in ('profile', 'node', 'cdn'):
        receipt, intent = store.load(key + '.receipt.json'), store.load(key + '.intent.json')
        u.need(isinstance(receipt, dict) and _uuid(receipt.get('uuid')) and isinstance(intent, dict),
               'Сначала завершите исходный CDN deploy.py: нет исходных UUID-квитанций.')
    ready, activation = store.load('origin.ready.json'), store.load('activation.verified.json')
    u.need(isinstance(ready, dict) and ready == {'id': plan['id'], 'node': store.load('node.receipt.json')['uuid'],
              'profile': store.load('profile.receipt.json')['uuid'],
              'inbound': store.load('node.intent.json')['configProfile']['activeInbounds'][0],
              'host': store.load('cdn.receipt.json')['uuid']}
           and activation == {'host': ready['host'], 'code': plan['code']}
           and not store.load('activation.pending.json'),
           'Сначала завершите --finish исходной CDN-установки; активация не подтверждена.')
    secret = store.load('node-secret.json')
    u.need(isinstance(secret, dict) and isinstance(secret.get('value'), str)
           and 20 <= len(secret['value']) <= 8192 and re.fullmatch('[A-Za-z0-9_/+=-]+', secret['value']),
           'Исходный ключ службы CDN отсутствует; новый ключ не создаётся.')
    intent, receipt = store.load('dns.intent.json'), store.load('dns.receipt.json')
    u.need(isinstance(intent, dict) and isinstance(receipt, dict) and intent.get('version') == 1
           and all(receipt.get(k) == v for k, v in intent.items() if k != 'version')
           and receipt.get('name') == plan['domain'] and receipt.get('content') == plan['ip']
           and receipt.get('type') == 'A' and receipt.get('proxied') is False and receipt.get('ttl') == 60
           and isinstance(receipt.get('id'), str) and isinstance(receipt.get('zone_id'), str)
           and re.fullmatch(r'SPN universal-reality:[0-9a-f]{32}', receipt.get('comment', '')),
           'Исходная квитанция владения origin A отсутствует; запись по имени не принимается.')
    for kind in ('cdn', 'certificate'):
        value = store.load('dns-' + kind + '.receipt.json')
        u.need(isinstance(value, dict) and isinstance(store.load('dns-' + kind + '.intent.json'), dict),
               'Нет исходной квитанции CDN/CNAME; чужие записи не принимаются.')
    return plan


def chain(store, plan):
    """Own append-only CDN operation chain; no direct-VPN journal interpretation."""
    root = store.path / 'replacements'
    u.need(not root.is_symlink(), 'Небезопасный каталог CDN-замен.')
    current, previous, commits, pending = _base_ip(store, plan), None, [], None
    if not root.exists():
        return commits, pending
    u.Store(root)
    for number, path in enumerate(sorted(root.iterdir()), 1):
        u.need(path.name == f'{number:06d}' and path.is_dir() and not path.is_symlink() and pending is None,
               'Цепочка CDN-замен повреждена или содержит несколько незавершённых операций.')
        op = u.Store(path)
        intent, commit = op.load('intent.json'), op.load('commit.json')
        if intent is None:
            u.need(commit is None and not any(x.name != '.lock' for x in path.iterdir()),
                   'В CDN-операции отсутствует исходное намерение.')
            pending = (op, None)
            continue
        u.need(isinstance(intent, dict) and intent.get('format') == 1 and intent.get('mode') == 'yandex-cdn-vps'
               and intent.get('generation') == number and intent.get('domain') == plan['domain']
               and intent.get('cdnDomain') == plan['cdnDomain'] and intent.get('planHash') == u.digest(plan)
               and intent.get('sourceHash') == u.digest(store.load('source.json'))
               and intent.get('fromIp') == current and intent.get('previousCommitHash') == previous
               and isinstance(intent.get('snapshot'), dict), 'CDN-журнал не соответствует исходной установке.')
        r.public_ipv4(intent.get('toIp', ''))
        u.need(intent['toIp'] != current, 'CDN-операция не меняет IPv4.')
        if commit is None:
            _machine(op)
            pending = (op, intent)
            continue
        expected = {key: intent[key] for key in ('format', 'mode', 'generation', 'domain', 'cdnDomain', 'fromIp', 'toIp', 'previousCommitHash')}
        expected.update(intentHash=u.digest(intent), originTrafficVerified=True, trafficAndExportVerified=True)
        proof = op.load('traffic.verified.json')
        u.need(isinstance(commit, dict) and all(commit.get(k) == v for k, v in expected.items())
               and commit.get('testRoute') in ('system', 'physical') and isinstance(proof, dict)
               and proof == {'passed': True, 'tests': 9, 'domain': plan['cdnDomain'], 'ip': intent['toIp'],
                             'testRoute': commit['testRoute'], 'cdnVerified': True}
               and op.load('origin-traffic.verified.json') == {'passed': True, 'tests': 9,
                       'domain': plan['domain'], 'ip': intent['toIp'], 'cdnVerified': False}
               and op.load('export.verified.json') == {'passed': True, 'domain': plan['cdnDomain']}
               and op.load('dns.verified.json') == {'domain': plan['domain'], 'ip': intent['toIp'], 'recursiveVerified': True}
               and op.load('source.verified.json') == {'ip': intent['toIp'], 'preferredSourceVerified': True,
                                                      'bootPersistenceVerified': True}
               and all(_machine(op))
               and op.load('node-address.receipt.json') == {'uuid': intent['snapshot']['node']['uuid'],
                   'target': intent['toIp'], 'intentHash': u.digest(intent)}
               and op.load('verification-kind.json') == {'format': 1, 'route': commit['testRoute']},
               'Завершение CDN-замены не подтверждено всеми этапами.')
        commits.append(commit)
        current, previous = intent['toIp'], u.digest(commit)
    return commits, pending


def start_operation(store, plan, new_ip, snapshot):
    commits, pending = chain(store, plan)
    current = commits[-1]['toIp'] if commits else _base_ip(store, plan)
    if pending and pending[1]:
        u.need(pending[1]['toIp'] == new_ip, 'Есть незавершённая CDN-замена на ' + pending[1]['toIp'] + '; повторите тот же адрес.')
        return pending
    u.need(new_ip != current, 'IPv4 уже является текущим; новая CDN-замена не нужна.')
    u.Store(store.path / 'replacements')
    number = len(commits) + 1
    op = pending[0] if pending else u.Store(store.path / 'replacements' / f'{number:06d}')
    intent = {'format': 1, 'mode': 'yandex-cdn-vps', 'generation': number, 'domain': plan['domain'],
              'cdnDomain': plan['cdnDomain'], 'planHash': u.digest(plan),
              'sourceHash': u.digest(store.load('source.json')), 'fromIp': current, 'toIp': new_ip,
              'previousCommitHash': u.digest(commits[-1]) if commits else None, 'snapshot': copy.deepcopy(snapshot)}
    op.save('intent.json', intent)
    return op, intent


def topology(api, store, plan, current, operation=None):
    profiles, nodes, hosts, squads = (api.rows(key) for key in ('config-profiles', 'nodes', 'hosts', 'internal-squads'))
    profile = u.one(profiles, store.load('profile.receipt.json')['uuid'])
    node = u.one(nodes, store.load('node.receipt.json')['uuid'])
    host = u.one(hosts, store.load('cdn.receipt.json')['uuid'])
    source = store.load('source.json')
    u.need(profile.get('config') == c.profile(plan, source), 'Технический профиль CDN изменён; чужие правки не перезаписываются.')
    inbounds = profile.get('inbounds')
    u.need(isinstance(inbounds, list) and len(inbounds) == 1 and inbounds[0].get('tag') == 'yandex-xhttp-' + plan['code'],
           'Исходный CDN-инбаунд не подтверждён.')
    inbound = inbounds[0]['uuid']
    expected_binding = {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': [inbound]}
    u.need(node.get('port') == 2222 and u.same(node, {'configProfile': expected_binding})
           and store.load('node.intent.json').get('configProfile') == expected_binding
           and store.load('node.intent.json').get('address') == plan['ip'],
           'Привязка или управляющий порт CDN-ноды изменились.')
    r.direct_controller(node)
    u.need(not any(n['uuid'] != node['uuid'] and (n.get('configProfile') or {}).get('activeConfigProfileUuid') == profile['uuid']
                   for n in nodes), 'CDN-профиль стал общим; замена остановлена.')
    expected_host = c.host_body(plan, source, profile['uuid'], node['uuid'], inbound)
    u.need(u.same(host, {key: value for key, value in expected_host.items()
                         if key not in ('remark', 'isDisabled', 'isHidden', 'tags')}),
           'Домен, UUID, путь или технические настройки CDN-хоста изменились.')
    audience = sorted(row['uuid'] for row in squads if inbound in u.ids(row['inbounds']))
    u.need(set(plan['squads']) <= set(audience), 'CDN-инбаунд отсутствует в выбранных сквадах.')
    snapshot = {'node': {**r.node_snapshot(node), 'isDisabled': False},
                'host': {k: copy.deepcopy(v) for k, v in host.items() if k not in ('createdAt', 'updatedAt')},
                'profile': {'uuid': profile['uuid'], 'name': profile.get('name'), 'configHash': u.digest(profile['config'])},
                'inbound': inbound, 'squads': audience}
    allowed, disabled = {current}, False
    # A proven extra origin listener can move public A while the panel still
    # manages that SAME original guest through its initial numeric SSH/API IP.
    # Only the receipted initial generation permits those two old addresses.
    if not chain(store, plan)[0] and _base_ip(store, plan) != plan['ip']:
        allowed.add(plan['ip'])
    if operation:
        op, intent = operation
        saved = intent['snapshot']
        allowed = {saved['node']['address']}
        u.need(r.replacement_matches(node, {k: v for k, v in saved['node'].items() if k not in ('address', 'isDisabled')})
               and all(snapshot[k] == saved[k] for k in ('host', 'profile', 'inbound', 'squads')),
               'Нода, хост, профиль или аудитория изменились во время CDN-замены; чужие правки не перезаписываются.')
        step, receipt = op.load('node-address.intent.json'), op.load('node-address.receipt.json')
        if step:
            allowed.add(intent['toIp'])
            disabled = receipt is None
        if receipt:
            allowed = {intent['toIp']}
        u.need(not any(n['uuid'] != node['uuid'] and n.get('address') == intent['toIp'] for n in nodes),
               'Новый IPv4 занят другой нодой; она не меняется.')
    u.need(node.get('address') in allowed and (node.get('isDisabled') is False or disabled),
           'Управляющий адрес или выключенное состояние CDN-ноды не соответствует журналу.')
    u.need(host.get('isDisabled') is False, 'CDN-хост выключен владельцем; замена его не включает.')
    return snapshot


def route_node(api, op, intent):
    saved, target = intent['snapshot']['node'], intent['toIp']
    uid = saved['uuid']
    marker = {'uuid': uid, 'target': target, 'intentHash': u.digest(intent)}
    op.save('node-address.intent.json', marker)
    def current():
        rows = api.rows('nodes')
        node = u.one(rows, uid)
        r.direct_controller(node)
        u.need(r.replacement_matches(node, {k: v for k, v in saved.items() if k not in ('address', 'isDisabled')})
               and node.get('address') in (saved['address'], target)
               and not any(n['uuid'] != uid and n.get('address') == target for n in rows),
               'CDN-нода изменилась во время переключения управляющего адреса.')
        return node
    node = current()
    if op.load('node-address.receipt.json'):
        u.need(op.load('node-address.receipt.json') == marker and node.get('address') == target
               and node.get('isDisabled') is False, 'Завершённое переключение CDN-ноды изменилось.')
        return node
    if node.get('isDisabled') is not True:
        api.disable_node(uid)
    u.need(current().get('isDisabled') is True, 'Приостановка CDN-контроллера не подтверждена.')
    if current().get('address') != target:
        api('PATCH', '/nodes', {'uuid': uid, 'address': target})
    u.need(current().get('address') == target, 'Новый управляющий IPv4 не подтверждён.')
    if current().get('isDisabled') is True:
        api.enable_node(uid)
    node = current()
    u.need(node.get('address') == target and node.get('isDisabled') is False, 'Включение CDN-ноды не подтверждено.')
    op.save('node-address.receipt.json', marker)
    return node


def _action(ssh, action, capture=False):
    p = ssh.plan
    return ssh.run(shlex.join(['bash', ssh.stage + '/managed-origin.sh', action, p['domain'], p['ip'],
                              p['panelIp'], str(p['sshPort']), p['id']]), capture=capture)


def _verify_source(ssh, op):
    proof = json.loads(ssh.run(shlex.join(['python3', ssh.stage + '/preferred_source.py', 'verify',
        '--ip', ssh.plan['ip'], '--state-dir', ssh.stage + '/' + ssh.plan['id']]), capture=True))
    marker = {'ip': ssh.plan['ip'], 'preferredSourceVerified': True, 'bootPersistenceVerified': True}
    u.need(isinstance(proof, dict) and all(proof.get(k) == v for k, v in marker.items()),
           'Исходящий IPv4 или его сохранение после перезагрузки не подтверждены.')
    op.save('source.verified.json', marker)


def _machine(store):
    values = []
    for path in store.path.glob('machine-*.json'):
        u.need(re.fullmatch(r'machine-[0-9a-f]{64}\.json', path.name), 'Некорректный журнал идентификации CDN VPS.')
        value = store.load(path.name)
        u.need(isinstance(value, dict) and re.fullmatch('[0-9a-f]{32}', value.get('machineId', ''))
               and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', value.get('sshFingerprint', '')),
               'Идентификация CDN VPS не подтверждена.')
        values.append((value['machineId'], value['sshFingerprint']))
    u.need(len(set(values)) <= 1, 'Несколько несовместимых идентичностей CDN VPS.')
    return values[0] if values else (None, None)


def _pin_machine(op, guest, expected_fingerprint=None, *, require_owned=False):
    u.need(isinstance(guest, dict) and isinstance(guest.get('machineId'), str)
           and re.fullmatch('[0-9a-f]{32}', guest['machineId'])
           and isinstance(guest.get('sshFingerprint'), str)
           and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', guest['sshFingerprint'])
           and type(guest.get('ownedInstallation')) is bool
           and (not require_owned or guest['ownedInstallation'] is True),
           'Новая CDN VPS не подтвердила свою идентичность.')
    u.need(expected_fingerprint in (None, guest['sshFingerprint']),
           'Фактический SSH-ключ CDN VPS отличается от подтверждённого отпечатка.')
    saved = _machine(op)
    pair = (guest['machineId'], guest['sshFingerprint'])
    u.need(saved == (None, None) or saved == pair,
           'CDN VPS изменилась после начала замены: machine-id или SSH-ключ не совпал; '
           'установка, нода и DNS не переключаются.')
    value = {key: guest[key] for key in ('machineId', 'sshFingerprint', 'ownedInstallation')}
    op.save('machine-' + u.digest(value) + '.json', value)
    return value


def installation_snapshot(state_dir):
    """Read-only public identity, including this family's committed VPS chain."""
    store = u.Store(Path(state_dir).expanduser().absolute())
    plan = _plan(store)
    commits, pending = chain(store, plan)
    source = u.Store(store.path / 'replacements' / f"{commits[-1]['generation']:06d}") if commits else store
    machine, fingerprint = _machine(source)
    current = commits[-1]['toIp'] if commits else _base_ip(store, plan)
    activation, host = store.load('activation.verified.json'), store.load('cdn.receipt.json')
    verified = isinstance(host, dict) and activation == {'host': host.get('uuid'), 'code': plan['code']}
    ready = store.load('origin.ready.json')
    return {'installation_id': plan['id'], 'domain': plan['domain'], 'cdn_domain': plan['cdnDomain'],
            'current_ip': current, 'pending_ip': pending[1]['toIp'] if pending and pending[1] else None,
            'ssh_port': plan['sshPort'], 'machine_id': machine, 'ssh_fingerprint': fingerprint,
            'status_proof': 'pending' if pending else 'traffic' if verified else 'origin-ready' if ready else 'unverified',
            'generation': len(commits)}


def _proof(op, proof, plan, *, origin=False):
    expected_domain = plan['domain'] if origin else plan['cdnDomain']
    u.need(isinstance(proof, dict) and proof.get('passed') is True and proof.get('tests') == 9
           and proof.get('transport') == 'xhttp-get-header' and proof.get('domain') == expected_domain
           and proof.get('expectedExitIp') == plan['ip'] and proof.get('testRoute') in ('physical', 'system')
           and len(proof.get('results', [])) == 9
           and all(row.get('passed') is True and row.get('clientAlive') is True for row in proof['results'])
           and (not origin or (proof.get('scope') == 'origin-direct' and proof.get('cdnVerified') is False
                               and proof.get('originIp') == plan['ip']))
           and (proof['testRoute'] != 'physical' or proof.get('directReachabilityVerified') is True),
           'Полный XHTTP VPN-тест не прошёл 9/9; CDN-замена остаётся незавершённой.')
    name = 'origin-traffic' if origin else 'traffic'
    op.save(name + '-test-' + str(time.time_ns()) + '.json', proof)
    marker = {'passed': True, 'tests': 9, 'domain': expected_domain, 'ip': plan['ip'], 'cdnVerified': not origin}
    if not origin:
        marker['testRoute'] = proof['testRoute']
    op.save(name + '.verified.json', marker)
    return marker


def _wait_dns(plan, zone, wait, not_before):
    deadline = time.monotonic() + wait
    while True:
        try:
            r.verify_authoritative_dns(plan, zone)
            u.need(all(r._dig(server, plan['domain'], 'A') == [plan['ip']]
                       and not r._dig(server, plan['domain'], 'AAAA') for server in ('1.1.1.1', '8.8.8.8'))
                   and time.time() >= not_before, 'DNS/TTL origin ещё не обновились.')
            return
        except u.DeployError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _cdn_traffic(op, runtime, source, subscription, args):
    deadline = time.monotonic() + args.wait
    while True:
        try:
            proof = d.probe.run(runtime, source, subscription, engine=args.engine,
                                interface=args.interface, route=args.test_route)
            return _proof(op, proof, runtime)
        except (d.probe.ProbeError, u.DeployError):
            if time.monotonic() >= deadline:
                raise u.DeployError('CDN ещё не подтвердил новую VPS полным VPN-тестом 9/9. '
                                   'DNS и журнал сохранены; повторите ту же операцию.') from None
            time.sleep(3)


def run(args):
    args.ip = r.public_ipv4(args.ip)
    try:
        args.domain = r.dns.normalize_name(args.domain)
    except ValueError:
        raise u.DeployError('Нужен исходный DNS-домен origin.') from None
    u.need(60 <= args.wait <= 1800, '--wait: от 60 до 1800 секунд.')
    if not args.apply:
        print(json.dumps({'planOnly': True, 'domain': args.domain, 'newIp': args.ip, 'transport': 'yandex-cdn',
            'preserve': ['CDN domain', 'origin domain', 'node/host/profile/inbound UUID', 'XHTTP path', 'squads', 'user credentials'],
            'changes': False, 'buysOrDeletesVps': False, 'changesYandexResources': False}, ensure_ascii=False))
        return
    settings = u.read_settings(Path(args.env_file).expanduser())
    base = u.panel_url(args.panel_url or settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    u.need(Path(args.state_dir).expanduser().is_dir(), 'Нужен существующий исходный --state-dir CDN-установки.')
    store = u.Store(Path(args.state_dir).expanduser())
    with store.lock():
        if store.load('fleet-delegation.intent.json') or store.load('fleet-delegation.json'):
            if str(r.ROOT) not in sys.path:
                sys.path.insert(0, str(r.ROOT))
            from spn_reserve_fleet.remote import require_local_control
            require_local_control(store)
        plan = validate_original(store, args.domain, base)
        u.need(args.ssh_port in (None, plan['sshPort']), 'При замене сохраните прежний SSH-порт.')
        commits, pending = chain(store, plan)
        current = commits[-1]['toIp'] if commits else _base_ip(store, plan)
        if pending and pending[1]:
            u.need(pending[1]['toIp'] == args.ip, 'Есть незавершённая CDN-замена; повторите тот же IPv4.')
        elif args.ip == current:
            print('IPv4 уже текущий; новая CDN-замена не создаётся. Это не новая проверка доступности.')
            return
        token = settings.get('REMNAWAVE_API_TOKEN') if settings.get('REMNAWAVE_BASE_URL', '').rstrip('/') == base else None
        u.need(token, 'Нужны API-токен и совпадающий REMNAWAVE_BASE_URL в --env-file; интерактивного запроса нет.')
        subscription = r.probe.private_file(Path(args.test_subscription).expanduser())
        r.probe.subscription_url(subscription)
        args.engine = r.probe.select_engine(args.engine)
        r.probe.validate_interface(args.interface, physical=args.test_route == 'physical')
        cert.require_tools()
        api = r.API(base, token)
        snapshot = topology(api, store, plan, current, pending if pending and pending[1] else None)
        op, intent = start_operation(store, plan, args.ip, snapshot)
        op.save('verification-kind.json', {'format': 1, 'route': args.test_route})
        topology(api, store, plan, current, (op, intent))
        runtime = {**plan, 'ip': args.ip, 'panelIp': r.panel_ipv4(base)}
        c.validate_plan(runtime)
        op.save('runtime.json', runtime)
        cloudflare_token = r.cloudflare_token(args, settings)
        client = r.dns.CloudflareDNS(cloudflare_token)
        zone = client.find_zone(plan['dns']['zone'])
        receipt = store.load('dns.receipt.json')
        u.need(zone.get('id') == receipt['zone_id'], 'Cloudflare вернул другую origin-зону.')
        manager = d.records.Records(client, zone)
        for kind, name in (('cdn', plan['cdnDomain']), ('certificate', '_acme-challenge.' + plan['cdnDomain'])):
            manager.check(store, name, store.load('dns-' + kind + '.intent.json')['content'], kind)
        client.check_update(op, receipt=receipt, old_ipv4=intent['fromIp'], new_ipv4=intent['toIp'])
        print('Подготавливаю TLS-сертификат для переноса.', flush=True)
        certificate = cert.issue(op, runtime, cloudflare_token, zone)
        known = None
        if not args.ssh_control:
            known = r.trust.resolve(args.ip, plan['sshPort'], u.Store(op.path / 'ssh'),
                                    expected=args.host_key_sha256, interactive=False)
        print('Замена CDN origin ' + plan['domain'] + ': ' + intent['fromIp'] + ' → ' + args.ip + '.', flush=True)
        with d.SSH({**runtime, 'batchMode': True}, known, args.ssh_control) as ssh:
            ssh.upload({'managed-origin.sh': (HERE / 'managed-origin.sh').read_bytes(),
                        'origin_health.py': (HERE / 'origin_health.py').read_bytes(),
                        'preferred_source.py': (r.HERE / 'preferred_source.py').read_bytes(),
                        'host_checks.py': (HERE.parent / 'universal-cascade' / 'host_checks.py').read_bytes(),
                        'host-policy.json': json.dumps(store.load('host-policy.json', {'version': 1, 'keepTcpPorts': []})).encode()})
            _action(ssh, 'preflight')
            _pin_machine(op, json.loads(_action(ssh, 'identity', capture=True)), args.host_key_sha256)
            _action(ssh, 'packages')
            ssh.source('check')
            ssh.source('apply')
            guest = json.loads(_action(ssh, 'identity', capture=True))
            _pin_machine(op, guest, args.host_key_sha256, require_owned=True)
            files = c.role_files(runtime, store.load('node-secret.json')['value'])
            files.update({'tls_certificate_import.py': (r.HERE / 'tls_certificate_import.py').read_bytes(),
                          'tls-certificate.bundle.json': json.dumps(certificate).encode()})
            ssh.upload(files)
            _action(ssh, 'install')
            node = route_node(api, op, intent)
            u.Deployment(api, op, runtime, {}).wait_ready(node, args.wait)
            _action(ssh, 'verify')
            _verify_source(ssh, op)
            health = ssh.origin_health()
            u.need(health.get('passed') is True and health.get('scope') == 'origin-local-ssh'
                   and health.get('tlsVerified') is True and health.get('cdnVerified') is False
                   and health.get('domain') == plan['domain'] and health.get('ip') == args.ip
                   and health.get('deployment') == plan['code'], 'HTTPS origin не подтвердил новую VPS.')
            op.save('origin-local.verified.json', health)
            source = store.load('source.json')
            proof = d.probe.run_origin(runtime, source, subscription, engine=args.engine,
                                       interface=args.interface, route=args.test_route)
            _proof(op, proof, runtime, origin=True)
            topology(api, store, plan, current, (op, intent))
            client.update_record(op, receipt=receipt, old_ipv4=intent['fromIp'], new_ipv4=intent['toIp'])
            observed = op.load('dns-cutover.observed.json')
            if observed is None:
                observed = {'domain': plan['domain'], 'ip': args.ip, 'observedAt': time.time()}
                op.save('dns-cutover.observed.json', observed)
            u.need(observed.get('domain') == plan['domain'] and observed.get('ip') == args.ip
                   and isinstance(observed.get('observedAt'), (int, float)), 'Маркер DNS cutover повреждён.')
            _wait_dns(runtime, zone, args.wait, observed['observedAt'] + plan['dns']['ttl'])
            op.save('dns.verified.json', {'domain': plan['domain'], 'ip': args.ip, 'recursiveVerified': True})
            traffic = _cdn_traffic(op, runtime, source, subscription, args)
            exported = d.probe.verify_export(runtime, source, subscription, interface=args.interface)
            u.need(exported.get('passed') is True and exported.get('domain') == plan['cdnDomain'],
                   'Выгрузка прежнего CDN-хоста не подтверждена.')
            op.save('export-test-' + str(time.time_ns()) + '.json', exported)
            op.save('export.verified.json', {'passed': True, 'domain': plan['cdnDomain']})
            _action(ssh, 'verify')
            _verify_source(ssh, op)
            topology(api, store, plan, current, (op, intent))
            for kind, name in (('cdn', plan['cdnDomain']), ('certificate', '_acme-challenge.' + plan['cdnDomain'])):
                manager.check(store, name, store.load('dns-' + kind + '.intent.json')['content'], kind)
            client.check_update(op, receipt=receipt, old_ipv4=intent['fromIp'], new_ipv4=intent['toIp'])
            commit = {key: intent[key] for key in ('format', 'mode', 'generation', 'domain', 'cdnDomain', 'fromIp', 'toIp', 'previousCommitHash')}
            commit.update(intentHash=u.digest(intent), originTrafficVerified=True, trafficAndExportVerified=True,
                          testRoute=traffic['testRoute'])
            op.save('commit.json', commit)
            chain(store, plan)
        print('CDN-замена завершена: ' + plan['cdnDomain'] + ' → origin ' + args.ip
              + '. VPN 9/9 и прежняя подписка подтверждены. Старая VPS не удалялась.')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--domain', required=True, help='Original origin domain, not the CDN client domain')
    p.add_argument('--ip', required=True)
    p.add_argument('--state-dir', required=True)
    p.add_argument('--ssh-port', type=int)
    p.add_argument('--panel-url')
    p.add_argument('--env-file', default=str(r.ROOT / '.env'))
    p.add_argument('--cloudflare-token-file')
    p.add_argument('--test-subscription', default=str(r.ROOT / '.vpn-test-link.txt'))
    p.add_argument('--engine')
    p.add_argument('--interface', default='lo')
    p.add_argument('--test-route', choices=('physical', 'system'), default='system')
    p.add_argument('--host-key-sha256')
    p.add_argument('--host-key', dest='host_key_sha256', help=argparse.SUPPRESS)
    p.add_argument('--ssh-control', help=argparse.SUPPRESS)
    p.add_argument('--wait', type=int, default=300)
    p.add_argument('--non-interactive', action='store_true', help='Compatibility: this command never requests passwords/tokens')
    p.add_argument('--apply', action='store_true')
    return p


if __name__ == '__main__':
    try:
        run(parser().parse_args())
    except (u.DeployError, r.dns.DNSProvisionError, cert.u.DeployError, cert.dns.DNSProvisionError,
            c.ConfigError, d.bindings.BindingError, d.probe.ProbeError, r.trust.TrustError) as error:
        print('Остановлено: ' + str(error) + ' Журнал CDN-замены сохранён; повторите тот же domain/IP. '
              'DNS/адрес ноды могут быть на промежуточном этапе; старая VPS не удалялась.', file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print('Остановлено: прервано. Журнал CDN-замены сохранён; повторите тот же domain/IP.', file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print('Остановлено: CDN-замена не завершена; приватные детали скрыты. Журнал сохранён.', file=sys.stderr)
        raise SystemExit(2)
