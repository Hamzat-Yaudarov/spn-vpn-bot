from __future__ import annotations

from aiogram.types import InlineKeyboardButton as AiogramInlineKeyboardButton

from config import WAY_SPN_CUSTOM_EMOJI_IDS


# Порядок используется командой /emoji_ids для подготовки готовой строки .env.
# Сами эмодзи могут быть взяты из любых существующих Telegram-наборов.
CUSTOM_EMOJI_KEYS = (
    "home",
    "buy",
    "subscriptions",
    "antijam",
    "connect",
    "support",
    "more",
    "renew",
    "device",
    "traffic",
    "bank_card",
    "crypto",
    "gift",
    "promo",
    "news",
    "invite",
    "settings",
    "back",
)

_LEADING_FALLBACKS = (
    "↩️",
    "🔙",
    "🛒",
    "🔑",
    "🔐",
    "🛡",
    "📲",
    "📱",
    "🆘",
    "🔄",
    "➕",
    "📦",
    "💳",
    "💎",
    "💰",
    "🏦",
    "🎁",
    "🎟",
    "📢",
    "👥",
    "🔗",
    "⚙️",
    "⚙",
    "🏠",
    "🤖",
    "🍎",
    "📄",
    "⚡",
    "←",
)


def custom_emoji_id(key: str | None) -> str | None:
    """Вернуть Telegram custom emoji ID для семантического значка."""
    if not key:
        return None
    value = WAY_SPN_CUSTOM_EMOJI_IDS.get(key)
    return str(value).strip() if value else None


def custom_emoji_button(
    text: str,
    *,
    emoji_key: str | None = None,
    fallback_emoji: str | None = None,
    **kwargs,
) -> AiogramInlineKeyboardButton:
    """Создать кнопку с custom emoji и безопасным обычным emoji до загрузки набора."""
    icon_id = custom_emoji_id(emoji_key)
    label = text if icon_id else " ".join(part for part in (fallback_emoji, text) if part)
    if icon_id:
        kwargs["icon_custom_emoji_id"] = icon_id
    return AiogramInlineKeyboardButton(text=label, **kwargs)


def infer_custom_emoji_key(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
) -> str | None:
    """Подобрать семантический значок для обычной пользовательской кнопки."""
    label = (text or "").casefold()
    callback = (callback_data or "").casefold()
    target_url = (url or "").casefold()

    if callback == "back_to_menu" or "главное меню" in label:
        return "home"
    if callback.startswith("back_") or "назад" in label or "отмена" in label or label.startswith(("←", "↩", "🔙")):
        return "back"
    if "support" in callback or "поддерж" in label or "помощ" in label or "support" in target_url:
        return "support"
    if (
        "yookassa" in callback
        or "check_payment" in callback
        or "withdraw" in callback
        or "банков" in label
        or "карт" in label
        or "сбп" in label
        or "баланс" in label
        or "вывести" in label
        or label.startswith(("💳", "💰", "🏦"))
    ):
        return "bank_card"
    if "cryptobot" in callback or "crypto" in callback or "usdt" in callback or "usdt" in label or label.startswith("💎"):
        return "crypto"
    if "reactivation" in callback or "бесплат" in label or label.startswith("🎁"):
        return "gift"
    if "promo" in callback or "промокод" in label or "купон" in label or label.startswith("🎟"):
        return "promo"
    if "news" in callback or "канал" in label or "новост" in label:
        return "news"
    if "referral" in callback or "partnership" in callback or "приглас" in label or "скопировать ссылку" in label:
        return "invite"
    if "renew" in callback or "продлить" in label or label.startswith("🔄"):
        return "renew"
    if "instruction" in callback or "connect" in callback or "подключ" in label or "android" in label or "iphone" in label:
        return "connect"
    if "device" in callback or "устройств" in label or "личный кабинет" in label:
        return "device"
    if callback.startswith("gb_") or "buy_gb" in callback or "traffic" in callback or "гб" in label or "трафик" in label:
        return "traffic"
    if "bypass" in callback or "антиглуш" in label or label.startswith("🛡"):
        return "antijam"
    if "my_subscriptions" in callback or "subscription_view" in callback or "открыть подписку" in label or "мои подписки" in label or label.startswith(("🔑", "🔐")):
        return "subscriptions"
    if "buy" in callback or callback.startswith("tariff_") or callback.startswith("plan_regular") or "купить" in label or "оплатить" in label or label.startswith("🛒"):
        return "buy"
    if "more_menu" in callback or label.strip() in {"ещё", "еще", "⋯", "..."}:
        return "more"
    if "settings" in callback or "admin" in callback or "настрой" in label:
        return "settings"
    if "дом" in label or label.startswith("🏠"):
        return "home"
    return None


def _without_leading_fallback(text: str) -> str:
    for prefix in _LEADING_FALLBACKS:
        if text.startswith(prefix):
            return text[len(prefix):].lstrip() or text
    return text


def semantic_button(
    *,
    text: str,
    emoji_key: str | None = None,
    **kwargs,
) -> AiogramInlineKeyboardButton:
    """Создать кнопку и автоматически заменить её обычный emoji на custom emoji."""
    resolved_key = emoji_key or infer_custom_emoji_key(
        text,
        callback_data=kwargs.get("callback_data"),
        url=kwargs.get("url"),
    )
    icon_id = custom_emoji_id(resolved_key)
    if icon_id:
        kwargs["icon_custom_emoji_id"] = icon_id
        text = _without_leading_fallback(text)
    return AiogramInlineKeyboardButton(text=text, **kwargs)
