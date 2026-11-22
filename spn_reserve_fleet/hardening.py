"""Explicit, resumable hardening of an enrolled, already owned VPN node."""
from __future__ import annotations

import json
from pathlib import Path
import time

from .enrollment import replacement_module, synchronize
from .probe import check_ssh


def harden(runtime, server_id, *, env_file, apply=False):
    row = runtime._active_server(server_id)
    if not apply:
        return {'planOnly': True, 'server': server_id, 'activeIp': row['current_ip'],
                'actions': ['isolate reserve VPN ports', 'filter listed scanners',
                            'HTTPS fallback site' if 'tls' in row['transport'] else 'REALITY SNI filter'],
                'changes': False}
    if not env_file:
        raise ValueError('Укажите --env-file с учётными данными существующей панели')
    r = replacement_module()
    d, u = r.d, r.u
    security = d.local_module('node_security')
    with runtime.store.server_lock(server_id):
        row = runtime._active_server(server_id)
        original = u.Store(Path(row['state_dir']))
        with original.lock():
            from .remote import require_local_control
            require_local_control(original)
            plan = r.validate_original(original, row['domain'], row['panel_url'])
            commits, pending = r.chain(original, plan)
            if pending:
                raise ValueError('Сначала разберите незавершённую смену IP этой установки')
            active = commits[-1]['toIp'] if commits else plan['ip']
            identity = r.original_machine(original, commits)
            settings = u.read_settings(Path(env_file).expanduser())
            u.need(settings.get('REMNAWAVE_BASE_URL', '').rstrip('/') == plan['panel']
                   and settings.get('REMNAWAVE_API_TOKEN'), 'Нет подходящих учётных данных панели')
            api = d.API(plan['panel'], settings['REMNAWAVE_API_TOKEN'])
            # An interrupted site upgrade is resumed by its own strict profile
            # transition validator. Otherwise audit the full topology first.
            if original.load('tls-site-v1.intent.json') is None:
                r.topology(api, original, plan)
            candidates = [active] + [item['ip'] for item in row['reserves'] if item['ip'] != active]
            connected = next((ip for ip in candidates if check_ssh(ip, row['ssh_port'])), None)
            if connected is None:
                raise ValueError('Нет доступного SSH-адреса этой VPS; защита пока не установлена')
            audit = u.Store(original.path / 'hardening-v1' / (connected + '-ssh' + str(row['ssh_port'])))
            known_hosts = d.trust.resolve(connected, row['ssh_port'], audit,
                                         expected=identity['sshFingerprint'], interactive=False)
            current_plan = d.tls_runtime_plan(plan, original)
            guest_plan = {**current_plan, 'ip': connected, 'sshPort': row['ssh_port'],
                          'nodeIp': active, 'panelIp': d.panel_ipv4(plan['panel']), 'batchMode': True}
            if d.is_tls(plan):
                guest_plan['tlsSiteVersion'] = 1
            with d.SSH(guest_plan, known_hosts) as ssh:
                ssh.upload({'managed-node.sh': d.managed_script(plan)})
                observed = json.loads(ssh.action('identity', capture=True))
                u.need(observed.get('ownedInstallation') is True
                       and all(observed.get(key) == value for key, value in identity.items()),
                       'VPS была переустановлена или относится к другой установке; защита не изменена')
                u.need(active in ssh.assigned_ipv4(), 'Текущий IP не назначен доверенной VPS')
                ssh.upload(security.files(reality=not d.is_tls(plan)))
                guard = security.guard(ssh, active)
                if d.is_tls(plan):
                    ssh.upload(d.render_files(guest_plan, original.load('node-secret.json')['value']))
                    ssh.action('site-install')
                    if not plan.get('tlsSiteVersion'):
                        d.upgrade_tls_site_profile(api, original, plan)
                    # wait_ready does not prove a newly pushed inbound has
                    # arrived; site-verify itself waits for both HTTP versions.
                    ssh.action('site-verify')
                else:
                    security.sni(ssh)
                security.guard(ssh, active, verify=True)
                r.topology(api, original, plan)
                proof = {'format': 1, 'server': server_id, 'activeIp': active,
                         'deployment': plan['id'], 'guard': guard,
                         'httpsSiteVerified': d.is_tls(plan),
                         'sniFilterVerified': not d.is_tls(plan), 'verifiedAt': time.time()}
                original.save('security-v1-' + u.digest(proof) + '.verified.json', proof)
            synchronize(runtime.store, original.path, locked=True)
            runtime.store.event(server_id, 'security_enabled',
                                'Защита включена: VPN закрыт на резервах, сканеры фильтруются; '
                                + ('HTTPS-сайт проверен' if d.is_tls(plan) else 'TLS-имя REALITY проверяется'))
            return proof
