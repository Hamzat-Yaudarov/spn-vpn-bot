import aiohttp
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from config import (
    REMNAWAVE_BASE_URL,
    REMNAWAVE_API_TOKEN,
    DEFAULT_SQUAD_UUID,
    API_REQUEST_TIMEOUT
)
from utils import retry_with_backoff, safe_api_call


async def remnawave_get_or_create_user(
    session: aiohttp.ClientSession,
    tg_id: int,
    days: int = 30,
    extend_if_exists: bool = False,
    sub_type: str = "regular"
) -> tuple[str | None, str | None]:
    """
    Получить или создать пользователя в Remnawave API с retry логикой

    Args:
        session: aiohttp сессия
        tg_id: ID пользователя Telegram
        days: Количество дней подписки для новых пользователей
        extend_if_exists: Продлить подписку если пользователь существует
        sub_type: Тип подписки ('regular' или 'vip')

    Returns:
        Кортеж (UUID пользователя, имя пользователя) или (None, None)
    """
    # Используем разные префиксы для разных типов подписок
    suffix = "_vip" if sub_type == "vip" else ""
    remna_username = f"tg_{tg_id}{suffix}"

    # Пытаемся получить существующего пользователя
    async def _get_existing_user():
        url = f"{REMNAWAVE_BASE_URL}/users/by-username/{remna_username}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as temp_session:
            async with temp_session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user_data = data.get("response", {})
                    uuid = user_data.get("uuid")
                    if uuid:
                        return uuid
                elif resp.status == 404:
                    return None  # Пользователь не существует
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Remnawave HTTP {resp.status}: {error_text}")

    try:
        uuid = await retry_with_backoff(_get_existing_user, max_attempts=2)
        if uuid:
            if extend_if_exists:
                await remnawave_extend_subscription(session, uuid, days)
            return uuid, remna_username
    except Exception as e:
        logging.warning(f"Get existing user error: {e}")

    # Создаём нового пользователя если не нашли существующего
    async def _create_user():
        create_url = f"{REMNAWAVE_BASE_URL}/users"
        alphabet = string.ascii_letters + string.digits
        password = (
            secrets.choice(string.ascii_uppercase) +
            secrets.choice(string.ascii_lowercase) +
            secrets.choice(string.digits) +
            ''.join(secrets.choice(alphabet) for _ in range(21))
        )

        expire_at = (datetime.utcnow() + timedelta(days=days)).isoformat()

        payload = {
            "username": remna_username,
            "password": password,
            "expireAt": expire_at
        }

        headers = {
            "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
            "Content-Type": "application/json"
        }

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as temp_session:
            async with temp_session.post(create_url, headers=headers, json=payload) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    user_data = data.get("response", {})
                    uuid = user_data.get("uuid")
                    if uuid:
                        logging.info(f"Created new Remnawave user: {remna_username}")
                        return uuid
                    else:
                        raise RuntimeError("No UUID in response")
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Remnawave HTTP {resp.status}: {error_text}")

    try:
        uuid = await safe_api_call(
            _create_user,
            error_message=f"Failed to create Remnawave user {remna_username}"
        )
        if uuid:
            return uuid, remna_username
    except Exception as e:
        logging.error(f"Create user error: {e}")

    return None, None


