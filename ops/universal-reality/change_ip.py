#!/usr/bin/env python3
"""Change an assigned IPv4 of an existing DNS-backed TLS/REALITY VPS, without reinstalling it."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location('spn_reality_change_ip', Path(__file__).with_name('replace.py'))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def main(argv=None):
    parser = r.parser()
    parser.description = __doc__
    parser.set_defaults(same_vps=True)
    parser._option_string_actions['--ip'].help = 'New public IPv4 already assigned to the SAME VPS'
    args = parser.parse_args(argv)
    try:
        r.run(args)
    except (r.u.DeployError, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, r.u.DeployError) else 'прервано пользователем.'
        print('Остановлено: ' + message + ' Журнал сохранён. Повторите ту же команду change_ip.py '
              'с теми же --domain и --ip. Не удаляйте адреса и локальный журнал до завершения.', file=sys.stderr)
        return 2
    except Exception as error:
        print('Остановлено: ' + type(error).__name__ + '; детали скрыты для защиты ключей. '
              'Смена IP не объявлена завершённой. Повторите ту же команду.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
