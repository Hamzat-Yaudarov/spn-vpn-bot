import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, LOG_LEVEL
import database as db

# Импортируем все роутеры обработчиков
from handlers import start, callbacks, subscription, gift, referral, promo, admin
from services.cryptobot import check_cryptobot_invoices


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
    # Инициализируем БД
    db.init_db()
    logger.info("Database initialized")
    
    # Регистрируем обработчики
    setup_handlers()
    
    # Запускаем фоновую задачу проверки платежей
    asyncio.create_task(check_cryptobot_invoices(bot))
    logger.info("Payment checker task started")
    
    # Выполняем polling
    logger.info("Bot started polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
