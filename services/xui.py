import aiohttp
import logging
import json
import secrets
import string
from datetime import datetime, timedelta
from config import (
    XUI_PANEL_URL,
    XUI_PANEL_PATH,
    XUI_USERNAME,
    XUI_PASSWORD,
    SUB_PORT,
    SUB_EXTERNAL_HOST,
    INBOUND_ID,
    API_REQUEST_TIMEOUT
)
from utils import safe_api_call, retry_with_backoff


logger = logging.getLogger(__name__)


async def get_xui_session() -> aiohttp.ClientSession:
    """Получить аутентифицированную сессию для 3X-UI"""
    session = aiohttp.ClientSession()
    
    try:
        login_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/login"
        payload = {"username": XUI_USERNAME, "password": XUI_PASSWORD}
        
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as temp_session:
            async with temp_session.post(login_url, json=payload, ssl=False) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"XUI login failed ({resp.status}): {error_text}")
                
                data = await resp.json()
                if not data.get("success"):
                    raise Exception(f"XUI login failed: {data}")
                
                # Копируем cookies в новую сессию
                session.cookie_jar.update_cookies(resp.cookies)
                
                return session
                
    except Exception as e:
        await session.close()
        logger.error(f"Failed to get XUI session: {e}")
        raise


async def get_client_expiry(email: str) -> int:
    """Получить время истечения подписки клиента в миллисекундах"""
    async def _get_expiry():
        url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/getClientTraffics/{email}"
        
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Get client traffic failed ({resp.status}): {error_text}")
                
                data = await resp.json()
                if not data.get("success"):
                    raise Exception(f"Get client traffic failed: {data}")
                
                obj = data.get('obj', {})
                expiry = obj.get('expiryTime')
                if not expiry:
                    raise Exception("expiryTime not found in response")
                
                return int(expiry)
    
    return await safe_api_call(
        _get_expiry,
        error_message=f"Failed to get client expiry for {email}"
    )


async def create_or_extend_vip_client(
    tg_id: int,
    days: int,
    is_new: bool = False
) -> tuple[str, str, str, str] | None:
    """
    Создать или продлить VIP клиента в 3X-UI
    
    Args:
        tg_id: ID пользователя Telegram
        days: Количество дней для подписки
        is_new: True если это новый клиент
    
    Returns:
        Кортеж (email, uuid, subscription_id, sub_url) или None при ошибке
    """
    async def _create_or_extend():
        session = None
        try:
            # Генерируем случайные значения
            alphabet = string.ascii_lowercase + string.digits
            email = ''.join(secrets.choice(alphabet) for _ in range(12))
            client_uuid = ''.join(secrets.choice(alphabet) for _ in range(8))
            subscription_id = ''.join(secrets.choice(alphabet) for _ in range(16))
            
            # Рассчитываем время истечения
            add_ms = int(days * 30 * 24 * 60 * 60 * 1000)
            new_expiry = int(datetime.now().timestamp() * 1000) + add_ms
            
            # Создаём конфиг клиента
            settings = {
                "clients": [{
                    "id": client_uuid,
                    "flow": "xtls-rprx-vision",
                    "email": email,
                    "limitIp": 0,
                    "totalGB": 0,
                    "expiryTime": new_expiry,
                    "enable": True,
                    "tgId": str(tg_id),
                    "subId": subscription_id,
                    "reset": 0
                }]
            }
            
            payload = {
                "id": str(INBOUND_ID),
                "settings": json.dumps(settings)
            }
            
            # Выбираем URL в зависимости от того новый клиент или нет
            if is_new:
                api_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/addClient"
            else:
                api_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/updateClient/{client_uuid}"
            
            # Отправляем запрос
            timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, data=payload, ssl=False) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise Exception(f"XUI operation failed ({resp.status}): {error_text}")
                    
                    data = await resp.json()
                    if not data.get("success"):
                        raise Exception(f"XUI operation failed: {data}")
            
            # Генерируем ссылку подписки
            sub_url = f"https://{SUB_EXTERNAL_HOST}:{SUB_PORT}/{subscription_id}"
            
            logger.info(f"Created/extended VIP client for user {tg_id}: {subscription_id}")
            return email, client_uuid, subscription_id, sub_url
            
        except Exception as e:
            logger.error(f"Create/extend VIP client error: {e}")
            if session:
                await session.close()
            raise
    
    return await safe_api_call(
        _create_or_extend,
        error_message=f"Failed to create/extend VIP client for user {tg_id}"
    )


async def extend_vip_client(
    tg_id: int,
    email: str,
    client_uuid: str,
    subscription_id: str,
    days: int
) -> bool:
    """
    Продлить существующую VIP подписку клиента
    
    Args:
        tg_id: ID пользователя Telegram
        email: Email клиента в 3X-UI
        client_uuid: UUID клиента
        subscription_id: ID подписки
        days: Количество дней для добавления
    
    Returns:
        True если успешно, False иначе
    """
    async def _extend():
        # 1. Получаем текущее время истечения
        current_expiry = await get_client_expiry(email)
        
        # 2. Рассчитываем новое время истечения
        add_ms = int(days * 30 * 24 * 60 * 60 * 1000)
        new_expiry = current_expiry + add_ms
        
        # 3. Создаём конфиг обновления
        settings = {
            "clients": [{
                "id": client_uuid,
                "flow": "xtls-rprx-vision",
                "email": email,
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": new_expiry,
                "enable": True,
                "tgId": str(tg_id),
                "subId": subscription_id,
                "reset": 0
            }]
        }
        
        payload = {
            "id": str(INBOUND_ID),
            "settings": json.dumps(settings)
        }
        
        # 4. Отправляем запрос обновления
        api_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/updateClient/{client_uuid}"
        
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(api_url, data=payload, ssl=False) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Extend failed ({resp.status}): {error_text}")
                
                data = await resp.json()
                if not data.get("success"):
                    raise Exception(f"Extend failed: {data}")
        
        logger.info(f"Extended VIP subscription for user {tg_id} by {days} days")
        return True
    
    return await safe_api_call(
        _extend,
        error_message=f"Failed to extend VIP client for user {tg_id}"
    ) or False


async def delete_vip_client(email: str) -> bool:
    """
    Удалить VIP клиента из 3X-UI
    
    Args:
        email: Email клиента в 3X-UI
    
    Returns:
        True если успешно, False иначе
    """
    async def _delete():
        api_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/delClient/{email}"
        
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(api_url, ssl=False) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Delete failed ({resp.status}): {error_text}")
                
                data = await resp.json()
                if not data.get("success"):
                    raise Exception(f"Delete failed: {data}")
        
        logger.info(f"Deleted VIP client: {email}")
        return True
    
    return await safe_api_call(
        _delete,
        error_message=f"Failed to delete VIP client {email}"
    ) or False
