"""Central manager onboarding and explicit operator-only webhook bootstrap.

No settings discovery, installation, network, identity grants or messages on
import/construction. A web_app button is signed by the CENTRAL manager bot in
Telegram; a tenant token can never produce trusted platform initData.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from uuid import UUID

from services.partner_bot import _identity
from services.partner_manager_access import normalize_manager_allowed_user_ids
from services.partner_launcher import (
    TelegramLaunchAdapter,
    TelegramLaunchError,
    _identity as _bot_identity,
    _origin,
    _webhook_info,
)
from services.partner_security import validate_bot_token


def manager_webhook_secret(key, bot_id):
    if not isinstance(key, bytes) or not 32 <= len(key) <= 512:
        raise ValueError("Нужен отдельный ключ вебхуков платформы.")
    if type(bot_id) is not int or not 0 < bot_id < 2**63:
        raise ValueError("Некорректный управляющий бот.")
    return hmac.new(
        key, f"wl-manager-webhook:v1:{bot_id}".encode(), hashlib.sha256
    ).hexdigest()


class PartnerManagerHandler:
    def __init__(
        self, bot_id, manager_origin, *, username="", manager_allowed_user_ids=None,
        managed_service=None, operator_ids=(), entry_pricing=None,
    ):
        if type(bot_id) is not int or not 0 < bot_id < 2**63:
            raise ValueError("Некорректный управляющий бот.")
        self.bot_id = bot_id
        self.manager_url = _origin(manager_origin) + "/partners"
        if username and (
            not isinstance(username, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
        ):
            raise ValueError("Проверьте имя управляющего бота.")
        self.username = username
        self.manager_allowed_user_ids = normalize_manager_allowed_user_ids(
            manager_allowed_user_ids
        )
        self.managed_service = managed_service
        self.operator_ids = normalize_manager_allowed_user_ids(operator_ids) or ()
        from services.partner_entry_pricing import EntryPricingPolicy
        self.entry_pricing = entry_pricing if entry_pricing is not None else EntryPricingPolicy()
        if not isinstance(self.entry_pricing, EntryPricingPolicy):
            raise ValueError("Нужна проверенная политика цены вступления.")

    async def handle(self, update):
        if (isinstance(update, dict) and ("managed_bot" in update
                or isinstance(update.get("message"), dict) and "managed_bot_created" in update["message"])):
            from services.partner_managed_bots import event_identity
            event = event_identity(update)
            if event is None or self.manager_allowed_user_ids is not None and event[0] not in self.manager_allowed_user_ids:
                return None
            if self.managed_service is not None and self.managed_service.available is True:
                await self.managed_service.handle_update(update)
            return None
        identity = _identity(update, {"bot": {"id": self.bot_id}})
        if identity is None:
            return None
        user_id, message, callback_id = identity
        if (
            self.manager_allowed_user_ids is not None
            and user_id not in self.manager_allowed_user_ids
        ):
            return None
        if callback_id is not None:
            return None
        text = message.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            return None
        parts = text.split(maxsplit=1)
        if not parts:
            return None
        command = parts[0]
        if "@" in command:
            command, addressed = command.split("@", 1)
            if not self.username or addressed.lower() != self.username.lower():
                return None
        if command == "/help":
            return {"method": "sendMessage", "chat_id": user_id,
                    "text": "Помощь с подключением и настройкой сервиса: https://t.me/vpnislam",
                    "reply_markup": {"inline_keyboard": [[{"text": "Поддержка", "url": "https://t.me/vpnislam"}]]}}
        if command != "/start":
            return None
        message_id = message.get("message_id")
        if type(message_id) is not int or not 0 < message_id < 2**63:
            return None
        if user_id in self.operator_ids and len(parts) == 2 and parts[1].startswith("support_"):
            try:
                project_id = str(UUID(parts[1][8:]))
            except (ValueError, TypeError, AttributeError):
                return None
            return {"method": "sendMessage", "chat_id": user_id,
                    "text": "🛠 Режим поддержки платформы. Вы редактируете настройки партнёра от своего имени; изменения записываются в журнал.",
                    "reply_markup": {"inline_keyboard": [[{
                        "text": "Открыть проект партнёра", "web_app": {
                            "url": self.manager_url + "/support?project=" + project_id,
                        },
                    }]]}}
        from services.partner_bot import _rubles
        entry_price = _rubles(self.entry_pricing.price_for(user_id))
        keyboard = [
            [{"text": "Создать свой сервис", "web_app": {"url": self.manager_url}}],
            [{"text": "Поддержка", "url": "https://t.me/vpnislam"}],
        ]
        if user_id in self.operator_ids:
            keyboard.append([{"text": "🛠 Проекты партнёров", "web_app": {"url": self.manager_url + "/support"}}])
        return {
            "method": "sendMessage",
            "chat_id": user_id,
            "text": (
                "Создайте свой сервис под своим брендом.\n\n"
                f"Вступление — {entry_price} один раз.\n"
                "Вам — 70% оплаченных продаж, платформе и на расходы — 30%.\n\n"
                "Оплатите подключение, затем создайте бота одной кнопкой. "
                "Дальнейшее управление — в вашем боте."
            ),
            "reply_markup": {
                "inline_keyboard": keyboard
            },
        }


class TelegramManagerAdapter(TelegramLaunchAdapter):
    async def set_manager_webhook(self, token, *, origin, secret_token, managed_bots_enabled=False):
        if type(managed_bots_enabled) is not bool:
            raise ValueError("Нужен явный флаг создания ботов.")
        target = _origin(origin) + "/partner-manager/webhook"
        if not isinstance(secret_token, str) or not re.fullmatch(
            r"[a-f0-9]{64}", secret_token
        ):
            raise TelegramLaunchError("Параметры управляющего бота требуют проверки.")
        result = await self._call(
            token,
            "setWebhook",
            {
                "url": target,
                "secret_token": secret_token,
                "allowed_updates": ["message", "managed_bot"] if managed_bots_enabled else ["message"],
                "drop_pending_updates": False,
            },
        )
        if result is not True:
            raise TelegramLaunchError(
                "Подключение управляющего бота пока не подтверждено."
            )


class PartnerManagerBootstrap:
    def __init__(
        self,
        *,
        token,
        webhook_origin,
        webhook_key,
        telegram=None,
        enabled=False,
        username="",
        managed_bots_enabled=False,
    ):
        if type(enabled) is not bool or type(managed_bots_enabled) is not bool:
            raise ValueError("Нужен явный флаг подключения управляющего бота.")
        self._token, self._origin, self._key = token, webhook_origin, webhook_key
        self.telegram = telegram if telegram is not None else TelegramManagerAdapter()
        self.enabled, self.username = enabled, username
        self.managed_bots_enabled = managed_bots_enabled

    def __repr__(self):
        return "<PartnerManagerBootstrap>"

    @property
    def ready(self):
        if not self.enabled:
            return False
        try:
            validate_bot_token(self._token)
            _origin(self._origin)
            manager_webhook_secret(self._key, int(self._token.split(":", 1)[0]))
            if self.username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", self.username):
                return False
            return all(
                callable(getattr(self.telegram, method, None))
                for method in ("get_me", "get_webhook_info", "set_manager_webhook")
            )
        except Exception:
            return False

    async def install(self, *, replace_existing=False):
        if type(replace_existing) is not bool:
            raise ValueError(
                "Замена чужого webhook требует явного подтверждения оператора."
            )
        if not self.ready:
            return {"state": "disabled"}
        try:
            bot_id = int(self._token.split(":", 1)[0])
            identity = _bot_identity(await self.telegram.get_me(self._token), bot_id)
            if self.managed_bots_enabled and identity.get("can_manage_bots") is not True:
                return {"state": "management_mode_required"}
            if self.username and identity["username"].lower() != self.username.lower():
                return {"state": "username_requires_review"}
            existing = _webhook_info(await self.telegram.get_webhook_info(self._token))
            target = _origin(self._origin) + "/partner-manager/webhook"
            if existing["url"] and existing["url"] != target and not replace_existing:
                return {"state": "needs_webhook_replace"}
            # Same target alone cannot prove that Telegram has our current key.
            secret = manager_webhook_secret(self._key, bot_id)
            await self.telegram.set_manager_webhook(
                self._token, origin=self._origin, secret_token=secret,
                **({"managed_bots_enabled": True} if self.managed_bots_enabled else {}),
            )
            verified = _webhook_info(await self.telegram.get_webhook_info(self._token))
            allowed = ["message", "managed_bot"] if self.managed_bots_enabled else ["message"]
            if verified["url"] != target or sorted(verified.get("allowed_updates") or []) != sorted(allowed):
                return {"state": "retry"}
            return {"state": "installed"}
        except Exception:
            # Includes an accepted setWebhook with a lost response: retrying the
            # SAME target/key never clears pending updates or pays/grants again.
            return {"state": "retry"}
