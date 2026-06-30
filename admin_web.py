import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import database as db
from config import (
    ADMIN_ID,
    BYPASS_TRAFFIC_PACKAGES,
    BOT_TOKEN,
    BOT_USERNAME,
    PUBLIC_SITE_URL,
    TARIFFS,
)
from services.remnawave import remnawave_set_subscription_expiry
from services.subscription_adjustment import SubscriptionAdjustmentError, adjust_subscription_days
from services.telegram_auth import TelegramAuthError, validate_telegram_init_data


router = APIRouter()
logger = logging.getLogger(__name__)
ADMIN_STATIC_DIR = Path(__file__).parent / "static" / "admin"
TRACKING_CODE_RE = re.compile(r"^[a-z0-9_-]{3,64}$")
PROMO_CODE_RE = re.compile(r"^[A-Z0-9]{2,32}$")


class AdjustDaysBody(BaseModel):
    days: int = Field(ge=-3650, le=3650)


class PromoBody(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    days: int = Field(ge=1, le=3650)
    max_uses: int = Field(ge=1, le=1_000_000)


class LinkBody(BaseModel):
    code: str = Field(min_length=3, max_length=64)
    title: str | None = Field(default=None, max_length=160)


class ToggleBody(BaseModel):
    active: bool


class DiscountBody(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    discount_type: str
    value: float = Field(gt=0)
    target_type: str
    target_code: str | None = Field(default=None, max_length=64)
    starts_at: datetime
    ends_at: datetime


async def require_admin(request: Request):
    auth_header = request.headers.get("Authorization", "")
    init_data = auth_header[4:] if auth_header.startswith("tma ") else ""
    try:
        user = validate_telegram_init_data(init_data, BOT_TOKEN)
    except TelegramAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if int(user["id"]) != ADMIN_ID:
        logger.warning("Admin panel access denied for Telegram user %s", user["id"])
        raise HTTPException(status_code=403, detail="Доступ разрешён только администратору")
    return int(user["id"])


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _plain(value):
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "items"):
        return {key: _plain(item) for key, item in value.items()}
    return value


@router.get("/admin")
async def admin_index():
    index_path = ADMIN_STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Admin panel is not built")
    return FileResponse(index_path)


@router.get("/admin/api/session")
async def admin_session(_: int = Depends(require_admin)):
    return {"authenticated": True, "admin_id": ADMIN_ID}


@router.get("/admin/api/dashboard")
async def admin_dashboard(_: int = Depends(require_admin)):
    return _plain(await db.admin_dashboard_stats())


@router.get("/admin/api/users")
async def admin_users(q: str = "", limit: int = 50, offset: int = 0, _: int = Depends(require_admin)):
    return _plain(await db.admin_list_users(q, min(max(limit, 1), 100), max(offset, 0)))


@router.get("/admin/api/users/{tg_id}")
async def admin_user(tg_id: int, _: int = Depends(require_admin)):
    result = await db.admin_get_user_bundle(tg_id)
    if not result:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return _plain(result)


@router.post("/admin/api/subscriptions/{subscription_id}/adjust")
async def admin_adjust_subscription(subscription_id: int, body: AdjustDaysBody, _: int = Depends(require_admin)):
    if body.days == 0:
        raise HTTPException(status_code=400, detail="Количество дней не может быть нулём")
    subscription = await db.get_subscription_by_id(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    tg_id = int(subscription["tg_id"])
    if not await db.acquire_user_lock(tg_id):
        raise HTTPException(status_code=409, detail="Пользователь сейчас занят другой операцией")
    try:
        try:
            new_until = await adjust_subscription_days(subscription, body.days)
        except SubscriptionAdjustmentError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        logger.info("Web admin adjusted subscription %s by %s days", subscription_id, body.days)
        return {"ok": True, "subscription_until": new_until, "days": body.days}
    finally:
        await db.release_user_lock(tg_id)


@router.delete("/admin/api/subscriptions/{subscription_id}")
async def admin_delete_subscription(subscription_id: int, _: int = Depends(require_admin)):
    subscription = await db.get_subscription_by_id(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    tg_id = int(subscription["tg_id"])
    if not await db.acquire_user_lock(tg_id):
        raise HTTPException(status_code=409, detail="Пользователь сейчас занят другой операцией")
    try:
        if subscription.get("remnawave_uuid"):
            expired_at = datetime.utcnow() - timedelta(seconds=1)
            updated = await remnawave_set_subscription_expiry(None, subscription["remnawave_uuid"], expired_at)
            if not updated:
                raise HTTPException(status_code=502, detail="Не удалось отключить подписку в Remnawave")
        await db.delete_subscription_record(subscription_id)
        logger.info("Web admin deleted subscription %s for user %s", subscription_id, tg_id)
        return {"ok": True}
    finally:
        await db.release_user_lock(tg_id)


@router.get("/admin/api/promos")
async def admin_promos(_: int = Depends(require_admin)):
    return {"items": _plain(await db.list_promo_codes())}


@router.post("/admin/api/promos")
async def admin_create_promo(body: PromoBody, _: int = Depends(require_admin)):
    code = body.code.strip().upper()
    if not PROMO_CODE_RE.fullmatch(code):
        raise HTTPException(status_code=400, detail="Код может содержать только латинские буквы и цифры")
    await db.create_promo_code(code, body.days, body.max_uses)
    logger.info("Web admin created promo %s", code)
    return {"ok": True, "code": code}


@router.post("/admin/api/promos/{code}/toggle")
async def admin_toggle_promo(code: str, body: ToggleBody, _: int = Depends(require_admin)):
    if not await db.set_promo_code_active(code, body.active):
        raise HTTPException(status_code=404, detail="Промокод не найден")
    return {"ok": True}


@router.get("/admin/api/links")
async def admin_links(_: int = Depends(require_admin)):
    items = _plain(await db.list_tracking_links_with_stats())
    for item in items:
        item["bot_url"] = f"https://t.me/{BOT_USERNAME}?start={item['code']}"
        item["site_url"] = f"{PUBLIC_SITE_URL}/?t={item['code']}"
    return {"items": items, "bot_username": BOT_USERNAME, "site_url": PUBLIC_SITE_URL}


@router.post("/admin/api/links")
async def admin_create_link(body: LinkBody, _: int = Depends(require_admin)):
    code = body.code.strip().lower()
    if not TRACKING_CODE_RE.fullmatch(code) or code.startswith(("ref_", "partner_")):
        raise HTTPException(status_code=400, detail="Некорректный или зарезервированный код ссылки")
    await db.create_tracking_link(code, body.title.strip() if body.title else None, ADMIN_ID)
    logger.info("Web admin created tracking link %s", code)
    return {
        "ok": True,
        "url": f"https://t.me/{BOT_USERNAME}?start={code}",
        "bot_url": f"https://t.me/{BOT_USERNAME}?start={code}",
        "site_url": f"{PUBLIC_SITE_URL}/?t={code}",
    }


@router.post("/admin/api/links/{code}/toggle")
async def admin_toggle_link(code: str, body: ToggleBody, _: int = Depends(require_admin)):
    if not await db.set_tracking_link_active(code, body.active):
        raise HTTPException(status_code=404, detail="Ссылка не найдена")
    return {"ok": True}


@router.get("/admin/api/discounts")
async def admin_discounts(_: int = Depends(require_admin)):
    return {"items": _plain(await db.list_discounts())}


@router.post("/admin/api/discounts")
async def admin_create_discount(body: DiscountBody, _: int = Depends(require_admin)):
    allowed_targets = {"all", "subscription", "regular", "bypass", "tariff", "traffic", "traffic_package"}
    if body.discount_type not in {"percent", "fixed"}:
        raise HTTPException(status_code=400, detail="Некорректный тип скидки")
    if body.target_type not in allowed_targets:
        raise HTTPException(status_code=400, detail="Некорректная цель скидки")
    if body.discount_type == "percent" and body.value > 95:
        raise HTTPException(status_code=400, detail="Процент скидки должен быть не больше 95")
    if body.target_type in {"tariff", "traffic_package"} and not body.target_code:
        raise HTTPException(status_code=400, detail="Укажите конкретный тариф или пакет")
    if body.target_type == "tariff" and body.target_code not in TARIFFS:
        raise HTTPException(status_code=400, detail="Такого тарифа не существует")
    if body.target_type == "traffic_package" and body.target_code not in BYPASS_TRAFFIC_PACKAGES:
        raise HTTPException(status_code=400, detail="Такого пакета трафика не существует")
    starts_at = _utc_naive(body.starts_at)
    ends_at = _utc_naive(body.ends_at)
    if ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="Дата окончания должна быть позже даты начала")
    discount = await db.create_discount(
        body.name.strip(),
        body.discount_type,
        body.value,
        body.target_type,
        body.target_code.strip() if body.target_code else None,
        starts_at,
        ends_at,
    )
    logger.info("Web admin created discount %s", body.name.strip())
    return {"ok": True, "discount": _plain(discount)}


@router.post("/admin/api/discounts/{discount_id}/toggle")
async def admin_toggle_discount(discount_id: int, body: ToggleBody, _: int = Depends(require_admin)):
    if not await db.set_discount_active(discount_id, body.active):
        raise HTTPException(status_code=404, detail="Скидка не найдена")
    return {"ok": True}


@router.delete("/admin/api/discounts/{discount_id}")
async def admin_delete_discount(discount_id: int, _: int = Depends(require_admin)):
    if not await db.delete_discount(discount_id):
        raise HTTPException(status_code=404, detail="Скидка не найдена")
    return {"ok": True}
