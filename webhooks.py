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
from datetime import datetime, timezone
from config import (
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
from services.remnawave import (
    remnawave_delete_all_hwid_devices,
    remnawave_delete_hwid_device,
    remnawave_get_hwid_devices,
    remnawave_get_subscription_url,
    remnawave_get_user_info,
)
from services.yookassa import create_yookassa_payment
from services.subscription_sync import reconcile_subscription_expiry
from services.discounts import calculate_discounted_price
from admin_web import router as admin_router


logger = logging.getLogger(__name__)

app = FastAPI(title="SPN VPN Bot Webhooks")
STATIC_DIR = Path(__file__).parent / "static" / "miniapp"
ADMIN_STATIC_DIR = Path(__file__).parent / "static" / "admin"

if STATIC_DIR.exists():
    app.mount("/app/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="miniapp_assets")
if ADMIN_STATIC_DIR.exists():
    app.mount("/admin/assets", StaticFiles(directory=ADMIN_STATIC_DIR / "assets"), name="admin_assets")

app.include_router(admin_router)


@app.middleware("http")
async def no_cache_miniapp(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/app" or request.url.path.startswith("/app/") or request.url.path == "/admin" or request.url.path.startswith("/admin/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

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


def _format_dt(value):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_gb(bytes_value: int | None) -> float:
    return round((bytes_value or 0) / GB_BYTES, 1)


def _serialize_tariffs(tariffs: dict, discounts=None) -> list[dict]:
    result = []
    for code, tariff in tariffs.items():
        pricing = calculate_discounted_price(
            tariff["price"],
            discounts,
            product_type="subscription",
            code=code,
            plan_kind=tariff["kind"],
        )
        result.append({
            "code": code,
            "title": tariff["title"],
            "days": tariff["days"],
            **pricing,
            "kind": tariff["kind"],
            "base_gb": tariff.get("base_gb"),
        })
    return result


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


async def _get_miniapp_subscription_or_404(subscription_id: int, tg_id: int):
    subscription = await db.get_subscription_by_id(subscription_id, tg_id)
    if not subscription or subscription.get("generation") != "v2" or not subscription.get("is_visible"):
        raise HTTPException(status_code=404, detail="Subscription not found")
    if not subscription.get("remnawave_uuid"):
        raise HTTPException(status_code=400, detail="Subscription is not active yet")
    return subscription


async def _serialize_subscription(subscription) -> dict:
    sub_url = None
    user_info = None
    used_bytes = subscription.get("last_known_used_traffic_bytes") or 0
    effective_until = subscription.get("subscription_until")
    if subscription.get("remnawave_uuid"):
        try:
            sub_url = await remnawave_get_subscription_url(None, subscription["remnawave_uuid"])
        except Exception as e:
            logger.warning("MiniApp failed to fetch subscription URL for %s: %s", subscription.get("id"), e)
        try:
            user_info = await remnawave_get_user_info(None, subscription["remnawave_uuid"])
            effective_until = await reconcile_subscription_expiry(subscription, user_info)
        except Exception as e:
            logger.warning("MiniApp failed to fetch subscription expiry for %s: %s", subscription.get("id"), e)
        if subscription.get("plan_kind") == "bypass" and user_info:
            used_bytes = (user_info.get("userTraffic") or {}).get("usedTrafficBytes") or used_bytes

    return {
        "id": subscription["id"],
        "plan_kind": subscription.get("plan_kind") or "regular",
        "type_index": subscription.get("type_index") or subscription.get("slot_number"),
        "status": "active" if effective_until and effective_until > datetime.utcnow() else "expired",
        "subscription_until": _format_dt(effective_until),
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
    discounts = await db.get_active_discounts()
    return JSONResponse({
        "regular": _serialize_tariffs(REGULAR_TARIFFS, discounts),
        "bypass": _serialize_tariffs(BYPASS_TARIFFS, discounts),
        "traffic_packages": [
            {
                "code": code,
                "gb": package["gb"],
                **calculate_discounted_price(
                    package["price"],
                    discounts,
                    product_type="traffic",
                    code=code,
                    plan_kind="bypass",
                ),
            }
            for code, package in BYPASS_TRAFFIC_PACKAGES.items()
        ],
    })


@app.get("/miniapp/api/subscriptions")
async def miniapp_subscriptions(request: Request):
    user = await _miniapp_user(request)
    subscriptions = await db.get_visible_subscriptions(int(user["id"]))
    serialized = [await _serialize_subscription(subscription) for subscription in subscriptions]
    return JSONResponse({"subscriptions": serialized})


@app.get("/miniapp/api/subscriptions/{subscription_id}/devices")
async def miniapp_subscription_devices(subscription_id: int, request: Request):
    user = await _miniapp_user(request)
    subscription = await _get_miniapp_subscription_or_404(subscription_id, int(user["id"]))
    devices = await remnawave_get_hwid_devices(None, subscription["remnawave_uuid"])
    if devices is None:
        raise HTTPException(status_code=502, detail="Could not fetch devices")
    return JSONResponse({"devices": [_serialize_device(device) for device in devices]})


@app.post("/miniapp/api/subscriptions/{subscription_id}/devices/delete")
async def miniapp_delete_subscription_device(subscription_id: int, request: Request):
    user = await _miniapp_user(request)
    subscription = await _get_miniapp_subscription_or_404(subscription_id, int(user["id"]))
    body = await request.json()
    hwid = body.get("hwid")
    if not hwid:
        raise HTTPException(status_code=400, detail="hwid is required")

    deleted = await remnawave_delete_hwid_device(None, subscription["remnawave_uuid"], hwid)
    if not deleted:
        raise HTTPException(status_code=502, detail="Could not delete device")
    return JSONResponse({"ok": True})


@app.post("/miniapp/api/subscriptions/{subscription_id}/devices/delete-all")
async def miniapp_delete_all_subscription_devices(subscription_id: int, request: Request):
    user = await _miniapp_user(request)
    subscription = await _get_miniapp_subscription_or_404(subscription_id, int(user["id"]))
    deleted = await remnawave_delete_all_hwid_devices(None, subscription["remnawave_uuid"])
    if not deleted:
        raise HTTPException(status_code=502, detail="Could not delete devices")
    return JSONResponse({"ok": True})


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

    discounts = await db.get_active_discounts()
    amount = calculate_discounted_price(
        tariff["price"],
        discounts,
        product_type="subscription",
        code=tariff_code,
        plan_kind=tariff["kind"],
    )["price"]
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
    )
    return JSONResponse({"invoice_id": invoice_id, "pay_url": pay_url, "provider": provider})


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
    package = BYPASS_TRAFFIC_PACKAGES.get(package_code)
    if provider not in {"cryptobot", "yookassa"} or not package or not subscription_id:
        raise HTTPException(status_code=400, detail="Invalid traffic payment")

    subscription = await db.get_subscription_by_id(int(subscription_id), tg_id)
    if not subscription or subscription.get("plan_kind") != "bypass":
        raise HTTPException(status_code=400, detail="Invalid bypass subscription")

    discounts = await db.get_active_discounts()
    amount = calculate_discounted_price(
        package["price"],
        discounts,
        product_type="traffic",
        code=package_code,
        plan_kind="bypass",
    )["price"]
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
    )
    await db.create_traffic_purchase(int(subscription_id), package_code, package["gb"] * GB_BYTES, amount, provider, invoice_id)
    return JSONResponse({"invoice_id": invoice_id, "pay_url": pay_url, "provider": provider})


@app.get("/miniapp/api/payments/{invoice_id}")
async def miniapp_payment_status(invoice_id: str, request: Request):
    user = await _miniapp_user(request)
    payment = await db.get_payment_by_invoice(invoice_id)
    if not payment or payment["tg_id"] != int(user["id"]):
        raise HTTPException(status_code=404, detail="Payment not found")
    return JSONResponse({"invoice_id": invoice_id, "status": payment["status"]})


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
