"""Explicit call-time adapter to the main bot's *bypass* add-on catalogue.

No config, database, Telegram or environment is loaded on import/construction.
The host must inject this loader into an explicitly enabled partner store.
It returns quotes, never permission to grant an entitlement or charge a card.
"""
from __future__ import annotations

from datetime import datetime, timezone
import inspect

from services.partner_catalog import MIN_PRICE_KOPECKS, catalog, load_main_addon_catalog


class MainPartnerAddonCatalog:
    def __init__(self, *, config_module=None, discounts_loader=None,
                 discount_calculator=None, device_option_provider=None):
        self.config_module = config_module
        self.discounts_loader = discounts_loader
        self.discount_calculator = discount_calculator
        self.device_option_provider = device_option_provider

    async def __call__(self, subscription, *, now):
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Время котировки должно содержать часовой пояс.")
        if not isinstance(subscription, dict):
            raise ValueError("Не удалось проверить подписку.")
        config = self.config_module
        if config is None:
            from importlib import import_module
            config = import_module("config")
        loader = self.discounts_loader
        if loader is None:
            import database as db
            loader = db.get_active_discounts
        calculate = self.discount_calculator
        if calculate is None:
            from services.discounts import calculate_discounted_price
            calculate = calculate_discounted_price
        discounts = loader()
        if inspect.isawaitable(discounts):
            discounts = await discounts
        until = subscription.get("expires_at")
        if isinstance(until, str):
            until = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if until is not None and (not isinstance(until, datetime) or until.tzinfo is None
                                  or until.utcoffset() is None):
            raise ValueError("Не удалось проверить срок подписки.")
        device_limit = subscription.get("device_limit")
        if type(device_limit) is not int or not 3 <= device_limit <= 10:
            raise ValueError("Не удалось проверить лимит устройств.")
        actual = {
            "generation": "v2", "is_visible": True, "is_renewable": True,
            "plan_kind": "bypass", "hwid_device_limit": device_limit,
            "subscription_until": until,
        }
        source = load_main_addon_catalog(
            config, subscription=actual, now=now.astimezone(timezone.utc),
            device_option_provider=self.device_option_provider,
        )
        traffic = {
            code: {**package, "price": str(calculate(
                package["price"], discounts, product_type="traffic", code=code,
                plan_kind="bypass",
            )["price"])}
            for code, package in source["traffic_packages"].items()
        }
        result = catalog(MIN_PRICE_KOPECKS, traffic, source["device_options"])["addons"]
        if until is None or until <= now:
            result["traffic_status"] = "expired"
            result["traffic_packages"] = []
        return result
