"""Optional bounded factual finance outbox delivery via central manager bot.

No automatic start, settings discovery, bank transfers or advertising. Telegram
has no send idempotency: a crash after the saved send intent becomes uncertain,
never an automatic retry. Requisites/evidence never enter this worker's query.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
from uuid import UUID, uuid4

from services.partner_notifications import NotificationResult
from services.partner_security import validate_bot_token


def _now(value=None):
    value = value if value is not None else datetime.now(timezone.utc)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Нужен часовой пояс.")
    return value.astimezone(timezone.utc)


def _positive(value):
    if type(value) is not int or not 1 <= value < 2**63:
        raise ValueError("Некорректный идентификатор.")
    return value


class FinanceNotificationStore:
    def __init__(self, pool_getter, *, admin_recipient_id=None, enabled=False):
        if not callable(pool_getter) or type(enabled) is not bool:
            raise ValueError("Очередь финансов не настроена.")
        self._pool_getter = pool_getter
        self.admin_recipient_id = (
            None if admin_recipient_id is None else _positive(admin_recipient_id)
        )
        self.enabled = enabled and self.admin_recipient_id is not None

    async def _pool(self):
        value = self._pool_getter()
        return await value if inspect.isawaitable(value) else value

    async def claim(self, *, now=None):
        if not self.enabled:
            return None
        now = _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE wl_finance_outbox SET delivery_state=CASE WHEN delivery_state='leased'
                    THEN 'pending' ELSE 'uncertain' END WHERE id IN (
                    SELECT id FROM wl_finance_outbox WHERE delivery_state IN ('leased','sending') AND lease_until<=$1
                    ORDER BY lease_until,id LIMIT 100 FOR UPDATE SKIP LOCKED)""",
                    now,
                )
                # Configuration changes never forward old events to a retired
                # admin, and tampered owner IDs never get a private notification.
                await conn.execute(
                    """UPDATE wl_finance_outbox SET delivery_state='blocked' WHERE id IN (
                    SELECT b.id FROM wl_finance_outbox b JOIN wl_projects p ON b.project_id=p.id
                    WHERE b.delivery_state='pending' AND
                    ((b.kind IN ('withdrawal_requested','reversal_confirmed') AND b.recipient_tg_id<>$1)
                    OR (b.kind IN ('withdrawal_paid','withdrawal_rejected') AND b.recipient_tg_id<>p.owner_tg_id))
                    ORDER BY b.id LIMIT 100 FOR UPDATE OF b SKIP LOCKED)""",
                    self.admin_recipient_id,
                )
                row = await conn.fetchrow(
                    """SELECT b.id,b.project_id,b.event_id,b.kind,b.recipient_tg_id AS recipient_id,
                    w.number,w.status AS withdrawal_status,COALESCE(w.amount_kopecks,r.partner_kopecks) AS amount_kopecks
                    FROM wl_finance_outbox b JOIN wl_projects p ON p.id=b.project_id
                    LEFT JOIN wl_withdrawals w ON w.project_id=b.project_id AND w.id=b.event_id
                    LEFT JOIN wl_revenue_reversals r ON r.project_id=b.project_id AND r.id=b.event_id
                    WHERE b.delivery_state='pending' AND b.next_attempt_at<=$1 AND (
                      (b.kind='withdrawal_requested' AND w.id IS NOT NULL AND b.recipient_tg_id=$2)
                      OR (b.kind='withdrawal_paid' AND w.status='paid' AND b.recipient_tg_id=p.owner_tg_id)
                      OR (b.kind='withdrawal_rejected' AND w.status='rejected' AND b.recipient_tg_id=p.owner_tg_id)
                      OR (b.kind='reversal_confirmed' AND r.id IS NOT NULL AND b.recipient_tg_id=$2))
                    ORDER BY b.id LIMIT 1 FOR UPDATE OF b SKIP LOCKED""",
                    now,
                    self.admin_recipient_id,
                )
                if row is None:
                    return None
                token = uuid4()
                await conn.execute(
                    """UPDATE wl_finance_outbox SET delivery_state='leased',lease_token=$2,
                    lease_until=$3,attempts=attempts+1 WHERE id=$1""",
                    row["id"],
                    token,
                    now + timedelta(minutes=2),
                )
                return dict(row) | {"lease_token": str(token)}

    async def begin_send(self, row, *, now=None):
        if not self.enabled:
            return False
        pool = await self._pool()
        async with pool.acquire() as conn:
            identity = await conn.fetchval(
                """UPDATE wl_finance_outbox SET delivery_state='sending'
                WHERE id=$1 AND project_id=$2 AND lease_token=$3 AND delivery_state='leased' AND lease_until>$4 RETURNING id""",
                _positive(row["id"]),
                UUID(str(row["project_id"])),
                UUID(row["lease_token"]),
                _now(now),
            )
        return identity is not None

    async def finish(self, row, result, *, now=None):
        if (
            not self.enabled
            or not isinstance(result, NotificationResult)
            or result.state not in {"sent", "retry", "blocked", "uncertain"}
        ):
            raise ValueError("Некорректное состояние доставки.")
        now = _now(now)
        if result.state == "sent":
            _positive(result.message_id)
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
            else (
                ("sending",) if result.state == "uncertain" else ("leased", "sending")
            )
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            identity = await conn.fetchval(
                """UPDATE wl_finance_outbox SET delivery_state=$4,status=$5,
                next_attempt_at=$6,telegram_message_id=$7 WHERE id=$1 AND project_id=$2 AND lease_token=$3
                AND delivery_state=ANY($8::text[]) RETURNING id""",
                _positive(row["id"]),
                UUID(str(row["project_id"])),
                UUID(row["lease_token"]),
                state,
                "delivered" if state == "delivered" else "pending",
                now + timedelta(seconds=result.retry_after),
                result.message_id if state == "delivered" else None,
                list(accepted),
            )
        return identity is not None

    async def health(self):
        if not self.enabled:
            return {}
        pool = await self._pool()
        async with pool.acquire() as conn:
            return {
                row["delivery_state"]: row["count"]
                for row in await conn.fetch(
                    "SELECT delivery_state,count(*)::bigint AS count FROM wl_finance_outbox GROUP BY delivery_state"
                )
            }


