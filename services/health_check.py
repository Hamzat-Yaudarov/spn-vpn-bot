import logging
import asyncio
from datetime import datetime, timezone
from aiohttp import web
import database as db


# Переменные для отслеживания здоровья бота
_bot_health = {
    'started_at': None,
    'is_running': False,
    'last_payment_check': None,
    'last_notification_check': None,
    'db_pool_available': False,
}


def set_bot_started():
    """Отметить что бот запущен"""
    _bot_health['started_at'] = datetime.now(timezone.utc).isoformat()
    _bot_health['is_running'] = True
    _bot_health['db_pool_available'] = True


def set_bot_stopped():
    """Отметить что бот остановлен"""
    _bot_health['is_running'] = False
    _bot_health['db_pool_available'] = False


def update_payment_check_time():
    """Обновить время последней проверки платежей"""
    _bot_health['last_payment_check'] = datetime.now(timezone.utc).isoformat()


def update_notification_check_time():
    """Обновить время последней проверки уведомлений"""
    _bot_health['last_notification_check'] = datetime.now(timezone.utc).isoformat()


async def health_check_handler(request):
    """HTTP эндпоинт для проверки здоровья бота"""
    try:
        # Проверяем БД
        try:
            pool = await db.get_pool()
            db_ok = pool is not None
            _bot_health['db_pool_available'] = db_ok
        except Exception as e:
            logging.error(f"Health check: DB pool error: {e}")
            db_ok = False
            _bot_health['db_pool_available'] = False
        
        # Подготавливаем ответ
        status = 'healthy' if _bot_health['is_running'] and db_ok else 'unhealthy'
        
        uptime_seconds = None
        if _bot_health['started_at']:
            started = datetime.fromisoformat(_bot_health['started_at'])
            uptime_seconds = (datetime.now(timezone.utc) - started).total_seconds()
        
        health_data = {
            'status': status,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'bot_running': _bot_health['is_running'],
            'db_available': _bot_health['db_pool_available'],
            'uptime_seconds': uptime_seconds,
            'started_at': _bot_health['started_at'],
            'last_payment_check': _bot_health['last_payment_check'],
            'last_notification_check': _bot_health['last_notification_check'],
        }
        
        http_status = 200 if status == 'healthy' else 503
        
        return web.json_response(health_data, status=http_status)
    
    except Exception as e:
        logging.error(f"Health check handler error: {e}", exc_info=True)
        return web.json_response(
            {'status': 'error', 'error': str(e)},
            status=500
        )


async def start_health_check_server(port: int = 8000):
    """
    Запустить HTTP сервер для health check эндпоинта
    
    Args:
        port: Порт на котором слушать (по умолчанию 8000)
    """
    app = web.Application()
    app.router.add_get('/health', health_check_handler)
    app.router.add_get('/healthz', health_check_handler)  # альтернативный эндпоинт
    
    runner = web.AppRunner(app)
    
    try:
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        
        logging.info(f"Health check server started on port {port}")
        logging.info(f"  /health - Полная информация о здоровье")
        logging.info(f"  /healthz - Краткая проверка (K8s compatible)")
        
        # Сохраняем runner для корректного shutdown'а
        return runner
    
    except Exception as e:
        logging.error(f"Failed to start health check server: {e}")
        raise


async def stop_health_check_server(runner):
    """
    Остановить HTTP сервер для health check
    
    Args:
        runner: AppRunner от aiohttp
    """
    try:
        await runner.cleanup()
        logging.info("Health check server stopped")
    except Exception as e:
        logging.error(f"Error stopping health check server: {e}")
