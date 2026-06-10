import logging
import asyncio
import hashlib
import html
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qsl
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime
from config import (
    ADMIN_ID,
    BOT_TOKEN,
    BYPASS_TRAFFIC_PACKAGES,
    REGULAR_TARIFFS,
    BYPASS_TARIFFS,
    TARIFFS,
    WEBHOOK_HOST,
    WEBHOOK_PORT,
    GB_BYTES,
)
import database as db
from services.cryptobot import create_cryptobot_invoice
from services.payment_processing import process_paid_payment
from services.remnawave import remnawave_get_subscription_url, remnawave_get_user_info, remnawave_set_subscription_expiry
from services.yookassa import create_yookassa_payment


logger = logging.getLogger(__name__)

app = FastAPI(title="SPN VPN Bot Webhooks")
STATIC_DIR = Path(__file__).parent / "static" / "miniapp"
ADMIN_STATIC_DIR = Path(__file__).parent / "static" / "admin"

if STATIC_DIR.exists():
    app.mount("/app/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="miniapp_assets")
if ADMIN_STATIC_DIR.exists():
    app.mount("/admin/assets", StaticFiles(directory=ADMIN_STATIC_DIR / "assets"), name="admin_assets")

# Глобальная переменная для хранения экземпляра бота
_bot = None


def set_bot(bot):
    """Установить экземпляр бота для отправки уведомлений"""
    global _bot
    _bot = bot
    logger.info(f"✅ Bot instance set for webhook processing: {bot.token[:20]}...")
    if _bot is None:
        logger.error("⚠️ Bot instance is None! Webhooks will not work!")


def _validate_webapp_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing initData hash")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram initData")

    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Missing Telegram user")

    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Invalid Telegram user")

    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Missing Telegram user id")

    return user


async def _miniapp_user(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    init_data = ""
    if auth_header.startswith("tma "):
        init_data = auth_header[4:]
    else:
        init_data = request.headers.get("X-Telegram-Init-Data", "")

    user = _validate_webapp_init_data(init_data)
    username = user.get("username") or user.get("first_name") or f"user_{user['id']}"
    await db.create_user(int(user["id"]), username)
    return user


async def _admin_user(request: Request) -> dict:
    user = await _miniapp_user(request)
    if int(user["id"]) != ADMIN_ID:
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "items"):
        return {k: _jsonable(v) for k, v in dict(value).items()}
    return value


def _format_dt(value):
    return value.isoformat() if value else None


def _format_gb(bytes_value: int | None) -> float:
    return round((bytes_value or 0) / GB_BYTES, 1)


def _serialize_tariffs(tariffs: dict) -> list[dict]:
    return [
        {
            "code": code,
            "title": tariff["title"],
            "days": tariff["days"],
            "price": tariff["price"],
            "kind": tariff["kind"],
            "base_gb": tariff.get("base_gb"),
        }
        for code, tariff in tariffs.items()
    ]


async def _serialize_subscription(subscription) -> dict:
    sub_url = None
    used_bytes = subscription.get("last_known_used_traffic_bytes") or 0
    if subscription.get("remnawave_uuid"):
        try:
            sub_url = await remnawave_get_subscription_url(None, subscription["remnawave_uuid"])
            if subscription.get("plan_kind") == "bypass":
                user_info = await remnawave_get_user_info(None, subscription["remnawave_uuid"])
                used_bytes = (user_info.get("userTraffic") or {}).get("usedTrafficBytes") or used_bytes if user_info else used_bytes
        except Exception as e:
            logger.warning("MiniApp failed to fetch subscription data for %s: %s", subscription.get("id"), e)

    return {
        "id": subscription["id"],
        "plan_kind": subscription.get("plan_kind") or "regular",
        "type_index": subscription.get("type_index") or subscription.get("slot_number"),
        "status": "active" if subscription.get("subscription_until") and subscription["subscription_until"] > datetime.utcnow() else "expired",
        "subscription_until": _format_dt(subscription.get("subscription_until")),
        "remnawave_uuid": str(subscription.get("remnawave_uuid")) if subscription.get("remnawave_uuid") else None,
        "subscription_url": sub_url,
        "traffic": {
            "enabled": subscription.get("plan_kind") == "bypass",
            "used_gb": _format_gb(used_bytes),
            "limit_gb": _format_gb(subscription.get("current_period_limit_bytes") or subscription.get("base_traffic_bytes")),
            "reset_at": _format_dt(subscription.get("traffic_reset_at")),
        },
    }


