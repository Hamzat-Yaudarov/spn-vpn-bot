import asyncio
import hashlib
import html
import json
import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import database as db
from config import (
    BYPASS_TRAFFIC_PACKAGES,
    GB_BYTES,
    PUBLIC_SITE_URL,
    REGULAR_TARIFFS,
    BYPASS_TARIFFS,
    TARIFFS,
    TELEGRAPH_AGREEMENT_URL,
    WEB_COOKIE_SECURE,
    WEB_SESSION_DAYS,
)
from services.discounts import calculate_discounted_price
from services.payment_processing import process_paid_payment
from services.remnawave import remnawave_get_subscription_url, remnawave_get_user_info
from services.subscription_sync import reconcile_subscription_expiry
from services.web_auth import (
    create_session_token,
    hash_password,
    hash_session_token,
    normalize_login,
    verify_password,
)
from services.yookassa import create_yookassa_payment, get_payment_status


router = APIRouter()
logger = logging.getLogger(__name__)
SITE_STATIC_DIR = Path(__file__).parent / "static" / "site"
SESSION_COOKIE = "wayspn_web_session"
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
_auth_attempts: dict[str, deque[float]] = defaultdict(deque)
_dummy_password_hash = hash_password("not-a-real-user-password")


class RegisterBody(BaseModel):
    login: str
    password: str
    password_confirmation: str
    terms_accepted: bool = False
    tracking_code: str | None = None


class LoginBody(BaseModel):
    login: str
    password: str


class SubscriptionPaymentBody(BaseModel):
    tariff_code: str
    payment_target: str = "new"
    subscription_id: int | None = None


class TrafficPaymentBody(BaseModel):
    package_code: str
    subscription_id: int


class TrackVisitBody(BaseModel):
    code: str
    client_id: str


def _check_auth_rate(request: Request, action: str) -> None:
    client = request.client.host if request.client else "unknown"
    key = f"{action}:{client}"
    now = time.monotonic()
    attempts = _auth_attempts[key]
    while attempts and now - attempts[0] > 900:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуйте через 15 минут")
    attempts.append(now)


def _validate_credentials(login: str, password: str) -> str:
    normalized = normalize_login(login)
    if not LOGIN_RE.fullmatch(normalized):
        raise HTTPException(
            status_code=400,
            detail="Логин: 3–32 символа, латинские буквы, цифры, точка, дефис или подчёркивание",
        )
    if len(password) < 8 or len(password) > 128:
        raise HTTPException(status_code=400, detail="Пароль должен содержать от 8 до 128 символов")
    return normalized


def _normalize_tracking_code(value: str | None) -> str | None:
    if not value:
        return None
    code = value.strip().lower()
    return code if re.fullmatch(r"[a-z0-9_-]{3,64}", code) else None


