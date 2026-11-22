"""Password authentication and short-lived owner sessions for the reserve panel."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
from threading import Lock
import time


def _read(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError("Файл пароля должен принадлежать вам и иметь права 0600")
    if info.st_size > 4096:
        raise ValueError("Некорректный файл пароля")
    try:
        result = json.loads(path.read_text())
        if (result.get("algorithm") != "scrypt" or len(bytes.fromhex(result["salt"])) != 16
                or len(bytes.fromhex(result["digest"])) != 64):
            raise ValueError()
        return result
    except (ValueError, KeyError, AttributeError, TypeError):
        raise ValueError("Некорректный файл пароля") from None


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Каталог панели должен принадлежать вам и иметь права 0700")
    _read(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".password-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Auth:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.sessions, self.failures, self.lock = {}, {}, Lock()

    def configured(self):
        return _read(self.path) is not None

    def set_password(self, password, *, replace=False):
        if self.configured() and not replace:
            raise ValueError("Пароль уже задан; для смены используйте явный сброс пароля")
        if not isinstance(password, str) or not 16 <= len(password) <= 1024:
            raise ValueError("Пароль панели: от 16 до 1024 символов")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()
        _write(self.path, {"algorithm": "scrypt", "salt": salt.hex(), "digest": digest})
        with self.lock:
            self.sessions.clear()

    def _prune(self):
        now = self.clock()
        self.sessions = {key: value for key, value in self.sessions.items() if value["expires"] > now}
        self.failures = {key: values for key, values in self.failures.items()
                         if values and values[-1] > now - 600}

    def session(self, token=None):
        with self.lock:
            self._prune()
            key = hashlib.sha256((token or "").encode()).hexdigest()
            if key in self.sessions:
                return token, dict(self.sessions[key])
            if len(self.sessions) >= 2048:
                raise ValueError("Слишком много сессий; повторите позже")
            token = secrets.token_urlsafe(32)
            state = {"authenticated": False, "csrf": secrets.token_urlsafe(32), "expires": self.clock() + 1800}
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = state
            return token, dict(state)

    def login(self, token, password, source):
        with self.lock:
            self._prune()
            recent = [stamp for stamp in self.failures.get(source, []) if stamp > self.clock() - 600]
            if len(recent) >= 5:
                raise ValueError("Слишком много попыток входа. Повторите через 10 минут")
        config = _read(self.path)
        valid = False
        if config and isinstance(password, str) and len(password) <= 1024:
            computed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(config["salt"]), n=16384, r=8, p=1).hex()
            valid = hmac.compare_digest(computed, config["digest"])
        if not valid:
            with self.lock:
                self.failures[source] = recent + [self.clock()]
            raise ValueError("Неверный пароль")
        self.logout(token)
        new, state = self.session()
        with self.lock:
            key = hashlib.sha256(new.encode()).hexdigest()
            self.sessions[key]["authenticated"] = True
            state = dict(self.sessions[key])
            self.failures.pop(source, None)
        return new, state

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)

    @staticmethod
    def csrf_valid(session, supplied):
        return isinstance(supplied, str) and hmac.compare_digest(session.get("csrf", ""), supplied)
