import logging
import json
import uuid
import aiohttp
from datetime import datetime
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


async def get_xui_session() -> aiohttp.ClientSession:
    """
    Получить сессию с 3X-UI с авторизацией
    
    Returns:
        aiohttp.ClientSession с авторизованными cookies
        
    Raises:
        Exception если не удалось авторизоваться
    """
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
    session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    
    login_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH.replace('/panel', '')}/login"
    
    try:
        payload = {
            "username": XUI_USERNAME,
            "password": XUI_PASSWORD
        }
        
        response = await session.post(login_url, json=payload)
        response.raise_for_status()
        
        data = await response.json()
        
        if not data.get("success"):
            await session.close()
            raise Exception(f"XUI login failed: {data.get('msg', 'Unknown error')}")
        
        logging.info("✅ 3X-UI сессия успешно открыта")
        return session
        
    except Exception as e:
        await session.close()
        logging.error(f"❌ Ошибка подключения к 3X-UI: {str(e)}")
        raise Exception(f"Ошибка подключения к панели 3X-UI: {str(e)}")


async def create_xui_client(tg_id: int, days: int) -> dict:
    """
    Создать клиента в 3X-UI
    
    Args:
        tg_id: ID пользователя Telegram
        days: Количество дней подписки
        
    Returns:
        Словарь с данными: {
            'xui_uuid': str,
            'xui_username': str,
            'subscription_url': str,
            'subscription_until': str
        }
        
    Raises:
        Exception если не удалось создать клиента
    """
    session = await get_xui_session()
    
    try:
        # Генерируем уникальные данные для клиента
        client_uuid = str(uuid.uuid4())
        client_sub_id = str(uuid.uuid4())[:16]
        client_email = f"user_{tg_id}_{datetime.now().timestamp()}"
        
        # Рассчитываем время истечения подписки (в миллисекундах)
        now_ms = int(datetime.now().timestamp() * 1000)
        expiry_time_ms = now_ms + (days * 24 * 60 * 60 * 1000)
        
        # Формируем данные клиента
        client_data = {
            "id": client_uuid,
            "flow": "",
            "email": client_email,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": expiry_time_ms,
            "enable": True,
            "tgId": str(tg_id),
            "subId": client_sub_id,
            "reset": 0
        }
        
        # Добавляем клиента в inbound
        add_client_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/addClient"
        
        payload = {
            "id": str(INBOUND_ID),
            "settings": json.dumps({"clients": [client_data]})
        }
        
        response = await session.post(add_client_url, data=payload)
        response.raise_for_status()
        
        result = await response.json()
        
        if not result.get("success"):
            raise Exception(f"Failed to add client: {result.get('msg', 'Unknown error')}")
        
        # Формируем ссылку подписки
        subscription_url = f"http://{SUB_EXTERNAL_HOST}:{SUB_PORT}/sub/{client_sub_id}"
        
        # Форматируем дату истечения подписки
        expiry_datetime = datetime.fromtimestamp(expiry_time_ms / 1000)
        subscription_until = expiry_datetime.isoformat()
        
        logging.info(f"✅ Клиент 3X-UI создан для пользователя {tg_id}")
        
        return {
            'xui_uuid': client_uuid,
            'xui_username': client_email,
            'subscription_url': subscription_url,
            'subscription_until': subscription_until
        }
        
    except Exception as e:
        logging.error(f"❌ Ошибка создания клиента 3X-UI: {str(e)}")
        raise Exception(f"Ошибка создания подписки в панели: {str(e)}")
        
    finally:
        await session.close()


async def extend_xui_subscription(xui_uuid: str, days: int) -> dict:
    """
    Продлить подписку 3X-UI клиента
    
    Args:
        xui_uuid: UUID клиента в 3X-UI
        days: Количество дней на продление
        
    Returns:
        Словарь с обновленными данными: {
            'subscription_url': str,
            'subscription_until': str
        }
        
    Raises:
        Exception если не удалось продлить подписку
    """
    session = await get_xui_session()
    
    try:
        # Рассчитываем новое время истечения (добавляем дни к текущему времени)
        now_ms = int(datetime.now().timestamp() * 1000)
        new_expiry_time_ms = now_ms + (days * 24 * 60 * 60 * 1000)
        
        # Формируем данные для обновления
        update_client_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/updateClient/{xui_uuid}"
        
        client_data = {
            "id": xui_uuid,
            "expiryTime": new_expiry_time_ms,
            "enable": True
        }
        
        payload = {
            "id": str(INBOUND_ID),
            "settings": json.dumps({"clients": [client_data]})
        }
        
        response = await session.post(update_client_url, data=payload)
        response.raise_for_status()
        
        result = await response.json()
        
        if not result.get("success"):
            raise Exception(f"Failed to extend client: {result.get('msg', 'Unknown error')}")
        
        # Форматируем дату истечения подписки
        expiry_datetime = datetime.fromtimestamp(new_expiry_time_ms / 1000)
        subscription_until = expiry_datetime.isoformat()
        
        logging.info(f"✅ Подписка 3X-UI продлена для клиента {xui_uuid}")
        
        return {
            'subscription_until': subscription_until
        }
        
    except Exception as e:
        logging.error(f"❌ Ошибка продления подписки 3X-UI: {str(e)}")
        raise Exception(f"Ошибка продления подписки в панели: {str(e)}")
        
    finally:
        await session.close()


async def get_xui_client_traffic(xui_username: str) -> dict:
    """
    Получить информацию о трафике клиента 3X-UI
    
    Args:
        xui_username: Email клиента в 3X-UI
        
    Returns:
        Словарь с информацией о трафике и дате истечения
        
    Raises:
        Exception если не удалось получить информацию
    """
    session = await get_xui_session()
    
    try:
        get_traffic_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/getClientTraffics/{xui_username}"
        
        response = await session.get(get_traffic_url)
        response.raise_for_status()
        
        result = await response.json()
        
        if not result.get("success"):
            raise Exception(f"Failed to get client traffic: {result.get('msg', 'Unknown error')}")
        
        return result.get('obj', {})
        
    except Exception as e:
        logging.error(f"❌ Ошибка получения информации о трафике 3X-UI: {str(e)}")
        raise Exception(f"Ошибка получения информации: {str(e)}")
        
    finally:
        await session.close()
