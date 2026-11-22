"""Partner setup secrets. No dotenv, database or network work on import."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from uuid import UUID


TOKEN_RE = re.compile(r"[1-9][0-9]{4,19}:[A-Za-z0-9_-]{20,128}\Z")


class PartnerConfigurationError(ValueError):
    pass


def validate_bot_token(value: str) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise ValueError("Некорректный токен бота.")
    return value


@dataclass(frozen=True)
class PartnerSettings:
    enabled: bool = False
    base_domain: str = ""
    manager_bot_token: str = field(default="", repr=False)
    encryption_keys: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls):
        # An explicit separate manager credential prevents trusting tenant tokens
        # for platform-level ownership and settings mutations.
        return cls(
            enabled=os.environ.get("PARTNER_PLATFORM_ENABLED", "").lower() in {"1", "true"},
            base_domain=os.environ.get("PARTNER_BASE_DOMAIN", ""),
            manager_bot_token=os.environ.get("PARTNER_MANAGER_BOT_TOKEN", ""),
            encryption_keys=tuple(key.strip() for key in
                                  os.environ.get("PARTNER_TOKEN_ENCRYPTION_KEYS", "").split(",") if key.strip()),
        )

    def check(self):
        from services.partner_catalog import validate_base_domain
        try:
            validate_bot_token(self.manager_bot_token)
            return validate_base_domain(self.base_domain)
        except ValueError:
            raise PartnerConfigurationError("Партнёрский кабинет ещё не настроен оператором.") from None


class TokenVault:
    """Authenticated encryption, bound to project AND bot identity.

    First key encrypts, all configured keys decrypt, allowing explicit rotation.
    Tokens are never returned to an HTTP client, logs or the project serializer.
    """

    def __init__(self, keys):
        try:
            from cryptography.fernet import Fernet, MultiFernet
            if not keys:
                raise ValueError()
            self._fernet = MultiFernet([Fernet(key.encode("ascii")) for key in keys])
        except (ImportError, TypeError, ValueError, UnicodeError):
            raise PartnerConfigurationError("Защищённое хранение токенов ещё не настроено.") from None

    def __repr__(self):
        return "<TokenVault>"

    def encrypt(self, project_id, bot_id, token):
        token = validate_bot_token(token)
        if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id != int(token.split(":", 1)[0]):
            raise ValueError("Бот не соответствует проверенному токену.")
        data = json.dumps({"v": 1, "project_id": str(UUID(str(project_id))),
                           "bot_id": bot_id, "token": token}, separators=(",", ":")).encode()
        return self._fernet.encrypt(data).decode("ascii")

    def decrypt(self, project_id, bot_id, encrypted):
        try:
            data = json.loads(self._fernet.decrypt(encrypted.encode("ascii")))
            if (data["v"] != 1 or data["project_id"] != str(UUID(str(project_id)))
                    or data["bot_id"] != bot_id):
                raise ValueError()
            token = validate_bot_token(data["token"])
            if int(token.split(":", 1)[0]) != bot_id:
                raise ValueError()
            return token
        except Exception:
            # No exception chaining: crypto error input may contain ciphertext.
            raise PartnerConfigurationError("Не удалось прочитать подключение бота.") from None


class BotConnectionError(ValueError):
    pass


class TelegramBotVerifier:
    async def verify(self, token):
        """Read-only getMe. Does NOT replace a webhook or send any messages."""
        validate_bot_token(token)
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=10, connect=5)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(f"https://api.telegram.org/bot{token}/getMe",
                                       allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError()
                    raw = bytearray()
                    # read(n) may return only the first network chunk. Wait for
                    # EOF within the existing timeout, but retain a hard bound.
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > 65_536:
                            raise ValueError()
                    data = json.loads(raw)
            result = data.get("result") or {}
            bot_id, username = result.get("id"), result.get("username")
            if (data.get("ok") is not True or result.get("is_bot") is not True
                    or isinstance(bot_id, bool) or not isinstance(bot_id, int)
                    or bot_id != int(token.split(":", 1)[0])
                    or not isinstance(username, str)
                    or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)):
                raise ValueError()
            return {"id": bot_id, "username": username}
        except Exception:
            # aiohttp exceptions include the URL (and therefore the token).
            raise BotConnectionError("Не удалось проверить токен в Telegram. Проверьте его и повторите.") from None