@app.get("/app")
async def miniapp_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="MiniApp is not built")
    return FileResponse(index_path)


@app.get("/app/")
async def miniapp_index_slash():
    return await miniapp_index()


@app.get("/admin")
async def admin_index():
    index_path = ADMIN_STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Admin panel is not built")
    return FileResponse(index_path)


@app.get("/admin/")
async def admin_index_slash():
    return await admin_index()


@app.get("/app/open-happ")
async def miniapp_open_happ(url: str):
    if not (url.startswith("https://") or url.startswith("http://")):
        raise HTTPException(status_code=400, detail="Invalid subscription URL")

    happ_url = f"happ://add/{url}"
    happ_url_attr = html.escape(happ_url, quote=True)
    happ_url_json = json.dumps(happ_url)
    url_json = json.dumps(url)
    return HTMLResponse(f"""
<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Открываем Happ</title>
    <style>
      * {{ box-sizing: border-box; }}
      html, body {{ margin: 0; min-height: 100%; background: #07090d; color: #fff7e6; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
      body {{ display: grid; place-items: center; padding: 22px; }}
      .card {{ width: min(460px, 100%); padding: 22px; border: 1px solid rgba(90,184,255,.24); border-radius: 26px; background: linear-gradient(135deg, rgba(25,55,99,.58), rgba(18,76,67,.34)), #0b1118; box-shadow: 0 18px 45px rgba(0,0,0,.32); }}
      h1, p {{ display: none; }}
      a, button {{ display: block; width: 100%; border: 0; border-radius: 17px; padding: 14px 16px; color: #041120; background: linear-gradient(135deg, #8bd0ff, #5ab8ff); font: inherit; font-weight: 900; text-align: center; text-decoration: none; }}
      button {{ margin-top: 10px; color: #fff7e6; background: rgba(255,255,255,.1); box-shadow: inset 0 0 0 1px rgba(255,255,255,.16); }}
      #openHapp, #copyKey {{ display: none; }}
      #backToApp {{ margin-top: 0; color: #041120; background: linear-gradient(135deg, #f0cf7a, #c7892d); }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Открываем Happ</h1>
      <p>Возвращаем в личный кабинет автоматически.</p>
      <a id="openHapp" href="{happ_url_attr}">Добавить ключ в Happ</a>
      <button id="copyKey" type="button">Скопировать ключ</button>
      <button id="backToApp" type="button">Вернуться в личный кабинет</button>
    </div>
    <script>
      const happUrl = {happ_url_json};
      const subUrl = {url_json};
      document.getElementById('copyKey').onclick = () => navigator.clipboard?.writeText(subUrl).catch(() => {{}});
      document.getElementById('backToApp').onclick = () => {{
        window.close();
        setTimeout(() => {{ window.location.href = '/app'; }}, 160);
      }};
      setTimeout(() => {{ window.location.href = happUrl; }}, 250);
      setTimeout(() => {{
        window.close();
        setTimeout(() => {{ window.location.href = '/app'; }}, 180);
      }}, 2200);
    </script>
  </body>
</html>
""")


@app.get("/miniapp/health")
async def miniapp_health():
    return JSONResponse({"ok": True, "app": "spn-miniapp"})


@app.get("/miniapp/api/me")
async def miniapp_me(request: Request):
    user = await _miniapp_user(request)
    db_user = await db.get_user(int(user["id"]))
    return JSONResponse({
        "tg_id": int(user["id"]),
        "username": db_user.get("username") if db_user else user.get("username"),
        "first_name": user.get("first_name"),
        "photo_url": user.get("photo_url"),
    })


@app.get("/miniapp/api/tariffs")
async def miniapp_tariffs(request: Request):
    await _miniapp_user(request)
    return JSONResponse({
        "regular": _serialize_tariffs(REGULAR_TARIFFS),
        "bypass": _serialize_tariffs(BYPASS_TARIFFS),
        "traffic_packages": [
            {"code": code, "gb": package["gb"], "price": package["price"]}
            for code, package in BYPASS_TRAFFIC_PACKAGES.items()
        ],
    })


