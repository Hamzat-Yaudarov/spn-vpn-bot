"""Durable subscription target and expiry for a provider-confirmed payment.

Never infer that an existing active subscription is a failed purchase. Old
empty placeholders may be reused only for the same user/type/index and only
when they have no credentials, term, or references from another operation.
"""
from datetime import datetime, timedelta

import database as db


class ActivationError(RuntimeError):
    pass


async def acquire_lock(tg_id):
    pool = await db.get_pool()
    conn = await pool.acquire()
    try:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", tg_id)
        if locked:
            return conn
    except BaseException:
        await pool.release(conn)
        raise
    await pool.release(conn)
    return None


async def release_lock(conn, tg_id):
    try:
        await conn.execute("SELECT pg_advisory_unlock($1::bigint)", tg_id)
    finally:
        await (await db.get_pool()).release(conn)


def valid_target(subscription, kind):
    return bool(subscription and subscription.get("generation") == "v2"
                and subscription.get("plan_kind") == kind
                and subscription.get("is_visible") and subscription.get("is_renewable"))


def choose_target(payment, subscriptions, reusable_ids, kind):
    """Pure selection, used both by the audit and the transactional reservation."""
    linked = payment.get("subscription_id")
    if linked:
        sub = next((s for s in subscriptions if s["id"] == linked), None)
        if not valid_target(sub, kind):
            raise ActivationError("Связанная подписка удалена, скрыта или имеет другой тип")
        return sub, sub.get("type_index")
    if (payment.get("payment_target") or "new") != "new":
        raise ActivationError("Платёж продления не привязан к подписке")

    index = payment.get("target_slot_number")
    if index is not None and (type(index) is not int or not 1 <= index <= db.MAX_SUBSCRIPTIONS_PER_USER):
        raise ActivationError("Некорректный номер подписки в платеже")
    visible = [s for s in subscriptions if s.get("generation") == "v2"
               and s.get("is_visible") and s.get("plan_kind") == kind]
    taken = {s.get("type_index") for s in visible}
    if index is None:
        index = next((i for i in range(1, db.MAX_SUBSCRIPTIONS_PER_USER + 1) if i not in taken), None)
    if index is None:
        raise ActivationError("Достигнут лимит подписок этого типа")
    matches = [s for s in visible if s.get("type_index") == index]
    if matches:
        # Never attach a new payment to an already issued key or steal a slot
        # reserved by a different invoice. Ambiguity requires an operator audit.
        if any(s["id"] not in reusable_ids or not valid_target(s, kind) for s in matches):
            raise ActivationError("Номер подписки уже занят; требуется сверка платежа")
        return min(matches, key=lambda s: s["id"]), index
    return None, index


async def reusable_placeholders(conn, payment):
    rows = await conn.fetch("""
        SELECT s.id FROM subscriptions s
        WHERE s.tg_id = $1 AND s.generation = 'v2'
          AND s.remnawave_uuid IS NULL AND s.remnawave_username IS NULL
          AND s.subscription_until IS NULL AND s.squad_uuid IS NULL
          AND s.created_at >= $2
          AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.subscription_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM traffic_purchases t WHERE t.subscription_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM device_addon_purchases d WHERE d.subscription_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM subscription_traffic_cycles c WHERE c.subscription_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM reactivation_offers r WHERE r.subscription_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM users u WHERE u.news_channel_bonus_subscription_id = s.id)
    """, payment["tg_id"], payment["created_at"])
    return {row["id"] for row in rows}


