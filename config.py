import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# ────────────────────────────────────────────────
#                TELEGRAM BOT
# ────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://wayspn.ru/app")
BOT_USERNAME = os.getenv("BOT_USERNAME", "WaySPN_robot").lstrip("@")
_MINIAPP_URL_WITHOUT_SLASH = MINIAPP_URL.rstrip("/")
ADMIN_PANEL_URL = os.getenv("ADMIN_PANEL_URL", f"{_MINIAPP_URL_WITHOUT_SLASH.rsplit('/', 1)[0]}/admin")
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", _MINIAPP_URL_WITHOUT_SLASH.rsplit("/", 1)[0]).rstrip("/")
WEB_SESSION_DAYS = int(os.getenv("WEB_SESSION_DAYS", "30"))
WEB_COOKIE_SECURE = os.getenv("WEB_COOKIE_SECURE", "True").lower() == "true"

# Android Way VPN / mobile API
MOBILE_AUTH_CHALLENGE_MINUTES = int(os.getenv("MOBILE_AUTH_CHALLENGE_MINUTES", "5"))
MOBILE_ACCESS_TOKEN_MINUTES = int(os.getenv("MOBILE_ACCESS_TOKEN_MINUTES", "15"))
MOBILE_REFRESH_TOKEN_DAYS = int(os.getenv("MOBILE_REFRESH_TOKEN_DAYS", "30"))
ANDROID_PACKAGE_ID = os.getenv("ANDROID_PACKAGE_ID", "ru.wayspn.vpn")
ANDROID_LATEST_VERSION_CODE = int(os.getenv("ANDROID_LATEST_VERSION_CODE", "12"))
ANDROID_LATEST_VERSION_NAME = os.getenv("ANDROID_LATEST_VERSION_NAME", "1.2.0")
ANDROID_MIN_SUPPORTED_VERSION_CODE = int(os.getenv("ANDROID_MIN_SUPPORTED_VERSION_CODE", "1"))
ANDROID_APK_URL = os.getenv("ANDROID_APK_URL", f"{PUBLIC_SITE_URL}/downloads/WayVPN-1.2.0-universal-release.apk")
ANDROID_APK_SHA256 = os.getenv("ANDROID_APK_SHA256", "273928b614f42d0212dcba8302928cf1a9fbffe8c13cfeeb95a1f5fa81fda529")
ANDROID_SIGNING_CERT_SHA256 = os.getenv("ANDROID_SIGNING_CERT_SHA256", "980d9e8dcdeaebbdcbaea80ec1417880b0ebc57a3cc20ba75a4b8f2104060957")
REMNAWAVE_CA_BUNDLE = os.getenv("REMNAWAVE_CA_BUNDLE", "").strip()

# ────────────────────────────────────────────────
#           SUPPORT & NEWS CHANNELS
# ────────────────────────────────────────────────

SUPPORT_URL = os.getenv("SUPPORT_URL", "")
NEWS_CHANNEL_USERNAME = os.getenv("NEWS_CHANNEL_USERNAME", "")
TELEGRAPH_AGREEMENT_URL = os.getenv("TELEGRAPH_AGREEMENT_URL", "")

# ────────────────────────────────────────────────
#           PARTNERSHIP AGREEMENT URLs
# ────────────────────────────────────────────────

PARTNERSHIP_AGREEMENTS = {
    15: os.getenv("PARTNERSHIP_AGREEMENT_15", ""),
    20: os.getenv("PARTNERSHIP_AGREEMENT_20", ""),
    25: os.getenv("PARTNERSHIP_AGREEMENT_25", ""),
    30: os.getenv("PARTNERSHIP_AGREEMENT_30", "")
}

# ────────────────────────────────────────────────
#              REMNAWAVE API CONFIG
# ────────────────────────────────────────────────

REMNAWAVE_BASE_URL = os.getenv("REMNAWAVE_BASE_URL", "https://panel.wayspn.online/api")
REMNAWAVE_API_TOKEN = os.getenv("REMNAWAVE_API_TOKEN", "")
SUBSCRIPTION_PUBLIC_BASE_URL = os.getenv("SUBSCRIPTION_PUBLIC_BASE_URL", "https://sub.wayspn.online").rstrip("/")
DEFAULT_SQUAD_UUID = os.getenv("DEFAULT_SQUAD_UUID", "")
REGULAR_SQUAD_UUID = os.getenv("REGULAR_SQUAD_UUID", "89902b23-6765-425c-ae27-9bb43c121a70")
BYPASS_SQUAD_UUID = os.getenv("BYPASS_SQUAD_UUID", "3766e220-ebe1-4a0c-b53f-a4731f805d7e")

