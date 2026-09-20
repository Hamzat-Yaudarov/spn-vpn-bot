import aiohttp
import hmac
import json
import logging
import re
import secrets
import ssl
import string
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit, urlunsplit
from config import (
    REMNAWAVE_BASE_URL,
    REMNAWAVE_API_TOKEN,
    DEFAULT_SQUAD_UUID,
    API_REQUEST_TIMEOUT,
    SUBSCRIPTION_PUBLIC_BASE_URL,
    REMNAWAVE_CA_BUNDLE,
)
from utils import retry_with_backoff, safe_api_call
from services import remnawave_identity as identity


MAX_SUBSCRIPTION_PROFILE_BYTES = 4 * 1024 * 1024
SUBSCRIPTION_SHORT_UUID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _verified_connector() -> aiohttp.TCPConnector:
    """TLS всегда проверяется; частный CA разрешён только явным bundle."""
    context = ssl.create_default_context(cafile=REMNAWAVE_CA_BUNDLE or None)
    return aiohttp.TCPConnector(ssl=context)


def extract_public_subscription_short_uuid(sub_url: str) -> str:
    """Извлечь short UUID только из URL закреплённого HTTPS subscription-host."""
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
        or candidate.query
        or candidate.fragment
    ):
        raise ValueError("Subscription URL is outside the configured HTTPS host")

    path_parts = [part for part in candidate.path.split("/") if part]
    if len(path_parts) == 1:
        short_uuid = path_parts[0]
    elif len(path_parts) == 2 and path_parts[0] == "sub":
        short_uuid = path_parts[1]
    else:
        raise ValueError("Subscription URL path is invalid")
    if not SUBSCRIPTION_SHORT_UUID_RE.fullmatch(short_uuid):
        raise ValueError("Subscription short UUID is invalid")
    return short_uuid


def validate_public_subscription_url(sub_url: str) -> str:
    """Разрешить проксирование только на закреплённый HTTPS subscription-host."""
    extract_public_subscription_short_uuid(sub_url)
    candidate = urlsplit(sub_url)
    return urlunsplit(("https", candidate.netloc, candidate.path, "", ""))


async def remnawave_resolve_user_uuid_by_short_uuid(short_uuid: str) -> str | None:
    """Проверить subscription short UUID через административный API без его логирования."""
    short_uuid = (short_uuid or "").strip()
    if not SUBSCRIPTION_SHORT_UUID_RE.fullmatch(short_uuid):
        return None
    url = f"{REMNAWAVE_BASE_URL.rstrip('/')}/users/by-short-uuid/{quote(short_uuid, safe='')}"
    headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    raise RuntimeError("Remnawave rejected subscription credential validation")
                data = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise RuntimeError("Remnawave subscription credential validation failed") from exc
    user = data.get("response") if isinstance(data, dict) else None
    if not isinstance(user, dict) or not hmac.compare_digest(str(user.get("shortUuid") or ""), short_uuid):
        return None
    if identity.REMNAWAVE_API_VERSION == 3:
        return await identity.remember_remote_user(user)
    try:
        return str(uuid.UUID(str(user.get("uuid") or "")))
    except (TypeError, ValueError):
        return None


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


