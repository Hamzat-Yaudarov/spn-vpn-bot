#!/usr/bin/env python3
"""One owned REALITY backend behind an operator-supplied TCP passthrough lease.

No rental purchases or provider-bot automation. Node management uses the EU VPS;
the subscription host uses the separately supplied rental IPv4 and port.
"""
from __future__ import annotations

import copy
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('spn_vk_reality_infrastructure', HERE.parent / 'universal-reality/deploy.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
u = r.u
DEFAULT_SNI = 'login.vk.com'
MODE = 'vk-relay'


def parse_relay(value):
    u.need(isinstance(value, str) and re.fullmatch(r'[0-9.]+:[1-9][0-9]{0,4}', value),
           '--relay: нужен внешний IPv4:порт, например 185.86.147.77:10038.')
    address, port = value.split(':')
    address, port = r.public_ipv4(address), int(port)
    u.need(1 <= port <= 65535, 'Порт реле должен быть от 1 до 65535.')
    return {'address': address, 'port': port}


def validate(args):
    r.validate(args)
    if args.relay is not None:
        relay = parse_relay(args.relay)
        u.need(relay['address'] != args.ip, 'Адрес VK-реле должен отличаться от IP вашего VPS.')


def write_pointer(store, name, value, expected):
    """CAS under the installation lock; histories/intents remain immutable."""
    u.need(name in ('relay.current.json', 'relay.pending.json'), 'Неизвестный указатель состояния.')
    u.need(store.load(name) == expected, 'Состояние аренды изменилось одновременно; повторите команду.')
    fd, temporary = tempfile.mkstemp(prefix='.relay-', dir=store.path)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        u.need(store.load(name) == expected, 'Состояние аренды изменилось перед фиксацией.')
        os.replace(temporary, store.path / name)
        parent = os.open(store.path, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def endpoint(store, args):
    pending = store.load('relay.pending.json')
    chosen = parse_relay(args.relay) if args.relay is not None else (
        pending['after'] if pending else store.load('relay.current.json') or store.load('relay.initial.json'))
    u.need(chosen is not None, 'При первом запуске укажите --relay ВНЕШНИЙ_IP:ВЫДАННЫЙ_ПОРТ.')
    u.need(chosen == parse_relay(str(chosen['address']) + ':' + str(chosen['port']))
           and chosen['address'] != args.ip, 'Неверное сохранённое назначение реле.')
    # A newly rented address explicitly supersedes an expired interrupted
    # attempt. Reconciliation is deferred until SSH/preflight, never done here.
    return chosen


def prepare(api, store, args, base, panel_ip):
    saved = store.load('plan.json')
    if saved:
        u.need(saved.get('mode') == MODE,
               'Для этого VPS сохранена другая установка. Прямую ноду автоматически не превращаю в VK-обход.')
        u.need(saved['format'] == 1 and saved['ip'] == args.ip and saved['sshPort'] == args.ssh_port
               and saved['panel'] == base and saved['panelIp'] == panel_ip, 'Адрес VPS/панели отличается от сохранённого.')
        for key in ('name', 'country', 'sni'):
            supplied = getattr(args, key)
            u.need(supplied is None or supplied == saved[key],
                   'Для продолжения используйте прежнее --' + key + ' или не указывайте его.')
        if args.squad:
            u.need(u.choose_squads(api, args.squad) == saved['squads'], 'Выбранные сквады отличаются от сохранённых.')
        u.choose_squads(api, [], saved['squads'])
        r.validate_replacement(api, store, args)
        endpoint(store, args)
        return saved
    chosen = endpoint(store, args)
    rows = {kind: api.rows(kind) for kind in ('config-profiles', 'nodes', 'hosts')}
    r.stage_replacement(api, store, args, rows['nodes'], rows['hosts'])
    squads = u.choose_squads(api, args.squad)
    code = r.suffix(rows)
    r.reality_keys(store)
    # Separate initial endpoint from the immutable backend identity. A changed
    # rental never creates another node/controller on the same VPS.
    if store.load('relay.initial.json') is None:
        store.save('relay.initial.json', chosen)
    plan = {'format': 1, 'mode': MODE, 'id': uuid.uuid4().hex, 'code': code,
            'ip': args.ip, 'sshPort': args.ssh_port, 'panel': base, 'panelIp': panel_ip,
            'name': args.name or 'VPN', 'country': args.country or '', 'squads': squads,
            'sni': args.sni or DEFAULT_SNI, 'transport': 'raw-reality-vision-safari-v1'}
    store.save('plan.json', plan)
    return plan


class Deployment(r.Deployment):
    def host_body(self, profile, node, inbound):
        body = super().host_body(profile, node, inbound)
        body.update(self.store.load('relay.initial.json'))
        body.update(remark=self.plan['name'] + ' • ОБХОД VK ' + self.plan['code'],
                    tags=['AUTO_REALITY', 'AUTO_VK_RELAY'])
        return body

    def _owned_host(self):
        receipt = self.store.load('tcp.receipt.json')
        initial = self.store.load('tcp.intent.json')
        u.need(receipt and initial, 'Нет квитанции собственного VK-хоста; адрес реле не является доказательством владения.')
        actual = u.one(self.api.rows('hosts'), receipt['uuid'])
        allowed = [self.store.load('relay.current.json') or self.store.load('relay.initial.json')]
        pending = self.store.load('relay.pending.json')
        if pending:
            u.need(pending['host'] == actual['uuid'], 'UUID незавершённой смены аренды не совпал.')
            allowed += [{key: pending['before'][key] for key in ('address', 'port')}, pending['after']]
        good = any(u.same(actual, {**initial, **address, 'isDisabled': actual['isDisabled'], 'remark': actual['remark']})
                   for address in allowed)
        u.need(good, 'Технические параметры собственного VK-хоста изменены; их не перезаписываю.')
        return actual

    def host(self, profile, node, inbound):
        body = self.host_body(profile, node, inbound)
        if self.store.load('tcp.receipt.json'):
            u.need(self.store.load('tcp.intent.json') == body, 'Исходные параметры VK-хоста изменились.')
            return self._owned_host()
        host = super().ensure('tcp', 'hosts', body, lambda row:
                              (row.get('inbound') or {}).get('configProfileInboundUuid') == inbound)
        return host

    def _clear_pending(self, pending):
        u.need(self.store.load('relay.pending.json') == pending, 'Указатель смены аренды изменился.')
        (self.store.path / 'relay.pending.json').unlink()

    def _rollback_receipt(self, pending):
        self.store.save('relay-rollback-' + str(time.time_ns()) + '.receipt.json',
                        {'host': pending['host'], 'before': pending['before'],
                         'cancelledTarget': pending['after'], 'operation': u.digest(pending)})

    def cancel_pending(self):
        """A replacement lease must not be blocked forever by an expired attempt."""
        pending = self.store.load('relay.pending.json')
        if pending is None:
            return self._owned_host()
        operation = u.digest(pending)
        u.need(self.store.load('relay-move-' + operation + '.intent.json') == pending,
               'Журнал отменяемой смены аренды не подтверждён.')
        actual = self._owned_host()
        committed = self.store.load('relay.current.json')
        after = {**pending['after'], 'isDisabled': False}
        if committed == pending['after'] and u.same(actual, after):
            # Crash after the verified local commit but before pending unlink:
            # retain that last verified lease rather than roll it back further.
            self.store.save('relay-move-' + operation + '.receipt.json',
                            {'host': actual['uuid'], 'endpoint': pending['after'], 'exportVerified': True})
            self._clear_pending(pending)
            return actual
        before = pending['before']
        u.need(u.same(actual, before) or u.same(actual, after),
               'Состояние незавершённой аренды изменено вручную; автоматически не отменяю.')
        if not u.same(actual, before):
            self.api('PATCH', '/hosts', {'uuid': actual['uuid'], **before})
        actual = self._owned_host()
        u.need(u.same(actual, before), 'Откат незавершённой аренды не подтверждён; журнал сохранён.')
        previous = pending.get('committedBefore',
                               {key: before[key] for key in ('address', 'port')} if committed is not None else None)
        write_pointer(self.store, 'relay.current.json', previous, committed)
        self._rollback_receipt(pending)
        self._clear_pending(pending)
        return actual

    def publish_verified(self, host, target, verify_callback):
        """Publish one verified lease, recover lost replies, retain the old lease on failure."""
        current = self._owned_host()
        u.need(current['uuid'] == host['uuid'], 'Выбран другой хост при смене аренды.')
        u.need(target == parse_relay(str(target['address']) + ':' + str(target['port']))
               and target['address'] != self.plan['ip'], 'Неверный адрес новой аренды.')
        committed = self.store.load('relay.current.json')
        before = {key: current[key] for key in ('address', 'port', 'isDisabled')}
        pending = self.store.load('relay.pending.json')
        if pending is None:
            pending = {'host': current['uuid'], 'before': before, 'after': copy.deepcopy(target),
                       'committedBefore': committed}
            operation = u.digest(pending)
            self.store.save('relay-move-' + operation + '.intent.json', pending)
            write_pointer(self.store, 'relay.pending.json', pending, None)
        else:
            u.need(pending['host'] == current['uuid'] and pending['after'] == target,
                   'Есть незавершённая смена другой аренды; новую не начинаю.')
            before = pending['before']
            operation = u.digest(pending)
            u.need(self.store.load('relay-move-' + operation + '.intent.json') == pending,
                   'Журнал смены аренды не подтверждён.')
        desired = {**target, 'isDisabled': False}
        try:
            if not u.same(current, desired):
                self.api('PATCH', '/hosts', {'uuid': current['uuid'], **desired})
            current = self._owned_host()
            u.need(u.same(current, desired), 'Панель не подтвердила новый адрес/порт реле.')
            exported = verify_callback()
            u.need(isinstance(exported, dict) and exported.get('passed') is True,
                   'Фактическая выгрузка новой аренды не подтверждена.')
        except BaseException:
            # Do not leave a formerly working lease replaced by a failed one.
            # CAS on the complete owned payload prevents clobbering manual edits.
            try:
                observed = self._owned_host()
                if u.same(observed, desired) and not u.same(observed, before):
                    self.api('PATCH', '/hosts', {'uuid': observed['uuid'], **before})
                u.need(u.same(self._owned_host(), before), 'Откат адреса реле не подтверждён.')
                previous = pending.get('committedBefore', committed)
                write_pointer(self.store, 'relay.current.json', previous, committed)
                self._rollback_receipt(pending)
                self._clear_pending(pending)
            except (u.DeployError, OSError, ValueError):
                print('Ответ публикации/отката не подтверждён; журнал сохранён. Повторите ту же команду.', file=sys.stderr)
            raise
        # Commit only after traffic proof (caller) AND actual export proof. A
        # crash after any local write resumes the same journalled transition.
        write_pointer(self.store, 'relay.current.json', target, committed)
        self.store.save('relay-move-' + operation + '.receipt.json',
                        {'host': current['uuid'], 'endpoint': target, 'exportVerified': True})
        self.store.save('activation.verified.json',
                        {'host': current['uuid'], 'code': self.plan['code'], 'trafficAndExportVerified': True})
        self._clear_pending(pending)
        return current


def valid_proof(proof):
    return (proof.get('passed') is True and proof.get('tests') == 9
            and len(proof.get('results', [])) == 9
            and all(row.get('passed') is True and row.get('clientAlive') is True for row in proof['results']))


def run(args):
    validate(args)
    if not args.apply:
        relay = parse_relay(args.relay) if args.relay is not None else None
        print(json.dumps({'planOnly': True, 'backendDestination': args.ip + ':443',
                          'relay': relay, 'sni': args.sni or DEFAULT_SNI, 'fingerprint': 'safari',
                          'changes': False, 'needsDomain': False, 'creates': 'one profile/inbound/node/host'}, ensure_ascii=False))
        return
    settings = u.read_settings(Path(args.env_file).expanduser())
    base = u.panel_url(args.panel_url or settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    token = settings.get('REMNAWAVE_API_TOKEN') if settings.get('REMNAWAVE_BASE_URL', '').rstrip('/') == base else None
    if not token:
        token = getpass.getpass('API-токен панели (не передаётся на VPS): ')
    api = r.API(base, token)
    try:
        subscription = r.probe.private_file(Path(args.test_subscription).expanduser())
        r.probe.subscription_url(subscription)
        engine = r.probe.select_engine(args.engine)
        r.probe.validate_interface(args.interface)
    except r.probe.ProbeError as error:
        raise u.DeployError(str(error)) from None
    store = u.Store(Path(args.state_dir).expanduser() if args.state_dir else r.state_directory(base, args.ip, args.ssh_port))
    with store.lock():
        print('Проверяю установку VK-реле и записи панели…', flush=True)
        plan = prepare(api, store, args, base, r.panel_ipv4(base))
        target = endpoint(store, args)
        print('Код установки: ' + plan['code'], flush=True)
        print('Назначение при аренде: ' + plan['ip'] + ':443. Внешний адрес: '
              + target['address'] + ':' + str(target['port']), flush=True)
        deployment = Deployment(api, store, plan)
        completed = store.load('activation.verified.json') is not None
        try:
            known = None if args.ssh_control else r.trust.resolve(plan['ip'], plan['sshPort'], store, expected=args.host_key_sha256)
        except r.trust.TrustError as error:
            raise u.DeployError(str(error)) from None
        with r.SSH(plan, known, args.ssh_control) as ssh:
            ssh.upload({'managed-node.sh': (r.HERE / 'managed-node.sh').read_bytes(),
                        'preferred_source.py': (r.HERE / 'preferred_source.py').read_bytes()})
            ssh.action('preflight')
            ssh.source('check', capture=True)
            ssh.source('apply')
            r.retire_owned_aliases(api, store, plan, ssh.assigned_ipv4())
            identity = json.loads(ssh.action('identity', capture=True))
            u.need(re.fullmatch('[0-9a-f]{32}', identity['machineId'])
                   and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', identity['sshFingerprint']), 'Некорректная идентификация VPS.')
            store.save('machine-' + u.digest(identity) + '.json', identity)
            fresh = identity.get('ownedInstallation') is False
            deployment.restore_node_bindings = fresh or args.reinstall
            profile, node, inbound = deployment.provision()
            secret = store.load('node-secret.json')
            if secret is None:
                secret = {'value': api('GET', '/keygen')['secretKey']}
                store.save('node-secret.json', secret)
            ssh.upload(r.render_files(plan, secret['value']))
            ssh.action('install')
            deployment.helper.wait_ready(node, args.wait)
            deployment.attach(inbound)
            ssh.action('verify')
            ssh.source('verify')
            host = deployment.host(profile, node, inbound)
            had_pending = store.load('relay.pending.json') is not None
            pending = store.load('relay.pending.json')
            if pending and pending['after'] != target:
                host = deployment.cancel_pending()
                print('Незавершённая прежняя смена аренды сверена; проверяю новый выданный адрес.', flush=True)
            keys = r.reality_keys(store)
            print('Проверяю сам бэкенд через SSH…', flush=True)
            try:
                with ssh.test_forward() as forward:
                    backend = r.probe.run(plan['ip'], keys['public'], keys['short'], subscription,
                                          engine=engine, interface=args.interface, sni=plan['sni'], route='ssh', relay_port=forward)
            except r.probe.ProbeError as error:
                raise u.DeployError(str(error) + ' VK-хост не публикую.') from None
            store.save('backend-test-' + str(time.time_ns()) + '.json', backend)
            u.need(valid_proof(backend) and backend.get('testRoute') == 'ssh'
                   and backend.get('directReachabilityVerified') is False, 'Реальная проверка бэкенда не прошла.')
            if args.test_route == 'ssh':
                print('Бэкенд проверен: 9/9. VK-реле НЕ проверено; публикацию аренды не меняю, новый хост остаётся выключенным.')
                print('В аренде укажи назначение ' + plan['ip'] + ':443, затем повтори команду без --test-route ssh.')
                return
            routes = ('physical', 'system') if args.test_route == 'auto' else (args.test_route,)
            proof, route = None, None
            for route in routes:
                print('Проверяю настоящее VK-реле ' + target['address'] + ':' + str(target['port'])
                      + ' (' + route + ')…', flush=True)
                try:
                    proof = r.probe.run(target['address'], keys['public'], keys['short'], subscription,
                                        engine=engine, interface=args.interface, sni=plan['sni'], route=route,
                                        public_port=target['port'], expected_exit_ip=plan['ip'])
                except r.probe.ProbeError as error:
                    if route == routes[-1]:
                        raise u.DeployError(str(error) + ' Бэкенд проверен, но VK-реле не подтверждено. '
                                            'Проверь адрес назначения аренды ' + plan['ip'] + ':443; повторная установка не нужна.') from None
                    continue
                store.save('relay-test-' + str(time.time_ns()) + '.json', proof)
                if valid_proof(proof) and proof.get('relayPathVerified') is True:
                    break
            proof = proof or {}
            u.need(valid_proof(proof) and proof.get('relayPathVerified') is True
                   and proof.get('ip') == target['address'] and proof.get('port') == target['port']
                   and proof.get('expectedExitIp') == plan['ip'] and proof.get('testRoute') == route
                   and (route != 'physical' or proof.get('physicalSourceObserved') is True),
                   'Бэкенд проверен, но трафик через выданный IP/порт VK не прошёл. Новый хост не включаю; '
                   'старую аренду не меняю. Проверь назначение ' + plan['ip'] + ':443.')
            keep_disabled = (completed and host['isDisabled'] and not fresh and not args.reinstall
                             and not had_pending and store.load('relay.pending.json') is None)
            if keep_disabled:
                print('VK-реле проверено; хост оставлен выключенным по вашему прежнему выбору. Адрес не меняю.')
                return

            def exported():
                for attempt in range(3):
                    try:
                        result = r.probe.verify_export(target['address'], keys['public'], keys['short'], subscription,
                                                        interface=args.interface, sni=plan['sni'], route=route,
                                                        public_port=target['port'])
                        u.need(result.get('passed') is True, 'Выгрузка VK-хоста не подтверждена.')
                        store.save('relay-export-' + str(time.time_ns()) + '.json', result)
                        return result
                    except r.probe.ProbeError:
                        if attempt == 2:
                            raise
                        time.sleep(1)
            try:
                host = deployment.publish_verified(host, target, exported)
            except r.probe.ProbeError as error:
                raise u.DeployError(str(error) + ' Смена аренды не завершена; журнал сохранён.') from None
        print('Настроено: ' + host['remark'])
        print('VK ' + target['address'] + ':' + str(target['port']) + ' → VPS ' + plan['ip'] + ':443')
        print('Бэкенд: 9/9. Настоящее реле: 9/9, выходной IP и выгрузка подписки подтверждены. Обнови подписку в Happ/INCY.')
        if route != 'physical':
            print('Проверено через текущее подключение Mac. Обход белых списков вашего оператора отдельно не подтверждён.')
        print('Приватное состояние: ' + str(store.path))


def parser():
    p = r.parser(test_route_help='auto: physical relay test, then system-route relay test; ssh verifies only the backend and never publishes a new relay')
    p.description = __doc__
    p.add_argument('--relay', help='Rented external IPv4:port; may be changed on a later run')
    p.add_argument('--sni', choices=('login.vk.com', 'www.cloudflare.com'), help='Allowed provider SNI; initial default login.vk.com')
    return p


if __name__ == '__main__':
    try:
        run(parser().parse_args())
    except (u.DeployError, KeyboardInterrupt) as error:
        print('Остановлено: ' + (str(error) if isinstance(error, u.DeployError) else
              'прервано; записанное состояние сохранено. Повторите ту же команду.'), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        print('Остановлено: ' + type(error).__name__ + '; детали скрыты для защиты ключей. Состояние сохранено.', file=sys.stderr)
        raise SystemExit(2)
