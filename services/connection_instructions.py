from html import escape


ANDROID_PLATFORM = "android"
IPHONE_PLATFORM = "iphone"

ANDROID_APP_URL = "https://play.google.com/store/apps/details?id=com.happproxy"
IPHONE_APP_URL = "https://apps.apple.com/ru/app/incy/id6756943388"


def _platform_details(platform: str) -> tuple[str, str, str]:
    if platform == ANDROID_PLATFORM:
        return "Android", "Happ Plus", ANDROID_APP_URL
    if platform == IPHONE_PLATFORM:
        return "iPhone", "INCY", IPHONE_APP_URL
    raise ValueError(f"Unsupported connection platform: {platform}")


def connection_app_url(platform: str) -> str:
    """Return the official store page for the selected connection app."""
    return _platform_details(platform)[2]


def connection_app_button_text(platform: str) -> str:
    _, app_name, _ = _platform_details(platform)
    return f"⬇️ Скачать {app_name}"


def build_connection_instruction(
    platform: str,
    *,
    support_url: str,
    subscription_url: str | None = None,
) -> str:
    """Build a short platform-specific Telegram instruction."""
    platform_name, app_name, _ = _platform_details(platform)
    safe_support_url = escape(support_url, quote=True)

    if subscription_url:
        safe_subscription_url = escape(subscription_url, quote=False)
        key = f"\n\n<b>Ваш ключ</b>\n<code>{safe_subscription_url}</code>"
        key_step = "2. Скопируйте ключ ниже\n"
        next_step = 3
    else:
        key = ""
        key_step = "2. В «Моих подписках» скопируйте ключ\n"
        next_step = 3

    if platform == ANDROID_PLATFORM:
        app_steps = (
            f"{next_step}. Откройте {app_name}\n"
            f"{next_step + 1}. Нажмите <b>+</b> → <b>Вставить из буфера</b>\n"
            f"{next_step + 2}. Включите VPN"
        )
    else:
        app_steps = (
            f"{next_step}. Откройте {app_name}\n"
            f"{next_step + 1}. Нажмите <b>+</b> и вставьте ключ\n"
            f"{next_step + 2}. Разрешите VPN и включите его"
        )

    return (
        f"📲 <b>Подключение на {platform_name}</b>\n\n"
        "<blockquote>"
        f"1. Установите {app_name}\n"
        f"{key_step}"
        f"{app_steps}"
        "</blockquote>"
        f"{key}\n\n"
        f"Нужна помощь? {safe_support_url}"
    )
