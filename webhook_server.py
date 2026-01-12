"""
Простой HTTP сервер для приёма вебхуков от 1Plat
"""

import logging
import asyncio
import json
from aiohttp import web
from services.oneplat import process_oneplat_callback


logger = logging.getLogger(__name__)


async def oneplat_webhook_handler(request: web.Request) -> web.Response:
    """
    Обработчик вебхука от 1Plat
    
    Метод: POST
    Путь: /1plat-webhook
    """
    try:
        # Получаем тело запроса
        data = await request.json()
        
        logger.info(f"1Plat webhook received: {data}")
        
        # Обрабатываем коллбек
        success = await process_oneplat_callback(data)
        
        if success:
            # Возвращаем 200 для подтверждения обработки
            return web.json_response(
                {"success": True, "message": "Webhook processed"},
                status=200
            )
        else:
            logger.error(f"Failed to process 1Plat webhook: {data}")
            return web.json_response(
                {"success": False, "error": "Processing failed"},
                status=400
            )
            
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook request")
        return web.json_response(
            {"success": False, "error": "Invalid JSON"},
            status=400
        )
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return web.json_response(
            {"success": False, "error": str(e)},
            status=500
        )


async def create_webhook_app() -> web.Application:
    """
    Создаёт приложение aiohttp для обработки вебхуков
    """
    app = web.Application()
    
    # Маршруты
    app.router.add_post("/1plat-webhook", oneplat_webhook_handler)
    app.router.add_get("/health", lambda request: web.json_response({"status": "ok"}))
    
    return app


async def start_webhook_server(host: str = "0.0.0.0", port: int = 8080):
    """
    Запускает вебхук-сервер
    """
    app = await create_webhook_app()
    runner = web.AppRunner(app)
    
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    
    logger.info(f"Webhook server started on {host}:{port}")
    
    return runner


async def stop_webhook_server(runner: web.AppRunner):
    """
    Останавливает вебхук-сервер
    """
    await runner.cleanup()
    logger.info("Webhook server stopped")