async def _lookup_username_in_complete_list(session, username: str, headers: dict):
    """Read-only fallback for proxies that strip the A025 error from a 404.

    An arbitrary 404 is NOT permission to create a user. Only a complete,
    validated API listing can establish absence when the lookup route fails.
    """
    start, expected_total = 0, None
    seen_ids, seen_names = set(), set()
    while True:
        async with session.get(
            f"{REMNAWAVE_BASE_URL}/users", headers=headers, allow_redirects=False,
            params={"start": start, "size": 500,
                    "sorting": json.dumps([{"id": "username", "desc": False}])},
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Username fallback listing failed (HTTP {resp.status})")
            payload = await resp.json()
        data = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("Invalid username fallback response")
        users, total = data.get("users"), data.get("total")
        if (not isinstance(users, list) or type(total) is not int or total < 0
                or expected_total is not None and expected_total != total):
            raise RuntimeError("Incomplete or changing username fallback listing")
        expected_total = total
        for user in users:
            if not isinstance(user, dict) or not isinstance(user.get("username"), str) or not user["username"]:
                raise RuntimeError("Invalid user in username fallback listing")
            user_id = identity.numeric_user_id(user)
            if user_id in seen_ids or user["username"] in seen_names:
                raise RuntimeError("Repeated user in username fallback listing")
            seen_ids.add(user_id)
            seen_names.add(user["username"])
            if user["username"] == username:
                return await identity.remember_remote_user(user)
        start += len(users)
        if start == total:
            return None
        if not users or start > total:
            raise RuntimeError("Incomplete username fallback listing")


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
        url = f"{REMNAWAVE_BASE_URL}/users/by-username/{quote(remna_username, safe='')}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.get(url, headers=headers, allow_redirects=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user_data = data.get("response", {})
                    if user_data.get("username") != remna_username:
                        raise identity.IdentityError("Username lookup returned a different user")
                    return await identity.remember_remote_user(user_data)
                elif resp.status == 404:
                    try:
                        error = await resp.json()
                    except (ValueError, aiohttp.ContentTypeError):
                        error = {}
                    if isinstance(error, dict) and error.get("errorCode") == "A025":
                        return None
                    if identity.REMNAWAVE_API_VERSION != 3:
                        raise RuntimeError("User lookup endpoint unavailable (HTTP 404 without A025)")
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Remnawave HTTP {resp.status}: {error_text}")
            logging.warning("Username lookup returned 404 without A025; verifying via read-only API listing")
            return await _lookup_username_in_complete_list(temp_session, remna_username, headers)

    try:
        uuid = await retry_with_backoff(_get_existing_user, max_attempts=2)
        if uuid:
            if extend_if_exists:
                if not await remnawave_extend_subscription(session, uuid, days):
                    return None, None
            if any(value is not None for value in (traffic_limit_bytes, traffic_limit_strategy, active_internal_squads, hwid_device_limit, telegram_id)):
                profile_updated = await remnawave_update_user_profile(
                    session,
                    uuid,
                    traffic_limit_bytes=traffic_limit_bytes,
                    traffic_limit_strategy=traffic_limit_strategy,
                    active_internal_squads=active_internal_squads,
                    hwid_device_limit=hwid_device_limit,
                    telegram_id=telegram_id,
                )
                if not profile_updated:
                    return None, None
            return uuid, remna_username
    except Exception as e:
        logging.warning(f"Get existing user error: {e}")
        # A timeout, incompatible response or missing mapping is NOT proof that
        # the user does not exist. Never create another subscription in this case.
        return None, None

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
            "expireAt": expire_at
        }
        if identity.REMNAWAVE_API_VERSION == 2:
            payload["password"] = password

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
                    uuid = await identity.remember_remote_user(user_data)
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
    payload = {}

    if expire_at is not None:
        payload.update(identity.expiry_fields(expire_at))
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
        user_id = await identity.api_user_id(user_uuid)
        request_payload = {**payload, "id" if identity.REMNAWAVE_API_VERSION == 3 else "uuid": user_id}
        reactivate = "expireAt" in payload and await identity.should_reactivate(user_uuid)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            if reactivate:
                async with temp_session.get(
                    f"{REMNAWAVE_BASE_URL}/users/{user_id}",
                    headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"},
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Could not verify disabled profile ({resp.status})")
                    data = (await resp.json()).get("response", {})
                    if identity.numeric_user_id(data) != user_id:
                        raise identity.IdentityError("Remnawave returned a different user")
                    if data.get("status") == "DISABLED":
                        request_payload["status"] = "ACTIVE"
            if identity.REMNAWAVE_API_VERSION == 3 and payload.get("status") == "DISABLED":
                # In 3.4.1 PATCH status=DISABLED only disables ACTIVE users.
                # The dedicated action also disables EXPIRED/LIMITED profiles.
                # Disable BEFORE modifying limits, so raising a limit cannot
                # accidentally reactivate an already exhausted/refunded user.
                async with temp_session.post(
                    f"{REMNAWAVE_BASE_URL}/users/{user_id}/actions/disable",
                    headers={"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"},
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        try:
                            error = json.loads(error_text)
                        except (TypeError, ValueError):
                            error = {}
                        if not (resp.status == 400 and isinstance(error, dict) and error.get("errorCode") == "A029"):
                            raise RuntimeError(f"Disable user failed ({resp.status})")
                await identity.remember_expiry(user_uuid, expire_at)
                if len(payload) == 1:
                    return True
            async with temp_session.patch(
                f"{REMNAWAVE_BASE_URL}/users",
                headers={
                    "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                    "Content-Type": "application/json"
                },
                json=request_payload
            ) as resp:
                if resp.status == 200:
                    if expire_at is not None:
                        await identity.remember_expiry(user_uuid, expire_at)
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
        user_id = await identity.api_user_id(user_uuid)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.delete(
                f"{REMNAWAVE_BASE_URL}/users/{user_id}",
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
                    data = await resp.json()
                    if data.get("errorCode") == "A025":
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
        user_id = await identity.api_user_id(user_uuid)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.post(
                f"{REMNAWAVE_BASE_URL}/users/{user_id}/actions/reset-traffic",
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
        user_id = await identity.api_user_id(user_uuid)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        endpoints = [
            f"{REMNAWAVE_BASE_URL}/users/{user_id}/actions/revoke",
            f"{REMNAWAVE_BASE_URL}/users/{user_uuid}/actions/revoke-subscription",
            f"{REMNAWAVE_BASE_URL}/users/{user_uuid}/actions/reset-subscription",
            f"{REMNAWAVE_BASE_URL}/users/{user_uuid}/actions/revoke-subscription-url",
        ]
        if identity.REMNAWAVE_API_VERSION == 3:
            endpoints = endpoints[:1]
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
        user_id = await identity.api_user_id(user_uuid)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.get(
                f"{REMNAWAVE_BASE_URL}/hwid/devices/{user_id}",
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
        user_id = await identity.api_user_id(user_uuid)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.post(
                f"{REMNAWAVE_BASE_URL}/hwid/devices/delete",
                headers={
                    "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"userId" if identity.REMNAWAVE_API_VERSION == 3 else "userUuid": user_id, "hwid": hwid},
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
        user_id = await identity.api_user_id(user_uuid)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.post(
                f"{REMNAWAVE_BASE_URL}/hwid/devices/delete-all",
                headers={
                    "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"userId" if identity.REMNAWAVE_API_VERSION == 3 else "userUuid": user_id},
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
    return await remnawave_update_user_profile(session, user_uuid, expire_at=expire_at)


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
    try:
        user_info = await remnawave_get_user_info(session, user_uuid)
        if not user_info or not user_info.get("expireAt"):
            return False
        current_dt = datetime.fromisoformat(user_info["expireAt"].replace("Z", "+00:00"))
        if current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=timezone.utc)
        # Calculate once; retrying the PATCH must not add another set of days.
        new_expire = max(current_dt, datetime.now(timezone.utc)) + timedelta(days=days)
        return await remnawave_set_subscription_expiry(session, user_uuid, new_expire)
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
    if identity.REMNAWAVE_API_VERSION == 2:
        # v2 has no add-many-users endpoint. Update only this user's squads;
        # the similarly named add-users action would affect EVERY panel user.
        user_info = await remnawave_get_user_info(session, user_uuid)
        if not user_info or not isinstance(user_info.get("activeInternalSquads"), list):
            return False
        squads = [item["uuid"] for item in user_info["activeInternalSquads"]]
        if squad_uuid not in squads:
            squads.append(squad_uuid)
        return await remnawave_update_user_profile(session, user_uuid, active_internal_squads=squads)

    async def _add_to_squad():
        user_id = await identity.api_user_id(user_uuid)
        # add-users means ALL panel users, not the list supplied in the body.
        url = f"{REMNAWAVE_BASE_URL}/internal-squads/{squad_uuid}/bulk-actions/add-many-users"
        payload = {"userIds": [user_id]}
        headers = {
            "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
            "Content-Type": "application/json"
        }

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.post(url, headers=headers, json=payload) as resp:
                if resp.status in (200, 201, 202):
                    logging.info(f"Requested adding user {user_uuid} to squad {squad_uuid}")
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
        user_id = await identity.api_user_id(user_uuid)
        url = f"{REMNAWAVE_BASE_URL}/users/{user_id}"
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
        user_id = await identity.api_user_id(user_uuid)
        url = f"{REMNAWAVE_BASE_URL}/users/{user_id}"
        headers = {"Authorization": f"Bearer {REMNAWAVE_API_TOKEN}"}

        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=_verified_connector()) as temp_session:
            async with temp_session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return await identity.normalize_user_info(user_uuid, data.get("response", {}))
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Get user info failed ({resp.status}): {error_text}")

    return await safe_api_call(
        _get_info,
        error_message=f"Failed to get user info for {user_uuid}"
    )
