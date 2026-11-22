"""Explicit, isolated Remnawave port for white-label activation snapshots.

Construction is inert: the host must deliberately inject credentials, approved
squads and this adapter. No main-bot configuration, identity mapping or traffic
reset is used. Only a complete, verified readback completes an activation. A
lost create response is recovered by the next lookup of the same stable name,
never by creating a second user or adding relative days a second time.

This is a local/staging adapter. API 2 and 3 shapes are covered by transport
fixtures, not a claim of compatibility with an untested production panel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
import re
import secrets
import string
from urllib.parse import urlsplit
from uuid import UUID

from services.partner_catalog import (
    BASE_TRAFFIC_GB,
    BASE_DEVICE_LIMIT,
    MAX_DEVICE_LIMIT,
)
from services.partner_checkout import CheckoutError, ProvisionedSubscription
from services.partner_traffic import PartnerTrafficUsage, bytes_value
from services.partner_panel_identity import identity_fields, legacy_identity


_MAX_RESPONSE_BYTES = 131_072
_MIN_TRAFFIC_BYTES = BASE_TRAFFIC_GB * 1024**3
_MAX_SAFE_INTEGER = 2**53 - 1
_SAFE_MESSAGE = "Выдача подписки требует повторной проверки. Повторно платить не нужно."
_SHORT_UUID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")


class PartnerProvisioningError(CheckoutError):
    """Redacted, retryable provisioning failure; no raw remote body or token."""


def _uuid(value):
    if not isinstance(value, (str, UUID)):
        raise ValueError()
    result = UUID(str(value))
    if result.int == 0:
        raise ValueError()
    return str(result)


def _origin(value, *, api=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError()
    parsed = urlsplit(value)
    expected_paths = {"/api", "/api/"} if api else {"", "/"}
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.path not in expected_paths
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(c.isspace() or ord(c) < 32 for c in value)
        or "%" in parsed.netloc
    ):
        raise ValueError()
    # Normalize the explicitly approved host, not a host received from the API.
    return value.rstrip("/")


@dataclass(frozen=True)
class PartnerRemnawaveSettings:
    api_base_url: str
    api_token: str = field(repr=False)
    api_version: int
    internal_squad_uuids: tuple[str, ...]
    subscription_base_url: str

    def __post_init__(self):
        try:
            api = _origin(self.api_base_url, api=True)
            subscription = _origin(self.subscription_base_url)
            if (
                not isinstance(self.api_token, str)
                or not 10 <= len(self.api_token) <= 4096
                or any(c.isspace() or ord(c) < 32 for c in self.api_token)
                or type(self.api_version) is not int
                or self.api_version not in (2, 3)
                or not isinstance(self.internal_squad_uuids, (list, tuple))
                or not 1 <= len(self.internal_squad_uuids) <= 100
            ):
                raise ValueError()
            squads = tuple(_uuid(value) for value in self.internal_squad_uuids)
            if len(set(squads)) != len(squads):
                raise ValueError()
            object.__setattr__(self, "api_base_url", api)
            object.__setattr__(self, "subscription_base_url", subscription)
            object.__setattr__(self, "internal_squad_uuids", squads)
        except Exception:
            raise ValueError("Адаптер выдачи подписок не настроен.") from None


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError()
    return value.astimezone(timezone.utc)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


class PartnerRemnawaveProvisioner:
    def __init__(
        self, settings: PartnerRemnawaveSettings, *, session_factory=None, clock=None
    ):
        if not isinstance(settings, PartnerRemnawaveSettings):
            raise ValueError("Адаптер выдачи подписок не настроен.")
        self.settings = settings
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return "<PartnerRemnawaveProvisioner>"

    def _remote_id(self, value):
        if self.settings.api_version == 2:
            return _uuid(value)
        if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,15}", value):
            value = int(value)
        if type(value) is not int or not 0 < value <= _MAX_SAFE_INTEGER:
            raise ValueError()
        return str(value)

    def _snapshot(self, activation):
        if not isinstance(activation, dict):
            raise ValueError()
        project_id = _uuid(activation["project_id"])
        subscription_id = _uuid(activation["subscription_id"])
        identity = identity_fields(
            (
                activation["remote_identity"]
                if "remote_identity" in activation
                else legacy_identity(project_id, subscription_id)
            ),
            project_id,
            subscription_id,
        )
        username = identity["username"]
        if activation.get("remote_username") != username:
            raise ValueError()
        expiry = _utc(activation["expires_at"])
        if expiry <= _utc(self._clock()):
            raise ValueError()
        traffic, devices = activation["traffic_limit_bytes"], activation["device_limit"]
        if (
            type(traffic) is not int
            or not _MIN_TRAFFIC_BYTES <= traffic <= _MAX_SAFE_INTEGER
            or type(devices) is not int
            or not BASE_DEVICE_LIMIT <= devices <= MAX_DEVICE_LIMIT
        ):
            raise ValueError()
        remote_id = activation.get("remote_user_id")
        if remote_id is not None:
            remote_id = self._remote_id(remote_id)
        return {
            **identity,
            "expireAt": expiry.isoformat(),
            "trafficLimitBytes": traffic,
            "hwidDeviceLimit": devices,
            "trafficLimitStrategy": "NO_RESET",
            "activeInternalSquads": list(self.settings.internal_squad_uuids),
        }, remote_id

    async def _request(self, method, path, *, payload=None, absent=False):
        # Paths are assembled solely from constant routes and validated IDs.
        try:
            import aiohttp

            factory = self._session_factory or aiohttp.ClientSession
            headers = {
                "Authorization": f"Bearer {self.settings.api_token}",
                "Accept": "application/json",
            }
            async with factory(
                timeout=aiohttp.ClientTimeout(total=20, connect=5), trust_env=False
            ) as session:
                async with session.request(
                    method,
                    self.settings.api_base_url + path,
                    headers=headers,
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > _MAX_RESPONSE_BYTES:
                            raise ValueError()
                    body = json.loads(raw, object_pairs_hook=_unique_object)
                    if (
                        absent
                        and response.status == 404
                        and isinstance(body, dict)
                        and body.get("errorCode") == "A025"
                    ):
                        return None
                    if response.status not in (200, 201) or not isinstance(body, dict):
                        raise ValueError()
                    user = body.get("response")
                    if not isinstance(user, dict):
                        raise ValueError()
                    return user
        except Exception:
            raise PartnerProvisioningError(_SAFE_MESSAGE) from None

    def _identity(self, user, expected, saved_id=None):
        if (
            not isinstance(user, dict)
            or user.get("username") != expected["username"]
            or not isinstance(user.get("description"), str)
            or not hmac.compare_digest(
                user["description"].encode("utf-8"),
                expected["description"].encode("utf-8"),
            )
        ):
            raise ValueError()
        # Exact immutable support metadata is also ownership proof. Never
        # replace a colliding foreign user or "repair" a changed marker.
        if "tag" in expected and (
            user.get("tag") != expected["tag"]
            or type(user.get("telegramId")) is not int
            or user["telegramId"] != expected["telegramId"]
        ):
            raise ValueError()
        if self.settings.api_version == 3:
            # Mixing API versions is not a license to guess a mapping.
            if user.get("uuid") is not None:
                raise ValueError()
            raw_id = user.get("id")
            if type(raw_id) is not int:
                raise ValueError()
        else:
            raw_id = user.get("uuid")
        remote_id = self._remote_id(raw_id)
        if saved_id is not None and remote_id != saved_id:
            raise ValueError()
        return remote_id

    @staticmethod
    def _squads(user):
        values = user.get("activeInternalSquads")
        if not isinstance(values, list):
            raise ValueError()
        squads = [
            _uuid(value.get("uuid") if isinstance(value, dict) else value)
            for value in values
        ]
        if len(squads) != len(set(squads)):
            raise ValueError()
        return set(squads)

    def _entitlements(self, user, expected, *, exact, device_expiry=False):
        expiry = _utc(user["expireAt"])
        wanted = _utc(expected["expireAt"])
        traffic, devices = user["trafficLimitBytes"], user["hwidDeviceLimit"]
        if (
            type(traffic) is not int
            or not 0 < traffic <= _MAX_SAFE_INTEGER
            or type(devices) is not int
            or not 0 < devices <= MAX_DEVICE_LIMIT
            or user.get("status") not in {"ACTIVE", "EXPIRED", "LIMITED"}
        ):
            raise ValueError()
        if exact:
            if (
                user.get("status") == "EXPIRED"
                or expiry != wanted
                or traffic != expected["trafficLimitBytes"]
                or devices != expected["hwidDeviceLimit"]
                or user.get("trafficLimitStrategy") != "NO_RESET"
                or self._squads(user) != set(self.settings.internal_squad_uuids)
            ):
                raise ValueError()
        elif (
            expiry > wanted
            or traffic > expected["trafficLimitBytes"]
            or devices > expected["hwidDeviceLimit"]
            and not device_expiry
        ):
            # A stale snapshot must not undo a newer entitlement. The caller's
            # per-subscription durable lock/FIFO remains mandatory as well.
            raise ValueError()

    def _subscription_url(self, user):
        short_uuid = user.get("shortUuid")
        if short_uuid is not None and (
            not isinstance(short_uuid, str) or not _SHORT_UUID.fullmatch(short_uuid)
        ):
            raise ValueError()
        value = user.get("subscriptionUrl")
        if value is None:
            if short_uuid is None:
                raise ValueError()
            return self.settings.subscription_base_url + "/sub/" + short_uuid
        if not isinstance(value, str) or not 1 <= len(value) <= 4096:
            raise ValueError()
        approved = urlsplit(self.settings.subscription_base_url)
        parsed = urlsplit(value)
        parts = parsed.path.split("/")
        credential = parts[-1] if len(parts) in (2, 3) and parts[0] == "" else ""
        if (
            parsed.scheme != "https"
            or parsed.hostname != approved.hostname
            or (parsed.port or 443) != (approved.port or 443)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(c.isspace() or ord(c) < 32 for c in value)
            or (len(parts) == 3 and parts[1] != "sub")
            or not _SHORT_UUID.fullmatch(credential)
            or short_uuid is not None
            and not hmac.compare_digest(short_uuid, credential)
        ):
            raise ValueError()
        return value

    async def ensure_subscription(self, activation: dict) -> ProvisionedSubscription:
        try:
            expected, saved_id = self._snapshot(activation)
            path = "/users/by-username/" + expected["username"]
            user = await self._request("GET", path, absent=True)
            if user is None:
                if saved_id is not None:
                    # Deleting an issued remote profile requires an explicit
                    # operator repair, not a silent new credential/identity.
                    raise ValueError()
                payload = dict(expected)
                if self.settings.api_version == 2:
                    alphabet = string.ascii_letters + string.digits
                    payload["password"] = (
                        secrets.choice(string.ascii_uppercase)
                        + secrets.choice(string.ascii_lowercase)
                        + secrets.choice(string.digits)
                        + "".join(secrets.choice(alphabet) for _ in range(21))
                    )
                # Deliberately no POST retry on timeout/409. The *next* call
                # starts with a read and can recover a remotely committed user.
                created = await self._request("POST", "/users", payload=payload)
                saved_id = self._identity(created, expected)
            else:
                saved_id = self._identity(user, expected, saved_id)
                self._usage_floor(user, activation)
                self._entitlements(
                    user,
                    expected,
                    exact=False,
                    device_expiry=activation.get("kind") == "device_expiry",
                )
                payload = {
                    **expected,
                    "id" if self.settings.api_version == 3 else "uuid": (
                        int(saved_id) if self.settings.api_version == 3 else saved_id
                    ),
                }
                await self._request("PATCH", "/users", payload=payload)
            # Neither a 200 mutation response nor a matching username alone is
            # proof that the paid snapshot was actually applied.
            readback = await self._request("GET", path)
            self._identity(readback, expected, saved_id)
            self._entitlements(readback, expected, exact=True)
            self._usage_floor(readback, activation)
            return ProvisionedSubscription(
                saved_id, expected["username"], self._subscription_url(readback)
            )
        except Exception:
            raise PartnerProvisioningError(_SAFE_MESSAGE) from None

    @staticmethod
    def _usage(user):
        traffic = user.get("userTraffic")
        if not isinstance(traffic, dict):
            raise ValueError()
        return bytes_value(traffic.get("usedTrafficBytes"))

    def _usage_floor(self, user, activation):
        if "usage_floor_bytes" in activation:
            if self._usage(user) < bytes_value(activation["usage_floor_bytes"]):
                raise ValueError()

    async def read_usage(self, context):
        return await self._read_usage(context)

    async def read_public_usage(self, context):
        """Also bind the saved source credential to the verified owned API user."""
        return await self._read_usage(context, bind_url=True)

    async def _read_usage(self, context, *, bind_url=False):
        """Read an owned existing user; missing/invalid counters are NOT zero."""
        try:
            expected, saved_id = self._snapshot(context)
            if saved_id is None:
                raise ValueError()
            user = await self._request(
                "GET", "/users/by-username/" + expected["username"]
            )
            self._identity(user, expected, saved_id)
            self._entitlements(user, expected, exact=True)
            if bind_url and self._subscription_url(user) != context.get(
                "subscription_url"
            ):
                raise ValueError()
            return PartnerTrafficUsage(
                str(UUID(context["project_id"])),
                str(UUID(context["subscription_id"])),
                saved_id,
                self._usage(user),
                expected["trafficLimitBytes"],
                _utc(self._clock()),
            )
        except Exception:
            raise PartnerProvisioningError(
                "Не удалось проверить трафик. Счётчик и платные ГБ не сброшены."
            ) from None
