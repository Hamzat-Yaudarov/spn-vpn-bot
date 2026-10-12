"""A subscription-link capability may renew that subscription, never log in.

The link is delivered in Happ metadata. Its fragment is consumed by the
browser and the credential is sent in Authorization, not in request URLs.
No VPN configuration, personal data or account-level actions are exposed.
"""
import time
import uuid
from collections import OrderedDict, deque
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import database as db
from config import PUBLIC_SITE_URL, SUPPORT_URL, TARIFFS, TELEGRAPH_AGREEMENT_URL
from services.discounts import calculate_discounted_price
from services.payment_reconciliation import reconcile_payment
from services.remnawave import (
    extract_public_subscription_short_uuid,
    remnawave_resolve_user_uuid_by_short_uuid,
)
from services.subscription_sync import refresh_subscription_expiry
from services.yookassa import create_yookassa_payment


router = APIRouter()
RENEWAL_PAGE = Path(__file__).parent / "static" / "site" / "renew.html"
_attempts = OrderedDict()
_MAX_RATE_KEYS = 4096
PRIVATE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
}


class RenewalPaymentBody(BaseModel):
    tariff_code: str
    request_id: uuid.UUID
    expected_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)

    class Config:
        extra = "forbid"


def _rate_limit(key: str, limit: int, seconds: int) -> None:
    now = time.monotonic()
    bucket = _attempts.setdefault(key, deque())
    _attempts.move_to_end(key)
    while len(_attempts) > _MAX_RATE_KEYS:
        _attempts.popitem(last=False)
    while bucket and now - bucket[0] >= seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, "Слишком много запросов. Попробуйте чуть позже",
                            headers={"Retry-After": str(seconds), **PRIVATE_HEADERS})
    bucket.append(now)


def _is_renewable(subscription) -> bool:
    return bool(
        subscription
        and subscription.get("generation") == "v2"
        and subscription.get("is_visible")
        and subscription.get("is_renewable")
        and not subscription.get("legacy_readonly")
        and subscription.get("plan_kind") in {"regular", "bypass"}
        and subscription.get("remnawave_uuid")
    )


async def require_renewal_subscription(request: Request, response: Response):
    response.headers.update(PRIVATE_HEADERS)
    client = request.client.host if request.client else "unknown"
    _rate_limit(f"read:{client}", 600, 60)
    # No cookies are used, and cross-origin browser calls are not allowed.
    origin = request.headers.get("origin")
    site = urlsplit(PUBLIC_SITE_URL)
    expected_origin = f"{site.scheme}://{site.netloc}"
    if origin and origin != expected_origin:
        raise HTTPException(403, "Откройте страницу продления из вашей подписки")
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization) > 2048:
        raise HTTPException(401, "Откройте сайт продления из вашей подписки в Happ или INCY")
    try:
        short_uuid = extract_public_subscription_short_uuid(authorization[7:])
    except (TypeError, ValueError):
        raise HTTPException(401, "Ссылка продления недействительна") from None
    try:
        local_uuid = await remnawave_resolve_user_uuid_by_short_uuid(short_uuid)
    except RuntimeError:
        raise HTTPException(503, "Проверка подписки временно недоступна. Попробуйте позже") from None
    subscription = await db.get_subscription_by_uuid(local_uuid) if local_uuid else None
    if not _is_renewable(subscription):
        raise HTTPException(401, "Ссылка продления недействительна")
    return dict(subscription)


def _iso_date(value):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _same_payment(payment, subscription) -> bool:
    return bool(
        payment
        and int(payment["tg_id"]) == int(subscription["tg_id"])
        and payment.get("subscription_id") == subscription["id"]
        and payment.get("payment_target") == "renew"
        and payment.get("payment_kind") == "subscription"
        and payment.get("provider") == "yookassa"
        and (TARIFFS.get(payment.get("tariff_code")) or {}).get("kind") == subscription["plan_kind"]
    )


@router.get("/renew", include_in_schema=False)
async def renewal_page():
    headers = {
        **PRIVATE_HEADERS,
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        ),
    }
    return FileResponse(RENEWAL_PAGE, headers=headers)


