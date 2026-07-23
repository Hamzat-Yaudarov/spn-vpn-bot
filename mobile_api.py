"""HTTP API Android-клиента Way VPN."""

from __future__ import annotations

import base64
import html
import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import database as db
from config import (
    ANDROID_APK_SHA256,
    ANDROID_APK_URL,
    ANDROID_LATEST_VERSION_CODE,
    ANDROID_LATEST_VERSION_NAME,
    ANDROID_MIN_SUPPORTED_VERSION_CODE,
    ANDROID_PACKAGE_ID,
    ANDROID_SIGNING_CERT_SHA256,
    BOT_USERNAME,
    BYPASS_TRAFFIC_PACKAGES,
    DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT,
    GB_BYTES,
    PUBLIC_SITE_URL,
    REGULAR_TARIFFS,
    BYPASS_TARIFFS,
    TARIFFS,
)
from services.cryptobot import create_cryptobot_invoice
from services.device_addons import available_device_addon_packages, current_device_limit, effective_device_limit
from services.discounts import calculate_discounted_price
from services.mobile_auth import (
    MobileAuthError,
    authenticate_access_token,
    create_challenge,
    exchange_access_key,
    exchange_challenge,
    issue_access_key,
    revoke_session,
    rotate_refresh_token,
)
from services.payment_summary import build_payment_success_summary
from services.remnawave import (
    remnawave_delete_hwid_device,
    remnawave_fetch_subscription_profile,
    remnawave_get_hwid_devices,
    remnawave_get_subscription_url,
)
from services.yookassa import create_yookassa_payment


router = APIRouter(prefix="/mobile/api/v1", tags=["Way VPN mobile"])
public_router = APIRouter(tags=["Way VPN public"])
HWID_RE = re.compile(r"^[A-Za-z0-9=-]{10,64}$")
ALLOWED_PROFILE_SCHEMES = ("vless://", "trojan://", "ss://")
_rate_events: dict[str, deque[float]] = defaultdict(deque)
RELEASE_DIR = Path(__file__).resolve().parent / "release"
PUBLIC_RELEASE_ARTIFACTS = {
    "WayVPN-1.1.5-universal-release.apk": "application/vnd.android.package-archive",
    "WayVPN-1.1.5-universal-release.apk.sha256": "text/plain",
    "WayVPN-1.1.5-gpl-source.zip": "application/zip",
    "WayVPN-1.1.5-gpl-source.zip.sha256": "text/plain",
    "WayVPN-1.1.4-universal-release.apk": "application/vnd.android.package-archive",
    "WayVPN-1.1.4-universal-release.apk.sha256": "text/plain",
    "WayVPN-1.1.4-gpl-source.zip": "application/zip",
    "WayVPN-1.1.4-gpl-source.zip.sha256": "text/plain",
    "WayVPN-1.1.3-universal-release.apk": "application/vnd.android.package-archive",
    "WayVPN-1.1.3-universal-release.apk.sha256": "text/plain",
    "WayVPN-1.1.3-gpl-source.zip": "application/zip",
    "WayVPN-1.1.3-gpl-source.zip.sha256": "text/plain",
    "WayVPN-1.1.2-universal-release.apk": "application/vnd.android.package-archive",
    "WayVPN-1.1.2-universal-release.apk.sha256": "text/plain",
    "WayVPN-1.1.2-gpl-source.zip": "application/zip",
    "WayVPN-1.1.2-gpl-source.zip.sha256": "text/plain",
    "WayVPN-1.1.1-universal-release.apk": "application/vnd.android.package-archive",
    "WayVPN-1.1.1-universal-release.apk.sha256": "text/plain",
    "WayVPN-1.1.1-gpl-source.zip": "application/zip",
    "WayVPN-1.1.1-gpl-source.zip.sha256": "text/plain",
    "WayVPN-1.1.0-universal-release.apk": "application/vnd.android.package-archive",
    "WayVPN-1.1.0-universal-release.apk.sha256": "text/plain",
    "WayVPN-1.1.0-gpl-source.zip": "application/zip",
    "WayVPN-1.1.0-gpl-source.zip.sha256": "text/plain",
    "WayVPN-1.0.0-universal-release.apk": "application/vnd.android.package-archive",
    "WayVPN-1.0.0-universal-release.apk.sha256": "text/plain",
    "WayVPN-1.0.0-gpl-source.zip": "application/zip",
    "WayVPN-1.0.0-gpl-source.zip.sha256": "text/plain",
    "LICENSES.md": "text/markdown",
    "open_source_licenses.html": "text/html",
    "sbom.cdx.json": "application/vnd.cyclonedx+json",
    "signing-cert-sha256.txt": "text/plain",
    "update-manifest.json": "application/json",
}