# ────────────────────────────────────────────────
#            CRYPTOBOT PAYMENT CONFIG
# ────────────────────────────────────────────────

CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_API_URL = os.getenv("CRYPTOBOT_API_URL", "")

# ────────────────────────────────────────────────
#            YOOKASSA PAYMENT CONFIG
# ────────────────────────────────────────────────

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
YOOKASSA_API_URL = "https://api.yookassa.ru/v3"

# ────────────────────────────────────────────────
#                DATABASE CONFIG (Supabase/PostgreSQL)
# ────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# ────────────────────────────────────────────────
#                LOGGING CONFIG
# ────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ────────────────────────────────────────────────
#                TARIFFS CONFIG
# ────────────────────────────────────────────────

REGULAR_HWID_DEVICE_LIMIT = 5
BYPASS_HWID_DEVICE_LIMIT = 3
HWID_DEVICE_LIMIT = REGULAR_HWID_DEVICE_LIMIT
DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT = 10
DEVICE_ADDON_PACKAGES = (1, 2, 3)
DEVICE_ADDON_PRICE_PER_30_DAYS = {
    "regular": 50,
    "bypass": 100,
}
DEVICE_ADDON_MIN_PRICE = 10
DEVICE_ADDON_DISCOUNT_STEP_PERCENT = 5
DEVICE_ADDON_MAX_DISCOUNT_PERCENT = 22
BYPASS_BASE_TRAFFIC_GB = 150
GB_BYTES = 1024 ** 3

REGULAR_TARIFFS = {
    "regular_1m": {"days": 30, "price": 200, "kind": "regular", "title": "Обычная 1 месяц"},
    "regular_3m": {"days": 90, "price": 500, "kind": "regular", "title": "Обычная 3 месяца"},
}

BYPASS_TARIFFS = {
    "bypass_1m": {"days": 30, "price": 300, "kind": "bypass", "base_gb": BYPASS_BASE_TRAFFIC_GB, "title": "С антиглушилкой 1 месяц"},
    "bypass_3m": {"days": 90, "price": 800, "kind": "bypass", "base_gb": BYPASS_BASE_TRAFFIC_GB, "title": "С антиглушилкой 3 месяца"},
}

BYPASS_TRAFFIC_PACKAGES = {
    "gb_10": {"gb": 10, "price": 24},
    "gb_20": {"gb": 20, "price": 45},
    "gb_40": {"gb": 40, "price": 79},
    "gb_80": {"gb": 80, "price": 149},
    "gb_150": {"gb": 150, "price": 289},
}

TARIFFS = {**REGULAR_TARIFFS, **BYPASS_TARIFFS}

# ────────────────────────────────────────────────
#             TASK CONFIGURATION
# ────────────────────────────────────────────────

PAYMENT_CHECK_INTERVAL = 30  # секунд - интервал проверки платежей
CLEANUP_CHECK_INTERVAL = 300  # 5 минут - интервал удаления истёкших платежей
PAYMENT_EXPIRY_TIME = 86400  # 24 часа - время жизни неоплаченного счёта (достаточно для вебхука от Юкассы)
TRACKING_ATTRIBUTION_DAYS = 30  # last-touch окно для привязки Telegram-платежа к рекламному переходу

# ────────────────────────────────────────────────
#           ANTI-SPAM COOLDOWNS
# ────────────────────────────────────────────────

GIFT_REQUEST_COOLDOWN = 2  # секунды - между попытками получить подарок
PROMO_REQUEST_COOLDOWN = 1.5  # секунды - между попытками активировать промокод
PAYMENT_CHECK_COOLDOWN = 1  # секунда - между проверками платежей

# ────────────────────────────────────────────────
#              API RETRY CONFIGURATION
# ────────────────────────────────────────────────

API_RETRY_ATTEMPTS = 3  # количество попыток при ошибке
API_RETRY_INITIAL_DELAY = 1  # начальная задержка в секундах
API_RETRY_MAX_DELAY = 10  # максимальная задержка между попытками
API_REQUEST_TIMEOUT = 30  # timeout для HTTP запросов в секундах

# ────────────────────────────────────────────────
#            WEBHOOK CONFIGURATION
# ────────────────────────────────────────────────

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8000"))
WEBHOOK_USE_POLLING = os.getenv("WEBHOOK_USE_POLLING", "False").lower() == "true"  # Fallback на polling
