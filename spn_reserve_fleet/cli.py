"""Run the panel and a local SSH observer, or perform an explicit one-off action."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import getpass
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event, Thread
import time

from .enrollment import register_installation
from .runtime import Runtime
from .store import DEFAULT_ROOT, private_file


@contextmanager
def monitor_lock(root):
    fd = os.open(private_file(root / 'monitor.lock'), os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Монитор уже запущен для этого каталога') from None
        yield
    finally:
        os.close(fd)


def monitor(runtime, stop):
    pending = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix='ssh-observer') as workers:
        while not stop.is_set():
            started = time.monotonic()
            try:
                runtime.scan()
                for server in runtime.store.list_servers():
                    server_id = server['id']
                    previous = pending.get(server_id)
                    if previous is not None:
                        if not previous.done():
                            continue
                        try:
                            previous.result()
                        except Exception:
                            print('Проверка сервера не завершена; проверьте состояние в панели.', file=sys.stderr, flush=True)
                    pending[server_id] = workers.submit(runtime.check, server_id)
            except Exception:
                print('Цикл мониторинга остановлен ошибкой; следующая проверка по расписанию.', file=sys.stderr, flush=True)
            stop.wait(max(1, runtime.interval - (time.monotonic() - started)))


def parser():
    p = argparse.ArgumentParser(description='WAY SPN: панель и проверка SSH резервных IP той же VPS')
    p.add_argument('--data-dir', default=DEFAULT_ROOT, help='Приватный каталог панели, по умолчанию ~/.spn-reserve-fleet')
    p.add_argument('--env-file', help='Файл REMNAWAVE_BASE_URL / REMNAWAVE_API_TOKEN для скрипта смены IP')
    p.add_argument('--interval', type=int, default=30, help='Интервал проверки SSH в секундах (30)')
    p.add_argument('--failures', type=int, default=3, help='Последовательных отказов перед переключением (3)')
    p.add_argument('--cooldown', type=int, default=300, help='Минимальная пауза между сменами в секундах (300)')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='Задать пароль панели и найти завершённые установки')
    sub.add_parser('password', help='Изменить пароль панели')
    sub.add_parser('import', help='Добавить установку из указанного каталога').add_argument('--state-dir', required=True)
    handoff = sub.add_parser('handoff', help='Передать управление установкой на настроенный VPS-контроллер')
    handoff.add_argument('--state-dir', required=True)
    handoff.add_argument('--controller-file', help='Приватный файл настроек SSH-контроллера')
    receive = sub.add_parser('receive', help=argparse.SUPPRESS)
    receive.add_argument('--journal-root', required=True)
    receive.add_argument('--request-id', required=True)
    sub.add_parser('scan', help='Найти ранее созданные серверы в ~/.spn-reality-deploy')
    status = sub.add_parser('status', help='Состояние серверов без сетевых проверок')
    status.add_argument('--include-archived', action='store_true', help='Показать также архивные записи')
    archive = sub.add_parser('archive', help='Убрать неиспользуемую установку из управления, сохранив историю')
    archive.add_argument('--server', required=True)
    archive.add_argument('--reason', required=True)
    archive.add_argument('--replacement', help='Домен действующей записи, заменившей старую')
    sub.add_parser('restore', help='Восстановить запись из архива без включения автоматики').add_argument('--server', required=True)
    sub.add_parser('monitor', help='Проверять SSH постоянно без веб-панели')
    serve = sub.add_parser('serve', help='Запустить панель и постоянный мониторинг')
    serve.add_argument('--host', default='127.0.0.1', help='Адрес прослушивания панели')
    serve.add_argument('--port', type=int, default=8787)
    serve.add_argument('--no-monitor', action='store_true', help='Только панель: монитор запущен отдельным процессом')
    check = sub.add_parser('check', help='Разовая проверка сервера; при включённой автоматике может менять IP')
    check.add_argument('--server', required=True, help='Домен сервера')
    switch = sub.add_parser('switch', help='Сменить IP на заранее добавленный резервный')
    switch.add_argument('--server', required=True)
    switch.add_argument('--ip', required=True)
    switch.add_argument('--apply', action='store_true')
    reserve = sub.add_parser('reserve', help='Добавить резервный IPv4 той же VPS')
    reserve.add_argument('--server', required=True)
    reserve.add_argument('--ip', required=True)
    auto = sub.add_parser('auto', help='Включить или выключить автоматическую смену IP')
    auto.add_argument('--server', required=True)
    auto.add_argument('--enabled', choices=('yes', 'no'), required=True)
    harden = sub.add_parser('harden', help='Защитить резервные IP и добавить фильтр сканеров/HTTPS-сайт')
    harden.add_argument('--server', required=True)
    harden.add_argument('--apply', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'switch' and not args.apply:
        from .store import public_ipv4
        try:
            ip = public_ipv4(args.ip)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps({'planOnly': True, 'server': args.server, 'newIp': ip, 'changes': False}, ensure_ascii=False))
        return 0
    try:
        if args.command == 'receive':
            from .remote import MAX_ARCHIVE, receive_archive
            result = receive_archive(sys.stdin.buffer.read(MAX_ARCHIVE + 1), data_dir=args.data_dir,
                                     journal_root=args.journal_root, request_id=args.request_id)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == 'handoff':
            from .remote import handoff_installation, load_controller
            controller = load_controller(args.controller_file)
            if controller is None:
                raise ValueError('Сначала настройте ~/.config/spn/reserve-controller.json')
            result = handoff_installation(args.state_dir, controller, local_data_dir=args.data_dir)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        runtime = Runtime(args.data_dir, failure_threshold=args.failures, cooldown=args.cooldown,
                          interval=args.interval, env_file=args.env_file)
        if args.command in ('init', 'password'):
            if args.command == 'init' and runtime.auth.configured():
                raise ValueError('Пароль уже задан. Для изменения используйте password')
            password = getpass.getpass('Пароль панели (минимум 16 символов): ')
            if password != getpass.getpass('Повторите пароль: '):
                raise ValueError('Пароли не совпали')
            runtime.auth.set_password(password, replace=args.command == 'password')
            result = runtime.scan()
            print('Пароль сохранён. Найдено установок: ' + str(result['imported']))
        elif args.command == 'import':
            print(register_installation(args.state_dir, args.data_dir)['domain'])
        elif args.command == 'scan':
            print(json.dumps(runtime.scan(), ensure_ascii=False))
        elif args.command == 'status':
            from .web import public_server
            print(json.dumps([public_server(row) for row in runtime.store.list_servers(include_archived=args.include_archived)],
                             ensure_ascii=False, indent=2))
        elif args.command in ('archive', 'restore'):
            from .web import public_server
            result = (runtime.store.archive(args.server, args.reason, args.replacement) if args.command == 'archive'
                      else runtime.store.restore(args.server))
            print(json.dumps(public_server(result), ensure_ascii=False, indent=2))
        elif args.command == 'reserve':
            runtime.store.add_reserve(args.server, args.ip)
            print('Резерв добавлен')
        elif args.command == 'auto':
            runtime.store.set_auto(args.server, args.enabled == 'yes')
            print('Автосмена ' + ('включена' if args.enabled == 'yes' else 'выключена'))
        elif args.command == 'harden':
            from .hardening import harden
            print(json.dumps(harden(runtime, args.server, env_file=args.env_file, apply=args.apply),
                             ensure_ascii=False, indent=2))
        elif args.command in ('check', 'switch'):
            from .web import public_server
            result = runtime.check(args.server) if args.command == 'check' else runtime.switch(args.server, args.ip)
            print(json.dumps(public_server(result), ensure_ascii=False, indent=2))
        elif args.command == 'monitor':
            stop = Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            with monitor_lock(runtime.store.root):
                print('Мониторинг SSH запущен из сети этой машины.', flush=True)
                monitor(runtime, stop)
        elif args.command == 'serve':
            if not runtime.auth.configured():
                raise ValueError('Сначала задайте пароль: python3 -m spn_reserve_fleet init')
            if not 1 <= args.port <= 65535:
                raise ValueError('Неверный порт панели')
            import uvicorn
            from .web import create_app
            from contextlib import nullcontext
            stop = Event()
            with monitor_lock(runtime.store.root) if not args.no_monitor else nullcontext():
                thread = None
                if not args.no_monitor:
                    thread = Thread(target=monitor, args=(runtime, stop), daemon=True, name='ssh-monitor')
                    thread.start()
                try:
                    uvicorn.run(create_app(runtime=runtime), host=args.host, port=args.port, workers=1)
                finally:
                    stop.set()
                    if thread:
                        thread.join(timeout=5)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError) as error:
        print('Остановлено: ' + str(error), file=sys.stderr)
        return 2
