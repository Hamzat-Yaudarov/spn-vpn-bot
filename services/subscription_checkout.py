from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import database as db


class SpecialDiscountCheckoutError(ValueError):
    pass


def special_discount_fields(pricing) -> dict:
    campaign_id = pricing.get("special_discount_id")
    return {"special_discount_id": campaign_id} if campaign_id is not None else {}


def ensure_checkout_price(amount, expected) -> None:
    if expected is None:
        return
    try:
        if Decimal(str(expected)) == Decimal(str(amount)):
            return
    except InvalidOperation:
        pass
    raise SpecialDiscountCheckoutError("Цена изменилась. Обновите тарифы и выберите срок ещё раз.")


async def _pending_special_invoice(tg_id, code, target, subscription_id, provider, pricing):
    payment = await db.get_pending_special_discount_payment(tg_id)
    if not payment:
        return None

    invoice_id = str(payment["invoice_id"])
    status, pay_url = None, None
    if payment["provider"] == "cryptobot":
        from services.cryptobot import get_invoice_status

        remote = await get_invoice_status(invoice_id)
        if remote and str(remote.get("invoice_id")) == invoice_id:
            status = remote.get("status")
            if status == "active":
                pay_url = remote.get("bot_invoice_url") or remote.get("mini_app_invoice_url")
    elif payment["provider"] == "yookassa":
        from services.yookassa import get_payment_status

        remote = await get_payment_status(invoice_id)
        if remote and str(remote.get("id")) == invoice_id:
            status = remote.get("status")
            if status == "pending":
                pay_url = (remote.get("confirmation") or {}).get("confirmation_url")

    if status in {"canceled", "cancelled", "expired"}:
        await db.update_payment_status_if_pending(invoice_id, "canceled")
        return None

    url = urlsplit(pay_url or "")
    if (url.scheme == "https" and url.hostname and not url.username and not url.password
            and payment["provider"] == provider and payment["tariff_code"] == code
            and payment["payment_target"] == target and payment.get("subscription_id") == subscription_id
            and payment["special_discount_id"] == pricing["special_discount_id"]
            and float(payment["amount"]) == pricing["price"]):
        return invoice_id, pay_url

    raise SpecialDiscountCheckoutError(
        "У вас уже есть счёт со спецскидкой. Откройте его, чтобы оплатить или проверить оплату."
    )


@asynccontextmanager
async def subscription_checkout(tg_id, code, target, subscription_id, provider, pricing):
    """Resume a compatible invoice before trying to reserve the offer in storage."""
    try:
        pending = None
        if pricing.get("special_discount_id"):
            pending = await _pending_special_invoice(tg_id, code, target, subscription_id, provider, pricing)
        yield pending
    except db.SpecialDiscountUnavailable as exc:
        raise SpecialDiscountCheckoutError(str(exc)) from exc