async def reserve(tg_id, invoice_id, tariff):
    """Called under the per-user payment lock; target+deadline commit before API."""
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        payment = await conn.fetchrow("SELECT * FROM payments WHERE invoice_id = $1 FOR UPDATE", invoice_id)
        if (not payment or payment["tg_id"] != tg_id or payment["status"] != "pending"
                or payment.get("refund_requested_at") is not None
                or (payment.get("payment_kind") or "subscription") != "subscription"):
            raise ActivationError("Платёж недоступен для активации")
        previous = await conn.fetchrow(
            "SELECT * FROM payment_subscription_activations WHERE invoice_id = $1", invoice_id)
        subscriptions = await conn.fetch("SELECT * FROM subscriptions WHERE tg_id = $1 ORDER BY id FOR UPDATE", tg_id)
        kind = tariff.get("kind", "regular")
        if previous:
            sub = next((s for s in subscriptions if s["id"] == previous["subscription_id"]), None)
            if not valid_target(sub, kind) or payment.get("subscription_id") != previous["subscription_id"]:
                raise ActivationError("Целевая подписка изменилась после начала активации")
            return dict(sub, payment_expires_at=previous["expires_at"])

        reusable = await reusable_placeholders(conn, payment)
        sub, index = choose_target(payment, subscriptions, reusable, kind)
        if sub is None:
            # The business limit is five visible subscriptions PER TYPE. A
            # storage slot is not a business limit and can include history.
            taken = {s["slot_number"] for s in subscriptions}
            slot = next(i for i in range(1, len(taken) + 2) if i not in taken)
            sub = await conn.fetchrow("""
                INSERT INTO subscriptions (tg_id, slot_number, plan_kind, type_index,
                    generation, is_visible, is_renewable, is_active, purchase_days)
                VALUES ($1, $2, $3, $4, 'v2', TRUE, TRUE, FALSE, $5) RETURNING *
            """, tg_id, slot, kind, index, tariff["days"])
        now = datetime.utcnow()
        existing = sub.get("subscription_until")
        # Old code could link a NEW payment after updating the local deadline
        # but before marking it paid. Completing that purchase adds no days.
        if payment.get("payment_target") != "renew" and payment.get("subscription_id") and existing:
            expires_at = existing
        else:
            expires_at = max(existing or now, now) + timedelta(days=tariff["days"])
        await conn.execute("""
            INSERT INTO payment_subscription_activations (invoice_id, subscription_id, expires_at)
            VALUES ($1, $2, $3)
        """, invoice_id, sub["id"], expires_at)
        await conn.execute("""
            UPDATE payments SET subscription_id = $2, target_slot_number = $3, updated_at = now()
            WHERE invoice_id = $1
        """, invoice_id, sub["id"], index)
        # Hide only unissued duplicates of the exact same placeholder. Keep
        # their rows for audit; no keys/users are deleted in Remnawave.
        duplicates = [s["id"] for s in subscriptions if s["id"] in reusable
                      and s["id"] != sub["id"] and s.get("plan_kind") == kind
                      and s.get("type_index") == index]
        if duplicates:
            await conn.execute("""
                UPDATE subscriptions SET is_active = FALSE, is_visible = FALSE,
                    is_renewable = FALSE, updated_at = now() WHERE id = ANY($1::bigint[])
            """, duplicates)
        return dict(sub, payment_expires_at=expires_at)


async def complete(tg_id, invoice_id, subscription, uuid, username, squad, traffic, device_limit, days):
    """Subscription state and paid status become durable in ONE transaction."""
    sid, until = subscription["id"], subscription["payment_expires_at"]
    notification_time, notification_type = db._calculate_notification_fields(until)
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        payment = await conn.fetchrow("SELECT * FROM payments WHERE invoice_id = $1 FOR UPDATE", invoice_id)
        if (not payment or payment["tg_id"] != tg_id or payment["status"] != "pending"
                or payment.get("subscription_id") != sid or payment.get("refund_requested_at")):
            raise ActivationError("Платёж изменился во время активации")
        saved = await conn.fetchrow("""
            UPDATE subscriptions SET remnawave_uuid = $1, remnawave_username = $2,
                subscription_until = $3, squad_uuid = $4, is_active = TRUE,
                traffic_enabled = $5, base_traffic_bytes = $6, carried_traffic_bytes = $7,
                current_paid_traffic_bytes = $8, current_period_limit_bytes = $9,
                traffic_reset_at = $10, hwid_device_limit = $11, last_known_used_traffic_bytes = $12,
                last_traffic_sync_at = now(), purchase_days = $13, next_notification_time = $14,
                notification_type = $15, updated_at = now()
            WHERE id = $16 AND tg_id = $17 AND is_visible AND is_renewable RETURNING id
        """, uuid, username, until, squad, traffic.enabled, traffic.base_bytes,
            traffic.carried_bytes, traffic.paid_bytes, traffic.limit_bytes, traffic.reset_at,
            device_limit, traffic.last_known_used_bytes, days, notification_time, notification_type, sid, tg_id)
        if not saved:
            raise ActivationError("Подписка удалена или скрыта во время активации")
        await conn.execute("UPDATE payments SET status = 'paid', updated_at = now() WHERE invoice_id = $1", invoice_id)
