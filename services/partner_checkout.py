"""Partner checkout saga, with explicit external ports and no startup side effects.

The host must inject a payment provider and a provisioner. No existing SPN
invoice, user, referral balance or Remnawave helper is reused implicitly.
Untrusted webhook payloads are never proof of payment: reconcile fetches the
canonical object from the provider before any entitlement or ledger write.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID


class CheckoutError(RuntimeError):
    """Safe checkout failure; raw provider/network errors must not reach users."""


class BillingUnavailable(CheckoutError):
    pass


class PaymentMismatch(CheckoutError):
    pass


class InvoiceRecoveryRequired(CheckoutError):
    pass


def https_url(value: str, *, empty: bool = False) -> str:
    if empty and value == "":
        return value
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError("Некорректная HTTPS-ссылка.")
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.port not in (None, 443)
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value
        ):
            raise ValueError()
    except ValueError:
        raise ValueError("Некорректная HTTPS-ссылка.") from None
    return value


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CheckoutError("Не удалось проверить срок заказа.")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class PartnerPayment:
    id: str
    order_id: str
    project_id: str
    tg_id: int
    amount_kopecks: int
    currency: str
    status: str
    paid: bool
    confirmation_url: str = ""
    purpose: str = "subscription"


@dataclass(frozen=True)
class ProvisionedSubscription:
    remote_user_id: str
    remote_username: str
    subscription_url: str


class PartnerPaymentProvider(Protocol):
    async def create_invoice(
        self, order: dict, *, idempotence_key: str
    ) -> PartnerPayment: ...
    async def get_payment(self, payment_id: str) -> PartnerPayment: ...


class PartnerProvisioner(Protocol):
    async def ensure_subscription(
        self, activation: dict
    ) -> ProvisionedSubscription: ...


def _validate_payment(payment, order):
    if (
        not isinstance(payment, PartnerPayment)
        or not payment.id
        or len(payment.id) > 128
        or payment.order_id != str(order["id"])
        or payment.project_id != str(order["project_id"])
        or type(payment.tg_id) is not int
        or payment.tg_id != order["tg_id"]
        or type(payment.amount_kopecks) is not int
        or payment.amount_kopecks != order["price_kopecks"]
        or payment.currency != "RUB"
        or type(payment.paid) is not bool
        or payment.purpose != order.get("purpose", "subscription")
        or payment.status
        not in {"pending", "waiting_for_capture", "succeeded", "canceled"}
    ):
        raise PaymentMismatch(
            "Данные оплаты не совпали с заказом. Повторная оплата не нужна."
        )
    if order.get("provider_payment_id") and order["provider_payment_id"] != payment.id:
        raise PaymentMismatch("Оплата не соответствует заказу.")
    try:
        https_url(payment.confirmation_url, empty=True)
    except ValueError:
        raise PaymentMismatch("Не удалось проверить платёжную ссылку.") from None


def public_order(order):
    """Customer response, never provider internals or another user's data."""
    result = {
        key: order.get(key)
        for key in (
            "id",
            "subscription_id",
            "price_kopecks",
            "status",
            "created_at",
            "paid_at",
        )
    }
    for key in ("purpose", "package_id", "addon_quantity", "addon_expires_at"):
        if key in order:
            result[key] = order[key]
    if order.get("status") == "created" and order.get("confirmation_url"):
        result["confirmation_url"] = order["confirmation_url"]
    if order.get("status") == "paid_pending_activation":
        result["message"] = (
            "Оплата сохранена, активация повторяется. Повторно платить не нужно."
        )
        if order.get("activation_issue") in {
            "addon_expired",
            "device_limit",
            "activation_expired",
        }:
            result["activation_issue"] = order["activation_issue"]
            result["message"] = (
                "Оплата сохранена. Покупка требует проверки поддержкой. Повторно платить не нужно."
            )
    return result


