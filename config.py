import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# ────────────────────────────────────────────────
#                TELEGRAM BOT
# ────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ────────────────────────────────────────────────
#           SUPPORT & NEWS CHANNELS
# ────────────────────────────────────────────────

SUPPORT_URL = os.getenv("SUPPORT_URL", "")
NEWS_CHANNEL_USERNAME = os.getenv("NEWS_CHANNEL_USERNAME", "")
TELEGRAPH_AGREEMENT_URL = os.getenv("TELEGRAPH_AGREEMENT_URL", "")

# ────────────────────────────────────────────────
#              REMNAWAVE API CONFIG
# ────────────────────────────────────────────────

REMNAWAVE_BASE_URL = os.getenv("REMNAWAVE_BASE_URL", "")
REMNAWAVE_API_TOKEN = os.getenv("REMNAWAVE_API_TOKEN", "")
DEFAULT_SQUAD_UUID = os.getenv("DEFAULT_SQUAD_UUID", "")

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

TARIFFS = {
    "1m": {"days": 30, "price": 200},
    "3m": {"days": 90, "price": 449},
    "6m": {"days": 180, "price": 790},
    "12m": {"days": 365, "price": 1200}
}

# ────────────────────────────────────────────────
#             TASK CONFIGURATION
# ────────────────────────────────────────────────

PAYMENT_CHECK_INTERVAL = 30  # секунд - интервал проверки платежей
CLEANUP_CHECK_INTERVAL = 300  # 5 минут - интервал удаления истёкших платежей
PAYMENT_EXPIRY_TIME = 600  # 10 минут - время жизни неоплаченного счёта

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