@router.get("/site/api/renewal/details")
async def renewal_details(subscription=Depends(require_renewal_subscription)):
    until = await refresh_subscription_expiry(subscription)
    until = _iso_date(until)
    discounts = await db.get_active_discounts()
    kind = subscription["plan_kind"]
    pending = await db.get_pending_renewal_for_subscription(subscription["id"], int(subscription["tg_id"]))
    return {
        "subscription": {
            "name": ("С антиглушилкой" if kind == "bypass" else "Обычная")
                    + f" #{subscription.get('type_index') or subscription.get('slot_number') or '—'}",
            "subscription_until": until,
        },
        "tariffs": [
            {"code": code, "days": tariff["days"], **calculate_discounted_price(
                tariff["price"], discounts, product_type="subscription", code=code, plan_kind=kind,
            )}
            for code, tariff in TARIFFS.items() if tariff["kind"] == kind
        ],
        "pending_invoice_id": pending["invoice_id"] if pending else None,
        "support_url": SUPPORT_URL,
        "agreement_url": TELEGRAPH_AGREEMENT_URL,
    }


@router.post("/site/api/renewal/payment")
async def renewal_payment(body: RenewalPaymentBody, subscription=Depends(require_renewal_subscription)):
    tariff = TARIFFS.get(body.tariff_code)
    if not tariff or tariff["kind"] != subscription["plan_kind"]:
        raise HTTPException(400, "Выберите срок вашей подписки")
    _rate_limit(f"checkout:{subscription['id']}", 10, 600)
    tg_id = int(subscription["tg_id"])
    if not await db.acquire_user_lock(tg_id):
        raise HTTPException(409, "Подписка обновляется. Попробуйте через несколько секунд")
    try:
        # Deletion/renewal may have happened since credential validation.
        current = await db.get_subscription_by_id(subscription["id"], tg_id)
        if (not _is_renewable(current) or current["remnawave_uuid"] != subscription["remnawave_uuid"]
                or current["plan_kind"] != tariff["kind"]):
            raise HTTPException(409, "Подписка изменилась. Откройте сайт из вашей подписки ещё раз")
        amount = calculate_discounted_price(
            tariff["price"], await db.get_active_discounts(), product_type="subscription",
            code=body.tariff_code, plan_kind=current["plan_kind"],
        )["price"]
        if body.expected_price != Decimal(str(amount)):
            raise HTTPException(409, "Цена изменилась. Обновите страницу и выберите срок ещё раз")
        request_key = str(uuid.uuid5(uuid.NAMESPACE_URL,
            f"wayspn:renew:{current['id']}:{body.tariff_code}:{amount}:{body.request_id}"))
        remote = await create_yookassa_payment(
            None, amount, body.tariff_code, tg_id,
            return_url=f"{PUBLIC_SITE_URL}/renew?payment=return", idempotence_key=request_key,
        )
        invoice_id = remote.get("id") if remote else None
        pay_url = ((remote or {}).get("confirmation") or {}).get("confirmation_url")
        try:
            url = urlsplit(pay_url or "")
            remote_amount = Decimal(str(((remote or {}).get("amount") or {}).get("value")))
            valid = (invoice_id and url.scheme == "https" and url.hostname
                     and not url.username and not url.password and remote_amount == Decimal(str(amount)))
        except (ValueError, ArithmeticError):
            valid = False
        if not valid:
            raise HTTPException(502, "Не удалось создать платёж. Попробуйте позже")
        # Provider retries may return the same invoice. Do not reset a paid row.
        payment = await db.create_renewal_checkout_payment(tg_id, current, body.tariff_code, amount, invoice_id)
        if not _same_payment(payment, current) or Decimal(str(payment["amount"])) != Decimal(str(amount)):
            raise HTTPException(409, "Не удалось проверить платёж. Попробуйте позже")
        return {"invoice_id": invoice_id, "pay_url": pay_url, "status": payment["status"]}
    finally:
        await db.release_user_lock(tg_id)


@router.get("/site/api/renewal/payments/{invoice_id}")
async def renewal_payment_status(invoice_id: uuid.UUID, request: Request,
                                 subscription=Depends(require_renewal_subscription)):
    invoice_id = str(invoice_id)
    payment = await db.get_payment_by_invoice(invoice_id)
    if not _same_payment(payment, subscription):
        raise HTTPException(404, "Платёж не найден")
    if payment["status"] == "pending":
        # Provider verification + durable activation are shared with bot/webhook/polling.
        await reconcile_payment(getattr(request.app.state, "bot", None), invoice_id,
                                expected_tg_id=int(subscription["tg_id"]))
        payment = await db.get_payment_by_invoice(invoice_id)
        if not _same_payment(payment, subscription):
            raise HTTPException(404, "Платёж не найден")
    return {"invoice_id": invoice_id, "status": payment["status"]}