def _format_dt(value) -> str | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _auth_error(exc: MobileAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}},
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store"},
    )


def _rate_limit(request: Request, action: str, limit: int, window_seconds: int) -> None:
    host = request.client.host if request.client else "unknown"
    key = f"{action}:{host}"
    now = time.monotonic()
    bucket = _rate_events[key]
    while bucket and bucket[0] <= now - window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail={"code": "rate_limit", "message": "Слишком много запросов"})
    bucket.append(now)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object is required")
    return body


async def _mobile_session(request: Request):
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Требуется вход"})
    session = await authenticate_access_token(token)
    if not session:
        raise HTTPException(status_code=401, detail={"code": "invalid_access_token", "message": "Сессия истекла"})
    return session


def _scoped_subscription_id(session) -> int | None:
    value = session.get("scoped_subscription_id")
    return int(value) if value is not None else None


async def _owned_subscription(subscription_id: int, session):
    scoped_subscription_id = _scoped_subscription_id(session)
    if scoped_subscription_id is not None and scoped_subscription_id != int(subscription_id):
        raise HTTPException(status_code=404, detail={"code": "subscription_not_found", "message": "Подписка не найдена"})
    subscription = await db.get_subscription_by_id(subscription_id, int(session["tg_id"]))
    if (
        not subscription
        or not subscription.get("remnawave_uuid")
        or (
            scoped_subscription_id is None
            and (subscription.get("generation") != "v2" or not subscription.get("is_visible"))
        )
    ):
        raise HTTPException(status_code=404, detail={"code": "subscription_not_found", "message": "Подписка не найдена"})
    return subscription


def _serialize_device(device: dict) -> dict:
    return {
        "hwid": device.get("hwid"),
        "platform": device.get("platform"),
        "os_version": device.get("osVersion"),
        "device_model": device.get("deviceModel"),
        "user_agent": device.get("userAgent"),
        "created_at": device.get("createdAt"),
        "updated_at": device.get("updatedAt"),
    }


async def _serialize_subscription(subscription) -> dict:
    plan_kind = subscription.get("plan_kind") if subscription.get("plan_kind") in {"regular", "bypass"} else "regular"
    active_addons = await db.get_active_device_addon_count(subscription["id"])
    device_limit = effective_device_limit(plan_kind, active_addons)
    until = subscription.get("subscription_until")
    return {
        "id": subscription["id"],
        "title": "С антиглушилкой" if plan_kind == "bypass" else "Обычная",
        "plan_kind": plan_kind,
        "type_index": subscription.get("type_index") or subscription.get("slot_number"),
        "status": "active" if until and until > datetime.utcnow() else "expired",
        "subscription_until": _format_dt(until),
        "offline_allowed_until": _format_dt(until),
        "traffic": {
            "enabled": plan_kind == "bypass",
            "used_bytes": int(subscription.get("last_known_used_traffic_bytes") or 0),
            "limit_bytes": int(subscription.get("current_period_limit_bytes") or subscription.get("base_traffic_bytes") or 0),
            "reset_at": _format_dt(subscription.get("traffic_reset_at")),
        },
        "devices": {
            "limit": device_limit,
            "max_limit": DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT,
            "packages": available_device_addon_packages({**subscription, "hwid_device_limit": device_limit}),
        },
    }


