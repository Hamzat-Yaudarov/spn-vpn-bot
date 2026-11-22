"""Explicit, paid-only launch of a partner bot. No import-time I/O.

The host must opt in AND mount the matching authenticated webhook handler before
constructing an enabled launcher. A successful Telegram write is not enough: the
durable project becomes active only after read-back verification. Retrying a
possibly accepted setWebhook reapplies the same URL/secret without discarding
pending updates. The launcher cannot issue subscriptions or accept payments.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

from partner_client_web import webhook_secret
from services.partner_catalog import validate_base_domain
from services.partner_security import validate_bot_token


MAX_TELEGRAM_BYTES = 65_536
ALLOWED_UPDATES = ("message", "callback_query")
_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
_SECRET = re.compile(r"[a-f0-9]{64}\Z")


class LaunchError(ValueError):
    """A safe, user-facing launch precondition error."""


class LaunchUnavailable(LaunchError):
    pass


class TelegramLaunchError(LaunchError):
    """No Telegram descriptions, URLs, tokens or chained network errors."""


def _origin(value):
    """An operator-configured HTTPS origin, never a customer-supplied URL."""
    try:
        if (not isinstance(value, str) or not value or len(value) > 253
                or not value.isascii() or any(c.isspace() for c in value)
                or any(c in value for c in ("\\", "?", "#"))):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or parsed.port not in {None, 443}
                or not parsed.hostname or parsed.netloc.endswith(":")):
            raise ValueError()
        host = validate_base_domain(parsed.hostname)
        return f"https://{host}"
    except (ValueError, TypeError, UnicodeError):
        raise LaunchUnavailable("Адрес запуска ботов ещё не настроен оператором.") from None


def _identity(result, expected_id):
    if (not isinstance(result, dict) or result.get("is_bot") is not True
            or type(result.get("id")) is not int or result["id"] != expected_id
            or not isinstance(result.get("username"), str)
            or not _USERNAME.fullmatch(result["username"])):
        raise TelegramLaunchError("Не удалось подтвердить подключение бота в Telegram.")
    return result


def _webhook_info(result):
    if (not isinstance(result, dict) or not isinstance(result.get("url"), str)
            or len(result["url"]) > 2048):
        raise TelegramLaunchError("Не удалось проверить подключение бота в Telegram.")
    allowed = result.get("allowed_updates")
    if allowed is not None and (not isinstance(allowed, list)
                                or any(not isinstance(item, str) for item in allowed)
                                or len(allowed) > 128):
        raise TelegramLaunchError("Не удалось проверить подключение бота в Telegram.")
    return result


class TelegramLaunchAdapter:
    """Fixed-host Telegram adapter. Construction never opens a connection."""

    def __repr__(self):
        return "<TelegramLaunchAdapter>"

    async def _call(self, token, method, payload=None):
        try:
            validate_bot_token(token)
            if method not in {"getMe", "getWebhookInfo", "setWebhook", "setMyName", "setMyDescription", "setMyShortDescription", "setMyCommands"}:
                raise ValueError()
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=20, connect=5)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                url = f"https://api.telegram.org/bot{token}/{method}"
                operation = (session.get(url, allow_redirects=False) if payload is None else
                             session.post(url, json=payload, allow_redirects=False))
                async with operation as response:
                    if response.status != 200:
                        raise ValueError()
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > MAX_TELEGRAM_BYTES:
                            raise ValueError()
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("ok") is not True:
                        raise ValueError()
                    return data["result"]
        except Exception:
            # aiohttp errors contain the bot token in their request URL.
            raise TelegramLaunchError("Telegram пока не подтвердил запуск. Повторно платить не нужно.") from None

    async def get_me(self, token):
        result = await self._call(token, "getMe")
        return _identity(result, int(token.split(":", 1)[0]))

    async def configure_basics(self, token, *, title):
        from services.partner_brand_defaults import telegram_profile
        if not isinstance(title, str) or not 1 <= len(title) <= 80 or any(ord(c) < 32 for c in title):
            raise TelegramLaunchError("Название проекта требует проверки.")
        profile = telegram_profile(title)
        for method, payload in (
            ("setMyName", {"name": profile["name"]}),
            ("setMyDescription", {"description": profile["description"]}),
            ("setMyShortDescription", {"short_description": profile["short_description"]}),
            ("setMyCommands", {"commands": profile["commands"]}),
        ):
            if await self._call(token, method, payload) is not True:
                raise TelegramLaunchError("Базовое оформление пока не подтверждено Telegram.")

    async def get_webhook_info(self, token):
        return _webhook_info(await self._call(token, "getWebhookInfo"))

    async def set_webhook(self, token, *, url, secret_token):
        try:
            parsed = urlsplit(url)
            origin = _origin(f"{parsed.scheme}://{parsed.netloc}")
            project_id = str(UUID(parsed.path.split("/")[2]))
            if (url != f"{origin}/partner-bots/{project_id}/webhook"
                    or not isinstance(secret_token, str) or not _SECRET.fullmatch(secret_token)):
                raise ValueError()
        except (ValueError, TypeError, IndexError):
            raise TelegramLaunchError("Параметры запуска бота требуют проверки.") from None
        result = await self._call(token, "setWebhook", {
            "url": url, "secret_token": secret_token,
            "allowed_updates": list(ALLOWED_UPDATES), "drop_pending_updates": False,
        })
        if result is not True:
            raise TelegramLaunchError("Telegram пока не подтвердил запуск. Повторно платить не нужно.")


class PartnerLaunchService:
    def __init__(self, store, vault, telegram=None, *, webhook_base_url,
                 webhook_secret_key, clients_enabled=False):
        self.store, self.vault = store, vault
        self.telegram = telegram if telegram is not None else TelegramLaunchAdapter()
        self._base_url = webhook_base_url
        self._secret_key = webhook_secret_key
        self._clients_enabled = clients_enabled is True

    def __repr__(self):
        return "<PartnerLaunchService>"

    @property
    def ready(self):
        """Pure capability check; explicit opt-in is never inferred from env."""
        try:
            if not self._clients_enabled:
                return False
            _origin(self._base_url)
            if not isinstance(self._secret_key, bytes) or not 32 <= len(self._secret_key) <= 512:
                return False
            if self.vault is None or not callable(getattr(self.vault, "decrypt", None)):
                return False
            if getattr(self.telegram, "ready", True) is not True:
                return False
            return all(callable(getattr(self.telegram, method, None))
                       for method in ("get_me", "get_webhook_info", "set_webhook"))
        except Exception:
            return False

    @staticmethod
    def _paid(order):
        if order["status"] != "paid_pending_launch":
            raise LaunchError("Для запуска нужна подтверждённая оплата вступления.")

    async def launch(self, project_id, owner_id, order_id, *, replace_existing=False):
        if type(replace_existing) is not bool:
            raise LaunchError("Замена подключения требует явного подтверждения владельца.")
        try:
            project_id, order_id = str(UUID(str(project_id))), str(UUID(str(order_id)))
            if type(owner_id) is not int or not 0 < owner_id < 2**63:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise LaunchError("Некорректный запрос запуска.") from None
        # Ownership is checked even when launching is disabled or already done.
        order = await self.store.get_entry_order(project_id, owner_id, order_id)
        if order["status"] == "launched":
            return {"launch_state": "launched"}
        self._paid(order)
        if "bot_id" in order and order["bot_id"] is None:
            return {"launch_state": "awaiting_bot"}
        if not self.ready:
            return {"launch_state": "disabled",
                    "message": "Оплата сохранена. Запуск пока недоступен, повторно платить не нужно."}
        # One leased connection is used for all reads/writes under this lock;
        # no second pool acquisition that could deadlock a pool of size one.
        async with self.store.acquire_launch_lock(project_id) as lease:
            order = await lease.get_entry_order(project_id, owner_id, order_id)
            if order["status"] == "launched":
                return {"launch_state": "launched"}
            self._paid(order)
            job = await lease.get_launch(project_id, order_id)
            # The store enforces the paid/frozen-bot/current-status guards. Also
            # fail closed if an integration accidentally returns another job.
            if (str(job.get("project_id")) != project_id or str(job.get("order_id")) != order_id
                    or job.get("owner_tg_id") != owner_id or type(job.get("bot_id")) is not int
                    or not 0 < job["bot_id"] < 2**63
                    or not isinstance(job.get("bot_username"), str)
                    or not _USERNAME.fullmatch(job["bot_username"])
                    or not isinstance(job.get("encrypted_token"), str)):
                raise LaunchError("Сохранённое подключение бота требует проверки.")
            target = f"{_origin(self._base_url)}/partner-bots/{project_id}/webhook"
            secret = webhook_secret(self._secret_key, project_id, job["bot_id"])
            try:
                token = self.vault.decrypt(project_id, job["bot_id"], job["encrypted_token"])
                validate_bot_token(token)
                if int(token.split(":", 1)[0]) != job["bot_id"]:
                    raise TelegramLaunchError("Не удалось подтвердить подключение бота в Telegram.")
                verified_identity = _identity(await self.telegram.get_me(token), job["bot_id"])
                # Username can legitimately change in BotFather. Bot identity,
                # not the old display username, is the paid entitlement guard.
                info = _webhook_info(await self.telegram.get_webhook_info(token))
            except Exception:
                await lease.mark_launch_retry(project_id, order_id)
                return {"launch_state": "retry",
                        "message": "Оплата сохранена. Повторите запуск позже, повторно платить не нужно."}
            if info["url"] and info["url"] != target and not replace_existing:
                await lease.mark_launch_retry(project_id, order_id, needs_webhook_replace=True)
                return {"launch_state": "needs_webhook_replace",
                        "message": "Бот подключён к другому сервису. Для замены нужно ваше подтверждение."}
            try:
                # Even the same URL cannot prove the current secret is ours.
                await self.telegram.set_webhook(token, url=target, secret_token=secret)
                verified = _webhook_info(await self.telegram.get_webhook_info(token))
                if (verified["url"] != target or verified.get("allowed_updates") is None
                        or sorted(verified["allowed_updates"]) != sorted(ALLOWED_UPDATES)):
                    raise TelegramLaunchError("Подключение пока не подтверждено Telegram.")
                # Initial setup only: launched projects return above, so retries
                # never overwrite an already active partner's customization.
                configure = getattr(self.telegram, "configure_basics", None)
                if callable(configure):
                    await configure(token, title=job.get("project_title") or "Мой VPN")
            except Exception:
                # Preserve the paid job after every ambiguous network outcome.
                # DB failures in this write are deliberately not acknowledged.
                await lease.mark_launch_retry(project_id, order_id)
                return {"launch_state": "retry",
                        "message": "Оплата сохранена. Повторите запуск позже, повторно платить не нужно."}
            # Keep DB errors outside the Telegram-error handler: HTTP must fail
            # rather than acknowledge an unknown transaction outcome as done.
            await lease.complete_launch(project_id, order_id, bot_id=job["bot_id"],
                                        bot_username=verified_identity["username"],
                                        webhook_url=target,
                                        secret_fingerprint=hashlib.sha256(secret.encode("ascii")).hexdigest())
            return {"launch_state": "launched"}
