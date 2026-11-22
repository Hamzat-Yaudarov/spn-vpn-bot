"""Opt-in renewal-only checkout through an existing bearer subscription link.

Only a previously issued subscription can be renewed. Host is not auth: the
existing tenant-bound MAC is verified before looking up its private customer,
then its generation is checked again under the same customer/slot row locks as
ordinary bot checkout. No Telegram ID, source URL or provider credential leaves
this module. No settings/network/startup work occurs on import/construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import inspect
import json
from uuid import UUID, uuid4

from services.partner_catalog import MIN_PRICE_KOPECKS, MAX_PRICE_KOPECKS
from services.partner_checkout import public_order
from services.partner_subscriptions import SubscriptionNotFound


class InvalidPublicCheckout(ValueError):
    pass


class PublicCheckoutConflict(RuntimeError):
    def __init__(self, message, *, code="purchase_conflict"):
        super().__init__(message)
        self.code = code


class PublicCheckoutUnavailable(RuntimeError):
    pass


def _uuid(value):
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError()
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise InvalidPublicCheckout("Некорректный идентификатор покупки.") from None


def _price(value):
    if type(value) is not int or not MIN_PRICE_KOPECKS <= value <= MAX_PRICE_KOPECKS:
        raise InvalidPublicCheckout("Проверьте подтверждаемую стоимость подписки.")
    return value


def _now(value=None):
    value = value if value is not None else datetime.now(timezone.utc)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InvalidPublicCheckout("Нужен часовой пояс.")
    return value.astimezone(timezone.utc)


def _fingerprint(price, subscription):
    # Same immutable input as ordinary PartnerCommerceStore.create_order.
    return hashlib.sha256(
        json.dumps(
            {"price": price, "subscription_id": str(subscription)}, sort_keys=True
        ).encode()
    ).hexdigest()


def _held(binding):
    if (
        binding["traffic_cycle_hold"] is not None
        or binding["traffic_cycle_baseline_bytes"] is None
    ):
        return "manual_review"
    if binding["activation_pending"]:
        return "activation_pending"
    return None


class PartnerPublicCheckoutStore:
    def __init__(self, pool_getter, *, enabled=False):
        if not callable(pool_getter) or type(enabled) is not bool:
            raise TypeError("Public checkout requires explicit configuration")
        self._pool_getter, self.enabled = pool_getter, enabled

    async def _pool(self):
        if not self.enabled:
            raise PublicCheckoutUnavailable("Продление на странице пока не включено.")
        value = self._pool_getter()
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    async def _binding(conn, context, *, lock=False):
        row = await conn.fetchrow(
            """SELECT s.id AS subscription_id,s.project_id,s.tg_id,s.public_link_generation,
            s.traffic_cycle_hold,s.traffic_cycle_baseline_bytes,p.price_kopecks,p.slug,
            EXISTS(SELECT 1 FROM wl_activation_jobs j WHERE j.project_id=s.project_id AND j.subscription_id=s.id
                AND j.status<>'complete'
                AND (j.blocked_reason IS NULL OR j.blocked_reason='activation_expired')) AS activation_pending
            FROM wl_subscriptions s JOIN wl_projects p ON p.id=s.project_id
            WHERE s.project_id=$1 AND s.id=$2 AND p.slug=$3 AND p.status='active' AND p.bot_id IS NOT NULL
                AND s.remote_user_id IS NOT NULL AND s.subscription_url IS NOT NULL AND s.expires_at IS NOT NULL
                AND s.desired_expires_at IS NOT NULL"""
            + (" FOR UPDATE OF s,p" if lock else ""),
            _uuid(context["project_id"]),
            _uuid(context["subscription_id"]),
            context["slug"],
        )
        if (
            row is None
            or row["public_link_generation"] != context["public_link_generation"]
        ):
            raise SubscriptionNotFound()
        return dict(row)

    @staticmethod
    async def _order(conn, binding, order_id):
        row = await conn.fetchrow(
            """SELECT * FROM wl_orders WHERE project_id=$1 AND subscription_id=$2
            AND tg_id=$3 AND id=$4 AND purpose='subscription'""",
            binding["project_id"],
            binding["subscription_id"],
            binding["tg_id"],
            order_id,
        )
        if row is None:
            raise SubscriptionNotFound()
        return dict(row)

    async def quote(self, context):
        pool = await self._pool()
        async with pool.acquire() as conn:
            binding = await self._binding(conn, context)
            pending = await conn.fetchrow(
                """SELECT o.*,(SELECT r.request_key FROM wl_public_renewal_requests r
                    WHERE r.project_id=o.project_id AND r.subscription_id=o.subscription_id AND r.order_id=o.id
                    ORDER BY r.created_at DESC,r.request_key DESC LIMIT 1) AS public_request_key
                FROM wl_orders o WHERE project_id=$1 AND subscription_id=$2
                AND status IN ('created','paid_pending_activation')
                AND (activation_issue IS NULL OR activation_issue='activation_expired')
                ORDER BY (status='paid_pending_activation') DESC,created_at,id LIMIT 1""",
                binding["project_id"],
                binding["subscription_id"],
            )
        reason = _held(binding)
        if pending and pending["status"] == "paid_pending_activation":
            reason = "activation_pending"
        elif pending and pending.get("purpose") != "subscription":
            reason = "purchase_in_progress"
        result = {
            "price_kopecks": _price(binding["price_kopecks"]),
            "days": 30,
            "renewal_available": reason is None,
            "unavailable_reason": reason,
        }
        if pending and pending["purpose"] == "subscription":
            result["pending_purchase"] = public_order(dict(pending))
            if pending["public_request_key"] is not None:
                result["pending_purchase"]["request_key"] = str(
                    pending["public_request_key"]
                )
        return result

    async def prepare_purchase(
        self, context, request_key, expected_price_kopecks, *, now=None
    ):
        key, price, now = _uuid(request_key), _price(expected_price_kopecks), _now(now)
        fingerprint = _fingerprint(price, context["subscription_id"])
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                initial = await self._binding(conn, context)
                # Same lock order as bot purchases: customer, then subscription.
                customer = await conn.fetchval(
                    "SELECT tg_id FROM wl_customers WHERE project_id=$1 AND tg_id=$2 FOR UPDATE",
                    initial["project_id"],
                    initial["tg_id"],
                )
                if customer is None:
                    raise SubscriptionNotFound()
                binding = await self._binding(conn, context, lock=True)
                old = await conn.fetchrow(
                    """SELECT request_fingerprint,order_id FROM wl_public_renewal_requests
                    WHERE project_id=$1 AND subscription_id=$2 AND request_key=$3""",
                    binding["project_id"],
                    binding["subscription_id"],
                    key,
                )
                if old:
                    if old["request_fingerprint"] != fingerprint:
                        raise PublicCheckoutConflict(
                            "Ключ уже использован с другой стоимостью.",
                            code="request_conflict",
                        )
                    return binding, await self._order(conn, binding, old["order_id"])
                reason = _held(binding)
                if reason:
                    raise PublicCheckoutConflict(
                        "Предыдущая покупка или счётчик требует проверки. Повторно платить не нужно.",
                        code=reason,
                    )
                # A UUID already used in the bot cannot be repurposed by this page.
                direct = await conn.fetchrow(
                    "SELECT * FROM wl_orders WHERE project_id=$1 AND tg_id=$2 AND request_key=$3",
                    binding["project_id"],
                    binding["tg_id"],
                    key,
                )
                if direct:
                    if (
                        str(direct["subscription_id"])
                        != str(binding["subscription_id"])
                        or direct["purpose"] != "subscription"
                        or direct["request_fingerprint"] != fingerprint
                    ):
                        raise PublicCheckoutConflict(
                            "Ключ уже использован для другой покупки.",
                            code="request_conflict",
                        )
                    order = dict(direct)
                else:
                    pending = await conn.fetchrow(
                        """SELECT * FROM wl_orders WHERE project_id=$1 AND subscription_id=$2
                        AND status IN ('created','paid_pending_activation')
                        AND (activation_issue IS NULL OR activation_issue='activation_expired')
                        ORDER BY (status='paid_pending_activation') DESC,created_at,id LIMIT 1""",
                        binding["project_id"],
                        binding["subscription_id"],
                    )
                    if pending:
                        if pending["status"] == "paid_pending_activation":
                            raise PublicCheckoutConflict(
                                "Оплата уже сохранена. Дождитесь активации, повторно платить не нужно.",
                                code="activation_pending",
                            )
                        if pending["purpose"] != "subscription":
                            raise PublicCheckoutConflict(
                                "Сначала завершите или отмените сохранённую докупку в боте.",
                                code="purchase_in_progress",
                            )
                        if pending["price_kopecks"] != price:
                            raise PublicCheckoutConflict(
                                "Уже есть счёт с другой сохранённой стоимостью. Проверьте его перед оплатой.",
                                code="pending_price_changed",
                            )
                        order = dict(pending)
                    else:
                        if binding["price_kopecks"] != price:
                            raise PublicCheckoutConflict(
                                "Стоимость изменилась. Подтвердите новую цену.",
                                code="price_changed",
                            )
                        order = dict(
                            await conn.fetchrow(
                                """INSERT INTO wl_orders(id,project_id,tg_id,subscription_id,request_key,
                            request_fingerprint,price_kopecks,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""",
                                uuid4(),
                                binding["project_id"],
                                binding["tg_id"],
                                binding["subscription_id"],
                                key,
                                fingerprint,
                                price,
                                now,
                            )
                        )
                await conn.execute(
                    """INSERT INTO wl_public_renewal_requests(project_id,subscription_id,request_key,
                    request_fingerprint,order_id,created_at) VALUES($1,$2,$3,$4,$5,$6)""",
                    binding["project_id"],
                    binding["subscription_id"],
                    key,
                    fingerprint,
                    order["id"],
                    now,
                )
                return binding, order

    async def get_purchase(self, context, request_key):
        key = _uuid(request_key)
        pool = await self._pool()
        async with pool.acquire() as conn:
            binding = await self._binding(conn, context)
            row = await conn.fetchrow(
                """SELECT order_id FROM wl_public_renewal_requests
                WHERE project_id=$1 AND subscription_id=$2 AND request_key=$3""",
                binding["project_id"],
                binding["subscription_id"],
                key,
            )
            if row is None:
                raise SubscriptionNotFound()
            return binding, await self._order(conn, binding, row["order_id"])


class PartnerPublicCheckoutService:
    def __init__(
        self,
        subscription_service,
        checkout_service,
        request_store,
        *,
        enabled=False,
        clock=None,
    ):
        if type(enabled) is not bool:
            raise TypeError("Public checkout requires explicit enabled flag")
        self.subscriptions, self.checkout, self.store = (
            subscription_service,
            checkout_service,
            request_store,
        )
        self.enabled = enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if enabled and (
            not getattr(checkout_service.store, "invoice_intents_enabled", False)
            or not getattr(checkout_service.store, "traffic_cycles_enabled", False)
        ):
            raise ValueError(
                "Public renewals require durable invoice intent and paid-period accounting"
            )

    @property
    def links(self):
        return self.subscriptions.links

    @property
    def ready(self):
        return bool(
            self.enabled
            and self.subscriptions.enabled
            and self.store.enabled
            and self.checkout.provider is not None
        )

    async def _context(self, slug, token):
        if not self.ready:
            raise PublicCheckoutUnavailable(
                "Продление на странице пока недоступно. Откройте бот проекта."
            )
        return await self.subscriptions._resolve(slug, token)

    async def quote(self, slug, token):
        return await self.store.quote(await self._context(slug, token))

    async def create_purchase(self, slug, token, request_key, expected_price_kopecks):
        key = str(_uuid(request_key))
        context = await self._context(slug, token)
        binding, order = await self.store.prepare_purchase(
            context, key, expected_price_kopecks, now=_now(self.clock())
        )
        result = await self.checkout.create_checkout(
            str(binding["project_id"]), binding["tg_id"], str(order["id"])
        )
        return result | {"request_key": key}

    async def get_purchase(self, slug, token, request_key):
        key = str(_uuid(request_key))
        binding, order = await self.store.get_purchase(
            await self._context(slug, token), key
        )
        # A GET may verify an existing payment but never creates an invoice.
        result = await self.checkout.reconcile(
            str(binding["project_id"]), binding["tg_id"], str(order["id"])
        )
        return result | {"request_key": key}
