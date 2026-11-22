"""Durable manager-bot notice AFTER a verified paid project launch commits.

Uses the existing partner outbox and its conservative Telegram send-intent
leases. Unknown sends remain uncertain: Telegram offers no send idempotency key.
No settings, network, scheduling or migration occurs on import/construction.
"""
from datetime import timedelta
import re
from uuid import uuid4

from services.partner_entry_pricing import EntryPricingPolicy
from services.partner_notifications import (
    NotificationResult, PartnerNotificationStore, TelegramNotificationAdapter,
    _integer, _now,
)
from services.partner_security import validate_bot_token


ENTRY_CREATED_TEXT = "Оплата прошла, ваш сервис создан. Дальше всё в вашем боте."
ENTRY_CREATED_BUTTON = "Перейти в моего бота"


class EntryLaunchNotificationStore(PartnerNotificationStore):
    async def enqueue_completed(self, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Некорректный размер обработки.")
        if not self.enabled:
            return 0
        pool = await self._pool()
        async with pool.acquire() as conn:
            # Recovery also covers an older completed launch or a restart before
            # notification delivery. The unique event key prevents duplicates.
            return await conn.fetchval("""WITH inserted AS (
                INSERT INTO wl_partner_outbox(project_id,order_id,owner_tg_id,kind)
                SELECT o.project_id,o.id,o.owner_tg_id,'entry_launched'
                FROM wl_entry_orders o JOIN wl_projects p ON p.id=o.project_id AND p.owner_tg_id=o.owner_tg_id
                JOIN wl_launch_jobs j ON j.project_id=o.project_id AND j.order_id=o.id AND j.owner_tg_id=o.owner_tg_id
                WHERE o.status='launched' AND o.paid_at IS NOT NULL AND o.launched_at IS NOT NULL
                  AND p.status='active' AND p.bot_id=o.bot_id AND j.bot_id=o.bot_id
                  AND j.status='complete' AND j.completed_at IS NOT NULL
                  AND NOT EXISTS(SELECT 1 FROM wl_partner_outbox b WHERE b.project_id=o.project_id
                      AND b.order_id=o.id AND b.kind='entry_launched')
                ORDER BY o.launched_at,o.id LIMIT $1
                ON CONFLICT(project_id,order_id,kind) DO NOTHING RETURNING id)
                SELECT count(*)::bigint FROM inserted""", limit)

    async def claim(self, *, now=None):
        if not self.enabled:
            return None
        now = _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""UPDATE wl_partner_outbox SET delivery_state=CASE WHEN delivery_state='leased'
                    THEN 'pending' ELSE 'uncertain' END WHERE id IN
                    (SELECT id FROM wl_partner_outbox WHERE kind='entry_launched'
                     AND delivery_state IN ('leased','sending') AND lease_until<=$1
                     ORDER BY lease_until,id LIMIT 100 FOR UPDATE SKIP LOCKED)""", now)
                row = await conn.fetchrow("""SELECT b.id,b.project_id,b.order_id,b.owner_tg_id AS recipient_id,
                    o.owner_tg_id,o.price_kopecks,p.bot_id,p.bot_username
                    FROM wl_partner_outbox b JOIN wl_entry_orders o
                      ON o.project_id=b.project_id AND o.id=b.order_id AND o.owner_tg_id=b.owner_tg_id
                    JOIN wl_projects p ON p.id=o.project_id AND p.owner_tg_id=o.owner_tg_id
                    JOIN wl_launch_jobs j ON j.project_id=o.project_id AND j.order_id=o.id AND j.owner_tg_id=o.owner_tg_id
                    WHERE b.kind='entry_launched' AND b.delivery_state='pending' AND b.next_attempt_at<=$1
                      AND o.status='launched' AND o.paid_at IS NOT NULL AND o.launched_at IS NOT NULL
                      AND p.status='active' AND p.bot_id=o.bot_id AND j.bot_id=o.bot_id
                      AND j.status='complete' AND j.completed_at IS NOT NULL
                      AND p.bot_username ~ '^[A-Za-z0-9_]{5,32}$'
                    ORDER BY b.id LIMIT 1 FOR UPDATE OF b SKIP LOCKED""", now)
                if row is None:
                    return None
                token = uuid4()
                await conn.execute("""UPDATE wl_partner_outbox SET delivery_state='leased',lease_token=$2,
                    lease_until=$3,attempts=attempts+1 WHERE id=$1 AND kind='entry_launched'""",
                    row["id"], token, now + timedelta(minutes=2))
                return {**dict(row), "channel": "partner", "lease_token": str(token)}


class TelegramEntryLaunchNotificationAdapter(TelegramNotificationAdapter):
    async def send(self, token, *, bot_id, chat_id, text, bot_url):
        try:
            validate_bot_token(token)
            if int(token.split(":", 1)[0]) != _integer(bot_id):
                raise ValueError()
            _integer(chat_id)
            if text != ENTRY_CREATED_TEXT or not isinstance(bot_url, str) or not re.fullmatch(r"https://t\.me/[A-Za-z0-9_]{5,32}", bot_url):
                raise ValueError()
            status, body = await self._call(token, "sendMessage", {
                "chat_id": chat_id, "text": text, "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [[{"text": ENTRY_CREATED_BUTTON, "url": bot_url}]]},
            })
            result = body.get("result")
            if status == 200 and body.get("ok") is True and isinstance(result, dict):
                chat, sender = result.get("chat"), result.get("from")
                if (not isinstance(chat, dict) or type(chat.get("id")) is not int or chat["id"] != chat_id
                        or chat.get("type") != "private" or not isinstance(sender, dict)
                        or type(sender.get("id")) is not int or sender["id"] != bot_id or sender.get("is_bot") is not True):
                    raise ValueError()
                return NotificationResult("sent", _integer(result.get("message_id")))
            if body.get("ok") is False and type(body.get("error_code")) is int and body["error_code"] == status:
                if status == 429:
                    delay = (body.get("parameters") or {}).get("retry_after")
                    if type(delay) is int and 1 <= delay <= 86400:
                        return NotificationResult("retry", retry_after=delay)
                if status in {400, 401, 403}:
                    return NotificationResult("blocked")
            return NotificationResult("uncertain")
        except Exception:
            return NotificationResult("uncertain")


class EntryLaunchNotificationService:
    def __init__(self, store, sender=None, *, manager_bot_token="", enabled=False, clock=None):
        if type(enabled) is not bool:
            raise ValueError("Нужен явный флаг доставки.")
        if manager_bot_token:
            validate_bot_token(manager_bot_token)
        self.store, self.sender, self._manager_token, self.enabled = store, sender, manager_bot_token, enabled
        self.clock = clock or _now

    @property
    def ready(self):
        return bool(self.enabled and self.store.enabled and self.sender is not None and self._manager_token)

    async def recover_once(self, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Некорректный размер обработки.")
        counts = {"enabled": self.ready, "queued": 0, "checked": 0, "sent": 0,
                  "retry": 0, "blocked": 0, "uncertain": 0, "errors": 0}
        if not self.ready:
            return counts
        counts["queued"] = await self.store.enqueue_completed(limit=limit)
        for _ in range(limit):
            row = await self.store.claim(now=_now(self.clock()))
            if row is None:
                break
            counts["checked"] += 1
            try:
                EntryPricingPolicy().validate_saved_order(row)
                recipient = _integer(row["recipient_id"])
                if recipient != row["owner_tg_id"] or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", row["bot_username"]):
                    raise ValueError()
                bot_url = "https://t.me/" + row["bot_username"]
                bot_id = int(self._manager_token.split(":", 1)[0])
            except Exception:
                await self.store.finish(row, NotificationResult("blocked"), now=_now(self.clock()))
                counts["blocked"] += 1
                continue
            try:
                await self.sender.verify(self._manager_token, bot_id)
            except Exception:
                await self.store.finish(row, NotificationResult("retry"), now=_now(self.clock()))
                counts["retry"] += 1
                continue
            if not await self.store.begin_send(row, now=_now(self.clock())):
                continue
            try:
                outcome = await self.sender.send(self._manager_token, bot_id=bot_id, chat_id=recipient,
                                                text=ENTRY_CREATED_TEXT, bot_url=bot_url)
                if not isinstance(outcome, NotificationResult):
                    outcome = NotificationResult("uncertain")
            except Exception:
                outcome = NotificationResult("uncertain")
            try:
                if await self.store.finish(row, outcome, now=_now(self.clock())):
                    counts[outcome.state] += 1
            except Exception:
                counts["errors"] += 1
        return counts
