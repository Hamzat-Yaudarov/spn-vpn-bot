import asyncio
import logging
import signal
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, LOG_LEVEL, WEBHOOK_USE_POLLING
import database as db

# Импортируем все роутеры обработчиков
from handlers import start, callbacks, subscription, gift, referral, promo, admin, partnership
from services.cryptobot import check_cryptobot_invoices
from services.yookassa import check_yookassa_payments, cleanup_expired_payments
from services.subscription_notifications import check_and_send_notifications
import webhooks


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
    dp.include_router(partnership.router)
    dp.include_router(admin.router)
    logger.info("All handlers registered")


# ────────────────────────────────────────────────
#          GRACEFUL SHUTDOWN & SIGNAL HANDLING
# ────────────────────────────────────────────────

shutdown_event = asyncio.Event()


def handle_signal(sig):
    """Обработчик сигналов для graceful shutdown"""
    logger.info(f"Received signal {sig.name}, initiating graceful shutdown...")
    shutdown_event.set()


async def wait_for_shutdown():
    """Ждём сигнала завершения"""
    loop = asyncio.get_event_loop()

    # Регистрируем обработчики сигналов
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    # Ждём события завершения
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        logger.info("Shutdown event cancelled")
        raise


# ────────────────────────────────────────────────
#                  ГЛАВНАЯ ФУНКЦИЯ
# ────────────────────────────────────────────────

async def main():
    """Главная функция запуска бота"""
    logger.info("=" * 60)
    logger.info("Starting SPN VPN Bot...")
    logger.info("=" * 60)

    # Инициализируем БД
    await db.init_db()
    logger.info("✅ Database initialized")

    # Регистрируем обработчики
    setup_handlers()
    logger.info("✅ Handlers registered")

    # Устанавливаем экземпляр бота для webhook'ов
    webhooks.set_bot(bot)

    # Список активных задач
    tasks = []

    # Запускаем фоновые задачи проверки платежей (если polling включен)
    if WEBHOOK_USE_POLLING:
        logger.info("Polling mode enabled for payment checks")
        tasks.append(asyncio.create_task(check_cryptobot_invoices(bot)))
        tasks.append(asyncio.create_task(check_yookassa_payments(bot)))
    else:
        logger.info("Webhook mode enabled for payment checks (polling disabled)")

    # Запускаем задачу очистки истёкших платежей
    tasks.append(asyncio.create_task(cleanup_expired_payments()))

    # Запускаем задачу отправки уведомлений о заканчивающихся подписках
    tasks.append(asyncio.create_task(check_and_send_notifications(bot)))
    logger.info("✅ Background tasks started")

    # Запускаем webhook сервер (асинхронно)
    webhook_task = asyncio.create_task(webhooks.run_webhook_server())
    tasks.append(webhook_task)
    logger.info("✅ Webhook server started")

    try:
        # Запускаем polling бота в отдельной задаче
        bot_task = asyncio.create_task(dp.start_polling(bot))
        tasks.append(bot_task)
        logger.info("✅ Bot started polling...")

        # Ждём сигнала завершения (SIGINT, SIGTERM)
        await wait_for_shutdown()

        logger.warning("Shutdown signal received, gracefully stopping...")

        # Даём время на завершение текущих операций
        await asyncio.sleep(1)

        # Отменяем все задачи
        logger.info("Cancelling background tasks...")
        for task in tasks:
            if not task.done():
                task.cancel()

        # Ждём завершения всех задач (с timeout)
        pending = [t for t in tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=5
                )
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for {len(pending)} tasks to complete, forcing shutdown")
                # Собираем информацию о невыполненных задачах
                for task in pending:
                    if not task.done():
                        logger.warning(f"  - Task still running: {task.get_name()}")

        logger.info("✅ All tasks cancelled")

    except Exception as e:
        logger.error(f"Error in main loop: {e}")
        raise

    finally:
        # Закрываем соединение с ботом
        try:
            await bot.session.close()
            logger.info("✅ Bot session closed")
        except Exception as e:
            logger.warning(f"Error closing bot session: {e}")

        # Закрываем БД при выходе
        try:
            await db.close_db()
            logger.info("✅ Database pool closed")
        except Exception as e:
            logger.warning(f"Error closing database: {e}")

        logger.info("=" * 60)
        logger.info("Bot shut down gracefully")
        logger.info("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