def finance_notification_text(row):
    amount = row["amount_kopecks"]
    if type(amount) is not int or not 0 <= amount < 2**63:
        raise ValueError("Некорректная сумма события.")
    money = (
        f"{amount//100:,}".replace(",", " ")
        + (f",{amount%100:02d}" if amount % 100 else "")
        + " ₽"
    )
    if row["kind"] == "reversal_confirmed":
        return f"Подтверждено сторно партнёрского начисления: {money}. Баланс обновлён; подробности в кабинете оператора."
    number = "P-" + str(_positive(row["number"]))
    if row["kind"] == "withdrawal_requested":
        statuses = {
            "pending": "ожидает проверки",
            "processing": "в обработке",
            "paid": "выплачена",
            "rejected": "отклонена",
        }
        return f"Создана заявка {number} на {money}. Сейчас: {statuses[row['withdrawal_status']]}. Реквизиты и проверка баланса — в кабинете оператора."
    if row["kind"] == "withdrawal_paid":
        return f"Выплата {number} на {money} подтверждена оператором. Подробности — в разделе «Финансы»."
    if row["kind"] == "withdrawal_rejected":
        return f"Заявка {number} на {money} отклонена. Резерв освобождён; причина — в разделе «Финансы»."
    raise ValueError("Неизвестное финансовое событие.")


class FinanceNotificationService:
    def __init__(
        self, store, sender=None, *, manager_bot_token="", enabled=False, clock=None
    ):
        if type(enabled) is not bool:
            raise ValueError("Нужен явный флаг доставки.")
        if manager_bot_token:
            validate_bot_token(manager_bot_token)
        self.store, self.sender = store, sender
        self._manager_token, self.enabled = manager_bot_token, enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def ready(self):
        return bool(
            self.enabled
            and self.store.enabled
            and self.sender is not None
            and self._manager_token
        )

    async def recover_once(self, *, limit=25):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Некорректный размер обработки.")
        counts = {
            "enabled": self.ready,
            "checked": 0,
            "sent": 0,
            "retry": 0,
            "blocked": 0,
            "uncertain": 0,
            "errors": 0,
        }
        if not self.ready:
            return counts
        bot_id = int(self._manager_token.split(":", 1)[0])
        for _ in range(limit):
            row = await self.store.claim(now=_now(self.clock()))
            if row is None:
                break
            counts["checked"] += 1
            try:
                text = finance_notification_text(row)
            except Exception:
                await self.store.finish(
                    row, NotificationResult("blocked"), now=_now(self.clock())
                )
                counts["blocked"] += 1
                continue
            try:
                await self.sender.verify(self._manager_token, bot_id)
            except Exception:
                await self.store.finish(
                    row, NotificationResult("retry"), now=_now(self.clock())
                )
                counts["retry"] += 1
                continue
            if not await self.store.begin_send(row, now=_now(self.clock())):
                continue
            try:
                outcome = await self.sender.send(
                    self._manager_token,
                    bot_id=bot_id,
                    chat_id=row["recipient_id"],
                    text=text,
                )
                if not isinstance(outcome, NotificationResult):
                    outcome = NotificationResult("uncertain")
            except Exception:
                outcome = NotificationResult("uncertain")
            try:
                if await self.store.finish(row, outcome, now=_now(self.clock())):
                    counts[outcome.state] += 1
            except Exception:
                # Persisted sending intent survives. After expiry: uncertain.
                counts["errors"] += 1
        return counts