def _serialize_tariffs(tariffs: dict, discounts) -> list[dict]:
    return [
        {
            "code": code,
            "title": tariff["title"],
            "days": tariff["days"],
            "kind": tariff["kind"],
            "base_gb": tariff.get("base_gb"),
            **calculate_discounted_price(
                tariff["price"], discounts, product_type="subscription", code=code, plan_kind=tariff["kind"]
            ),
        }
        for code, tariff in tariffs.items()
    ]


def _filter_profile(profile: bytes) -> bytes:
    """Оставить только VLESS, Trojan и Shadowsocks subscription-ссылки."""
    try:
        text = profile.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=502, detail={"code": "invalid_profile", "message": "Профиль имеет неизвестный формат"}) from exc

    was_base64 = False
    decoded = text
    if not any(line.strip().lower().startswith(ALLOWED_PROFILE_SCHEMES) for line in text.splitlines()):
        try:
            decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=True).decode("utf-8")
            was_base64 = True
        except (ValueError, UnicodeDecodeError):
            decoded = text
    allowed = [line.strip() for line in decoded.splitlines() if line.strip().lower().startswith(ALLOWED_PROFILE_SCHEMES)]
    if not allowed:
        raise HTTPException(status_code=502, detail={"code": "unsupported_profile", "message": "В профиле нет поддерживаемых узлов"})
    filtered = ("\n".join(allowed) + "\n").encode("utf-8")
    return base64.b64encode(filtered) if was_base64 else filtered


