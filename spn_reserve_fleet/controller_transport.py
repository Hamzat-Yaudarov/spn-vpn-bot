"""Local SSH routing overrides, separate from immutable controller identity.

The caller must keep StrictHostKeyChecking=yes. HostKeyAlias deliberately keeps
the original controller's trusted host key when its public IP changes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat


DEFAULT_TRANSPORT_CONFIG = '~/.config/spn/reserve-controller-transport.json'


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _host(value):
    return (isinstance(value, str) and 1 <= len(value) <= 253
            and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value)
            and '..' not in value)


def ssh_transport_options(controller, path=None):
    """Resolve only the network destination; never mutate controller/journals.

    Example private JSON: {"format": 1, "controllers": [{"host": "old.example",
    "port": 22, "connect_host": "new.example", "bind_interface": "en0"}]}.
    bind_interface is optional and affects only this matching transport route.
    An explicit missing path is an error; an absent default file is optional.
    """
    selected = Path(path or DEFAULT_TRANSPORT_CONFIG).expanduser().absolute()
    _require(not any(item.is_symlink() for item in (selected, *selected.parents)),
             'Настройки маршрута контроллера не должны быть символической ссылкой')
    try:
        descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        _require(path is None, 'Файл маршрута SSH-контроллера не найден')
        return []
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                 and info.st_size <= 16384,
                 'Файл маршрута контроллера должен принадлежать вам и иметь права 0600')
        try:
            value = json.loads(source.read(16385))
        except (ValueError, UnicodeDecodeError):
            raise ValueError('Некорректный JSON маршрута контроллера') from None
    _require(isinstance(value, dict) and set(value) == {'format', 'controllers'}
             and type(value['format']) is int and value['format'] == 1
             and isinstance(value['controllers'], list) and len(value['controllers']) <= 100,
             'Неизвестный формат маршрутов контроллера')
    seen, result = set(), []
    for route in value['controllers']:
        required = {'host', 'port', 'connect_host'}
        _require(isinstance(route, dict) and required <= set(route) <= required | {'bind_interface'}
                 and _host(route['host']) and _host(route['connect_host'])
                 and type(route['port']) is int and 1 <= route['port'] <= 65535
                 and ('bind_interface' not in route or
                      (isinstance(route['bind_interface'], str) and
                       re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,31}', route['bind_interface']))),
                 'Некорректный маршрут SSH-контроллера')
        identity = (route['host'], route['port'])
        _require(identity not in seen, 'Повторный маршрут SSH-контроллера')
        seen.add(identity)
        if identity == (controller['host'], controller['port']):
            # HostKeyAlias replaces the whole known_hosts lookup name.
            alias = route['host'] if route['port'] == 22 else f"[{route['host']}]:{route['port']}"
            result = ['-o', 'HostName=' + route['connect_host'],
                      '-o', 'HostKeyAlias=' + alias]
            if 'bind_interface' in route:
                result += ['-o', 'BindInterface=' + route['bind_interface']]
    return result
