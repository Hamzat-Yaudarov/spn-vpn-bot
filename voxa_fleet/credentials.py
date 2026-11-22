"""Private, write-only-to-the-UI credentials. No network requests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import urlsplit


SECRET_KEYS = frozenset({"provider_token", "cloudflare_token", "remnawave_token"})
CONFIG_KEYS = frozenset({"project_id", "remnawave_url"})


class CredentialError(ValueError):
    pass


def private_directory(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise CredentialError("Папка секретов не должна содержать симлинки")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CredentialError("Папка секретов должна принадлежать пользователю программы и иметь права 0700")
    return path


def read_private(path):
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 131072):
            raise CredentialError("Небезопасный файл конфигурации")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return json.loads(handle.read(131073))
    finally:
        os.close(fd)


def write_private(path, value):
    path = Path(path)
    parent = private_directory(path.parent)
    # Validate the existing inode even when replacing its content atomically.
    read_private(path)
    fd, temporary = tempfile.mkstemp(prefix=".credential-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Credentials:
    def __init__(self, directory):
        self.directory = private_directory(directory)
        self.path = self.directory / "api.json"

    def read(self):
        value = read_private(self.path) or {}
        if not isinstance(value, dict) or set(value) - (SECRET_KEYS | CONFIG_KEYS):
            raise CredentialError("Неизвестный формат защищённой конфигурации")
        return value

    def update(self, changes):
        if not isinstance(changes, dict) or set(changes) - (SECRET_KEYS | CONFIG_KEYS):
            raise CredentialError("Неизвестные поля конфигурации")
        value = self.read()
        for key, item in changes.items():
            if item == "":
                continue  # Blank form fields never erase a previously saved token.
            if not isinstance(item, str) or not 1 <= len(item) <= 4096:
                raise CredentialError("Неверный формат поля конфигурации; значение скрыто")
            if key in SECRET_KEYS:
                if not all(33 <= ord(c) <= 126 for c in item):
                    raise CredentialError("Токен должен быть без пробелов; значение скрыто")
            elif key == "project_id":
                import re
                if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", item):
                    raise CredentialError("Нужен ID или slug проекта Voxa")
            elif key == "remnawave_url":
                url = urlsplit(item)
                if (url.scheme != "https" or not url.hostname or url.username or url.password
                        or url.query or url.fragment or url.port not in (None, 443)):
                    raise CredentialError("Для Remnawave нужен HTTPS URL без пароля и параметров")
                item = item.rstrip("/")
            value[key] = item
        write_private(self.path, value)
        return self.status()

    def status(self):
        value = self.read()
        return {**{key: bool(value.get(key)) for key in SECRET_KEYS},
                **{key: value.get(key, "") for key in CONFIG_KEYS}}