@router.post("/auth/challenges")
async def auth_challenges(request: Request):
    _rate_limit(request, "challenge", 5, 15 * 60)
    body = await _json_body(request)
    try:
        challenge = await create_challenge(body.get("code_challenge", ""), body.get("device_name"))
    except MobileAuthError as exc:
        return _auth_error(exc)
    return JSONResponse(
        {
            "challenge_id": challenge["id"],
            "telegram_url": f"https://t.me/{BOT_USERNAME}?start=app_{challenge['start_token']}",
            "expires_at": _format_dt(challenge["expires_at"]),
            "poll_interval_seconds": 3,
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/auth/exchange")
async def auth_exchange(request: Request):
    body = await _json_body(request)
    challenge_id = str(body.get("challenge_id") or "")[:64]
    # Клиент опрашивает один и тот же challenge до пяти минут. Ограничение
    # привязано к challenge, а не блокирует всех пользователей одного NAT.
    _rate_limit(request, f"exchange:{challenge_id}", 110, 5 * 60)
    try:
        tokens = await exchange_challenge(challenge_id, body.get("code_verifier", ""))
    except MobileAuthError as exc:
        return _auth_error(exc)
    return JSONResponse(tokens, headers={"Cache-Control": "no-store"})


@router.post("/auth/key-exchange")
async def auth_key_exchange(request: Request):
    _rate_limit(request, "key-exchange", 10, 15 * 60)
    body = await _json_body(request)
    try:
        tokens = await exchange_access_key(body.get("access_key", ""), body.get("device_name"))
    except MobileAuthError as exc:
        return _auth_error(exc)
    return JSONResponse(tokens, headers={"Cache-Control": "no-store"})


@router.post("/auth/refresh")
async def auth_refresh(request: Request):
    _rate_limit(request, "refresh", 10, 60)
    body = await _json_body(request)
    try:
        tokens = await rotate_refresh_token(body.get("refresh_token", ""))
    except MobileAuthError as exc:
        return _auth_error(exc)
    return JSONResponse(tokens, headers={"Cache-Control": "no-store"})


@router.post("/auth/logout")
async def auth_logout(session=Depends(_mobile_session)):
    await revoke_session(session["id"])
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.post("/auth/access-key")
async def auth_access_key(request: Request, session=Depends(_mobile_session)):
    _rate_limit(request, f"access-key:{int(session['tg_id'])}", 5, 60 * 60)
    if _scoped_subscription_id(session) is not None:
        raise HTTPException(
            status_code=403,
            detail={"code": "subscription_session", "message": "Эта сессия ограничена одной подпиской"},
        )
    access_key = await issue_access_key(int(session["tg_id"]))
    return JSONResponse(
        {"access_key": access_key},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.get("/me")
async def mobile_me(session=Depends(_mobile_session)):
    scoped_subscription_id = _scoped_subscription_id(session)
    user = await db.get_user(int(session["tg_id"])) if scoped_subscription_id is None else None
    return JSONResponse(
        {
            "tg_id": int(session["tg_id"]) if scoped_subscription_id is None else 0,
            "username": user.get("username") if user else None,
            "auth_scope": "subscription" if scoped_subscription_id is not None else "account",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/catalog")
async def mobile_catalog(session=Depends(_mobile_session)):
    discounts = await db.get_active_discounts()
    return JSONResponse({
        "regular": _serialize_tariffs(REGULAR_TARIFFS, discounts),
        "bypass": _serialize_tariffs(BYPASS_TARIFFS, discounts),
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
    })


@router.get("/subscriptions")
async def mobile_subscriptions(session=Depends(_mobile_session)):
    scoped_subscription_id = _scoped_subscription_id(session)
    if scoped_subscription_id is None:
        subscriptions = await db.get_visible_subscriptions(int(session["tg_id"]))
    else:
        scoped = await db.get_subscription_by_id(scoped_subscription_id, int(session["tg_id"]))
        subscriptions = [scoped] if scoped and scoped.get("remnawave_uuid") else []
    return JSONResponse({"subscriptions": [await _serialize_subscription(item) for item in subscriptions]})


@router.post("/subscriptions/{subscription_id}/profile")
async def mobile_subscription_profile(subscription_id: int, request: Request, session=Depends(_mobile_session)):
    body = await _json_body(request)
    hwid = str(body.get("hwid") or "")
    if not HWID_RE.fullmatch(hwid):
        raise HTTPException(status_code=400, detail={"code": "invalid_hwid", "message": "Некорректный HWID"})
    subscription = await _owned_subscription(subscription_id, session)
    if not subscription.get("subscription_until") or subscription["subscription_until"] <= datetime.utcnow():
        raise HTTPException(status_code=403, detail={"code": "subscription_expired", "message": "Срок подписки истёк"})

    subscription_url = await remnawave_get_subscription_url(None, subscription["remnawave_uuid"])
    if not subscription_url:
        raise HTTPException(status_code=502, detail={"code": "profile_unavailable", "message": "Профиль временно недоступен"})
    upstream = await remnawave_fetch_subscription_profile(subscription_url, {
        "x-hwid": hwid,
        "x-device-os": str(body.get("device_os") or "Android")[:64],
        "x-ver-os": str(body.get("os_version") or "")[:64],
        "x-device-model": str(body.get("device_model") or "")[:120],
        "user-agent": str(body.get("user_agent") or request.headers.get("User-Agent") or "WayVPN/Android")[:200],
    })
    hwid_headers = upstream["headers"]
    if upstream["status"] != 200:
        if "x-hwid-max-devices-reached" in hwid_headers:
            return JSONResponse(
                {
                    "error": {
                        "code": "hwid_limit_reached",
                        "message": "Достигнут лимит устройств",
                        "active": hwid_headers.get("x-hwid-active"),
                        "limit": hwid_headers.get("x-hwid-max-devices-reached"),
                    }
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        code = "hwid_not_supported" if "x-hwid-not-supported" in hwid_headers else "profile_unavailable"
        status = 400 if code == "hwid_not_supported" else 502
        return JSONResponse({"error": {"code": code, "message": "Профиль не выдан сервером"}}, status_code=status)

    filtered_profile = _filter_profile(upstream["body"])
    return Response(
        content=filtered_profile,
        media_type=upstream["content_type"].split(";", 1)[0],
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Way-Subscription-Expires-At": _format_dt(subscription["subscription_until"]) or "",
        },
    )


@router.get("/subscriptions/{subscription_id}/devices")
async def mobile_subscription_devices(subscription_id: int, session=Depends(_mobile_session)):
    subscription = await _owned_subscription(subscription_id, session)
    devices = await remnawave_get_hwid_devices(None, subscription["remnawave_uuid"])
    if devices is None:
        raise HTTPException(status_code=502, detail="Could not fetch devices")
    return JSONResponse({"devices": [_serialize_device(device) for device in devices]})


@router.delete("/subscriptions/{subscription_id}/devices/{hwid}")
async def mobile_delete_subscription_device(subscription_id: int, hwid: str, session=Depends(_mobile_session)):
    if not HWID_RE.fullmatch(hwid):
        raise HTTPException(status_code=400, detail={"code": "invalid_hwid", "message": "Некорректный HWID"})
    subscription = await _owned_subscription(subscription_id, session)
    if not await remnawave_delete_hwid_device(None, subscription["remnawave_uuid"], hwid):
        raise HTTPException(status_code=502, detail="Could not delete device")
    return JSONResponse({"ok": True})


async def _provider_invoice(provider: str, amount: float, tariff_code: str, tg_id: int) -> tuple[str | None, str | None]:
    return_url = f"{PUBLIC_SITE_URL}/mobile/payment-return"
    if provider == "cryptobot":
        invoice = await create_cryptobot_invoice(None, amount, tariff_code, tg_id, return_url=return_url)
        return (str(invoice["invoice_id"]), invoice.get("bot_invoice_url") or invoice.get("mini_app_invoice_url")) if invoice else (None, None)
    payment = await create_yookassa_payment(None, amount, tariff_code, tg_id, return_url=return_url)
    return (payment.get("id"), (payment.get("confirmation") or {}).get("confirmation_url")) if payment else (None, None)


@router.post("/payments/subscription")
async def mobile_create_subscription_payment(request: Request, session=Depends(_mobile_session)):
    _rate_limit(request, "payment", 10, 15 * 60)
    body = await _json_body(request)
    tg_id = int(session["tg_id"])
    code, provider = body.get("tariff_code"), body.get("provider")
    target, subscription_id = body.get("payment_target", "new"), body.get("subscription_id")
    if code not in TARIFFS or provider not in {"cryptobot", "yookassa"} or target not in {"new", "renew"}:
        raise HTTPException(status_code=400, detail="Invalid tariff or provider")
    if _scoped_subscription_id(session) is not None and target != "renew":
        raise HTTPException(
            status_code=403,
            detail={"code": "subscription_session", "message": "По этой ссылке можно продлить только текущую подписку"},
        )
    tariff = TARIFFS[code]
    if target == "renew":
        if not subscription_id:
            raise HTTPException(status_code=400, detail="subscription_id is required")
        subscription = await _owned_subscription(int(subscription_id), session)
        if not subscription.get("is_renewable") or subscription.get("plan_kind") != tariff["kind"]:
            raise HTTPException(status_code=400, detail="Invalid subscription")
        target_index = subscription.get("type_index")
    else:
        target_index = await db.get_next_type_index(tg_id, tariff["kind"])
        if target_index is None:
            raise HTTPException(status_code=409, detail={"code": "subscription_limit", "message": "Достигнут лимит подписок"})
    discounts = await db.get_active_discounts()
    amount = calculate_discounted_price(
        tariff["price"], discounts, product_type="subscription", code=code, plan_kind=tariff["kind"]
    )["price"]
    invoice_id, pay_url = await _provider_invoice(provider, amount, code, tg_id)
    if not invoice_id or not pay_url:
        raise HTTPException(status_code=502, detail="Payment provider error")
    await db.create_payment(
        tg_id, code, amount, provider, invoice_id,
        subscription_id=int(subscription_id) if subscription_id else None,
        payment_target=target,
        target_slot_number=target_index,
    )
    return JSONResponse({"invoice_id": invoice_id, "pay_url": pay_url, "provider": provider, "amount": amount})


@router.post("/payments/traffic")
async def mobile_create_traffic_payment(request: Request, session=Depends(_mobile_session)):
    _rate_limit(request, "payment", 10, 15 * 60)
    body = await _json_body(request)
    tg_id = int(session["tg_id"])
    provider, package_code, subscription_id = body.get("provider"), body.get("package_code"), body.get("subscription_id")
    package = BYPASS_TRAFFIC_PACKAGES.get(package_code)
    if provider not in {"cryptobot", "yookassa"} or not package or not subscription_id:
        raise HTTPException(status_code=400, detail="Invalid traffic payment")
    subscription = await _owned_subscription(int(subscription_id), session)
    if subscription.get("plan_kind") != "bypass" or not subscription.get("is_renewable"):
        raise HTTPException(status_code=400, detail="Invalid bypass subscription")
    discounts = await db.get_active_discounts()
    amount = calculate_discounted_price(
        package["price"], discounts, product_type="traffic", code=package_code, plan_kind="bypass"
    )["price"]
    invoice_id, pay_url = await _provider_invoice(provider, amount, package_code, tg_id)
    if not invoice_id or not pay_url:
        raise HTTPException(status_code=502, detail="Payment provider error")
    await db.create_payment(
        tg_id, package_code, amount, provider, invoice_id,
        subscription_id=int(subscription_id), payment_target="traffic",
        payment_kind="traffic_package", traffic_package_code=package_code,
    )
    await db.create_traffic_purchase(int(subscription_id), package_code, package["gb"] * GB_BYTES, amount, provider, invoice_id)
    return JSONResponse({"invoice_id": invoice_id, "pay_url": pay_url, "provider": provider, "amount": amount})


@router.post("/payments/devices")
async def mobile_create_device_payment(request: Request, session=Depends(_mobile_session)):
    _rate_limit(request, "payment", 10, 15 * 60)
    body = await _json_body(request)
    tg_id = int(session["tg_id"])
    provider, subscription_id = body.get("provider"), body.get("subscription_id")
    try:
        device_count = int(body.get("device_count") or 0)
    except (TypeError, ValueError):
        device_count = 0
    if provider not in {"cryptobot", "yookassa"} or not subscription_id or device_count <= 0:
        raise HTTPException(status_code=400, detail="Invalid device payment")
    subscription = await _owned_subscription(int(subscription_id), session)
    if not subscription.get("is_renewable") or not subscription.get("subscription_until") or subscription["subscription_until"] <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="Invalid subscription")
    active_addons = await db.get_active_device_addon_count(int(subscription_id))
    limit = effective_device_limit(subscription.get("plan_kind"), active_addons)
    package = next((item for item in available_device_addon_packages({**subscription, "hwid_device_limit": limit}) if item["count"] == device_count), None)
    if not package:
        raise HTTPException(status_code=409, detail={"code": "device_limit", "message": "Достигнут лимит устройств"})
    amount, code = package["price"], f"devices_{device_count}"
    invoice_id, pay_url = await _provider_invoice(provider, amount, code, tg_id)
    if not invoice_id or not pay_url:
        raise HTTPException(status_code=502, detail="Payment provider error")
    await db.create_payment(
        tg_id, code, amount, provider, invoice_id,
        subscription_id=int(subscription_id), payment_target="devices",
        target_slot_number=device_count, payment_kind="device_addon",
    )
    await db.create_device_addon_purchase(
        int(subscription_id), device_count, amount, provider, invoice_id, subscription["subscription_until"]
    )
    return JSONResponse({"invoice_id": invoice_id, "pay_url": pay_url, "provider": provider, "amount": amount})


@router.get("/payments/{invoice_id}")
async def mobile_payment_status(invoice_id: str, session=Depends(_mobile_session)):
    payment = await db.get_payment_by_invoice(invoice_id)
    scoped_subscription_id = _scoped_subscription_id(session)
    if (
        not payment
        or int(payment["tg_id"]) != int(session["tg_id"])
        or (
            scoped_subscription_id is not None
            and int(payment.get("subscription_id") or 0) != scoped_subscription_id
        )
    ):
        raise HTTPException(status_code=404, detail="Payment not found")
    summary = await build_payment_success_summary(payment) if payment["status"] == "paid" else None
    return JSONResponse({"invoice_id": invoice_id, "status": payment["status"], "summary": summary})


@public_router.get("/.well-known/assetlinks.json")
async def android_asset_links():
    fingerprint = ANDROID_SIGNING_CERT_SHA256.strip().upper()
    if fingerprint and ":" not in fingerprint and len(fingerprint) == 64:
        fingerprint = ":".join(fingerprint[index:index + 2] for index in range(0, 64, 2))
    statements = [] if not fingerprint else [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": ANDROID_PACKAGE_ID,
            "sha256_cert_fingerprints": [fingerprint],
        },
    }]
    return JSONResponse(statements, headers={"Cache-Control": "public, max-age=3600"})


@public_router.get("/downloads/{artifact_name}")
async def android_release_artifact(artifact_name: str):
    media_type = PUBLIC_RELEASE_ARTIFACTS.get(artifact_name)
    if not media_type:
        raise HTTPException(status_code=404, detail="Release artifact not found")
    artifact = RELEASE_DIR / artifact_name
    if not artifact.is_file():
        raise HTTPException(status_code=404, detail="Release artifact not found")
    return FileResponse(
        artifact,
        media_type=media_type,
        filename=artifact_name,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@public_router.get("/mobile/updates/manifest.json")
async def android_update_manifest():
    return JSONResponse({
        "versionCode": ANDROID_LATEST_VERSION_CODE,
        "versionName": ANDROID_LATEST_VERSION_NAME,
        "minSupportedVersionCode": ANDROID_MIN_SUPPORTED_VERSION_CODE,
        "apkUrl": ANDROID_APK_URL,
        "sha256": ANDROID_APK_SHA256.lower(),
        "signingCertSha256": ANDROID_SIGNING_CERT_SHA256.replace(":", "").lower(),
        "releaseNotes": [
            "Интерфейс больше не перекрывается статус-баром и системными кнопками Android",
            "Нижняя навигация адаптирована к увеличенному системному шрифту",
            "Пустой профиль автоматически загружается повторно при запуске",
            "Ошибка загрузки сохраняется и показывается прямо в списке серверов",
        ],
    }, headers={"Cache-Control": "no-store"})


@public_router.get("/mobile/payment-return")
async def mobile_payment_return():
    package = html.escape(ANDROID_PACKAGE_ID, quote=True)
    return HTMLResponse(f"""<!doctype html>
<html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Way VPN — проверка оплаты</title><style>body{{font-family:sans-serif;background:#08131f;color:#fff;display:grid;place-items:center;min-height:100vh;margin:0}}main{{max-width:420px;padding:28px}}a{{display:block;padding:15px;border-radius:14px;background:#55d6be;color:#07131d;text-align:center;text-decoration:none;font-weight:700}}</style></head>
<body><main><h1>Возвращаемся в Way VPN</h1><p>Приложение само запросит статус счёта у сервера. Параметры этой страницы не используются как подтверждение оплаты.</p>
<a href=\"intent://wayspn.ru/mobile/payment-return#Intent;scheme=https;package={package};end\">Открыть Way VPN</a></main></body></html>""",
        headers={"Cache-Control": "no-store"},
    )


@public_router.get("/mobile/auth-return")
async def mobile_auth_return():
    package = html.escape(ANDROID_PACKAGE_ID, quote=True)
    return HTMLResponse(f"""<!doctype html>
<html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Way VPN — вход подтверждён</title><style>body{{font-family:sans-serif;background:#0b0d12;color:#fff;display:grid;place-items:center;min-height:100vh;margin:0}}main{{max-width:420px;padding:28px}}a{{display:block;margin-top:12px;padding:15px;border-radius:18px;background:#39e6a5;color:#07110c;text-align:center;text-decoration:none;font-weight:700}}</style></head>
<body><main><h1>Вход подтверждён</h1><p>Откройте Way VPN — приложение безопасно завершит обмен одноразового запроса.</p>
<a href=\"wayvpn://auth-return\">Открыть Way VPN</a>
<a href=\"intent://auth-return#Intent;scheme=wayvpn;package={package};end\">Открыть через Android</a>
<script>setTimeout(function(){{window.location.href='wayvpn://auth-return'}},150);</script>
</main></body></html>""",
        headers={"Cache-Control": "no-store"},
    )
