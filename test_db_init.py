#!/usr/bin/env python3
"""
Скрипт для тестирования инициализации базы данных

Запуск:
    python test_db_init.py
"""

import asyncio
import logging
from database import init_db, close_db

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

logger = logging.getLogger(__name__)


async def main():
    """Тест инициализации БД"""
    try:
        logger.info("Starting database initialization test...")
        
        # Инициализируем БД (создаёт таблицы + выполняет миграции)
        await init_db()
        
        logger.info("✅ Database initialized successfully!")
        logger.info("Tables created:")
        logger.info("  - users")
        logger.info("  - payments")
        logger.info("  - promo_codes")
        
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return 1
    finally:
        await close_db()
        logger.info("Database connection closed")
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
