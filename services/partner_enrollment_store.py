"""Durable partner entry checkout and launch state; no migrations/network/imported settings.

Only a canonical server-side provider verifier may confirm or cancel a payment.
Entry money is platform revenue, NOT a subscription sale or partner 70/30 credit.
The paid snapshot is durable before any remote bot change and never re-charged
because Telegram, DNS, process restart or launch configuration failed.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg

from services.partner_entry_pricing import EntryPricingError, EntryPricingPolicy, ENTRY_FEE_KOPECKS


class InvalidEnrollment(ValueError):
    """Malformed or impermissible entry operation."""


class EnrollmentNotFound(LookupError):
    """Missing resource or owner/project scope mismatch."""


class EnrollmentConflict(RuntimeError):
    """Immutable checkout or launch snapshot conflicts with current state."""


def _uuid(value: Any) -> UUID:
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise InvalidEnrollment("Некорректный идентификатор.") from None


def _integer(value: Any, maximum=2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise InvalidEnrollment("Ожидалось положительное целое число.")
    return value


def _now(value=None):
    value = value if value is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidEnrollment("Время должно содержать часовой пояс.")
    return value.astimezone(timezone.utc)


def _string(value, maximum=200):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise InvalidEnrollment("Некорректное текстовое значение.")
    return value


def _https(value, *, webhook=False):
    value = _string(value, 2048)
    try:
        parts = urlsplit(value)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise InvalidEnrollment("Некорректная HTTPS-ссылка.") from None
    if (parts.scheme != "https" or not hostname or parts.username or parts.password
            or port not in (None, 443) or "\\" in value or any(c.isspace() for c in value)
            or (webhook and ("?" in value or "#" in value))):
        raise InvalidEnrollment("Некорректная HTTPS-ссылка.")
    return value


def _public(row):
    # Explicit allowlist: private launch rows include a current encrypted token.
    fields = ("id", "project_id", "owner_tg_id", "request_key", "purpose", "bot_id",
              "price_kopecks", "currency", "status", "provider_payment_id", "confirmation_url",
              "created_at", "paid_at", "launched_at", "needs_webhook_replace")
    result = {key: str(row[key]) if isinstance(row[key], UUID) else row[key]
              for key in fields if key in row}
    result["tg_id"] = result["owner_tg_id"]
    result.setdefault("needs_webhook_replace", False)
    return result


def _lock_key(project_id):
    return int.from_bytes(hashlib.sha256(f"wl-launch:{project_id}".encode()).digest()[:8],
                          "big", signed=True)


class _ConnectionLease:
    """Pool-shaped, expiring view of the advisory-lock-owning connection."""
    def __init__(self, connection):
        self.connection = connection
        self.active = True

    @asynccontextmanager
    async def acquire(self):
        if not self.active:
            raise RuntimeError("Launch session lease has already ended")
        yield self.connection


class PartnerEnrollmentStore:
    def __init__(self, pool_getter, *, entry_pricing=None, entry_launch_notifications_enabled=False,
                 payment_first_enabled=False):
        if not callable(pool_getter):
            raise TypeError("pool_getter must be callable")
        if entry_pricing is not None and not isinstance(entry_pricing, EntryPricingPolicy):
            raise TypeError("entry_pricing must be an EntryPricingPolicy")
        if type(entry_launch_notifications_enabled) is not bool:
            raise TypeError("entry_launch_notifications_enabled must be bool")
        if type(payment_first_enabled) is not bool:
            raise TypeError("payment_first_enabled must be bool")
        self._pool_getter = pool_getter
        self.entry_pricing = entry_pricing if entry_pricing is not None else EntryPricingPolicy()
        self.entry_launch_notifications_enabled = entry_launch_notifications_enabled
        self.payment_first_enabled = payment_first_enabled

    def _validate_price(self, order, *, new_invoice=False):
        try:
            method = self.entry_pricing.validate_new_invoice if new_invoice else self.entry_pricing.validate_saved_order
            method(order)
        except EntryPricingError:
            raise EnrollmentConflict("Стоимость вступления требует подтверждения; сохранённый заказ не изменён.") from None

    async def _pool(self):
        pool = self._pool_getter()
        return await pool if inspect.isawaitable(pool) else pool

    @staticmethod
    async def _project(conn, project_id, owner_id=None, *, lock=False):
        query = "SELECT id,owner_tg_id,status,bot_id FROM wl_projects WHERE id=$1"
        args = [project_id]
        if owner_id is not None:
            query += " AND owner_tg_id=$2"
            args.append(owner_id)
        if lock:
            query += " FOR UPDATE"
        row = await conn.fetchrow(query, *args)
        if not row:
            raise EnrollmentNotFound("Проект не найден.")
        return row

    @staticmethod
    async def _order(conn, project_id, order_id, *, owner_id=None, lock=False):
        query = "SELECT * FROM wl_entry_orders WHERE project_id=$1 AND id=$2"
        args = [project_id, order_id]
        if owner_id is not None:
            query += " AND owner_tg_id=$3"
            args.append(owner_id)
        if lock:
            query += " FOR UPDATE"
        row = await conn.fetchrow(query, *args)
        if not row:
            raise EnrollmentNotFound("Заказ вступления не найден.")
        return row

    @staticmethod
    async def _order_public(conn, row):
        result = _public(row)
        result["needs_webhook_replace"] = bool(await conn.fetchval(
            "SELECT needs_webhook_replace FROM wl_launch_jobs WHERE project_id=$1 AND order_id=$2",
            row["project_id"], row["id"]))
        return result

    async def create_entry_order(self, project_id, owner_id, request_key, expected_fee_kopecks=ENTRY_FEE_KOPECKS):
        project_id, owner_id, request_key = _uuid(project_id), _integer(owner_id), _uuid(request_key)
        expected_fee_kopecks = _integer(expected_fee_kopecks)
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    project = await self._project(conn, project_id, owner_id, lock=True)
                    # Owner-wide UUID uniqueness prevents a lost request being
                    # retried accidentally against another selected project.
                    existing = await conn.fetchrow("SELECT * FROM wl_entry_orders WHERE owner_tg_id=$1 AND request_key=$2", owner_id, request_key)
                    if existing:
                        if existing["project_id"] != project_id and str(existing["project_id"]) != str(project_id):
                            raise EnrollmentConflict("Ключ запроса уже использован для другого проекта.")
                        self._validate_price(existing)
                        if existing["price_kopecks"] != expected_fee_kopecks:
                            raise EnrollmentConflict("Подтверждённая цена не совпадает с сохранённым заказом.")
                        return await self._order_public(conn, existing)
                    price = self.entry_pricing.price_for(owner_id)
                    if expected_fee_kopecks != price:
                        raise EnrollmentConflict("Стоимость вступления изменилась. Подтвердите актуальную цену.")
                    if await conn.fetchval("SELECT 1 FROM wl_entry_orders WHERE project_id=$1 AND status<>'canceled'", project_id):
                        raise EnrollmentConflict("У проекта уже есть заказ вступления. Восстановите его.")
                    if (project["status"] not in ("draft", "ready")
                            or not project["bot_id"] and not self.payment_first_enabled):
                        raise EnrollmentConflict("Покупка для этого проекта сейчас недоступна.")
                    row = await conn.fetchrow("""INSERT INTO wl_entry_orders
                        (id,project_id,owner_tg_id,request_key,bot_id,price_kopecks)
                        VALUES($1,$2,$3,$4,$5,$6) RETURNING *""",
                        uuid4(), project_id, owner_id, request_key, project["bot_id"], price)
                    return _public(row)
        except asyncpg.UniqueViolationError:
            raise EnrollmentConflict("Заказ вступления уже существует. Восстановите сохранённый заказ.") from None

    async def get_entry_order(self, project_id, owner_id, order_id):
        project_id, owner_id, order_id = _uuid(project_id), _integer(owner_id), _uuid(order_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await self._order(conn, project_id, order_id, owner_id=owner_id)
            return await self._order_public(conn, row)

    async def get_checkout_order(self, project_id, owner_id, order_id):
        """PRIVATE new-invoice guard; existing bound receipts reconcile normally.

        A draft can be suspended or closed after saving an entry order. Never
        create a fresh provider invoice merely because that old order exists.
        This does not cancel or discard an already issued or paid invoice.
        """
        project_id, owner_id, order_id = _uuid(project_id), _integer(owner_id), _uuid(order_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                project = await self._project(conn, project_id, owner_id, lock=True)
                order = await self._order(conn, project_id, order_id, owner_id=owner_id)
                self._validate_price(order, new_invoice=True)
                if (project["status"] not in ("draft", "ready")
                        or project["bot_id"] != order["bot_id"]
                        or order["bot_id"] is None and not self.payment_first_enabled
                        or order["status"] != "created" or order["provider_payment_id"] is not None):
                    raise EnrollmentConflict("Создание нового счёта для этого проекта сейчас недоступно.")
                return _public(order)

    async def get_current_entry_order(self, project_id, owner_id):
        project_id, owner_id = _uuid(project_id), _integer(owner_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, owner_id)
            row = await conn.fetchrow("""SELECT * FROM wl_entry_orders WHERE project_id=$1
                AND owner_tg_id=$2 AND status<>'canceled' ORDER BY created_at DESC,id DESC LIMIT 1""", project_id, owner_id)
            return await self._order_public(conn, row) if row else None

    async def save_invoice(self, project_id, owner_id, order_id, provider_payment_id, confirmation_url):
        project_id, owner_id, order_id = _uuid(project_id), _integer(owner_id), _uuid(order_id)
        provider_payment_id = _string(provider_payment_id)
        confirmation_url = "" if confirmation_url == "" else _https(confirmation_url)
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    order = await self._order(conn, project_id, order_id, owner_id=owner_id, lock=True)
                    if order["provider_payment_id"] is not None:
                        if (order["provider_payment_id"], order["confirmation_url"]) != (provider_payment_id, confirmation_url):
                            raise EnrollmentConflict("Заказ уже связан с другим счётом.")
                        return await self._order_public(conn, order)
                    if order["status"] != "created":
                        raise EnrollmentConflict("Нельзя привязать счёт к завершённому заказу.")
                    row = await conn.fetchrow("""UPDATE wl_entry_orders SET provider_payment_id=$4,confirmation_url=$5
                        WHERE project_id=$1 AND owner_tg_id=$2 AND id=$3 RETURNING *""", project_id, owner_id, order_id,
                        provider_payment_id, confirmation_url)
                    return _public(row)
        except asyncpg.UniqueViolationError:
            raise EnrollmentConflict("Этот платёж уже связан с другим заказом вступления.") from None

    async def cancel_verified_invoice(self, project_id, order_id, provider_payment_id):
        """Canonical provider cancellation; paid funds never become unpaid."""
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        provider_payment_id = _string(provider_payment_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                order = await self._order(conn, project_id, order_id, lock=True)
                if order["provider_payment_id"] != provider_payment_id:
                    raise EnrollmentConflict("Отмена не соответствует сохранённому счёту.")
                if order["status"] != "created":
                    return await self._order_public(conn, order)
                row = await conn.fetchrow("UPDATE wl_entry_orders SET status='canceled' WHERE project_id=$1 AND id=$2 RETURNING *", project_id, order_id)
                return _public(row)

    async def record_verified_payment(self, project_id, order_id, provider_payment_id, amount_kopecks, currency, *, now=None):
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        provider_payment_id, amount_kopecks, now = _string(provider_payment_id), _integer(amount_kopecks), _now(now)
        if currency != "RUB":
            raise InvalidEnrollment("Принимаются только подтверждённые оплаты в RUB.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                order = await self._order(conn, project_id, order_id, lock=True)
                self._validate_price(order)
                if order["provider_payment_id"] != provider_payment_id or order["price_kopecks"] != amount_kopecks:
                    raise EnrollmentConflict("Платёж не соответствует сохранённому счёту вступления.")
                if order["status"] == "canceled":
                    raise EnrollmentConflict("Ранее отменённый платёж требует ручной сверки.")
                if order["status"] != "created":
                    return await self._order_public(conn, order)
                # Payment-first orders have no bot yet. Preserve their paid
                # receipt without manufacturing a launch job for a null bot.
                # A verified Managed Bots binding creates that job atomically.
                if order["bot_id"] is not None:
                    await conn.execute("""INSERT INTO wl_launch_jobs(project_id,order_id,owner_tg_id,bot_id,next_attempt_at)
                        VALUES($1,$2,$3,$4,$5)""", project_id, order_id, order["owner_tg_id"], order["bot_id"], now)
                await conn.execute("""INSERT INTO wl_partner_outbox(project_id,order_id,owner_tg_id,kind)
                    VALUES($1,$2,$3,'entry_paid')""", project_id, order_id, order["owner_tg_id"])
                row = await conn.fetchrow("""UPDATE wl_entry_orders SET status='paid_pending_launch',paid_at=$3
                    WHERE project_id=$1 AND id=$2 RETURNING *""", project_id, order_id, now)
                return _public(row)

    @asynccontextmanager
    async def acquire_launch_lock(self, project_id):
        """Hold a cross-process session lock across Telegram work, not a DB tx.

        Call ALL inner operations on the yielded store. It reuses this session
        even with a one-connection pool, and expires when the context closes.
        """
        project_id = _uuid(project_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id)
            key = _lock_key(project_id)
            await conn.execute("SELECT pg_advisory_lock($1)", key)
            lease = _ConnectionLease(conn)
            try:
                yield PartnerEnrollmentStore(lambda: lease, entry_pricing=self.entry_pricing,
                    entry_launch_notifications_enabled=self.entry_launch_notifications_enabled,
                    payment_first_enabled=self.payment_first_enabled)
            finally:
                lease.active = False
                await conn.execute("SELECT pg_advisory_unlock($1)", key)

    async def get_launch(self, project_id, order_id):
        """PRIVATE launcher snapshot. NEVER return through an HTTP serializer.

        Current token for the paid bot permits token rotation, but changing the
        actual bot after payment blocks automatic launch and needs resolution.
        """
        project_id, order_id = _uuid(project_id), _uuid(order_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""SELECT j.*,p.bot_username,p.encrypted_token,p.title AS project_title,p.version AS project_version,
                p.bot_id AS current_bot_id,p.status AS project_status,o.status AS order_status,o.bot_id AS paid_bot_id
                FROM wl_launch_jobs j JOIN wl_entry_orders o ON o.project_id=j.project_id AND o.id=j.order_id
                JOIN wl_projects p ON p.id=j.project_id AND p.owner_tg_id=j.owner_tg_id
                WHERE j.project_id=$1 AND j.order_id=$2""", project_id, order_id)
        if not row:
            raise EnrollmentNotFound("Подтверждённая оплата для запуска не найдена.")
        if (row["order_status"] != "paid_pending_launch" or row["status"] == "complete"
                or row["project_status"] not in ("draft", "ready")
                or row["bot_id"] != row["paid_bot_id"] or row["bot_id"] != row["current_bot_id"]
                or not row["encrypted_token"]):
            raise EnrollmentConflict("Запуск требует сверки оплаченного проекта и подключённого бота.")
        return {key: str(value) if isinstance(value, UUID) else value for key, value in dict(row).items()
                if key not in ("current_bot_id", "project_status", "order_status", "paid_bot_id")}

    async def complete_launch(self, project_id, order_id, *, bot_id, webhook_url, secret_fingerprint, bot_username=None):
        project_id, order_id, bot_id = _uuid(project_id), _uuid(order_id), _integer(bot_id)
        if bot_username is not None and (
            not isinstance(bot_username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username)
        ):
            raise InvalidEnrollment("Telegram не подтвердил имя оплаченного бота.")
        webhook_url = _https(webhook_url, webhook=True)
        if not isinstance(secret_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", secret_fingerprint):
            raise InvalidEnrollment("Ожидался SHA-256 отпечаток секрета webhook.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                order = await self._order(conn, project_id, order_id, lock=True)
                job = await conn.fetchrow("SELECT * FROM wl_launch_jobs WHERE project_id=$1 AND order_id=$2 FOR UPDATE", project_id, order_id)
                if not job or order["status"] not in ("paid_pending_launch", "launched"):
                    raise EnrollmentConflict("Для запуска нужна подтверждённая оплата вступления.")
                if order["bot_id"] != bot_id or job["bot_id"] != bot_id:
                    raise EnrollmentConflict("Запуск не соответствует оплаченному боту.")
                if order["status"] == "launched":
                    if (job["status"], job["webhook_url"], job["secret_fingerprint"]) != ("complete", webhook_url, secret_fingerprint):
                        raise EnrollmentConflict("Запуск уже сохранён с другими параметрами.")
                    await self._enqueue_launch_notification(conn, order)
                    return await self._order_public(conn, order)
                # Conditional UPDATE locks the current project atomically. No
                # project activation before Telegram setup has been verified.
                changed = await conn.fetchrow("""UPDATE wl_projects SET status='active',
                    bot_username=COALESCE($4,bot_username),version=version+1,updated_at=now()
                    WHERE id=$1 AND owner_tg_id=$2 AND bot_id=$3 AND status IN ('draft','ready') RETURNING id""",
                    project_id, order["owner_tg_id"], bot_id, bot_username)
                if not changed:
                    raise EnrollmentConflict("Проект или бот изменён; запуск требует ручной сверки.")
                await conn.execute("""UPDATE wl_launch_jobs SET status='complete',completed_at=now(),attempts=attempts+1,
                    needs_webhook_replace=FALSE,webhook_url=$3,secret_fingerprint=$4
                    WHERE project_id=$1 AND order_id=$2""", project_id, order_id, webhook_url, secret_fingerprint)
                row = await conn.fetchrow("""UPDATE wl_entry_orders SET status='launched',launched_at=now()
                    WHERE project_id=$1 AND id=$2 RETURNING *""", project_id, order_id)
                await self._enqueue_launch_notification(conn, row)
                return _public(row)

    async def _enqueue_launch_notification(self, conn, order):
        if not self.entry_launch_notifications_enabled:
            return
        # Same transaction as project activation + job/order completion. A send
        # worker can observe this event only AFTER the whole launch commits.
        await conn.execute("""INSERT INTO wl_partner_outbox(project_id,order_id,owner_tg_id,kind)
            SELECT o.project_id,o.id,o.owner_tg_id,'entry_launched'
            FROM wl_entry_orders o JOIN wl_projects p ON p.id=o.project_id AND p.owner_tg_id=o.owner_tg_id
            JOIN wl_launch_jobs j ON j.project_id=o.project_id AND j.order_id=o.id AND j.owner_tg_id=o.owner_tg_id
            WHERE o.project_id=$1 AND o.id=$2 AND o.status='launched' AND o.paid_at IS NOT NULL
              AND o.launched_at IS NOT NULL AND p.status='active' AND p.bot_id=o.bot_id
              AND j.status='complete' AND j.bot_id=o.bot_id AND j.completed_at IS NOT NULL
            ON CONFLICT(project_id,order_id,kind) DO NOTHING""", order["project_id"], order["id"])

    async def mark_launch_retry(self, project_id, order_id, *, needs_webhook_replace=False, now=None):
        project_id, order_id, now = _uuid(project_id), _uuid(order_id), _now(now)
        if not isinstance(needs_webhook_replace, bool):
            raise InvalidEnrollment("Некорректное состояние замены webhook.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                order = await self._order(conn, project_id, order_id, lock=True)
                job = await conn.fetchrow("SELECT * FROM wl_launch_jobs WHERE project_id=$1 AND order_id=$2 FOR UPDATE", project_id, order_id)
                if not job or order["status"] not in ("paid_pending_launch", "launched"):
                    raise EnrollmentConflict("Для повтора запуска нужна подтверждённая оплата.")
                if order["status"] == "launched" or job["status"] == "complete":
                    return await self._order_public(conn, order)
                # Bounded exponential backoff. Waiting for owner consent is not
                # returned by the unattended launch recovery worklist.
                delay = min(3600, 30 * (2 ** min(job["attempts"], 7)))
                await conn.execute("""UPDATE wl_launch_jobs SET status='retry',attempts=attempts+1,
                    next_attempt_at=$3,needs_webhook_replace=$4 WHERE project_id=$1 AND order_id=$2""",
                    project_id, order_id, now + timedelta(seconds=delay), needs_webhook_replace)
                result = _public(order)
                result["needs_webhook_replace"] = needs_webhook_replace
                return result

    async def list_recoverable_launches(self, limit=50):
        """Internal due paid jobs only. No token, URL, secret or user-provided error."""
        limit = _integer(limit, 100)
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""SELECT o.project_id,o.owner_tg_id,o.id AS order_id FROM wl_entry_orders o
                JOIN wl_launch_jobs j ON j.project_id=o.project_id AND j.order_id=o.id
                WHERE o.status='paid_pending_launch' AND j.status<>'complete'
                    AND NOT j.needs_webhook_replace AND j.next_attempt_at<=now()
                ORDER BY j.next_attempt_at,o.created_at,o.id LIMIT $1""", limit)
        return [{key: str(value) if isinstance(value, UUID) else value for key, value in dict(row).items()} for row in rows]

    async def list_recoverable_entry_orders(self, limit=50):
        """Internal unpaid invoices and due paid jobs; provider recheck is trusted.

        Excludes paid jobs awaiting an explicit webhook-replacement decision.
        The combined limit avoids repeated processing when both lists are full.
        """
        limit = _integer(limit, 100)
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""SELECT o.project_id,o.owner_tg_id,o.id FROM wl_entry_orders o
                LEFT JOIN wl_launch_jobs j ON j.project_id=o.project_id AND j.order_id=o.id
                WHERE (o.status='created' AND o.provider_payment_id IS NOT NULL)
                    OR (o.status='paid_pending_launch' AND j.status<>'complete'
                        AND NOT j.needs_webhook_replace AND j.next_attempt_at<=now())
                ORDER BY (o.status='paid_pending_launch') DESC,j.next_attempt_at NULLS LAST,o.created_at,o.id LIMIT $1""", limit)
        return [{key: str(value) if isinstance(value, UUID) else value for key, value in dict(row).items()} for row in rows]
