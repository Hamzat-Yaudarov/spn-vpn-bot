import asyncio
import logging
from typing import Callable, Any, TypeVar
from config import API_RETRY_ATTEMPTS, API_RETRY_INITIAL_DELAY, API_RETRY_MAX_DELAY


logger = logging.getLogger(__name__)

T = TypeVar('T')


async def retry_with_backoff(
    func: Callable[..., Any],
    *args,
    max_attempts: int = API_RETRY_ATTEMPTS,
    initial_delay: float = API_RETRY_INITIAL_DELAY,
    max_delay: float = API_RETRY_MAX_DELAY,
    backoff_factor: float = 2.0,
    **kwargs
) -> Any:
    """
    Выполнить функцию с повторными попытками и exponential backoff
    
    Args:
        func: Асинхронная функция для выполнения
        max_attempts: Максимальное количество попыток
        initial_delay: Начальная задержка в секундах
        max_delay: Максимальная задержка между попытками
        backoff_factor: Множитель для exponential backoff (по умолчанию 2)
        *args: Позиционные аргументы для функции
        **kwargs: Именованные аргументы для функции
        
    Returns:
        Результат выполнения функции или None если все попытки исчерпаны
        
    Raises:
        Exception: Последнее исключение если все попытки неудачны
    """
    delay = initial_delay
    last_exception = None
    func_name = getattr(func, '__name__', str(func))
    
    for attempt in range(1, max_attempts + 1):
        try:
            logger.debug(f"[{func_name}] Попытка {attempt}/{max_attempts}")
            result = await func(*args, **kwargs)
            
            if attempt > 1:
                logger.info(f"[{func_name}] ✅ Успешно с {attempt}-й попытки")
            
            return result
            
        except asyncio.TimeoutError as e:
            last_exception = e
            logger.warning(f"[{func_name}] ⏱️ Timeout на попытке {attempt}/{max_attempts}")
            
        except Exception as e:
            last_exception = e
            logger.warning(f"[{func_name}] ❌ Ошибка на попытке {attempt}/{max_attempts}: {type(e).__name__}: {e}")
        
        # Если это последняя попытка, не ждём
        if attempt < max_attempts:
            await asyncio.sleep(delay)
            # Exponential backoff: delay *= backoff_factor, но не больше max_delay
            delay = min(delay * backoff_factor, max_delay)
            logger.debug(f"[{func_name}] Ожидание {delay:.1f}с перед следующей попыткой...")
    
    # Если все попытки исчерпаны
    logger.error(f"[{func_name}] ❌ Все {max_attempts} попыток исчерпаны")
    if last_exception:
        raise last_exception
    
    raise RuntimeError(f"Function {func_name} failed after {max_attempts} attempts")


async def safe_api_call(
    func: Callable[..., Any],
    *args,
    error_message: str = "API call failed",
    **kwargs
) -> Any:
    """
    Безопасный вызов API функции с retry и обработкой ошибок
    
    Args:
        func: Асинхронная функция для выполнения
        error_message: Сообщение об ошибке для логирования
        *args: Позиционные аргументы
        **kwargs: Именованные аргументы
        
    Returns:
        Результат функции или None при ошибке
    """
    try:
        return await retry_with_backoff(func, *args, **kwargs)
    except Exception as e:
        logger.error(f"{error_message}: {type(e).__name__}: {e}")
        return None
