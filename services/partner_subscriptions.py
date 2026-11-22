"""Read-only branded subscription gateway. Explicit ports, disabled by default.

No settings import, network at construction, background tasks, DNS or grants.
The operator supplies a separate 256-bit signing key and approved source origin.
Links are bearer secrets: do not log paths, bodies or upstream URLs.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
from urllib.parse import urlsplit
from uuid import UUID

from services.partner_catalog import (
    project_hostname,
    validate_base_domain,
    validate_slug,
)
from services.partner_traffic import PartnerTrafficUsage, bytes_value, utc

MAX_CONFIG_BYTES = 2 * 1024 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{64}\Z")
_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
HWID_FLAGS = frozenset(
    {
        "x-hwid-active",
        "x-hwid-not-supported",
        "x-hwid-limit",
        "x-hwid-max-devices-reached",
    }
)
CLIENT_HEADERS = frozenset(
    {"user-agent", "x-hwid", "x-device-os", "x-ver-os", "x-device-model"}
)
_TYPES = frozenset(
    {
        "text/plain",
        "application/json",
        "application/x-yaml",
        "text/yaml",
        "application/octet-stream",
    }
)


class SubscriptionNotFound(LookupError):
    pass


class SubscriptionUnavailable(RuntimeError):
    pass


class SubscriptionExpired(SubscriptionUnavailable):
    pass


@dataclass(frozen=True)
class PartnerLinkSettings:
    base_domain: str
    signing_key: bytes = field(repr=False)
    short_links: bool = False

    def __post_init__(self):
        object.__setattr__(self, "base_domain", validate_base_domain(self.base_domain))
        if type(self.short_links) is not bool:
            raise ValueError("Нужен явный формат ссылок подписки.")
        if (
            not isinstance(self.signing_key, bytes)
            or not 32 <= len(self.signing_key) <= 64
        ):
            raise ValueError("Нужен отдельный ключ публичных ссылок не короче 256 бит.")


class PartnerSubscriptionLinks:
    def __init__(self, settings):
        if not isinstance(settings, PartnerLinkSettings):
            raise TypeError("PartnerLinkSettings required")
        self.settings = settings

    def slug_from_host(self, host):
        # Never use forwarded headers or request.url to build a credential URL.
        if not isinstance(host, str) or host != host.strip() or len(host) > 253:
            raise SubscriptionNotFound()
        host = host.lower()
        if host.endswith(":443"):
            host = host[:-4]
        suffix = "." + self.settings.base_domain
        if not host.endswith(suffix):
            raise SubscriptionNotFound()
        try:
            return validate_slug(host[: -len(suffix)])
        except ValueError:
            raise SubscriptionNotFound() from None

    @staticmethod
    def subscription_id(token):
        try:
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise ValueError()
            raw = base64.b64decode(token, altchars=b"-_", validate=True)
            if len(raw) != 48 or base64.urlsafe_b64encode(raw).decode() != token:
                raise ValueError()
            uid = UUID(bytes=raw[:16])
            if uid.int == 0:
                raise ValueError()
            return str(uid)
        except ValueError:
            raise SubscriptionNotFound() from None

    def token(self, context):
        project, subscription = UUID(str(context["project_id"])), UUID(
            str(context["subscription_id"])
        )
        generation = context["public_link_generation"]
        if type(generation) is not int or not 0 <= generation <= 2147483647:
            raise ValueError("Invalid public link generation")
        slug = validate_slug(context["slug"])
        message = (
            b"SPN-WL-subscription-v1\0"
            + self.settings.base_domain.encode()
            + b"\0"
            + slug.encode()
            + b"\0"
            + project.bytes
            + subscription.bytes
            + generation.to_bytes(4, "big")
        )
        signature = hmac.new(
            self.settings.signing_key, message, hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(subscription.bytes + signature).decode()

    def verify(self, token, context):
        self.subscription_id(token)
        if not hmac.compare_digest(token, self.token(context)):
            raise SubscriptionNotFound()

    def url(self, context, *, config=False):
        host = project_hostname(context["slug"], self.settings.base_domain)
        return (
            "https://"
            + host
            + ("/" if self.settings.short_links else "/s/")
            + self.token(context)
            + ("/config" if config else "")
        )

    def import_url(self, context):
        """New branded links are identical in the browser and VPN clients.

        The explicit /config endpoint remains a deterministic compatibility
        fallback; URL style never changes the signature or tenant identity.
        """
        return self.url(context, config=not self.settings.short_links)


def client_headers(headers):
    """Forward only device identity; no cookies, authorization, Host or proxy hints."""
    result = {}
    for name, value in headers.items():
        name = name.lower()
        if name not in CLIENT_HEADERS:
            continue
        if name in result:
            raise ValueError("Повторяющийся заголовок приложения.")
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 512
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
        ):
            raise ValueError("Некорректный заголовок приложения.")
        if name == "x-hwid" and not re.fullmatch(r"[a-zA-Z0-9=-]{10,64}", value):
            raise ValueError("Некорректный идентификатор устройства.")
        result[name] = value
    result.setdefault("user-agent", "PartnerSubscriptionGateway/1.0")
    return result


def normalized_quota(context, observation, now):
    """NO_RESET absolute cap -> current-period used/total; same true remaining."""
    if not isinstance(observation, PartnerTrafficUsage):
        raise SubscriptionUnavailable()
    baseline, cap, last, used = map(
        bytes_value,
        (
            context["traffic_cycle_baseline_bytes"],
            context["traffic_limit_bytes"],
            context["traffic_last_used_bytes"],
            observation.used_bytes,
        ),
    )
    if (
        context.get("traffic_cycle_hold") is not None
        or baseline >= cap
        or observation.project_id != context["project_id"]
        or observation.subscription_id != context["subscription_id"]
        or observation.remote_user_id != context["remote_user_id"]
        or observation.limit_bytes != cap
        or used < max(baseline, last)
        or not utc(now) - timedelta(minutes=1)
        <= utc(observation.observed_at)
        <= utc(now)
    ):
        raise SubscriptionUnavailable()
    return {
        "used_bytes": used - baseline,
        "total_bytes": cap - baseline,
        "remaining_bytes": max(0, cap - used),
        "observed_at": observation.observed_at,
    }


@dataclass(frozen=True)
class SubscriptionPayload:
    body: bytes = field(repr=False)
    content_type: str
    headers: dict = field(repr=False)
    status: int = 200


class PartnerSubscriptionSource:
    """Only an operator-approved HTTPS origin and the saved short credential.

    Payload is NOT parsed/rebuilt: JSON, URI/base64 and YAML remain byte-identical.
    No shared cache: device limits and response rules must see each real client.
    """

    def __init__(self, base_url, *, session_factory=None):
        from services.partner_remnawave import _origin

        self._origin = _origin(base_url)
        self._host = urlsplit(self._origin).hostname
        self._session_factory = session_factory

    def __repr__(self):
        return "<PartnerSubscriptionSource>"

    def _url(self, value):
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError()
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self._host
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "%" in parsed.netloc
            or not re.fullmatch(r"/(?:sub/)?[A-Za-z0-9_-]{8,128}", parsed.path)
        ):
            raise ValueError()
        # Build from the configured origin rather than returning a supplied netloc.
        return self._origin + parsed.path

    async def fetch(self, url, headers):
        try:
            import aiohttp

            target, outgoing = self._url(url), client_headers(headers)
            outgoing["accept-encoding"] = "identity"
            factory = self._session_factory or aiohttp.ClientSession
            async with factory(
                timeout=aiohttp.ClientTimeout(total=12, connect=4), trust_env=False
            ) as session:
                async with session.request(
                    "GET", target, headers=outgoing, allow_redirects=False
                ) as response:
                    if response.status not in (200, 404):
                        raise ValueError()
                    selected = {}
                    for key in (
                        *HWID_FLAGS,
                        "subscription-userinfo",
                        "profile-update-interval",
                        "routing",
                    ):
                        values = response.headers.getall(key, [])
                        if len(values) > 1:
                            raise ValueError()
                        if values:
                            selected[key] = values[0]
                    if any(
                        not isinstance(v, str)
                        or len(v) > 8192
                        or any(ord(c) < 32 or ord(c) > 126 for c in v)
                        for v in selected.values()
                    ):
                        raise ValueError()
                    for key in HWID_FLAGS & selected.keys():
                        if selected[key] != "true":
                            raise ValueError()
                    denied = (
                        selected.get("x-hwid-not-supported") == "true"
                        or selected.get("x-hwid-max-devices-reached") == "true"
                        or selected.get("x-hwid-limit") == "true"
                    )
                    media = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if (
                        response.status == 404
                        and not denied
                        or media not in _TYPES
                        and not denied
                    ):
                        raise ValueError()
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > MAX_CONFIG_BYTES:
                            raise ValueError()
                    # An upstream denial must never smuggle an HTML/error page as config.
                    if denied:
                        return SubscriptionPayload(
                            b"", "text/plain", selected, response.status
                        )
                    if not raw:
                        raise ValueError()
                    return SubscriptionPayload(bytes(raw), media, selected)
        except Exception:
            raise SubscriptionUnavailable(
                "Не удалось обновить подписку. Повторите позже."
            ) from None


def profile_headers(context, links, quota, source_headers=None):
    from services.partner_store import _text, _url
    from services.partner_brand_defaults import DEFAULT_CUSTOMER_SUPPORT_URL

    source_headers = source_headers or {}
    brand = context.get("brand") if isinstance(context.get("brand"), dict) else {}
    title = _text(
        brand.get("subscription_title") or context["title"],
        "subscription_title",
        maximum=80,
    )
    expires = int(utc(context["expires_at"]).timestamp())
    result = {
        "profile-title": "base64:" + base64.b64encode(title.encode()).decode(),
        "profile-web-page-url": links.url(context),
        "subscription-userinfo": f"upload=0; download={quota['used_bytes']}; total={quota['total_bytes']}; expire={expires}",
        "content-disposition": 'attachment; filename="subscription"',
    }
    support = _url(context.get("support_url") or DEFAULT_CUSTOMER_SUPPORT_URL, "support_url")
    if support:
        # HTTP header values must be ASCII; URL-encode non-ASCII path components.
        from urllib.parse import quote, urlunsplit

        parsed = urlsplit(support)
        host = parsed.hostname.encode("idna").decode("ascii")
        if ":" in host:
            host = "[" + host + "]"
        result["support-url"] = urlunsplit(
            (
                "https",
                host,
                quote(parsed.path, safe="/%:@!$&'()*+,;=-._~"),
                quote(parsed.query, safe="=&%/:?@!$'()*+,;-._~"),
                quote(parsed.fragment, safe="%-._~"),
            )
        )
    for key in HWID_FLAGS:
        if source_headers.get(key) == "true":
            result[key] = "true"
    interval = source_headers.get("profile-update-interval", "")
    if (
        isinstance(interval, str)
        and re.fullmatch(r"[1-9][0-9]{0,2}", interval)
        and int(interval) <= 168
    ):
        result["profile-update-interval"] = interval
    if source_headers.get("routing"):
        result["routing"] = source_headers["routing"]
    return result


def _source_usage(payload, context, observation):
    """Prove upstream metadata matches this exact paid snapshot, not another URL."""
    value = payload.headers.get("subscription-userinfo", "")
    if not isinstance(value, str) or len(value) > 256:
        raise SubscriptionUnavailable()
    fields = {}
    for item in value.split(";"):
        match = re.fullmatch(
            r"\s*(upload|download|total|expire)=([0-9]{1,16})\s*", item
        )
        if not match or match[1] in fields:
            raise SubscriptionUnavailable()
        fields[match[1]] = int(match[2])
    if (
        set(fields) != {"upload", "download", "total", "expire"}
        or fields["total"] != context["traffic_limit_bytes"]
        or fields["expire"] != int(utc(context["expires_at"]).timestamp())
    ):
        raise SubscriptionUnavailable()
    used = bytes_value(fields["upload"] + fields["download"])
    # Source caches must not roll the API's verified monotone observation back.
    used = max(used, observation.used_bytes)
    return PartnerTrafficUsage(
        observation.project_id,
        observation.subscription_id,
        observation.remote_user_id,
        used,
        observation.limit_bytes,
        observation.observed_at,
    )


class PartnerSubscriptionService:
    def __init__(
        self, store, links, *, provisioner=None, source=None, enabled=False, clock=None
    ):
        self.store, self.links = store, links
        self.provisioner, self.source, self.enabled = (
            provisioner,
            source,
            enabled is True,
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if not isinstance(links, PartnerSubscriptionLinks):
            raise TypeError("PartnerSubscriptionLinks required")
        if self.enabled and not getattr(store, "traffic_cycles_enabled", False):
            raise ValueError(
                "Public subscriptions require explicit paid-period traffic accounting"
            )

    async def link_for_customer(self, project_id, tg_id, subscription_id):
        if not self.enabled:
            raise SubscriptionUnavailable()
        context = await self.store.get_subscription_link_context(
            project_id, tg_id, subscription_id
        )
        return self.links.url(context)

    async def _resolve(self, slug, token, store=None):
        if not self.enabled:
            raise SubscriptionNotFound()
        sub = self.links.subscription_id(token)
        from services.partner_commerce_store import CommerceNotFound

        try:
            context = await (store or self.store).resolve_public_subscription(slug, sub)
        except CommerceNotFound:
            raise SubscriptionNotFound() from None
        if context["slug"] != slug or context["subscription_id"] != sub:
            raise SubscriptionNotFound()
        self.links.verify(token, context)
        return context

    def _readable(self, context):
        if utc(context["expires_at"]) <= utc(self.clock()):
            raise SubscriptionExpired()
        if (
            context.get("activation_pending")
            or context.get("traffic_cycle_hold") is not None
            or context.get("traffic_cycle_baseline_bytes") is None
            or self.provisioner is None
        ):
            raise SubscriptionUnavailable()

    async def page(self, slug, token):
        context = await self._resolve(slug, token)
        state, quota = "ready", None
        try:
            self._readable(context)
            async with asyncio.timeout(8):
                async with self.store.acquire_activation_lock(
                    context["project_id"], context["subscription_id"]
                ) as lease:
                    context = await self._resolve(slug, token, lease)
                    self._readable(context)
                    observation = await self.provisioner.read_public_usage(context)
                    await lease.remember_public_usage(
                        context, observation, now=self.clock()
                    )
                    quota = normalized_quota(context, observation, self.clock())
                    await self._resolve(slug, token, lease)
        except SubscriptionExpired:
            state = "expired"
        except SubscriptionNotFound:
            raise
        except Exception:
            state = "updating" if context.get("activation_pending") else "unavailable"
        # Return only to the server-side renderer. No JSON endpoint for these rows.
        return context, state, quota

    async def config(self, slug, token, headers):
        context = await self._resolve(slug, token)
        self._readable(context)
        if self.source is None:
            raise SubscriptionUnavailable()
        outgoing = client_headers(headers)
        try:
            async with asyncio.timeout(20):
                async with self.store.acquire_activation_lock(
                    context["project_id"], context["subscription_id"]
                ) as lease:
                    context = await self._resolve(slug, token, lease)
                    self._readable(context)
                    observation = await self.provisioner.read_public_usage(context)
                    await lease.remember_public_usage(
                        context, observation, now=self.clock()
                    )
                    normalized_quota(context, observation, self.clock())
                    payload = await self.source.fetch(
                        context["subscription_url"], outgoing
                    )
                    denial = any(
                        payload.headers.get(key) == "true"
                        for key in HWID_FLAGS - {"x-hwid-active"}
                    )
                    if not denial:
                        observation = _source_usage(payload, context, observation)
                    quota = normalized_quota(context, observation, self.clock())
                    # Rotation/project suspension may occur while the remote GET is running.
                    fresh = await self._resolve(slug, token, lease)
                    for key in (
                        "expires_at",
                        "traffic_limit_bytes",
                        "device_limit",
                        "remote_user_id",
                        "subscription_url",
                        "traffic_cycle_baseline_bytes",
                        "traffic_cycle_hold",
                        "activation_pending",
                    ):
                        if fresh[key] != context[key]:
                            raise SubscriptionUnavailable()
                    self._readable(fresh)
                    return SubscriptionPayload(
                        payload.body,
                        payload.content_type,
                        profile_headers(context, self.links, quota, payload.headers),
                        payload.status,
                    )
        except (SubscriptionNotFound, SubscriptionExpired):
            raise
        except Exception:
            raise SubscriptionUnavailable(
                "Не удалось обновить подписку. Повторите позже."
            ) from None
