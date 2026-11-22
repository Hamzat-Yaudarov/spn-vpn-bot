"""Neutral, ready-to-use branding for NEW projects; never rewrites a brand."""

DEFAULT_CUSTOMER_SUPPORT_URL = "https://t.me/wayspn_support"


def brand_defaults(title):
    return {
        "accent_color": "#e6c77a",
        "welcome_text": "Рады видеть вас! Здесь можно купить подписку, подключить VPN и получить бонусы за друзей. Начните с покупки — дальше подскажем каждый шаг.",
        "subscription_title": title,
        "subscription_description": "Ваша подписка для подключения в Happ или INCY. Инструкция и помощь — в вашем боте.",
    }


def telegram_profile(title):
    return {
        "name": title[:64],
        "description": (
            title + "\n\n"
            "🌍 Обычные серверы и антиглушилка в одной подписке.\n"
            "📦 300 ГБ · 📱 3 устройства. При необходимости можно докупить больше.\n\n"
            "👇 Нажмите «Старт» — поможем купить подписку и подключиться."
        ),
        "short_description": "VPN: обычные серверы и антиглушилка. Подписка, подключение и помощь — в одном боте.",
        "commands": [{"command": "start", "description": "Главное меню"}],
    }
