"""Single-owner admin authentication; no default password and no bearer UI secret."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from threading import Lock

from .credentials import read_private, write_private


class Auth:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self.sessions, self.failures, self.lock = {}, {}, Lock()

    def configured(self):
        return read_private(self.path) is not None

    def set_password(self, password, *, replace=False):
        if self.configured() and not replace:
            raise ValueError("Пароль уже задан; существующий пароль не изменён")
        if not isinstance(password, str) or not 16 <= len(password) <= 1024:
            raise ValueError("Пароль панели: от 16 до 1024 символов")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()
        write_private(self.path, {"algorithm": "scrypt", "salt": salt.hex(), "digest": digest})
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
            value = self.sessions.get(key)
            if value:
                return token, dict(value)
            if len(self.sessions) >= 2048:
                raise ValueError("Слишком много сессий; повторите позже")
            token = secrets.token_urlsafe(32)
            value = {"authenticated": False, "csrf": secrets.token_urlsafe(32), "expires": self.clock() + 1800}
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = value
            return token, dict(value)

    def login(self, token, password, source):
        with self.lock:
            self._prune()
            recent = [stamp for stamp in self.failures.get(source, []) if stamp > self.clock() - 600]
            if len(recent) >= 5:
                raise ValueError("Слишком много попыток входа. Повторите через 10 минут")
        config = read_private(self.path)
        valid = False
        if config and isinstance(password, str) and len(password) <= 1024:
            computed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(config["salt"]), n=16384, r=8, p=1).hex()
            valid = hmac.compare_digest(computed, config["digest"])
        if not valid:
            with self.lock:
                self.failures[source] = recent + [self.clock()]
            raise ValueError("Неверный пароль")
        self.logout(token)
        new, value = self.session()
        with self.lock:
            key = hashlib.sha256(new.encode()).hexdigest()
            self.sessions[key]["authenticated"] = True
            value = dict(self.sessions[key])
            self.failures.pop(source, None)
        return new, value

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)

    @staticmethod
    def csrf_valid(session, supplied):
        return isinstance(supplied, str) and hmac.compare_digest(session.get("csrf", ""), supplied)
