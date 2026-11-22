#!/usr/bin/env python3
"""Безопасная смена исходящего IPv4 на VPS с одним простым маршрутом Linux."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile

CONFIG = Path('/etc/network/interfaces')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'HOME': '/nonexistent'}


class MigrationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise MigrationError(message)


def ipv4(value):
    require(isinstance(value, str), 'Ожидается IPv4 без порта или маски')
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise MigrationError('Ожидается IPv4 без порта или маски') from None


def command(*args, timeout=12):
    try:
        result = subprocess.run(args, env=ENV, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise MigrationError(f'Не удалось выполнить {shlex.join(args)}') from None
    require(result.returncode == 0, f'Ошибка: {shlex.join(args)}\n{result.stderr.strip()}'.rstrip())
    return result.stdout.strip()


def ip_json(*args):
    result = json.loads(command('ip', '-j', '-4', *args))
    require(isinstance(result, list), 'Неожиданный вывод iproute2')
    return result


def build_route(default_row, dev, source):
    allowed = {'dst', 'dev', 'gateway', 'type', 'table', 'protocol', 'scope', 'prefsrc', 'metric', 'flags'}
    require(default_row.keys() <= allowed, 'Нестандартные атрибуты маршрута; изменений нет')
    require(re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', dev)
            and default_row.get('dev') == dev and default_row.get('dst') == 'default'
            and default_row.get('type', 'unicast') == 'unicast'
            and default_row.get('table', 'main') in ('main', 254),
            'Не один обычный маршрут через интерфейс нового адреса')
    flags = default_row.get('flags', [])
    require(flags in ([], ['onlink']), 'Неожиданные флаги маршрута; изменений нет')
    args = ['default', 'via', ipv4(default_row.get('gateway')), 'dev', dev]
    for key, option in [('protocol', 'proto'), ('scope', 'scope'), ('metric', 'metric')]:
        if key in default_row:
            value = default_row[key]
            require(type(value) in (str, int) and re.fullmatch(r'[A-Za-z0-9_-]+', str(value)),
                    f'Неожиданное значение {option}')
            args.extend([option, str(value)])
    if source is not None:
        args.extend(['src', ipv4(source)])
    return args + flags


def render_ifupdown(raw, dev, gateway, new, prefix):
    """Only the explicitly recognized static alias and its post-up line."""
    lines = raw.splitlines(keepends=True)
    header = re.compile(rf'iface ({re.escape(dev)}:[0-9]+) inet static[ \t]*')
    source = re.compile(rf'([ \t]+post-up /usr/sbin/ip -4 route change default via {re.escape(gateway)} '
                        rf'dev {re.escape(dev)} onlink src )([0-9.]+)([ \t]*)')
    boundaries = [i for i, line in enumerate(lines) if re.match(r'^\S', line) and not line.startswith('#')]
    matching = [(i, source.fullmatch(line.rstrip('\r\n'))) for i, line in enumerate(lines)]
    matching = [(i, m) for i, m in matching if m]
    require(len(matching) == 1, 'Не найден единственный известный post-up исходящего IP')
    route_index, route = matching[0]
    other_sources = [i for i, line in enumerate(lines) if not line.lstrip().startswith('#')
                     and re.search(r'\broute\s+(?:add|change|replace)\s+default\b.*\bsrc\s+', line)]
    require(other_sources == [route_index], 'Есть другие команды изменения исходящего IP')
    bounds = [i for i in boundaries if i < route_index]
    alias = header.fullmatch(lines[bounds[-1]].rstrip('\r\n')) if bounds else None
    require(alias is not None, 'post-up не в статическом IPv4-алиасе нужного интерфейса')
    begin = bounds[-1] + 1
    end = next((i for i in boundaries if i > route_index), len(lines))
    addresses = [(i, m) for i in range(begin, end) if (m := re.fullmatch(
        r'([ \t]+address[ \t]+)([0-9.]+)(/[0-9]+)?([ \t]*)', lines[i].rstrip('\r\n')))]
    require(len(addresses) == 1, 'В алиасе нужен ровно один IPv4-адрес')
    addr_index, addr = addresses[0]
    old = ipv4(addr[2])
    require(ipv4(route[2]) in {old, new} or old == new, 'Адрес алиаса не совпадает с его post-up')
    for i, line in enumerate(lines):
        other = re.match(r'^[ \t]+address[ \t]+([^\s#]+)', line)
        require(i == addr_index or other is None or other[1].split('/')[0] != new,
                'Новый IP уже прописан в другой секции')
    mask = re.findall(r'(?m)^[ \t]+netmask[ \t]+([0-9.]+)[ \t]*\r?$', ''.join(lines[begin:end]))
    require(len(mask) <= 1 and bool(addr[3]) != bool(mask), 'Неоднозначная маска алиаса')
    config_prefix = ipaddress.IPv4Network('0.0.0.0/' + (addr[3][1:] if addr[3] else mask[0])).prefixlen
    require(config_prefix == prefix, 'Маска нового адреса отличается от маски алиаса')
    for i, replacement in ((addr_index, addr[1] + new + (addr[3] or '') + addr[4]),
                           (route_index, route[1] + new + route[3])):
        lines[i] = replacement + lines[i][len(lines[i].rstrip('\r\n')):]
    return ''.join(lines), alias[1]


def default_route():
    rows = ip_json('route', 'show', 'table', 'main', 'default')
    require(len(rows) == 1, 'В main должен быть ровно один IPv4-маршрут по умолчанию')
    return rows[0]


def check_rules():
    rules = ip_json('rule', 'show')
    expected = [(0, 'local'), (32766, 'main'), (32767, 'default')]
    require(len(rules) == 3 and all(row.keys() <= {'priority', 'src', 'table', 'protocol'}
            and row.get('src') == 'all' for row in rules)
            and [(row.get('priority'), row.get('table')) for row in rules] == expected,
            'Найдены отдельные правила IPv4-маршрутизации; изменений нет')


def selected_source(row):
    found = ip_json('route', 'get', '1.1.1.1')
    require(len(found) == 1 and found[0].get('dev') == row.get('dev')
            and found[0].get('gateway') == row.get('gateway'), 'Проверочный адрес не идёт через этот маршрут')
    return ipv4(found[0].get('prefsrc') or found[0].get('src'))


def check_https(new, *, bind=False):
    dest = 'https://1.1.1.1/cdn-cgi/trace' if bind else 'https://www.gstatic.com/generate_204'
    options = ['--interface', new] if bind else []
    actual = command('curl', '-q', '--noproxy', '*', '-4', '-fsS', '--connect-timeout', '5',
                     '--max-time', '9', '-o', '/dev/null', '-w', '%{http_code} %{local_ip}', *options, dest)
    expected = '200' if bind else '204'
    require(actual == f'{expected} {new}', f'Ожидался HTTPS {expected} с {new}, получено: {actual or "нет ответа"}')


def config_snapshot():
    with os.fdopen(os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022, 'Неожиданные права сетевого конфига')
        raw = stream.read(65537)
        require(len(raw) <= 65536, 'Сетевой конфиг слишком большой')
    return raw, info


def migrate(new, *, apply=False, ifupdown=False):
    require(sys.platform.startswith('linux'), 'Запускай этот файл на Linux VPS, не на Mac')
    new = ipv4(new)
    addr = ipaddress.IPv4Address(new)
    require(addr.is_global and not addr.is_multicast, 'Нужен назначенный VPS публичный IPv4')
    assigned = [(iface['ifname'], item['prefixlen'])
                for iface in ip_json('address', 'show', 'scope', 'global') if 'UP' in iface.get('flags', [])
                for item in iface.get('addr_info', []) if item.get('family') == 'inet' and item.get('local') == new]
    require(len(assigned) == 1, 'Новый IP не назначен однозначно на поднятом интерфейсе. Сначала настрой его у хостера')
    dev, prefix = assigned[0]
    check_rules()
    row = default_route()
    before = build_route(row, dev, row.get('prefsrc'))
    after = build_route(row, dev, new)
    current = selected_source(row)
    persistent = None
    if ifupdown:
        require(row.get('flags') == ['onlink'] and 'metric' not in row,
                'Постоянная правка поддерживает только известный ifupdown-маршрут onlink без метрики')
        command('ifquery', '--state', dev)
        raw, meta = config_snapshot()
        candidate, alias = render_ifupdown(raw.decode(), dev, row['gateway'], new, prefix)
        persistent = raw, meta, candidate.encode(), alias
    print(f'{dev}, шлюз {row["gateway"]}: исходящий {current} → {new}', flush=True)
    if current != new:
        print('Команда: ' + shlex.join(['ip', '-4', 'route', 'change', *after]), flush=True)
    if persistent:
        print(f'Сохранение: только address и post-up в {persistent[3]} ({CONFIG})', flush=True)
    else:
        print('Только текущий маршрут. Перезагрузка или сетевой менеджер могут его сбросить.', flush=True)
    if not apply:
        print('Режим просмотра. Изменений и HTTPS-запросов нет.')
        return
    require(os.geteuid() == 0, 'Для --apply нужен root или sudo')
    check_https(new, bind=True)
    backup = Path(tempfile.mkdtemp(prefix='spn-ip-move-', dir='/root'))
    (backup / 'route.json').write_text(json.dumps(row, indent=2) + '\n')
    print(f'Резервная копия: {backup}', flush=True)
    tmp = None
    attempted = False
    committed = False
    try:
        if persistent:
            raw, meta, candidate, alias = persistent
            (backup / 'interfaces').write_bytes(raw)
            fd, name = tempfile.mkstemp(prefix='.interfaces.spn-', dir=CONFIG.parent)
            tmp = Path(name)
            with os.fdopen(fd, 'wb') as stream:
                os.fchown(stream.fileno(), meta.st_uid, meta.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(meta.st_mode))
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            queried = command('ifquery', '-i', str(tmp), alias)
            require(any(line.split() in (['address:', new], ['address:', f'{new}/{prefix}'])
                        for line in queried.splitlines()), 'ifquery не подтвердил адрес в новом конфиге')
        check_rules()
        require(default_route() == row, 'Маршрут изменился во время проверки')
        if current != new:
            attempted = True
            command('ip', '-4', 'route', 'change', *after)
        require(selected_source(row) == new, 'Ядро не выбрало новый исходящий IP')
        check_https(new)
        require(default_route() == ({**row, 'prefsrc': new} if attempted else row),
                'Маршрут изменился во время HTTPS-проверки')
        if persistent:
            actual, info = config_snapshot()
            require(actual == raw and (info.st_dev, info.st_ino, info.st_ctime_ns) ==
                    (meta.st_dev, meta.st_ino, meta.st_ctime_ns), 'Сетевой конфиг изменился во время проверки')
            os.replace(tmp, CONFIG)
        committed = True
        print(f'Готово: HTTPS 204 через {new}. Сеть и Xray не перезапускались.')
    finally:
        if attempted and not committed:
            try:
                latest = default_route()
                if latest == row:
                    pass
                elif latest == {**row, 'prefsrc': new}:
                    command('ip', '-4', 'route', 'replace', *before)
                    require(default_route() == row, 'Не удалось подтвердить откат')
                    print('Прежний маршрут возвращён.', file=sys.stderr)
                else:
                    print('Маршрут изменился параллельно; откат не перезапишет его.', file=sys.stderr)
            except (OSError, ValueError) as exc:
                print(f'Откат не подтверждён: {exc}. Проверь консоль VPS. Копия: {backup}', file=sys.stderr)
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ip', help='новый IPv4, уже назначенный на VPS')
    parser.add_argument('--apply', action='store_true', help='применить после проверок; без флага только план')
    parser.add_argument('--ifupdown', action='store_true', help='также сохранить точный известный ifupdown-алиас')
    args = parser.parse_args()

    def interrupted(number, frame):
        raise InterruptedError(f'Получен сигнал {number}')

    for number in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(number, interrupted)
    try:
        migrate(args.ip, apply=args.apply, ifupdown=args.ifupdown)
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        print(f'Стоп: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