def validate_provisioned_subscription(remote, activation):
    """A successful transport alone is not proof of the correct VPN identity."""
    if (
        not isinstance(remote, ProvisionedSubscription)
        or remote.remote_username != activation["remote_username"]
        or not isinstance(remote.remote_user_id, str)
        or not 1 <= len(remote.remote_user_id) <= 128
        or (
            activation.get("remote_user_id") is not None
            and remote.remote_user_id != activation["remote_user_id"]
        )
    ):
        raise CheckoutError("Активация требует повторной проверки.")
    https_url(remote.subscription_url)


class PartnerCheckoutService:
    def __init__(
        self,
        store,
        provider: PartnerPaymentProvider | None = None,
        provisioner: PartnerProvisioner | None = None,
        *,
        clock=None,
    ):
        self.store = store
        self.provider = provider
        self.provisioner = provisioner
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def quote_addons(self, project_id, tg_id, subscription_id):
        """No client price or quantity is accepted as a catalogue source."""
        return await self.store.get_addon_catalog(
            project_id,
            tg_id,
            subscription_id,
            now=_utc(self.clock()),
        )

    async def prepare_addon_order(
        self,
        project_id,
        tg_id,
        request_key,
        expected_price_kopecks,
        *,
        subscription_id,
        purpose,
        package_id,
    ):
        # This saves a draft only. The user must confirm it before an invoice
        # is created, using the ordinary saved-order checkout route.
        return await self.store.create_addon_order(
            project_id,
            tg_id,
            request_key,
            expected_price_kopecks,
            subscription_id=subscription_id,
            purpose=purpose,
            package_id=package_id,
            now=_utc(self.clock()),
        )

    async def create_checkout(self, project_id, tg_id, order_id):
        order = await self.store.get_order(project_id, tg_id, order_id)
        if order["status"] != "created":
            return public_order(order)
        if order.get("provider_payment_id"):
            return public_order(order)
        if self.provider is None:
            raise BillingUnavailable("Оплата пока не подключена. Деньги не списаны.")
        preflight = getattr(self.store, "get_checkout_order", None)
        if preflight is not None:
            order = await preflight(project_id, tg_id, order_id, now=_utc(self.clock()))
        if getattr(self.store, "invoice_intents_enabled", False):
            order = await self.store.begin_invoice_creation(
                project_id, tg_id, order_id, now=_utc(self.clock())
            )
            if order["status"] != "created" or order.get("provider_payment_id"):
                return public_order(order)
        # YooKassa guarantees repeated-create idempotency for 24h only. Never
        # blindly recreate an invoice after a long outage with an old key.
        now, created = _utc(self.clock()), _utc(
            order.get("invoice_requested_at") or order["created_at"]
        )
        if now < created - timedelta(minutes=1) or now - created >= timedelta(hours=23):
            raise InvoiceRecoveryRequired(
                "Нужна проверка старого счёта. Новый счёт автоматически не создаётся."
            )
        try:
            payment = await self.provider.create_invoice(
                order, idempotence_key=str(UUID(str(order["id"])))
            )
            _validate_payment(payment, order)
            bound = await self.store.save_invoice(
                project_id, tg_id, order_id, payment.id, payment.confirmation_url
            )
        except CheckoutError:
            raise
        except Exception:
            # Retain the saved order and its key. A retry uses the same invoice
            # parameters; a timeout is not authorization for a fresh charge.
            raise CheckoutError(
                "Счёт пока не подтверждён. Повторите проверку этого заказа."
            ) from None
        if (
            payment.status == "succeeded" and payment.paid
        ) or payment.status == "canceled":
            return await self.reconcile(project_id, tg_id, order_id)
        return public_order(bound)

    async def reconcile(self, project_id, tg_id, order_id):
        # Check customer/project scope before any privileged provider request.
        order = await self.store.get_order(project_id, tg_id, order_id)
        if order["status"] in {"active", "canceled"}:
            return public_order(order)
        if order["status"] == "created":
            if not order.get("provider_payment_id"):
                return public_order(order)
            if self.provider is None:
                raise BillingUnavailable("Проверка оплаты пока недоступна.")
            try:
                payment = await self.provider.get_payment(order["provider_payment_id"])
                _validate_payment(payment, order)
            except CheckoutError:
                raise
            except Exception:
                raise CheckoutError(
                    "Не удалось проверить оплату. Повторно платить не нужно."
                ) from None
            if payment.status != "succeeded" or not payment.paid:
                result = public_order(order)
                result["payment_status"] = payment.status
                if payment.status == "canceled":
                    if payment.paid:
                        raise PaymentMismatch(
                            "Статус отменённой оплаты требует проверки."
                        )
                    canceled = await self.store.cancel_verified_invoice(
                        project_id, order_id, payment.id
                    )
                    result = public_order(canceled)
                    result["payment_status"] = "canceled"
                    result.pop("confirmation_url", None)
                    result["message"] = "Платёж отменён. Подписка не выдана."
                return result
            order = await self.store.record_verified_payment(
                project_id,
                order_id,
                payment.id,
                payment.amount_kopecks,
                payment.currency,
                now=_utc(self.clock()),
            )
        if (
            order["status"] == "paid_pending_activation"
            and self.provisioner is not None
            and not order.get("activation_issue")
        ):
            return await self._activate(project_id, tg_id, order)
        return public_order(order)

    async def _activate(self, project_id, tg_id, order):
        try:
            # Dedicated PostgreSQL session lock, not an in-process asyncio lock:
            # two workers cannot apply old/new deadlines remotely out of order.
            async with self.store.acquire_activation_lock(
                project_id, order["subscription_id"]
            ) as lease:
                active_store = lease or self.store
                current = await active_store.get_order(project_id, tg_id, order["id"])
                if current["status"] == "active":
                    return public_order(current)
                activation = await active_store.get_activation(project_id, order["id"])
                if getattr(active_store, "traffic_cycles_enabled", False) and _utc(
                    activation["expires_at"]
                ) <= _utc(self.clock()):
                    await active_store.mark_activation_expired(
                        project_id, activation["job_id"], now=_utc(self.clock())
                    )
                    return public_order(
                        await active_store.get_order(project_id, tg_id, order["id"])
                    )
                remote = await self.provisioner.ensure_subscription(activation)
                validate_provisioned_subscription(remote, activation)
                await active_store.complete_activation(
                    project_id,
                    order["id"],
                    remote_user_id=remote.remote_user_id,
                    subscription_url=remote.subscription_url,
                )
                return public_order(
                    await active_store.get_order(project_id, tg_id, order["id"])
                )
        except (Exception, asyncio.CancelledError) as error:
            # Money/absolute entitlement remains committed. Recovery re-applies
            # exactly that snapshot, never +30 days or a new charge.
            await self.store.mark_activation_retry(
                project_id,
                order["id"],
                **(
                    {"now": _utc(self.clock())}
                    if getattr(self.store, "traffic_cycles_enabled", False)
                    else {}
                ),
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            return public_order(
                await self.store.get_order(project_id, tg_id, order["id"])
            )


async def recover_partner_orders_once(store, checkout, *, limit=50):
    """One explicit recovery pass; nothing schedules itself on app startup.

    The host may later schedule this on a reviewed isolated worker. Pending
    snapshots are processed oldest-first by storage and retried through the
    exact same canonical verification/activation path as a manual check.
    """
    rows = await store.list_recoverable_orders(
        limit=limit,
        **(
            {"now": _utc(checkout.clock())}
            if getattr(store, "traffic_cycles_enabled", False)
            else {}
        ),
    )
    counts = {"checked": 0, "active": 0, "pending": 0, "canceled": 0, "errors": 0}
    for row in rows:
        counts["checked"] += 1
        try:
            if getattr(store, "invoice_intents_enabled", False):
                saved = await store.get_order(
                    row["project_id"], row["tg_id"], row["id"]
                )
                if (
                    saved["status"] == "created"
                    and not saved.get("provider_payment_id")
                    and saved.get("invoice_requested_at") is not None
                ):
                    await checkout.create_checkout(
                        row["project_id"], row["tg_id"], row["id"]
                    )
            result = await checkout.reconcile(
                row["project_id"], row["tg_id"], row["id"]
            )
            key = (
                result["status"]
                if result["status"] in {"active", "canceled"}
                else "pending"
            )
            counts[key] += 1
        except Exception:
            counts["errors"] += 1
    return counts
