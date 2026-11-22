#!/usr/bin/env python3
"""Open short-lived local SSH masters for diagnosing an existing cascade.

No remote configuration, password storage, authorized_keys changes or purchases.
Passwords are entered only into standard SSH prompts in the owner's Terminal.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cascade_support_deploy', HERE / 'deploy.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ru-ip', required=True)
    parser.add_argument('--eu-ip', required=True)
    parser.add_argument('--ru-ssh-port', type=int, default=22)
    parser.add_argument('--eu-ssh-port', type=int, default=22)
    parser.add_argument('--state-dir')
    parser.add_argument('--env-file', default=str(d.r.ROOT / '.env'))
    parser.add_argument('--close', action='store_true')
    args = parser.parse_args()
    args.ru_ip, args.eu_ip = d.r.public_ipv4(args.ru_ip), d.r.public_ipv4(args.eu_ip)
    settings = d.u.read_settings(Path(args.env_file).expanduser())
    base = d.u.panel_url(settings.get('REMNAWAVE_BASE_URL', 'https://panel.wayspn.online/api'))
    directory = d.directory(base, args)
    d.u.need(directory.is_dir(), 'Нет локального состояния этого каскада.')
    state = d.u.Store(directory)
    plan = state.load('plan.json')
    d.c.validate_plan(plan)
    d.u.need(all(plan[role] == {'ip': getattr(args, role + '_ip'), 'sshPort': getattr(args, role + '_ssh_port')}
                 for role in ('ru', 'eu')), 'Адреса отличаются от сохранённого каскада.')
    path = Path('/private/tmp' if sys.platform == 'darwin' else '/tmp') / ('spn-cascade-support-' + args.ru_ip + '-' + args.eu_ip)
    access = d.u.Store(path)
    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'SSH_AUTH_SOCK', 'TERM')}
    opened = []
    try:
        for role in ('ru', 'eu'):
            ip, port = plan[role]['ip'], plan[role]['sshPort']
            known = d.r.trust.resolve(ip, port, d.u.Store(state.path / ('ssh-' + role)))
            socket = str(path / role)
            command = ['ssh', '-p', str(port), '-S', socket, '-o', 'ConnectTimeout=15',
                       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                       '-o', 'UserKnownHostsFile=' + str(known), '-o', 'GlobalKnownHostsFile=/dev/null',
                       '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlgorithms=ssh-ed25519',
                       '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ClearAllForwardings=yes', '-o', 'SendEnv=-*']
            target = 'root@' + ip
            check = subprocess.run(command + ['-o', 'ProxyCommand=/usr/bin/false', '-O', 'check', target],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if args.close:
                if check.returncode == 0:
                    subprocess.run(command + ['-o', 'ProxyCommand=/usr/bin/false', '-O', 'exit', target], env=env, check=True)
                continue
            if check.returncode:
                d.u.need(not Path(socket).exists(), 'Остался неактивный SSH socket; новый сеанс не создан поверх него.')
                process = subprocess.run(command + ['-M', '-N', '-f', '-o', 'ControlPersist=3600', target], env=env)
                d.u.need(process.returncode == 0, 'SSH не подключился к ' + role.upper() + '.')
                opened.append((command, target))
        if not args.close:
            access.save('access.json', {'version': 1, 'ru': plan['ru'], 'eu': plan['eu'], 'stateDir': str(state.path)})
        print('Временные SSH-сеансы закрыты.' if args.close else 'Готово. Временный доступ: ' + str(path))
    except BaseException:
        for command, target in opened:
            subprocess.run(command + ['-o', 'ProxyCommand=/usr/bin/false', '-O', 'exit', target], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise


if __name__ == '__main__':
    try:
        main()
    except (d.u.DeployError, d.c.ConfigError, d.r.trust.TrustError, KeyboardInterrupt) as error:
        print('Остановлено: ' + ('прервано' if isinstance(error, KeyboardInterrupt) else str(error)), file=sys.stderr)
        raise SystemExit(2)
