#!/usr/bin/env python3
"""Save a Cloudflare token locally with hidden input; no network operations."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import stat
import sys
import warnings


DEFAULT_FILE = '~/.config/spn/cloudflare-token'


class SetupError(RuntimeError):
    pass


def save_token(path, token):
    """Create a new private file only; never replace a file or follow symlinks."""
    if not isinstance(token, str) or not 1 <= len(token) < 4096 or any(c.isspace() for c in token):
        raise SetupError('Нужен токен без пробелов и переносов строк; значение скрыто.')
    path = Path(path).expanduser().absolute()
    for parent in reversed((path.parent, *path.parent.parents)):
        if parent.is_symlink():
            raise SetupError('Путь к токену не должен содержать симлинки.')
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise SetupError('Путь к токену не должен содержать симлинки.')
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise SetupError('Папка токена должна принадлежать вам и иметь права 0700.')
        try:
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        except FileExistsError:
            raise SetupError('Файл уже существует и не изменён. Для другого токена используйте --file.') from None
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(token + '\n')
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            os.unlink(path.name, dir_fd=directory)
            raise
    finally:
        os.close(directory)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', default=DEFAULT_FILE, help='New private local file; existing files are not overwritten')
    args = parser.parse_args(argv)
    print('Токен Cloudflare: Zone / DNS / Edit и Zone / Zone / Read только для wayspn.com.')
    print('Ввод скрыт. Токен останется на Mac, на VPS он не передаётся. Сетевых запросов нет.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = getpass.getpass('Вставьте API-токен Cloudflare: ')
        path = save_token(args.file, token)
    except getpass.GetPassWarning:
        print('Остановлено: скрытый ввод недоступен; запустите команду в обычном Терминале.', file=sys.stderr)
        return 2
    except (SetupError, OSError, EOFError, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, SetupError) else 'не удалось сохранить токен; значение скрыто.'
        print('Остановлено: ' + message, file=sys.stderr)
        return 2
    print('Сохранено с правами 0600: ' + str(path))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
