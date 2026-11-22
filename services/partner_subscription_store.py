"""Private gateway lookups. Never put these rows in a partner/owner API."""

import json
from uuid import UUID

from services.partner_catalog import validate_slug


def _helpers():
    from services.partner_commerce_store import (
        _uuid,
        _integer,
        CommerceNotFound,
        CommerceConflict,
    )

    return _uuid, _integer, CommerceNotFound, CommerceConflict


class PartnerSubscriptionStoreMixin:
    async def remember_public_usage(self, context, observation, *, now):
        """Persist a verified monotone observation, never a client-supplied value.

        Called on the activation lease. This is an accounting observation, not
        a quota grant/reset. A confirmed rollback holds traffic for reconciliation.
        """
        from services.partner_subscriptions import (
            normalized_quota,
            SubscriptionUnavailable,
        )

        self._require_traffic()
        # Validate identity, numeric bounds and freshness first. The actual floor
        # is checked against the LOCKED current row below, not a stale snapshot.
        normalized_quota(
            {
                **context,
                "traffic_cycle_baseline_bytes": 0,
                "traffic_last_used_bytes": 0,
            },
            observation,
            now,
        )
        project_id, subscription_id = map(
            _helpers()[0], (context["project_id"], context["subscription_id"])
        )
        rolled_back = False
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT s.* FROM wl_subscriptions s
                    JOIN wl_projects p ON p.id=s.project_id WHERE s.project_id=$1 AND s.id=$2
                    AND p.status='active' FOR UPDATE OF s""",
                    project_id,
                    subscription_id,
                )
                fields = (
                    "expires_at",
                    "traffic_limit_bytes",
                    "device_limit",
                    "remote_user_id",
                    "subscription_url",
                    "traffic_cycle_baseline_bytes",
                    "public_link_generation",
                    "traffic_cycle_hold",
                )
                if not row or any(row[key] != context[key] for key in fields):
                    raise SubscriptionUnavailable()
                pending = await conn.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM wl_activation_jobs
                    WHERE project_id=$1 AND subscription_id=$2 AND status<>'complete'
                    AND (blocked_reason IS NULL OR blocked_reason='activation_expired'))""",
                    project_id,
                    subscription_id,
                )
                if pending or row["traffic_cycle_baseline_bytes"] is None:
                    raise SubscriptionUnavailable()
                used = observation.used_bytes
                if used < max(
                    row["traffic_last_used_bytes"], row["traffic_cycle_baseline_bytes"]
                ):
                    rolled_back = True
                    await conn.execute(
                        """UPDATE wl_subscriptions SET traffic_cycle_hold='counter_changed'
                        WHERE project_id=$1 AND id=$2""",
                        project_id,
                        subscription_id,
                    )
                else:
                    await conn.execute(
                        """UPDATE wl_subscriptions SET traffic_last_used_bytes=$3
                        WHERE project_id=$1 AND id=$2""",
                        project_id,
                        subscription_id,
                        used,
                    )
        # Raise AFTER committing the hold, otherwise a transaction rollback
        # would erase the evidence and the next public request could pass again.
        if rolled_back:
            raise SubscriptionUnavailable(
                "Счётчик трафика изменился; нужна сверка без сброса ГБ."
            )

    async def resolve_public_subscription(self, slug, subscription_id):
        """Host selects a tenant, NOT an authorization. Caller must verify MAC.

        On an activation lease this reuses the same connection. No token, owner
        identity or ciphertext is selected. Pending snapshots cannot be served.
        Requires traffic.sql and subscriptions.sql, applied explicitly.
        """
        self._require_traffic()
        subscription_id = _helpers()[0](subscription_id)
        slug = validate_slug(slug)
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.id AS subscription_id,s.project_id,
                s.expires_at,s.desired_expires_at,s.traffic_limit_bytes,s.device_limit,
                s.remote_user_id,s.subscription_url,s.public_link_generation,
                s.traffic_cycle_baseline_bytes,s.traffic_last_used_bytes,s.traffic_cycle_hold,
                p.slug,p.title,p.brand,p.support_url,p.bot_username,
                EXISTS(SELECT 1 FROM wl_activation_jobs j WHERE j.project_id=s.project_id
                    AND j.subscription_id=s.id AND j.status<>'complete'
                    AND (j.blocked_reason IS NULL OR j.blocked_reason='activation_expired')) AS activation_pending
                FROM wl_subscriptions s JOIN wl_projects p ON p.id=s.project_id
                WHERE p.slug=$1 AND s.id=$2 AND p.status='active'
                    AND p.bot_id IS NOT NULL AND s.remote_user_id IS NOT NULL
                    AND s.subscription_url IS NOT NULL AND s.expires_at IS NOT NULL""",
                slug,
                subscription_id,
            )
        if not row:
            raise _helpers()[2]("Подписка не найдена.")
        result = dict(row)
        for field in ("project_id", "subscription_id"):
            result[field] = str(result[field])
        if isinstance(result["brand"], str):
            result["brand"] = json.loads(result["brand"])
        # Snapshot is server-only and is consumed solely by ownership/usage
        # verification. Renderer/header builders select explicit public fields.
        async with pool.acquire() as conn:
            await self._set_panel_identity(conn, result)
        return result

    async def get_subscription_link_context(self, project_id, tg_id, subscription_id):
        """Issue only to the verified end customer, never a partner's client list."""
        self._require_traffic()
        uuid, integer, not_found, _ = _helpers()
        project_id, subscription_id = uuid(project_id), uuid(subscription_id)
        tg_id = integer(tg_id)
        pool = await self._pool()
        async with pool.acquire() as conn:
            slug = await conn.fetchval(
                """SELECT p.slug FROM wl_projects p
                JOIN wl_subscriptions s ON s.project_id=p.id
                WHERE p.id=$1 AND s.id=$2 AND s.tg_id=$3 AND p.status='active'""",
                project_id,
                subscription_id,
                tg_id,
            )
        if slug is None:
            raise not_found("Подписка не найдена.")
        return await self.resolve_public_subscription(slug, subscription_id)

    async def rotate_subscription_link(
        self, project_id, tg_id, subscription_id, expected_generation
    ):
        """Trusted customer-only recovery port; not exposed as an owner/admin API.

        Retry of a lost response returns expected+1; a later rotation conflicts.
        Existing client subscriptions must be reimported after explicit rotation.
        """
        self._require_traffic()
        uuid, integer, not_found, conflict = _helpers()
        project_id, subscription_id = uuid(project_id), uuid(subscription_id)
        tg_id = integer(tg_id)
        if (
            type(expected_generation) is not int
            or not 0 <= expected_generation < 2147483647
        ):
            raise ValueError("Некорректная версия ссылки.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT public_link_generation FROM wl_subscriptions
                    WHERE project_id=$1 AND id=$2 AND tg_id=$3 FOR UPDATE""",
                    project_id,
                    subscription_id,
                    tg_id,
                )
                if not row:
                    raise not_found("Подписка не найдена.")
                current = row["public_link_generation"]
                if current == expected_generation:
                    await conn.execute(
                        """UPDATE wl_subscriptions SET public_link_generation=$4
                        WHERE project_id=$1 AND id=$2 AND tg_id=$3""",
                        project_id,
                        subscription_id,
                        tg_id,
                        current + 1,
                    )
                elif current != expected_generation + 1:
                    raise conflict("Ссылка уже изменена. Обновите подписку.")
        return await self.get_subscription_link_context(
            project_id, tg_id, subscription_id
        )
