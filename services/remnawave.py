import aiohttp
import json
import logging
import secrets
import ssl
import string
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from config import (
    REMNAWAVE_BASE_URL,
    REMNAWAVE_API_TOKEN,
    DEFAULT_SQUAD_UUID,
    API_REQUEST_TIMEOUT,
    SUBSCRIPTION_PUBLIC_BASE_URL,
    REMNAWAVE_CA_BUNDLE,
)
from utils import retry_with_backoff, safe_api_call


MAX_SUBSCRIPTION_PROFILE_BYTES = 4 * 1024 * 1024


def _verified_connector() -> aiohttp.TCPConnector:
    """TLS всегда проверяется; частный CA разрешён только явным bundle."""
    context = ssl.create_default_context(cafile=REMNAWAVE_CA_BUNDLE or None)
    return aiohttp.TCPConnector(ssl=context)


def validate_public_subscription_url(sub_url: str) -> str:
    """Разрешить проксирование только на закреплённый HTTPS subscription-host."""
    configured = urlsplit(SUBSCRIPTION_PUBLIC_BASE_URL)
    candidate = urlsplit(sub_url)
    configured_port = configured.port or 443
    candidate_port = candidate.port or 443
    if (
        configured.scheme.lower() != "https"
        or candidate.scheme.lower() != "https"
        or not configured.hostname
        or candidate.hostname != configured.hostname
        or candidate_port != configured_port
        or candidate.username
        or candidate.password
        or not candidate.path.startswith("/sub/")
    ):
        raise ValueError("Subscription URL is outside the configured HTTPS host")
    return urlunsplit(("https", candidate.netloc, candidate.path, candidate.query, ""))


async def remnawave_fetch_subscription_profile(sub_url: str, device_headers: dict[str, str]) -> dict:
    """Получить профиль без раскрытия постоянного subscription URL клиенту."""
    safe_url = validate_public_subscription_url(sub_url)
    forwarded = {
        name: str(device_headers[name])[:256]
        for name in ("x-hwid", "x-device-os", "x-ver-os", "x-device-model", "user-agent")
        if device_headers.get(name)
    }
    timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as session:
        async with session.get(safe_url, headers=forwarded, allow_redirects=False) as resp:
            if resp.content_length and resp.content_length > MAX_SUBSCRIPTION_PROFILE_BYTES:
                raise RuntimeError("Subscription profile is too large")
            body = await resp.content.read(MAX_SUBSCRIPTION_PROFILE_BYTES + 1)
            if len(body) > MAX_SUBSCRIPTION_PROFILE_BYTES:
                raise RuntimeError("Subscription profile is too large")
            return {
                "status": resp.status,
                "body": body,
                "content_type": resp.headers.get("Content-Type", "text/plain; charset=utf-8"),
                "headers": {
                    key.lower(): value
                    for key, value in resp.headers.items()
                    if key.lower().startswith("x-hwid-")
                },
            }


def normalize_subscription_url(sub_url: str | None) -> str | None:
    """Показать пользователям подписочную ссылку на публичном sub-домене."""
    if not sub_url or not SUBSCRIPTION_PUBLIC_BASE_URL:
        return sub_url

    try:
        public = urlsplit(SUBSCRIPTION_PUBLIC_BASE_URL)
        original = urlsplit(sub_url)
        if not public.scheme or not public.netloc or not original.scheme or not original.netloc:
            return sub_url
        return urlunsplit((public.scheme, public.netloc, original.path, original.query, original.fragment))
    except Exception:
        return sub_url


def _build_subscription_url_from_short_uuid(short_uuid: str | None) -> str | None:
    if not short_uuid or not SUBSCRIPTION_PUBLIC_BASE_URL:
        return None
    return f"{SUBSCRIPTION_PUBLIC_BASE_URL}/sub/{short_uuid}"


def _extract_subscription_url(user_data: dict) -> str | None:
    sub_url = user_data.get("subscriptionUrl") or user_data.get("subscription_url")
    if sub_url:
        return normalize_subscription_url(sub_url)

    short_uuid = (
        user_data.get("shortUuid")
        or user_data.get("short_uuid")
        or user_data.get("subscriptionShortUuid")
        or user_data.get("subscription_short_uuid")
    )
    return _build_subscription_url_from_short_uuid(short_uuid)


