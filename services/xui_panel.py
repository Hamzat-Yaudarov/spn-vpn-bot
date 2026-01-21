import aiohttp
import logging
import secrets
import string
import json
import uuid as uuid_lib
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
from utils import retry_with_backoff, safe_api_call

logger = logging.getLogger(__name__)


async def get_xui_session() -> aiohttp.ClientSession | None:
    """
    Получить аутентифицированную сессию для 3X-UI панели с retry логикой

    Returns:
        aiohttp.ClientSession с установленными cookies или None если ошибка
    """
    async def _login():
        login_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH.replace('/panel', '')}/login"
        payload = {"username": XUI_USERNAME, "password": XUI_PASSWORD}

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        try:
            async with session.post(login_url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        return session
                    else:
                        await session.close()
                        raise RuntimeError(f"XUI login failed: {data}")
                else:
                    await session.close()
                    error_text = await resp.text()
                    raise RuntimeError(f"XUI HTTP {resp.status}: {error_text}")
        except Exception as e:
            await session.close()
            raise e

    try:
        session = await safe_api_call(
            _login,
            error_message="Failed to authenticate with XUI panel"
        )
        return session
    except Exception as e:
        logger.error(f"Get XUI session error: {e}")
        return None


async def xui_get_client_expiry(session: aiohttp.ClientSession, email: str) -> int | None:
    """
    Получить время истечения подписки клиента в 3X-UI

    Args:
        session: Аутентифицированная сессия
        email: Email/идентификатор клиента

    Returns:
        Unix timestamp в миллисекундах или None если ошибка
    """
    async def _get_expiry():
        get_traffic_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/getClientTraffics/{email}"

        async with session.get(get_traffic_url) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("success"):
                    return data['obj']['expiryTime']
                else:
                    raise RuntimeError(f"Get client traffic failed: {data}")
            else:
                error_text = await resp.text()
                raise RuntimeError(f"XUI HTTP {resp.status}: {error_text}")

    try:
        expiry = await safe_api_call(
            _get_expiry,
            error_message=f"Failed to get client expiry for {email}"
        )
        return expiry
    except Exception as e:
        logger.error(f"Get client expiry error: {e}")
        return None


async def xui_create_or_extend_client(
    session: aiohttp.ClientSession,
    tg_id: int,
    days: int
) -> tuple[str | None, str | None]:
    """
    Создать или продлить клиента в 3X-UI панели с retry логикой

    Args:
        session: Аутентифицированная сессия
        tg_id: ID пользователя Telegram
        days: Количество дней подписки

    Returns:
        Кортеж (UUID, email) или (None, None) если ошибка
    """
    async def _create_or_extend():
        # Используем UUID для уникальности внутри панели
        client_uuid = str(uuid_lib.uuid4())
        client_email = f"tg_{tg_id}_{secrets.token_hex(4)}"
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
                "subId": secrets.token_hex(8),
                "reset": 0
            }]
        }

        payload = {
            "id": str(INBOUND_ID),
            "settings": json.dumps(settings)
        }

        add_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/addClient"

        async with session.post(add_url, data=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("success"):
                    logger.info(f"Created XUI client for TG {tg_id}: {client_uuid}")
                    return client_uuid, client_email
                else:
                    raise RuntimeError(f"Add client failed: {data}")
            else:
                error_text = await resp.text()
                raise RuntimeError(f"XUI HTTP {resp.status}: {error_text}")

    try:
        result = await safe_api_call(
            _create_or_extend,
            error_message=f"Failed to create XUI client for TG {tg_id}"
        )
        if result:
            return result
    except Exception as e:
        logger.error(f"Create/extend XUI client error: {e}")

    return None, None


async def xui_extend_client(
    session: aiohttp.ClientSession,
    client_uuid: str,
    client_email: str,
    days: int
) -> bool:
    """
    Продлить подписку существующего клиента в 3X-UI с retry логикой

    Args:
        session: Аутентифицированная сессия
        client_uuid: UUID клиента
        client_email: Email клиента
        days: Количество дней для продления

    Returns:
        True если успешно, False иначе
    """
    async def _extend():
        add_ms = int(days * 30 * 24 * 60 * 60 * 1000)

        # Получаем текущее время истечения
        current_expiry = await xui_get_client_expiry(session, client_email)
        if not current_expiry:
            raise RuntimeError("Could not get current expiry time")

        new_expiry = current_expiry + add_ms

        settings = {
            "clients": [{
                "id": client_uuid,
                "flow": "",
                "email": client_email,
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": new_expiry,
                "enable": True,
                "reset": 0
            }]
        }

        payload = {
            "id": str(INBOUND_ID),
            "settings": json.dumps(settings)
        }

        update_url = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api/inbounds/updateClient/{client_uuid}"

        async with session.post(update_url, data=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("success"):
                    logger.info(f"Extended XUI client {client_uuid} by {days} days")
                    return True
                else:
                    raise RuntimeError(f"Update client failed: {data}")
            else:
                error_text = await resp.text()
                raise RuntimeError(f"XUI HTTP {resp.status}: {error_text}")

    try:
        result = await safe_api_call(
            _extend,
            error_message=f"Failed to extend XUI client {client_uuid}"
        )
        return result is not None
    except Exception as e:
        logger.error(f"Extend XUI client error: {e}")
        return False


async def xui_get_subscription_url(client_email: str) -> str | None:
    """
    Получить ссылку подписки для клиента из 3X-UI

    Args:
        client_email: Email клиента

    Returns:
        URL подписки или None если ошибка
    """
    try:
        sub_url = f"http://{SUB_EXTERNAL_HOST}:{SUB_PORT}/sub/{client_email}"
        logger.debug(f"Generated XUI subscription URL for {client_email}")
        return sub_url
    except Exception as e:
        logger.error(f"Get XUI subscription URL error: {e}")
        return None
