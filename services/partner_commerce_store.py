"""Tenant-isolated, durable checkout. No settings, migrations or network on import.

Only a trusted server-side canonical payment verifier may call
record_verified_payment. This module never accepts a client-supplied paid flag.
Financial amounts are integer kopecks; reporting periods use UTC. Activation
jobs freeze absolute deadlines, permitting safe retries after remote failures.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg

from services.partner_catalog import MAX_PRICE_KOPECKS, MIN_PRICE_KOPECKS, split_revenue
from services.partner_traffic_store import PartnerTrafficStoreMixin
from services.partner_subscription_store import PartnerSubscriptionStoreMixin
from services.partner_panel_identity import (
    legacy_identity,
    readable_identity,
    validate_identity,
)

TRAFFIC_LIMIT_BYTES = 300 * 1024**3
DEVICE_LIMIT = 3


class InvalidCommerce(ValueError):
    """Malformed or impermissible commerce input."""


class CommerceNotFound(LookupError):
    """Resource absent or outside the authenticated tenant/customer scope."""


class CommerceConflict(RuntimeError):
    """Immutable request, invoice binding, or activation order conflicts."""


class SalesUnavailable(RuntimeError):
    """Project is not explicitly activated for sales."""


def _uuid(value: Any, name="идентификатор") -> UUID:
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise InvalidCommerce(f"Некорректный {name}.") from None


def _integer(value: Any, name="значение", maximum=2**63 - 1) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise InvalidCommerce(f"Некорректное поле: {name}.")
    return value


def _price(value):
    value = _integer(value, "цена", MAX_PRICE_KOPECKS)
    if value < MIN_PRICE_KOPECKS:
        raise InvalidCommerce("Минимальная цена — 199 ₽.")
    return value


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InvalidCommerce("Время должно содержать часовой пояс.")
    return value.astimezone(timezone.utc)


def _string(value, name, maximum=200):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise InvalidCommerce(f"Некорректное поле: {name}.")
    return value


def _https(value):
    value = _string(value, "HTTPS-ссылка", 2048)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise InvalidCommerce("Некорректная HTTPS-ссылка.") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or port not in (None, 443)
        or "\\" in value
        or any(c.isspace() for c in value)
    ):
        raise InvalidCommerce("Некорректная HTTPS-ссылка.")
    return value


def _public(row):
    return {k: str(v) if isinstance(v, UUID) else v for k, v in dict(row).items()}


def _lock_key(namespace, *values):
    return int.from_bytes(
        hashlib.sha256(":".join([namespace, *map(str, values)]).encode()).digest()[:8],
        "big",
        signed=True,
    )


class _ConnectionLease:
    """Pool-shaped adapter: operations reuse the held lock-owning PG session."""

    def __init__(self, connection):
        self.connection = connection
        self.active = True

    @asynccontextmanager
    async def acquire(self):
        if not self.active:
            raise RuntimeError("Activation session lease has already ended")
        yield self.connection


class PartnerCommerceStore(PartnerTrafficStoreMixin, PartnerSubscriptionStoreMixin):
    def __init__(
        self,
        pool_getter,
        addon_catalog_loader=None,
        *,
        entitlements_enabled=False,
        traffic_cycles_enabled=False,
        invoice_intents_enabled=False,
        panel_identity_enabled=False,
    ):
        if not callable(pool_getter):
            raise TypeError("pool_getter must be callable")
        self._pool_getter = pool_getter
        if addon_catalog_loader is not None and not callable(addon_catalog_loader):
            raise TypeError("addon_catalog_loader must be callable")
        if not isinstance(entitlements_enabled, bool):
            raise TypeError("entitlements_enabled must be bool")
        self._addon_catalog_loader = addon_catalog_loader
        # This is an explicit operator capability, never a public request flag.
        # It requires entitlements.sql to have been applied separately.
        self.entitlements_enabled = entitlements_enabled
        if (
            type(traffic_cycles_enabled) is not bool
            or traffic_cycles_enabled
            and not entitlements_enabled
        ):
            raise TypeError("traffic_cycles_enabled requires entitlements_enabled")
        self.traffic_cycles_enabled = traffic_cycles_enabled
        if (
            type(invoice_intents_enabled) is not bool
            or invoice_intents_enabled
            and not traffic_cycles_enabled
        ):
            raise TypeError("invoice_intents_enabled requires traffic_cycles_enabled")
        self.invoice_intents_enabled = invoice_intents_enabled
        if type(panel_identity_enabled) is not bool:
            raise TypeError("panel_identity_enabled must be bool")
        # Explicit cutover requires panel_identity.sql. Never infer the new
        # format from mutable project branding or an existing remote profile.
        self.panel_identity_enabled = panel_identity_enabled

    def _require_entitlements(self):
        if not self.entitlements_enabled:
            raise SalesUnavailable("Докупки и бонусы этого проекта пока недоступны.")

    async def _addons(self, subscription, now):
        self._require_entitlements()
        if self._addon_catalog_loader is None:
            raise SalesUnavailable("Каталог докупок пока недоступен.")
        value = self._addon_catalog_loader(_public(subscription), now=now)
        value = await value if inspect.isawaitable(value) else value
        if not isinstance(value, dict):
            raise SalesUnavailable("Каталог докупок пока недоступен.")
        return value

    @staticmethod
    async def _device_limit(conn, project_id, subscription_id, now):
        count = await conn.fetchval(
            """SELECT COALESCE(sum(device_count),0)::bigint
            FROM wl_device_grants WHERE project_id=$1 AND subscription_id=$2 AND expires_at>$3""",
            project_id,
            subscription_id,
            now,
        )
        return DEVICE_LIMIT + count

    async def _addon_subscription(
        self, conn, project_id, tg_id, subscription_id, now, *, lock=False
    ):
        row = await conn.fetchrow(
            "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND tg_id=$2 AND id=$3"
            + (" FOR UPDATE" if lock else ""),
            project_id,
            tg_id,
            subscription_id,
        )
        if not row:
            raise CommerceNotFound("Подписка не найдена.")
        if row["expires_at"] is None or row["expires_at"] <= now:
            raise CommerceConflict("Для докупки сначала продлите подписку.")
        if await conn.fetchval(
            """SELECT 1 FROM wl_activation_jobs WHERE project_id=$1
            AND subscription_id=$2 AND status<>'complete'
            AND (blocked_reason IS NULL OR blocked_reason='activation_expired') LIMIT 1""",
            project_id,
            subscription_id,
        ):
            raise CommerceConflict(
                "Оплаченная покупка ещё активируется. Повторите после активации."
            )
        row = dict(row)
        row["device_limit"] = await self._device_limit(
            conn, project_id, subscription_id, now
        )
        return row

    async def get_addon_catalog(self, project_id, tg_id, subscription_id, *, now=None):
        self._require_entitlements()
        project_id, tg_id, subscription_id, now = (
            _uuid(project_id),
            _integer(tg_id, "tg_id"),
            _uuid(subscription_id),
            _now(now),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, selling=True)
            subscription = await self._addon_subscription(
                conn, project_id, tg_id, subscription_id, now
            )
        # Main pricing can itself read a database. Do not retain a partner
        # connection while awaiting it (including a one-connection shared pool).
        return await self._addons(subscription, now)

    async def create_addon_order(
        self,
        project_id,
        tg_id,
        request_key,
        expected_price_kopecks,
        *,
        subscription_id,
        purpose,
        package_id,
        now=None,
    ):
        self._require_entitlements()
        project_id, tg_id, request_key, subscription_id = (
            _uuid(project_id),
            _integer(tg_id, "tg_id"),
            _uuid(request_key, "ключ запроса"),
            _uuid(subscription_id),
        )
        price, now = _integer(expected_price_kopecks, "цена", MAX_PRICE_KOPECKS), _now(
            now
        )
        if purpose not in {"traffic", "devices"}:
            raise InvalidCommerce("Неизвестный вид докупки.")
        if not isinstance(package_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,63}", package_id
        ):
            raise InvalidCommerce("Некорректный пакет докупки.")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "price": price,
                    "subscription_id": str(subscription_id),
                    "purpose": purpose,
                    "package_id": package_id,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        pool = await self._pool()
        async with pool.acquire() as conn:
            previous = await conn.fetchrow(
                "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND request_key=$3",
                project_id,
                tg_id,
                request_key,
            )
            if previous:
                if previous["request_fingerprint"] != fingerprint:
                    raise CommerceConflict(
                        "Этот ключ запроса уже использован для другой покупки."
                    )
                return _public(previous)
            await self._project(conn, project_id, selling=True)
            quoted_subscription = await self._addon_subscription(
                conn, project_id, tg_id, subscription_id, now
            )
        addons = await self._addons(quoted_subscription, now)
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await conn.fetchval(
                    "SELECT 1 FROM wl_customers WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    project_id,
                    tg_id,
                ):
                    raise CommerceNotFound("Сначала откройте бот проекта.")
                existing = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND request_key=$3",
                    project_id,
                    tg_id,
                    request_key,
                )
                if existing:
                    if existing["request_fingerprint"] != fingerprint:
                        raise CommerceConflict(
                            "Этот ключ запроса уже использован для другой покупки."
                        )
                    return _public(existing)
                await self._project(conn, project_id, selling=True)
                subscription = await self._addon_subscription(
                    conn, project_id, tg_id, subscription_id, now, lock=True
                )
                for field in (
                    "expires_at",
                    "desired_expires_at",
                    "device_limit",
                    "traffic_limit_bytes",
                    "last_activation_sequence",
                ):
                    if subscription[field] != quoted_subscription[field]:
                        raise CommerceConflict(
                            "Подписка изменилась во время расчёта. Обновите каталог."
                        )
                if purpose == "traffic":
                    candidates = (
                        addons.get("traffic_packages", [])
                        if addons.get("traffic_status") == "available"
                        else []
                    )
                    quote = next(
                        (
                            p
                            for p in candidates
                            if isinstance(p, dict) and p.get("id") == package_id
                        ),
                        None,
                    )
                    quantity = (
                        _integer(quote.get("gb"), "ГБ", 100000) if quote else None
                    )
                    expiry = None
                else:
                    devices = addons.get("devices", {})
                    candidates = (
                        devices.get("packages", [])
                        if devices.get("addon_status") == "available"
                        else []
                    )
                    quote = next(
                        (
                            p
                            for p in candidates
                            if isinstance(p, dict) and str(p.get("count")) == package_id
                        ),
                        None,
                    )
                    quantity = (
                        _integer(quote.get("count"), "устройства", 3) if quote else None
                    )
                    expiry = subscription["expires_at"]
                    if (
                        quantity is not None
                        and subscription["device_limit"] + quantity > 10
                    ):
                        raise CommerceConflict("Максимум — 10 устройств.")
                    if await conn.fetchval(
                        """SELECT 1 FROM wl_orders WHERE project_id=$1 AND subscription_id=$2
                        AND purpose='devices' AND status='created' LIMIT 1""",
                        project_id,
                        subscription_id,
                    ):
                        raise CommerceConflict(
                            "Сначала завершите существующий заказ устройств."
                        )
                if not quote:
                    raise CommerceConflict(
                        "Этот пакет сейчас недоступен. Обновите каталог."
                    )
                quoted_price = _integer(
                    quote.get("price_kopecks"), "цена каталога", MAX_PRICE_KOPECKS
                )
                if quoted_price != price:
                    raise CommerceConflict(
                        "Цена изменилась. Подтвердите новую стоимость."
                    )
                row = await conn.fetchrow(
                    """INSERT INTO wl_orders
                    (id,project_id,tg_id,subscription_id,request_key,request_fingerprint,price_kopecks,
                     purpose,package_id,addon_quantity,addon_expires_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
                    uuid4(),
                    project_id,
                    tg_id,
                    subscription_id,
                    request_key,
                    fingerprint,
                    price,
                    purpose,
                    package_id,
                    quantity,
                    expiry,
                )
                return _public(row)

    async def _pool(self):
        pool = self._pool_getter()
        return await pool if inspect.isawaitable(pool) else pool

    @staticmethod
    async def _project(conn, project_id, owner_id=None, *, selling=False):
        query = "SELECT id,status,price_kopecks,bot_id FROM wl_projects WHERE id=$1"
        args = [project_id]
        if owner_id is not None:
            query += " AND owner_tg_id=$2"
            args.append(owner_id)
        if selling:
            query += " FOR SHARE"
        row = await conn.fetchrow(query, *args)
        if not row:
            raise CommerceNotFound("Проект не найден.")
        if selling and (row["status"] != "active" or row["bot_id"] is None):
            raise SalesUnavailable("Продажи этого проекта пока недоступны.")
        return row

    async def register_customer(self, project_id, tg_id, referrer_tg_id=None):
        project_id, tg_id = _uuid(project_id), _integer(tg_id, "tg_id")
        if referrer_tg_id is not None:
            referrer_tg_id = _integer(referrer_tg_id, "referrer_tg_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._project(conn, project_id)
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    _lock_key("wl-customer", project_id, tg_id),
                )
                existing = await conn.fetchrow(
                    "SELECT * FROM wl_customers WHERE project_id=$1 AND tg_id=$2",
                    project_id,
                    tg_id,
                )
                if existing:
                    return _public(
                        existing
                    )  # First registration is immutable, including no referrer.
                if referrer_tg_id is not None:
                    if referrer_tg_id == tg_id or not await conn.fetchval(
                        "SELECT 1 FROM wl_customers WHERE project_id=$1 AND tg_id=$2",
                        project_id,
                        referrer_tg_id,
                    ):
                        raise InvalidCommerce("Приглашающий не найден в этом проекте.")
                row = await conn.fetchrow(
                    """INSERT INTO wl_customers(id,project_id,tg_id,referrer_tg_id)
                    VALUES($1,$2,$3,$4) RETURNING *""",
                    uuid4(),
                    project_id,
                    tg_id,
                    referrer_tg_id,
                )
                return _public(row)

    async def get_customer(self, project_id, tg_id):
        project_id, tg_id = _uuid(project_id), _integer(tg_id, "tg_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM wl_customers WHERE project_id=$1 AND tg_id=$2",
                project_id,
                tg_id,
            )
        if not row:
            raise CommerceNotFound("Клиент не найден.")
        return _public(row)

    async def list_subscriptions(self, project_id, tg_id):
        project_id, tg_id = _uuid(project_id), _integer(tg_id, "tg_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND tg_id=$2 ORDER BY created_at,id",
                project_id,
                tg_id,
            )
        return [
            {
                key: value
                for key, value in _public(row).items()
                if key != "remote_identity"
            }
            for row in rows
        ]

    async def create_order(
        self,
        project_id,
        tg_id,
        request_key,
        expected_price_kopecks,
        *,
        subscription_id=None,
    ):
        project_id, tg_id, request_key = (
            _uuid(project_id),
            _integer(tg_id, "tg_id"),
            _uuid(request_key, "ключ запроса"),
        )
        price = _price(expected_price_kopecks)
        subscription_id = (
            _uuid(subscription_id) if subscription_id is not None else None
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "price": price,
                    "subscription_id": (
                        str(subscription_id) if subscription_id else None
                    ),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Customer lock serializes new-vs-renew decisions across workers.
                customer = await conn.fetchrow(
                    "SELECT * FROM wl_customers WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    project_id,
                    tg_id,
                )
                if not customer:
                    raise CommerceNotFound("Сначала откройте бот проекта.")
                existing = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND request_key=$3",
                    project_id,
                    tg_id,
                    request_key,
                )
                if existing:
                    if existing["request_fingerprint"] != fingerprint:
                        raise CommerceConflict(
                            "Этот ключ запроса уже использован для другой покупки."
                        )
                    return _public(
                        existing
                    )  # Retry uses frozen price even if project changed/suspended.
                project = await self._project(conn, project_id, selling=True)
                if project["price_kopecks"] != price:
                    raise CommerceConflict(
                        "Цена изменилась. Подтвердите новую стоимость."
                    )
                slot = await conn.fetchrow(
                    "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    project_id,
                    tg_id,
                )
                if (
                    self.traffic_cycles_enabled
                    and slot
                    and await conn.fetchval(
                        """SELECT 1 FROM wl_activation_jobs WHERE project_id=$1 AND subscription_id=$2
                    AND status<>'complete' AND blocked_reason='activation_expired' LIMIT 1""",
                        project_id,
                        slot["id"],
                    )
                ):
                    raise SalesUnavailable(
                        "Предыдущая оплата требует сверки поддержкой. Новая оплата не нужна."
                    )
                if subscription_id is None:
                    if slot:
                        # A provider-confirmed cancellation keeps the invoice ID
                        # forever but must not trap an unpaid first-purchase slot.
                        reusable = slot[
                            "desired_expires_at"
                        ] is None and await conn.fetchval(
                            """SELECT
                            EXISTS(SELECT 1 FROM wl_orders WHERE project_id=$1 AND subscription_id=$2 AND status='canceled')
                            AND NOT EXISTS(SELECT 1 FROM wl_orders WHERE project_id=$1 AND subscription_id=$2 AND status<>'canceled')""",
                            project_id,
                            slot["id"],
                        )
                        if not reusable:
                            raise CommerceConflict(
                                "Подписка уже создана. Выберите её продление или существующий заказ."
                            )
                        subscription_id = slot["id"]
                    else:
                        subscription_id = uuid4()
                        if self.panel_identity_enabled:
                            metadata = await conn.fetchrow(
                                "SELECT slug,title,bot_username FROM wl_projects WHERE id=$1",
                                project_id,
                            )
                            identity = readable_identity(
                                project_id, subscription_id, tg_id, **dict(metadata)
                            )
                            await conn.execute(
                                "INSERT INTO wl_subscriptions(id,project_id,tg_id,remote_identity) VALUES($1,$2,$3,$4::jsonb)",
                                subscription_id,
                                project_id,
                                tg_id,
                                json.dumps(identity, ensure_ascii=False),
                            )
                        else:
                            await conn.execute(
                                "INSERT INTO wl_subscriptions(id,project_id,tg_id) VALUES($1,$2,$3)",
                                subscription_id,
                                project_id,
                                tg_id,
                            )
                elif not slot or str(slot["id"]) != str(subscription_id):
                    raise CommerceNotFound("Подписка не найдена.")
                elif slot["desired_expires_at"] is None:
                    raise CommerceConflict("Сначала завершите первую покупку подписки.")
                row = await conn.fetchrow(
                    """INSERT INTO wl_orders
                    (id,project_id,tg_id,subscription_id,request_key,request_fingerprint,price_kopecks)
                    VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
                    uuid4(),
                    project_id,
                    tg_id,
                    subscription_id,
                    request_key,
                    fingerprint,
                    price,
                )
                return _public(row)

    async def get_order(self, project_id, tg_id, order_id):
        project_id, tg_id, order_id = (
            _uuid(project_id),
            _integer(tg_id, "tg_id"),
            _uuid(order_id),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND id=$3",
                project_id,
                tg_id,
                order_id,
            )
        if not row:
            raise CommerceNotFound("Заказ не найден.")
        return _public(row)

    async def get_pending_order(self, project_id, tg_id):
        """Recover a saved checkout after a lost message/process restart."""
        project_id, tg_id = _uuid(project_id), _integer(tg_id, "tg_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2
                AND status IN ('created','paid_pending_activation')
                ORDER BY (status='paid_pending_activation') DESC,created_at DESC,id DESC LIMIT 1""",
                project_id,
                tg_id,
            )
        return _public(row) if row else None

    async def get_checkout_order(self, project_id, tg_id, order_id, *, now=None):
        """Preflight only a NEW invoice; bound invoices remain reconcilable."""
        project_id, tg_id, order_id, now = (
            _uuid(project_id),
            _integer(tg_id, "tg_id"),
            _uuid(order_id),
            _now(now),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND id=$3",
                project_id,
                tg_id,
                order_id,
            )
            if not row:
                raise CommerceNotFound("Заказ не найден.")
            if row["status"] != "created" or row["provider_payment_id"] is not None:
                return _public(row)
            if self.invoice_intents_enabled and row["invoice_requested_at"] is not None:
                return _public(
                    row
                )  # Recover the frozen prior request, not a new quote.
            await self._project(conn, project_id, selling=True)
            if dict(row).get("purpose", "subscription") != "subscription":
                self._require_entitlements()
                await self._addon_subscription(
                    conn, project_id, tg_id, row["subscription_id"], now
                )
                if (
                    row["addon_expires_at"] is not None
                    and row["addon_expires_at"] <= now
                ):
                    raise CommerceConflict("Срок докупки истёк. Нужен новый расчёт.")
            return _public(row)

    async def begin_invoice_creation(self, project_id, tg_id, order_id, *, now=None):
        if not self.invoice_intents_enabled:
            raise SalesUnavailable("Сохранение намерения оплаты пока не включено.")
        project_id, tg_id, order_id, now = (
            _uuid(project_id),
            _integer(tg_id, "tg_id"),
            _uuid(order_id),
            _now(now),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND id=$3 FOR UPDATE",
                    project_id,
                    tg_id,
                    order_id,
                )
                if not row:
                    raise CommerceNotFound("Заказ не найден.")
                if row["status"] != "created" or row["provider_payment_id"] is not None:
                    return _public(row)
                if row["invoice_requested_at"] is None:
                    await self._project(conn, project_id, selling=True)
                    if row["purpose"] != "subscription":
                        self._require_entitlements()
                        await self._addon_subscription(
                            conn,
                            project_id,
                            tg_id,
                            row["subscription_id"],
                            now,
                            lock=True,
                        )
                        if (
                            row["addon_expires_at"] is not None
                            and row["addon_expires_at"] <= now
                        ):
                            raise CommerceConflict(
                                "Срок докупки истёк. Нужен новый расчёт."
                            )
                    row = await conn.fetchrow(
                        "UPDATE wl_orders SET invoice_requested_at=$4 WHERE project_id=$1 AND tg_id=$2 AND id=$3 RETURNING *",
                        project_id,
                        tg_id,
                        order_id,
                        now,
                    )
                return _public(row)

    async def cancel_uninvoiced_order(self, project_id, tg_id, order_id):
        if not self.invoice_intents_enabled:
            raise SalesUnavailable("Отмена черновиков пока не включена.")
        project_id, tg_id, order_id = (
            _uuid(project_id),
            _integer(tg_id, "tg_id"),
            _uuid(order_id),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND id=$3 FOR UPDATE",
                    project_id,
                    tg_id,
                    order_id,
                )
                if not row:
                    raise CommerceNotFound("Заказ не найден.")
                if row["status"] == "canceled" and row["provider_payment_id"] is None:
                    return _public(row)
                if (
                    row["status"] != "created"
                    or row["provider_payment_id"] is not None
                    or row["invoice_requested_at"] is not None
                ):
                    raise CommerceConflict(
                        "Счёт уже создаётся или оплачен. Сначала проверьте сохранённый заказ."
                    )
                return _public(
                    await conn.fetchrow(
                        "UPDATE wl_orders SET status='canceled' WHERE project_id=$1 AND tg_id=$2 AND id=$3 RETURNING *",
                        project_id,
                        tg_id,
                        order_id,
                    )
                )

    async def save_invoice(
        self, project_id, tg_id, order_id, provider_payment_id, confirmation_url
    ):
        project_id, tg_id, order_id = (
            _uuid(project_id),
            _integer(tg_id, "tg_id"),
            _uuid(order_id),
        )
        provider_payment_id = _string(provider_payment_id, "платёж")
        confirmation_url = "" if confirmation_url == "" else _https(confirmation_url)
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    order = await conn.fetchrow(
                        "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND id=$3 FOR UPDATE",
                        project_id,
                        tg_id,
                        order_id,
                    )
                    if not order:
                        raise CommerceNotFound("Заказ не найден.")
                    if order["provider_payment_id"] is not None:
                        if (
                            order["provider_payment_id"],
                            order["confirmation_url"],
                        ) != (provider_payment_id, confirmation_url):
                            raise CommerceConflict("Заказ уже связан с другим счётом.")
                        return _public(order)
                    if order["status"] != "created":
                        raise CommerceConflict(
                            "Счёт нельзя привязать к завершённому заказу."
                        )
                    return _public(
                        await conn.fetchrow(
                            """UPDATE wl_orders SET provider_payment_id=$4,confirmation_url=$5
                        WHERE project_id=$1 AND tg_id=$2 AND id=$3 RETURNING *""",
                            project_id,
                            tg_id,
                            order_id,
                            provider_payment_id,
                            confirmation_url,
                        )
                    )
        except asyncpg.UniqueViolationError:
            raise CommerceConflict("Этот платёж уже связан с другим заказом.") from None

    async def cancel_verified_invoice(self, project_id, order_id, provider_payment_id):
        """Trusted canonical provider cancellation only; paid money is immutable."""
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        provider_payment_id = _string(provider_payment_id, "платёж")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                order = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND id=$2 FOR UPDATE",
                    project_id,
                    order_id,
                )
                if not order:
                    raise CommerceNotFound("Заказ не найден.")
                if order["provider_payment_id"] != provider_payment_id:
                    raise CommerceConflict(
                        "Отмена не соответствует сохранённому счёту."
                    )
                if order["status"] != "created":
                    return _public(order)
                return _public(
                    await conn.fetchrow(
                        "UPDATE wl_orders SET status='canceled' WHERE project_id=$1 AND id=$2 RETURNING *",
                        project_id,
                        order_id,
                    )
                )

    async def record_verified_payment(
        self,
        project_id,
        order_id,
        provider_payment_id,
        amount_kopecks,
        currency,
        *,
        now=None,
    ):
        if self.entitlements_enabled:
            return await self._record_entitlement_payment(
                project_id,
                order_id,
                provider_payment_id,
                amount_kopecks,
                currency,
                now=now,
            )
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        provider_payment_id = _string(provider_payment_id, "платёж")
        amount_kopecks, now = _price(amount_kopecks), _now(now)
        if currency != "RUB":
            raise InvalidCommerce("Принимаются только подтверждённые оплаты в RUB.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                order = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND id=$2 FOR UPDATE",
                    project_id,
                    order_id,
                )
                if not order:
                    raise CommerceNotFound("Заказ не найден.")
                if (
                    order["provider_payment_id"] != provider_payment_id
                    or order["price_kopecks"] != amount_kopecks
                ):
                    raise CommerceConflict(
                        "Подтверждённый платёж не соответствует сохранённому счёту."
                    )
                if order["status"] == "canceled":
                    raise CommerceConflict(
                        "Платёж относится к ранее отменённому счёту; нужна ручная сверка."
                    )
                if order["status"] != "created":
                    return _public(order)
                subscription = await conn.fetchrow(
                    "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND id=$2 FOR UPDATE",
                    project_id,
                    order["subscription_id"],
                )
                deadline = max(
                    now, subscription["desired_expires_at"] or now
                ) + timedelta(days=30)
                await conn.execute(
                    "UPDATE wl_subscriptions SET desired_expires_at=$3,updated_at=$4 WHERE project_id=$1 AND id=$2",
                    project_id,
                    subscription["id"],
                    deadline,
                    now,
                )
                await conn.execute(
                    """INSERT INTO wl_activation_jobs(project_id,order_id,subscription_id,expires_at,traffic_limit_bytes,device_limit)
                    VALUES($1,$2,$3,$4,$5,$6)""",
                    project_id,
                    order_id,
                    subscription["id"],
                    deadline,
                    subscription["traffic_limit_bytes"],
                    subscription["device_limit"],
                )
                shares = split_revenue(amount_kopecks)
                await conn.execute(
                    """INSERT INTO wl_revenue_ledger(project_id,order_id,amount_kopecks,partner_kopecks,platform_kopecks,created_at)
                    VALUES($1,$2,$3,$4,$5,$6)""",
                    project_id,
                    order_id,
                    amount_kopecks,
                    shares["partner_kopecks"],
                    shares["platform_kopecks"],
                    now,
                )
                await conn.execute(
                    """INSERT INTO wl_customer_outbox(project_id,order_id,tg_id,kind)
                    VALUES($1,$2,$3,'purchase_paid')""",
                    project_id,
                    order_id,
                    order["tg_id"],
                )
                row = await conn.fetchrow(
                    "UPDATE wl_orders SET status='paid_pending_activation',paid_at=$3 WHERE project_id=$1 AND id=$2 RETURNING *",
                    project_id,
                    order_id,
                    now,
                )
                return _public(row)

    async def _activation(self, conn, project_id, order_id, *, lock=False):
        job = await conn.fetchrow(
            "SELECT * FROM wl_activation_jobs WHERE project_id=$1 AND order_id=$2"
            + (" FOR UPDATE" if lock else ""),
            project_id,
            order_id,
        )
        if not job:
            raise CommerceNotFound("Подтверждённая оплата для активации не найдена.")
        if self.entitlements_enabled and job["blocked_reason"] is not None:
            raise CommerceConflict("Оплаченная докупка требует сверки поддержкой.")
        if job["status"] != "complete" and await conn.fetchval(
            """SELECT 1 FROM wl_activation_jobs
            WHERE project_id=$1 AND subscription_id=$2 AND sequence<$3 AND status<>'complete'"""
            + (
                " AND (blocked_reason IS NULL OR blocked_reason='activation_expired')"
                if self.entitlements_enabled
                else ""
            )
            + " LIMIT 1",
            project_id,
            job["subscription_id"],
            job["sequence"],
        ):
            raise CommerceConflict("Предыдущая оплаченная покупка ещё активируется.")
        return job

    async def get_activation(self, project_id, order_id):
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, selling=True)
            job = _public(await self._activation(conn, project_id, order_id))
            job["remote_user_id"] = await conn.fetchval(
                "SELECT remote_user_id FROM wl_subscriptions WHERE project_id=$1 AND id=$2",
                project_id,
                UUID(job["subscription_id"]),
            )
            await self._traffic_activation_floor(conn, job)
            await self._set_panel_identity(conn, job)
        return job

    async def _set_panel_identity(self, conn, context):
        """Only trusted provisioning/usage contexts receive the private snapshot."""
        pid, sid = _uuid(context["project_id"]), _uuid(context["subscription_id"])
        subscription = await conn.fetchrow(
            "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND id=$2", pid, sid
        )
        if not subscription:
            raise CommerceNotFound("Подписка не найдена.")
        identity = dict(subscription).get("remote_identity")
        if identity is not None:
            identity = validate_identity(identity, pid, sid)
            context["remote_identity"] = identity
        elif self.panel_identity_enabled:
            raise CommerceConflict("Идентификатор подписки требует сверки поддержкой.")
        else:
            identity = legacy_identity(pid, sid)
        context["remote_username"] = identity["username"]

    @asynccontextmanager
    async def acquire_activation_lock(self, project_id, subscription_id):
        """Cross-process lock held on a dedicated PG session across remote ensure.

        Use the yielded store for ALL operations inside this context. They reuse
        the held lock-owning connection, so even pool size one is safe. The lease
        expires on context exit; never retain it for later/background work.
        """
        project_id, subscription_id = _uuid(project_id), _uuid(subscription_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            if not await conn.fetchval(
                "SELECT 1 FROM wl_subscriptions WHERE project_id=$1 AND id=$2",
                project_id,
                subscription_id,
            ):
                raise CommerceNotFound("Подписка не найдена.")
            key = _lock_key("wl-activation", project_id, subscription_id)
            await conn.execute("SELECT pg_advisory_lock($1)", key)
            lease = _ConnectionLease(conn)
            try:
                yield PartnerCommerceStore(
                    lambda: lease,
                    self._addon_catalog_loader,
                    entitlements_enabled=self.entitlements_enabled,
                    traffic_cycles_enabled=self.traffic_cycles_enabled,
                    invoice_intents_enabled=self.invoice_intents_enabled,
                    panel_identity_enabled=self.panel_identity_enabled,
                )
            finally:
                lease.active = False
                await conn.execute("SELECT pg_advisory_unlock($1)", key)

    async def complete_activation(
        self, project_id, order_id, *, remote_user_id, subscription_url
    ):
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        remote_user_id, subscription_url = _string(
            remote_user_id, "remote_user_id"
        ), _https(subscription_url)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                job = await self._activation(conn, project_id, order_id, lock=True)
                subscription = await conn.fetchrow(
                    "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND id=$2 FOR UPDATE",
                    project_id,
                    job["subscription_id"],
                )
                if job["status"] == "complete":
                    return _public(subscription)
                if subscription["last_activation_sequence"] >= job["sequence"]:
                    raise CommerceConflict("Более новая активация уже применена.")
                if (
                    subscription["remote_user_id"] is not None
                    and subscription["remote_user_id"] != remote_user_id
                ):
                    raise CommerceConflict(
                        "Изменение удалённого пользователя подписки запрещено."
                    )
                row = await conn.fetchrow(
                    """UPDATE wl_subscriptions SET expires_at=$3,remote_user_id=$4,
                    subscription_url=$5,last_activation_sequence=$6,updated_at=now()
                    WHERE project_id=$1 AND id=$2 RETURNING *""",
                    project_id,
                    subscription["id"],
                    job["expires_at"],
                    remote_user_id,
                    subscription_url,
                    job["sequence"],
                )
                await conn.execute(
                    "UPDATE wl_activation_jobs SET status='complete',completed_at=now(),attempts=attempts+1 WHERE project_id=$1 AND order_id=$2",
                    project_id,
                    order_id,
                )
                await conn.execute(
                    "UPDATE wl_orders SET status='active' WHERE project_id=$1 AND id=$2",
                    project_id,
                    order_id,
                )
                if self.traffic_cycles_enabled:
                    await self._traffic_job_completed(conn, project_id, job["job_id"])
                return _public(row)

    async def mark_activation_expired(self, project_id, job_id, *, now=None):
        """Persist a manual stop, not an invented extension or a completed grant."""
        self._require_traffic()
        project_id, job_id, now = _uuid(project_id), _uuid(job_id), _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM wl_activation_jobs WHERE project_id=$1 AND job_id=$2 FOR UPDATE",
                    project_id,
                    job_id,
                )
                if not row:
                    raise CommerceNotFound("Активация не найдена.")
                if row["status"] == "complete":
                    return _public(row)
                if row["expires_at"] > now:
                    raise CommerceConflict("Срок этой задачи ещё не истёк.")
                row = await conn.fetchrow(
                    """UPDATE wl_activation_jobs SET blocked_reason='activation_expired',status='retry'
                    WHERE project_id=$1 AND job_id=$2 RETURNING *""",
                    project_id,
                    job_id,
                )
                if row["order_id"]:
                    await conn.execute(
                        "UPDATE wl_orders SET activation_issue='activation_expired' WHERE project_id=$1 AND id=$2 AND status='paid_pending_activation'",
                        project_id,
                        row["order_id"],
                    )
        return _public(row)

    async def mark_activation_retry(self, project_id, order_id, *, now=None):
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            schedule = (
                ",next_attempt_at=$3::timestamptz+CASE WHEN attempts<3 THEN interval '30 seconds' ELSE interval '5 minutes' END"
                if self.traffic_cycles_enabled
                else ""
            )
            args = [project_id, order_id] + (
                [_now(now)] if self.traffic_cycles_enabled else []
            )
            row = await conn.fetchrow(
                "UPDATE wl_activation_jobs SET status='retry',attempts=attempts+1"
                + schedule
                + " WHERE project_id=$1 AND order_id=$2 AND status<>'complete' RETURNING *",
                *args,
            )
            if not row:
                row = await conn.fetchrow(
                    "SELECT * FROM wl_activation_jobs WHERE project_id=$1 AND order_id=$2",
                    project_id,
                    order_id,
                )
            if not row:
                raise CommerceNotFound("Активация не найдена.")
        return _public(row)

    @staticmethod
    async def _expiry_job(conn, project_id, subscription, now, *, deadline=None):
        """Queue one absolute correction for every expired unprocessed grant."""
        grants = await conn.fetch(
            """SELECT * FROM wl_device_grants WHERE project_id=$1
            AND subscription_id=$2 AND expires_at<=$3 AND expiry_job_id IS NULL FOR UPDATE""",
            project_id,
            subscription["id"],
            now,
        )
        if not grants:
            return None
        deadline = deadline or subscription["desired_expires_at"]
        if deadline is None or deadline <= now:
            return None  # An expired subscription is corrected before its next paid renewal.
        limit = await PartnerCommerceStore._device_limit(
            conn, project_id, subscription["id"], now
        )
        job_id = uuid4()
        job = await conn.fetchrow(
            """INSERT INTO wl_activation_jobs
            (job_id,project_id,subscription_id,kind,source_id,expires_at,traffic_limit_bytes,device_limit)
            VALUES($1,$2,$3,'device_expiry',$4,$5,$6,$7) RETURNING *""",
            job_id,
            project_id,
            subscription["id"],
            uuid4(),
            deadline,
            subscription["traffic_limit_bytes"],
            limit,
        )
        await conn.execute(
            """UPDATE wl_device_grants SET expiry_job_id=$4
            WHERE project_id=$1 AND subscription_id=$2 AND expires_at<=$3 AND expiry_job_id IS NULL""",
            project_id,
            subscription["id"],
            now,
            job_id,
        )
        await conn.execute(
            "UPDATE wl_subscriptions SET device_limit=$3 WHERE project_id=$1 AND id=$2",
            project_id,
            subscription["id"],
            limit,
        )
        return _public(job)

    async def _record_entitlement_payment(
        self,
        project_id,
        order_id,
        provider_payment_id,
        amount_kopecks,
        currency,
        *,
        now=None,
    ):
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        provider_payment_id = _string(provider_payment_id, "платёж")
        amount_kopecks, now = _integer(amount_kopecks, "цена", MAX_PRICE_KOPECKS), _now(
            now
        )
        if currency != "RUB":
            raise InvalidCommerce("Принимаются только подтверждённые оплаты в RUB.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Match create_order's customer -> order -> subscription lock
                # ordering; concurrent first payments cannot both earn +7 days.
                original = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND id=$2",
                    project_id,
                    order_id,
                )
                if not original:
                    raise CommerceNotFound("Заказ не найден.")
                customer = await conn.fetchrow(
                    "SELECT * FROM wl_customers WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    project_id,
                    original["tg_id"],
                )
                order = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND id=$2 FOR UPDATE",
                    project_id,
                    order_id,
                )
                if (
                    order["provider_payment_id"] != provider_payment_id
                    or order["price_kopecks"] != amount_kopecks
                ):
                    raise CommerceConflict(
                        "Подтверждённый платёж не соответствует сохранённому счёту."
                    )
                if order["status"] == "canceled":
                    raise CommerceConflict(
                        "Платёж отменённого счёта требует ручной сверки."
                    )
                if order["status"] != "created":
                    return _public(order)
                subscription = await conn.fetchrow(
                    "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND id=$2 FOR UPDATE",
                    project_id,
                    order["subscription_id"],
                )
                purpose = order["purpose"]
                first = purpose == "subscription" and not await conn.fetchval(
                    """SELECT 1 FROM wl_orders
                    WHERE project_id=$1 AND tg_id=$2 AND purpose='subscription' AND paid_at IS NOT NULL LIMIT 1""",
                    project_id,
                    order["tg_id"],
                )
                deadline = subscription["desired_expires_at"]
                if purpose == "subscription":
                    deadline = max(now, deadline or now) + timedelta(days=30)
                issue = None
                if purpose != "subscription" and (deadline is None or deadline <= now):
                    issue = "addon_expired"
                if purpose == "devices" and order["addon_expires_at"] <= now:
                    issue = "addon_expired"
                devices = await self._device_limit(
                    conn, project_id, subscription["id"], now
                )
                if purpose == "devices" and devices + order["addon_quantity"] > 10:
                    issue = "device_limit"
                if not issue:
                    await self._expiry_job(
                        conn, project_id, subscription, now, deadline=deadline
                    )
                traffic = subscription["traffic_limit_bytes"]
                if purpose == "traffic" and not issue:
                    traffic += order["addon_quantity"] * 1024**3
                    if traffic > 2**53 - 1:
                        raise CommerceConflict("Лимит трафика требует ручной проверки.")
                if purpose == "devices" and not issue:
                    await conn.execute(
                        """INSERT INTO wl_device_grants(project_id,order_id,subscription_id,device_count,expires_at)
                        VALUES($1,$2,$3,$4,$5)""",
                        project_id,
                        order_id,
                        subscription["id"],
                        order["addon_quantity"],
                        order["addon_expires_at"],
                    )
                    devices += order["addon_quantity"]
                if not issue:
                    await conn.execute(
                        """UPDATE wl_subscriptions SET desired_expires_at=$3,
                        traffic_limit_bytes=$4,device_limit=$5,updated_at=$6 WHERE project_id=$1 AND id=$2""",
                        project_id,
                        subscription["id"],
                        deadline,
                        traffic,
                        devices,
                        now,
                    )
                    await conn.execute(
                        """INSERT INTO wl_activation_jobs
                        (project_id,order_id,subscription_id,expires_at,traffic_limit_bytes,device_limit)
                        VALUES($1,$2,$3,$4,$5,$6)""",
                        project_id,
                        order_id,
                        subscription["id"],
                        deadline,
                        traffic,
                        devices,
                    )
                await self._traffic_order_recorded(
                    conn,
                    project_id,
                    order,
                    subscription,
                    now,
                    issue=issue,
                    deadline=deadline,
                )
                shares = split_revenue(amount_kopecks)
                await conn.execute(
                    """INSERT INTO wl_revenue_ledger(project_id,order_id,amount_kopecks,partner_kopecks,platform_kopecks,created_at)
                    VALUES($1,$2,$3,$4,$5,$6)""",
                    project_id,
                    order_id,
                    amount_kopecks,
                    shares["partner_kopecks"],
                    shares["platform_kopecks"],
                    now,
                )
                await conn.execute(
                    "INSERT INTO wl_customer_outbox(project_id,order_id,tg_id,kind) VALUES($1,$2,$3,'purchase_paid')",
                    project_id,
                    order_id,
                    order["tg_id"],
                )
                if first and customer["referrer_tg_id"] is not None:
                    await conn.execute(
                        """INSERT INTO wl_referral_bonus_grants
                        (id,project_id,referred_tg_id,referrer_tg_id,source_order_id,created_at)
                        VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(project_id,referred_tg_id) DO NOTHING""",
                        uuid4(),
                        project_id,
                        order["tg_id"],
                        customer["referrer_tg_id"],
                        order_id,
                        now,
                    )
                return _public(
                    await conn.fetchrow(
                        """UPDATE wl_orders SET status='paid_pending_activation',
                    paid_at=$3,activation_issue=$4 WHERE project_id=$1 AND id=$2 RETURNING *""",
                        project_id,
                        order_id,
                        now,
                        issue,
                    )
                )

    async def get_referral_summary(self, project_id, tg_id):
        self._require_entitlements()
        project_id, tg_id = _uuid(project_id), _integer(tg_id, "tg_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            if not await conn.fetchval(
                "SELECT 1 FROM wl_customers WHERE project_id=$1 AND tg_id=$2",
                project_id,
                tg_id,
            ):
                raise CommerceNotFound("Клиент не найден.")
            invited = await conn.fetchval(
                "SELECT count(*) FROM wl_customers WHERE project_id=$1 AND referrer_tg_id=$2",
                project_id,
                tg_id,
            )
            row = await conn.fetchrow(
                """SELECT count(*) AS qualified_count,COALESCE(sum(g.days),0)::bigint AS earned_days,
                COALESCE(sum(g.days) FILTER(WHERE a.status='complete'),0)::bigint AS applied_days
                FROM wl_referral_bonus_grants g LEFT JOIN wl_activation_jobs a ON a.project_id=g.project_id AND a.job_id=g.activation_job_id
                WHERE g.project_id=$1 AND g.referrer_tg_id=$2""",
                project_id,
                tg_id,
            )
        return {
            "invited_count": invited,
            **dict(row),
            "pending_days": row["earned_days"] - row["applied_days"],
            "referral_tg_id": tg_id,
        }

    async def list_bonus_customers(self, limit=50):
        self._require_entitlements()
        limit = _integer(limit, "limit", 100)
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT g.project_id,g.referrer_tg_id AS tg_id
                FROM wl_referral_bonus_grants g JOIN wl_projects p ON p.id=g.project_id
                JOIN wl_subscriptions s ON s.project_id=g.project_id AND s.tg_id=g.referrer_tg_id
                WHERE g.activation_job_id IS NULL AND p.status='active' AND s.desired_expires_at IS NOT NULL
                AND EXISTS(SELECT 1 FROM wl_orders o WHERE o.project_id=g.project_id
                    AND o.tg_id=g.referrer_tg_id AND o.purpose='subscription' AND o.paid_at IS NOT NULL)
                GROUP BY g.project_id,g.referrer_tg_id ORDER BY min(g.created_at) LIMIT $1""",
                limit,
            )
        return [_public(row) for row in rows]

    async def queue_referral_bonus_activation(self, project_id, tg_id, *, now=None):
        self._require_entitlements()
        project_id, tg_id, now = _uuid(project_id), _integer(tg_id, "tg_id"), _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._project(conn, project_id, selling=True)
                subscription = await conn.fetchrow(
                    "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    project_id,
                    tg_id,
                )
                if not subscription or subscription["desired_expires_at"] is None:
                    return None
                if not await conn.fetchval(
                    """SELECT 1 FROM wl_orders WHERE project_id=$1 AND tg_id=$2
                    AND purpose='subscription' AND paid_at IS NOT NULL LIMIT 1""",
                    project_id,
                    tg_id,
                ):
                    return None
                grants = await conn.fetch(
                    """SELECT * FROM wl_referral_bonus_grants WHERE project_id=$1
                    AND referrer_tg_id=$2 AND activation_job_id IS NULL ORDER BY created_at,id FOR UPDATE""",
                    project_id,
                    tg_id,
                )
                if not grants:
                    return None
                deadline = max(now, subscription["desired_expires_at"]) + timedelta(
                    days=sum(g["days"] for g in grants)
                )
                await self._expiry_job(
                    conn, project_id, subscription, now, deadline=deadline
                )
                devices = await self._device_limit(
                    conn, project_id, subscription["id"], now
                )
                job_id = uuid4()
                job = await conn.fetchrow(
                    """INSERT INTO wl_activation_jobs
                    (job_id,project_id,subscription_id,kind,source_id,expires_at,traffic_limit_bytes,device_limit)
                    VALUES($1,$2,$3,'referral_bonus',$4,$5,$6,$7) RETURNING *""",
                    job_id,
                    project_id,
                    subscription["id"],
                    grants[0]["id"],
                    deadline,
                    subscription["traffic_limit_bytes"],
                    devices,
                )
                await conn.execute(
                    """UPDATE wl_referral_bonus_grants SET activation_job_id=$3
                    WHERE project_id=$1 AND referrer_tg_id=$2 AND activation_job_id IS NULL""",
                    project_id,
                    tg_id,
                    job_id,
                )
                await conn.execute(
                    "UPDATE wl_subscriptions SET desired_expires_at=$3,device_limit=$4,updated_at=$5 WHERE project_id=$1 AND id=$2",
                    project_id,
                    subscription["id"],
                    deadline,
                    devices,
                    now,
                )
                return _public(job)

    async def queue_expired_device_activations(self, *, now=None, limit=50):
        self._require_entitlements()
        now, limit = _now(now), _integer(limit, "limit", 100)
        pool = await self._pool()
        result = []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT DISTINCT g.project_id,g.subscription_id FROM wl_device_grants g
                JOIN wl_subscriptions s ON s.project_id=g.project_id AND s.id=g.subscription_id
                JOIN wl_projects p ON p.id=g.project_id WHERE g.expires_at<=$1 AND g.expiry_job_id IS NULL
                AND s.desired_expires_at>$1 AND p.status='active' LIMIT $2""",
                now,
                limit,
            )
            for row in rows:
                async with conn.transaction():
                    sub = await conn.fetchrow(
                        "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND id=$2 FOR UPDATE",
                        row["project_id"],
                        row["subscription_id"],
                    )
                    job = await self._expiry_job(conn, row["project_id"], sub, now)
                    if job:
                        result.append(
                            {
                                key: job[key]
                                for key in ("project_id", "subscription_id", "job_id")
                            }
                        )
        return result

    async def list_entitlement_activations(self, limit=50, *, now=None):
        self._require_entitlements()
        limit = _integer(limit, "limit", 100)
        pool = await self._pool()
        async with pool.acquire() as conn:
            args = [limit] + ([_now(now)] if self.traffic_cycles_enabled else [])
            schedule = (
                """ AND a.next_attempt_at<=$2 AND NOT EXISTS(SELECT 1 FROM wl_activation_jobs older
                WHERE older.project_id=a.project_id AND older.subscription_id=a.subscription_id
                AND older.sequence<a.sequence AND older.status<>'complete'
                AND (older.blocked_reason IS NULL OR older.blocked_reason='activation_expired'))"""
                if self.traffic_cycles_enabled
                else ""
            )
            ordering = (
                " ORDER BY (a.kind='traffic_cycle') DESC,(a.status='pending') DESC,a.next_attempt_at,a.sequence"
                if self.traffic_cycles_enabled
                else " ORDER BY a.sequence"
            )
            rows = await conn.fetch(
                """SELECT a.project_id,a.subscription_id,a.job_id FROM wl_activation_jobs a
                JOIN wl_projects p ON p.id=a.project_id WHERE a.kind<>'order' AND a.status<>'complete'
                AND p.status='active' AND a.blocked_reason IS NULL"""
                + (
                    " AND a.kind<>'traffic_cycle'"
                    if not self.traffic_cycles_enabled
                    else ""
                )
                + schedule
                + ordering
                + " LIMIT $1",
                *args,
            )
        return [_public(row) for row in rows]

    async def _entitlement_job(self, conn, project_id, job_id, *, lock=False):
        row = await conn.fetchrow(
            """SELECT * FROM wl_activation_jobs WHERE project_id=$1 AND job_id=$2
            AND kind<>'order'"""
            + (" FOR UPDATE" if lock else ""),
            project_id,
            job_id,
        )
        if not row:
            raise CommerceNotFound("Начисление не найдено.")
        if row["blocked_reason"] is not None:
            raise CommerceConflict("Активация требует сверки поддержкой.")
        if row["status"] != "complete" and await conn.fetchval(
            """SELECT 1 FROM wl_activation_jobs
            WHERE project_id=$1 AND subscription_id=$2 AND sequence<$3 AND status<>'complete'
            AND (blocked_reason IS NULL OR blocked_reason='activation_expired') LIMIT 1""",
            project_id,
            row["subscription_id"],
            row["sequence"],
        ):
            raise CommerceConflict("Предыдущая покупка ещё активируется.")
        return row

    async def get_entitlement_activation(self, project_id, job_id):
        self._require_entitlements()
        project_id, job_id = _uuid(project_id), _uuid(job_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, selling=True)
            row = _public(await self._entitlement_job(conn, project_id, job_id))
            if row["kind"] == "traffic_cycle" and not self.traffic_cycles_enabled:
                raise SalesUnavailable("Месячный учёт трафика временно выключен.")
            row["remote_user_id"] = await conn.fetchval(
                "SELECT remote_user_id FROM wl_subscriptions WHERE project_id=$1 AND id=$2",
                project_id,
                UUID(row["subscription_id"]),
            )
            await self._traffic_activation_floor(conn, row)
            await self._set_panel_identity(conn, row)
        return row

    async def complete_entitlement_activation(
        self, project_id, job_id, *, remote_user_id, subscription_url
    ):
        self._require_entitlements()
        project_id, job_id = _uuid(project_id), _uuid(job_id)
        remote_user_id, subscription_url = _string(
            remote_user_id, "remote_user_id"
        ), _https(subscription_url)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                job = await self._entitlement_job(conn, project_id, job_id, lock=True)
                sub = await conn.fetchrow(
                    "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND id=$2 FOR UPDATE",
                    project_id,
                    job["subscription_id"],
                )
                if job["status"] == "complete":
                    return _public(sub)
                if sub["last_activation_sequence"] >= job["sequence"] or sub[
                    "remote_user_id"
                ] not in (None, remote_user_id):
                    raise CommerceConflict(
                        "Начисление требует проверки удалённой подписки."
                    )
                row = await conn.fetchrow(
                    """UPDATE wl_subscriptions SET expires_at=$3,remote_user_id=$4,
                    subscription_url=$5,last_activation_sequence=$6,updated_at=now() WHERE project_id=$1 AND id=$2 RETURNING *""",
                    project_id,
                    sub["id"],
                    job["expires_at"],
                    remote_user_id,
                    subscription_url,
                    job["sequence"],
                )
                await conn.execute(
                    "UPDATE wl_activation_jobs SET status='complete',completed_at=now(),attempts=attempts+1 WHERE project_id=$1 AND job_id=$2",
                    project_id,
                    job_id,
                )
                await self._traffic_job_completed(conn, project_id, job_id)
                return _public(row)

    async def mark_entitlement_activation_retry(self, project_id, job_id, *, now=None):
        self._require_entitlements()
        project_id, job_id = _uuid(project_id), _uuid(job_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            schedule = (
                ",next_attempt_at=$3::timestamptz+CASE WHEN attempts<3 THEN interval '30 seconds' ELSE interval '5 minutes' END"
                if self.traffic_cycles_enabled
                else ""
            )
            args = [project_id, job_id] + (
                [_now(now)] if self.traffic_cycles_enabled else []
            )
            row = await conn.fetchrow(
                "UPDATE wl_activation_jobs SET status='retry',attempts=attempts+1"
                + schedule
                + " WHERE project_id=$1 AND job_id=$2 AND kind<>'order' AND status<>'complete' RETURNING *",
                *args,
            )
        return _public(row) if row else None

    async def list_orders(self, project_id, owner_id, limit=100):
        project_id, owner_id, limit = (
            _uuid(project_id),
            _integer(owner_id, "owner_id"),
            _integer(limit, "limit", 500),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, owner_id)
            rows = await conn.fetch(
                "SELECT * FROM wl_orders WHERE project_id=$1 ORDER BY created_at DESC,id DESC LIMIT $2",
                project_id,
                limit,
            )
        return [_public(row) for row in rows]

    async def list_recoverable_orders(self, limit=50, *, now=None):
        """Internal recovery worklist only. No grants, payment checks, or writes.

        A trusted worker must still re-fetch provider state and acquire the
        subscription activation lock before performing external work.
        """
        limit = _integer(limit, "limit", 100)
        pool = await self._pool()
        async with pool.acquire() as conn:
            args = [limit] + ([_now(now)] if self.traffic_cycles_enabled else [])
            schedule = (
                """ AND (o.status='created' OR (a.next_attempt_at<=$2 AND NOT EXISTS(
                SELECT 1 FROM wl_activation_jobs older WHERE older.project_id=a.project_id
                AND older.subscription_id=a.subscription_id AND older.sequence<a.sequence
                AND older.status<>'complete'
                AND (older.blocked_reason IS NULL OR older.blocked_reason='activation_expired'))))"""
                if self.traffic_cycles_enabled
                else ""
            )
            rows = await conn.fetch(
                """SELECT o.project_id,o.tg_id,o.id FROM wl_orders o
                LEFT JOIN wl_activation_jobs a ON a.project_id=o.project_id AND a.order_id=o.id
                WHERE (o.status='paid_pending_activation' OR (o.status='created' AND (o.provider_payment_id IS NOT NULL"""
                + (
                    " OR (o.invoice_requested_at IS NOT NULL AND o.invoice_requested_at>$2::timestamptz-interval '23 hours')"
                    if self.invoice_intents_enabled
                    else ""
                )
                + ")))"
                + (
                    " AND o.activation_issue IS NULL"
                    if self.entitlements_enabled
                    else ""
                )
                + schedule
                + " ORDER BY (o.status='paid_pending_activation') DESC,a.sequence NULLS LAST,o.created_at,o.id LIMIT $1",
                *args,
            )
        return [_public(row) for row in rows]

    async def list_customers(self, project_id, owner_id, limit=100):
        project_id, owner_id, limit = (
            _uuid(project_id),
            _integer(owner_id, "owner_id"),
            _integer(limit, "limit", 500),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, owner_id)
            rows = await conn.fetch(
                """SELECT c.*,
                CASE WHEN s.id IS NULL THEN 'none' WHEN s.expires_at>now() THEN 'active'
                WHEN s.desired_expires_at>s.expires_at OR (s.desired_expires_at IS NOT NULL AND s.expires_at IS NULL)
                    THEN 'pending' WHEN s.expires_at IS NULL THEN 'unpaid' ELSE 'expired' END AS subscription_status,
                s.expires_at FROM wl_customers c LEFT JOIN wl_subscriptions s ON s.project_id=c.project_id AND s.tg_id=c.tg_id
                WHERE c.project_id=$1 ORDER BY c.created_at DESC,c.id DESC LIMIT $2""",
                project_id,
                limit,
            )
        return [_public(row) for row in rows]

    async def overview(self, project_id, owner_id, *, now=None):
        project_id, owner_id, now = (
            _uuid(project_id),
            _integer(owner_id, "owner_id"),
            _now(now),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, owner_id)
            customers = await conn.fetchval(
                "SELECT count(*) FROM wl_customers WHERE project_id=$1", project_id
            )
            totals = await conn.fetchrow(
                """SELECT count(*) FILTER(WHERE desired_expires_at IS NOT NULL) AS total,
                count(*) FILTER(WHERE expires_at>$2) AS active FROM wl_subscriptions WHERE project_id=$1""",
                project_id,
                now,
            )
            pending = await conn.fetchval(
                "SELECT count(*) FROM wl_orders WHERE project_id=$1 AND status='paid_pending_activation'",
                project_id,
            )
            periods = {}
            for label, start in (
                ("today", now.replace(hour=0, minute=0, second=0, microsecond=0)),
                ("days7", now - timedelta(days=7)),
                ("days30", now - timedelta(days=30)),
            ):
                row = await conn.fetchrow(
                    """SELECT count(*) AS sales,COALESCE(sum(amount_kopecks),0)::bigint AS revenue_kopecks,
                    COALESCE(sum(partner_kopecks),0)::bigint AS partner_kopecks,COALESCE(sum(platform_kopecks),0)::bigint AS platform_kopecks
                    FROM wl_revenue_ledger WHERE project_id=$1 AND created_at>=$2 AND created_at<=$3""",
                    project_id,
                    start,
                    now,
                )
                periods[label] = dict(row)
        return {
            "customers": customers,
            "active_subscriptions": totals["active"],
            "pending_activations": pending,
            "periods": periods,
            "timezone": "UTC",
        }
