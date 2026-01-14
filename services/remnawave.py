import aiohttp
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from config import REMNAWAVE_BASE_URL, REMNAWAVE_API_TOKEN, DEFAULT_SQUAD_UUID


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
        async with session.get(url, headers=headers) as resp:
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
        async with session.post(create_url, headers=headers, json=payload) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                user_data = data.get("response", {})
                uuid = user_data.get("uuid")
                if uuid:
                    logging.info(f"Created new Remnawave user: {remna_username}")
                    return uuid, remna_username
            else:
                logging.error(f"Create user failed ({resp.status}): {await resp.text()}")
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
        # 1. Получаем текущий expireAt
        async with session.get(
            f"{REMNAWAVE_BASE_URL}/users/{user_uuid}",
            headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
        ) as resp:
            if resp.status != 200:
                logging.error(f"Get user failed ({resp.status}): {await resp.text()}")
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

        # 3. PATCH /users для обновления
        async with session.patch(
            f"{REMNAWAVE_BASE_URL}/users",
            headers={
                "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                "Content-Type": "application/json"
            },
            json=payload
        ) as resp:
            if resp.status == 200:
                logging.info(f"Extended subscription for {user_uuid} by {days} days")
                return True

            logging.error(f"Extend failed ({resp.status}): {await resp.text()}")
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
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status in (200, 201):
                logging.info(f"Added user {user_uuid} to squad {squad_uuid}")
                return True
            else:
                logging.error(f"Add to squad failed: {resp.status} → {await resp.text()}")
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
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                sub_url = data.get("response", {}).get("subscriptionUrl")
                if sub_url:
                    return sub_url
            else:
                logging.error(f"Get subscription URL failed ({resp.status})")
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
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("response", {})
    except Exception as e:
        logging.error(f"Get user info exception: {e}")

    return None
