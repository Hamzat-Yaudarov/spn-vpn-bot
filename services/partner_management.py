"""Tenant management ports. No configuration reads, network or send worker.

Actor IDs are accepted ONLY from the central manager's verified initData adapter.
The customer preference/start ports instead require the tenant webhook's verified
private-chat actor. Neither is a public permission/identity assertion endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import re
import secrets
from uuid import UUID, uuid4

from services.partner_store import (
    _positive_int,
    _project_id,
    _text,
    _url,
    InvalidProject,
)


class ManagementDenied(LookupError):
    """Unknown tenant/member or insufficient role. Do not reveal which."""


class ManagementConflict(RuntimeError):
    """Refresh the object or reuse the original immutable request parameters."""


PERMISSIONS = {
    "owner": frozenset(
        {"analytics", "customers", "orders", "referrals", "marketing", "team"}
    ),
    "viewer": frozenset({"analytics", "customers", "orders", "referrals"}),
    "support": frozenset({"customers", "orders", "referrals"}),
    "marketer": frozenset({"analytics", "referrals", "marketing"}),
}


def _uuid(value):
    return _project_id(value)


def _page(limit, cursor):
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(cursor) is not int
        or not 0 <= cursor <= 10000
    ):
        raise InvalidProject("Размер страницы: 1–100, смещение: 0–10000.")
    return limit, cursor


def _version(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 2147483646:
        raise InvalidProject("Обновите данные перед сохранением.")
    return value


def _plain(row):
    return {
        key: str(value) if isinstance(value, UUID) else value
        for key, value in dict(row).items()
    }


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _result(value):
    return json.loads(value) if isinstance(value, str) else dict(value)


class PartnerManagementStore:
    def __init__(
        self, pool_getter, *, delivery_enabled=False, support_delivery_enabled=False
    ):
        if not callable(pool_getter):
            raise TypeError("pool_getter must be callable")
        self._pool_getter = pool_getter
        if (
            type(delivery_enabled) is not bool
            or type(support_delivery_enabled) is not bool
        ):
            raise ValueError("Нужен явный флаг доставки.")
        self.delivery_enabled = delivery_enabled
        self.support_delivery_enabled = support_delivery_enabled

    async def _pool(self):
        pool = self._pool_getter()
        return await pool if inspect.isawaitable(pool) else pool

    @staticmethod
    async def _authorize(conn, pid, actor, permission, *, lock=True):
        # Within mutations this coordinates role revocation with the action.
        # Pure authorization has no surrounding transaction: its former lock
        # was released before the next SELECT anyway. Keep the lock for every
        # mutation caller, but make fresh read-only permission checks one query.
        if lock:
            await conn.fetchrow("SELECT id FROM wl_projects WHERE id=$1 FOR UPDATE", pid)
        row = await conn.fetchrow(
            """SELECT p.id,p.owner_tg_id,p.title,p.slug,p.status,p.bot_username,
            CASE WHEN p.owner_tg_id=$2 THEN 'owner' ELSE m.role END AS role
            FROM wl_projects p LEFT JOIN wl_project_members m
              ON m.project_id=p.id AND m.tg_id=$2 AND m.active
            WHERE p.id=$1 AND (p.owner_tg_id=$2 OR m.tg_id IS NOT NULL)""",
            pid,
            actor,
        )
        if not row or permission not in PERMISSIONS.get(row["role"], ()):
            raise ManagementDenied("Проект или действие недоступны.")
        return row

    async def authorize(self, project_id, actor_id, permission):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await self._authorize(conn, pid, actor, permission, lock=False)
        return {
            "project_id": str(pid),
            "role": row["role"],
            "permissions": sorted(PERMISSIONS[row["role"]]),
        }

    async def record_manager_identity(self, actor_id):
        """Called only AFTER central manager initData verification, never a body ID."""
        actor = _positive_int(actor_id, "actor_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO wl_manager_identities(tg_id) VALUES($1)
                ON CONFLICT(tg_id) DO UPDATE SET verified_at=now()""",
                actor,
            )

    async def visible_projects(self, actor_id):
        actor = _positive_int(actor_id, "actor_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT p.id,p.title,p.slug,p.status,p.price_kopecks,p.version,
                p.brand,p.support_url,p.news_url,p.bot_id,p.bot_username,p.owner_tg_id,p.created_at,p.updated_at,
                CASE WHEN p.owner_tg_id=$1 THEN 'owner' ELSE m.role END AS role
                FROM wl_projects p LEFT JOIN wl_project_members m
                  ON m.project_id=p.id AND m.tg_id=$1 AND m.active
                WHERE p.owner_tg_id=$1 OR m.tg_id IS NOT NULL
                ORDER BY p.created_at DESC,p.id LIMIT 101""",
                actor,
            )
        items = []
        for row in rows[:100]:
            item = _plain(row)
            item["brand"] = _result(row["brand"])
            item["bot"] = {
                "id": item.pop("bot_id"),
                "username": item.pop("bot_username"),
            }
            item["permissions"] = sorted(PERMISSIONS[row["role"]])
            items.append(item)
        return {"items": items, "truncated": len(rows) > 100}

    @staticmethod
    async def _request(conn, pid, actor, key, payload):
        # Serializes same-key retries between processes before inspecting the result.
        digest = _fingerprint(payload)
        lock = int.from_bytes(
            hashlib.sha256(f"wl-management:{pid}:{actor}:{key}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        await conn.execute("SELECT pg_advisory_xact_lock($1)", lock)
        old = await conn.fetchrow(
            """SELECT fingerprint,result FROM wl_management_requests
            WHERE project_id=$1 AND actor_tg_id=$2 AND request_key=$3""",
            pid,
            actor,
            key,
        )
        if old and old["fingerprint"] != digest:
            raise ManagementConflict("Параметры ранее созданного запроса изменились.")
        return digest, _result(old["result"]) if old else None

    @staticmethod
    async def _save_request(conn, pid, actor, key, digest, result):
        result = json.loads(json.dumps(result, default=str))
        await conn.execute(
            """INSERT INTO wl_management_requests(project_id,actor_tg_id,request_key,fingerprint,result)
            VALUES($1,$2,$3,$4,$5::jsonb)""",
            pid,
            actor,
            key,
            digest,
            json.dumps(result, default=str),
        )
        return result

    async def register_customer(
        self, project_id, tg_id, referrer_tg_id=None, *, campaign_code=None
    ):
        """Atomic verified-webhook registration + immutable first attribution.

        Reuses commerce's cross-process registration lock, including if the old
        bot route registers simultaneously. Existing customers never gain a new
        referrer or retroactive campaign. No settings or network are imported.
        """
        from services.partner_commerce_store import (
            _lock_key,
            CommerceNotFound,
            InvalidCommerce,
        )

        pid, tg = _uuid(project_id), _positive_int(tg_id, "tg_id")
        ref = (
            _positive_int(referrer_tg_id, "referrer_tg_id")
            if referrer_tg_id is not None
            else None
        )
        code = (
            campaign_code[2:]
            if isinstance(campaign_code, str) and campaign_code.startswith("m_")
            else campaign_code
        )
        code = (
            code
            if isinstance(code, str) and re.fullmatch(r"[a-z0-9]{20}", code)
            else None
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            # Repeat menu taps must not acquire a transaction/advisory lock and
            # rewrite an already frozen attribution. No cache: the exact tenant
            # and customer are read fresh. Legacy customers lacking attribution
            # still take the unchanged locked path below to freeze their first
            # (possibly empty) attribution, and new registrations keep all
            # existing cross-process/referral validation.
            existing = await conn.fetchrow(
                """SELECT c.* FROM wl_customers c
                JOIN wl_campaign_attributions a
                  ON a.project_id=c.project_id AND a.tg_id=c.tg_id
                JOIN wl_projects p ON p.id=c.project_id
                WHERE c.project_id=$1 AND c.tg_id=$2""",
                pid,
                tg,
            )
            if existing:
                return _plain(existing)
            async with conn.transaction():
                if not await conn.fetchval(
                    "SELECT id FROM wl_projects WHERE id=$1", pid
                ):
                    raise CommerceNotFound("Проект недоступен.")
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    _lock_key("wl-customer", pid, tg),
                )
                row = await conn.fetchrow(
                    "SELECT * FROM wl_customers WHERE project_id=$1 AND tg_id=$2",
                    pid,
                    tg,
                )
                fresh = row is None
                if fresh:
                    if ref is not None and (
                        ref == tg
                        or not await conn.fetchval(
                            "SELECT tg_id FROM wl_customers WHERE project_id=$1 AND tg_id=$2",
                            pid,
                            ref,
                        )
                    ):
                        raise InvalidCommerce("Приглашающий не найден в этом проекте.")
                    row = await conn.fetchrow(
                        """INSERT INTO wl_customers(id,project_id,tg_id,referrer_tg_id)
                        VALUES($1,$2,$3,$4) RETURNING *""",
                        uuid4(),
                        pid,
                        tg,
                        ref,
                    )
                cid = (
                    await conn.fetchval(
                        "SELECT id FROM wl_campaigns WHERE project_id=$1 AND code=$2 AND status='active'",
                        pid,
                        code,
                    )
                    if fresh and code
                    else None
                )
                await conn.execute(
                    """INSERT INTO wl_campaign_attributions(project_id,tg_id,campaign_id)
                    VALUES($1,$2,$3) ON CONFLICT(project_id,tg_id) DO NOTHING""",
                    pid,
                    tg,
                    cid,
                )
                return _plain(row)

    async def get_marketing_consent(self, project_id, tg_id):
        pid, tg = _uuid(project_id), _positive_int(tg_id, "tg_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT coalesce(p.opted_in,false) AS enabled,coalesce(p.blocked,false) AS blocked
                FROM wl_customers c LEFT JOIN wl_marketing_preferences p ON p.project_id=c.project_id AND p.tg_id=c.tg_id
                WHERE c.project_id=$1 AND c.tg_id=$2""",
                pid,
                tg,
            )
            if not row:
                raise ManagementDenied("Клиент недоступен.")
            return _plain(row)

    async def set_marketing_consent(self, project_id, tg_id, enabled):
        await self.set_marketing_preference(project_id, tg_id, opted_in=enabled)
        return await self.get_marketing_consent(project_id, tg_id)

    async def overview(self, project_id, actor_id):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._authorize(conn, pid, actor, "analytics")
            row = await conn.fetchrow(
                """SELECT
                (SELECT count(*) FROM wl_customers WHERE project_id=$1) AS customers,
                (SELECT count(*) FROM wl_subscriptions WHERE project_id=$1 AND expires_at>now()) AS active_subscriptions,
                (SELECT count(*) FROM wl_orders WHERE project_id=$1 AND paid_at IS NOT NULL) AS paid_orders,
                (SELECT coalesce(sum(amount_kopecks),0)::bigint FROM wl_revenue_ledger WHERE project_id=$1) AS revenue_kopecks,
                (SELECT coalesce(sum(partner_kopecks),0)::bigint FROM wl_revenue_ledger WHERE project_id=$1) AS partner_kopecks""",
                pid,
            )
            return _plain(row)

    async def customers(self, project_id, actor_id, *, query="", limit=50, cursor=0):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        limit, cursor = _page(limit, cursor)
        if not isinstance(query, str) or not re.fullmatch(r"[0-9]{0,19}", query):
            raise InvalidProject("Поиск клиентов — по Telegram ID.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._authorize(conn, pid, actor, "customers")
            rows = await conn.fetch(
                """SELECT c.id,c.tg_id,c.created_at,c.referrer_tg_id,
                s.id AS subscription_id,s.expires_at,s.device_limit,
                (SELECT count(*) FROM wl_orders o WHERE o.project_id=c.project_id AND o.tg_id=c.tg_id AND o.paid_at IS NOT NULL) AS paid_orders
                FROM wl_customers c LEFT JOIN wl_subscriptions s ON s.project_id=c.project_id AND s.tg_id=c.tg_id
                WHERE c.project_id=$1 AND ($2='' OR c.tg_id::text LIKE $2||'%')
                ORDER BY c.created_at DESC,c.tg_id DESC LIMIT $3 OFFSET $4""",
                pid,
                query,
                limit + 1,
                cursor,
            )
        return {
            "items": [_plain(r) for r in rows[:limit]],
            "next_cursor": (
                cursor + limit
                if len(rows) > limit and cursor + limit <= 10000
                else None
            ),
        }

    async def subscriptions(self, project_id, actor_id, *, limit=50, cursor=0):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        limit, cursor = _page(limit, cursor)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._authorize(conn, pid, actor, "customers")
            rows = await conn.fetch(
                """SELECT s.id,s.tg_id,s.expires_at,s.desired_expires_at,s.device_limit,
                CASE WHEN s.traffic_cycle_baseline_bytes IS NOT NULL AND s.traffic_cycle_hold IS NULL
                    AND s.traffic_cycle_baseline_bytes<=s.traffic_limit_bytes
                    THEN s.traffic_limit_bytes-s.traffic_cycle_baseline_bytes ELSE NULL END AS traffic_period_limit_bytes,
                CASE WHEN s.traffic_cycle_baseline_bytes IS NOT NULL AND s.traffic_cycle_hold IS NULL
                    AND s.traffic_cycle_baseline_bytes<=s.traffic_limit_bytes THEN 'ready' ELSE 'review' END AS traffic_state,
                CASE WHEN EXISTS(SELECT 1 FROM wl_activation_jobs j WHERE j.project_id=s.project_id
                    AND j.subscription_id=s.id AND j.status<>'complete') THEN 'pending'
                    WHEN s.expires_at IS NULL THEN 'unpaid' WHEN s.expires_at>now() THEN 'active' ELSE 'expired' END AS status,
                s.last_activation_sequence,s.created_at,s.updated_at
                FROM wl_subscriptions s WHERE s.project_id=$1
                ORDER BY created_at DESC,id DESC LIMIT $2 OFFSET $3""",
                pid,
                limit + 1,
                cursor,
            )
        return {
            "items": [_plain(r) for r in rows[:limit]],
            "next_cursor": (
                cursor + limit
                if len(rows) > limit and cursor + limit <= 10000
                else None
            ),
        }

    async def orders(self, project_id, actor_id, *, tg_id=None, limit=50, cursor=0):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        limit, cursor = _page(limit, cursor)
        if tg_id is not None:
            tg_id = _positive_int(tg_id, "tg_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._authorize(conn, pid, actor, "orders")
            rows = await conn.fetch(
                """SELECT id,tg_id,subscription_id,price_kopecks,currency,purpose,
                status,activation_issue,created_at,paid_at FROM wl_orders
                WHERE project_id=$1 AND ($2::bigint IS NULL OR tg_id=$2)
                ORDER BY created_at DESC,id DESC LIMIT $3 OFFSET $4""",
                pid,
                tg_id,
                limit + 1,
                cursor,
            )
        return {
            "items": [_plain(r) for r in rows[:limit]],
            "next_cursor": (
                cursor + limit
                if len(rows) > limit and cursor + limit <= 10000
                else None
            ),
        }

    async def referrals(self, project_id, actor_id):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._authorize(conn, pid, actor, "referrals")
            row = await conn.fetchrow(
                """SELECT
                (SELECT count(*) FROM wl_customers WHERE project_id=$1 AND referrer_tg_id IS NOT NULL) AS invited,
                count(*) AS qualified,coalesce(sum(g.days),0)::bigint AS earned_days,
                coalesce(sum(g.days) FILTER(WHERE j.status IS DISTINCT FROM 'complete'),0)::bigint AS pending_days,
                coalesce(sum(g.days) FILTER(WHERE j.status='complete'),0)::bigint AS applied_days
                FROM wl_referral_bonus_grants g LEFT JOIN wl_activation_jobs j
                  ON j.project_id=g.project_id AND j.job_id=g.activation_job_id WHERE g.project_id=$1""",
                pid,
            )
        return _plain(row)

    async def team(self, project_id, actor_id):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        pool = await self._pool()
        async with pool.acquire() as conn:
            project = await self._authorize(conn, pid, actor, "team")
            rows = await conn.fetch(
                "SELECT tg_id,role,version,active,updated_at FROM wl_project_members WHERE project_id=$1 ORDER BY tg_id LIMIT 101",
                pid,
            )
        return {
            "owner_tg_id": project["owner_tg_id"],
            "items": [_plain(row) for row in rows[:100]],
            "truncated": len(rows) > 100,
        }

    async def set_member(
        self, project_id, actor_id, request_key, *, tg_id, role, expected_version
    ):
        pid, actor, key = (
            _uuid(project_id),
            _positive_int(actor_id, "actor_id"),
            _uuid(request_key),
        )
        target, version = _positive_int(tg_id, "tg_id"), _version(
            expected_version, zero=True
        )
        if role is not None and (
            not isinstance(role, str) or role not in {"viewer", "support", "marketer"}
        ):
            raise InvalidProject("Роль: viewer, support или marketer.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                project = await self._authorize(conn, pid, actor, "team")
                if target == project["owner_tg_id"]:
                    raise InvalidProject("Владелец проекта неизменяем.")
                digest, old = await self._request(
                    conn,
                    pid,
                    actor,
                    key,
                    {
                        "action": "member",
                        "tg_id": target,
                        "role": role,
                        "version": version,
                    },
                )
                if old:
                    return old
                if not await conn.fetchval(
                    "SELECT tg_id FROM wl_manager_identities WHERE tg_id=$1", target
                ):
                    raise InvalidProject(
                        "Участник должен сначала открыть центральный кабинет через Telegram."
                    )
                await conn.fetchrow(
                    "SELECT id FROM wl_projects WHERE id=$1 FOR UPDATE", pid
                )
                member = await conn.fetchrow(
                    "SELECT * FROM wl_project_members WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    pid,
                    target,
                )
                if (member["version"] if member else 0) != version:
                    raise ManagementConflict(
                        "Состав команды изменился. Обновите список."
                    )
                if not member and role is None:
                    raise InvalidProject("Участник не найден.")
                if (
                    not member
                    and await conn.fetchval(
                        "SELECT count(*) FROM wl_project_members WHERE project_id=$1 AND active",
                        pid,
                    )
                    >= 100
                ):
                    raise InvalidProject("Достигнут лимит команды.")
                row = (
                    await conn.fetchrow(
                        """INSERT INTO wl_project_members(project_id,tg_id,role) VALUES($1,$2,$3)
                    ON CONFLICT(project_id,tg_id) DO UPDATE SET role=coalesce($3,wl_project_members.role),
                    active=($3 IS NOT NULL),version=wl_project_members.version+1,updated_at=now()
                    RETURNING tg_id,role,version,active""",
                        pid,
                        target,
                        role or (member["role"] if member else None),
                    )
                    if role is not None
                    else await conn.fetchrow(
                        """UPDATE wl_project_members SET active=false,version=version+1,updated_at=now()
                    WHERE project_id=$1 AND tg_id=$2 RETURNING tg_id,role,version,active""",
                        pid,
                        target,
                    )
                )
                return await self._save_request(
                    conn, pid, actor, key, digest, _plain(row)
                )

    @staticmethod
    async def _campaign_public(conn, row):
        result = _plain(row)
        pid, cid = row["project_id"], row["id"]
        result.pop("project_id", None)
        username = await conn.fetchval(
            "SELECT bot_username FROM wl_projects WHERE id=$1", pid
        )
        result["start_payload"] = "m_" + row["code"]
        result["link"] = (
            f"https://t.me/{username}?start=m_{row['code']}" if username else ""
        )
        stats = await conn.fetchrow(
            """SELECT
            (SELECT count(*) FROM wl_campaign_attributions a WHERE a.project_id=$1 AND a.campaign_id=$2) AS registrations,
            count(*) FILTER(WHERE o.purpose='subscription' AND NOT EXISTS(
                SELECT 1 FROM wl_orders prev WHERE prev.project_id=o.project_id AND prev.tg_id=o.tg_id
                AND prev.purpose='subscription' AND prev.paid_at IS NOT NULL
                AND (prev.paid_at,prev.id)<(o.paid_at,o.id))) AS first_purchases,
            count(*) FILTER(WHERE o.purpose='subscription' AND EXISTS(
                SELECT 1 FROM wl_orders prev WHERE prev.project_id=o.project_id AND prev.tg_id=o.tg_id
                AND prev.purpose='subscription' AND prev.paid_at IS NOT NULL
                AND (prev.paid_at,prev.id)<(o.paid_at,o.id))) AS repeat_purchases,
            coalesce(sum(o.price_kopecks),0)::bigint AS revenue_kopecks
            FROM wl_orders o JOIN wl_campaign_attributions a ON a.project_id=o.project_id AND a.tg_id=o.tg_id
            WHERE o.project_id=$1 AND a.campaign_id=$2 AND o.paid_at IS NOT NULL""",
            pid,
            cid,
        )
        result.update(_plain(stats))
        return result

    async def campaigns(self, project_id, actor_id, *, limit=50, cursor=0):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        limit, cursor = _page(limit, cursor)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._authorize(conn, pid, actor, "marketing")
            rows = await conn.fetch(
                "SELECT * FROM wl_campaigns WHERE project_id=$1 ORDER BY created_at DESC,id DESC LIMIT $2 OFFSET $3",
                pid,
                limit + 1,
                cursor,
            )
            items = [await self._campaign_public(conn, row) for row in rows[:limit]]
        return {
            "items": items,
            "next_cursor": (
                cursor + limit
                if len(rows) > limit and cursor + limit <= 10000
                else None
            ),
        }

    async def save_campaign(
        self,
        project_id,
        actor_id,
        request_key,
        *,
        title,
        campaign_id=None,
        expected_version=0,
        status="active",
    ):
        pid, actor, key = (
            _uuid(project_id),
            _positive_int(actor_id, "actor_id"),
            _uuid(request_key),
        )
        title = _text(title, "title", maximum=80)
        version = _version(expected_version, zero=True)
        cid = _uuid(campaign_id) if campaign_id is not None else None
        if (
            not isinstance(status, str)
            or status not in {"active", "archived"}
            or (cid is None and (version != 0 or status != "active"))
        ):
            raise InvalidProject("Некорректное состояние кампании.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._authorize(conn, pid, actor, "marketing")
                digest, old = await self._request(
                    conn,
                    pid,
                    actor,
                    key,
                    {
                        "action": "campaign",
                        "title": title,
                        "id": str(cid) if cid else None,
                        "version": version,
                        "status": status,
                    },
                )
                if old:
                    return old
                if cid:
                    row = await conn.fetchrow(
                        """UPDATE wl_campaigns SET title=$3,status=$4,version=version+1,updated_at=now()
                        WHERE project_id=$1 AND id=$2 AND version=$5 RETURNING *""",
                        pid,
                        cid,
                        title,
                        status,
                        version,
                    )
                    if not row:
                        raise ManagementConflict("Кампания изменена или недоступна.")
                else:
                    row = await conn.fetchrow(
                        """INSERT INTO wl_campaigns(id,project_id,title,code) VALUES($1,$2,$3,$4) RETURNING *""",
                        uuid4(),
                        pid,
                        title,
                        secrets.token_hex(10),
                    )
                result = await self._campaign_public(conn, row)
                return await self._save_request(conn, pid, actor, key, digest, result)

    async def record_first_start(self, project_id, tg_id, payload, *, new_customer):
        """Internal verified tenant webhook port; call on EVERY first registration.

        The first recorded start wins, even if it had no campaign. Existing users
        are never retro-attributed; attribution never updates the referrer.
        """
        pid, tg = _uuid(project_id), _positive_int(tg_id, "tg_id")
        if type(new_customer) is not bool:
            raise InvalidProject("Нужно подтверждённое состояние регистрации.")
        code = (
            payload[2:]
            if isinstance(payload, str) and re.fullmatch(r"m_[a-z0-9]{20}", payload)
            else None
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                customer = await conn.fetchrow(
                    "SELECT created_at FROM wl_customers WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    pid,
                    tg,
                )
                if not customer:
                    raise ManagementDenied("Клиент недоступен.")
                cid = None
                if new_customer and code:
                    cid = await conn.fetchval(
                        """SELECT id FROM wl_campaigns WHERE project_id=$1 AND code=$2 AND status='active'
                        AND created_at<=$3""",
                        pid,
                        code,
                        customer["created_at"],
                    )
                await conn.execute(
                    """INSERT INTO wl_campaign_attributions(project_id,tg_id,campaign_id) VALUES($1,$2,$3)
                    ON CONFLICT(project_id,tg_id) DO NOTHING""",
                    pid,
                    tg,
                    cid,
                )

    async def set_marketing_preference(
        self, project_id, tg_id, *, opted_in, blocked=False
    ):
        """Internal verified CUSTOMER identity port, never a partner audience editor."""
        pid, tg = _uuid(project_id), _positive_int(tg_id, "tg_id")
        if type(opted_in) is not bool or type(blocked) is not bool:
            raise InvalidProject("Некорректное согласие.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await conn.fetchval(
                    "SELECT tg_id FROM wl_customers WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    pid,
                    tg,
                ):
                    raise ManagementDenied("Клиент недоступен.")
                await conn.execute(
                    """INSERT INTO wl_marketing_preferences(project_id,tg_id,opted_in,blocked) VALUES($1,$2,$3,$4)
                    ON CONFLICT(project_id,tg_id) DO UPDATE SET opted_in=$3,blocked=$4,updated_at=now()""",
                    pid,
                    tg,
                    opted_in,
                    blocked,
                )
                if not opted_in or blocked:
                    await conn.execute(
                        """UPDATE wl_marketing_recipients SET status='canceled'
                        WHERE project_id=$1 AND tg_id=$2 AND status IN ('review','queued','leased')""",
                        pid,
                        tg,
                    )

    async def _broadcast_public(self, conn, row):
        result = _plain(row)
        for key in ("project_id", "review_token_hash"):
            result.pop(key, None)
        counts = await conn.fetch(
            """SELECT status,count(*) AS count FROM wl_marketing_recipients
            WHERE project_id=$1 AND broadcast_id=$2 GROUP BY status""",
            row["project_id"],
            row["id"],
        )
        result["recipients"] = {r["status"]: r["count"] for r in counts}
        result["delivery_enabled"] = self.delivery_enabled
        return result

    async def broadcasts(self, project_id, actor_id, *, limit=50, cursor=0):
        pid, actor = _uuid(project_id), _positive_int(actor_id, "actor_id")
        limit, cursor = _page(limit, cursor)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._authorize(conn, pid, actor, "marketing")
            rows = await conn.fetch(
                "SELECT * FROM wl_marketing_broadcasts WHERE project_id=$1 ORDER BY created_at DESC,id DESC LIMIT $2 OFFSET $3",
                pid,
                limit + 1,
                cursor,
            )
            items = [await self._broadcast_public(conn, row) for row in rows[:limit]]
        return {
            "items": items,
            "next_cursor": (
                cursor + limit
                if len(rows) > limit and cursor + limit <= 10000
                else None
            ),
        }

    async def save_broadcast(
        self,
        project_id,
        actor_id,
        request_key,
        *,
        text,
        broadcast_id=None,
        expected_version=0,
    ):
        pid, actor, key = (
            _uuid(project_id),
            _positive_int(actor_id, "actor_id"),
            _uuid(request_key),
        )
        message = _text(text, "message", maximum=3000)
        version = _version(expected_version, zero=True)
        bid = _uuid(broadcast_id) if broadcast_id is not None else None
        if bid is None and version != 0:
            raise InvalidProject("Некорректная версия.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._authorize(conn, pid, actor, "marketing")
                digest, old = await self._request(
                    conn,
                    pid,
                    actor,
                    key,
                    {
                        "action": "broadcast",
                        "text": message,
                        "id": str(bid) if bid else None,
                        "version": version,
                    },
                )
                if old:
                    return old
                if bid:
                    row = await conn.fetchrow(
                        """UPDATE wl_marketing_broadcasts SET text=$3,status='draft',version=version+1,
                        review_token_hash=NULL,reviewed_at=NULL,updated_at=now()
                        WHERE project_id=$1 AND id=$2 AND version=$4 AND status IN ('draft','review') RETURNING *""",
                        pid,
                        bid,
                        message,
                        version,
                    )
                    if not row:
                        raise ManagementConflict(
                            "Рассылка изменена или уже подтверждена."
                        )
                    await conn.execute(
                        "DELETE FROM wl_marketing_recipients WHERE project_id=$1 AND broadcast_id=$2",
                        pid,
                        bid,
                    )
                else:
                    row = await conn.fetchrow(
                        "INSERT INTO wl_marketing_broadcasts(id,project_id,text) VALUES($1,$2,$3) RETURNING *",
                        uuid4(),
                        pid,
                        message,
                    )
                return await self._save_request(
                    conn,
                    pid,
                    actor,
                    key,
                    digest,
                    await self._broadcast_public(conn, row),
                )

    async def review_broadcast(
        self, project_id, actor_id, broadcast_id, expected_version
    ):
        pid, actor, bid = (
            _uuid(project_id),
            _positive_int(actor_id, "actor_id"),
            _uuid(broadcast_id),
        )
        version = _version(expected_version)
        token = secrets.token_urlsafe(32)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._authorize(conn, pid, actor, "marketing")
                row = await conn.fetchrow(
                    """UPDATE wl_marketing_broadcasts SET status='review',version=version+1,
                    review_token_hash=$4,reviewed_at=now(),updated_at=now()
                    WHERE project_id=$1 AND id=$2 AND version=$3 AND status IN ('draft','review') RETURNING *""",
                    pid,
                    bid,
                    version,
                    hashlib.sha256(token.encode()).hexdigest(),
                )
                if not row:
                    raise ManagementConflict("Рассылка изменена или уже подтверждена.")
                if (
                    await conn.fetchval(
                        "SELECT count(*) FROM wl_marketing_preferences WHERE project_id=$1 AND opted_in AND NOT blocked",
                        pid,
                    )
                    > 10000
                ):
                    raise InvalidProject(
                        "За один подтверждённый запуск разрешено не больше 10000 получателей."
                    )
                await conn.execute(
                    "DELETE FROM wl_marketing_recipients WHERE project_id=$1 AND broadcast_id=$2",
                    pid,
                    bid,
                )
                await conn.execute(
                    """INSERT INTO wl_marketing_recipients(project_id,broadcast_id,tg_id)
                    SELECT project_id,$2,tg_id FROM wl_marketing_preferences WHERE project_id=$1 AND opted_in AND NOT blocked""",
                    pid,
                    bid,
                )
                result = await self._broadcast_public(conn, row)
                result["review_token"] = token
                result["eligible_recipients"] = result["recipients"].get("review", 0)
                result["requires_confirmation"] = True
                return result

    async def confirm_broadcast(
        self,
        project_id,
        actor_id,
        request_key,
        *,
        broadcast_id,
        expected_version,
        review_token,
    ):
        pid, actor, key, bid = (
            _uuid(project_id),
            _positive_int(actor_id, "actor_id"),
            _uuid(request_key),
            _uuid(broadcast_id),
        )
        version = _version(expected_version)
        if not isinstance(review_token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", review_token
        ):
            raise InvalidProject("Проверьте текст и аудиторию заново.")
        token_hash = hashlib.sha256(review_token.encode()).hexdigest()
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._authorize(conn, pid, actor, "marketing")
                digest, old = await self._request(
                    conn,
                    pid,
                    actor,
                    key,
                    {
                        "action": "confirm_broadcast",
                        "id": str(bid),
                        "version": version,
                        "review": token_hash,
                    },
                )
                if old:
                    return old
                row = await conn.fetchrow(
                    """SELECT * FROM wl_marketing_broadcasts WHERE project_id=$1 AND id=$2 FOR UPDATE""",
                    pid,
                    bid,
                )
                if (
                    not row
                    or row["status"] != "review"
                    or row["version"] != version
                    or not secrets.compare_digest(
                        row["review_token_hash"] or "", token_hash
                    )
                ):
                    raise ManagementConflict(
                        "Проверьте актуальный текст и аудиторию заново."
                    )
                # Strictly bounded confirmation freshness; a new review is needed later.
                if not await conn.fetchval(
                    "SELECT reviewed_at>now()-interval '30 minutes' FROM wl_marketing_broadcasts WHERE project_id=$1 AND id=$2",
                    pid,
                    bid,
                ):
                    raise ManagementConflict(
                        "Срок подтверждения истёк. Обновите предпросмотр."
                    )
                await conn.execute(
                    """UPDATE wl_marketing_recipients r SET status=CASE WHEN EXISTS(
                    SELECT 1 FROM wl_marketing_preferences p WHERE p.project_id=r.project_id AND p.tg_id=r.tg_id AND p.opted_in AND NOT p.blocked)
                    THEN 'queued' ELSE 'canceled' END WHERE r.project_id=$1 AND r.broadcast_id=$2 AND r.status='review'""",
                    pid,
                    bid,
                )
                row = await conn.fetchrow(
                    """UPDATE wl_marketing_broadcasts SET status='queued',version=version+1,
                    queued_at=now(),updated_at=now() WHERE project_id=$1 AND id=$2 RETURNING *""",
                    pid,
                    bid,
                )
                return await self._save_request(
                    conn,
                    pid,
                    actor,
                    key,
                    digest,
                    await self._broadcast_public(conn, row),
                )

    async def cancel_broadcast(
        self, project_id, actor_id, request_key, *, broadcast_id, expected_version
    ):
        pid, actor, key, bid = (
            _uuid(project_id),
            _positive_int(actor_id, "actor_id"),
            _uuid(request_key),
            _uuid(broadcast_id),
        )
        version = _version(expected_version)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._authorize(conn, pid, actor, "marketing")
                digest, old = await self._request(
                    conn,
                    pid,
                    actor,
                    key,
                    {"action": "cancel_broadcast", "id": str(bid), "version": version},
                )
                if old:
                    return old
                row = await conn.fetchrow(
                    """UPDATE wl_marketing_broadcasts SET status='canceled',version=version+1,updated_at=now()
                    WHERE project_id=$1 AND id=$2 AND version=$3 AND status IN ('draft','review','queued') RETURNING *""",
                    pid,
                    bid,
                    version,
                )
                if not row:
                    raise ManagementConflict("Рассылка изменена или уже завершена.")
                await conn.execute(
                    """UPDATE wl_marketing_recipients SET status='canceled'
                    WHERE project_id=$1 AND broadcast_id=$2 AND status IN ('review','queued','leased')""",
                    pid,
                    bid,
                )
                return await self._save_request(
                    conn,
                    pid,
                    actor,
                    key,
                    digest,
                    await self._broadcast_public(conn, row),
                )

    async def resend_subscription(self, project_id, actor_id, request_key, *, tg_id):
        """Queue only an existing paid, issued subscription. Never return its URL.

        The separate operator-enabled factual delivery worker must obtain the
        current branded capability from the subscription-page service. This
        intent cannot create a subscription or change its limits/expiration.
        """
        pid, actor, key, tg = (
            _uuid(project_id),
            _positive_int(actor_id, "actor_id"),
            _uuid(request_key),
            _positive_int(tg_id, "tg_id"),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await self._authorize(conn, pid, actor, "customers")
                if row["role"] not in {"owner", "support"}:
                    raise ManagementDenied("Действие недоступно.")
                digest, old = await self._request(
                    conn,
                    pid,
                    actor,
                    key,
                    {"action": "resend_subscription", "tg_id": tg},
                )
                if old:
                    return old
                sub = await conn.fetchrow(
                    """SELECT s.id FROM wl_subscriptions s WHERE s.project_id=$1 AND s.tg_id=$2
                    AND s.expires_at>now() AND s.remote_user_id IS NOT NULL AND s.subscription_url IS NOT NULL
                    AND EXISTS(SELECT 1 FROM wl_orders o WHERE o.project_id=s.project_id AND o.subscription_id=s.id
                      AND o.purpose='subscription' AND o.paid_at IS NOT NULL) FOR UPDATE""",
                    pid,
                    tg,
                )
                if not sub:
                    raise ManagementConflict(
                        "Нет действующей оплаченной и выданной подписки."
                    )
                previous = await conn.fetchrow(
                    """SELECT id FROM wl_support_resend_intents WHERE project_id=$1 AND tg_id=$2
                    AND (status IN ('queued','leased','sending','uncertain') OR created_at>now()-interval '10 minutes') LIMIT 1""",
                    pid,
                    tg,
                )
                if previous:
                    raise ManagementConflict(
                        "Отправка уже запрошена. Проверьте состояние позже."
                    )
                intent = uuid4()
                await conn.execute(
                    """INSERT INTO wl_support_resend_intents(id,project_id,tg_id,subscription_id,actor_tg_id)
                    VALUES($1,$2,$3,$4,$5)""",
                    intent,
                    pid,
                    tg,
                    sub["id"],
                    actor,
                )
                return await self._save_request(
                    conn,
                    pid,
                    actor,
                    key,
                    digest,
                    {
                        "id": str(intent),
                        "status": "queued",
                        "delivery_enabled": self.support_delivery_enabled,
                    },
                )

    async def get_resend(self, project_id, actor_id, intent_id):
        pid, actor, iid = (
            _uuid(project_id),
            _positive_int(actor_id, "actor_id"),
            _uuid(intent_id),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            role = await self._authorize(conn, pid, actor, "customers")
            if role["role"] not in {"owner", "support"}:
                raise ManagementDenied("Действие недоступно.")
            row = await conn.fetchrow(
                "SELECT id,tg_id,status,created_at FROM wl_support_resend_intents WHERE project_id=$1 AND id=$2",
                pid,
                iid,
            )
            if not row:
                raise ManagementDenied("Отправка недоступна.")
            return {**_plain(row), "delivery_enabled": self.support_delivery_enabled}

    async def claim_support(self, *, now=None):
        if not self.support_delivery_enabled:
            return None
        now = _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE wl_support_resend_intents SET status=CASE WHEN status='sending' THEN 'uncertain' ELSE 'queued' END
                    WHERE id IN (SELECT id FROM wl_support_resend_intents WHERE status IN ('leased','sending') AND lease_until<=$1
                    ORDER BY lease_until,id LIMIT 100 FOR UPDATE SKIP LOCKED)""",
                    now,
                )
                await conn.execute(
                    """UPDATE wl_support_resend_intents r SET status='blocked' WHERE r.id IN (
                    SELECT i.id FROM wl_support_resend_intents i JOIN wl_subscriptions s ON s.project_id=i.project_id AND s.id=i.subscription_id AND s.tg_id=i.tg_id
                    WHERE i.status='queued' AND (s.expires_at IS NULL OR s.expires_at<=$1 OR s.remote_user_id IS NULL OR s.subscription_url IS NULL)
                    ORDER BY i.created_at LIMIT 100 FOR UPDATE OF i SKIP LOCKED)""",
                    now,
                )
                row = await conn.fetchrow(
                    """SELECT r.id,r.project_id,r.tg_id,r.subscription_id,p.bot_id,p.encrypted_token
                    FROM wl_support_resend_intents r JOIN wl_projects p ON p.id=r.project_id
                    JOIN wl_subscriptions s ON s.project_id=r.project_id AND s.id=r.subscription_id AND s.tg_id=r.tg_id
                    WHERE r.status='queued' AND r.next_attempt_at<=$1 AND p.status='active' AND p.bot_id IS NOT NULL
                    AND s.expires_at>$1 AND s.remote_user_id IS NOT NULL AND s.subscription_url IS NOT NULL
                    AND EXISTS(SELECT 1 FROM wl_orders o WHERE o.project_id=s.project_id AND o.subscription_id=s.id
                        AND o.tg_id=s.tg_id AND o.purpose='subscription' AND o.paid_at IS NOT NULL)
                    ORDER BY r.created_at,r.id LIMIT 1 FOR UPDATE OF r SKIP LOCKED""",
                    now,
                )
                if not row:
                    return None
                token = uuid4()
                await conn.execute(
                    """UPDATE wl_support_resend_intents SET status='leased',lease_token=$2,lease_until=$3,attempts=attempts+1 WHERE id=$1""",
                    row["id"],
                    token,
                    now + timedelta(minutes=2),
                )
                return {**_plain(row), "lease_token": str(token)}

    async def begin_support_send(self, row, *, now=None):
        if not self.support_delivery_enabled:
            return False
        pool = await self._pool()
        async with pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """UPDATE wl_support_resend_intents r SET status='sending'
                WHERE r.project_id=$1 AND r.id=$2 AND r.lease_token=$3 AND r.status='leased' AND r.lease_until>$4
                AND EXISTS(SELECT 1 FROM wl_projects p WHERE p.id=r.project_id AND p.status='active' AND p.bot_id=$5)
                AND EXISTS(SELECT 1 FROM wl_subscriptions s WHERE s.project_id=r.project_id AND s.id=r.subscription_id AND s.tg_id=r.tg_id
                    AND s.expires_at>$4 AND s.remote_user_id IS NOT NULL AND s.subscription_url IS NOT NULL
                    AND EXISTS(SELECT 1 FROM wl_orders o WHERE o.project_id=s.project_id AND o.subscription_id=s.id AND o.tg_id=s.tg_id
                        AND o.purpose='subscription' AND o.paid_at IS NOT NULL)) RETURNING r.id""",
                    _uuid(row["project_id"]),
                    _uuid(row["id"]),
                    _uuid(row["lease_token"]),
                    _now(now),
                    row["bot_id"],
                )
            )

    async def finish_support(self, row, result, *, now=None):
        from services.partner_notifications import NotificationResult

        if (
            not self.support_delivery_enabled
            or not isinstance(result, NotificationResult)
            or result.state not in {"sent", "retry", "blocked", "uncertain"}
        ):
            raise InvalidProject("Некорректный результат доставки.")
        if type(result.retry_after) is not int or not 1 <= result.retry_after <= 86400:
            raise InvalidProject("Некорректная задержка.")
        if result.state == "sent":
            _positive_int(result.message_id, "message_id")
        state = {
            "sent": "delivered",
            "retry": "queued",
            "blocked": "blocked",
            "uncertain": "uncertain",
        }[result.state]
        accepted = (
            ["sending", "uncertain"]
            if result.state == "sent"
            else (
                ["leased", "sending"]
                if result.state in {"retry", "blocked"}
                else ["sending"]
            )
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """UPDATE wl_support_resend_intents SET status=$4,next_attempt_at=$5,telegram_message_id=$6
                WHERE project_id=$1 AND id=$2 AND lease_token=$3 AND status=ANY($7::text[]) RETURNING id""",
                    _uuid(row["project_id"]),
                    _uuid(row["id"]),
                    _uuid(row["lease_token"]),
                    state,
                    _now(now) + timedelta(seconds=result.retry_after),
                    result.message_id if state == "delivered" else None,
                    accepted,
                )
            )

    async def claim_marketing(self, *, now=None):
        if not self.delivery_enabled:
            return None
        now = _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE wl_marketing_broadcasts SET status='complete',version=version+1,updated_at=now()
                    WHERE (project_id,id) IN (SELECT b.project_id,b.id FROM wl_marketing_broadcasts b
                    WHERE b.status='queued' AND NOT EXISTS(SELECT 1 FROM wl_marketing_recipients r
                        WHERE r.project_id=b.project_id AND r.broadcast_id=b.id
                        AND r.status IN ('review','queued','leased','sending','uncertain'))
                    ORDER BY b.queued_at,b.id LIMIT 100 FOR UPDATE OF b SKIP LOCKED)"""
                )
                await conn.execute(
                    """UPDATE wl_marketing_recipients SET status=CASE WHEN status='sending' THEN 'uncertain' ELSE 'queued' END
                    WHERE (project_id,broadcast_id,tg_id) IN (SELECT project_id,broadcast_id,tg_id FROM wl_marketing_recipients
                    WHERE status IN ('leased','sending') AND lease_until<=$1 ORDER BY lease_until LIMIT 100 FOR UPDATE SKIP LOCKED)""",
                    now,
                )
                row = await conn.fetchrow(
                    """SELECT r.project_id,r.broadcast_id,r.tg_id,b.text,p.bot_id,p.encrypted_token
                    FROM wl_marketing_recipients r JOIN wl_marketing_broadcasts b ON b.project_id=r.project_id AND b.id=r.broadcast_id
                    JOIN wl_projects p ON p.id=r.project_id JOIN wl_marketing_preferences pref ON pref.project_id=r.project_id AND pref.tg_id=r.tg_id
                    WHERE r.status='queued' AND r.next_attempt_at<=$1 AND b.status='queued' AND p.status='active'
                    AND p.bot_id IS NOT NULL AND pref.opted_in AND NOT pref.blocked
                    ORDER BY b.queued_at,b.id,r.tg_id LIMIT 1 FOR UPDATE OF r SKIP LOCKED""",
                    now,
                )
                if not row:
                    return None
                token = uuid4()
                await conn.execute(
                    """UPDATE wl_marketing_recipients SET status='leased',lease_token=$4,lease_until=$5,attempts=attempts+1
                    WHERE project_id=$1 AND broadcast_id=$2 AND tg_id=$3""",
                    row["project_id"],
                    row["broadcast_id"],
                    row["tg_id"],
                    token,
                    now + timedelta(minutes=2),
                )
                return {**_plain(row), "lease_token": str(token)}

    async def begin_marketing_send(self, row, *, now=None):
        if not self.delivery_enabled:
            return False
        pool = await self._pool()
        async with pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """UPDATE wl_marketing_recipients r SET status='sending'
                WHERE r.project_id=$1 AND r.broadcast_id=$2 AND r.tg_id=$3 AND r.lease_token=$4 AND r.status='leased' AND r.lease_until>$5
                AND EXISTS(SELECT 1 FROM wl_marketing_preferences p WHERE p.project_id=r.project_id AND p.tg_id=r.tg_id AND p.opted_in AND NOT p.blocked)
                AND EXISTS(SELECT 1 FROM wl_marketing_broadcasts b WHERE b.project_id=r.project_id AND b.id=r.broadcast_id AND b.status='queued')
                AND EXISTS(SELECT 1 FROM wl_projects p WHERE p.id=r.project_id AND p.status='active' AND p.bot_id=$6)
                RETURNING r.tg_id""",
                    _uuid(row["project_id"]),
                    _uuid(row["broadcast_id"]),
                    row["tg_id"],
                    _uuid(row["lease_token"]),
                    _now(now),
                    row["bot_id"],
                )
            )

    async def finish_marketing(self, row, result, *, now=None):
        from services.partner_notifications import NotificationResult

        if (
            not self.delivery_enabled
            or not isinstance(result, NotificationResult)
            or result.state not in {"sent", "retry", "blocked", "uncertain"}
        ):
            raise InvalidProject("Некорректный результат доставки.")
        if type(result.retry_after) is not int or not 1 <= result.retry_after <= 86400:
            raise InvalidProject("Некорректная задержка.")
        if result.state == "sent":
            _positive_int(result.message_id, "message_id")
        state = {
            "sent": "delivered",
            "retry": "queued",
            "blocked": "blocked",
            "uncertain": "uncertain",
        }[result.state]
        accepted = (
            ["sending", "uncertain"]
            if result.state == "sent"
            else (
                ["leased", "sending"]
                if result.state in {"retry", "blocked"}
                else ["sending"]
            )
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """UPDATE wl_marketing_recipients SET status=$5,next_attempt_at=$6,telegram_message_id=$7
                WHERE project_id=$1 AND broadcast_id=$2 AND tg_id=$3 AND lease_token=$4 AND status=ANY($8::text[]) RETURNING tg_id""",
                    _uuid(row["project_id"]),
                    _uuid(row["broadcast_id"]),
                    row["tg_id"],
                    _uuid(row["lease_token"]),
                    state,
                    _now(now) + timedelta(seconds=result.retry_after),
                    result.message_id if state == "delivered" else None,
                    accepted,
                )
            )


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidProject("Нужен часовой пояс.")
    return value.astimezone(timezone.utc)


class PartnerMarketingDeliveryService:
    """Explicit opt-in-only one-pass worker; never instantiated/scheduled on import.

    Unknown POST outcome is durable 'uncertain', not automatically resent.
    Cancellation after begin intent deliberately leaves sending for lease cleanup.
    """

    def __init__(self, store, sender=None, *, vault=None, enabled=False, clock=None):
        if type(enabled) is not bool:
            raise ValueError("Нужен явный флаг доставки.")
        self.store, self.sender, self.vault, self.enabled = (
            store,
            sender,
            vault,
            enabled,
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def ready(self):
        return bool(
            self.enabled and self.store.delivery_enabled and self.sender and self.vault
        )

    async def recover_once(self, *, limit=20):
        from services.partner_notifications import NotificationResult

        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidProject("Размер обработки: 1–100.")
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
        for _ in range(limit):
            row = await self.store.claim_marketing(now=self.clock())
            if row is None:
                break
            result["checked"] += 1
            try:
                token = self.vault.decrypt(
                    row["project_id"], row["bot_id"], row["encrypted_token"]
                )
                await self.sender.verify(token, row["bot_id"])
            except Exception:
                await self.store.finish_marketing(
                    row, NotificationResult("retry", retry_after=300), now=self.clock()
                )
                result["errors"] += 1
                continue
            if not await self.store.begin_marketing_send(row, now=self.clock()):
                continue
            try:
                outcome = await self.sender.send(
                    token,
                    bot_id=row["bot_id"],
                    chat_id=row["tg_id"],
                    text=row["text"] + "\n\nОтключить новости: /news_off",
                )
            except Exception:
                outcome = NotificationResult("uncertain")
            if await self.store.finish_marketing(row, outcome, now=self.clock()):
                result[outcome.state] += 1
        return result


class PartnerSupportDeliveryService:
    """Factual resend worker with no granting authority and no URL in admin API.

    A customer need not opt into marketing to receive the specifically requested
    existing subscription. Explicit operator enablement is nevertheless required.
    The capability URL is generated only for its verified tenant/customer/sub.
    """

    def __init__(
        self,
        store,
        sender=None,
        *,
        vault=None,
        subscription_pages=None,
        enabled=False,
        clock=None,
    ):
        if type(enabled) is not bool:
            raise ValueError("Нужен явный флаг доставки.")
        self.store, self.sender, self.vault, self.pages, self.enabled = (
            store,
            sender,
            vault,
            subscription_pages,
            enabled,
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def ready(self):
        return bool(
            self.enabled
            and self.store.support_delivery_enabled
            and self.sender
            and self.vault
            and self.pages
        )

    async def recover_once(self, *, limit=20):
        from services.partner_notifications import NotificationResult

        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidProject("Размер обработки: 1–100.")
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
        for _ in range(limit):
            row = await self.store.claim_support(now=self.clock())
            if row is None:
                break
            result["checked"] += 1
            try:
                token = self.vault.decrypt(
                    row["project_id"], row["bot_id"], row["encrypted_token"]
                )
                await self.sender.verify(token, row["bot_id"])
                link = _url(
                    await self.pages.link_for_customer(
                        row["project_id"], row["tg_id"], row["subscription_id"]
                    ),
                    "subscription_url",
                )
                if not link:
                    raise InvalidProject("Личная ссылка пока недоступна.")
            except Exception:
                await self.store.finish_support(
                    row, NotificationResult("retry", retry_after=300), now=self.clock()
                )
                result["errors"] += 1
                continue
            if not await self.store.begin_support_send(row, now=self.clock()):
                continue
            try:
                outcome = await self.sender.send(
                    token,
                    bot_id=row["bot_id"],
                    chat_id=row["tg_id"],
                    text="Ваша подписка: "
                    + link
                    + "\n\nНе передавайте личную ссылку другим людям.",
                )
            except Exception:
                outcome = NotificationResult("uncertain")
            if await self.store.finish_support(row, outcome, now=self.clock()):
                result[outcome.state] += 1
        return result
