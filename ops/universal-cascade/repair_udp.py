#!/usr/bin/env python3
"""Repair one owned format-2 cascade's EU UDP port, retaining VPN identities.

No profile/node/host recreation, key rotation, network restart or VPN-core
restart. Private before/after snapshots stay in the installer's 0700 state.
"""
import argparse
import base64
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import uuid


HERE = Path(__file__).resolve().parent


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


d = module('cascade_udp_deploy', HERE / 'deploy.py')
h = module('cascade_udp_repair_helpers', HERE / 'repair_udp_remote.py')


def raw(item):
    return base64.b64decode(item['data'], validate=True)


def replacements(before, after, role, snapshot, keys):
    stage = '/root/spn-cascade-managed-' + before['id']
    old_wg = d.c._wireguard(before, role, keys).encode()
    new_wg = d.c._wireguard(after, role, keys).encode()
    names = ['/etc/wireguard/' + before['link']['interface'] + '.conf', stage + '/wg.conf',
             '/opt/spn-cascade/wg.conf' if role == 'eu' else
             '/opt/spn-cascade-ru/branches/' + before['id'] + '/wg.conf']
    d.u.need(all(raw(snapshot[name]) == old_wg for name in names), 'Конфигурация WireGuard отличается от локального плана.')
    result = {name: new_wg for name in names}
    if role == 'eu':
        for path in ('/opt/spn-cascade/.spn-parameters', stage + '/preflight-ok'):
            d.u.need(raw(snapshot[path]) == (h.eu_parameters(before) + '\n').encode(), 'Метаданные EU изменены.')
            result[path] = (h.eu_parameters(after) + '\n').encode()
    else:
        marker = stage + '/preflight-ok'
        d.u.need(raw(snapshot[marker]) == (h.ru_preflight(before) + '\n').encode(), 'Метаданные RU изменены.')
        result[marker] = (h.ru_preflight(after) + '\n').encode()
        registry = '/opt/spn-cascade-ru/registry.json'
        result[registry] = h.update_ru_registry(raw(snapshot[registry]), before['id'], before['link']['port'],
                                              after['link']['port'], old_wg, new_wg)
    return {name: base64.b64encode(data).decode() for name, data in result.items()}


def commit(store, before, after):
    d.u.need(store.load('plan.json') == before, 'Локальный план изменён параллельно; замена остановлена.')
    data = (json.dumps(after, ensure_ascii=False, indent=2) + '\n').encode()
    fd, temp = tempfile.mkstemp(prefix='.udp-plan-', dir=store.path)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, store.path / 'plan.json')
        parent = os.open(store.path, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ru-ip', required=True)
    parser.add_argument('--eu-ip', required=True)
    parser.add_argument('--ru-ssh-port', type=int, default=22)
    parser.add_argument('--eu-ssh-port', type=int, default=22)
    parser.add_argument('--eu-wg-port', type=int, required=True)
    parser.add_argument('--ru-ssh-control')
    parser.add_argument('--eu-ssh-control')
    parser.add_argument('--state-dir')
    parser.add_argument('--env-file', default=str(d.r.ROOT / '.env'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    args.ru_ip, args.eu_ip = d.r.public_ipv4(args.ru_ip), d.r.public_ipv4(args.eu_ip)
    settings = d.u.read_settings(Path(args.env_file).expanduser())
    base = d.u.panel_url(settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    folder = d.directory(base, args)
    d.u.need(folder.is_dir(), 'Нет сохранённого каскада.')
    store = d.u.Store(folder)
    host = d.u.Store(store.path.parent / '.ru-locks' / (args.ru_ip + '-ssh' + str(args.ru_ssh_port)))
    with host.lock(), store.lock():
        before = store.load('plan.json')
        d.c.validate_plan(before)
        d.u.need(before['format'] == 2 and all(before[role] == {'ip': getattr(args, role + '_ip'),
                 'sshPort': getattr(args, role + '_ssh_port')} for role in ('ru', 'eu')), 'Нужен исходный format-2 каскад с теми же VPS.')
        after = copy.deepcopy(before); after['link']['port'] = args.eu_wg_port
        d.c.validate_plan(after)
        if before == after:
            print('Этот UDP-порт уже сохранён; повторный ремонт не нужен.'); return
        if not args.apply:
            print('План: UDP EU ' + str(before['link']['port']) + ' → ' + str(args.eu_wg_port) + '; UUID и ключи сохраняются.'); return
        keys = d.c.wg_keys(store)
        transaction = 'udp-repair-' + uuid.uuid4().hex
        store.save(transaction + '.intent.json', {'before': before, 'after': after})
        snapshots, applied, servers = {}, {}, {}
        committed = False
        with contextlib.ExitStack() as stack:
            for role in ('ru', 'eu'):
                known = d.r.trust.resolve(before[role]['ip'], before[role]['sshPort'], d.u.Store(store.path / ('ssh-' + role)))
                servers[role] = stack.enter_context(d.SSH(before, role, known, control_socket=getattr(args, role + '_ssh_control')))
                servers[role].upload({'repair_udp_remote.py': (HERE / 'repair_udp_remote.py').read_bytes(),
                                      'repair_udp_rpc.py': (HERE / 'repair_udp_rpc.py').read_bytes()})
                servers[role].action('preflight')

            def rpc(role, action, **fields):
                request = {'action': action, 'role': role, 'beforePlan': before, 'afterPlan': after, **fields}
                data = json.dumps(request).encode()
                command = shlex.join(['python3', servers[role].stage + '/repair_udp_rpc.py'])
                return json.loads(servers[role].run(command, data, capture=True))

            for role in ('ru', 'eu'):
                snapshots[role] = rpc(role, 'inspect')
                store.save(transaction + '.' + role + '.before.json', snapshots[role])
            changes = {role: replacements(before, after, role, snapshots[role], keys) for role in ('ru', 'eu')}
            try:
                for role in ('eu', 'ru'):
                    applied[role] = rpc(role, 'apply', snapshot=snapshots[role], changes=changes[role])
                    store.save(transaction + '.' + role + '.after.json', applied[role])
                for role in ('ru', 'eu'):
                    servers[role].cascade = after
                    servers[role].action('verify')
                commit(store, before, after)
                committed = True
                store.save(transaction + '.complete.json', {'verified': True, 'euUDPPort': args.eu_wg_port})
            finally:
                if not committed:
                    for role in ('ru', 'eu'):
                        if role in applied:
                            rpc(role, 'rollback', snapshot=snapshots[role], expected=applied[role])
        print('UDP-порт EU изменён и проверен: ' + str(args.eu_wg_port) + '. Нода, профиль, хост и ключи сохранены.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        safe = str(error) if isinstance(error, (d.u.DeployError, d.c.ConfigError, h.RepairError)) else type(error).__name__
        print('UDP-ремонт остановлен: ' + safe + '. Приватные резервные копии сохранены; успех не подтверждён.', file=sys.stderr)
        raise SystemExit(2)
