"""Traffic-only extension for PartnerCommerceStore; no import-time I/O."""

from datetime import timedelta
from uuid import UUID, uuid4

from services.partner_traffic import (
    BASE_BYTES,
    MAX_CYCLE_LAG,
    MAX_OBSERVATION_AGE,
    PartnerTrafficUsage,
    TrafficStateError,
    bytes_value,
    next_traffic_cycle,
    utc,
)


def helpers():
    # Deferred to avoid a module cycle; importing either module remains inert.
    from services.partner_commerce_store import (
        _integer,
        _now,
        _public,
        _uuid,
        CommerceConflict,
        CommerceNotFound,
        SalesUnavailable,
    )

    return (
        _integer,
        _now,
        _public,
        _uuid,
        CommerceConflict,
        CommerceNotFound,
        SalesUnavailable,
    )


class PartnerTrafficStoreMixin:
    def _require_traffic(self):
        if not self.traffic_cycles_enabled:
            raise helpers()[-1]("Месячный учёт трафика пока не включён.")

    async def traffic_health(self):
        """Internal monitoring only: counts, never identities or credentials."""
        self._require_traffic()
        pool = await self._pool()
        async with pool.acquire() as conn:
            holds = await conn.fetch(
                """SELECT traffic_cycle_hold AS reason,count(*)::bigint AS count
                FROM wl_subscriptions WHERE traffic_cycle_hold IS NOT NULL GROUP BY traffic_cycle_hold"""
            )
            pending = await conn.fetchval(
                "SELECT count(*) FROM wl_traffic_credits WHERE state='prepared'"
            )
        return {"holds": [dict(row) for row in holds], "prepared_cycles": pending}

    async def _traffic_order_recorded(
        self, conn, project_id, order, previous_sub, now, *, issue, deadline
    ):
        if not self.traffic_cycles_enabled or issue:
            return
        if order["purpose"] == "traffic":
            await conn.execute(
                """UPDATE wl_subscriptions SET traffic_paid_credit_bytes=
                traffic_paid_credit_bytes+$3 WHERE project_id=$1 AND id=$2
                AND traffic_cycle_baseline_bytes IS NOT NULL""",
                project_id,
                previous_sub["id"],
                order["addon_quantity"] * 1024**3,
            )
        elif order["purpose"] == "subscription":
            start = max(now, previous_sub["desired_expires_at"] or now)
            initial = previous_sub["desired_expires_at"] is None
            if initial:
                if (
                    previous_sub["traffic_limit_bytes"] != BASE_BYTES
                    or previous_sub["remote_user_id"] is not None
                ):
                    raise TrafficStateError(
                        "Новая подписка требует проверки исходной квоты."
                    )
                await conn.execute(
                    """UPDATE wl_subscriptions SET traffic_cycle_baseline_bytes=0,
                    traffic_cycle_started_at=$3,traffic_last_used_bytes=0,traffic_paid_credit_bytes=0,
                    traffic_cycle_hold=NULL WHERE project_id=$1 AND id=$2""",
                    project_id,
                    previous_sub["id"],
                    now,
                )
            job_id = await conn.fetchval(
                "SELECT job_id FROM wl_activation_jobs WHERE project_id=$1 AND order_id=$2",
                project_id,
                order["id"],
            )
            await conn.execute(
                """INSERT INTO wl_traffic_credits(project_id,order_id,subscription_id,
                available_at,expires_at,state,activation_job_id,observed_usage_bytes,remaining_paid_bytes,new_limit_bytes,prepared_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                project_id,
                order["id"],
                previous_sub["id"],
                start,
                deadline,
                "prepared" if initial else "waiting",
                job_id if initial else None,
                0 if initial else None,
                0 if initial else None,
                BASE_BYTES if initial else None,
                now if initial else None,
            )

    async def _traffic_job_completed(self, conn, project_id, job_id):
        if self.traffic_cycles_enabled:
            await conn.execute(
                "UPDATE wl_traffic_credits SET state='applied' WHERE project_id=$1 AND activation_job_id=$2 AND state='prepared'",
                project_id,
                job_id,
            )

    async def _traffic_activation_floor(self, conn, row):
        if self.traffic_cycles_enabled:
            sub = await conn.fetchrow(
                "SELECT traffic_limit_bytes,traffic_last_used_bytes FROM wl_subscriptions WHERE project_id=$1 AND id=$2",
                UUID(str(row["project_id"])),
                UUID(str(row["subscription_id"])),
            )
            # Quota caps are monotonic and already financially authorised.
            # A later committed credit must not be undone by an older job.
            row["traffic_limit_bytes"] = max(
                row["traffic_limit_bytes"], sub["traffic_limit_bytes"]
            )
            row["usage_floor_bytes"] = sub["traffic_last_used_bytes"]
        return row

    async def list_due_traffic_cycles(self, *, now=None, limit=50):
        self._require_traffic()
        integer, clock, public, *_ = helpers()
        now, limit = clock(now), integer(limit, "limit", 100)
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.project_id,c.subscription_id FROM wl_traffic_credits c
                JOIN wl_projects p ON p.id=c.project_id JOIN wl_subscriptions s
                    ON s.project_id=c.project_id AND s.id=c.subscription_id
                WHERE c.state='waiting' AND c.available_at<=$1 AND c.next_attempt_at<=$1 AND p.status='active'
                    AND s.expires_at>$1 AND s.remote_user_id IS NOT NULL AND s.traffic_cycle_hold IS NULL
                    AND s.traffic_cycle_baseline_bytes IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM wl_activation_jobs a WHERE a.project_id=c.project_id
                        AND a.subscription_id=c.subscription_id AND a.status<>'complete' AND a.blocked_reason IS NULL)
                GROUP BY c.project_id,c.subscription_id ORDER BY min(c.next_attempt_at),min(c.available_at) LIMIT $2""",
                now,
                limit,
            )
        return [public(row) for row in rows]

    async def get_traffic_context(self, project_id, subscription_id, *, now=None):
        self._require_traffic()
        _, clock, public, uuid, _, absent, _ = helpers()
        project_id, subscription_id, now = (
            uuid(project_id),
            uuid(subscription_id),
            clock(now),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, selling=True)
            sub = await conn.fetchrow(
                "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND id=$2",
                project_id,
                subscription_id,
            )
            if not sub:
                raise absent("Подписка не найдена.")
            if (
                sub["traffic_cycle_hold"]
                or sub["traffic_cycle_baseline_bytes"] is None
                or sub["expires_at"] is None
                or sub["expires_at"] <= now
                or not sub["remote_user_id"]
            ):
                return None
            if await conn.fetchval(
                """SELECT 1 FROM wl_activation_jobs WHERE project_id=$1 AND subscription_id=$2
                AND status<>'complete' AND blocked_reason IS NULL LIMIT 1""",
                project_id,
                subscription_id,
            ):
                return None
            credit = await conn.fetchrow(
                """SELECT * FROM wl_traffic_credits WHERE project_id=$1 AND subscription_id=$2
                AND state='waiting' AND available_at<=$3 ORDER BY available_at,order_id LIMIT 1""",
                project_id,
                subscription_id,
                now,
            )
            if not credit or credit["next_attempt_at"] > now:
                return None
        context = public(sub)
        context.update(
            credit_order_id=str(credit["order_id"]),
            credit_available_at=credit["available_at"],
            credit_expires_at=credit["expires_at"],
            subscription_id=str(subscription_id),
        )
        async with pool.acquire() as conn:
            await self._set_panel_identity(conn, context)
        return context

    async def defer_traffic_cycle(self, project_id, subscription_id, *, now=None):
        self._require_traffic()
        _, clock, _, uuid, *_ = helpers()
        pid, sid, now = uuid(project_id), uuid(subscription_id), clock(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE wl_traffic_credits SET attempts=attempts+1,
                next_attempt_at=$3::timestamptz+CASE WHEN attempts<3 THEN interval '30 seconds' ELSE interval '5 minutes' END
                WHERE project_id=$1 AND subscription_id=$2 AND state='waiting'""",
                pid,
                sid,
                now,
            )

    async def prepare_traffic_cycle(self, context, observation, *, now=None):
        """Trusted worker only: persist the full target BEFORE the remote PATCH."""
        self._require_traffic()
        _, clock, public, uuid, conflict, absent, _ = helpers()
        now = clock(now)
        if not isinstance(context, dict) or not isinstance(
            observation, PartnerTrafficUsage
        ):
            raise TrafficStateError("Нужен проверенный замер трафика.")
        pid, sid, oid = (
            uuid(context["project_id"]),
            uuid(context["subscription_id"]),
            uuid(context["credit_order_id"]),
        )
        if (
            observation.project_id != str(pid)
            or observation.subscription_id != str(sid)
            or observation.remote_user_id != context["remote_user_id"]
            or not timedelta(0)
            <= now - utc(observation.observed_at)
            <= MAX_OBSERVATION_AGE
            or bytes_value(observation.limit_bytes) != context["traffic_limit_bytes"]
        ):
            raise TrafficStateError("Замер не соответствует подписке или устарел.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._project(conn, pid, selling=True)
                sub = await conn.fetchrow(
                    "SELECT * FROM wl_subscriptions WHERE project_id=$1 AND id=$2 FOR UPDATE",
                    pid,
                    sid,
                )
                if not sub:
                    raise absent("Подписка не найдена.")
                credit = await conn.fetchrow(
                    "SELECT * FROM wl_traffic_credits WHERE project_id=$1 AND order_id=$2 AND subscription_id=$3 FOR UPDATE",
                    pid,
                    oid,
                    sid,
                )
                if not credit:
                    raise absent("Оплаченный период не найден.")
                if credit["state"] != "waiting":
                    return {
                        "job_id": str(credit["activation_job_id"]),
                        "existing": True,
                    }
                keys = (
                    "traffic_cycle_baseline_bytes",
                    "traffic_paid_credit_bytes",
                    "traffic_limit_bytes",
                    "traffic_last_used_bytes",
                    "remote_user_id",
                )
                if sub["traffic_cycle_hold"] or any(sub[k] != context[k] for k in keys):
                    raise conflict(
                        "Квота изменилась во время замера. Нужен новый замер."
                    )
                if await conn.fetchval(
                    """SELECT 1 FROM wl_activation_jobs WHERE project_id=$1 AND subscription_id=$2
                    AND status<>'complete' AND blocked_reason IS NULL LIMIT 1""",
                    pid,
                    sid,
                ):
                    raise conflict("Сначала завершите ожидающую выдачу.")
                if (
                    sub["expires_at"] is None
                    or sub["expires_at"] <= now
                    or credit["available_at"] > now
                ):
                    raise conflict("Период ещё не доступен или подписка истекла.")
                # Without usage history a long-missed boundary is ambiguous:
                # do not quietly consume paid GB or grant stacked base quotas.
                if (
                    now - credit["available_at"] > MAX_CYCLE_LAG
                    or credit["expires_at"] <= now
                ):
                    await conn.execute(
                        "UPDATE wl_subscriptions SET traffic_cycle_hold='late_observation' WHERE project_id=$1 AND id=$2",
                        pid,
                        sid,
                    )
                    return {"hold": "late_observation"}
                try:
                    target = next_traffic_cycle(
                        baseline=sub["traffic_cycle_baseline_bytes"],
                        paid_credit=sub["traffic_paid_credit_bytes"],
                        previous_limit=sub["traffic_limit_bytes"],
                        last_used=sub["traffic_last_used_bytes"],
                        used=observation.used_bytes,
                    )
                except TrafficStateError:
                    await conn.execute(
                        "UPDATE wl_subscriptions SET traffic_cycle_hold='counter_changed' WHERE project_id=$1 AND id=$2",
                        pid,
                        sid,
                    )
                    return {"hold": "counter_changed"}
                await self._expiry_job(conn, pid, sub, now)
                devices = await self._device_limit(conn, pid, sid, now)
                job_id = uuid4()
                await conn.execute(
                    """INSERT INTO wl_activation_jobs(job_id,project_id,subscription_id,
                    kind,source_id,expires_at,traffic_limit_bytes,device_limit)
                    VALUES($1,$2,$3,'traffic_cycle',$4,$5,$6,$7)""",
                    job_id,
                    pid,
                    sid,
                    oid,
                    sub["desired_expires_at"],
                    target["new_limit_bytes"],
                    devices,
                )
                await conn.execute(
                    """UPDATE wl_subscriptions SET traffic_cycle_baseline_bytes=$3,
                    traffic_cycle_started_at=$4,traffic_last_used_bytes=$3,traffic_paid_credit_bytes=$5,
                    traffic_limit_bytes=$6,device_limit=$7,updated_at=$4 WHERE project_id=$1 AND id=$2""",
                    pid,
                    sid,
                    observation.used_bytes,
                    now,
                    target["remaining_paid_bytes"],
                    target["new_limit_bytes"],
                    devices,
                )
                await conn.execute(
                    """UPDATE wl_traffic_credits SET state='prepared',activation_job_id=$3,
                    observed_usage_bytes=$4,remaining_paid_bytes=$5,new_limit_bytes=$6,prepared_at=$7
                    WHERE project_id=$1 AND order_id=$2""",
                    pid,
                    oid,
                    job_id,
                    observation.used_bytes,
                    target["remaining_paid_bytes"],
                    target["new_limit_bytes"],
                    now,
                )
                return {
                    "job_id": str(job_id),
                    "project_id": str(pid),
                    "subscription_id": str(sid),
                    **target,
                }