async def remnawave_get_or_create_user(
    session: aiohttp.ClientSession,
    tg_id: int,
    days: int = 30,
    extend_if_exists: bool = False,
    remna_username: str | None = None,
    traffic_limit_bytes: int | None = None,
    traffic_limit_strategy: str | None = None,
    active_internal_squads: list[str] | None = None,
    hwid_device_limit: int | None = None,
    telegram_id: int | None = None,
) -> tuple[str | None, str | None]:
    """
    Получить или создать пользователя в Remnawave API с retry логикой

    Args:
        session: aiohttp сессия
        tg_id: ID пользователя Telegram
        days: Количество дней подписки для новых пользователей
        extend_if_exists: Продлить подписку если пользователь существует

    Returns:
        Кортеж (UUID пользователя, имя пользователя) или (None, None)
    """
    remna_username = remna_username or f"tg_{tg_id}"

    # Пытаемся получить существующего пользователя
    async def _get_existing_user():
        url = f"{REMNAWAVE_BASE_URL}/users/by-username/{remna_username}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
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
            if any(value is not None for value in (traffic_limit_bytes, traffic_limit_strategy, active_internal_squads, hwid_device_limit, telegram_id)):
                await remnawave_update_user_profile(
                    session,
                    uuid,
                    traffic_limit_bytes=traffic_limit_bytes,
                    traffic_limit_strategy=traffic_limit_strategy,
                    active_internal_squads=active_internal_squads,
                    hwid_device_limit=hwid_device_limit,
                    telegram_id=telegram_id,
                )
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

        if traffic_limit_bytes is not None:
            payload["trafficLimitBytes"] = traffic_limit_bytes
        if traffic_limit_strategy is not None:
            payload["trafficLimitStrategy"] = traffic_limit_strategy
        if active_internal_squads is not None:
            payload["activeInternalSquads"] = active_internal_squads
        if hwid_device_limit is not None:
            payload["hwidDeviceLimit"] = hwid_device_limit
        if telegram_id is not None:
            payload["telegramId"] = telegram_id

        headers = {
            "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
            "Content-Type": "application/json"
        }

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
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


async def remnawave_update_user_profile(
    session: aiohttp.ClientSession,
    user_uuid: str,
    *,
    expire_at: datetime | None = None,
    traffic_limit_bytes: int | None = None,
    traffic_limit_strategy: str | None = None,
    active_internal_squads: list[str] | None = None,
    hwid_device_limit: int | None = None,
    telegram_id: int | None = None,
    missing_user_is_success: bool = False,
) -> bool:
    """Обновить профиль пользователя Remnawave.

    ``missing_user_is_success`` используется только для идемпотентной очистки
    старых записей: если Remnawave точно отвечает ``404 / A025``, значит
    пользователя уже нет и удалять у него лимит больше не требуется.
    Для обычных покупок, продлений и синхронизаций значение остаётся False.
    """
    payload = {"uuid": str(user_uuid)}

    if expire_at is not None:
        payload["expireAt"] = expire_at.isoformat()
    if traffic_limit_bytes is not None:
        payload["trafficLimitBytes"] = traffic_limit_bytes
    if traffic_limit_strategy is not None:
        payload["trafficLimitStrategy"] = traffic_limit_strategy
    if active_internal_squads is not None:
        payload["activeInternalSquads"] = active_internal_squads
    if hwid_device_limit is not None:
        payload["hwidDeviceLimit"] = hwid_device_limit
    if telegram_id is not None:
        payload["telegramId"] = telegram_id

    async def _update():
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.patch(
                f"{REMNAWAVE_BASE_URL}/users",
                headers={
                    "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                    "Content-Type": "application/json"
                },
                json=payload
            ) as resp:
                if resp.status == 200:
                    return True
                error_text = await resp.text()
                if missing_user_is_success and resp.status == 404:
                    try:
                        error_data = json.loads(error_text)
                    except (TypeError, ValueError):
                        error_data = {}
                    if error_data.get("errorCode") == "A025":
                        logging.info(
                            "Remnawave user %s is already absent; profile cleanup is complete",
                            user_uuid,
                        )
                        return True
                raise RuntimeError(f"Update user failed ({resp.status}): {error_text}")

    try:
        result = await safe_api_call(_update, error_message=f"Failed to update Remnawave user {user_uuid}")
        return result is not None
    except Exception as e:
        logging.error(f"Update Remnawave user profile error: {e}")
        return False


