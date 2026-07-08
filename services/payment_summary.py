from __future__ import annotations

from datetime import datetime, timezone

import database as db
from config import BYPASS_TRAFFIC_PACKAGES, GB_BYTES, TARIFFS
from services.device_addons import device_count_text


def _format_dt(value) -> str:
    if not value:
        return "неизвестно"
    if getattr(value, "tzinfo", None) is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%d.%m.%Y")


def _subscription_name(subscription) -> str:
    if not subscription:
        return "подписка"
    plan_kind = subscription.get("plan_kind") if subscription.get("plan_kind") in {"regular", "bypass"} else "regular"
    title = "С антиглушилкой" if plan_kind == "bypass" else "Обычная"
    index = subscription.get("type_index") or subscription.get("slot_number") or "—"
    return f"{title} #{index}"


async def build_payment_success_summary(payment) -> dict:
    """Собрать короткое понятное описание успешной покупки для UI."""
    if not payment:
        return {
            "type": "unknown",
            "title": "Оплата прошла",
            "message": "Покупка активирована.",
            "toast": "Оплата прошла. Покупка активирована.",
            "subscription_id": None,
        }

    subscription_id = payment.get("subscription_id")
    subscription = await db.get_subscription_by_id(subscription_id) if subscription_id else None
    payment_kind = payment.get("payment_kind") or "subscription"

    if payment_kind == "traffic_package":
        package_code = payment.get("traffic_package_code") or payment.get("tariff_code")
        package = BYPASS_TRAFFIC_PACKAGES.get(package_code) or {}
        gb = package.get("gb") or str(package_code or "").replace("gb_", "")
        limit_gb = (subscription.get("current_period_limit_bytes") or 0) / GB_BYTES if subscription else 0
        title = f"+{gb} ГБ добавлены"
        message = (
            f"Пакет {gb} ГБ активирован для {_subscription_name(subscription)}.\n"
            f"Новый лимит периода: {limit_gb:.1f} ГБ."
        )
        return {
            "type": "traffic",
            "title": title,
            "message": message,
            "toast": f"+{gb} ГБ добавлены.",
            "subscription_id": subscription_id,
        }

    if payment_kind == "device_addon":
        purchase = await db.get_device_addon_purchase_by_invoice(payment.get("invoice_id"))
        count = int((purchase or {}).get("device_count") or payment.get("target_slot_number") or 0)
        limit = int((subscription or {}).get("hwid_device_limit") or 0)
        title = "Устройства добавлены"
        message = (
            f"+{device_count_text(count)} подключено к {_subscription_name(subscription)}.\n"
            f"Теперь лимит: до {device_count_text(limit)}.\n"
            f"Действует до {_format_dt((purchase or {}).get('valid_until') or (subscription or {}).get('subscription_until'))}."
        )
        return {
            "type": "devices",
            "title": title,
            "message": message,
            "toast": f"+{device_count_text(count)} добавлено.",
            "subscription_id": subscription_id,
        }

    tariff = TARIFFS.get(payment.get("tariff_code")) or {}
    action = "продлена" if (payment.get("payment_target") or "new") == "renew" else "активирована"
    title = "Подписка продлена" if action == "продлена" else "Подписка активирована"
    until = _format_dt((subscription or {}).get("subscription_until"))
    device_limit = int((subscription or {}).get("hwid_device_limit") or 0)
    message = (
        f"{_subscription_name(subscription)} {action}.\n"
        f"Срок действия: до {until}."
    )
    if device_limit:
        message += f"\nУстройства: до {device_count_text(device_limit)}."
    if subscription and subscription.get("plan_kind") == "bypass":
        traffic_limit = (subscription.get("current_period_limit_bytes") or subscription.get("base_traffic_bytes") or 0) / GB_BYTES
        message += f"\nТрафик антиглушилки: {traffic_limit:.1f} ГБ."
    if tariff.get("title"):
        message += f"\nТариф: {tariff['title']}."

    return {
        "type": "subscription",
        "title": title,
        "message": message,
        "toast": f"{title}.",
        "subscription_id": subscription_id,
    }
