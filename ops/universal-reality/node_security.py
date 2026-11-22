"""Install and verify the isolated IP/scanner guard over an already trusted SSH connection."""
from pathlib import Path
import json
import shlex

HERE = Path(__file__).resolve().parent
CONTROLLER_IP = '147.45.164.28'


def files(*, reality=False):
    names = ['node_guard.py', 'scanner_guard.py', 'scanner_lists.json', 'node_guard_upgrade.py']
    if reality:
        names += ['reality_sni_guard.py', 'guard.py', 'core_fd_health.py', 'haproxy-reality.cfg']
    return {name: (HERE / name).read_bytes() for name in names}


def guard(ssh, active_ip, *, old_ip=None, verify=False, controller_ip=CONTROLLER_IP):
    plan = ssh.plan
    if not verify:
        # Only the explicitly pinned downloader repair is eligible; unknown
        # code or an administrator's edits are refused by the guest helper.
        ssh.run(shlex.join(['python3', '/root/spn-reality-managed-' + plan['id'] + '/node_guard_upgrade.py',
                            '--deployment', plan['id']]), capture=True)
    fields = ['python3', '/root/spn-reality-managed-' + plan['id'] + '/node_guard.py',
              'verify' if verify else 'install', '--active-ip', active_ip,
              '--deployment', plan['id'], '--panel-ip', plan['panelIp'], '--controller-ip', controller_ip]
    if old_ip is not None and not verify:
        fields += ['--old-ip', old_ip]
    result = json.loads(ssh.run(shlex.join(fields), capture=True))
    if (not isinstance(result, dict) or result.get('activeIp') != active_ip or result.get('deployment') != plan['id']
            or any(result.get(key) is not True for key in
                   ('reserveVpnBlocked', 'scannerFilterEnabled', 'bootPersistenceVerified'))):
        raise ValueError('Защита резервных IP и её сохранение после перезагрузки не подтверждены')
    return result


def sni(ssh, *, verify=False):
    fields = ['python3', '/root/spn-reality-managed-' + ssh.plan['id'] + '/reality_sni_guard.py',
              'verify' if verify else 'install', '--ip', ssh.plan['ip'],
              '--deployment', ssh.plan['id'], '--sni', ssh.plan.get('sni', 'www.cloudflare.com')]
    result = json.loads(ssh.run(shlex.join(fields), capture=True))
    if (not isinstance(result, dict) or result.get('allowedSni') != ssh.plan.get('sni', 'www.cloudflare.com')
            or result.get('tlsTerminated') is not False
            or any(result.get(key) is not True for key in ('sniFiltered', 'validSniHandshakeVerified',
                                                         'unknownSniRejected', 'missingSniRejected'))):
        raise ValueError('Фильтр TLS-имён REALITY не подтверждён')
    return result
