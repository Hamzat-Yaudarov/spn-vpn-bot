import aiohttp
import asyncio
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from config import REMNAWAVE_BASE_URL, REMNAWAVE_API_TOKEN, DEFAULT_SQUAD_UUID


# ⚠️ RETRY КОНФИГУРАЦИЯ
MAX_RETRIES = 3
RETRY_DELAY_BASE = 1  # секунды


async def _retry_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    **kwargs
) -> aiohttp.ClientResponse | None:
    """
    Выполнить HTTP запрос с retry логикой

    Args:
        session: aiohttp сессия
        method: GET, POST, PATCH и т.д.
        url: URL запроса
        **kwargs: Остальные параметры для session.request()

    Returns:
        Response если успешен, None если все retry'и исчерпаны
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if method.upper() == "GET":
                response = await session.get(url, **kwargs)
            elif method.upper() == "POST":
                response = await session.post(url, **kwargs)
            elif method.upper() == "PATCH":
                response = await session.patch(url, **kwargs)
            else:
                response = await session.request(method, url, **kwargs)

            # Если успешен (200-299) - возвращаем
            if 200 <= response.status < 300:
                return response

            # Если ошибка 4xx - не retry'им (это ошибка запроса)
            if 400 <= response.status < 500:
                logging.error(f"Client error {response.status}: {url}")
                return response

            # Если ошибка 5xx или другое - retry'им
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_BASE * (2 ** (attempt - 1))  # 1, 2, 4 сек
                logging.warning(f"Attempt {attempt}/{MAX_RETRIES} failed ({response.status}), retrying in {delay}s: {url}")
                await asyncio.sleep(delay)
                continue
            else:
                logging.error(f"All {MAX_RETRIES} attempts failed for {url}, last status: {response.status}")
                return response

        except asyncio.TimeoutError:
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_BASE * (2 ** (attempt - 1))
                logging.warning(f"Attempt {attempt}/{MAX_RETRIES} timeout, retrying in {delay}s: {url}")
                await asyncio.sleep(delay)
                continue
            else:
                logging.error(f"All {MAX_RETRIES} attempts timed out for {url}")
                return None
        except Exception as e:
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_BASE * (2 ** (attempt - 1))
                logging.warning(f"Attempt {attempt}/{MAX_RETRIES} error: {e}, retrying in {delay}s: {url}")
                await asyncio.sleep(delay)
                continue
            else:
                logging.error(f"All {MAX_RETRIES} attempts failed with exception for {url}: {e}")
                return None

    return None


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
    remna_username = f"tg_{tg_id}"

    url = f"{REMNAWAVE_BASE_URL}/users/by-username/{remna_username}"
    headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

    try:
        # ⚠️ Добавляем таймаут для GET запроса (максимум 10 сек)
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                user_data = data.get("response", {})
                uuid = user_data.get("uuid")
                if uuid:
                    if extend_if_exists:
                        await remnawave_extend_subscription(session, uuid, days)
                    return uuid, remna_username
    except Exception as e:
        logging.error(f"Get existing user error: {e}")

    # Создаём нового пользователя если не нашли существующего
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

    try:
        # ⚠️ Добавляем таймаут для POST запроса (максимум 15 сек)
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.post(create_url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                user_data = data.get("response", {})
                uuid = user_data.get("uuid")
                if uuid:
                    logging.info(f"Created new Remnawave user: {remna_username}")
                    return uuid, remna_username
            else:
                logging.error(f"Create user failed ({resp.status}): {await resp.text()}")
    except asyncio.TimeoutError:
        logging.error(f"Timeout creating Remnawave user: {remna_username}")
    except Exception as e:
        logging.error(f"Create user exception: {e}")

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
    try:
        timeout = aiohttp.ClientTimeout(total=10)

        # 1. Получаем текущий expireAt (с retry логикой)
        resp = await _retry_request(
            session,
            "GET",
            f"{REMNAWAVE_BASE_URL}/users/{user_uuid}",
            headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"},
            timeout=timeout
        )

        if not resp or resp.status != 200:
            logging.error(f"Failed to get user {user_uuid}")
            return False

        data = await resp.json()
        current_expire = data["response"].get("expireAt")
        if not current_expire:
            logging.error("expireAt not found in response")
            return False

        # 2. Считаем новую дату
        current_dt = datetime.fromisoformat(current_expire.replace("Z", "+00:00"))
        new_expire = current_dt + timedelta(days=days)

        payload = {
            "uuid": user_uuid,
            "expireAt": new_expire.isoformat()
        }

        # 3. PATCH /users для обновления (с retry логикой)
        resp = await _retry_request(
            session,
            "PATCH",
            f"{REMNAWAVE_BASE_URL}/users",
            headers={
                "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=timeout
        )

        if resp and resp.status == 200:
            logging.info(f"Extended subscription for {user_uuid} by {days} days")
            return True

        logging.error(f"Extend failed: response status {resp.status if resp else 'None'}")
        return False

    except asyncio.TimeoutError:
        logging.error(f"Timeout extending subscription for {user_uuid}")
        return False
    except Exception as e:
        logging.exception(f"Extend subscription exception: {e}")
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
    url = f"{REMNAWAVE_BASE_URL}/internal-squads/{squad_uuid}/bulk-actions/add-users"
    payload = {"userUuids": [user_uuid]}
    headers = {
        "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # ⚠️ Добавляем таймаут + retry логику для POST запроса
        timeout = aiohttp.ClientTimeout(total=10)
        resp = await _retry_request(
            session,
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=timeout
        )

        if resp and resp.status in (200, 201):
            logging.info(f"Added user {user_uuid} to squad {squad_uuid}")
            return True
        else:
            logging.error(f"Add to squad failed: status={resp.status if resp else 'None'}")
            return False

    except asyncio.TimeoutError:
        logging.error(f"Timeout adding user {user_uuid} to squad")
        return False
    except Exception as e:
        logging.error(f"Add to squad exception: {e}")
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
    url = f"{REMNAWAVE_BASE_URL}/users/{user_uuid}"
    headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

    try:
        # ⚠️ Добавляем таймаут (максимум 10 сек)
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                sub_url = data.get("response", {}).get("subscriptionUrl")
                if sub_url:
                    return sub_url
            else:
                logging.error(f"Get subscription URL failed ({resp.status})")
    except asyncio.TimeoutError:
        logging.error(f"Timeout getting subscription URL for {user_uuid}")
    except Exception as e:
        logging.error(f"Get subscription URL exception: {e}")

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
    url = f"{REMNAWAVE_BASE_URL}/users/{user_uuid}"
    headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

    try:
        # ⚠️ Добавляем таймаут (максимум 10 сек)
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("response", {})
    except asyncio.TimeoutError:
        logging.error(f"Timeout getting user info for {user_uuid}")
    except Exception as e:
        logging.error(f"Get user info exception: {e}")

    return None