@app.get("/miniapp/api/subscriptions")
async def miniapp_subscriptions(request: Request):
    user = await _miniapp_user(request)
    subscriptions = await db.get_visible_subscriptions(int(user["id"]))
    serialized = [await _serialize_subscription(subscription) for subscription in subscriptions]
    return JSONResponse({"subscriptions": serialized})


@app.get("/miniapp/api/referral")
async def miniapp_referral(request: Request):
    user = await _miniapp_user(request)
    tg_id = int(user["id"])
    stats = await db.get_referral_stats(tg_id)
    bot_username = (await _bot.get_me()).username if _bot else "WaySPN_robot"
    return JSONResponse({
        "link": f"https://t.me/{bot_username}?start=ref_{tg_id}",
        "active_referrals": stats["active_referrals"],
        "total_earned": float(stats["total_earned"]),
        "total_withdrawn": float(stats["total_withdrawn"]),
        "current_balance": float(stats["current_balance"]),
        "earnings_by_tariff": [
            {
                "tariff_code": row["tariff_code"],
                "purchase_count": row["purchase_count"],
                "total_share": float(row["total_share"] or 0),
            }
            for row in stats["earnings_by_tariff"]
        ],
    })


@app.post("/miniapp/api/payments/subscription")
async def miniapp_create_subscription_payment(request: Request):
    user = await _miniapp_user(request)
    if not _bot:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    body = await request.json()
    tg_id = int(user["id"])
    tariff_code = body.get("tariff_code")
    provider = body.get("provider")
    payment_target = body.get("payment_target", "new")
    subscription_id = body.get("subscription_id")
    discount_code = (body.get("discount_code") or None)

    if tariff_code not in TARIFFS or provider not in {"cryptobot", "yookassa"}:
        raise HTTPException(status_code=400, detail="Invalid tariff or provider")

    tariff = TARIFFS[tariff_code]
    target_slot_number = None
    if payment_target == "renew":
        if not subscription_id:
            raise HTTPException(status_code=400, detail="subscription_id is required")
        subscription = await db.get_subscription_by_id(int(subscription_id), tg_id)
        if not subscription or subscription.get("plan_kind") != tariff.get("kind"):
            raise HTTPException(status_code=400, detail="Invalid subscription")
        target_slot_number = subscription.get("type_index")
    else:
        target_slot_number = await db.get_next_type_index(tg_id, tariff["kind"])
        if target_slot_number is None:
            raise HTTPException(status_code=400, detail="Subscription limit reached")

    original_amount = tariff["price"]
    discount = await db.get_applicable_discount(tg_id, tariff_code, "subscription", original_amount, discount_code=discount_code, plan_kind=tariff.get("kind"))
    amount = discount["final_amount"] if discount else original_amount
    if provider == "cryptobot":
        invoice = await create_cryptobot_invoice(_bot, amount, tariff_code, tg_id)
        invoice_id = str(invoice["invoice_id"]) if invoice else None
        pay_url = invoice.get("bot_invoice_url") if invoice else None
    else:
        payment = await create_yookassa_payment(_bot, amount, tariff_code, tg_id)
        invoice_id = payment.get("id") if payment else None
        pay_url = (payment.get("confirmation") or {}).get("confirmation_url") if payment else None

    if not invoice_id or not pay_url:
        raise HTTPException(status_code=502, detail="Payment provider error")

    await db.create_payment(
        tg_id,
        tariff_code,
        amount,
        provider,
        invoice_id,
        subscription_id=int(subscription_id) if subscription_id else None,
        payment_target=payment_target,
        target_slot_number=target_slot_number,
        discount_id=discount["campaign"]["id"] if discount else None,
        discount_code=discount["campaign"].get("code") if discount else None,
        discount_amount=discount["discount_amount"] if discount else 0,
        original_amount=original_amount,
    )
    return JSONResponse({"invoice_id": invoice_id, "pay_url": pay_url, "provider": provider, "amount": amount, "original_amount": original_amount, "discount": _jsonable(discount) if discount else None})


