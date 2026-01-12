import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, LOG_LEVEL
import database as db

# Импортируем все роутеры обработчиков
from handlers import start, callbacks, subscription, gift, referral, promo, admin
from services.cryptobot import check_cryptobot_invoices
from services.oneplat import handle_oneplat_callback


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
#              WEBHOOK HANDLERS FOR 1PLAT
# ────────────────────────────────────────────────

async def handle_oneplat_webhook(request: web.Request) -> web.Response:
    """
    Handle 1Plat webhook callback
    Expected to receive POST request with payment callback data
    """
    try:
        data = await request.json()
        logger.info(f"Received 1Plat webhook: {data.get('payment_id')}")

        # Process callback
        result = await handle_oneplat_callback(bot, data)

        if result.get("success"):
            return web.json_response({"status": "ok"}, status=200)
        else:
            logger.error(f"Failed to process 1Plat callback: {result.get('message')}")
            return web.json_response({"status": "error"}, status=400)

    except Exception as e:
        logger.error(f"Webhook handler exception: {e}")
        return web.json_response({"status": "error"}, status=500)


def setup_webhook_routes(app: web.Application):
    """Setup webhook routes for payment callbacks"""
    app.router.add_post('/1plat-webhook', handle_oneplat_webhook)
    logger.info("Webhook routes setup complete")


# ────────────────────────────────────────────────
#                  ГЛАВНАЯ ФУНКЦИЯ
# ────────────────────────────────────────────────

async def main():
    """Главная функция запуска бота"""
    # Инициализируем БД
    await db.init_db()
    logger.info("Database initialized")

    # Регистрируем обработчики
    setup_handlers()

    # Запускаем фоновую задачу проверки платежей
    asyncio.create_task(check_cryptobot_invoices(bot))
    logger.info("Payment checker task started")

    # Создаём web приложение для webhook
    app = web.Application()
    setup_webhook_routes(app)

    # Запускаем web сервер на порту 8080
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Webhook server started on port 8080")

    try:
        # Выполняем polling
        logger.info("Bot started polling...")
        await dp.start_polling(bot)
    finally:
        # Закрываем БД при выходе
        await db.close_db()
        await runner.cleanup()
        logger.info("Database pool closed and webhook server stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
