import logging
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime
from config import WEBHOOK_HOST, WEBHOOK_PORT
import database as db
from services.payment_processing import process_paid_payment


logger = logging.getLogger(__name__)

app = FastAPI(title="SPN VPN Bot Webhooks")

# Глобальная переменная для хранения экземпляра бота
_bot = None


def set_bot(bot):
    """Установить экземпляр бота для отправки уведомлений"""
    global _bot
    _bot = bot
    logger.info(f"✅ Bot instance set for webhook processing: {bot.token[:20]}...")
    if _bot is None:
        logger.error("⚠️ Bot instance is None! Webhooks will not work!")


async def _process_paid_invoice(bot, tg_id: int, invoice_id: str, tariff_code: str) -> bool:
    """
    Обработать оплаченный счёт и активировать подписку

    Args:
        bot: Экземпляр Bot
        tg_id: ID пользователя Telegram
        invoice_id: ID счёта в CryptoBot или Yookassa
        tariff_code: Код тарифа

    Returns:
        True если успешно, False иначе
    """
    return await process_paid_payment(bot, tg_id, invoice_id, tariff_code, acquire_lock=True)


@app.post("/webhook/cryptobot")
async def webhook_cryptobot(request: Request):
    """
    Webhook endpoint для CryptoBot платежей

    CryptoBot отправляет JSON с информацией об оплате:
    {
        "update_id": 123,
        "invoice_id": "456",
        "status": "paid",
        "paid_at": "2024-01-16T12:00:00Z"
    }
    """
    logger.info("🔔 CryptoBot webhook endpoint called")

    try:
        payload = await request.json()
        logger.info(f"📦 CryptoBot webhook payload received: {payload}")

        invoice_id = payload.get("invoice_id")
        status = payload.get("status")

        if not invoice_id or not status:
            logger.warning(f"❌ Invalid CryptoBot webhook payload (missing fields): {payload}")
            return JSONResponse({"ok": False, "error": "Missing required fields"}, status_code=400)

        logger.info(f"📊 CryptoBot invoice {invoice_id} status: {status}")

        if status != "paid":
            logger.info(f"⏭️ Ignoring CryptoBot webhook with status: {status}")
            return JSONResponse({"ok": True})

        # Получаем информацию о платеже из БД
        logger.info(f"🔍 Looking up payment for invoice {invoice_id} in database")
        result = await db.db_execute(
            """
            SELECT tg_id, tariff_code
            FROM payments
            WHERE invoice_id = $1 AND status = 'pending' AND provider = 'cryptobot'
            LIMIT 1
            """,
            (invoice_id,),
            fetch_one=True
        )

        if not result:
            logger.warning(f"❌ Payment record not found for invoice {invoice_id} (may already be processed)")
            return JSONResponse({"ok": True})

        tg_id = result['tg_id']
        tariff_code = result['tariff_code']

        logger.info(f"✅ Found payment: user {tg_id}, tariff {tariff_code}")

        # Проверяем доступность бота
        if not _bot:
            logger.error("❌ CRITICAL: Bot instance not available! Webhooks cannot process payments.")
            logger.error("⚠️ This usually means set_bot() was not called during initialization")
            return JSONResponse({"ok": False, "error": "Bot not available"}, status_code=500)

        # Обрабатываем платеж асинхронно
        logger.info(f"🚀 Creating async task to process payment for user {tg_id}")
        task = asyncio.create_task(_process_paid_invoice(_bot, tg_id, invoice_id, tariff_code))

        # Добавляем callback для отслеживания ошибок в фоновой задаче
        def task_done_callback(t):
            if t.cancelled():
                logger.warning(f"⚠️ Payment processing task cancelled for invoice {invoice_id}")
            elif t.exception():
                logger.error(f"❌ Payment processing task failed for invoice {invoice_id}: {t.exception()}")
            else:
                logger.info(f"✅ Payment processing task completed for invoice {invoice_id}")

        task.add_done_callback(task_done_callback)

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"❌ CryptoBot webhook error: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/webhook/yookassa")
async def webhook_yookassa(request: Request):
    """
    Webhook endpoint для Yookassa платежей

    Yookassa отправляет JSON с информацией об оплате:
    {
        "type": "notification",
        "event": "payment.succeeded",
        "object": {
            "id": "123",
            "status": "succeeded",
            "metadata": {
                "tg_id": "456",
                "tariff_code": "1m"
            }
        }
    }
    """
    logger.info("🔔 Yookassa webhook endpoint called")

    try:
        payload = await request.json()
        event = payload.get("event")
        webhook_type = payload.get("type")
        logger.info(f"📦 Yookassa webhook payload: type={webhook_type}, event={event}")

        if event != "payment.succeeded":
            logger.info(f"⏭️ Ignoring Yookassa event (not payment.succeeded): {event}")
            return JSONResponse({"ok": True})

        obj = payload.get("object", {})
        payment_id = obj.get("id")
        metadata = obj.get("metadata", {})

        tg_id_str = metadata.get("tg_id")
        tariff_code = metadata.get("tariff_code")

        if not all([payment_id, tg_id_str, tariff_code]):
            logger.warning(f"❌ Invalid Yookassa webhook payload (missing fields): {payload}")
            return JSONResponse({"ok": False, "error": "Missing required fields"}, status_code=400)

        try:
            tg_id = int(tg_id_str)
        except (ValueError, TypeError):
            logger.warning(f"❌ Invalid tg_id format in Yookassa webhook: {tg_id_str}")
            return JSONResponse({"ok": False, "error": "Invalid tg_id"}, status_code=400)

        logger.info(f"📊 Yookassa payment {payment_id} succeeded: user {tg_id}, tariff {tariff_code}")

        # Получаем информацию о платеже из БД
        logger.info(f"🔍 Looking up payment for ID {payment_id} in database")
        result = await db.db_execute(
            """
            SELECT tg_id, tariff_code
            FROM payments
            WHERE invoice_id = $1 AND status = 'pending' AND provider = 'yookassa'
            LIMIT 1
            """,
            (payment_id,),
            fetch_one=True
        )

        if not result:
            logger.warning(f"❌ Payment record not found for payment ID {payment_id} (may already be processed)")
            return JSONResponse({"ok": True})

        logger.info(f"✅ Found payment in database: user {tg_id}, tariff {tariff_code}")

        # Проверяем доступность бота
        if not _bot:
            logger.error("❌ CRITICAL: Bot instance not available! Webhooks cannot process payments.")
            logger.error("⚠️ This usually means set_bot() was not called during initialization")
            return JSONResponse({"ok": False, "error": "Bot not available"}, status_code=500)

        # Обрабатываем платеж асинхронно
        logger.info(f"🚀 Creating async task to process payment for user {tg_id}")
        task = asyncio.create_task(_process_paid_invoice(_bot, tg_id, payment_id, tariff_code))

        # Добавляем callback для отслеживания ошибок в фоновой задаче
        def task_done_callback(t):
            if t.cancelled():
                logger.warning(f"⚠️ Payment processing task cancelled for payment {payment_id}")
            elif t.exception():
                logger.error(f"❌ Payment processing task failed for payment {payment_id}: {t.exception()}")
            else:
                logger.info(f"✅ Payment processing task completed for payment {payment_id}")

        task.add_done_callback(task_done_callback)

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"❌ Yookassa webhook error: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    bot_available = "✅ Yes" if _bot else "❌ No"
    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "bot_available": bot_available,
        "webhook_endpoints": [
            "/webhook/cryptobot - CryptoBot payment notifications",
            "/webhook/yookassa - Yookassa payment notifications"
        ]
    })


@app.on_event("startup")
async def startup_event():
    """Called when the server starts"""
    logger.info("=" * 60)
    logger.info("🚀 Webhook Server Starting")
    logger.info("=" * 60)
    logger.info(f"📍 Listening on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    logger.info("📞 Webhook endpoints:")
    logger.info("  - POST /webhook/cryptobot")
    logger.info("  - POST /webhook/yookassa")
    logger.info("  - GET /health")
    logger.info(f"🤖 Bot instance available: {'✅ Yes' if _bot else '❌ No (will be set after connection)'}")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """Called when the server shuts down"""
    logger.info("=" * 60)
    logger.info("🛑 Webhook Server Shutting Down")
    logger.info("=" * 60)


async def run_webhook_server():
    """
    Запустить FastAPI сервер для webhook'ов

    Используется uvicorn для асинхронного запуска
    """
    import uvicorn

    logger.info("=" * 60)
    logger.info(f"🚀 Starting webhook server on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    logger.info("=" * 60)

    config = uvicorn.Config(
        app,
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        log_level="info",
        access_log=True
    )

    server = uvicorn.Server(config)
    await server.serve()