def _tracking_client_id(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return -7_000_000_000_000_000_000 - (int(digest[:12], 16) % 1_000_000_000_000)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=WEB_SESSION_DAYS * 86_400,
        httponly=True,
        secure=WEB_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


async def _new_session(account, response: Response) -> None:
    token = create_session_token()
    expires_at = datetime.utcnow() + timedelta(days=WEB_SESSION_DAYS)
    await db.create_web_session(int(account["id"]), hash_session_token(token), expires_at)
    await db.mark_web_account_login(int(account["id"]))
    _set_session_cookie(response, token)


async def require_web_account(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(status_code=401, detail="Войдите в аккаунт")
    account = await db.get_web_account_by_session(hash_session_token(token))
    if not account:
        raise HTTPException(status_code=401, detail="Сессия завершена. Войдите снова")
    return account


def _format_dt(value) -> str | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def _serialize_subscription(subscription) -> dict:
    plan_kind = subscription.get("plan_kind") if subscription.get("plan_kind") in {"regular", "bypass"} else "regular"
    effective_until = subscription.get("subscription_until")
    subscription_url = None
    used_bytes = subscription.get("last_known_used_traffic_bytes") or 0
    if subscription.get("remnawave_uuid"):
        try:
            subscription_url = await remnawave_get_subscription_url(None, subscription["remnawave_uuid"])
            user_info = await remnawave_get_user_info(None, subscription["remnawave_uuid"])
            effective_until = await reconcile_subscription_expiry(subscription, user_info)
            if plan_kind == "bypass" and user_info:
                used_bytes = (user_info.get("userTraffic") or {}).get("usedTrafficBytes") or used_bytes
        except Exception as exc:
            logger.warning("Website subscription refresh failed for %s: %s", subscription.get("id"), exc)

    return {
        "id": subscription["id"],
        "plan_kind": plan_kind,
        "type_index": subscription.get("type_index") or subscription.get("slot_number"),
        "status": "active" if effective_until and effective_until > datetime.utcnow() else "expired",
        "subscription_until": _format_dt(effective_until),
        "subscription_url": subscription_url,
        "traffic": {
            "enabled": plan_kind == "bypass",
            "used_gb": round(used_bytes / GB_BYTES, 1),
            "limit_gb": round((subscription.get("current_period_limit_bytes") or subscription.get("base_traffic_bytes") or 0) / GB_BYTES, 1),
            "reset_at": _format_dt(subscription.get("traffic_reset_at")),
        },
    }


def _catalog(discounts) -> dict:
    def tariff_items(source):
        return [
            {
                "code": code,
                "title": tariff["title"],
                "days": tariff["days"],
                "kind": tariff["kind"],
                **calculate_discounted_price(
                    tariff["price"], discounts, product_type="subscription", code=code, plan_kind=tariff["kind"]
                ),
            }
            for code, tariff in source.items()
        ]

    return {
        "regular": tariff_items(REGULAR_TARIFFS),
        "bypass": tariff_items(BYPASS_TARIFFS),
        "traffic_packages": [
            {
                "code": code,
                "gb": package["gb"],
                **calculate_discounted_price(
                    package["price"], discounts, product_type="traffic", code=code, plan_kind="bypass"
                ),
            }
            for code, package in BYPASS_TRAFFIC_PACKAGES.items()
        ],
    }


@router.get("/")
@router.get("/account")
@router.get("/login")
@router.get("/register")
async def website_index():
    path = SITE_STATIC_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Website is not built")
    return FileResponse(path)


@router.get("/open-happ")
async def website_open_happ(url: str):
    if not (url.startswith("https://") or url.startswith("http://")):
        raise HTTPException(status_code=400, detail="Некорректная ссылка подписки")

    happ_url = f"happ://add/{quote(url, safe='')}"
    happ_url_attr = html.escape(happ_url, quote=True)
    happ_url_json = (
        json.dumps(happ_url)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return HTMLResponse(f"""
<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Открываем Happ</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; min-height: 100vh; padding: 20px; display: grid; place-items: center; background: #07111f; color: #f5f8fb; font-family: system-ui, sans-serif; }}
      main {{ width: min(420px, 100%); padding: 28px; border: 1px solid rgba(167,194,220,.16); border-radius: 22px; background: #102036; text-align: center; }}
      h1 {{ margin: 0 0 9px; font-size: 25px; }}
      p {{ margin: 0 0 22px; color: #91a3b8; line-height: 1.5; }}
      a {{ display: flex; min-height: 48px; align-items: center; justify-content: center; border-radius: 12px; color: #06271d; background: #6ee7b7; text-decoration: none; font-weight: 800; }}
      a + a {{ margin-top: 10px; color: #f5f8fb; background: rgba(255,255,255,.07); }}
    </style>
  </head>
  <body>
    <main>
      <h1>Открываем Happ</h1>
      <p>Если приложение не открылось автоматически, нажмите кнопку.</p>
      <a href="{happ_url_attr}">Открыть Happ</a>
      <a href="/account">Вернуться в кабинет</a>
    </main>
    <script>
      const happUrl = {happ_url_json};
      setTimeout(() => {{ window.location.href = happUrl; }}, 120);
      setTimeout(() => {{ window.location.href = '/account'; }}, 2600);
    </script>
  </body>
</html>
""")


@router.get("/site/api/config")
async def website_config():
    return {"agreement_url": TELEGRAPH_AGREEMENT_URL}


@router.get("/site/api/catalog")
async def website_catalog():
    return _catalog(await db.get_active_discounts())


@router.post("/site/api/track")
async def website_track_visit(body: TrackVisitBody):
    code = _normalize_tracking_code(body.code)
    client_id = body.client_id.strip()
    if not code or not re.fullmatch(r"[a-zA-Z0-9_-]{12,96}", client_id):
        raise HTTPException(status_code=400, detail="Некорректная tracking-ссылка")
    recorded = await db.record_tracking_link_click(code, _tracking_client_id(client_id), is_new_user=False)
    return {"ok": recorded}


@router.post("/site/api/auth/register", status_code=201)
async def website_register(body: RegisterBody, request: Request, response: Response):
    _check_auth_rate(request, "register")
    login = _validate_credentials(body.login, body.password)
    if body.password != body.password_confirmation:
        raise HTTPException(status_code=400, detail="Пароли не совпадают")
    if not body.terms_accepted:
        raise HTTPException(status_code=400, detail="Необходимо принять пользовательское соглашение")
    tracking_code = _normalize_tracking_code(body.tracking_code)
    if tracking_code:
        link = await db.get_tracking_link(tracking_code)
        if not link or not link.get("is_active"):
            tracking_code = None

    password_hash = await asyncio.to_thread(hash_password, body.password)
    account = await db.create_web_account(login, password_hash, tracking_code=tracking_code)
    if not account:
        raise HTTPException(status_code=409, detail="Такой логин уже занят")
    await _new_session(account, response)
    return {"id": account["id"], "login": account["login"]}


@router.post("/site/api/auth/login")
async def website_login(body: LoginBody, request: Request, response: Response):
    _check_auth_rate(request, "login")
    login = normalize_login(body.login)
    account = await db.get_web_account_by_login(login)
    encoded_password = account["password_hash"] if account else _dummy_password_hash
    password_matches = await asyncio.to_thread(verify_password, body.password, encoded_password)
    valid = bool(account and account.get("is_active") and password_matches)
    if not valid:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    await _new_session(account, response)
    return {"id": account["id"], "login": account["login"]}


@router.post("/site/api/auth/logout")
async def website_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        await db.delete_web_session(hash_session_token(token))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/site/api/me")
async def website_me(account=Depends(require_web_account)):
    return {
        "id": account["id"],
        "login": account["login"],
        "created_at": _format_dt(account["created_at"]),
    }


@router.get("/site/api/subscriptions")
async def website_subscriptions(account=Depends(require_web_account)):
    subscriptions = await db.get_visible_subscriptions(int(account["service_user_id"]))
    return {"subscriptions": [await _serialize_subscription(item) for item in subscriptions]}


@router.get("/site/api/payments")
async def website_payments(account=Depends(require_web_account)):
    rows = await db.list_web_account_payments(int(account["service_user_id"]))
    return {
        "payments": [
            {
                **dict(row),
                "amount": float(row["amount"]),
                "created_at": _format_dt(row["created_at"]),
                "updated_at": _format_dt(row["updated_at"]),
            }
            for row in rows
        ]
    }


@router.post("/site/api/payments/subscription")
async def website_subscription_payment(body: SubscriptionPaymentBody, account=Depends(require_web_account)):
    service_user_id = int(account["service_user_id"])
    tariff = TARIFFS.get(body.tariff_code)
    if not tariff or body.payment_target not in {"new", "renew"}:
        raise HTTPException(status_code=400, detail="Некорректный тариф")

    target_slot_number = None
    subscription_id = body.subscription_id
    if body.payment_target == "renew":
        if not subscription_id:
            raise HTTPException(status_code=400, detail="Выберите подписку для продления")
        subscription = await db.get_subscription_by_id(subscription_id, service_user_id)
        if not subscription or subscription.get("plan_kind") != tariff["kind"]:
            raise HTTPException(status_code=400, detail="Эту подписку нельзя продлить выбранным тарифом")
        target_slot_number = subscription.get("type_index")
    else:
        target_slot_number = await db.get_next_type_index(service_user_id, tariff["kind"])
        if target_slot_number is None:
            raise HTTPException(status_code=400, detail="Достигнут лимит подписок этого типа")

    discounts = await db.get_active_discounts()
    amount = calculate_discounted_price(
        tariff["price"], discounts, product_type="subscription", code=body.tariff_code, plan_kind=tariff["kind"]
    )["price"]
    payment = await create_yookassa_payment(
        None,
        amount,
        body.tariff_code,
        service_user_id,
        return_url=f"{PUBLIC_SITE_URL}/account?payment=return",
    )
    invoice_id = payment.get("id") if payment else None
    pay_url = (payment.get("confirmation") or {}).get("confirmation_url") if payment else None
    if not invoice_id or not pay_url:
        raise HTTPException(status_code=502, detail="Не удалось создать платёж. Попробуйте позже")

    await db.create_payment(
        service_user_id,
        body.tariff_code,
        amount,
        "yookassa",
        invoice_id,
        subscription_id=subscription_id,
        payment_target=body.payment_target,
        target_slot_number=target_slot_number,
    )
    return {"invoice_id": invoice_id, "pay_url": pay_url}


@router.post("/site/api/payments/traffic")
async def website_traffic_payment(body: TrafficPaymentBody, account=Depends(require_web_account)):
    service_user_id = int(account["service_user_id"])
    package = BYPASS_TRAFFIC_PACKAGES.get(body.package_code)
    subscription = await db.get_subscription_by_id(body.subscription_id, service_user_id)
    if not package or not subscription or subscription.get("plan_kind") != "bypass":
        raise HTTPException(status_code=400, detail="Некорректный пакет или подписка")
    if not subscription.get("subscription_until") or subscription["subscription_until"] <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="Сначала продлите подписку с антиглушилкой")

    discounts = await db.get_active_discounts()
    amount = calculate_discounted_price(
        package["price"], discounts, product_type="traffic", code=body.package_code, plan_kind="bypass"
    )["price"]
    payment = await create_yookassa_payment(
        None,
        amount,
        body.package_code,
        service_user_id,
        return_url=f"{PUBLIC_SITE_URL}/account?payment=return",
    )
    invoice_id = payment.get("id") if payment else None
    pay_url = (payment.get("confirmation") or {}).get("confirmation_url") if payment else None
    if not invoice_id or not pay_url:
        raise HTTPException(status_code=502, detail="Не удалось создать платёж. Попробуйте позже")

    await db.create_payment(
        service_user_id,
        body.package_code,
        amount,
        "yookassa",
        invoice_id,
        subscription_id=body.subscription_id,
        payment_target="traffic",
        payment_kind="traffic_package",
        traffic_package_code=body.package_code,
    )
    await db.create_traffic_purchase(
        body.subscription_id, body.package_code, package["gb"] * GB_BYTES, amount, "yookassa", invoice_id
    )
    return {"invoice_id": invoice_id, "pay_url": pay_url}


@router.get("/site/api/payments/{invoice_id}")
async def website_payment_status(invoice_id: str, account=Depends(require_web_account)):
    service_user_id = int(account["service_user_id"])
    payment = await db.get_payment_by_invoice(invoice_id)
    if not payment or int(payment["tg_id"]) != service_user_id:
        raise HTTPException(status_code=404, detail="Платёж не найден")

    if payment["status"] == "pending" and payment["provider"] == "yookassa":
        provider_payment = await get_payment_status(invoice_id)
        provider_status = (provider_payment or {}).get("status")
        provider_amount = ((provider_payment or {}).get("amount") or {}).get("value")
        if (
            provider_status == "succeeded"
            and provider_amount is not None
            and abs(float(provider_amount) - float(payment["amount"])) <= 0.009
        ):
            await process_paid_payment(None, service_user_id, invoice_id, payment["tariff_code"])
        elif provider_status == "canceled":
            await db.update_payment_status_by_invoice(invoice_id, "canceled")
        payment = await db.get_payment_by_invoice(invoice_id)

    return {"invoice_id": invoice_id, "status": payment["status"]}
