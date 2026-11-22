#!/usr/bin/env python3
"""Deploy only the five-host RF monitor. Never enables paid IP replacement."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('monitor_deploy_infra', ROOT / 'ops/universal-reality/deploy.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
DEPLOYMENT = '276abbcb4f334b58ab25d9b2adc9d55c'
SHA = '64d46afb80adea1bf97a0d467e83f4a9ac1ebd0995891e84bca3f1a1d1affb1d'
SELECTED = [
    {'key': 'sweden-gemini', 'host_uuid': '7737ba07-6d54-435c-8c01-aea4fe1369b9',
     'node_uuid': '5c472650-1f93-470a-abdb-0caadef059b7', 'service_id': 'f5c0287a-636e-4981-95c7-80b24c9559c0',
     'service_name': 'srv-mwfa434s', 'ip_at_enrollment': '177.3.250.179'},
    {'key': 'estonia-gemini', 'host_uuid': '8a26ad46-2a93-49c0-995d-81014fbe8fce',
     'node_uuid': '6caf57c3-e03d-48ac-80e5-c51ba2fbd62a', 'service_id': '21e508f0-2ef2-47da-aee7-15dc8c66eb15',
     'service_name': 'vds100269.1cent.network', 'ip_at_enrollment': '185.252.177.203'},
    {'key': 'finland-gemini', 'host_uuid': '1db3bc12-a131-45da-8100-85344ede076d',
     'node_uuid': 'b7db14dd-d587-42d3-862e-ff784b33e375', 'service_id': '9b0316d5-fa83-4bf8-b6dc-8202b66622c9',
     'service_name': 'vds97813.1cent.network', 'ip_at_enrollment': '87.251.16.245'},
    {'key': 'finland-gemini-2', 'host_uuid': 'f07a16c5-5111-49d0-9801-f728135aa528',
     'node_uuid': '9f63b074-8b8a-4655-a2b8-02177a7c3c0f', 'service_id': '626633f5-3bf3-48c7-aaba-9bbf43f8ea9d',
     'service_name': 'vds56962.1cent.network', 'ip_at_enrollment': '87.251.16.241'},
    {'key': 'germany-gemini', 'host_uuid': 'c8274c47-23a4-47bb-b8a4-f3e5dcbac609',
     'node_uuid': 'f87b14cf-438a-4c61-acc1-7522491c05ae', 'service_id': '4d6f358a-e75f-4dd8-a635-c6ad320f2732',
     'service_name': 'vds104020.1cent.network', 'ip_at_enrollment': '177.3.248.30'},
]


def configs(panel_url):
    refresh = {'panel_url': panel_url, 'token_env': 'REMNAWAVE_API_TOKEN', 'username': 'Admin',
               'xray': '/opt/xray/26.7.28/xray', 'sha256': SHA,
               'output': '/var/lib/spn-ip-failover/routes.json',
               'coverage_output': '/var/lib/spn-ip-failover/coverage.json',
               'selected_host_uuids': [x['host_uuid'] for x in SELECTED]}
    selected = {'format': 1, 'deployment': DEPLOYMENT, 'auto_purchase_enabled': False,
                'scope': 'five-selected-ordinary-hosts-only', 'bypasses_excluded': True,
                'max_purchases_per_node': 10, 'max_daily_gross_rub': '3000.00',
                'max_monthly_per_ip_rub': '300.00', 'budget_timezone': 'Europe/Saratov',
                'refunds_replenish_daily_budget': False,
                'protected_host_uuids': ['0ae0ce08-7bbc-47a5-89b8-f6ea8c9b982b'],
                'servers': SELECTED,
                'required_before_spending': ['verified-guest-network-handoff', 'outside-control-probe',
                                            'verified-onecent-priced-order-flow', 'priced-pilot']}
    billing = {'profile': '/var/lib/spn-ip-failover/onecent-browser',
               'journal': '/var/lib/spn-ip-failover/onecent-orders',
               'budget': {'journal': '/var/lib/spn-ip-failover/onecent-daily-budget',
                          'max_daily_rub': '3000.00', 'timezone': 'Europe/Saratov'},
               'servers': {x['service_id']: {'name': x['service_name'], 'max_upfront_rub': '3000.00',
                           'max_monthly_rub': '300.00', 'removable_existing_additional_ipv4': []} for x in SELECTED}}
    return refresh, selected, billing


def payload(panel_url, token, public_key):
    r.u.need(re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?', public_key), 'Некорректный открытый ключ проверок.')
    r.u.need(token and not any(x.isspace() for x in token), 'Неверный токен панели.')
    refresh, selected, billing = configs(panel_url)
    files = {}
    for path in (ROOT / 'spn_ip_failover').rglob('*'):
        if not path.is_file() or path.suffix not in ('.py', '.sh', '.service', '.timer'):
            continue
        r.u.need(not path.is_symlink(), 'Небезопасный файл пакета контроллера.')
        files[str(path.relative_to(ROOT))] = (path.read_bytes(), 0o644)
    files['ops/host-health-audit/e2e.py'] = ((ROOT / 'ops/host-health-audit/e2e.py').read_bytes(), 0o644)
    for name, value in [('refresh.json', refresh), ('selected.json', selected), ('onecent.json', billing)]:
        files[name] = ((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode(), 0o600)
    files['remnawave'] = ((token + '\n').encode(), 0o600)
    forced = ('restrict,command="cd /opt/wayspn && exec /usr/bin/python3 -B -m spn_ip_failover.worker '
              '--config /var/lib/spn-ip-failover/routes.json" ')
    files['authorized_keys'] = ((forced + public_key + '\n').encode(), 0o600)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ip', required=True)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--ssh-interface', help='Optional physical Mac interface for this SSH connection only, e.g. en0')
    parser.add_argument('--ssh-control', type=Path, help='Optional owned private SSH master socket for this same controller')
    parser.add_argument('--probe-public-key', default=str(ROOT / '.local-state/ip-failover-ru-20261113/probe_ed25519.pub'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    ip = r.public_ipv4(args.ip)
    if not args.apply:
        print(json.dumps({'planOnly': True, 'controller': ip, 'hostCount': len(SELECTED), 'paidOperations': False}))
        return
    settings = r.u.read_settings(ROOT / '.env')
    panel = r.u.panel_url(settings['REMNAWAVE_BASE_URL'])
    data = payload(panel, settings['REMNAWAVE_API_TOKEN'], Path(args.probe_public_key).read_text().strip())
    ssh = ['ssh', '-i', str(Path(args.identity).resolve()), '-o', 'IdentitiesOnly=yes',
           '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=20',
           '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3']
    if args.ssh_control:
        path = args.ssh_control.absolute()
        info, parent = path.lstat(), path.parent.lstat()
        r.u.need(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o600 and stat.S_ISDIR(parent.st_mode)
                 and parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700,
                 'Небезопасный временный SSH-канал контроллера.')
        ssh += ['-S', str(path)]
    if args.ssh_interface:
        r.probe.validate_interface(args.ssh_interface)
        proxy = shlex.join([sys.executable, str(Path(__file__).with_name('physical_connect.py')),
                            '--ip', ip, '--interface', args.ssh_interface])
        ssh += ['-o', 'ProxyCommand=' + proxy]
    ssh += ['root@' + ip]
    stage = '/root/spn-controller-stage-' + DEPLOYMENT
    command = f'''set -eu
test "$(cat /opt/wayspn/.controller-deployment)" = {shlex.quote(DEPLOYMENT)}
test ! -L {stage}
install -d -m 700 {stage}
test "$(stat -c %u:%a {stage})" = 0:700
tar -xmf - --no-same-owner -C {stage}
'''
    result = subprocess.run(ssh + [command], input=data, stdout=subprocess.DEVNULL)
    r.u.need(result.returncode == 0, 'Загрузка контроллера не подтверждена; секретные данные скрыты.')
    command = f'''set -eu
umask 077
STAGE={stage}
test "$(cat /opt/wayspn/.controller-deployment)" = {DEPLOYMENT}
install -d -m 755 /opt/wayspn/spn_ip_failover/deploy /opt/wayspn/ops/host-health-audit
for file in "$STAGE"/spn_ip_failover/*.py; do install -m 644 "$file" /opt/wayspn/spn_ip_failover/; done
for file in "$STAGE"/spn_ip_failover/deploy/*; do install -m 644 "$file" /opt/wayspn/spn_ip_failover/deploy/; done
install -m 644 "$STAGE/ops/host-health-audit/e2e.py" /opt/wayspn/ops/host-health-audit/e2e.py
install -o spn-failover -g spn-failover -m 600 "$STAGE/refresh.json" /var/lib/spn-ip-failover/refresh.json
install -o root -g spn-failover -m 640 "$STAGE/selected.json" /etc/spn-ip-failover/selected.json
install -o root -g spn-failover -m 640 "$STAGE/onecent.json" /etc/spn-ip-failover/onecent.json
install -o root -g root -m 600 "$STAGE/remnawave" /etc/spn-ip-failover/credentials/remnawave
rm -f "$STAGE/remnawave"
install -d -o spn-failover -g spn-failover -m 700 /var/lib/spn-ip-failover/.ssh
install -o spn-failover -g spn-failover -m 600 "$STAGE/authorized_keys" /var/lib/spn-ip-failover/.ssh/authorized_keys
for name in spn-controller-health.service spn-controller-health.timer; do
    install -m 644 /opt/wayspn/spn_ip_failover/deploy/"$name" /etc/systemd/system/"$name"
done
cd /opt/wayspn
python3 -m compileall -q spn_ip_failover ops/host-health-audit/e2e.py
systemd-analyze verify /etc/systemd/system/spn-controller-health.service /etc/systemd/system/spn-controller-health.timer
systemctl daemon-reload
systemctl start spn-controller-health.service
systemctl show spn-controller-health.service -p Result -p ExecMainStatus
systemctl enable --now spn-controller-health.timer
test ! -e /etc/systemd/system/spn-ip-failover.service
printf 'Five-host read-only monitor enabled. Paid replacement service is not installed.\\n'
'''
    result = subprocess.run(ssh + [command])
    r.u.need(result.returncode == 0, 'Проверка установки не прошла; покупки не включены. Нужна проверка состояния службы.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print('Монитор не подтверждён: ' + type(exc).__name__ + '; секретные подробности скрыты.', file=sys.stderr)
        raise SystemExit(1)
