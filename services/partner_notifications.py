"""Tenant-bound factual outbox delivery, never enabled or scheduled on import.

Telegram has no sendMessage idempotency key. Unknown delivery is retained as
uncertain for inspection, NOT automatically sent again or marked delivered.
The main SPN bot/outbox and advertising workflows are not reused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import inspect
import json
from uuid import UUID, uuid4

from services.partner_security import validate_bot_token


TABLES = {"customer": "wl_customer_outbox", "partner": "wl_partner_outbox"}


def _now(value=None):
    value = value if value is not None else datetime.now(timezone.utc)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Нужен часовой пояс.")
    return value.astimezone(timezone.utc)


def _integer(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Некорректный идентификатор.")
    return value


@dataclass(frozen=True)
class NotificationResult:
    state: str
    message_id: int | None = None
    retry_after: int = 60


class PartnerNotificationStore:
    def __init__(self, pool_getter, *, enabled=False):
        if not callable(pool_getter) or type(enabled) is not bool:
            raise ValueError("Очередь уведомлений не настроена.")
        self._pool_getter, self.enabled = pool_getter, enabled

    async def _pool(self):
        result = self._pool_getter()
        return await result if inspect.isawaitable(result) else result

    async def claim(self, channel, *, now=None):
        if not self.enabled:
            return None
        table, now = TABLES[channel], _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # A crash before starting a send is safe to retry. A crash
                # after the intent was saved may have delivered the message.
                await conn.execute(
                    f"""UPDATE {table} SET delivery_state=CASE WHEN delivery_state='leased'
                    THEN 'pending' ELSE 'uncertain' END WHERE id IN
                    (SELECT id FROM {table} WHERE delivery_state IN ('leased','sending') AND lease_until<=$1
                        ORDER BY lease_until,id LIMIT 100 FOR UPDATE SKIP LOCKED)""",
                    now,
                )
                if channel == "customer":
                    query = f"""SELECT b.id,b.project_id,b.order_id,b.tg_id AS recipient_id,p.bot_id,
                        p.encrypted_token,o.price_kopecks,o.purpose,o.status AS order_status,o.activation_issue
                        FROM {table} b JOIN wl_projects p ON p.id=b.project_id
                        JOIN wl_orders o ON o.project_id=b.project_id AND o.id=b.order_id AND o.tg_id=b.tg_id
                        WHERE b.delivery_state='pending' AND b.next_attempt_at<=$1 AND o.paid_at IS NOT NULL
                        AND p.status='active' AND p.bot_id IS NOT NULL ORDER BY b.id LIMIT 1 FOR UPDATE OF b SKIP LOCKED"""
                else:
                    query = f"""SELECT b.id,b.project_id,b.order_id,b.owner_tg_id AS recipient_id,
                        o.price_kopecks,o.status AS order_status FROM {table} b JOIN wl_entry_orders o
                        ON o.project_id=b.project_id AND o.id=b.order_id AND o.owner_tg_id=b.owner_tg_id
                        WHERE b.kind='entry_paid' AND b.delivery_state='pending' AND b.next_attempt_at<=$1 AND o.paid_at IS NOT NULL
                        ORDER BY b.id LIMIT 1 FOR UPDATE OF b SKIP LOCKED"""
                row = await conn.fetchrow(query, now)
                if not row:
                    return None
                token = uuid4()
                await conn.execute(
                    f"""UPDATE {table} SET delivery_state='leased',lease_token=$2,
                    lease_until=$3,attempts=attempts+1 WHERE id=$1""",
                    row["id"],
                    token,
                    now + timedelta(minutes=2),
                )
                return {**dict(row), "channel": channel, "lease_token": str(token)}

    async def begin_send(self, row, *, now=None):
        if not self.enabled:
            return False
        table, now = TABLES[row["channel"]], _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval(
                f"""UPDATE {table} SET delivery_state='sending'
                WHERE id=$1 AND project_id=$2 AND lease_token=$3 AND delivery_state='leased' AND lease_until>$4
                RETURNING id""",
                _integer(row["id"]),
                UUID(str(row["project_id"])),
                UUID(row["lease_token"]),
                now,
            )
        return result is not None

    async def finish(self, row, result, *, now=None):
        if (
            not self.enabled
            or not isinstance(result, NotificationResult)
            or result.state not in {"sent", "retry", "blocked", "uncertain"}
        ):
            raise ValueError("Некорректное состояние доставки.")
        table, now = TABLES[row["channel"]], _now(now)
        if result.state == "sent":
            _integer(result.message_id)
        if type(result.retry_after) is not int or not 1 <= result.retry_after <= 86400:
            raise ValueError("Некорректная задержка.")
        state = {
            "sent": "delivered",
            "retry": "pending",
            "blocked": "blocked",
            "uncertain": "uncertain",
        }[result.state]
        accepted = (
            ("sending", "uncertain")
            if result.state == "sent"
            else ("sending",) if result.state == "uncertain" else ("leased", "sending")
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            result_id = await conn.fetchval(
                f"""UPDATE {table} SET delivery_state=$4,status=$5,
                next_attempt_at=$6,telegram_message_id=$7 WHERE id=$1 AND project_id=$2 AND lease_token=$3
                AND delivery_state=ANY($8::text[]) RETURNING id""",
                _integer(row["id"]),
                UUID(str(row["project_id"])),
                UUID(row["lease_token"]),
                state,
                "delivered" if state == "delivered" else "pending",
                now + timedelta(seconds=result.retry_after),
                result.message_id if state == "delivered" else None,
                list(accepted),
            )
        return result_id is not None

    async def health(self):
        if not self.enabled:
            return {}
        pool = await self._pool()
        async with pool.acquire() as conn:
            return {
                channel: [
                    dict(row)
                    for row in await conn.fetch(
                        f"SELECT delivery_state,count(*)::bigint AS count FROM {table} GROUP BY delivery_state"
                    )
                ]
                for channel, table in TABLES.items()
            }


class NotificationTransportError(RuntimeError):
    pass


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


class TelegramNotificationAdapter:
    def __init__(self, *, session_factory=None):
        self._session_factory = session_factory

    async def _call(self, token, method, payload=None):
        try:
            validate_bot_token(token)
            if method not in {"getMe", "sendMessage"}:
                raise ValueError()
            import aiohttp

            factory = self._session_factory or aiohttp.ClientSession
            async with factory(
                timeout=aiohttp.ClientTimeout(total=20, connect=5), trust_env=False
            ) as session:
                async with session.request(
                    "GET" if payload is None else "POST",
                    f"https://api.telegram.org/bot{token}/{method}",
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise ValueError()
                    body = json.loads(raw, object_pairs_hook=_json_object)
                    if not isinstance(body, dict):
                        raise ValueError()
                    return response.status, body
        except Exception:
            raise NotificationTransportError(
                "Telegram не подтвердил доставку."
            ) from None

    async def verify(self, token, bot_id):
        _integer(bot_id)
        validate_bot_token(token)
        if int(token.split(":", 1)[0]) != bot_id:
            raise NotificationTransportError("Не совпало подключение бота.")
        status, body = await self._call(token, "getMe")
        result = body.get("result")
        if (
            status != 200
            or body.get("ok") is not True
            or not isinstance(result, dict)
            or type(result.get("id")) is not int
            or result["id"] != bot_id
            or result.get("is_bot") is not True
        ):
            raise NotificationTransportError("Подключение бота не подтверждено.")

    async def send(self, token, *, bot_id, chat_id, text):
        try:
            validate_bot_token(token)
            if int(token.split(":", 1)[0]) != _integer(bot_id):
                raise ValueError()
            _integer(chat_id)
            if not isinstance(text, str) or not 1 <= len(text) <= 4000:
                raise ValueError()
            status, body = await self._call(
                token,
                "sendMessage",
                {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
            result = body.get("result")
            if status == 200 and body.get("ok") is True and isinstance(result, dict):
                chat, sender = result.get("chat"), result.get("from")
                if (
                    not isinstance(chat, dict)
                    or type(chat.get("id")) is not int
                    or chat["id"] != chat_id
                    or chat.get("type") != "private"
                    or not isinstance(sender, dict)
                    or type(sender.get("id")) is not int
                    or sender["id"] != bot_id
                    or sender.get("is_bot") is not True
                ):
                    raise ValueError()
                return NotificationResult("sent", _integer(result.get("message_id")))
            if (
                body.get("ok") is False
                and type(body.get("error_code")) is int
                and body["error_code"] == status
            ):
                if status == 429:
                    delay = (body.get("parameters") or {}).get("retry_after")
                    if type(delay) is int and 1 <= delay <= 86400:
                        return NotificationResult("retry", retry_after=delay)
                if status in {400, 401, 403}:
                    return NotificationResult("blocked")
            return NotificationResult("uncertain")
        except Exception:
            return NotificationResult("uncertain")


def notification_text(row):
    amount = _integer(row["price_kopecks"])
    value = (
        f"{amount//100:,}".replace(",", " ")
        + (f",{amount%100:02d}" if amount % 100 else "")
        + " ₽"
    )
    if row["channel"] == "partner":
        return f"Вступление {value} оплачено. Состояние подключения бота — в кабинете партнёра. Повторно платить не нужно."
    if row.get("activation_issue"):
        return f"Оплата {value} сохранена. Покупка требует проверки поддержкой. Повторно платить не нужно."
    return f"Оплата {value} получена. Состояние покупки — в разделе «Моя подписка». Повторно платить не нужно."


class PartnerNotificationService:
    def __init__(
        self,
        store,
        sender=None,
        *,
        vault=None,
        manager_bot_token="",
        enabled=False,
        clock=None,
    ):
        if type(enabled) is not bool:
            raise ValueError("Нужен явный флаг доставки.")
        if manager_bot_token:
            validate_bot_token(manager_bot_token)
        self.store, self.sender, self.vault = store, sender, vault
        self._manager_token, self.enabled = manager_bot_token, enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def ready(self):
        return bool(
            self.enabled
            and self.store.enabled
            and self.sender is not None
            and (self.vault is not None or self._manager_token)
        )

    async def recover_once(self, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Некорректный размер обработки.")
        result = {
            "enabled": self.ready,
            "checked": 0,
            "sent": 0,
            "retry": 0,
            "blocked": 0,
            "uncertain": 0,
            "errors": 0,
        }
        if not self.ready:
            return result
        channels = (["customer"] if self.vault is not None else []) + (
            ["partner"] if self._manager_token else []
        )
        if not channels:
            return result
        for index in range(limit):
            channel = channels[index % len(channels)]
            row = await self.store.claim(channel, now=_now(self.clock()))
            if row is None:
                continue
            result["checked"] += 1
            try:
                if channel == "customer":
                    token = self.vault.decrypt(
                        row["project_id"], row["bot_id"], row["encrypted_token"]
                    )
                    bot_id = row["bot_id"]
                else:
                    token = self._manager_token
                    bot_id = int(token.split(":", 1)[0])
                text = notification_text(row)
            except Exception:
                await self.store.finish(
                    row, NotificationResult("blocked"), now=_now(self.clock())
                )
                result["blocked"] += 1
                continue
            try:
                await self.sender.verify(token, bot_id)
            except Exception:
                await self.store.finish(
                    row, NotificationResult("retry"), now=_now(self.clock())
                )
                result["retry"] += 1
                continue
            if not await self.store.begin_send(row, now=_now(self.clock())):
                continue
            try:
                outcome = await self.sender.send(
                    token, bot_id=bot_id, chat_id=row["recipient_id"], text=text
                )
                if not isinstance(outcome, NotificationResult):
                    outcome = NotificationResult("uncertain")
            except Exception:
                outcome = NotificationResult("uncertain")
            try:
                if await self.store.finish(row, outcome, now=_now(self.clock())):
                    result[outcome.state] += 1
            except Exception:
                # Telegram may already have accepted it. The saved send intent
                # expires to 'uncertain', never an automatic second send.
                result["errors"] += 1
        return result