async def remnawave_delete_user(session: aiohttp.ClientSession, user_uuid: str) -> bool:
    """Физически удалить пользователя из Remnawave."""
    async def _delete():
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.delete(
                f"{REMNAWAVE_BASE_URL}/users/{user_uuid}",
                headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"},
            ) as resp:
                if resp.status in (200, 201, 204):
                    if resp.status != 204:
                        data = {}
                        if resp.content_length != 0:
                            try:
                                data = await resp.json(content_type=None)
                            except Exception:
                                data = {}
                        response = data.get("response", data) if isinstance(data, dict) else {}
                        deleted_flag = None
                        for key in ("isDeleted", "is_deleted", "deleted"):
                            if isinstance(response, dict) and key in response:
                                deleted_flag = response[key]
                                break
                            if isinstance(data, dict) and key in data:
                                deleted_flag = data[key]
                                break
                        if deleted_flag is False:
                            raise RuntimeError(f"Delete user returned false: {data}")
                    logging.info("Deleted Remnawave user %s", user_uuid)
                    return True
                if resp.status == 404:
                    logging.info("Remnawave user %s already deleted", user_uuid)
                    return True
                error_text = await resp.text()
                raise RuntimeError(f"Delete user failed ({resp.status}): {error_text}")

    try:
        result = await safe_api_call(_delete, error_message=f"Failed to delete Remnawave user {user_uuid}")
        return bool(result)
    except Exception as e:
        logging.error(f"Delete Remnawave user error: {e}")
        return False


async def remnawave_reset_user_traffic(session: aiohttp.ClientSession, user_uuid: str) -> bool:
    """Сбросить трафик пользователя в Remnawave."""
    async def _reset():
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.post(
                f"{REMNAWAVE_BASE_URL}/users/{user_uuid}/actions/reset-traffic",
                headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
            ) as resp:
                if resp.status == 200:
                    return True
                error_text = await resp.text()
                raise RuntimeError(f"Reset traffic failed ({resp.status}): {error_text}")

    try:
        result = await safe_api_call(_reset, error_message=f"Failed to reset traffic for {user_uuid}")
        return result is not None
    except Exception as e:
        logging.error(f"Reset traffic error: {e}")
        return False


async def remnawave_revoke_subscription(session: aiohttp.ClientSession, user_uuid: str) -> bool:
    """Перевыпустить подписочную ссылку пользователя в Remnawave."""
    async def _revoke():
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        endpoints = [
            f"{REMNAWAVE_BASE_URL}/users/{user_uuid}/actions/revoke-subscription",
            f"{REMNAWAVE_BASE_URL}/users/{user_uuid}/actions/reset-subscription",
            f"{REMNAWAVE_BASE_URL}/users/{user_uuid}/actions/revoke-subscription-url",
        ]
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            errors = []
            for endpoint in endpoints:
                async with temp_session.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
                ) as resp:
                    if resp.status in (200, 201, 204):
                        return True
                    error_text = await resp.text()
                    errors.append(f"{endpoint} -> {resp.status}: {error_text[:300]}")
                    if resp.status not in (404, 405):
                        break
            raise RuntimeError("Revoke subscription failed: " + " | ".join(errors))

    try:
        result = await safe_api_call(_revoke, error_message=f"Failed to revoke subscription for {user_uuid}")
        return result is not None
    except Exception as e:
        logging.error(f"Revoke subscription error: {e}")
        return False


async def remnawave_get_user_usage(session: aiohttp.ClientSession, user_uuid: str) -> dict | None:
    """Получить traffic usage пользователя."""
    user_info = await remnawave_get_user_info(session, user_uuid)
    if not user_info:
        return None
    return user_info.get("userTraffic") or {}


