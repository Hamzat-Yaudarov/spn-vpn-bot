"""Entry fee checkout, separate from subscription revenue and entitlements.

The server-selected fee is paid once per project. It never credits a partner's 70% ledger or
issues a VPN subscription. External adapters must be injected by a reviewed host;
import/construction never reads credentials, starts a worker or performs I/O.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from services.partner_entry_pricing import EntryPricingError, EntryPricingPolicy
from services.partner_checkout import (
    BillingUnavailable, CheckoutError, InvoiceRecoveryRequired, _utc, _validate_payment,
)


class EntryNotPaid(CheckoutError):
    pass


def public_entry_order(order):
    if order is None:
        return None
    result = {key: order.get(key) for key in (
        "id", "price_kopecks", "status", "created_at", "paid_at",
    )}
    result["needs_webhook_replace"] = bool(order.get("needs_webhook_replace"))
    result["creation_available"] = bool(order.get("status") == "paid_pending_launch"
                                        and "bot_id" in order and order["bot_id"] is None)
    if order.get("status") == "created" and order.get("confirmation_url"):
        result["confirmation_url"] = order["confirmation_url"]
    return result


class PartnerEntryCheckoutService:
    def __init__(self, store, provider=None, launcher=None, *, clock=None, entry_pricing=None):
        self.store, self.provider, self.launcher = store, provider, launcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        pricing = entry_pricing if entry_pricing is not None else getattr(store, "entry_pricing", None)
        if pricing is not None and not isinstance(pricing, EntryPricingPolicy):
            raise TypeError("entry_pricing must be an EntryPricingPolicy")
        self.entry_pricing = pricing if pricing is not None else EntryPricingPolicy()

    def _validate_price(self, order, *, new_invoice=False):
        try:
            method = self.entry_pricing.validate_new_invoice if new_invoice else self.entry_pricing.validate_saved_order
            method(order)
        except EntryPricingError:
            raise CheckoutError("Стоимость вступления требует подтверждения; сохранённая цена не изменена.") from None

    @property
    def launch_available(self):
        return bool(self.launcher is not None and self.launcher.ready)

    @property
    def payments_available(self):
        # Do not accept a new entry fee when the installation cannot even launch
        # a bot. Already-issued invoices remain reconcilable during an outage.
        return self.provider is not None and self.launch_available

    @staticmethod
    def _payment_order(order):
        return {**order, "tg_id": order["owner_tg_id"], "purpose": "entry"}

    async def create_checkout(self, project_id, owner_id, order_id):
        order = await self.store.get_entry_order(project_id, owner_id, order_id)
        self._validate_price(order)
        if order["status"] != "created" or order.get("provider_payment_id"):
            return {"order": public_entry_order(order)}
        if not self.payments_available:
            raise BillingUnavailable("Приём вступления пока не подключён. Деньги не списаны.")
        # A project can be suspended after the owner saved an unpaid order.
        # Re-check current launch eligibility before issuing its FIRST invoice;
        # already bound invoices remain independently reconcilable.
        order = await self.store.get_checkout_order(project_id, owner_id, order_id)
        self._validate_price(order, new_invoice=True)
        now, created = _utc(self.clock()), _utc(order["created_at"])
        if now < created - timedelta(minutes=1) or now - created >= timedelta(hours=23):
            raise InvoiceRecoveryRequired("Старый счёт требует сверки. Повторный платёж не создаётся.")
        try:
            payment_order = self._payment_order(order)
            payment = await self.provider.create_invoice(payment_order, idempotence_key=str(UUID(str(order["id"]))))
            _validate_payment(payment, payment_order)
            order = await self.store.save_invoice(project_id, owner_id, order_id,
                                                   payment.id, payment.confirmation_url)
        except CheckoutError:
            raise
        except Exception:
            raise CheckoutError("Счёт пока не подтверждён. Проверьте этот же заказ позже.") from None
        if (payment.status == "succeeded" and payment.paid) or payment.status == "canceled":
            return await self.reconcile(project_id, owner_id, order_id)
        return {"order": public_entry_order(order)}

    async def reconcile(self, project_id, owner_id, order_id):
        order = await self.store.get_entry_order(project_id, owner_id, order_id)
        self._validate_price(order)
        if order["status"] in {"launched", "canceled"}:
            return {"order": public_entry_order(order)}
        if order["status"] == "created":
            if not order.get("provider_payment_id"):
                return {"order": public_entry_order(order)}
            if self.provider is None:
                raise BillingUnavailable("Проверка оплаты временно недоступна. Повторно платить не нужно.")
            try:
                payment = await self.provider.get_payment(order["provider_payment_id"])
                _validate_payment(payment, self._payment_order(order))
            except CheckoutError:
                raise
            except Exception:
                raise CheckoutError("Не удалось проверить вступление. Повторная оплата не нужна.") from None
            if payment.status == "canceled":
                if payment.paid:
                    raise CheckoutError("Отменённая оплата требует проверки.")
                order = await self.store.cancel_verified_invoice(project_id, order_id, payment.id)
            elif payment.status == "succeeded" and payment.paid:
                order = await self.store.record_verified_payment(project_id, order_id, payment.id,
                    payment.amount_kopecks, payment.currency, now=_utc(self.clock()))
            else:
                return {"order": public_entry_order(order)}
        if order["status"] == "paid_pending_launch":
            return await self.launch(project_id, owner_id, order_id)
        return {"order": public_entry_order(order)}

    async def launch(self, project_id, owner_id, order_id, *, replace_existing=False):
        if type(replace_existing) is not bool:
            raise ValueError("Подтверждение замены должно быть явным.")
        order = await self.store.get_entry_order(project_id, owner_id, order_id)
        self._validate_price(order)
        if order["status"] == "launched":
            return {"order": public_entry_order(order), "launch_state": "launched"}
        if order["status"] != "paid_pending_launch":
            raise EntryNotPaid("Сначала требуется подтверждённая оплата вступления.")
        if "bot_id" in order and order["bot_id"] is None:
            return {"order": public_entry_order(order), "launch_state": "awaiting_bot",
                    "creation_available": True}
        if not self.launch_available:
            return {"order": public_entry_order(order), "launch_state": "disabled",
                    "message": "Оплата сохранена. Запуск пока недоступен, повторно платить не нужно."}
        result = await self.launcher.launch(project_id, owner_id, order_id, replace_existing=replace_existing)
        # Launcher result is a small envelope; never serialize its private job.
        current = await self.store.get_entry_order(project_id, owner_id, order_id)
        return {"order": public_entry_order(current),
                **{key: result[key] for key in ("launch_state", "message") if key in result}}


async def recover_partner_entries_once(store, service, *, limit=50):
    """Explicit invoice/paid-launch recovery pass, not an automatic scheduler."""
    rows = await store.list_recoverable_entry_orders(limit=limit)
    counts = {"checked": 0, "launched": 0, "pending": 0, "errors": 0}
    for row in rows:
        counts["checked"] += 1
        try:
            result = await service.reconcile(row["project_id"], row["owner_tg_id"], row["id"])
            counts["launched" if result["order"]["status"] == "launched" else "pending"] += 1
        except Exception:
            counts["errors"] += 1
    return counts