async def remnawave_extend_subscription(
    session: aiohttp.ClientSession,
    user_uuid: str,
    days: int
) -> bool:
    """
    Продлить подписку пользователя в Remnawave с retry логикой

    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave
        days: Количество дней для продления

    Returns:
        True если успешно, False иначе
    """
    async def _extend():
        # 1. Получаем текущий expireAt
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as temp_session:
            async with temp_session.get(
                f"{REMNAWAVE_BASE_URL}/users/{user_uuid}",
                headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"Get user failed ({resp.status}): {error_text}")

                data = await resp.json()
                current_expire = data["response"].get("expireAt")
                if not current_expire:
                    raise RuntimeError("expireAt not found in response")

        # 2. Считаем новую дату
        current_dt = datetime.fromisoformat(current_expire.replace("Z", "+00:00"))
        new_expire = current_dt + timedelta(days=days)

        payload = {
            "uuid": user_uuid,
            "expireAt": new_expire.isoformat()
        }

        # 3. PATCH /users для обновления
        async with aiohttp.ClientSession(timeout=timeout) as temp_session:
            async with temp_session.patch(
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
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Extend failed ({resp.status}): {error_text}")

    try:
        result = await safe_api_call(
            _extend,
            error_message=f"Failed to extend subscription for {user_uuid}"
        )
        return result is not None
    except Exception as e:
        logging.error(f"Extend subscription error: {e}")
        return False


async def remnawave_add_to_squad(
    session: aiohttp.ClientSession,
    user_uuid: str,
    squad_uuid: str = DEFAULT_SQUAD_UUID
) -> bool:
    """
    Добавить пользователя в сквад с retry логикой

    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave
        squad_uuid: UUID сквада для добавления

    Returns:
        True если успешно, False иначе
    """
    async def _add_to_squad():
        url = f"{REMNAWAVE_BASE_URL}/internal-squads/{squad_uuid}/bulk-actions/add-users"
        payload = {"userUuids": [user_uuid]}
        headers = {
            "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
            "Content-Type": "application/json"
        }

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as temp_session:
            async with temp_session.post(url, headers=headers, json=payload) as resp:
                if resp.status in (200, 201):
                    logging.info(f"Added user {user_uuid} to squad {squad_uuid}")
                    return True
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Add to squad failed: {resp.status} → {error_text}")

    try:
        result = await safe_api_call(
            _add_to_squad,
            error_message=f"Failed to add user {user_uuid} to squad {squad_uuid}"
        )
        return result is not None
    except Exception as e:
        logging.error(f"Add to squad error: {e}")

    return False


async def remnawave_get_subscription_url(
    session: aiohttp.ClientSession,
    user_uuid: str
) -> str | None:
    """
    Получить ссылку подписки пользователя с retry логикой

    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave

    Returns:
        Ссылка подписки или None
    """
    async def _get_url():
        url = f"{REMNAWAVE_BASE_URL}/users/{user_uuid}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as temp_session:
            async with temp_session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sub_url = data.get("response", {}).get("subscriptionUrl")
                    if sub_url:
                        return sub_url
                    else:
                        raise RuntimeError("subscriptionUrl not found in response")
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Get subscription URL failed ({resp.status}): {error_text}")

    return await safe_api_call(
        _get_url,
        error_message=f"Failed to get subscription URL for {user_uuid}"
    )


async def remnawave_get_user_info(
    session: aiohttp.ClientSession,
    user_uuid: str
) -> dict | None:
    """
    Получить информацию о пользователе из Remnawave с retry логикой

    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave

    Returns:
        Словарь с информацией пользователя или None
    """
    async def _get_info():
        url = f"{REMNAWAVE_BASE_URL}/users/{user_uuid}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as temp_session:
            async with temp_session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("response", {})
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Get user info failed ({resp.status}): {error_text}")

    return await safe_api_call(
        _get_info,
        error_message=f"Failed to get user info for {user_uuid}"
    )


# ════════════════════════════════════════════════════════════════════════════════════
# VIP Subscription Helper Functions (для удобства работы с VIP подписками)
# ════════════════════════════════════════════════════════════════════════════════════

async def remnawave_get_or_create_vip_user(
    session: aiohttp.ClientSession,
    tg_id: int,
    days: int = 30,
    extend_if_exists: bool = False
) -> tuple[str | None, str | None]:
    """
    Получить или создать VIP пользователя в Remnawave (Обход глушилок)

    Args:
        session: aiohttp сессия
        tg_id: ID пользователя Telegram
        days: Количество дней VIP подписки
        extend_if_exists: Продлить VIP подписку если пользователь существует

    Returns:
        Кортеж (UUID пользователя, имя пользователя) или (None, None)
    """
    return await remnawave_get_or_create_user(session, tg_id, days, extend_if_exists, sub_type="vip")


async def remnawave_extend_vip_subscription(
    session: aiohttp.ClientSession,
    user_uuid: str,
    days: int
) -> bool:
    """
    Продлить VIP подписку пользователя

    Args:
        session: aiohttp сессия
        user_uuid: UUID VIP пользователя в Remnawave
        days: Количество дней для продления

    Returns:
        True если успешно, False иначе
    """
    return await remnawave_extend_subscription(session, user_uuid, days)
