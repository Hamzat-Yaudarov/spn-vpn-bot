import asyncio
import logging
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, LOG_LEVEL, ADMIN_ID
import database as db

# Импортируем все роутеры обработчиков
from handlers import start, callbacks, subscription, gift, referral, promo, admin
from services.cryptobot import check_cryptobot_invoices
from services.yookassa import check_yookassa_payments, cleanup_expired_payments
from services.notifications import (
    send_subscription_expiry_notifications,
    send_subscription_expired_notifications,
    send_admin_daily_report
)
from services.health_check import (
    start_health_check_server,
    stop_health_check_server,
    set_bot_started,
    set_bot_stopped
)

# Глобальная aiohttp сессия (переиспользуется для всех запросов к API)
global_session = None


# ────────────────────────────────────────────────
#                 НАСТРОЙКА ЛОГОВ
# ────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────
#          ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА
# ────────────────────────────────────────────────

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ────────────────────────────────────────────────
#        УПРАВЛЕНИЕ ГЛОБАЛЬНОЙ SESSION
# ────────────────────────────────────────────────

async def init_global_session():
    """Инициализировать глобальную aiohttp сессию"""
    global global_session
    connector = aiohttp.TCPConnector(ssl=False)
    global_session = aiohttp.ClientSession(connector=connector)
    logger.info("Global aiohttp session initialized")


async def close_global_session():
    """Закрыть глобальную aiohttp сессию"""
    global global_session
    if global_session:
        await global_session.close()
        global_session = None
        logger.info("Global aiohttp session closed")


def get_global_session() -> aiohttp.ClientSession:
    """Получить глобальную aiohttp сессию"""
    global global_session
    if not global_session:
        raise RuntimeError("Global session not initialized. Call init_global_session() first.")
    return global_session


# ────────────────────────────────────────────────
#          РЕГИСТРАЦИЯ РОУТЕРОВ
# ────────────────────────────────────────────────

def setup_handlers():
    """Регистрируем все роутеры обработчиков"""
    dp.include_router(start.router)
    dp.include_router(callbacks.router)
    dp.include_router(subscription.router)
    dp.include_router(gift.router)
    dp.include_router(referral.router)
    dp.include_router(promo.router)
    dp.include_router(admin.router)
    logger.info("All handlers registered")


# ────────────────────────────────────────────────
#                  ГЛАВНАЯ ФУНКЦИЯ
# ────────────────────────────────────────────────

async def main():
    """Главная функция запуска бота"""
    health_check_runner = None

    try:
        # Инициализируем глобальную session
        await init_global_session()

        # Инициализируем БД
        await db.init_db()
        logger.info("Database initialized")

        # Регистрируем обработчики
        setup_handlers()

        # Запускаем health check сервер (порт 8000)
        health_check_runner = await start_health_check_server(port=8000)
        set_bot_started()

        # Запускаем фоновые задачи проверки платежей и очистки
        asyncio.create_task(check_cryptobot_invoices(bot))
        asyncio.create_task(check_yookassa_payments(bot))
        asyncio.create_task(cleanup_expired_payments())
        logger.info("Payment checker and cleanup tasks started")

        # Запускаем фоновые задачи уведомлений
        asyncio.create_task(send_subscription_expiry_notifications(bot))
        asyncio.create_task(send_subscription_expired_notifications(bot))
        if ADMIN_ID:
            asyncio.create_task(send_admin_daily_report(bot, ADMIN_ID))
        logger.info("Notification tasks started")

        # Выполняем polling
        logger.info("Bot started polling...")
        await dp.start_polling(bot)

    finally:
        # Закрываем ресурсы при выходе
        set_bot_stopped()

        if health_check_runner:
            await stop_health_check_server(health_check_runner)

        await db.close_db()
        logger.info("Database pool closed")

        await close_global_session()
        logger.info("Global session closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