@app.post("/miniapp/api/payments/traffic")
async def miniapp_create_traffic_payment(request: Request):
    user = await _miniapp_user(request)
    if not _bot:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    body = await request.json()
    tg_id = int(user["id"])
    provider = body.get("provider")
    package_code = body.get("package_code")
    subscription_id = body.get("subscription_id")
    discount_code = (body.get("discount_code") or None)
    package = BYPASS_TRAFFIC_PACKAGES.get(package_code)
    if provider not in {"cryptobot", "yookassa"} or not package or not subscription_id:
        raise HTTPException(status_code=400, detail="Invalid traffic payment")

    subscription = await db.get_subscription_by_id(int(subscription_id), tg_id)
    if not subscription or subscription.get("plan_kind") != "bypass":
        raise HTTPException(status_code=400, detail="Invalid bypass subscription")

    original_amount = package["price"]
    discount = await db.get_applicable_discount(tg_id, package_code, "traffic_package", original_amount, discount_code=discount_code, plan_kind="traffic_package")
    amount = discount["final_amount"] if discount else original_amount
    if provider == "cryptobot":
        invoice = await create_cryptobot_invoice(_bot, amount, package_code, tg_id)
        invoice_id = str(invoice["invoice_id"]) if invoice else None
        pay_url = invoice.get("bot_invoice_url") if invoice else None
    else:
        payment = await create_yookassa_payment(_bot, amount, package_code, tg_id)
        invoice_id = payment.get("id") if payment else None
        pay_url = (payment.get("confirmation") or {}).get("confirmation_url") if payment else None

    if not invoice_id or not pay_url:
        raise HTTPException(status_code=502, detail="Payment provider error")

    await db.create_payment(
        tg_id,
        package_code,
        amount,
        provider,
        invoice_id,
        subscription_id=int(subscription_id),
        payment_target="traffic",
        payment_kind="traffic_package",
        traffic_package_code=package_code,
        discount_id=discount["campaign"]["id"] if discount else None,
        discount_code=discount["campaign"].get("code") if discount else None,
        discount_amount=discount["discount_amount"] if discount else 0,
        original_amount=original_amount,
    )
    await db.create_traffic_purchase(int(subscription_id), package_code, package["gb"] * GB_BYTES, amount, provider, invoice_id)
    return JSONResponse({"invoice_id": invoice_id, "pay_url": pay_url, "provider": provider, "amount": amount, "original_amount": original_amount, "discount": _jsonable(discount) if discount else None})


@app.get("/miniapp/api/payments/{invoice_id}")
async def miniapp_payment_status(invoice_id: str, request: Request):
    user = await _miniapp_user(request)
    payment = await db.get_payment_by_invoice(invoice_id)
    if not payment or payment["tg_id"] != int(user["id"]):
        raise HTTPException(status_code=404, detail="Payment not found")
    return JSONResponse({"invoice_id": invoice_id, "status": payment["status"]})


@app.get("/admin/api/me")
async def admin_me(request: Request):
    user = await _admin_user(request)
    return JSONResponse({"id": user["id"], "username": user.get("username"), "first_name": user.get("first_name")})


@app.get("/admin/api/dashboard")
async def admin_dashboard(request: Request):
    await _admin_user(request)
    return JSONResponse(_jsonable(await db.get_admin_dashboard_stats()))


@app.get("/admin/api/users")
async def admin_users(request: Request, q: str | None = None):
    await _admin_user(request)
    return JSONResponse({"users": _jsonable(await db.admin_search_users(q))})


@app.get("/admin/api/users/{tg_id}")
async def admin_user_detail(tg_id: int, request: Request):
    await _admin_user(request)
    detail = await db.admin_get_user_detail(tg_id)
    if not detail:
        raise HTTPException(status_code=404, detail="User not found")
    return JSONResponse(_jsonable(detail))