async def remnawave_get_hwid_devices(session: aiohttp.ClientSession, user_uuid: str) -> list[dict] | None:
    """Получить список HWID-устройств пользователя."""
    async def _get_devices():
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.get(
                f"{REMNAWAVE_BASE_URL}/hwid/devices/{user_uuid}",
                headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response = data.get("response", {})
                    return response.get("devices") or []
                if resp.status == 404:
                    return []
                error_text = await resp.text()
                raise RuntimeError(f"Get HWID devices failed ({resp.status}): {error_text}")

    return await safe_api_call(
        _get_devices,
        error_message=f"Failed to get HWID devices for {user_uuid}"
    )


async def remnawave_delete_hwid_device(session: aiohttp.ClientSession, user_uuid: str, hwid: str) -> bool:
    """Удалить одно HWID-устройство пользователя."""
    async def _delete_device():
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.post(
                f"{REMNAWAVE_BASE_URL}/hwid/devices/delete",
                headers={
                    "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"userUuid": str(user_uuid), "hwid": hwid},
            ) as resp:
                if resp.status == 200:
                    return True
                error_text = await resp.text()
                raise RuntimeError(f"Delete HWID device failed ({resp.status}): {error_text}")

    try:
        result = await safe_api_call(
            _delete_device,
            error_message=f"Failed to delete HWID device for {user_uuid}"
        )
        return result is not None
    except Exception as e:
        logging.error(f"Delete HWID device error: {e}")
        return False


async def remnawave_delete_all_hwid_devices(session: aiohttp.ClientSession, user_uuid: str) -> bool:
    """Удалить все HWID-устройства пользователя."""
    async def _delete_all_devices():
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.post(
                f"{REMNAWAVE_BASE_URL}/hwid/devices/delete-all",
                headers={
                    "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"userUuid": str(user_uuid)},
            ) as resp:
                if resp.status == 200:
                    return True
                error_text = await resp.text()
                raise RuntimeError(f"Delete all HWID devices failed ({resp.status}): {error_text}")

    try:
        result = await safe_api_call(
            _delete_all_devices,
            error_message=f"Failed to delete all HWID devices for {user_uuid}"
        )
        return result is not None
    except Exception as e:
        logging.error(f"Delete all HWID devices error: {e}")
        return False


async def remnawave_set_subscription_expiry(
    session: aiohttp.ClientSession,
    user_uuid: str,
    expire_at: datetime
) -> bool:
    """
    Установить точную дату окончания подписки в Remnawave с retry логикой

    Args:
        session: aiohttp сессия
        user_uuid: UUID пользователя в Remnawave
        expire_at: Дата и время окончания подписки (datetime object)

    Returns:
        True если успешно, False иначе
    """
    async def _set_expiry():
        # Конвертируем datetime в ISO формат
        expire_iso = expire_at.isoformat() if not expire_at.isoformat().endswith('Z') else expire_at.isoformat()

        payload = {
            "uuid": str(user_uuid),
            "expireAt": expire_iso
        }

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.patch(
                f"{REMNAWAVE_BASE_URL}/users",
                headers={
                    "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                    "Content-Type": "application/json"
                },
                json=payload
            ) as resp:
                if resp.status == 200:
                    logging.info(f"Set subscription expiry for {user_uuid} to {expire_iso}")
                    return True
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Set expiry failed ({resp.status}): {error_text}")

    try:
        result = await safe_api_call(
            _set_expiry,
            error_message=f"Failed to set subscription expiry for {user_uuid}"
        )
        return result is not None
    except Exception as e:
        logging.error(f"Set subscription expiry error: {e}")
        return False


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
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
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
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
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
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
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
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user_data = data.get("response", {})
                    sub_url = _extract_subscription_url(user_data)
                    if sub_url:
                        return sub_url
                    else:
                        available_keys = ", ".join(sorted(user_data.keys()))
                        raise RuntimeError(f"subscriptionUrl not found in response. Keys: {available_keys}")
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
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
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
