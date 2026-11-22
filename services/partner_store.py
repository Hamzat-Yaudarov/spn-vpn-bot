"""Durable, tenant-scoped white-label project registry (no billing or activation).

The caller injects a pool getter; importing this module neither reads settings nor
connects to a database. Apply ops/partner-platform/schema.sql explicitly to an
isolated/staging database before use. This module never migrates an existing bot.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
import inspect
import json
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg

from services.partner_catalog import MAX_PRICE_KOPECKS, MIN_PRICE_KOPECKS, validate_slug
from services.partner_brand_defaults import DEFAULT_CUSTOMER_SUPPORT_URL, brand_defaults

EDITABLE_FIELDS = frozenset(
    {"title", "price_kopecks", "support_url", "news_url", "brand"}
)
BRAND_FIELDS = frozenset(
    {
        "logo_url",
        "logo_asset_id",
        "accent_color",
        "welcome_text",
        "subscription_title",
        "subscription_description",
    }
)
_PROJECT_COLUMNS = """id, owner_tg_id, title, slug, price_kopecks, version, status,
    support_url, news_url, brand, bot_id, bot_username, created_at, updated_at"""
_PROJECT_SELECT = f"SELECT {_PROJECT_COLUMNS} FROM wl_projects"


@dataclass(frozen=True, slots=True)
class BotUpdateContext:
    """Fresh, private DB receipt for one webhook, never a client credential.

    Only the registry creates this object after a current exact-tenant SELECT.
    It saves repeated *read-only* registration/staff discovery in the customer
    renderer. Money and privileged management operations still authorize and
    lock independently. Nothing here may be retained between requests.
    """

    project_id: str
    bot_id: int
    user_id: int
    project_version: int
    registered: bool
    attribution_frozen: bool
    staff_role: str | None

    def matches(self, project, user_id):
        return (
            self.project_id == project.get("id")
            and self.bot_id == (project.get("bot") or {}).get("id")
            and self.user_id == user_id
            and self.project_version == project.get("version")
        )


class InvalidProject(ValueError):
    """Input is not a permitted project setting."""


class ProjectNotFound(LookupError):
    """Project does not exist or is not owned by this user."""


class ProjectConflict(RuntimeError):
    """Request key, globally unique identifier or expected version conflicts."""


def _positive_int(value: Any, name: str, *, maximum: int = 2**63 - 1) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise InvalidProject(f"Некорректное поле: {name}.")
    return value


def _text(value: Any, name: str, *, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidProject(f"Некорректное поле: {name}.")
    value = value.strip()
    forbidden = "\u007f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    if (
        (not value and not empty)
        or len(value) > maximum
        or any((ord(c) < 32 and c not in "\n\t") or c in forbidden for c in value)
        or (name in {"title", "subscription_title"} and any(c in value for c in "\n\t"))
    ):
        raise InvalidProject(f"Некорректное поле: {name}.")
    return value


def _url(value: Any, name: str) -> str:
    value = _text(value, name, maximum=500, empty=True)
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise InvalidProject(f"Некорректная HTTPS-ссылка: {name}.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or any(c.isspace() for c in value)
        or "\\" in value
    ):
        raise InvalidProject(f"Некорректная HTTPS-ссылка: {name}.")
    return value


def _price(value: Any) -> int:
    value = _positive_int(value, "price_kopecks", maximum=MAX_PRICE_KOPECKS)
    if value < MIN_PRICE_KOPECKS:
        raise InvalidProject("Минимальная цена подписки — 199 ₽.")
    return value


def _brand(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) - BRAND_FIELDS:
        raise InvalidProject("Неизвестные поля оформления.")
    result = {}
    for key, item in value.items():
        if key == "logo_url":
            result[key] = _url(item, key)
        elif key == "logo_asset_id":
            if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item):
                raise InvalidProject("Некорректный логотип.")
            result[key] = item
        elif key == "accent_color":
            if not isinstance(item, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", item):
                raise InvalidProject("Цвет должен быть в формате #RRGGBB.")
            result[key] = item.lower()
        else:
            maximum = 80 if key == "subscription_title" else 2000
            result[key] = _text(item, key, maximum=maximum, empty=True)
    return result


def validate_changes(changes: Any) -> dict:
    """Whitelist settings here too; HTTP validation alone is not a security gate."""
    if not isinstance(changes, dict) or not changes or set(changes) - EDITABLE_FIELDS:
        raise InvalidProject("Разрешено менять только настройки оформления и цену.")
    validated = {}
    for key, value in changes.items():
        if key == "title":
            validated[key] = _text(value, key, maximum=80)
        elif key == "price_kopecks":
            validated[key] = _price(value)
        elif key == "brand":
            validated[key] = _brand(value)
        else:
            validated[key] = _url(value, key)
    return validated


def _project_id(value: Any) -> UUID:
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise InvalidProject("Некорректный идентификатор проекта.") from None


def _public(row: Mapping[str, Any]) -> dict:
    """Explicit allowlist: ciphertext and future private columns cannot leak."""
    brand = row["brand"]
    if isinstance(brand, str):
        brand = json.loads(brand)
    return {
        "id": str(row["id"]),
        "owner_tg_id": row["owner_tg_id"],
        "title": row["title"],
        "slug": row["slug"],
        "price_kopecks": row["price_kopecks"],
        "version": row["version"],
        "status": row["status"],
        "support_url": row["support_url"] or DEFAULT_CUSTOMER_SUPPORT_URL,
        "news_url": row["news_url"],
        "brand": dict(brand),
        "bot": {"id": row["bot_id"], "username": row["bot_username"]},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class PartnerStore:
    def __init__(self, pool_getter: Callable):
        if not callable(pool_getter):
            raise TypeError("pool_getter must be callable")
        self._pool_getter = pool_getter

    async def _pool(self):
        pool = self._pool_getter()
        return await pool if inspect.isawaitable(pool) else pool

    @staticmethod
    async def _audit(conn, project_id, owner_id, action, version, fields=()):
        # Store only field NAMES and metadata. Never token, message or URL values.
        await conn.execute(
            """INSERT INTO wl_project_audit
            (project_id, owner_tg_id, action, project_version, details)
            VALUES ($1, $2, $3, $4, $5::jsonb)""",
            project_id,
            owner_id,
            action,
            version,
            json.dumps({"fields": sorted(fields)}, separators=(",", ":")),
        )

    async def create_project(
        self, owner_id, request_key, slug, title, price_kopecks=MIN_PRICE_KOPECKS
    ):
        owner_id = _positive_int(owner_id, "owner_id")
        request_key = _text(request_key, "request_key", maximum=128)
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_key):
            raise InvalidProject("Некорректный ключ запроса.")
        try:
            slug = validate_slug(slug)
        except ValueError:
            raise InvalidProject(
                "Адрес проекта: 3–32 строчных латинских символа, цифры и дефис."
            )
        title = _text(title, "title", maximum=80)
        price_kopecks = _price(price_kopecks)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "slug": slug,
                    "title": title,
                    "price_kopecks": price_kopecks,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        # Request-local DB lock serializes same-key retries across processes.
        lock = int.from_bytes(
            hashlib.sha256(
                f"wl-project-create:{owner_id}:{request_key}".encode()
            ).digest()[:8],
            "big",
            signed=True,
        )
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", lock)
                    prior = await conn.fetchrow(
                        """SELECT fingerprint, project_id
                        FROM wl_project_create_requests WHERE owner_tg_id=$1 AND request_key=$2""",
                        owner_id,
                        request_key,
                    )
                    if prior:
                        if prior["fingerprint"] != fingerprint:
                            raise ProjectConflict(
                                "Ключ запроса уже использован с другими настройками."
                            )
                        row = await conn.fetchrow(
                            _PROJECT_SELECT + " WHERE id=$1 AND owner_tg_id=$2",
                            prior["project_id"],
                            owner_id,
                        )
                        if not row:
                            raise ProjectConflict(
                                "Не удалось восстановить созданный проект."
                            )
                        return _public(row)
                    project_id = uuid4()
                    row = await conn.fetchrow(
                        f"""INSERT INTO wl_projects
                        (id, owner_tg_id, slug, title, price_kopecks, brand)
                        VALUES ($1,$2,$3,$4,$5,$6::jsonb) RETURNING {_PROJECT_COLUMNS}""",
                        project_id,
                        owner_id,
                        slug,
                        title,
                        price_kopecks,
                        json.dumps(brand_defaults(title), ensure_ascii=False),
                    )
                    await conn.execute(
                        """INSERT INTO wl_project_create_requests
                        (owner_tg_id, request_key, fingerprint, project_id) VALUES ($1,$2,$3,$4)""",
                        owner_id,
                        request_key,
                        fingerprint,
                        project_id,
                    )
                    await self._audit(
                        conn, project_id, owner_id, "created", row["version"]
                    )
                    return _public(row)
        except asyncpg.UniqueViolationError:
            raise ProjectConflict("Этот адрес проекта уже занят.") from None

    async def prepare_enrollment_project(self, owner_id, request_key):
        """Reuse the owner's saved enrollment before preparing a new invoice.

        The owner-wide transaction lock converges different browser request
        keys onto one project. It is released before any provider network I/O;
        the entry order and the provider's idempotence key then own recovery.
        This method is only used by the payment-first enrollment surface.
        """
        owner_id = _positive_int(owner_id, "owner_id")
        try:
            request_key = str(UUID(str(request_key)))
        except (ValueError, TypeError, AttributeError):
            raise InvalidProject("Некорректный ключ запроса.") from None
        lock = int.from_bytes(hashlib.sha256(
            f"wl-enrollment-project:{owner_id}".encode()
        ).digest()[:8], "big", signed=True)
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", lock)
                    row = await conn.fetchrow(
                        f"""SELECT {', '.join('p.' + c.strip() for c in _PROJECT_COLUMNS.split(','))}
                        FROM wl_projects p
                        WHERE p.owner_tg_id=$1
                        ORDER BY
                          EXISTS (SELECT 1 FROM wl_entry_orders e WHERE e.project_id=p.id
                            AND e.owner_tg_id=p.owner_tg_id
                            AND e.status IN ('paid_pending_launch','launched')) DESC,
                          EXISTS (SELECT 1 FROM wl_entry_orders e WHERE e.project_id=p.id
                            AND e.owner_tg_id=p.owner_tg_id AND e.status <> 'canceled') DESC,
                          p.created_at DESC, p.id DESC LIMIT 1 FOR UPDATE OF p""",
                        owner_id,
                    )
                    if row:
                        return _public(row)
                    # Server-owned defaults, never hidden price/slug fields
                    # supplied by an untrusted browser.
                    title = "Мой сервис"
                    digest = hashlib.sha256(f"{owner_id}:{request_key}".encode()).hexdigest()
                    slug = "service-" + digest[:16]
                    fingerprint = hashlib.sha256(json.dumps({
                        "slug": slug, "title": title, "price_kopecks": MIN_PRICE_KOPECKS,
                    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                    prior = await conn.fetchrow(
                        """SELECT fingerprint, project_id FROM wl_project_create_requests
                        WHERE owner_tg_id=$1 AND request_key=$2""", owner_id, request_key,
                    )
                    if prior:
                        raise ProjectConflict("Предыдущий проект требует проверки.")
                    project_id = uuid4()
                    row = await conn.fetchrow(
                        f"""INSERT INTO wl_projects
                        (id, owner_tg_id, slug, title, price_kopecks, brand)
                        VALUES ($1,$2,$3,$4,$5,$6::jsonb) RETURNING {_PROJECT_COLUMNS}""",
                        project_id, owner_id, slug, title, MIN_PRICE_KOPECKS,
                        json.dumps(brand_defaults(title), ensure_ascii=False),
                    )
                    await conn.execute(
                        """INSERT INTO wl_project_create_requests
                        (owner_tg_id, request_key, fingerprint, project_id) VALUES ($1,$2,$3,$4)""",
                        owner_id, request_key, fingerprint, project_id,
                    )
                    await self._audit(conn, project_id, owner_id, "created", row["version"])
                    return _public(row)
        except asyncpg.UniqueViolationError:
            raise ProjectConflict("Предыдущий проект требует проверки.") from None

    async def list_projects(self, owner_id):
        owner_id = _positive_int(owner_id, "owner_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _PROJECT_SELECT
                + " WHERE owner_tg_id=$1 ORDER BY created_at DESC, id DESC",
                owner_id,
            )
        return [_public(row) for row in rows]

    async def list_enrollment_projects(self, owner_id):
        """Same saved-payment priority as prepare_enrollment_project, read-only."""
        owner_id = _positive_int(owner_id, "owner_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT {', '.join('p.' + c.strip() for c in _PROJECT_COLUMNS.split(','))}
                FROM wl_projects p WHERE p.owner_tg_id=$1
                ORDER BY
                  EXISTS (SELECT 1 FROM wl_entry_orders e WHERE e.project_id=p.id
                    AND e.owner_tg_id=p.owner_tg_id
                    AND e.status IN ('paid_pending_launch','launched')) DESC,
                  EXISTS (SELECT 1 FROM wl_entry_orders e WHERE e.project_id=p.id
                    AND e.owner_tg_id=p.owner_tg_id AND e.status <> 'canceled') DESC,
                  p.created_at DESC, p.id DESC""", owner_id,
            )
        return [_public(row) for row in rows]

    async def get_project(self, project_id, owner_id):
        project_id = _project_id(project_id)
        owner_id = _positive_int(owner_id, "owner_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                _PROJECT_SELECT + " WHERE id=$1 AND owner_tg_id=$2",
                project_id,
                owner_id,
            )
        if row is None:
            raise ProjectNotFound("Проект не найден.")
        return _public(row)

    async def get_runtime_project(self, project_id):
        """Server-side bot routing only; never an unauthenticated admin API.

        The webhook boundary must verify its separate per-project secret before
        processing any customer update. No ciphertext is selected or returned.
        Draft/suspended projects are not live client bots.
        """
        project_id = _project_id(project_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                _PROJECT_SELECT
                + " WHERE id=$1 AND status='active' AND bot_id IS NOT NULL",
                project_id,
            )
        if row is None:
            raise ProjectNotFound("Проект недоступен.")
        return _public(row)

    async def get_bot_update_context(self, project_id, user_id=None):
        """One fresh query for bot identity, known customer and current staff.

        ``user_id`` is only a lookup hint from the bounded update. The route
        still checks its webhook secret and the handler validates the complete
        private Telegram identity before using the receipt. Missing/legacy
        customer attribution takes the original locked registration path.
        No token, price, balance, role or project cache is introduced.
        """
        project_id = _project_id(project_id)
        if user_id is not None:
            user_id = _positive_int(user_id, "user_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT {', '.join('p.' + c.strip() for c in _PROJECT_COLUMNS.split(','))},
                EXISTS(SELECT 1 FROM wl_customers c
                  WHERE c.project_id=p.id AND c.tg_id=$2::bigint) AS customer_registered,
                EXISTS(SELECT 1 FROM wl_campaign_attributions a
                  WHERE a.project_id=p.id AND a.tg_id=$2::bigint) AS attribution_frozen,
                (SELECT m.role FROM wl_project_members m
                  WHERE m.project_id=p.id AND m.tg_id=$2::bigint AND m.active) AS staff_role
                FROM wl_projects p
                WHERE p.id=$1 AND p.status='active' AND p.bot_id IS NOT NULL""",
                project_id,
                user_id,
            )
        if row is None:
            raise ProjectNotFound("Проект недоступен.")
        result = _public(row)
        if user_id is not None:
            result["_bot_update_context"] = BotUpdateContext(
                project_id=result["id"], bot_id=result["bot"]["id"], user_id=user_id,
                project_version=result["version"],
                registered=row["customer_registered"] is True,
                attribution_frozen=row["attribution_frozen"] is True,
                staff_role=row["staff_role"],
            )
        return result

    async def get_workspace_auth_project(self, project_id):
        """Private exact-tenant auth port. NEVER serialize this result to a client."""
        project_id = _project_id(project_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_PROJECT_COLUMNS}, encrypted_token FROM wl_projects "
                "WHERE id=$1 AND status='active' AND bot_id IS NOT NULL "
                "AND encrypted_token IS NOT NULL",
                project_id,
            )
        if row is None:
            raise ProjectNotFound("Проект недоступен.")
        return {**_public(row), "encrypted_token": row["encrypted_token"]}

    async def update_project(self, project_id, owner_id, expected_version, changes):
        project_id = _project_id(project_id)
        owner_id = _positive_int(owner_id, "owner_id")
        expected_version = _positive_int(expected_version, "version", maximum=2**31 - 2)
        changes = validate_changes(changes)
        fields = sorted(changes)
        values = [
            json.dumps(changes[k], ensure_ascii=False) if k == "brand" else changes[k]
            for k in fields
        ]
        assignments = ", ".join(
            f"{key}=${n + 4}" + ("::jsonb" if key == "brand" else "")
            for n, key in enumerate(fields)
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""UPDATE wl_projects SET {assignments},
                    version=version+1, updated_at=now()
                    WHERE id=$1 AND owner_tg_id=$2 AND version=$3
                    RETURNING {_PROJECT_COLUMNS}""",
                    project_id,
                    owner_id,
                    expected_version,
                    *values,
                )
                if row is None:
                    await self._missing_or_stale(conn, project_id, owner_id)
                await self._audit(
                    conn,
                    project_id,
                    owner_id,
                    "settings_updated",
                    row["version"],
                    fields,
                )
                return _public(row)

    @staticmethod
    async def _missing_or_stale(conn, project_id, owner_id):
        exists = await conn.fetchval(
            "SELECT 1 FROM wl_projects WHERE id=$1 AND owner_tg_id=$2",
            project_id,
            owner_id,
        )
        if not exists:
            raise ProjectNotFound("Проект не найден.")
        raise ProjectConflict(
            "Настройки уже изменились. Обновите страницу и повторите."
        )

    async def save_bot_connection(
        self, project_id, owner_id, expected_version, bot_id, username, encrypted_token
    ):
        project_id = _project_id(project_id)
        owner_id = _positive_int(owner_id, "owner_id")
        expected_version = _positive_int(expected_version, "version", maximum=2**31 - 2)
        bot_id = _positive_int(bot_id, "bot_id")
        if not isinstance(username, str) or not re.fullmatch(
            r"[A-Za-z0-9_]{5,32}", username
        ):
            raise InvalidProject("Некорректное имя бота.")
        # Opaque, already encrypted value supplied by the token connection service.
        if (
            not isinstance(encrypted_token, str)
            or not 32 <= len(encrypted_token) <= 16_384
        ):
            raise InvalidProject("Не удалось безопасно сохранить подключение бота.")
        if re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", encrypted_token):
            raise InvalidProject("Не удалось безопасно сохранить подключение бота.")
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        f"""UPDATE wl_projects SET bot_id=$4, bot_username=$5,
                        encrypted_token=$6, token_updated_at=now(), version=version+1, updated_at=now()
                        WHERE id=$1 AND owner_tg_id=$2 AND version=$3
                        RETURNING {_PROJECT_COLUMNS}""",
                        project_id,
                        owner_id,
                        expected_version,
                        bot_id,
                        username,
                        encrypted_token,
                    )
                    if row is None:
                        await self._missing_or_stale(conn, project_id, owner_id)
                    await self._audit(
                        conn,
                        project_id,
                        owner_id,
                        "bot_connected",
                        row["version"],
                        ["bot"],
                    )
                    return _public(row)
        except asyncpg.UniqueViolationError:
            raise ProjectConflict("Этот бот уже подключён к другому проекту.") from None
        except asyncpg.CheckViolationError as exc:
            if getattr(exc, "constraint_name", None) == "wl_entry_bot_identity_locked":
                raise ProjectConflict(
                    "Бот уже закреплён за вступлением или работающим проектом."
                ) from None
            raise
