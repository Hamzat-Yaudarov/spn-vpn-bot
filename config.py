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
#                DATABASE CONFIG
# ────────────────────────────────────────────────

DB_FILE = os.getenv("DB_FILE", "spn_vpn_bot.db")

# ────────────────────────────────────────────────
#                LOGGING CONFIG
# ────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ────────────────────────────────────────────────
#                TARIFFS CONFIG
# ────────────────────────────────────────────────

TARIFFS = {
    "1m": {"days": 30, "price": 100},
    "3m": {"days": 90, "price": 249},
    "6m": {"days": 180, "price": 449},
    "12m": {"days": 365, "price": 990}
}

# ────────────────────────────────────────────────
#             TASK CONFIGURATION
# ────────────────────────────────────────────────

PAYMENT_CHECK_INTERVAL = 30  # секунд - интервал проверки платежей
