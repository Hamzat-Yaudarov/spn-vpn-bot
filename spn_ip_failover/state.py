"""Durable, single-controller journal. Keep its directory private and backed up."""

import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile


class JournalError(RuntimeError):
    """The controller cannot guarantee that its transaction state was saved."""


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.lock = None
        self.data = None

    def __enter__(self):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.validate_directory()
        self.lock = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.data = self.snapshot()
        except BaseException:
            os.close(self.lock)
            self.lock = None
            raise
        return self

    def __exit__(self, *_):
        os.close(self.lock)
        self.lock = None

    def validate_directory(self):
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Каталог журнала должен принадлежать текущему пользователю и иметь режим 0700")

    def snapshot(self):
        try:
            self.validate_directory()
            fd = os.open(self.directory / "state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"version": 1, "servers": {}}
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("Журнал должен быть приватным обычным файлом")
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("servers"), dict):
            raise ValueError("Неизвестный формат журнала")
        return data

    def save(self):
        try:
            fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.directory)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(self.data, stream, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.directory / "state.json")
                directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except OSError:
            raise JournalError("Не удалось сохранить журнал. Применение остановлено") from None