@app.post("/admin/api/subscriptions/{subscription_id}/days")
async def admin_change_subscription_days(subscription_id: int, request: Request):
    await _admin_user(request)
    payload = await request.json()
    days = int(payload.get("days") or 0)
    if days == 0:
        raise HTTPException(status_code=400, detail="days must not be 0")
    subscription = await db.get_subscription_by_id(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    from datetime import timedelta
    current_until = subscription.get("subscription_until") or datetime.utcnow()
    if current_until < datetime.utcnow():
        current_until = datetime.utcnow()
    new_until = current_until + timedelta(days=days)
    if subscription.get("remnawave_uuid"):
        await remnawave_set_subscription_expiry(None, str(subscription["remnawave_uuid"]), new_until)
    await db.db_execute("UPDATE subscriptions SET subscription_until = $1, updated_at = now() WHERE id = $2", (new_until, subscription_id))
    await db.sync_primary_subscription_to_user(subscription["tg_id"])
    return JSONResponse({"ok": True, "subscription_until": new_until.isoformat()})


@app.post("/admin/api/subscriptions/{subscription_id}/archive")
async def admin_archive_subscription(subscription_id: int, request: Request):
    await _admin_user(request)
    ok = await db.admin_archive_subscription(subscription_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return JSONResponse({"ok": True})


@app.delete("/admin/api/subscriptions/{subscription_id}")
async def admin_delete_subscription(subscription_id: int, request: Request):
    await _admin_user(request)
    subscription = await db.get_subscription_by_id(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    await db.delete_subscription_record(subscription_id)
    return JSONResponse({"ok": True})


@app.get("/admin/api/promos")
async def admin_promos(request: Request):
    await _admin_user(request)
    return JSONResponse({"promos": _jsonable(await db.admin_list_promo_codes())})


@app.post("/admin/api/promos")
async def admin_create_promo(request: Request):
    await _admin_user(request)
    payload = await request.json()
    code = str(payload.get("code") or "").strip().upper()
    days = int(payload.get("days") or 0)
    max_uses = int(payload.get("max_uses") or 0)
    if len(code) < 3 or not code.isalnum() or days <= 0 or max_uses <= 0:
        raise HTTPException(status_code=400, detail="Invalid promo data")
    await db.create_promo_code(code, days, max_uses)
    return JSONResponse({"ok": True})


@app.post("/admin/api/promos/{code}/active")
async def admin_toggle_promo(code: str, request: Request):
    await _admin_user(request)
    payload = await request.json()
    ok = await db.admin_set_promo_active(code, bool(payload.get("active")))
    if not ok:
        raise HTTPException(status_code=404, detail="Promo not found")
    return JSONResponse({"ok": True})


@app.delete("/admin/api/promos/{code}")
async def admin_delete_promo(code: str, request: Request):
    await _admin_user(request)
    return JSONResponse({"ok": await db.admin_delete_promo_code(code)})


@app.get("/admin/api/tracking-links")
async def admin_tracking_links(request: Request):
    await _admin_user(request)
    links = await db.list_tracking_links()
    stats = []
    for link in links or []:
        stats.append(await db.get_tracking_link_stats(link["code"]))
    return JSONResponse({"links": _jsonable(stats)})


@app.post("/admin/api/tracking-links")
async def admin_create_tracking_link(request: Request):
    user = await _admin_user(request)
    payload = await request.json()
    code = str(payload.get("code") or "").strip().lower()
    if len(code) < 3:
        raise HTTPException(status_code=400, detail="Invalid code")
    return JSONResponse(_jsonable(await db.create_tracking_link(code, payload.get("title") or None, int(user["id"]))))


@app.post("/admin/api/tracking-links/{code}/active")
async def admin_toggle_tracking_link(code: str, request: Request):
    await _admin_user(request)
    payload = await request.json()
    return JSONResponse({"ok": await db.set_tracking_link_active(code.lower(), bool(payload.get("active")))})


@app.get("/admin/api/referrals")
async def admin_referrals(request: Request):
    await _admin_user(request)
    return JSONResponse({"referrals": _jsonable(await db.get_referral_overview())})


@app.get("/admin/api/notifications")
async def admin_notifications(request: Request):
    await _admin_user(request)
    return JSONResponse({"rules": _jsonable(await db.get_notification_rules()), "state": _jsonable(await db.get_notification_state_overview())})


@app.post("/admin/api/notifications/{notification_type}")
async def admin_update_notification(notification_type: str, request: Request):
    await _admin_user(request)
    row = await db.update_notification_rule(notification_type, await request.json())
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    return JSONResponse(_jsonable(row))


@app.get("/admin/api/discounts")
async def admin_discounts(request: Request):
    await _admin_user(request)
    return JSONResponse({"discounts": _jsonable(await db.list_discount_campaigns())})


@app.post("/admin/api/discounts")
async def admin_create_discount(request: Request):
    user = await _admin_user(request)
    payload = await request.json()
    if not payload.get("title") or payload.get("discount_type") not in {"percent", "fixed"}:
        raise HTTPException(status_code=400, detail="Invalid discount data")
    row = await db.create_discount_campaign(payload, int(user["id"]))
    return JSONResponse(_jsonable(row))


@app.post("/admin/api/discounts/{discount_id}/active")
async def admin_toggle_discount(discount_id: int, request: Request):
    await _admin_user(request)
    payload = await request.json()
    return JSONResponse({"ok": await db.set_discount_campaign_active(discount_id, bool(payload.get("active")))})


async def _process_paid_invoice(bot, tg_id: int, invoice_id: str, tariff_code: str) -> bool:
    """
    Обработать оплаченный счёт и активировать подписку

    Args:
        bot: Экземпляр Bot
        tg_id: ID пользователя Telegram
        invoice_id: ID счёта в CryptoBot или Yookassa
        tariff_code: Код тарифа

    Returns:
        True если успешно, False иначе
    """
    return await process_paid_payment(bot, tg_id, invoice_id, tariff_code, acquire_lock=True)


@app.post("/webhook/cryptobot")
async def webhook_cryptobot(request: Request):
    """
    Webhook endpoint для CryptoBot платежей

    CryptoBot отправляет JSON с информацией об оплате:
    {
        "update_id": 123,
        "invoice_id": "456",
        "status": "paid",
        "paid_at": "2024-01-16T12:00:00Z"
    }
    """
    logger.info("🔔 CryptoBot webhook endpoint called")

    try:
        payload = await request.json()
        logger.info(f"📦 CryptoBot webhook payload received: {payload}")

        invoice_id = payload.get("invoice_id")
        status = payload.get("status")

        if not invoice_id or not status:
            logger.warning(f"❌ Invalid CryptoBot webhook payload (missing fields): {payload}")
            return JSONResponse({"ok": False, "error": "Missing required fields"}, status_code=400)

        logger.info(f"📊 CryptoBot invoice {invoice_id} status: {status}")

        if status != "paid":
            logger.info(f"⏭️ Ignoring CryptoBot webhook with status: {status}")
            return JSONResponse({"ok": True})

        # Получаем информацию о платеже из БД
        logger.info(f"🔍 Looking up payment for invoice {invoice_id} in database")
        result = await db.db_execute(
            """
            SELECT tg_id, tariff_code
            FROM payments
            WHERE invoice_id = $1 AND status = 'pending' AND provider = 'cryptobot'
            LIMIT 1
            """,
            (invoice_id,),
            fetch_one=True
        )

        if not result:
            logger.warning(f"❌ Payment record not found for invoice {invoice_id} (may already be processed)")
            return JSONResponse({"ok": True})

        tg_id = result['tg_id']
        tariff_code = result['tariff_code']

        logger.info(f"✅ Found payment: user {tg_id}, tariff {tariff_code}")

        # Проверяем доступность бота
        if not _bot:
            logger.error("❌ CRITICAL: Bot instance not available! Webhooks cannot process payments.")
            logger.error("⚠️ This usually means set_bot() was not called during initialization")
            return JSONResponse({"ok": False, "error": "Bot not available"}, status_code=500)

        # Обрабатываем платеж асинхронно
        logger.info(f"🚀 Creating async task to process payment for user {tg_id}")
        task = asyncio.create_task(_process_paid_invoice(_bot, tg_id, invoice_id, tariff_code))

        # Добавляем callback для отслеживания ошибок в фоновой задаче
        def task_done_callback(t):
            if t.cancelled():
                logger.warning(f"⚠️ Payment processing task cancelled for invoice {invoice_id}")
            elif t.exception():
                logger.error(f"❌ Payment processing task failed for invoice {invoice_id}: {t.exception()}")
            else:
                logger.info(f"✅ Payment processing task completed for invoice {invoice_id}")

        task.add_done_callback(task_done_callback)

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"❌ CryptoBot webhook error: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/webhook/yookassa")
async def webhook_yookassa(request: Request):
    """
    Webhook endpoint для Yookassa платежей

    Yookassa отправляет JSON с информацией об оплате:
    {
        "type": "notification",
        "event": "payment.succeeded",
        "object": {
            "id": "123",
            "status": "succeeded",
            "metadata": {
                "tg_id": "456",
                "tariff_code": "1m"
            }
        }
    }
    """
    logger.info("🔔 Yookassa webhook endpoint called")

    try:
        payload = await request.json()
        event = payload.get("event")
        webhook_type = payload.get("type")
        logger.info(f"📦 Yookassa webhook payload: type={webhook_type}, event={event}")

        if event != "payment.succeeded":
            logger.info(f"⏭️ Ignoring Yookassa event (not payment.succeeded): {event}")
            return JSONResponse({"ok": True})

        obj = payload.get("object", {})
        payment_id = obj.get("id")
        metadata = obj.get("metadata", {})

        tg_id_str = metadata.get("tg_id")
        tariff_code = metadata.get("tariff_code")

        if not all([payment_id, tg_id_str, tariff_code]):
            logger.warning(f"❌ Invalid Yookassa webhook payload (missing fields): {payload}")
            return JSONResponse({"ok": False, "error": "Missing required fields"}, status_code=400)

        try:
            tg_id = int(tg_id_str)
        except (ValueError, TypeError):
            logger.warning(f"❌ Invalid tg_id format in Yookassa webhook: {tg_id_str}")
            return JSONResponse({"ok": False, "error": "Invalid tg_id"}, status_code=400)

        logger.info(f"📊 Yookassa payment {payment_id} succeeded: user {tg_id}, tariff {tariff_code}")

        # Получаем информацию о платеже из БД
        logger.info(f"🔍 Looking up payment for ID {payment_id} in database")
        result = await db.db_execute(
            """
            SELECT tg_id, tariff_code
            FROM payments
            WHERE invoice_id = $1 AND status = 'pending' AND provider = 'yookassa'
            LIMIT 1
            """,
            (payment_id,),
            fetch_one=True
        )

        if not result:
            logger.warning(f"❌ Payment record not found for payment ID {payment_id} (may already be processed)")
            return JSONResponse({"ok": True})

        logger.info(f"✅ Found payment in database: user {tg_id}, tariff {tariff_code}")

        # Проверяем доступность бота
        if not _bot:
            logger.error("❌ CRITICAL: Bot instance not available! Webhooks cannot process payments.")
            logger.error("⚠️ This usually means set_bot() was not called during initialization")
            return JSONResponse({"ok": False, "error": "Bot not available"}, status_code=500)

        # Обрабатываем платеж асинхронно
        logger.info(f"🚀 Creating async task to process payment for user {tg_id}")
        task = asyncio.create_task(_process_paid_invoice(_bot, tg_id, payment_id, tariff_code))

        # Добавляем callback для отслеживания ошибок в фоновой задаче
        def task_done_callback(t):
            if t.cancelled():
                logger.warning(f"⚠️ Payment processing task cancelled for payment {payment_id}")
            elif t.exception():
                logger.error(f"❌ Payment processing task failed for payment {payment_id}: {t.exception()}")
            else:
                logger.info(f"✅ Payment processing task completed for payment {payment_id}")

        task.add_done_callback(task_done_callback)

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"❌ Yookassa webhook error: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    bot_available = "✅ Yes" if _bot else "❌ No"
    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "bot_available": bot_available,
        "webhook_endpoints": [
            "/webhook/cryptobot - CryptoBot payment notifications",
            "/webhook/yookassa - Yookassa payment notifications"
        ]
    })


@app.on_event("startup")
async def startup_event():
    """Called when the server starts"""
    logger.info("=" * 60)
    logger.info("🚀 Webhook Server Starting")
    logger.info("=" * 60)
    logger.info(f"📍 Listening on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    logger.info("📞 Webhook endpoints:")
    logger.info("  - POST /webhook/cryptobot")
    logger.info("  - POST /webhook/yookassa")
    logger.info("  - GET /health")
    logger.info(f"🤖 Bot instance available: {'✅ Yes' if _bot else '❌ No (will be set after connection)'}")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """Called when the server shuts down"""
    logger.info("=" * 60)
    logger.info("🛑 Webhook Server Shutting Down")
    logger.info("=" * 60)


async def run_webhook_server():
    """
    Запустить FastAPI сервер для webhook'ов

    Используется uvicorn для асинхронного запуска
    """
    import uvicorn

    logger.info("=" * 60)
    logger.info(f"🚀 Starting webhook server on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    logger.info("=" * 60)

    config = uvicorn.Config(
        app,
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        log_level="info",
        access_log=True
    )

    server = uvicorn.Server(config)
    await server.serve()
