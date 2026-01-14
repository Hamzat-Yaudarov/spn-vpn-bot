import aiohttp
import asyncio
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from config import REMNAWAVE_BASE_URL, REMNAWAVE_API_TOKEN, DEFAULT_SQUAD_UUID


# ────────────────────────────────────────────────
#                RETRY CONFIG
# ────────────────────────────────────────────────

REMNAWAVE_MAX_RETRIES = 3
REMNAWAVE_RETRY_DELAY = 1.0  # секунды между попытками


async def _remnawave_request_with_retry(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    headers: dict,
    json_payload: dict | None = None,
    max_retries: int = REMNAWAVE_MAX_RETRIES,
    retry_delay: float = REMNAWAVE_RETRY_DELAY
) -> tuple[int, dict | str | None]:
    """
    Выполнить запрос к Remnawave API с автоматическими повторами при ошибках
    
    Args:
        session: aiohttp ClientSession
        method: HTTP метод (GET, POST, PATCH)
        url: URL для запроса
        headers: HTTP заголовки
        json_payload: JSON payload для POST/PATCH
        max_retries: Максимальное количество попыток
        retry_delay: Задержка между попытками в секундах
        
    Returns:
        Кортеж (status_code, response_data)
    """
    for attempt in range(max_retries):
        try:
            if method.upper() == "GET":
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    status = resp.status
                    try:
                        data = await resp.json()
                    except:
                        data = await resp.text()
                    return status, data
                    
            elif method.upper() == "POST":
                async with session.post(url, headers=headers, json=json_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    status = resp.status
                    try:
                        data = await resp.json()
                    except:
                        data = await resp.text()
                    return status, data
                    
            elif method.upper() == "PATCH":
                async with session.patch(url, headers=headers, json=json_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    status = resp.status
                    try:
                        data = await resp.json()
                    except:
                        data = await resp.text()
                    return status, data
                    
        except asyncio.TimeoutError:
            logging.warning(f"Remnawave request timeout (attempt {attempt + 1}/{max_retries}): {method} {url}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            continue
            
        except Exception as e:
            logging.warning(f"Remnawave request error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            continue
    
    logging.error(f"Remnawave request failed after {max_retries} retries: {method} {url}")
    return None, None


async def remnawave_get_or_create_user(
    session: aiohttp.ClientSession,
    tg_id: int,
    days: int = 30,
    extend_if_exists: bool = False
) -> tuple[str | None, str | None]:
    """
    Получить или создать пользователя в Remnawave API
    
    Args:
        session: aiohttp сессия
        tg_id: ID пользователя Telegram
        days: Количество дней подписки для новых пользователей
        extend_if_exists: Продлить подписку если пользователь существует
        
    Returns:
        Кортеж (UUID пользователя, имя пользователя) или (None, None)
    """
    if not isinstance(tg_id, int) or tg_id <= 0:
        logging.error(f"Invalid tg_id: {tg_id}")
        return None, None
    
    if not isinstance(days, int) or days <= 0 or days > 3650:
        logging.error(f"Invalid days: {days}")
        return None, None
    
    remna_username = f"tg_{tg_id}"
    url = f"{REMNAWAVE_BASE_URL}/users/by-username/{remna_username}"
    headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

    status, data = await _remnawave_request_with_retry(session, "GET", url, headers)
    
    if status == 200 and isinstance(data, dict):
        user_data = data.get("response", {})
        uuid = user_data.get("uuid")
        
        if uuid:
            logging.info(f"Found existing Remnawave user: {remna_username} (UUID: {uuid})")
            
            if extend_if_exists:
                success = await remnawave_extend_subscription(session, uuid, days)
                if success:
                    return uuid, remna_username
                else:
                    logging.error(f"Failed to extend subscription for existing user {uuid}")
                    return uuid, remna_username  # Возвращаем пользователя даже если расширение не удалось
            
            return uuid, remna_username

    # Пользователь не найден - создаём нового
    create_url = f"{REMNAWAVE_BASE_URL}/users"
    alphabet = string.ascii_letters + string.digits
    password = (
        secrets.choice(string.ascii_uppercase) +
        secrets.choice(string.ascii_lowercase) +
        secrets.choice(string.digits) +
        ''.join(secrets.choice(alphabet) for _ in range(21))
    )

    expire_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    payload = {
        "username": remna_username,
        "password": password,
        "expireAt": expire_at
    }

    headers = {
        "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
        "Content-Type": "application/json"
    }

    status, data = await _remnawave_request_with_retry(session, "POST", create_url, headers, payload)
    
    if status in (200, 201) and isinstance(data, dict):
        user_data = data.get("response", {})
        uuid = user_data.get("uuid")
        
        if uuid:
            logging.info(f"Created new Remnawave user: {remna_username} (UUID: {uuid})")
            return uuid, remna_username
        else:
            logging.error(f"Create user response missing UUID: {data}")
    else:
        logging.error(f"Create user failed ({status}): {data}")

    return None, None


async def remnawave_extend_subscription(
    session: aiohttp.ClientSession,
    user_uuid: str,
    days: int
) -> bool:
    """
    Продлить подписку пользователя в Remnawave
    
    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave
        days: Количество дней для продления
        
    Returns:
        True если успешно, False иначе
    """
    if not user_uuid or not isinstance(days, int) or days <= 0 or days > 3650:
        logging.error(f"Invalid parameters for extend_subscription: uuid={user_uuid}, days={days}")
        return False
    
    try:
        # 1. Получаем текущий expireAt
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
        url = f"{REMNAWAVE_BASE_URL}/users/{user_uuid}"
        
        status, data = await _remnawave_request_with_retry(session, "GET", url, headers)
        
        if status != 200 or not isinstance(data, dict):
            logging.error(f"Get user failed ({status}): {data}")
            return False

        current_expire = data.get("response", {}).get("expireAt")
        if not current_expire:
            logging.error(f"expireAt not found in response: {data}")
            return False

        # 2. Считаем новую дату
        try:
            current_dt = datetime.fromisoformat(current_expire.replace("Z", "+00:00"))
        except ValueError as e:
            logging.error(f"Failed to parse expireAt date '{current_expire}': {e}")
            return False
        
        new_expire = current_dt + timedelta(days=days)

        payload = {
            "uuid": user_uuid,
            "expireAt": new_expire.isoformat()
        }

        # 3. PATCH /users для обновления
        headers["Content-Type"] = "application/json"
        
        status, data = await _remnawave_request_with_retry(session, "PATCH", url, headers, payload)
        
        if status == 200:
            logging.info(f"Extended subscription for {user_uuid} by {days} days")
            return True
        else:
            logging.error(f"Extend subscription failed ({status}): {data}")
            return False

    except Exception as e:
        logging.error(f"Extend subscription exception: {e}", exc_info=True)
        return False


async def remnawave_add_to_squad(
    session: aiohttp.ClientSession,
    user_uuid: str,
    squad_uuid: str = DEFAULT_SQUAD_UUID
) -> bool:
    """
    Добавить пользователя в сквад
    
    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave
        squad_uuid: UUID сквада для добавления
        
    Returns:
        True если успешно, False иначе
    """
    if not user_uuid or not squad_uuid:
        logging.error(f"Invalid parameters: user_uuid={user_uuid}, squad_uuid={squad_uuid}")
        return False
    
    url = f"{REMNAWAVE_BASE_URL}/internal-squads/{squad_uuid}/bulk-actions/add-users"
    payload = {"userUuids": [user_uuid]}
    headers = {
        "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
        "Content-Type": "application/json"
    }

    status, data = await _remnawave_request_with_retry(session, "POST", url, headers, payload)
    
    if status in (200, 201):
        logging.info(f"Added user {user_uuid} to squad {squad_uuid}")
        return True
    else:
        logging.error(f"Add to squad failed ({status}): {data}")
        return False


async def remnawave_get_subscription_url(
    session: aiohttp.ClientSession,
    user_uuid: str
) -> str | None:
    """
    Получить ссылку подписки пользователя
    
    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave
        
    Returns:
        Ссылка подписки или None
    """
    if not user_uuid:
        logging.error("user_uuid is empty")
        return None
    
    url = f"{REMNAWAVE_BASE_URL}/users/{user_uuid}"
    headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

    status, data = await _remnawave_request_with_retry(session, "GET", url, headers)
    
    if status == 200 and isinstance(data, dict):
        sub_url = data.get("response", {}).get("subscriptionUrl")
        if sub_url:
            logging.info(f"Got subscription URL for {user_uuid}")
            return sub_url
        else:
            logging.warning(f"subscriptionUrl not found in response: {data}")
    else:
        logging.error(f"Get subscription URL failed ({status}): {data}")

    return None


async def remnawave_get_user_info(
    session: aiohttp.ClientSession,
    user_uuid: str
) -> dict | None:
    """
    Получить информацию о пользователе из Remnawave
    
    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave
        
    Returns:
        Словарь с информацией пользователя или None
    """
    if not user_uuid:
        logging.error("user_uuid is empty")
        return None
    
    url = f"{REMNAWAVE_BASE_URL}/users/{user_uuid}"
    headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

    status, data = await _remnawave_request_with_retry(session, "GET", url, headers)
    
    if status == 200 and isinstance(data, dict):
        user_info = data.get("response", {})
        logging.info(f"Got user info for {user_uuid}")
        return user_info
    else:
        logging.error(f"Get user info failed ({status}): {data}")

    return None
