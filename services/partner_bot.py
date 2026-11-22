"""Tenant-scoped partner-bot presentation, with no Telegram I/O.

``handle`` consumes a verified webhook update and returns Bot API command
descriptions. The caller is responsible for selecting the project using its
trusted webhook route, verifying its secret, and sending commands with that
project's bot. Importing or testing this module does not read settings, start a
bot, contact Telegram, charge money, or issue VPN access.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from html import escape
import re
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from services.connection_instructions import (
    ANDROID_PLATFORM, IPHONE_PLATFORM, connection_app_url, connection_app_button_text,
)
from services.partner_brand_defaults import DEFAULT_CUSTOMER_SUPPORT_URL, brand_defaults
from services.partner_catalog import (
    BASE_DEVICE_LIMIT,
    BASE_TRAFFIC_GB,
    BILLING_DAYS,
    MAX_PRICE_KOPECKS,
    MIN_PRICE_KOPECKS,
    validate_base_domain,
)


_MENU = (
    ("🛒 Купить подписку", "buy"),
    ("🔑 Моя подписка", "subscriptions"),
    ("🎁 Бонусы", "bonuses"),
    ("📲 Как подключиться", "connect"),
    ("🆘 Помощь", "help"),
    ("📢 Новости", "news"),
)
_PENDING = frozenset(
    {"paid", "paid_pending_activation", "activation_pending", "activating"}
)
_UNPAID = frozenset({"created", "pending", "awaiting_payment"})
_NOT_LOADED = object()


def _positive_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63


def _uuid(value: Any) -> str:
    if not isinstance(value, (str, UUID)):
        raise ValueError("Invalid identifier")
    return str(UUID(str(value)))


def _short_id(value: Any) -> str:
    return (
        base64.urlsafe_b64encode(UUID(_uuid(value)).bytes).decode("ascii").rstrip("=")
    )


def _long_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{22}", value, re.ASCII
    ):
        raise ValueError("Invalid identifier")
    raw = base64.b64decode(value + "==", altchars=b"-_", validate=True)
    result = str(UUID(bytes=raw))
    if _short_id(result) != value:
        raise ValueError("Noncanonical identifier")
    return result


def _price(project: dict) -> int:
    price = project.get("price_kopecks")
    if (
        isinstance(price, bool)
        or not isinstance(price, int)
        or not MIN_PRICE_KOPECKS <= price <= MAX_PRICE_KOPECKS
    ):
        raise ValueError("Invalid project price")
    return price


def _rubles(value: int) -> str:
    rubles, kopecks = divmod(value, 100)
    return (
        f"{rubles:,}".replace(",", " ") + (f",{kopecks:02d}" if kopecks else "") + " ₽"
    )


def _safe_url(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or "\\" in value
        or any(ord(c) < 33 for c in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return None
    return value


def _workspace_origin(value: Any) -> str:
    """Only an operator-supplied HTTPS origin may host the tenant cabinet."""
    try:
        safe = _safe_url(value)
        if not safe or not safe.isascii():
            raise ValueError()
        parsed = urlsplit(safe)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.netloc.endswith(":"):
            raise ValueError()
        return "https://" + validate_base_domain(parsed.hostname)
    except Exception:
        raise ValueError("Адрес личного кабинета проекта не настроен.") from None


def _plain(value: Any, fallback: str, maximum: int = 2000) -> str:
    return escape(value[:maximum] if isinstance(value, str) and value else fallback)


def _button(text: str, callback: str | None = None, url: str | None = None) -> dict:
    result = {"text": text}
    if callback is not None:
        if not 1 <= len(callback.encode("utf-8")) <= 64:
            raise ValueError("Callback is too long")
        result["callback_data"] = callback
    elif url is not None and _safe_url(url):
        result["url"] = url
    else:
        raise ValueError("Button requires safe destination")
    return result


def _screen(text: str, rows: list[list[dict]] | None = None) -> dict:
    return {
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": rows or [[_button("В меню", "menu")]]},
    }


def _customer_support(project: dict) -> str:
    return _safe_url(project.get("support_url")) or DEFAULT_CUSTOMER_SUPPORT_URL


def render_menu(project: dict, is_owner: bool = False) -> dict:
    """Render customer navigation; Admin is never sent to ordinary clients."""
    brand = project.get("brand") if isinstance(project.get("brand"), dict) else {}
    title = _plain(project.get("title"), "VPN", 80)
    welcome = _plain(
        brand.get("welcome_text"), brand_defaults("VPN")["welcome_text"], 1500
    )
    rows = [
        [_button(text, url=_customer_support(project)) if callback == "help" else _button(text, callback)]
        for text, callback in _MENU
    ]
    rows[-2:] = [rows[-2] + rows[-1]]  # Help/news stay together like the main bot.
    if is_owner:
        rows.append([_button("⚙️ Админ", "admin")])
    return _screen(
        f"👋 <b>{title}</b>\n\n{welcome}\n\n"
        "🌍 Обычные серверы и антиглушилка\n"
        f"📦 {BASE_TRAFFIC_GB} ГБ · {BASE_DEVICE_LIMIT} устройства в базовой подписке\n\n"
        "👇 Выберите нужный раздел.", rows,
    )


def render_connection(project: dict, platform: str | None = None) -> dict:
    """Pure instructions: existing official app links, no token/raw VPN link."""
    if platform is None:
        return _screen(
            "📲 <b>Подключим ваш VPN</b>\n\n"
            "Выберите устройство — покажем, какое приложение установить и как добавить подписку.\n\n"
            "💡 Если подписки ещё нет, сначала нажмите «Купить подписку» в меню.",
            [[_button("🤖 Android", "connect:android")],
             [_button("🍎 iPhone", "connect:iphone")],
             [_button("🔑 Моя подписка", "subscriptions")],
             [_button("🏠 В меню", "menu")]],
        )
    if platform == ANDROID_PLATFORM:
        title, app = "Android", "Happ Plus"
        adding = "Нажмите <b>+</b> в правом верхнем углу → <b>Вставить из буфера</b>. Подтвердите добавление."
        permission = "Разрешите VPN-подключение, если приложение попросит."
    elif platform == IPHONE_PLATFORM:
        title, app = "iPhone", "INCY"
        adding = "Нажмите <b>+</b> и добавьте подписку из буфера."
        permission = "Разрешите создание VPN-конфигурации, когда iPhone попросит подтверждение."
    else:
        raise ValueError("Unsupported connection platform")
    return _screen(
        f"📲 <b>VPN на {title}</b>\n\n"
        f"<b>1. Установите {app}</b>\nНажмите кнопку скачивания ниже.\n\n"
        "<b>2. Скопируйте ссылку подписки</b>\n"
        "В этом боте откройте <b>«Моя подписка» → «Открыть подписку»</b>. "
        "На странице подписки нажмите кнопку копирования ссылки.\n\n"
        f"<b>3. Добавьте подписку в {app}</b>\n{adding}\n\n"
        f"<b>4. Включите VPN</b>\n{permission} Выберите сервер и нажмите кнопку подключения.\n\n"
        "💡 Не подключается? Попробуйте другой сервер или напишите в поддержку — поможем разобраться.",
        [[_button(connection_app_button_text(platform), url=connection_app_url(platform))],
         [_button("🔑 Моя подписка", "subscriptions")],
         [_button("📱 Другое устройство", "connect")],
         [_button("🆘 Написать в поддержку", url=_customer_support(project))],
         [_button("🏠 В меню", "menu")]],
    )


def render_purchase(
    project: dict, subscription_id: str | None = None, *, request_key: str | None = None
) -> dict:
    """Create a price-confirmation screen. Its button retains one request key.

    UUIDs use a canonical compact encoding only to fit Telegram's 64-byte
    callback limit. They are not authorization: every store call is scoped by
    the trusted project and Telegram user.
    """
    price = _price(project)
    request_key = _uuid(request_key) if request_key is not None else str(uuid4())
    callback = f"pay:{_short_id(request_key)}:{price}"
    if subscription_id is not None:
        callback += f":{_short_id(subscription_id)}"
    action = "Продление подписки" if subscription_id else "Новая подписка"
    text = (
        f"🛒 <b>{action} · {BILLING_DAYS} дней</b>\n\n"
        "🌍 Обычные серверы и серверы с антиглушилкой.\n"
        f"📦 {BASE_TRAFFIC_GB} ГБ · {BASE_DEVICE_LIMIT} устройства\n\n"
        f"💳 К оплате: <b>{_rubles(price)}</b>\n"
        "Нужно больше? ГБ и устройства можно докупить отдельно."
    )
    return _screen(
        text,
        [
            [_button("Перейти к оплате", callback)],
            [_button("Назад", "subscriptions" if subscription_id else "menu")],
        ],
    )


def _date(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _identity(update: Any, project: dict) -> tuple[int, dict, str | None] | None:
    if not isinstance(update, dict):
        return None
    forbidden = {
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
        "inline_query",
        "chosen_inline_result",
    }
    if forbidden.intersection(update) or ("message" in update) == (
        "callback_query" in update
    ):
        return None
    callback = update.get("callback_query")
    if callback is not None:
        if (
            not isinstance(callback, dict)
            or callback.get("inline_message_id") is not None
        ):
            return None
        message, sender = callback.get("message"), callback.get("from")
        callback_id = callback.get("id")
        if not isinstance(callback_id, str) or not 1 <= len(callback_id) <= 256:
            return None
    else:
        message = update.get("message")
        sender = message.get("from") if isinstance(message, dict) else None
        callback_id = None
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return None
    chat = message.get("chat")
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "private"
        or not _positive_id(sender.get("id"))
        or not _positive_id(chat.get("id"))
        or chat.get("id") != sender["id"]
        or sender.get("is_bot") is not False
        or any(
            key in message
            for key in (
                "sender_chat",
                "forward_origin",
                "forward_from",
                "forward_from_chat",
                "forward_sender_name",
                "forward_date",
                "business_connection_id",
                "via_bot",
            )
        )
    ):
        return None
    if callback is not None:
        bot_sender = message.get("from")
        bot = project.get("bot") if isinstance(project.get("bot"), dict) else {}
        if bot.get("id") is not None and (
            not isinstance(bot_sender, dict)
            or bot_sender.get("id") != bot["id"]
            or bot_sender.get("is_bot") is not True
        ):
            return None
        if not _positive_id(message.get("message_id")):
            return None
    return sender["id"], message, callback_id


class PartnerBotHandler:
    """Small customer workflow with injected persistence and checkout ports."""

    def __init__(
        self,
        store,
        checkout=None,
        manager_url: str | None = None,
        *,
        subscription_pages=None,
        management=None,
        workspace_origin: str | None = None,
        operator_ids=(),
        manager_username: str = "",
    ):
        self.store = store
        self.checkout = checkout
        self.manager_url = _safe_url(manager_url)
        self.subscription_pages = subscription_pages
        self.management = management
        self.workspace_origin = (
            _workspace_origin(workspace_origin) if workspace_origin is not None else None
        )
        if not isinstance(operator_ids, (tuple, list, set, frozenset)) or any(
            not _positive_id(value) for value in operator_ids
        ):
            raise ValueError("Некорректный список администраторов платформы.")
        if manager_username and (
            not isinstance(manager_username, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", manager_username, re.ASCII)
        ):
            raise ValueError("Некорректное имя управляющего бота.")
        self.operator_ids = frozenset(operator_ids)
        self.manager_username = manager_username

    @staticmethod
    def _commands(
        user_id: int,
        message: dict,
        callback_id: str | None,
        screen: dict,
        *,
        answer: str = "",
    ) -> list[dict]:
        payload = {"chat_id": user_id, **screen}
        if callback_id:
            payload["message_id"] = message["message_id"]
            return [
                {
                    "method": "answerCallbackQuery",
                    "payload": {"callback_query_id": callback_id, "text": answer},
                },
                {"method": "editMessageText", "payload": payload},
            ]
        return [{"method": "sendMessage", "payload": payload}]

    async def _subscription_state(self, project: dict, user_id: int):
        """Read exact-tenant state once; also supports single-connection ports."""
        pending_loader = getattr(self.store, "get_pending_order", None)
        subscriptions = await self.store.list_subscriptions(project["id"], user_id)
        pending = (
            await pending_loader(project["id"], user_id)
            if subscriptions and pending_loader is not None else None
        )
        return subscriptions, pending

    async def _subscriptions(self, project: dict, user_id: int, *, subscriptions=None,
                             pending=_NOT_LOADED) -> dict:
        # Reuse reads already made by this exact handler invocation (e.g. Buy
        # deciding whether to show renewal). Never retain them across updates.
        if subscriptions is None:
            if pending is _NOT_LOADED:
                subscriptions, pending = await self._subscription_state(project, user_id)
            else:
                subscriptions = await self.store.list_subscriptions(project["id"], user_id)
        if not subscriptions:
            return _screen(
                "🔑 <b>Моя подписка</b>\n\nУ вас пока нет подписки.\n"
                "Купите её, затем вернитесь сюда — здесь появятся срок, лимиты и ссылка для подключения.",
                [
                    [_button("Купить подписку", "buy")],
                    [_button("В меню", "menu")],
                ],
            )
        # The store permits one subscription slot. Never fall back to an
        # unrelated object or expose a row with a conflicting tenant/user.
        subscription = subscriptions[0]
        if not isinstance(subscription, dict):
            raise ValueError("Invalid subscription")
        if (
            subscription.get("project_id", project["id"]) != project["id"]
            or subscription.get("tg_id", user_id) != user_id
        ):
            raise ValueError("Foreign subscription")
        subscription_id = _uuid(subscription["id"])
        expires = _date(subscription.get("expires_at"))
        if pending is _NOT_LOADED:
            pending_loader = getattr(self.store, "get_pending_order", None)
            pending = await pending_loader(project["id"], user_id) if pending_loader else None
        rows = []
        if expires is None:
            if pending:
                return self._order_screen(
                    pending,
                    unavailable=self.checkout is None
                    and pending.get("status") in _UNPAID,
                )
            if _date(subscription.get("desired_expires_at")):
                text = "⏳ <b>Моя подписка</b>\n\nОплата сохранена, активация повторяется."
            else:
                text = "🔑 <b>Моя подписка</b>\n\nПодписка ещё не оплачена."
                rows.append([_button("Купить подписку", "buy")])
        else:
            active = expires > datetime.now(timezone.utc)
            limits_pending = bool(
                pending
                and pending.get("status") in _PENDING
                and not pending.get("activation_issue")
            )
            status = "Подписка активна" if active else "Срок подписки истёк"
            text = f"{'✅' if active else '⌛'} <b>{status}</b>\n📅 До: {expires:%d.%m.%Y}\n"
            # Base limits are explanatory, not live remaining traffic or an
            # online-device count. Actual add-on values stay server-owned.
            traffic = subscription.get("traffic_limit_gb")
            cycle_baseline = subscription.get("traffic_cycle_baseline_bytes")
            byte_limit = subscription.get("traffic_limit_bytes")
            cycle_tracked = type(cycle_baseline) is int and cycle_baseline >= 0
            if (
                cycle_tracked
                and type(byte_limit) is int
                and byte_limit - cycle_baseline >= BASE_TRAFFIC_GB * 1024**3
            ):
                # Remnawave's absolute NO_RESET cap includes historical usage.
                # The customer must see the current period's quota, not that cap.
                traffic = (byte_limit - cycle_baseline) // 1024**3
            if traffic is None:
                if isinstance(byte_limit, int) and not isinstance(byte_limit, bool):
                    traffic = byte_limit // 1024**3
            devices = subscription.get("device_limit", BASE_DEVICE_LIMIT)
            traffic = (
                traffic
                if isinstance(traffic, int)
                and not isinstance(traffic, bool)
                and traffic >= BASE_TRAFFIC_GB
                else BASE_TRAFFIC_GB
            )
            devices = (
                devices
                if isinstance(devices, int)
                and not isinstance(devices, bool)
                and BASE_DEVICE_LIMIT <= devices <= 10
                else BASE_DEVICE_LIMIT
            )
            text += f"{'Новые лимиты' if limits_pending else 'Квота периода' if cycle_tracked else 'Лимиты'}: {traffic} ГБ · Устройства: {devices}"
            if limits_pending and pending.get("purpose") in {"traffic", "devices"}:
                text += "\nДокупка оплачена, новые лимиты ещё применяются. Повторно платить не нужно."
            if subscription.get("traffic_cycle_hold") in {
                "legacy_review",
                "counter_changed",
                "late_observation",
            }:
                text += "\nОбновление трафика ожидает сверки. Купленные ГБ не списываются заново. Обратитесь в поддержку."
                rows.append([_button("Помощь", "help")])
            desired = _date(subscription.get("desired_expires_at"))
            pending_activation = desired is not None and desired > expires
            if pending_activation:
                text += "\nПродление оплачено, активация повторяется. Повторно платить не нужно."
            if active:
                if self.subscription_pages is not None:
                    # No fallback to the private platform URL if branded issuance fails.
                    try:
                        url = _safe_url(
                            await self.subscription_pages.link_for_customer(
                                project["id"], user_id, subscription_id
                            )
                        )
                    except Exception:
                        url = None
                else:
                    url = _safe_url(subscription.get("subscription_url"))
                if url:
                    rows.append([_button("Открыть подписку", url=url)])
                    rows.append([_button("📲 Как подключиться", "connect")])
                else:
                    text += "\nСсылка подключения пока недоступна."
            if not pending_activation:
                rows.extend(
                    [
                        [_button("Продлить", "renew:" + _short_id(subscription_id))],
                        [
                            _button("Докупить ГБ", "traffic"),
                            _button("Докупить устройства", "devices"),
                        ],
                    ]
                )
            if pending:
                rows.append(
                    [
                        _button(
                            "Незавершённая покупка", "order:" + _short_id(pending["id"])
                        )
                    ]
                )
        rows.append([_button("В меню", "menu")])
        return _screen(text, rows)

    def _order_screen(
        self, order: dict, *, payment_url: str | None = None, unavailable: bool = False
    ) -> dict:
        order_id = _uuid(order["id"])
        status = order.get("status")
        addon = order.get("purpose") in {"traffic", "devices"}
        if status in _PENDING:
            if order.get("activation_issue") in {
                "addon_expired",
                "device_limit",
                "activation_expired",
            }:
                return _screen(
                    "🧾 <b>Оплата сохранена.</b>\nПокупка требует проверки поддержкой. Повторно платить не нужно.",
                    [
                        [_button("Помощь", "help")],
                        [_button("Моя подписка", "subscriptions")],
                    ],
                )
            return _screen(
                "⏳ <b>Оплата сохранена, активация повторяется.</b>\nПовторно платить не нужно.",
                [
                    [_button("Проверить оплату", "check:" + _short_id(order_id))],
                    [_button("Моя подписка", "subscriptions")],
                ],
            )
        if status == "active":
            return _screen(
                (
                    "✅ <b>Докупка применена.</b>\nПараметры — в разделе «Моя подписка»."
                    if addon
                    else "✅ <b>Подписка активирована.</b>\nТеперь можно подключаться! Ссылка — в разделе «Моя подписка»; ниже есть пошаговая инструкция."
                ),
                [
                    [_button("Моя подписка", "subscriptions")],
                    [_button("📲 Как подключиться", "connect")],
                    [_button("В меню", "menu")],
                ],
            )
        if status == "canceled":
            return _screen(
                "↩️ <b>Платёж отменён.</b>\nПодписка по этому заказу не выдана.",
                [
                    [_button("Купить подписку", "buy")],
                    [_button("В меню", "menu")],
                ],
            )
        if status in _UNPAID:
            url = _safe_url(payment_url) or _safe_url(order.get("confirmation_url"))
            price = order.get("price_kopecks")
            text = "💳 <b>Оплата докупки</b>" if addon else "💳 <b>Оплата подписки</b>"
            if (
                isinstance(price, int)
                and not isinstance(price, bool)
                and (1 if addon else MIN_PRICE_KOPECKS) <= price <= MAX_PRICE_KOPECKS
            ):
                text += f"\nК оплате: {_rubles(price)}"
            if unavailable:
                text += "\n\nОплата ещё не подключена, деньги не списаны."
            elif url:
                text += "\n\nОткройте счёт, затем проверьте оплату."
            else:
                text += "\n\nНе удалось получить ссылку оплаты. Проверка не создаёт новую покупку."
            rows = [[_button("Оплатить", url=url)]] if url and not unavailable else []
            if not url and not unavailable:
                rows.append([_button("Открыть счёт", "resume:" + _short_id(order_id))])
            if self._can_cancel_draft(order):
                rows.append(
                    [
                        _button(
                            "Отменить черновик", "cancel_draft:" + _short_id(order_id)
                        )
                    ]
                )
            rows.extend(
                [
                    [_button("Проверить оплату", "check:" + _short_id(order_id))],
                    [_button("В меню", "menu")],
                ]
            )
            return _screen(text, rows)
        return _screen(
            "<b>Заказ не оплачен.</b>\nСчёт закрыт или недоступен. Проверьте статус перед новой покупкой.",
            [
                [_button("Проверить оплату", "check:" + _short_id(order_id))],
                [_button("В меню", "menu")],
            ],
        )

    def _can_cancel_draft(self, order):
        return (
            getattr(self.store, "invoice_intents_enabled", False)
            and order.get("status") == "created"
            and not order.get("provider_payment_id")
            and not order.get("invoice_requested_at")
        )

    async def _cancel_draft(self, project, user_id, data, *, confirm=False):
        order_id = _long_id(data.split(":", 1)[1])
        order = await self.store.get_order(project["id"], user_id, order_id)
        if not self._can_cancel_draft(order):
            return self._order_screen(order)
        if confirm:
            order = await self.store.cancel_uninvoiced_order(
                project["id"], user_id, order_id
            )
            return self._order_screen(order)
        return _screen(
            "<b>Отменить черновик?</b>\nСчёт ещё не создавался, деньги не списаны. Подписка и ГБ не изменятся.",
            [
                [
                    _button(
                        "Да, отменить черновик", "confirm_cancel:" + _short_id(order_id)
                    )
                ],
                [_button("Назад к заказу", "order:" + _short_id(order_id))],
            ],
        )

    async def _purchase(self, project: dict, user_id: int, data: str) -> dict:
        parts = data.split(":")
        if len(parts) not in (3, 4) or not re.fullmatch(
            r"[1-9][0-9]{4,7}", parts[2], re.ASCII
        ):
            raise ValueError("Invalid purchase callback")
        request_key, expected_price = _long_id(parts[1]), int(parts[2])
        if not MIN_PRICE_KOPECKS <= expected_price <= MAX_PRICE_KOPECKS:
            raise ValueError("Invalid confirmed price")
        subscription_id = _long_id(parts[3]) if len(parts) == 4 else None
        # The store compares a new order with the CURRENT server price, but an
        # existing request must retain its saved price even if the catalogue
        # changed. Do not reject a retry here based on the screen's snapshot.
        order = await self.store.create_order(
            project["id"],
            user_id,
            request_key,
            expected_price,
            subscription_id=subscription_id,
        )
        if order.get("status") not in _UNPAID:
            return self._order_screen(order)
        return await self._checkout_order(project, user_id, order)

    async def _checkout_order(self, project: dict, user_id: int, order: dict) -> dict:
        if self.checkout is None:
            return self._order_screen(order, unavailable=True)
        try:
            checkout = await self.checkout.create_checkout(
                project["id"], user_id, order["id"]
            )
        except Exception as exc:
            # Never render exception text: provider exceptions can contain
            # credentials, invoice URLs, or private customer details.
            # The failed response may follow a saved invoice intent. Refresh
            # before offering draft cancellation; the store is still decisive.
            try:
                order = await self.store.get_order(project["id"], user_id, order["id"])
            except Exception:
                order = {**order, "invoice_requested_at": True}
            return self._order_screen(
                order, unavailable=type(exc).__name__ == "BillingUnavailable"
            )
        if not isinstance(checkout, dict):
            return self._order_screen(order)
        merged = {**order, **checkout, "id": order["id"]}
        return self._order_screen(merged, payment_url=checkout.get("confirmation_url"))

    async def _addon_subscription(self, project, user_id):
        rows = await self.store.list_subscriptions(project["id"], user_id)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise LookupError("Subscription not found")
        row = rows[0]
        if (
            row.get("project_id", project["id"]) != project["id"]
            or row.get("tg_id", user_id) != user_id
        ):
            raise LookupError("Subscription not found")
        _uuid(row["id"])
        return row

    async def _addons(self, project, user_id, purpose):
        title = "Докупить ГБ" if purpose == "traffic" else "Докупить устройства"
        back = [[_button("К подписке", "subscriptions")]]
        if (
            not getattr(self.store, "entitlements_enabled", False)
            or self.checkout is None
            or not callable(getattr(self.checkout, "quote_addons", None))
        ):
            return _screen(
                f"<b>{title}</b>\n\nДокупка пока не подключена. Заказ не создан, деньги не списаны.",
                back,
            )
        sub = await self._addon_subscription(project, user_id)
        expiry = _date(sub.get("expires_at"))
        if expiry is None or expiry <= datetime.now(timezone.utc):
            return _screen(f"<b>{title}</b>\n\nСначала продлите подписку.", back)
        addons = await self.checkout.quote_addons(project["id"], user_id, sub["id"])
        if not isinstance(addons, dict):
            raise ValueError("Invalid catalogue")
        details = addons.get("devices", {}) if purpose == "devices" else addons
        status = details.get(
            "addon_status" if purpose == "devices" else "traffic_status"
        )
        if status != "available":
            reason = {
                "expired": "Сначала продлите подписку.",
                "max_limit": "Достигнут максимум: 10 устройств.",
                "unavailable": "Для этой подписки докупка недоступна.",
            }.get(status, "Каталог пока не загрузился. Повторите позже.")
            return _screen(
                f"<b>{title}</b>\n\n{reason}", [[_button("Обновить", purpose)], *back]
            )
        packages = details.get(
            "packages" if purpose == "devices" else "traffic_packages"
        )
        if not isinstance(packages, list):
            raise ValueError("Invalid packages")
        buttons = []
        for package in packages:
            if not isinstance(package, dict):
                raise ValueError("Invalid package")
            package_id = (
                str(package.get("count")) if purpose == "devices" else package.get("id")
            )
            price = package.get("price_kopecks")
            quantity = package.get("count" if purpose == "devices" else "gb")
            if (
                not isinstance(package_id, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,15}", package_id)
                or type(price) is not int
                or not 1 <= price <= MAX_PRICE_KOPECKS
                or type(quantity) is not int
                or quantity <= 0
            ):
                continue  # Never substitute a mutable list index for package identity.
            label = (
                f"+{quantity} "
                + ("ГБ" if purpose == "traffic" else "устр.")
                + f" · {_rubles(price)}"
            )
            data = f"addon:{purpose[0]}:{_short_id(uuid4())}:{package_id}:{price}"
            buttons.append([_button(label, data)])
        if not buttons:
            return _screen(
                f"<b>{title}</b>\n\nПакеты пока недоступны. Заказ не создан.", back
            )
        term = (
            "Пакет устройств действует до конца текущего срока подписки. Продление не продлевает его автоматически."
            if purpose == "devices"
            else "Докупка увеличивает лимит трафика и не сбрасывает уже использованные ГБ."
        )
        return _screen(
            f"<b>{title}</b>\n\n{term}\n\nВыберите пакет, затем подтвердите покупку.",
            [*buttons, *back],
        )

    async def _prepare_addon(self, project, user_id, data):
        match = re.fullmatch(
            r"addon:([td]):([A-Za-z0-9_-]{22}):([a-z0-9][a-z0-9_-]{0,15}):([1-9][0-9]{0,7})",
            data,
        )
        if (
            not match
            or not getattr(self.store, "entitlements_enabled", False)
            or self.checkout is None
        ):
            raise ValueError("Invalid addon confirmation")
        purpose = "traffic" if match[1] == "t" else "devices"
        sub = await self._addon_subscription(project, user_id)
        order = await self.checkout.prepare_addon_order(
            project["id"],
            user_id,
            _long_id(match[2]),
            int(match[4]),
            subscription_id=sub["id"],
            purpose=purpose,
            package_id=match[3],
        )
        if order.get("status") not in _UNPAID or order.get("provider_payment_id"):
            return self._order_screen(order)
        price, quantity = order.get("price_kopecks"), order.get("addon_quantity")
        if (
            type(price) is not int
            or type(quantity) is not int
            or not 1 <= price <= MAX_PRICE_KOPECKS
        ):
            raise ValueError("Invalid stored addon")
        label = f"+{quantity} " + ("ГБ" if purpose == "traffic" else "устр.")
        expiry = _date(order.get("addon_expires_at"))
        term = f"\nДо: {expiry:%d.%m.%Y}" if expiry is not None else ""
        rows = [
            [_button("Подтвердить и открыть счёт", "resume:" + _short_id(order["id"]))],
            [_button("К подписке", "subscriptions")],
        ]
        if self._can_cancel_draft(order):
            rows.insert(
                1,
                [
                    _button(
                        "Отменить черновик", "cancel_draft:" + _short_id(order["id"])
                    )
                ],
            )
        return _screen(
            f"<b>Подтвердите докупку</b>\n\n{label}{term}\nК оплате: <b>{_rubles(price)}</b>\n\nСчёт ещё не создан.",
            rows,
        )

    async def _bonuses(self, project, user_id):
        loader = getattr(self.store, "get_referral_summary", None)
        if not getattr(self.store, "entitlements_enabled", False) or not callable(
            loader
        ):
            return _screen(
                "🎁 <b>Бонусы за друзей</b>\n\n+7 дней за друга, который впервые оплатит подписку.\n\nБонусная программа пока не включена. Приглашения сейчас не начисляют дни."
            )
        summary = await loader(project["id"], user_id)
        fields = (
            "invited_count",
            "qualified_count",
            "earned_days",
            "pending_days",
            "applied_days",
        )
        if not isinstance(summary, dict) or any(
            type(summary.get(k)) is not int or summary[k] < 0 for k in fields
        ):
            raise ValueError("Invalid bonus summary")
        text = (
            "🎁 <b>Бонусы за друзей</b>\n\n+7 дней за первую оплаченную подписку каждого нового друга.\n"
            "Продления друга новых дней не дают. Трафик и устройства бонусом не добавляются.\n\n"
            f"👥 Пришло: {summary['invited_count']} · Купили: {summary['qualified_count']}\n"
            f"✨ Начислено: {summary['earned_days']} дней\n"
            f"Применено: {summary['applied_days']} · Ожидают: {summary['pending_days']}\n\n"
            "Без подписки дни сохраняются. После первой покупки они добавятся к её сроку; повторная покупка для уже оплаченной ранее подписки не нужна."
        )
        bot = project.get("bot") or {}
        username = bot.get("username")
        rows = []
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            url = f"https://t.me/{username}?start=ref_{user_id}"
            text += "\n\nЛучший VPN: " + escape(url)
            rows.append(
                [
                    _button(
                        "Пригласить друга",
                        url="https://t.me/share/url?url="
                        + quote(url, safe="")
                        + "&text="
                        + quote("Лучший VPN:", safe=""),
                    )
                ]
            )
        rows.append([_button("В меню", "menu")])
        return _screen(text, rows)

    async def handle(self, project: dict, update: dict) -> list[dict]:
        """Return safe Telegram command dicts; never send or log a bot token."""
        if not isinstance(project, dict):
            return []
        try:
            project_id = _uuid(project.get("id"))
        except ValueError:
            return []
        project = {**project, "id": project_id}
        identity = _identity(update, project)
        if identity is None:
            return []
        user_id, message, callback_id = identity
        callback = update.get("callback_query")
        data = callback.get("data") if callback else None
        if callback and (
            not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64
        ):
            return []
        is_owner = (
            _positive_id(project.get("owner_tg_id"))
            and project["owner_tg_id"] == user_id
        )
        text = message.get("text", "") if not callback else ""
        referrer = None
        campaign = None
        if isinstance(text, str):
            command = text.split(maxsplit=1)
            bot = project.get("bot") if isinstance(project.get("bot"), dict) else {}
            start = (
                re.fullmatch(r"/start(?:@([A-Za-z0-9_]{5,32}))?", command[0], re.ASCII)
                if command
                else None
            )
            if (
                start
                and start.group(1)
                and start.group(1).lower() != str(bot.get("username", "")).lower()
            ):
                return []
            if start and len(command) == 2:
                if re.fullmatch(r"m_[a-z0-9]{20}", command[1]):
                    campaign = command[1]
                match = re.fullmatch(r"ref_([1-9][0-9]{0,18})", command[1], re.ASCII)
                if (
                    match
                    and _positive_id(int(match.group(1)))
                    and int(match.group(1)) != user_id
                ):
                    referrer = int(match.group(1))
        try:
            from services.partner_store import BotUpdateContext

            context = project.pop("_bot_update_context", None)
            if not isinstance(context, BotUpdateContext) or not context.matches(project, user_id):
                context = None
            # The registry's one fresh SELECT already proved this exact user
            # registered and their first attribution frozen. New/legacy users
            # keep the original cross-process locked registration, including
            # referral validation. No customer/role/price cache is used.
            registered = context is not None and context.registered and (
                self.management is None or context.attribution_frozen
            )
            if not registered:
                register = (
                    self.management.register_customer
                    if self.management is not None
                    else self.store.register_customer
                )
                registration_kwargs = (
                    {"campaign_code": campaign} if self.management is not None else {}
                )
                try:
                    await register(
                        project_id, user_id, referrer_tg_id=referrer, **registration_kwargs
                    )
                except ValueError as exc:
                    if referrer is None or type(exc).__name__ != "InvalidCommerce":
                        raise
                    # A broken/foreign referral must not prevent a legitimate
                    # customer from starting the bot. The store still validates
                    # and freezes the first binding atomically.
                    await register(project_id, user_id, **registration_kwargs)
            is_admin = is_owner
            # Staff membership affects only the menu's Admin button and the
            # Admin screen. Other customer screens neither expose Admin nor
            # perform privileged work, so do not add a remote role lookup to
            # every ordinary navigation tap. Privileged APIs still authorize
            # independently; menu/Admin always use a fresh read (no role cache).
            needs_admin_navigation = not callback or data in {"menu", "admin"}
            if self.management is not None and not is_owner and needs_admin_navigation:
                if context is not None:
                    from services.partner_management import PERMISSIONS

                    is_admin = "referrals" in PERMISSIONS.get(context.staff_role, ())
                else:
                    try:
                        await self.management.authorize(project_id, user_id, "referrals")
                        is_admin = True
                    except Exception as failure:
                        if type(failure).__name__ != "ManagementDenied":
                            raise
            if self.management is not None and (
                data in {"news_on", "news_off"}
                or text.strip() in {"/news_on", "/news_off"}
            ):
                enabled = data == "news_on" or text.strip() == "/news_on"
                await self.management.set_marketing_consent(
                    project_id, user_id, enabled
                )
                screen = _screen(
                    (
                        "Вы подписались на сообщения проекта. Отписаться можно командой /news_off."
                        if enabled
                        else "Сообщения проекта отключены. Уведомления об оплатах и подписке сохраняются."
                    ),
                    [[_button("Новости", "news")], [_button("В меню", "menu")]],
                )
            elif not callback or data == "menu":
                screen = render_menu(project, is_admin)
                if user_id in self.operator_ids and self.manager_username:
                    # A tenant bot cannot authenticate a platform operator.
                    # Open the central bot, whose own initData is verified by
                    # the platform support routes before granting any access.
                    screen["reply_markup"]["inline_keyboard"].append([_button(
                        "🛠 Управление проектом",
                        url=f"https://t.me/{self.manager_username}?start=support_{project_id}",
                    )])
            elif data == "buy":
                subscriptions, pending = await self._subscription_state(project, user_id)
                unpaid_empty = bool(subscriptions) and all(
                    not _date(item.get("expires_at"))
                    and not _date(item.get("desired_expires_at"))
                    for item in subscriptions
                )
                if unpaid_empty and pending is None:
                    screen = render_purchase(project)
                elif subscriptions:
                    screen = await self._subscriptions(
                        project, user_id, subscriptions=subscriptions, pending=pending
                    )
                else:
                    screen = render_purchase(project)
            elif data == "subscriptions":
                screen = await self._subscriptions(project, user_id)
            elif data.startswith("renew:"):
                subscription_id = _long_id(data[6:])
                subscriptions = await self.store.list_subscriptions(project_id, user_id)
                if not any(
                    isinstance(item, dict) and _uuid(item.get("id")) == subscription_id
                    for item in subscriptions
                ):
                    screen = _screen("Подписка не найдена.")
                else:
                    screen = render_purchase(project, subscription_id)
            elif data.startswith("pay:"):
                screen = await self._purchase(project, user_id, data)
            elif data.startswith("addon:"):
                screen = await self._prepare_addon(project, user_id, data)
            elif data.startswith("cancel_draft:") or data.startswith("confirm_cancel:"):
                screen = await self._cancel_draft(
                    project, user_id, data, confirm=data.startswith("confirm_cancel:")
                )
            elif data.startswith("resume:"):
                order_id = _long_id(data[7:])
                order = await self.store.get_order(project_id, user_id, order_id)
                screen = (
                    await self._checkout_order(project, user_id, order)
                    if order.get("status") in _UNPAID
                    else self._order_screen(order)
                )
            elif data.startswith("order:"):
                order_id = _long_id(data[6:])
                order = await self.store.get_order(project_id, user_id, order_id)
                screen = self._order_screen(
                    order,
                    unavailable=self.checkout is None
                    and order.get("status") in _UNPAID,
                )
            elif data.startswith("check:"):
                order_id = _long_id(data[6:])
                # Ownership is checked before invoking any external provider.
                order = await self.store.get_order(project_id, user_id, order_id)
                if self.checkout is not None:
                    try:
                        reconciled = await self.checkout.reconcile(
                            project_id, user_id, order_id
                        )
                        if isinstance(reconciled, dict):
                            order = {**order, **reconciled, "id": order_id}
                    except Exception:
                        if (
                            order.get("status") not in _PENDING
                            and order.get("status") != "active"
                        ):
                            screen = _screen(
                                "Не удалось проверить оплату. Повторно платить не нужно: попробуйте проверить этот же заказ позже.",
                                [
                                    [_button("Проверить оплату", data)],
                                    [_button("Моя подписка", "subscriptions")],
                                ],
                            )
                            return self._commands(user_id, message, callback_id, screen)
                screen = self._order_screen(
                    order,
                    unavailable=self.checkout is None
                    and order.get("status") in _UNPAID,
                )
            elif data in {"devices", "traffic"}:
                screen = await self._addons(project, user_id, data)
            elif data == "bonuses":
                screen = await self._bonuses(project, user_id)
            elif data == "connect":
                screen = render_connection(project)
            elif data in {"connect:android", "connect:iphone"}:
                screen = render_connection(project, data.split(":", 1)[1])
            elif data in {"help", "news"}:
                url = _customer_support(project) if data == "help" else _safe_url(project.get("news_url"))
                title = "🆘 Помощь" if data == "help" else "📢 Новости"
                body = (
                    "Поможем с оплатой, подпиской и подключением.\n\n"
                    "Напишите, какое у вас устройство и приложение, и что не получается. "
                    "Можно приложить скриншот ошибки.\n\n"
                    "🔒 Не отправляйте пароли, коды входа и данные карты."
                    if data == "help"
                    else "Новости проекта — по ссылке ниже."
                )
                if url:
                    screen = _screen(
                        f"<b>{title}</b>\n\n{body}",
                        [
                            [
                                _button(
                                    (
                                        "Открыть поддержку"
                                        if data == "help"
                                        else "Открыть новости"
                                    ),
                                    url=url,
                                )
                            ],
                            [_button("В меню", "menu")],
                        ],
                    )
                else:
                    screen = _screen(
                        f"<b>{title}</b>\n\n"
                        + (
                            "Контакт поддержки пока не добавлен."
                            if data == "help"
                            else "Новостей пока нет."
                        )
                    )
                if data == "news" and self.management is not None:
                    preference = await self.management.get_marketing_consent(
                        project_id, user_id
                    )
                    screen["reply_markup"]["inline_keyboard"].insert(
                        0,
                        [
                            _button(
                                (
                                    "Отключить сообщения проекта"
                                    if preference["enabled"]
                                    else "Подписаться на сообщения проекта"
                                ),
                                "news_off" if preference["enabled"] else "news_on",
                            )
                        ],
                    )
            elif data == "admin" and is_admin:
                cabinet_button = None
                if self.workspace_origin is not None:
                    # The project UUID is canonicalized from the trusted webhook
                    # project above, never from callback data or a user URL. The
                    # backend still verifies tenant initData and project access.
                    cabinet_button = {
                        "text": "Личный кабинет",
                        "web_app": {
                            "url": self.workspace_origin + "/partners/workspace/" + project_id
                        },
                    }
                elif self.manager_url:
                    # Compatibility for hosts not yet passing workspace_origin.
                    # The standalone runtime explicitly supplies the new origin.
                    cabinet_button = _button("Личный кабинет", url=self.manager_url)
                screen = _screen(
                    "⚙️ <b>Личный кабинет проекта</b>\n\n"
                    "Обзор, клиенты, заказы и настройки проекта — в одном месте. "
                    "Здесь можно изменить название, оформление и цену подписки. "
                    "Доступ к разделам зависит от вашей роли.",
                    (
                        [
                            [cabinet_button],
                            [_button("Поддержка платформы", url="https://t.me/vpnislam")],
                            [_button("В меню", "menu")],
                        ]
                        if cabinet_button is not None
                        else None
                    ),
                )
            else:
                screen = _screen("Действие недоступно.")
        except Exception as exc:
            kind = type(exc).__name__
            if kind == "CommerceConflict" and callback and data.startswith("pay:"):
                parts = data.split(":")
                renewal_id = _long_id(parts[3]) if len(parts) == 4 else None
                screen = render_purchase(project, renewal_id)
                screen["text"] = (
                    "Цена или условия покупки изменились. Проверьте их и подтвердите заново.\n\n"
                    + screen["text"]
                )
            elif kind == "SalesUnavailable":
                screen = _screen("Продажи пока не подключены. Деньги не списаны.")
            elif kind == "CommerceConflict":
                screen = _screen(
                    "Условия докупки изменились или предыдущая покупка ещё активируется. Обновите подписку. Повторно платить не нужно.",
                    [[_button("Моя подписка", "subscriptions")]],
                )
            elif isinstance(exc, LookupError) or kind == "CommerceNotFound":
                screen = _screen("Заказ или подписка не найдены.")
            elif isinstance(exc, ValueError):
                screen = _screen(
                    "Не удалось выполнить действие. Попробуйте ещё раз. Если вы уже оплатили — повторно платить не нужно."
                )
            else:
                # A transient durable write failure must reach the webhook's
                # 503 boundary so Telegram can retry this same update. Do not
                # acknowledge an unsaved registration/order with a friendly200.
                raise
        return self._commands(user_id, message, callback_id, screen)
