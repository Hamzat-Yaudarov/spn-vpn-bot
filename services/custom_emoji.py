from __future__ import annotations

from aiogram.types import InlineKeyboardButton

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
) -> InlineKeyboardButton:
    """Создать кнопку с custom emoji и безопасным обычным emoji до загрузки набора."""
    icon_id = custom_emoji_id(emoji_key)
    label = text if icon_id else " ".join(part for part in (fallback_emoji, text) if part)
    if icon_id:
        kwargs["icon_custom_emoji_id"] = icon_id
    return InlineKeyboardButton(text=label, **kwargs)
