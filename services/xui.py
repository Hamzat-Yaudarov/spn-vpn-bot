import aiohttp
import logging
import secrets
import string
import json
from datetime import datetime, timedelta
from config import API_REQUEST_TIMEOUT
from utils import retry_with_backoff, safe_api_call


# Параметры 3X-UI из окружения (нужно добавить в .env)
XUI_PANEL_URL = None
XUI_PANEL_PATH = None
XUI_USERNAME = None
XUI_PASSWORD = None
SUB_PORT = None
SUB_EXTERNAL_HOST = None
INBOUND_ID = None


def init_xui_config(panel_url: str, panel_path: str, username: str, password: str,
                    sub_port: int, sub_external_host: str, inbound_id: int):
    """
    Инициализировать параметры 3X-UI
    
    Args:
        panel_url: URL панели 3X-UI (например, https://51.250.117.234:2053)
        panel_path: Путь к панели (например, /sXvL8myMex46uSa3NP/panel)
        username: Логин для панели
        password: Пароль для панели
        sub_port: Порт для выдачи подписок (например, 2096)
        sub_external_host: Внешний хост для ссылок подписок (например, 51.250.117.234)
        inbound_id: ID inbound для создания клиентов
    """
    global XUI_PANEL_URL, XUI_PANEL_PATH, XUI_USERNAME, XUI_PASSWORD, SUB_PORT, SUB_EXTERNAL_HOST, INBOUND_ID
    XUI_PANEL_URL = panel_url
    XUI_PANEL_PATH = panel_path
    XUI_USERNAME = username
    XUI_PASSWORD = password
    SUB_PORT = sub_port
    SUB_EXTERNAL_HOST = sub_external_host
    INBOUND_ID = inbound_id


async def xui_get_session():
    """
    Получить сессию 3X-UI с аутентификацией
    
    Returns:
        aiohttp.ClientSession с cookie аутентификации
    """
    session = aiohttp.ClientSession()
    
    try:
        login_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH.replace('/panel', '')}/login"
        payload = {
            "username": XUI_USERNAME,
            "password": XUI_PASSWORD
        }
        
        async with session.post(login_url, json=payload, ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise Exception(f"3X-UI login failed with status {resp.status}")
            
            data = await resp.json()
            if not data.get("success"):
                raise Exception(f"3X-UI login failed: {data}")
                
        logging.info("Successfully authenticated with 3X-UI")
        return session
        
    except Exception as e:
        await session.close()
        raise Exception(f"3X-UI authentication error: {str(e)}")


async def xui_get_client_expiry(session: aiohttp.ClientSession, email: str) -> int:
    """
    Получить время истечения подписки клиента в 3X-UI
    
    Args:
        session: aiohttp сессия
        email: Email (идентификатор) клиента
        
    Returns:
        expiryTime в миллисекундах
    """
    async def _get_expiry():
        get_traffic_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/getClientTraffics/{email}"
        
        async with session.get(get_traffic_url, ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"3X-UI get client traffic failed ({resp.status}): {error_text}")
            
            data = await resp.json()
            if not data.get("success"):
                raise Exception(f"3X-UI API error: {data}")
            
            return data['obj']['expiryTime']
    
    return await safe_api_call(
        _get_expiry,
        error_message=f"Failed to get client expiry from 3X-UI for {email}"
    )


async def xui_get_or_create_client(tg_id: int, days: int = 30, extend_if_exists: bool = False) -> tuple[str | None, str | None]:
    """
    Получить или создать клиента в 3X-UI для VIP подписки (Обход глушилок)
    
    Args:
        tg_id: ID пользователя Telegram
        days: Количество дней подписки
        extend_if_exists: Продлить если клиент уже существует
        
    Returns:
        Кортеж (UUID клиента, Email клиента) или (None, None)
    """
    session = await xui_get_session()
    
    try:
        client_email = f"vip_{tg_id}"  # Используем префикс VIP для различия
        
        # Пытаемся получить существующего клиента
        try:
            expiry_time = await xui_get_client_expiry(session, client_email)
            
            if extend_if_exists:
                # Продлеваем существующего клиента
                add_ms = int(days * 30 * 24 * 60 * 60 * 1000)
                new_expiry = expiry_time + add_ms
                
                settings = {
                    "clients": [{
                        "email": client_email,
                        "expiryTime": new_expiry,
                    }]
                }
                
                update_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/updateClient/{client_email}"
                payload = {
                    "id": str(INBOUND_ID),
                    "settings": json.dumps(settings)
                }
                
                async with session.post(update_url, data=payload, ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise Exception(f"3X-UI update failed ({resp.status}): {error_text}")
                    
                    data = await resp.json()
                    if not data.get("success"):
                        raise Exception(f"3X-UI update failed: {data}")
                    
                    logging.info(f"Extended VIP client for user {tg_id} in 3X-UI")
                    return client_email, client_email
            
            # Клиент существует и не нужно продлевать
            logging.info(f"VIP client already exists for user {tg_id}")
            return client_email, client_email
            
        except Exception as e:
            if "API error" not in str(e) and "failed" not in str(e).lower():
                # Клиента нет, создаём нового
                pass
            else:
                raise
        
        # Создаём нового клиента
        client_uuid = f"vip_{tg_id}_{secrets.token_hex(4)}"
        
        add_ms = int(days * 30 * 24 * 60 * 60 * 1000)
        new_expiry = int(datetime.utcnow().timestamp() * 1000) + add_ms
        
        settings = {
            "clients": [{
                "id": client_uuid,
                "flow": "",
                "email": client_email,
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": new_expiry,
                "enable": True,
                "tgId": str(tg_id),
                "reset": 0
            }]
        }
        
        add_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/addClient"
        payload = {
            "id": str(INBOUND_ID),
            "settings": json.dumps(settings)
        }
        
        async with session.post(add_url, data=payload, ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"3X-UI add client failed ({resp.status}): {error_text}")
            
            data = await resp.json()
            if not data.get("success"):
                raise Exception(f"3X-UI add client failed: {data}")
            
            logging.info(f"Created new VIP client in 3X-UI for user {tg_id}")
            return client_email, client_email
    
    finally:
        await session.close()


async def xui_get_subscription_url(tg_id: int, email: str) -> str | None:
    """
    Получить ссылку подписки VIP клиента из 3X-UI
    
    Args:
        tg_id: ID пользователя Telegram
        email: Email клиента в 3X-UI
        
    Returns:
        Ссылка подписки или None
    """
    try:
        # Формируем ссылку подписки на основе email (который работает как sub_id)
        sub_url = f"http://{SUB_EXTERNAL_HOST}:{SUB_PORT}/sub/{email}"
        logging.info(f"Generated VIP subscription URL for user {tg_id}")
        return sub_url
    except Exception as e:
        logging.error(f"Error generating subscription URL for {tg_id}: {e}")
        return None
