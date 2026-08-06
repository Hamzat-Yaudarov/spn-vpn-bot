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
    store_name = "Google Play" if platform == ANDROID_PLATFORM else "App Store"
    return f"⬇️ Скачать {app_name} в {store_name}"


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
        key_step = f"2. Скопируй ключ:\n<code>{safe_subscription_url}</code>\n"
        next_step = 3
    else:
        key_step = (
            "2. Открой <b>🔐 Мои подписки</b>\n"
            "3. Выбери подписку и скопируй ключ\n"
        )
        next_step = 4

    if platform == ANDROID_PLATFORM:
        app_steps = (
            f"{next_step}. Открой <b>{app_name}</b>\n"
            f"{next_step + 1}. Нажми <b>+</b> в правом верхнем углу\n"
            f"{next_step + 2}. Выбери <b>Вставить из буфера обмена</b>\n"
            f"{next_step + 3}. Подтверди добавление и включи подключение"
        )
    else:
        app_steps = (
            f"{next_step}. Открой <b>{app_name}</b>\n"
            f"{next_step + 1}. Нажми <b>+</b> и добавь подписку из буфера обмена\n"
            f"{next_step + 2}. Разреши создание VPN-конфигурации, если iPhone попросит\n"
            f"{next_step + 3}. Включи подключение"
        )

    return (
        f"📲 <b>Подключение на {platform_name}</b>\n\n"
        f"1. Установи <b>{app_name}</b> кнопкой ниже\n"
        f"{key_step}"
        f"{app_steps}\n\n"
        f"Если что-то не получается: {safe_support_url}"
    )
